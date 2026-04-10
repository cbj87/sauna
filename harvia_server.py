"""
Sweat Box — Harvia sauna booking & control server.
Flask API + static SPA host.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import secrets
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory, session
from sqlalchemy.exc import IntegrityError

from harvia_client import HarviaClient
from models import DB_PATH, Booking, ControlLog, FamilyMember, Preset, PushSubscription, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", static_url_path="")

_secret_key = os.environ.get("APP_SECRET_KEY", "dev-secret-change-me")
if _secret_key == "dev-secret-change-me":
    import sys
    if os.environ.get("FLASK_ENV") == "production" or os.environ.get("RAILWAY_ENVIRONMENT"):
        logger.critical(
            "APP_SECRET_KEY is still the default dev value — refusing to start in production. "
            "Set a long random string in your environment variables."
        )
        sys.exit(1)
    else:
        logger.warning("APP_SECRET_KEY is using the insecure default. Set it before deploying.")
app.secret_key = _secret_key
app.permanent_session_lifetime = timedelta(days=30)

HARVIA_USERNAME = os.environ.get("HARVIA_USERNAME", "")
HARVIA_PASSWORD = os.environ.get("HARVIA_PASSWORD", "")
HARVIA_DEVICE_ID = os.environ.get("HARVIA_DEVICE_ID", "")

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "sweatbox@localhost")

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Australia/Sydney")

def app_now() -> datetime:
    """Current local time as a naive datetime in the configured APP_TIMEZONE."""
    return datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)


harvia: HarviaClient | None = None

COOLDOWN_MINUTES = 15
PREHEAT_WINDOW_MINUTES = 90
WALKUP_WINDOW_MINUTES = 120

# ---------------------------------------------------------------------------
# Login rate limiting (in-memory, per IP)
# ---------------------------------------------------------------------------
LOGIN_MAX_ATTEMPTS = 10        # max failures before lockout
LOGIN_WINDOW_SECONDS = 900     # 15-minute sliding window
LOGIN_LOCKOUT_SECONDS = 900    # lockout duration after max failures

_login_attempts: dict[str, list[float]] = collections.defaultdict(list)
_login_lock = threading.Lock()

# Serialises the overlap-check + insert so two simultaneous requests can't
# both pass the overlap check before either has committed.
_booking_lock = threading.Lock()


def _get_client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is currently locked out."""
    now = datetime.now().timestamp()
    with _login_lock:
        attempts = _login_attempts[ip]
        # Discard attempts outside the window
        _login_attempts[ip] = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
        return len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> int:
    """Record a failed login and return the remaining attempts before lockout."""
    now = datetime.now().timestamp()
    with _login_lock:
        _login_attempts[ip].append(now)
        return max(0, LOGIN_MAX_ATTEMPTS - len(_login_attempts[ip]))


def _clear_attempts(ip: str) -> None:
    with _login_lock:
        _login_attempts.pop(ip, None)


def _log_sauna_action(
    member_id: int | None,
    member_name: str | None,
    action: str,
    target_temp: int | None = None,
    on_time: int | None = None,
    preset_name: str | None = None,
    notes: str | None = None,
) -> None:
    """Write a control log entry in its own session — never raises."""
    try:
        with SessionLocal() as db:
            db.add(ControlLog(
                member_id=member_id,
                member_name=member_name,
                action=action,
                target_temp=target_temp,
                on_time=on_time,
                preset_name=preset_name,
                notes=notes,
            ))
            db.commit()
    except Exception as exc:
        logger.error("Failed to write control log: %s", exc)


def _send_push(sub_info: dict, payload: dict):
    """Send a Web Push notification.

    Returns True on success, an HTTP status code int on push-service error, or False on other failure.
    A 410 response means the subscription has expired and should be deleted.
    """
    if not VAPID_PRIVATE_KEY:
        return False
    try:
        from pywebpush import WebPushException, webpush
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL}"},
        )
        return True
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            code = getattr(response, "status_code", None)
            logger.warning("Push notification HTTP error %s", code)
            return code
        logger.error("Push notification failed: %s", exc)
        return False


def _notify_admins_push(payload: dict, pref_key: str | None = None) -> None:
    """Send a push notification to admin subscribers who have opted in.

    pref_key: if provided, only admins whose notification_prefs has that key set to true
    (or have no prefs set, meaning all defaults are on) will receive the notification.
    """
    if not VAPID_PRIVATE_KEY:
        return
    try:
        with SessionLocal() as db:
            admins = db.query(FamilyMember).filter_by(is_admin=1, status="approved").all()
            dead = []
            for admin in admins:
                if pref_key is not None:
                    prefs = admin.get_notification_prefs()
                    if not prefs.get(pref_key, True):  # default True if not set
                        continue
                subs = db.query(PushSubscription).filter_by(member_id=admin.id).all()
                for sub in subs:
                    result = _send_push(
                        {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                        payload,
                    )
                    if result == 410:
                        dead.append(sub.endpoint)
            for ep in dead:
                db.query(PushSubscription).filter_by(endpoint=ep).delete()
            if dead:
                db.commit()
    except Exception as exc:
        logger.error("Failed to send admin push notifications: %s", exc)


def _notify_member_push(member_id: int, payload: dict) -> None:
    """Send a push notification to a specific member's subscribers. Never raises."""
    if not VAPID_PRIVATE_KEY:
        return
    try:
        with SessionLocal() as db:
            subs = db.query(PushSubscription).filter_by(member_id=member_id).all()
            dead = []
            for sub in subs:
                result = _send_push(
                    {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    payload,
                )
                if result == 410:
                    dead.append(sub.endpoint)
            for ep in dead:
                db.query(PushSubscription).filter_by(endpoint=ep).delete()
            if dead:
                db.commit()
    except Exception as exc:
        logger.error("Failed to send member push notification: %s", exc)


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


def require_auth_or_pending():
    """Like require_auth but also accepts pending members (used for push subscribe)."""
    db = SessionLocal()
    member_id = session.get("member_id")
    if not member_id:
        db.close()
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    member = db.query(FamilyMember).filter_by(id=member_id).first()
    if not member or member.status == "rejected":
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
# CSRF protection
# ---------------------------------------------------------------------------

def _generate_csrf_token() -> str:
    """Return the session's CSRF token, creating one if it doesn't exist yet."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


# Endpoints that don't require a CSRF token (pre-authentication flows).
_CSRF_EXEMPT = {"login", "signup", "static"}


@app.before_request
def csrf_protect():
    """Reject state-changing requests that lack a valid CSRF token."""
    if request.method not in ("POST", "DELETE", "PUT", "PATCH"):
        return None
    if request.endpoint in _CSRF_EXEMPT:
        return None
    token = request.headers.get("X-CSRF-Token", "")
    if not token or token != session.get("csrf_token"):
        return jsonify({"error": "CSRF token missing or invalid"}), 403


# ---------------------------------------------------------------------------
# Background scheduler — auto-shutoff
# ---------------------------------------------------------------------------

def check_and_auto_shutoff():
    now = app_now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    current_time = now.time()

    with SessionLocal() as db:
        # ── Same-day bookings: start has passed, end hasn't yet → active ──────
        newly_active = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.start_time <= current_time,
                Booking.end_time > current_time,
                Booking.end_time > Booking.start_time,   # normal (non-midnight-spanning)
                Booking.status.in_(["scheduled", "preheating"]),
            )
            .all()
        )
        # Midnight-spanning bookings from yesterday still running now
        midnight_still_active = (
            db.query(Booking)
            .filter(
                Booking.date == yesterday,
                Booking.end_time < Booking.start_time,   # midnight-spanning marker
                Booking.end_time > current_time,         # hasn't ended yet today
                Booking.status.in_(["scheduled", "preheating"]),
            )
            .all()
        )
        newly_active.extend(midnight_still_active)
        for booking in newly_active:
            booking.status = "active"
            logger.info("Booking %d is now active", booking.id)

        # ── Same-day bookings whose end has passed → completed ────────────────
        past_bookings = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.end_time <= current_time,
                Booking.end_time > Booking.start_time,   # normal bookings only
                Booking.status.in_(["scheduled", "preheating", "active"]),
            )
            .all()
        )
        # Midnight-spanning bookings from yesterday that have now ended
        past_midnight = (
            db.query(Booking)
            .filter(
                Booking.date == yesterday,
                Booking.end_time < Booking.start_time,
                Booking.end_time <= current_time,
                Booking.status.in_(["scheduled", "preheating", "active"]),
            )
            .all()
        )
        past_bookings.extend(past_midnight)

        # Yesterday's normal bookings still stuck as active/preheating after a
        # server restart — they are unambiguously in the past, complete them all.
        past_yesterday_stranded = (
            db.query(Booking)
            .filter(
                Booking.date == yesterday,
                Booking.end_time > Booking.start_time,   # normal (not midnight-spanning)
                Booking.status.in_(["scheduled", "preheating", "active"]),
            )
            .all()
        )
        past_bookings.extend(past_yesterday_stranded)
        for booking in past_bookings:
            booking.status = "completed"
            logger.info("Auto-completed booking %d", booking.id)

        if newly_active or past_bookings:
            db.commit()

        if past_bookings:
            # Turn off sauna only if no other booking is still running
            still_today = (
                db.query(Booking)
                .filter(
                    Booking.date == today,
                    Booking.start_time <= current_time,
                    Booking.end_time > current_time,
                    Booking.status == "active",
                )
                .first()
            )
            still_midnight = (
                db.query(Booking)
                .filter(
                    Booking.date == yesterday,
                    Booking.end_time < Booking.start_time,
                    Booking.end_time > current_time,
                    Booking.status == "active",
                )
                .first()
            )
            if not still_today and not still_midnight:
                try:
                    get_harvia().turn_off()
                    logger.info("Auto-shutoff: sauna turned off after booking ended")
                except Exception as exc:
                    logger.error("Auto-shutoff failed: %s", exc)


def check_preheat_reminders():
    """Send push notifications for upcoming bookings when it's time to preheat."""
    if not VAPID_PRIVATE_KEY:
        return

    now = app_now()
    today = now.date()

    with SessionLocal() as db:
        pending = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.status == "scheduled",
                Booking.preheat_notified_at.is_(None),
            )
            .all()
        )

        for booking in pending:
            # Notify 35 min before start (30 min to heat up + 5 min to react).
            # We intentionally do NOT use booking.on_time here — that is the
            # session *duration*, not the preheat lead time.
            NOTIFY_BEFORE_MINUTES = 35
            start_dt = datetime.combine(booking.date, booking.start_time)
            notify_at = start_dt - timedelta(minutes=NOTIFY_BEFORE_MINUTES)

            if now < notify_at or now >= start_dt:
                continue

            # Check preheat pref — default on if not set
            member_prefs = booking.member.get_notification_prefs() if booking.member else {}
            if not member_prefs.get("preheat", True):
                booking.preheat_notified_at = now
                db.commit()
                continue

            subs = db.query(PushSubscription).filter_by(member_id=booking.member_id).all()
            # Mark notified even if no subscriptions so we don't re-check every minute
            booking.preheat_notified_at = now

            dead_endpoints = []
            for sub in subs:
                start_12h = booking.start_time.strftime("%I:%M %p").lstrip("0")
                result = _send_push(
                    {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    {
                        "title": "🔥 Time to preheat!",
                        "body": f"Your session starts at {start_12h}. Head over and start preheating now.",
                        "tag": f"preheat-{booking.id}",
                        "url": "/",
                        "bookingId": booking.id,
                    },
                )
                if result is True:
                    logger.info(
                        "Preheat reminder sent for booking %d (member_id=%d)",
                        booking.id,
                        booking.member_id,
                    )
                elif result == 410:
                    dead_endpoints.append(sub.endpoint)

            for ep in dead_endpoints:
                db.query(PushSubscription).filter_by(endpoint=ep).delete()

            db.commit()


def refresh_harvia_token():
    """Proactively refresh the Harvia Cognito token every 30 min so it never
    expires mid-request.  Runs independently of any user activity."""
    if harvia:
        harvia.proactive_refresh()


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_and_auto_shutoff,  "interval", seconds=60,  id="auto_shutoff")
scheduler.add_job(check_preheat_reminders, "interval", seconds=60,  id="preheat_reminders")
scheduler.add_job(refresh_harvia_token,    "interval", minutes=30,  id="token_refresh")


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
            return jsonify({"ok": True, "status": "approved", "member": member.to_dict(), "csrf_token": _generate_csrf_token()}), 201

        session.permanent = True
        session["member_id"] = member.id
        csrf = _generate_csrf_token()
        _notify_admins_push({
            "title": "👤 New signup",
            "body": f"{name} wants to join — approve them in the Admin tab.",
            "tag": f"signup-{member.id}",
            "url": "/",
        }, pref_key="signup")
        return jsonify({"ok": True, "status": "pending", "csrf_token": csrf}), 201
    finally:
        db.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    ip = _get_client_ip()
    if _check_rate_limit(ip):
        logger.warning("Login rate limit hit for IP %s", ip)
        return err("Too many failed attempts — please wait 15 minutes before trying again.", 429)

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
            _record_failed_attempt(ip)
            return err("Invalid credentials", 401)
        if member.status == "pending":
            return err("Your account is waiting for approval", 403)
        if member.status == "rejected":
            return err("Your account request was not approved", 403)
        if not bcrypt.checkpw(pin.encode(), member.pin_hash.encode()):
            remaining = _record_failed_attempt(ip)
            msg = "Invalid PIN"
            if remaining <= 3:
                msg += f" — {remaining} attempt{'s' if remaining != 1 else ''} remaining before lockout"
            return err(msg, 401)

        _clear_attempts(ip)
        session.permanent = True
        session["member_id"] = member.id
        return jsonify({"ok": True, "member": member.to_dict(), "csrf_token": _generate_csrf_token()})
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
        return jsonify({"member": member.to_dict(), "csrf_token": _generate_csrf_token()})
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
    body = request.get_json(silent=True) or {}
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        member.status = "approved"
        if "max_temp" in body:
            raw = body["max_temp"]
            member.max_temp = int(raw) if raw is not None else None
        db.commit()
        approved_id = member.id
        _notify_member_push(approved_id, {
            "title": "✅ You're approved!",
            "body": "Your Sweat Box account has been approved. Log in to get started.",
            "tag": "account-approved",
            "url": "/",
        })
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
        if "max_temp" in body:
            raw = body["max_temp"]
            member.max_temp = int(raw) if raw is not None else None
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
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        target_f = body.get("targetTempF")
        target_c = body.get("targetTemp")
        if target_f is not None:
            target_c = f_to_c(float(target_f))
        elif target_c is None:
            target_c = 90
        target_c = max(40, min(110, int(target_c)))
        # Enforce per-member temperature limit
        if member.max_temp is not None and target_c > member.max_temp:
            return err(
                f"Temperature exceeds your limit of {c_to_f(member.max_temp)}°F ({member.max_temp}°C)", 400
            )
        on_time = int(body.get("onTime", 60))
        mid, mname = member.id, member.name
    finally:
        db.close()
    try:
        get_harvia().turn_on(target_c, on_time)
        _log_sauna_action(mid, mname, "on", target_temp=target_c, on_time=on_time)
        return jsonify({"ok": True, "targetTemp": target_c, "targetTempF": c_to_f(target_c), "onTime": on_time})
    except Exception as exc:
        logger.error("Turn on failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/off", methods=["POST"])
def sauna_off():
    db, member, error = require_auth()
    if error:
        return error
    try:
        mid, mname = member.id, member.name
    finally:
        db.close()
    try:
        get_harvia().turn_off()
        _log_sauna_action(mid, mname, "off")
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Turn off failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/set", methods=["POST"])
def sauna_set():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        allowed = {"active", "targetTemp", "onTime", "maxOnTime", "maxTemp", "light", "fan", "steamEn", "targetRh"}
        payload = {k: v for k, v in body.items() if k in allowed}
        if "targetTempF" in body and "targetTemp" not in payload:
            payload["targetTemp"] = f_to_c(float(body["targetTempF"]))
        if "targetTemp" in payload:
            payload["targetTemp"] = max(40, min(110, int(payload["targetTemp"])))
            # Enforce per-member temperature limit
            if member.max_temp is not None and payload["targetTemp"] > member.max_temp:
                return err(
                    f"Temperature exceeds your limit of {c_to_f(member.max_temp)}°F ({member.max_temp}°C)", 400
                )
        if not payload:
            return err("No valid fields provided")
        mid, mname = member.id, member.name
    finally:
        db.close()
    try:
        log_target = payload.get("targetTemp")
        log_on_time = payload.get("onTime")
        extra = {k: v for k, v in payload.items() if k not in ("targetTemp", "onTime", "active")}
        notes = json.dumps(extra) if extra else None
        get_harvia().set_state(payload)
        _log_sauna_action(mid, mname, "set", target_temp=log_target, on_time=log_on_time, notes=notes)
        return jsonify({"ok": True, "applied": payload})
    except Exception as exc:
        logger.error("Set state failed: %s", exc)
        return err(str(exc), 502)


# ---------------------------------------------------------------------------
# Preset routes
# ---------------------------------------------------------------------------

_DEFAULT_PRESETS = [
    {"name": "quick",    "label": "Quick Heat",   "target_temp": 80,  "on_time": 30,  "sort_order": 0},
    {"name": "standard", "label": "Standard",     "target_temp": 90,  "on_time": 60,  "sort_order": 1},
    {"name": "long",     "label": "Long Session", "target_temp": 85,  "on_time": 90,  "sort_order": 2},
    {"name": "hot",      "label": "Hot & Fast",   "target_temp": 100, "on_time": 60,  "sort_order": 3},
    {"name": "steam",    "label": "Steam",        "target_temp": 70,  "on_time": 60,  "steam_en": 1, "target_rh": 30, "sort_order": 4},
]


def _seed_presets():
    with SessionLocal() as db:
        if db.query(Preset).count() == 0:
            for p in _DEFAULT_PRESETS:
                db.add(Preset(**p))
            db.commit()
            logger.info("Seeded %d default presets", len(_DEFAULT_PRESETS))


@app.route("/api/presets")
def list_presets():
    db = SessionLocal()
    try:
        presets = db.query(Preset).order_by(Preset.sort_order).all()
        return jsonify([p.to_dict() for p in presets])
    finally:
        db.close()


@app.route("/api/admin/presets/<name>", methods=["PUT"])
def admin_update_preset(name: str):
    db, _, error = require_admin()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        preset = db.query(Preset).filter_by(name=name).first()
        if not preset:
            return err("Preset not found", 404)
        if "label" in body:
            preset.label = str(body["label"]).strip() or preset.label
        if "target_temp" in body:
            preset.target_temp = max(40, min(110, int(body["target_temp"])))
        if "on_time" in body:
            preset.on_time = max(5, min(240, int(body["on_time"])))
        db.commit()
        db.refresh(preset)
        return jsonify({"ok": True, "preset": preset.to_dict()})
    finally:
        db.close()


@app.route("/api/admin/presets/<name>", methods=["DELETE"])
def admin_delete_preset(name: str):
    db, _, error = require_admin()
    if error:
        return error
    try:
        preset = db.query(Preset).filter_by(name=name).first()
        if not preset:
            return err("Preset not found", 404)
        db.delete(preset)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/sauna/preset/<name>", methods=["POST"])
def apply_preset(name: str):
    db, member, error = require_auth()
    if error:
        return error
    try:
        preset = db.query(Preset).filter_by(name=name).first()
        if not preset:
            return err(f"Unknown preset '{name}'", 404)
        if member.max_temp is not None and preset.target_temp > member.max_temp:
            return err(
                f"Preset temperature ({c_to_f(preset.target_temp)}°F) exceeds your limit of "
                f"{c_to_f(member.max_temp)}°F ({member.max_temp}°C)", 400
            )
        mid, mname = member.id, member.name
        p_temp, p_time, p_steam, p_rh, p_name = (
            preset.target_temp, preset.on_time, preset.steam_en, preset.target_rh, preset.name
        )
    finally:
        db.close()
    payload = {"targetTemp": p_temp, "onTime": p_time, "active": 1}
    if p_steam:
        payload["steamEn"] = 1
        if p_rh:
            payload["targetRh"] = p_rh
    try:
        get_harvia().set_state(payload)
        _log_sauna_action(mid, mname, "preset", target_temp=p_temp, on_time=p_time, preset_name=p_name)
        return jsonify({"ok": True, "preset": p_name, "applied": payload})
    except Exception as exc:
        logger.error("Preset apply failed: %s", exc)
        return err(str(exc), 502)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/admin/harvia-stats")
def harvia_stats():
    _, _, error = require_admin()
    if error:
        return error
    if not harvia:
        return jsonify({"error": "Harvia client not initialised"}), 503
    return jsonify(harvia.get_stats())


# ---------------------------------------------------------------------------
# Control log (admin only)
# ---------------------------------------------------------------------------

@app.route("/api/admin/control_log")
def admin_control_log():
    db, _, error = require_admin()
    if error:
        return error
    try:
        logs = (
            db.query(ControlLog)
            .order_by(ControlLog.created_at.desc())
            .limit(200)
            .all()
        )
        return jsonify([l.to_dict() for l in logs])
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Push notification routes
# ---------------------------------------------------------------------------

@app.route("/api/members/<int:member_id>/notification-prefs", methods=["PUT"])
def update_notification_prefs(member_id: int):
    """Update notification preferences for the authenticated member (or admin for any)."""
    db, member, error = require_auth()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        if member.id != member_id and not member.is_admin:
            return err("Cannot update another member's preferences", 403)
        target = db.query(FamilyMember).filter_by(id=member_id).first()
        if not target:
            return err("Member not found", 404)
        # Merge new prefs over existing ones
        prefs = target.get_notification_prefs()
        for key in ("preheat", "signup", "booking", "approval"):
            if key in body:
                prefs[key] = bool(body[key])
        target.notification_prefs = json.dumps(prefs)
        db.commit()
        return jsonify({"ok": True, "notification_prefs": prefs})
    finally:
        db.close()


@app.route("/api/push/vapid-key")
def push_vapid_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    db, member, error = require_auth_or_pending()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        endpoint = body.get("endpoint", "").strip()
        p256dh = body.get("p256dh", "").strip()
        auth_key = body.get("auth", "").strip()
        if not endpoint or not p256dh or not auth_key:
            return err("endpoint, p256dh, and auth are required")

        existing = db.query(PushSubscription).filter_by(endpoint=endpoint).first()
        if existing:
            existing.member_id = member.id
            existing.p256dh = p256dh
            existing.auth = auth_key
        else:
            db.add(PushSubscription(
                member_id=member.id,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth_key,
            ))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        endpoint = body.get("endpoint", "").strip()
        if endpoint:
            db.query(PushSubscription).filter_by(
                member_id=member.id, endpoint=endpoint
            ).delete()
        else:
            db.query(PushSubscription).filter_by(member_id=member.id).delete()
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/push/test", methods=["POST"])
def push_test():
    """Send a test push notification to the current user. Useful for verifying the setup end-to-end."""
    db, member, error = require_auth()
    if error:
        return error
    try:
        if not VAPID_PRIVATE_KEY:
            return err("VAPID_PRIVATE_KEY is not configured on the server")

        subs = db.query(PushSubscription).filter_by(member_id=member.id).all()
        if not subs:
            return err("No push subscriptions found for your account — enable notifications first")

        sent, failed, dead = 0, 0, []
        for sub in subs:
            result = _send_push(
                {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                {
                    "title": "🛖 Sweat Box test",
                    "body": f"Hey {member.name}! Push notifications are working.",
                    "tag": "push-test",
                    "url": "/",
                },
            )
            if result is True:
                sent += 1
            elif result == 410:
                dead.append(sub.endpoint)
                failed += 1
            else:
                failed += 1

        for ep in dead:
            db.query(PushSubscription).filter_by(endpoint=ep).delete()
        if dead:
            db.commit()

        if sent == 0:
            return err(f"Notification delivery failed on all {failed} subscription(s) — check server logs"), 502
        return jsonify({"ok": True, "sent": sent, "failed": failed})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Booking routes (require auth)
# ---------------------------------------------------------------------------

@app.route("/api/bookings")
def list_bookings():
    date_str      = request.args.get("date")
    date_from_str = request.args.get("date_from")
    date_to_str   = request.args.get("date_to")
    db = SessionLocal()
    try:
        q = db.query(Booking).filter(Booking.status != "cancelled")
        if date_str:
            try:
                d = date.fromisoformat(date_str)
            except ValueError:
                return err("Invalid date format, use YYYY-MM-DD")
            q = q.filter(Booking.date == d)
        else:
            if date_from_str:
                try:
                    q = q.filter(Booking.date >= date.fromisoformat(date_from_str))
                except ValueError:
                    return err("Invalid date_from format, use YYYY-MM-DD")
            if date_to_str:
                try:
                    q = q.filter(Booking.date <= date.fromisoformat(date_to_str))
                except ValueError:
                    return err("Invalid date_to format, use YYYY-MM-DD")
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

    # Allow end < start for midnight-spanning bookings (e.g. 23:00–01:00 next day)
    if end == start:
        return err("end_time must differ from start_time")

    target_temp_f = body.get("target_temp_f")
    target_temp = body.get("target_temp")
    if target_temp_f is not None:
        target_temp = f_to_c(float(target_temp_f))
    if target_temp is not None:
        target_temp = max(40, min(110, int(target_temp)))

    # Duration: if end < start the session crosses midnight
    if end > start:
        duration_mins = int(
            (datetime.combine(booking_date, end) - datetime.combine(booking_date, start)).seconds / 60
        )
    else:
        duration_mins = int(
            (datetime.combine(booking_date + timedelta(days=1), end) - datetime.combine(booking_date, start)).seconds / 60
        )
    on_time = body.get("on_time") or duration_mins

    db = SessionLocal()
    try:
        with _booking_lock:
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
        if not member.is_admin:
            day = booking_date.day
            month = booking_date.strftime("%b")
            weekday = booking_date.strftime("%a")
            start_fmt = start.strftime("%I:%M %p").lstrip("0")
            end_fmt = end.strftime("%I:%M %p").lstrip("0")
            _notify_admins_push({
                "title": "📅 New booking",
                "body": f"{member.name} booked {weekday} {day} {month}, {start_fmt}–{end_fmt}",
                "tag": f"booking-new-{booking.id}",
                "url": "/",
            }, pref_key="booking")
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
        was_active = booking.status == "active"
        booking.status = "cancelled"
        db.commit()
        # If the session was in progress, turn the sauna off immediately
        if was_active:
            try:
                get_harvia().turn_off()
                logger.info("Sauna turned off after active booking %d was cancelled", booking.id)
            except Exception as exc:
                logger.warning("Could not turn off sauna after cancellation: %s", exc)
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>", methods=["PUT"])
def edit_booking(booking_id: int):
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    body = request.get_json(silent=True) or {}

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.member_id != member.id and not member.is_admin:
            return err("Cannot edit someone else's booking", 403)
        if booking.status != "scheduled":
            return err(f"Cannot edit a booking with status '{booking.status}'")

        try:
            start = time.fromisoformat(body["start_time"]) if "start_time" in body else booking.start_time
            end   = time.fromisoformat(body["end_time"])   if "end_time"   in body else booking.end_time
        except ValueError as exc:
            return err(f"Invalid time: {exc}")

        if end == start:
            return err("end_time must differ from start_time")

        # Recalculate duration if times changed
        if "on_time" in body:
            on_time = int(body["on_time"])
        elif end > start:
            on_time = int((datetime.combine(booking.date, end) - datetime.combine(booking.date, start)).seconds / 60)
        else:
            on_time = int((datetime.combine(booking.date + timedelta(days=1), end) - datetime.combine(booking.date, start)).seconds / 60)

        # Temperature
        target_temp = booking.target_temp
        if "target_temp_f" in body:
            target_temp = max(40, min(110, int(f_to_c(float(body["target_temp_f"])))))
        elif "target_temp" in body:
            target_temp = max(40, min(110, int(body["target_temp"])))

        # Overlap check — exclude the booking being edited
        cooldown_start = (datetime.combine(booking.date, start) - timedelta(minutes=COOLDOWN_MINUTES)).time()
        cooldown_end   = (datetime.combine(booking.date, end)   + timedelta(minutes=COOLDOWN_MINUTES)).time()
        overlap = (
            db.query(Booking)
            .filter(
                Booking.id != booking_id,
                Booking.date == booking.date,
                Booking.status != "cancelled",
                Booking.start_time < cooldown_end,
                Booking.end_time   > cooldown_start,
            )
            .first()
        )
        if overlap:
            return err(
                f"Overlaps with existing booking ({overlap.start_time.strftime('%H:%M')}–"
                f"{overlap.end_time.strftime('%H:%M')}) including {COOLDOWN_MINUTES}-min cooldown"
            )

        booking.start_time = start
        booking.end_time   = end
        booking.on_time    = on_time
        booking.target_temp = target_temp
        # Reset preheat notification so it re-fires at the new time
        booking.preheat_notified_at = None
        db.commit()
        db.refresh(booking)
        if not member.is_admin:
            day = booking.date.day
            month = booking.date.strftime("%b")
            weekday = booking.date.strftime("%a")
            start_fmt = start.strftime("%I:%M %p").lstrip("0")
            end_fmt = end.strftime("%I:%M %p").lstrip("0")
            _notify_admins_push({
                "title": "✏️ Booking updated",
                "body": f"{member.name}'s booking on {weekday} {day} {month} moved to {start_fmt}–{end_fmt}",
                "tag": f"booking-edit-{booking.id}",
                "url": "/",
            }, pref_key="booking")
        return jsonify(booking.to_dict())
    except IntegrityError:
        db.rollback()
        return err("Database error updating booking", 500)
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

        now = app_now()
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
# Admin DB browser
# ---------------------------------------------------------------------------

_DB_TABLES = {
    "family_members": {
        "model": FamilyMember,
        "editable": ["name", "status", "is_admin", "default_temp", "default_time", "max_temp", "color"],
    },
    "bookings": {
        "model": Booking,
        "editable": ["date", "start_time", "end_time", "target_temp", "on_time", "status", "member_id"],
    },
    "presets": {
        "model": Preset,
        "editable": ["name", "label", "target_temp", "on_time", "sort_order"],
    },
    "control_log": {
        "model": ControlLog,
        "editable": [],  # read-only
    },
}


@app.route("/api/admin/db/<table>")
def db_list(table: str):
    db, _, error = require_admin()
    if error:
        return error
    if table not in _DB_TABLES:
        db.close()
        return err(f"Unknown table '{table}'")
    try:
        rows = db.query(_DB_TABLES[table]["model"]).order_by(
            _DB_TABLES[table]["model"].id
        ).all()
        return jsonify([r.to_dict() for r in rows])
    finally:
        db.close()


@app.route("/api/admin/db/<table>/<int:row_id>", methods=["PUT"])
def db_update(table: str, row_id: int):
    db, _, error = require_admin()
    if error:
        return error
    if table not in _DB_TABLES:
        db.close()
        return err(f"Unknown table '{table}'")
    editable = _DB_TABLES[table]["editable"]
    if not editable:
        db.close()
        return err("This table is read-only")
    try:
        model = _DB_TABLES[table]["model"]
        row = db.query(model).filter_by(id=row_id).first()
        if not row:
            return err("Row not found", 404)
        body = request.get_json(silent=True) or {}
        for field, value in body.items():
            if field in editable:
                setattr(row, field, value)
        db.commit()
        db.refresh(row)
        return jsonify(row.to_dict())
    finally:
        db.close()


@app.route("/api/admin/db/<table>/<int:row_id>", methods=["DELETE"])
def db_delete(table: str, row_id: int):
    db, admin_member, error = require_admin()
    if error:
        return error
    if table not in _DB_TABLES:
        db.close()
        return err(f"Unknown table '{table}'")
    try:
        model = _DB_TABLES[table]["model"]
        row = db.query(model).filter_by(id=row_id).first()
        if not row:
            return err("Row not found", 404)
        # Safety: don't let admin delete themselves
        if table == "family_members" and row.id == admin_member.id:
            return err("Cannot delete your own account")
        db.delete(row)
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

    init_db()
    logger.info("Database initialised at %s", DB_PATH)
    _seed_presets()

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
