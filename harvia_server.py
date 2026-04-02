"""
Sweat Box — Harvia sauna booking & control server.
Flask API + static SPA host.
"""
import logging
import os
from datetime import date, datetime, time, timedelta

import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory, session
from sqlalchemy.exc import IntegrityError

from harvia_client import HarviaClient
from models import DB_PATH, Booking, FamilyMember, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(days=30)

HARVIA_USERNAME = os.environ.get("HARVIA_USERNAME", "")
HARVIA_PASSWORD = os.environ.get("HARVIA_PASSWORD", "")
HARVIA_DEVICE_ID = os.environ.get("HARVIA_DEVICE_ID", "")

harvia: HarviaClient | None = None

COOLDOWN_MINUTES = 15
PREHEAT_WINDOW_MINUTES = 90
WALKUP_WINDOW_MINUTES = 120


def get_harvia() -> HarviaClient:
    global harvia
    if harvia is None:
        raise RuntimeError("Harvia client not initialised")
    return harvia


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def c_to_f(c: float) -> int:
    """Convert °C to °F, rounded to nearest integer."""
    return round(c * 9 / 5 + 32)


def f_to_c(f: float) -> int:
    """Convert °F to °C, rounded to nearest integer (Harvia API expects whole °C)."""
    return round((f - 32) * 5 / 9)


def status_with_f(status: dict) -> dict:
    out = dict(status)
    if out.get("targetTemp") is not None:
        out["targetTempF"] = c_to_f(out["targetTemp"])
    if out.get("temperature") is not None:
        out["temperatureF"] = c_to_f(out["temperature"])
    return out


def current_member(db):
    """Return the logged-in FamilyMember or None."""
    member_id = session.get("member_id")
    if not member_id:
        return None
    return db.query(FamilyMember).filter_by(id=member_id, status="approved").first()


def require_auth():
    """Return (db, member) or raise. Caller must close db."""
    db = SessionLocal()
    member = current_member(db)
    if not member:
        db.close()
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    return db, member, None


def require_admin():
    """Return (db, member) or error tuple. Caller must close db."""
    db = SessionLocal()
    member = current_member(db)
    if not member:
        db.close()
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    if not member.is_admin:
        db.close()
        return None, None, (jsonify({"error": "Admin access required"}), 403)
    return db, member, None


# ---------------------------------------------------------------------------
# Background scheduler — auto-shutoff
# ---------------------------------------------------------------------------

def check_and_auto_shutoff():
    now = datetime.now()
    today = now.date()
    current_time = now.time()

    with SessionLocal() as db:
        past_bookings = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.end_time <= current_time,
                Booking.status.in_(["scheduled", "preheating", "active"]),
            )
            .all()
        )
        for booking in past_bookings:
            booking.status = "completed"
            logger.info("Auto-completed booking %d", booking.id)

        if past_bookings:
            db.commit()
            active_booking = (
                db.query(Booking)
                .filter(
                    Booking.date == today,
                    Booking.start_time <= current_time,
                    Booking.end_time > current_time,
                    Booking.status.in_(["preheating", "active"]),
                )
                .first()
            )
            if not active_booking:
                try:
                    get_harvia().turn_off()
                    logger.info("Auto-shutoff: sauna turned off after booking ended")
                except Exception as exc:
                    logger.error("Auto-shutoff failed: %s", exc)


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_and_auto_shutoff, "interval", seconds=60, id="auto_shutoff")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    pin = str(body.get("pin", "")).strip()
    color = body.get("color", "#F97316")

    if not name:
        return err("Name is required")
    if not pin or len(pin) != 4 or not pin.isdigit():
        return err("PIN must be exactly 4 digits")

    db = SessionLocal()
    try:
        # First-ever signup is auto-approved as admin
        existing_count = db.query(FamilyMember).count()
        is_first = existing_count == 0

        pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()
        member = FamilyMember(
            name=name,
            pin_hash=pin_hash,
            status="approved" if is_first else "pending",
            is_admin=1 if is_first else 0,
            color=color,
        )
        db.add(member)
        db.commit()
        db.refresh(member)

        if is_first:
            session.permanent = True
            session["member_id"] = member.id
            return jsonify({"ok": True, "status": "approved", "member": member.to_dict()}), 201

        return jsonify({"ok": True, "status": "pending"}), 201
    finally:
        db.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    member_id = body.get("member_id")
    pin = str(body.get("pin", "")).strip()

    if not member_id or not pin:
        return err("member_id and pin are required")

    try:
        member_id = int(member_id)
    except (ValueError, TypeError):
        return err("Invalid member_id", 400)

    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member or not member.pin_hash:
            return err("Invalid credentials", 401)
        if member.status == "pending":
            return err("Your account is waiting for approval", 403)
        if member.status == "rejected":
            return err("Your account request was not approved", 403)
        if not bcrypt.checkpw(pin.encode(), member.pin_hash.encode()):
            return err("Invalid credentials", 401)

        session.permanent = True
        session["member_id"] = member.id
        return jsonify({"ok": True, "member": member.to_dict()})
    finally:
        db.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def me():
    member_id = session.get("member_id")
    if not member_id:
        return jsonify({"member": None})
    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            session.clear()
            return jsonify({"member": None})
        return jsonify({"member": member.to_dict()})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/api/admin/members")
def admin_list_members():
    db, _, error = require_admin()
    if error:
        return error
    try:
        members = db.query(FamilyMember).order_by(FamilyMember.created_at).all()
        pending_count = sum(1 for m in members if m.status == "pending")
        return jsonify({
            "members": [m.to_dict() for m in members],
            "pending_count": pending_count,
        })
    finally:
        db.close()


@app.route("/api/admin/members", methods=["POST"])
def admin_create_member():
    db, _, error = require_admin()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    color = body.get("color", "#F97316")
    default_temp = body.get("default_temp", 90)
    default_time = body.get("default_time", 60)
    if not name:
        return err("Name is required")
    try:
        member = FamilyMember(
            name=name,
            color=color,
            default_temp=int(default_temp),
            default_time=int(default_time),
            status="approved",
            is_admin=0,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return jsonify(member.to_dict()), 201
    finally:
        db.close()


@app.route("/api/admin/members/<int:member_id>/approve", methods=["POST"])
def admin_approve_member(member_id: int):
    db, _, error = require_admin()
    if error:
        return error
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        member.status = "approved"
        db.commit()
        return jsonify({"ok": True, "member": member.to_dict()})
    finally:
        db.close()


@app.route("/api/admin/members/<int:member_id>/reject", methods=["POST"])
def admin_reject_member(member_id: int):
    db, _, error = require_admin()
    if error:
        return error
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        if member.is_admin:
            return err("Cannot reject the admin account")
        member.status = "rejected"
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/admin/members/<int:member_id>", methods=["PUT"])
def admin_update_member(member_id: int):
    db, _, error = require_admin()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        if "name" in body:
            member.name = body["name"]
        if "default_temp" in body:
            member.default_temp = int(body["default_temp"])
        if "default_time" in body:
            member.default_time = int(body["default_time"])
        if "color" in body:
            member.color = body["color"]
        if "is_admin" in body:
            member.is_admin = 1 if body["is_admin"] else 0
        db.commit()
        db.refresh(member)
        return jsonify({"ok": True, "member": member.to_dict()})
    finally:
        db.close()


@app.route("/api/admin/members/<int:member_id>", methods=["DELETE"])
def admin_delete_member(member_id: int):
    db, admin_member, error = require_admin()
    if error:
        return error
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        if member.id == admin_member.id:
            return err("Cannot delete your own account")
        db.delete(member)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Family member routes (public read, auth-gated write)
# ---------------------------------------------------------------------------

@app.route("/api/members")
def list_members():
    """Returns approved members only — safe to call unauthenticated (login screen needs this)."""
    db = SessionLocal()
    try:
        members = (
            db.query(FamilyMember)
            .filter_by(status="approved")
            .order_by(FamilyMember.id)
            .all()
        )
        return jsonify([m.to_public_dict() for m in members])
    finally:
        db.close()


@app.route("/api/members/<int:member_id>", methods=["PUT"])
def update_own_member(member_id: int):
    """Authenticated users can update their own preferences."""
    db, member, error = require_auth()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        if member.id != member_id and not member.is_admin:
            return err("Cannot update another member's profile", 403)
        target = db.query(FamilyMember).filter_by(id=member_id).first()
        if not target:
            return err("Member not found", 404)
        if "default_temp" in body:
            target.default_temp = int(body["default_temp"])
        if "default_time" in body:
            target.default_time = int(body["default_time"])
        if "color" in body:
            target.color = body["color"]
        db.commit()
        db.refresh(target)
        return jsonify(target.to_dict())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sauna control routes (require auth)
# ---------------------------------------------------------------------------

@app.route("/api/sauna/status")
def sauna_status():
    # Status is readable without auth (shown on login screen too)
    try:
        status = get_harvia().get_full_status()
        return jsonify(status_with_f(status))
    except Exception as exc:
        logger.error("Status fetch failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/on", methods=["POST"])
def sauna_on():
    _, _, error = require_auth()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    target_f = body.get("targetTempF")
    target_c = body.get("targetTemp")
    if target_f is not None:
        target_c = f_to_c(float(target_f))
    elif target_c is None:
        target_c = 90
    target_c = max(40, min(110, int(target_c)))
    on_time = int(body.get("onTime", 60))
    try:
        get_harvia().turn_on(target_c, on_time)
        return jsonify({"ok": True, "targetTemp": target_c, "targetTempF": c_to_f(target_c), "onTime": on_time})
    except Exception as exc:
        logger.error("Turn on failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/off", methods=["POST"])
def sauna_off():
    _, _, error = require_auth()
    if error:
        return error
    try:
        get_harvia().turn_off()
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Turn off failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/set", methods=["POST"])
def sauna_set():
    _, _, error = require_auth()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    allowed = {"active", "targetTemp", "onTime", "maxOnTime", "maxTemp", "light", "fan", "steamEn", "targetRh"}
    payload = {k: v for k, v in body.items() if k in allowed}
    if "targetTempF" in body and "targetTemp" not in payload:
        payload["targetTemp"] = f_to_c(float(body["targetTempF"]))
    if "targetTemp" in payload:
        payload["targetTemp"] = max(40, min(110, int(payload["targetTemp"])))
    if not payload:
        return err("No valid fields provided")
    try:
        get_harvia().set_state(payload)
        return jsonify({"ok": True, "applied": payload})
    except Exception as exc:
        logger.error("Set state failed: %s", exc)
        return err(str(exc), 502)


# ---------------------------------------------------------------------------
# Preset routes
# ---------------------------------------------------------------------------

PRESETS = {
    "quick": {"targetTemp": 80, "onTime": 30},
    "standard": {"targetTemp": 90, "onTime": 60},
    "long": {"targetTemp": 85, "onTime": 90},
    "hot": {"targetTemp": 100, "onTime": 60},
    "steam": {"targetTemp": 70, "onTime": 60, "steamEn": 1, "targetRh": 30},
}


@app.route("/api/presets")
def list_presets():
    presets = []
    for name, fields in PRESETS.items():
        p = dict(fields)
        p["name"] = name
        if "targetTemp" in p:
            p["targetTempF"] = c_to_f(p["targetTemp"])
        presets.append(p)
    return jsonify(presets)


@app.route("/api/sauna/preset/<name>", methods=["POST"])
def apply_preset(name: str):
    _, _, error = require_auth()
    if error:
        return error
    if name not in PRESETS:
        return err(f"Unknown preset '{name}'", 404)
    payload = {**PRESETS[name], "active": 1}
    try:
        get_harvia().set_state(payload)
        return jsonify({"ok": True, "preset": name, "applied": payload})
    except Exception as exc:
        logger.error("Preset apply failed: %s", exc)
        return err(str(exc), 502)


# ---------------------------------------------------------------------------
# Booking routes (require auth)
# ---------------------------------------------------------------------------

@app.route("/api/bookings")
def list_bookings():
    date_str = request.args.get("date")
    db = SessionLocal()
    try:
        q = db.query(Booking).filter(Booking.status != "cancelled")
        if date_str:
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                return err("Invalid date format, use YYYY-MM-DD")
            q = q.filter(Booking.date == d)
        bookings = q.order_by(Booking.date, Booking.start_time).all()
        return jsonify([b.to_dict() for b in bookings])
    finally:
        db.close()


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    body = request.get_json(silent=True) or {}
    required = ("date", "start_time", "end_time")
    for field in required:
        if field not in body:
            return err(f"Missing field: {field}")

    try:
        booking_date = date.fromisoformat(body["date"])
        start = time.fromisoformat(body["start_time"])
        end = time.fromisoformat(body["end_time"])
    except ValueError as exc:
        return err(f"Invalid date/time: {exc}")

    if end <= start:
        return err("end_time must be after start_time")

    target_temp_f = body.get("target_temp_f")
    target_temp = body.get("target_temp")
    if target_temp_f is not None:
        target_temp = f_to_c(float(target_temp_f))
    if target_temp is not None:
        target_temp = max(40, min(110, int(target_temp)))

    on_time = body.get("on_time") or int(
        (datetime.combine(booking_date, end) - datetime.combine(booking_date, start)).seconds / 60
    )

    db = SessionLocal()
    try:
        cooldown_start = (
            datetime.combine(booking_date, start) - timedelta(minutes=COOLDOWN_MINUTES)
        ).time()
        cooldown_end = (
            datetime.combine(booking_date, end) + timedelta(minutes=COOLDOWN_MINUTES)
        ).time()

        overlap = (
            db.query(Booking)
            .filter(
                Booking.date == booking_date,
                Booking.status != "cancelled",
                Booking.start_time < cooldown_end,
                Booking.end_time > cooldown_start,
            )
            .first()
        )
        if overlap:
            return err(
                f"Overlaps with existing booking ({overlap.start_time.strftime('%H:%M')}–"
                f"{overlap.end_time.strftime('%H:%M')}) including {COOLDOWN_MINUTES}-min cooldown"
            )

        booking = Booking(
            member_id=member.id,
            date=booking_date,
            start_time=start,
            end_time=end,
            target_temp=target_temp or member.default_temp,
            on_time=on_time,
            status="scheduled",
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)
        return jsonify(booking.to_dict()), 201
    except IntegrityError:
        db.rollback()
        return err("Database error creating booking", 500)
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>", methods=["DELETE"])
def cancel_booking(booking_id: int):
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.member_id != member.id and not member.is_admin:
            return err("Cannot cancel someone else's booking", 403)
        if booking.status == "completed":
            return err("Cannot cancel a completed booking")
        booking.status = "cancelled"
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>/preheat", methods=["POST"])
def preheat_booking(booking_id: int):
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.member_id != member.id and not member.is_admin:
            return err("Cannot preheat someone else's booking", 403)
        if booking.status != "scheduled":
            return err(f"Cannot preheat a booking with status '{booking.status}'")

        now = datetime.now()
        booking_start = datetime.combine(booking.date, booking.start_time)
        minutes_until = (booking_start - now).total_seconds() / 60

        if minutes_until > PREHEAT_WINDOW_MINUTES:
            return err(
                f"Too early — booking starts in {int(minutes_until)} min "
                f"(preheat window is {PREHEAT_WINDOW_MINUTES} min)"
            )
        if minutes_until < 0:
            return err("Booking has already passed")

        try:
            get_harvia().turn_on(booking.target_temp or 90, booking.on_time or 60)
        except Exception as exc:
            logger.error("Preheat API call failed: %s", exc)
            return err(f"Harvia API error: {exc}", 502)

        booking.status = "preheating"
        db.commit()
        return jsonify({"ok": True, "status": "preheating"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SPA catch-all
# ---------------------------------------------------------------------------

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Startup — runs at import time so gunicorn picks it up too
# ---------------------------------------------------------------------------

def _startup():
    global harvia

    init_db()
    logger.info("Database initialised at %s", DB_PATH)

    if HARVIA_USERNAME and HARVIA_PASSWORD and HARVIA_DEVICE_ID:
        harvia = HarviaClient(HARVIA_USERNAME, HARVIA_PASSWORD, HARVIA_DEVICE_ID)
        try:
            harvia.init()
            logger.info("Harvia client ready")
        except Exception as exc:
            logger.error("Harvia init failed — sauna control will be unavailable: %s", exc)
            harvia = None
    else:
        logger.warning("Harvia credentials not configured — sauna control disabled")

    if not scheduler.running:
        scheduler.start()
        logger.info("Background scheduler started")


_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
