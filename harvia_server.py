"""
Sweat Box — Harvia sauna booking & control server.
Flask API + static SPA host.
"""
import json
import logging
import os
from datetime import date, datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory
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

HARVIA_USERNAME = os.environ.get("HARVIA_USERNAME", "")
HARVIA_PASSWORD = os.environ.get("HARVIA_PASSWORD", "")
HARVIA_DEVICE_ID = os.environ.get("HARVIA_DEVICE_ID", "")

harvia: HarviaClient | None = None

COOLDOWN_MINUTES = 15  # minimum gap between consecutive bookings
PREHEAT_WINDOW_MINUTES = 90  # allow preheat within this many minutes of start time
WALKUP_WINDOW_MINUTES = 120  # allow controls if no booking in next N minutes


def get_harvia() -> HarviaClient:
    global harvia
    if harvia is None:
        raise RuntimeError("Harvia client not initialised")
    return harvia


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_session():
    """Context manager for DB sessions outside Flask request context."""
    return SessionLocal()


# ---------------------------------------------------------------------------
# Temperature helpers
# ---------------------------------------------------------------------------

def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def f_to_c(f: float) -> int:
    return round((f - 32) * 5 / 9)


def status_with_f(status: dict) -> dict:
    """Add Fahrenheit equivalents to a status dict."""
    out = dict(status)
    if out.get("targetTemp") is not None:
        out["targetTempF"] = c_to_f(out["targetTemp"])
    if out.get("temperature") is not None:
        out["temperatureF"] = c_to_f(out["temperature"])
    return out


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

def check_and_auto_shutoff():
    """Run every 60 s: complete past bookings and turn off the sauna if needed."""
    now = datetime.now()
    today = now.date()
    current_time = now.time()

    with db_session() as db:
        # Find bookings that ended in the past and are still active/preheating
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

            # Check whether any current or upcoming booking is active
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
                # No active booking — turn sauna off
                try:
                    get_harvia().turn_off()
                    logger.info("Auto-shutoff: sauna turned off after booking ended")
                except Exception as exc:
                    logger.error("Auto-shutoff failed: %s", exc)


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_and_auto_shutoff, "interval", seconds=60, id="auto_shutoff")


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------------------
# Sauna control routes
# ---------------------------------------------------------------------------

@app.route("/api/sauna/status")
def sauna_status():
    try:
        status = get_harvia().get_full_status()
        return jsonify(status_with_f(status))
    except Exception as exc:
        logger.error("Status fetch failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/on", methods=["POST"])
def sauna_on():
    body = request.get_json(silent=True) or {}
    target_f = body.get("targetTempF")
    target_c = body.get("targetTemp")
    if target_f is not None:
        target_c = f_to_c(float(target_f))
    elif target_c is None:
        target_c = 90  # default

    target_c = max(40, min(110, int(target_c)))
    on_time = int(body.get("onTime", 60))

    try:
        get_harvia().turn_on(target_c, on_time)
        return jsonify({"ok": True, "targetTemp": target_c, "onTime": on_time})
    except Exception as exc:
        logger.error("Turn on failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/off", methods=["POST"])
def sauna_off():
    try:
        get_harvia().turn_off()
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Turn off failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/set", methods=["POST"])
def sauna_set():
    body = request.get_json(silent=True) or {}
    allowed = {"active", "targetTemp", "onTime", "maxOnTime", "maxTemp", "light", "fan", "steamEn", "targetRh"}
    payload = {k: v for k, v in body.items() if k in allowed}

    # Convert °F targetTemp if provided as targetTempF
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
    if name not in PRESETS:
        return err(f"Unknown preset '{name}'", 404)
    payload = dict(PRESETS[name])
    payload["active"] = 1
    try:
        get_harvia().set_state(payload)
        return jsonify({"ok": True, "preset": name, "applied": payload})
    except Exception as exc:
        logger.error("Preset apply failed: %s", exc)
        return err(str(exc), 502)


# ---------------------------------------------------------------------------
# Booking routes
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
    body = request.get_json(silent=True) or {}
    required = ("member_id", "date", "start_time", "end_time")
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
        # Verify member exists
        member = db.query(FamilyMember).filter_by(id=int(body["member_id"])).first()
        if not member:
            return err("Member not found", 404)

        # Check for overlapping bookings (including cooldown)
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
            member_id=int(body["member_id"]),
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
    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.status in ("completed",):
            return err("Cannot cancel a completed booking")
        booking.status = "cancelled"
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>/preheat", methods=["POST"])
def preheat_booking(booking_id: int):
    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.status not in ("scheduled",):
            return err(f"Cannot preheat a booking with status '{booking.status}'")

        now = datetime.now()
        booking_start = datetime.combine(booking.date, booking.start_time)
        minutes_until = (booking_start - now).total_seconds() / 60

        if minutes_until > PREHEAT_WINDOW_MINUTES:
            return err(
                f"Too early to preheat — booking starts in {int(minutes_until)} min "
                f"(preheat window is {PREHEAT_WINDOW_MINUTES} min)"
            )
        if minutes_until < -5:
            return err("Booking has already started or passed")

        try:
            get_harvia().turn_on(
                booking.target_temp or 90,
                booking.on_time or 60,
            )
        except Exception as exc:
            logger.error("Preheat API call failed: %s", exc)
            return err(f"Harvia API error: {exc}", 502)

        booking.status = "preheating"
        db.commit()
        return jsonify({"ok": True, "status": "preheating"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Family member routes
# ---------------------------------------------------------------------------

@app.route("/api/members")
def list_members():
    db = SessionLocal()
    try:
        members = db.query(FamilyMember).order_by(FamilyMember.id).all()
        return jsonify([m.to_dict() for m in members])
    finally:
        db.close()


@app.route("/api/members", methods=["POST"])
def create_member():
    body = request.get_json(silent=True) or {}
    if not body.get("name"):
        return err("Missing field: name")

    db = SessionLocal()
    try:
        member = FamilyMember(
            name=body["name"],
            default_temp=int(body.get("default_temp", 90)),
            default_time=int(body.get("default_time", 60)),
            color=body.get("color", "#F97316"),
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return jsonify(member.to_dict()), 201
    except IntegrityError:
        db.rollback()
        return err("Database error creating member", 500)
    finally:
        db.close()


@app.route("/api/members/<int:member_id>", methods=["PUT"])
def update_member(member_id: int):
    body = request.get_json(silent=True) or {}
    db = SessionLocal()
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
        db.commit()
        db.refresh(member)
        return jsonify(member.to_dict())
    finally:
        db.close()


@app.route("/api/members/<int:member_id>", methods=["DELETE"])
def delete_member(member_id: int):
    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        db.delete(member)
        db.commit()
        return jsonify({"ok": True})
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

    # Initialise DB
    init_db()
    logger.info("Database initialised at %s", DB_PATH)

    # Initialise Harvia client
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

    # Start background scheduler (guard against double-start in debug reloader)
    if not scheduler.running:
        scheduler.start()
        logger.info("Background scheduler started")


_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
