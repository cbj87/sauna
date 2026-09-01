"""
Sweat Box — Harvia sauna booking & control server.
Flask API + static SPA host.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import secrets
import threading
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory, session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import selectinload
from sqlalchemy.types import Integer as _SAInteger, Date as _SADate, Time as _SATime, Boolean as _SABoolean

from harvia_client import HarviaClient, SaunaOffline, SaunaStartUnconfirmed
from models import (
    DB_PATH,
    Booking,
    BookingInvite,
    BookingParticipant,
    ControlLog,
    DeviceStateLog,
    FamilyMember,
    GuestRsvp,
    LiveActivityPushToStartToken,
    LiveActivityToken,
    NativeDevice,
    Preset,
    PushSubscription,
    SessionLocal,
    init_db,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("apscheduler").setLevel(logging.WARNING)
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

APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_PRIVATE_KEY = os.environ.get("APNS_PRIVATE_KEY", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "dev.cbj87.SweatBox")
APNS_USE_SANDBOX = os.environ.get("APNS_USE_SANDBOX", "1").lower() not in ("0", "false", "no")
# Apple Team ID for the Universal Links AASA file — same team as the APNs key,
# so it defaults to APNS_TEAM_ID; override only if they ever differ.
APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", APNS_TEAM_ID)

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Australia/Sydney")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
APP_URL        = os.environ.get("APP_URL", "http://localhost:5000")

# Sauna location for Open-Meteo outdoor temperature (preheat telemetry). Unset = skip fetch.
WEATHER_LAT = os.environ.get("WEATHER_LAT", "")
WEATHER_LON = os.environ.get("WEATHER_LON", "")
DEVICE_LOG_RETENTION_DAYS = int(os.environ.get("DEVICE_LOG_RETENTION_DAYS", "365"))

def app_now() -> datetime:
    """Current local time as a naive datetime in the configured APP_TIMEZONE."""
    return datetime.now(ZoneInfo(APP_TIMEZONE)).replace(tzinfo=None)


harvia: HarviaClient | None = None

COOLDOWN_MINUTES = 15
PREHEAT_WINDOW_MINUTES = 90
WALKUP_WINDOW_MINUTES = 120
BOOKING_WINDOW_DAYS = int(os.environ.get("BOOKING_WINDOW_DAYS", "30"))

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
_apns_jwt_cache: dict[str, float | str] = {"token": "", "created_at": 0.0}

# ---------------------------------------------------------------------------
# Async sauna activation (B1)
#
# turn_on() takes 20-45s (the device needs onTime committed to `reported`
# before activating), which blew past the frontend's 10s request timeout.
# Endpoints now stage the state synchronously (fast, proves the device is
# reachable) and run the slow wait + activate in a background thread.
#
# The generation counter is the cancellation mechanism: every new start takes
# a fresh generation, and any turn-off bumps it.  A pending background
# activation re-checks its generation right before sending active=1, so a
# turn-off issued during the wait window can never be undone by a stale
# activation re-igniting the sauna.
# ---------------------------------------------------------------------------
_activation_lock = threading.Lock()
_activation_generation = 0


def _next_activation_generation() -> int:
    global _activation_generation
    with _activation_lock:
        _activation_generation += 1
        return _activation_generation


def _activation_still_current(gen: int) -> bool:
    with _activation_lock:
        return _activation_generation == gen


def _cancel_pending_activations() -> None:
    """Invalidate any in-flight background activation (called on turn-off)."""
    _next_activation_generation()


# A start that the device never confirmed.  Surfaced on /api/sauna/status so a
# client that optimistically showed "starting…" learns it didn't take, even if
# the push notification is missed or notifications are off.
_START_FAILURE_TTL = 15 * 60
_start_failure_lock = threading.Lock()
_start_failure: dict | None = None


def _record_start_failure(message: str, context: str) -> None:
    global _start_failure
    with _start_failure_lock:
        _start_failure = {"at": _time.time(), "message": message, "context": context}


def _clear_start_failure() -> None:
    global _start_failure
    with _start_failure_lock:
        _start_failure = None


def _get_start_failure() -> dict | None:
    with _start_failure_lock:
        if not _start_failure:
            return None
        if _time.time() - _start_failure["at"] > _START_FAILURE_TTL:
            return None
        return dict(_start_failure)


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


def _send_email(to: str, subject: str, body_text: str) -> None:
    """Send a plain-text email via Resend HTTP API. No-ops silently if not configured."""
    if not RESEND_API_KEY:
        logger.warning("Email not configured (RESEND_API_KEY missing) — skipping send to %s", to)
        return
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "text": body_text,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "SweatBox/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Email sent to %s: %s (status %d)", to, subject, resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("Failed to send email to %s: HTTP %d — %s", to, exc.code, body)
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to, exc)


def _validate_password(pw: str) -> str | None:
    """Returns an error string if the password is invalid, or None if it's acceptable."""
    if not pw or len(pw) < 8:
        return "Password must be at least 8 characters"
    return None


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


def _apns_is_configured() -> bool:
    return bool(APNS_KEY_ID and APNS_TEAM_ID and APNS_PRIVATE_KEY and APNS_BUNDLE_ID)


def _apns_private_key() -> str:
    return APNS_PRIVATE_KEY.replace("\\n", "\n")


def _apns_jwt() -> str:
    """Return a cached APNs provider JWT."""
    now = _time.time()
    cached_token = str(_apns_jwt_cache.get("token") or "")
    cached_created_at = float(_apns_jwt_cache.get("created_at") or 0)
    if cached_token and now - cached_created_at < 45 * 60:
        return cached_token

    import jwt

    token = jwt.encode(
        {"iss": APNS_TEAM_ID, "iat": int(now)},
        _apns_private_key(),
        algorithm="ES256",
        headers={"alg": "ES256", "kid": APNS_KEY_ID},
    )
    _apns_jwt_cache["token"] = token
    _apns_jwt_cache["created_at"] = now
    return token


_apns_http_client: "httpx.Client | None" = None
_apns_http_client_lock = threading.Lock()


def _get_apns_http_client():
    """Return a cached httpx.Client with HTTP/2 enabled.

    APNs requires HTTP/2 — Python's urllib only speaks HTTP/1.1, so requests
    bounce off Apple's frontend with garbled binary errors. httpx[http2] +
    the `h2` package give us HTTP/2 and connection reuse across pushes.
    """
    global _apns_http_client
    if _apns_http_client is not None:
        return _apns_http_client
    with _apns_http_client_lock:
        if _apns_http_client is None:
            import httpx
            _apns_http_client = httpx.Client(
                http2=True,
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
        return _apns_http_client


def _device_env_map(db, native_device_ids) -> dict[str, str | None]:
    """Return {native_device_id: apns_environment} for the given devices.

    Live Activity token tables only store native_device_id (not the env directly),
    so this one lookup lets each token pick the right APNs host per device.
    """
    ids = [d for d in (native_device_ids or []) if d]
    if not ids:
        return {}
    rows = (
        db.query(NativeDevice.native_device_id, NativeDevice.apns_environment)
        .filter(NativeDevice.native_device_id.in_(ids))
        .all()
    )
    return {ndid: env for ndid, env in rows}


def _apns_host_for(environment: str | None) -> str:
    """Pick the APNs endpoint for a given device environment.

    A device token issued to a dev/Xcode build (sandbox) is only valid against
    api.sandbox.push.apple.com; a token issued to a TestFlight/App Store build
    (production) is only valid against api.push.apple.com. The two systems are
    parallel and tokens are NOT interchangeable — sending to the wrong host
    returns BadDeviceToken and the notification is silently dropped.

    Falls back to the legacy APNS_USE_SANDBOX env for old device rows that
    predate per-device environment tracking.
    """
    env = (environment or "").strip().lower()
    if env == "sandbox":
        return "api.sandbox.push.apple.com"
    if env == "production":
        return "api.push.apple.com"
    return "api.sandbox.push.apple.com" if APNS_USE_SANDBOX else "api.push.apple.com"


def _send_live_activity_apns(
    token: str,
    payload: dict,
    *,
    environment: str | None = None,
) -> tuple[bool, int | None, str | None]:
    """Send one ActivityKit APNs payload over HTTP/2.

    Returns (ok, status_code, response_body). Missing APNs config is a soft failure.
    """
    if not _apns_is_configured():
        return False, None, "APNs is not configured"

    host = _apns_host_for(environment)
    url = f"https://{host}/3/device/{token}"
    headers = {
        "authorization": f"bearer {_apns_jwt()}",
        "apns-topic": f"{APNS_BUNDLE_ID}.push-type.liveactivity",
        "apns-push-type": "liveactivity",
        "apns-priority": "10",
        "content-type": "application/json",
        "user-agent": "SweatBox/1.0",
    }
    body_bytes = json.dumps(payload, separators=(",", ":")).encode()
    try:
        resp = _get_apns_http_client().post(url, content=body_bytes, headers=headers)
        body = resp.text
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("Live Activity APNs HTTP error %s: %s", resp.status_code, body)
        return ok, resp.status_code, body
    except Exception as exc:
        logger.error("Live Activity APNs send failed: %s", exc)
        return False, None, str(exc)


def _live_activity_attributes(native_device_id: str = "") -> dict:
    return {
        "nativeDeviceId": native_device_id,
        "createdAtMillis": int(_time.time() * 1000),
    }


def _live_activity_start_payload(
    content_state: dict,
    native_device_id: str = "",
    alert_title: str = "Sauna is on",
    alert_body: str = "A sauna session is running.",
) -> dict:
    return {
        "aps": {
            "timestamp": int(_time.time()),
            "event": "start",
            "attributes-type": "SaunaActivityAttributes",
            "attributes": _live_activity_attributes(native_device_id),
            "content-state": content_state,
            "input-push-token": 1,
            "alert": {
                "title": alert_title,
                "body": alert_body,
                "sound": "default",
            },
        }
    }


def _send_apns_alert(
    token: str,
    title: str,
    body: str,
    *,
    collapse_id: str | None = None,
    interruption_level: str = "active",
    thread_id: str | None = None,
    extras: dict | None = None,
    environment: str | None = None,
) -> tuple[bool, int | None, str | None]:
    """Send an APNs alert-type push (regular banner notification, not a Live Activity).

    interruption_level: "passive" | "active" | "time-sensitive" | "critical" — controls
    whether iOS respects Focus modes and how prominently the alert renders. Use
    "time-sensitive" for safety alerts like the auto-shutoff failsafe.

    environment: "sandbox" or "production" — which APNs endpoint the device's
    token was issued for. Unset falls back to APNS_USE_SANDBOX default.

    Returns (ok, status_code, response_body). Missing APNs config is a soft failure.
    """
    if not _apns_is_configured():
        return False, None, "APNs is not configured"

    host = _apns_host_for(environment)
    url = f"https://{host}/3/device/{token}"
    headers = {
        "authorization": f"bearer {_apns_jwt()}",
        "apns-topic": APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "content-type": "application/json",
        "user-agent": "SweatBox/1.0",
    }
    if collapse_id:
        # apns-collapse-id must be <= 64 bytes — truncate defensively.
        headers["apns-collapse-id"] = collapse_id[:64]

    aps: dict = {
        "alert": {"title": title, "body": body},
        "sound": "default",
        "interruption-level": interruption_level,
    }
    if thread_id:
        aps["thread-id"] = thread_id
    payload: dict = {"aps": aps}
    if extras:
        payload.update(extras)

    body_bytes = json.dumps(payload, separators=(",", ":")).encode()
    try:
        resp = _get_apns_http_client().post(url, content=body_bytes, headers=headers)
        text = resp.text
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("APNs alert HTTP error %s: %s", resp.status_code, text)
        return ok, resp.status_code, text
    except Exception as exc:
        logger.error("APNs alert send failed: %s", exc)
        return False, None, str(exc)


# APNs reasons that mean "this token is dead — stop using it." Other 4xx codes
# (MissingTopic, PayloadTooLarge, BadCollapseId, credential errors, …) reflect
# server-side bugs and MUST NOT nuke the device row — otherwise one bad payload
# would wipe every registration in the DB on the next send.
_APNS_STALE_TOKEN_REASONS = frozenset({
    "BadDeviceToken",
    "Unregistered",
    "DeviceTokenNotForTopic",
    "ExpiredProviderToken",  # rare — provider token issue, but token is unusable in this state
})


def _apns_token_is_stale(status_code: int | None, body: str | None) -> bool:
    """Only 410 Unregistered or a 400 with a token-specific reason marks the
    device token as dead. Everything else is treated as a transient/server bug."""
    if status_code == 410:
        return True
    if status_code == 400 and body:
        try:
            reason = json.loads(body).get("reason")
        except (ValueError, AttributeError):
            reason = None
        return reason in _APNS_STALE_TOKEN_REASONS
    return False


def _dispatch_alert_to_member(db, member_id: int, payload: dict) -> dict:
    """Deliver one alert to one member. Prefer APNS (native app), fall back to Web Push.

    payload keys: title, body, url?, tag?, interruption_level?, thread_id?, plus any
    extra keys that should ride along on Web Push (e.g. bookingId).

    Returns a dict describing what happened — used by /api/push/test and other
    callers that want to surface delivery status.
    """
    result = {"transport": None, "sent": 0, "failed": 0, "cleared": 0}

    title = payload.get("title") or ""
    body = payload.get("body") or ""
    tag = payload.get("tag")
    interruption_level = payload.get("interruption_level") or "active"
    thread_id = payload.get("thread_id")

    apns_tokens = (
        db.query(NativeDevice)
        .filter(NativeDevice.member_id == member_id, NativeDevice.apns_token.isnot(None))
        .all()
    )

    if apns_tokens:
        result["transport"] = "apns"
        for device in apns_tokens:
            ok, status_code, resp_body = _send_apns_alert(
                device.apns_token,
                title,
                body,
                collapse_id=tag,
                interruption_level=interruption_level,
                thread_id=thread_id,
                environment=device.apns_environment,
                # Ride-along fields let the app deep-link or handle the tap.
                extras={
                    k: v for k, v in payload.items()
                    if k not in ("title", "body", "interruption_level", "thread_id")
                    and v is not None
                },
            )
            if ok:
                result["sent"] += 1
            else:
                result["failed"] += 1
            if _apns_token_is_stale(status_code, resp_body):
                device.apns_token = None
                result["cleared"] += 1
        return result

    # Fallback: Web Push. Same payload shape as before.
    if not VAPID_PRIVATE_KEY:
        return result
    result["transport"] = "webpush"
    subs = db.query(PushSubscription).filter_by(member_id=member_id).all()
    dead: list[str] = []
    for sub in subs:
        push_result = _send_push(
            {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            payload,
        )
        if push_result is True:
            result["sent"] += 1
        else:
            result["failed"] += 1
            if push_result == 410:
                dead.append(sub.endpoint)
    for ep in dead:
        db.query(PushSubscription).filter_by(endpoint=ep).delete()
        result["cleared"] += 1
    return result


def _notify_admins(payload: dict, pref_key: str | None = None,
                   exclude_member_id: int | None = None) -> None:
    """Alert every admin. Prefers APNS per-member; falls back to Web Push.

    exclude_member_id skips one admin — used when they already got the same
    alert as the acting member and shouldn't be buzzed twice."""
    try:
        with SessionLocal() as db:
            admins = db.query(FamilyMember).filter_by(is_admin=1, status="approved").all()
            for admin in admins:
                if exclude_member_id is not None and admin.id == exclude_member_id:
                    continue
                if pref_key is not None:
                    prefs = admin.get_notification_prefs()
                    if not prefs.get(pref_key, True):
                        continue
                _dispatch_alert_to_member(db, admin.id, payload)
            db.commit()
    except Exception as exc:
        logger.error("Failed to send admin notifications: %s", exc)


def _notify_member(member_id: int, payload: dict, pref_key: str | None = None) -> None:
    """Alert a specific member. Prefers APNS; falls back to Web Push. Never raises.

    pref_key: if set, skip when the member has this key = false in their
    notification_prefs. Keeps opt-out honoured at the helper so new call sites
    can't accidentally bypass user preferences.
    """
    try:
        with SessionLocal() as db:
            if pref_key is not None:
                member = db.query(FamilyMember).filter_by(id=member_id).first()
                if not member:
                    return
                prefs = member.get_notification_prefs()
                if not prefs.get(pref_key, True):
                    return
            _dispatch_alert_to_member(db, member_id, payload)
            db.commit()
    except Exception as exc:
        logger.error("Failed to send member notification: %s", exc)


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


def _booking_to_dict_for(booking: Booking, viewer: FamilyMember | None = None) -> dict:
    data = booking.to_dict()
    participants = sorted(
        (p for p in (booking.participants or []) if p.member is not None),
        key=lambda p: p.joined_at or datetime.min,
    )
    data["participants"] = [p.to_dict() for p in participants]
    participant_ids = {p.member_id for p in participants}
    invites = sorted(
        (i for i in (booking.invites or []) if i.member is not None),
        key=lambda i: i.invited_at or datetime.min,
    )
    data["invites"] = [i.to_dict() for i in invites]
    if viewer is not None:
        data["joined"] = viewer.id == booking.member_id or viewer.id in participant_ids
        data["can_control"] = bool(viewer.is_admin) or data["joined"]
        data["my_invite_status"] = next((i.status for i in invites if i.member_id == viewer.id), None)
    else:
        data["joined"] = False
        data["can_control"] = False
        data["my_invite_status"] = None
    data["participant_count"] = len(participant_ids) + (0 if booking.member_id in participant_ids else 1)
    return data


def _is_booking_participant(db, booking_id: int, member_id: int) -> bool:
    return (
        db.query(BookingParticipant)
        .filter_by(booking_id=booking_id, member_id=member_id)
        .first()
        is not None
    )


def _can_control_booking(db, booking: Booking, member: FamilyMember) -> bool:
    return bool(
        member.is_admin
        or booking.member_id == member.id
        or _is_booking_participant(db, booking.id, member.id)
    )


def _add_participant(db, booking: Booking, member_id: int) -> None:
    """Insert-if-missing a BookingParticipant (grants control + Live Activity eligibility)."""
    if booking.member_id == member_id or _is_booking_participant(db, booking.id, member_id):
        return
    db.add(BookingParticipant(booking_id=booking.id, member_id=member_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    db.refresh(booking)


def _booking_when_text(booking: Booking) -> str:
    """Human date/time for notification bodies, e.g. 'Sat 12 Jul, 6:00 PM–7:00 PM'."""
    day = booking.date.day
    month = booking.date.strftime("%b")
    weekday = booking.date.strftime("%a")
    start_fmt = booking.start_time.strftime("%I:%M %p").lstrip("0")
    end_fmt = booking.end_time.strftime("%I:%M %p").lstrip("0")
    return f"{weekday} {day} {month}, {start_fmt}–{end_fmt}"


def _create_invites(db, booking: Booking, inviter: FamilyMember, member_ids: list[int]) -> list[BookingInvite]:
    """Create invite rows for members not already involved and push each an invite alert."""
    skip_ids = {booking.member_id}
    skip_ids.update(p.member_id for p in (booking.participants or []))
    skip_ids.update(i.member_id for i in (booking.invites or []))

    valid_ids = {
        m.id for m in db.query(FamilyMember)
        .filter(FamilyMember.id.in_(member_ids), FamilyMember.status == "approved")
        .all()
    }

    created = []
    for mid in member_ids:
        if mid in skip_ids or mid not in valid_ids:
            continue
        skip_ids.add(mid)
        invite = BookingInvite(
            booking_id=booking.id,
            member_id=mid,
            invited_by=inviter.id,
            status="invited",
            invited_at=app_now(),
        )
        db.add(invite)
        created.append(invite)
    if not created:
        return []
    db.commit()
    db.refresh(booking)

    when = _booking_when_text(booking)
    for invite in created:
        _notify_member(invite.member_id, {
            "title": "🔥 Sauna invite",
            "body": f"{inviter.name} invited you — {when}",
            "tag": f"invite-{booking.id}-{invite.member_id}",
            "thread_id": "booking-invite",
            "url": f"/?booking={booking.id}",
            "bookingId": booking.id,
        }, pref_key="invite")
    return created


def _booking_end_datetime(booking: Booking, now: datetime | None = None) -> datetime:
    """Naive local datetime when the booking ends (handles midnight-spanning)."""
    now = now or app_now()
    if booking.end_time < booking.start_time and now.time() >= booking.end_time:
        return datetime.combine(now.date() + timedelta(days=1), booking.end_time)
    return datetime.combine(now.date(), booking.end_time)


def _booking_remaining_minutes(booking: Booking, now: datetime | None = None) -> int:
    now = now or app_now()
    remaining = round((_booking_end_datetime(booking, now) - now).total_seconds() / 60)
    return max(0, remaining)


def _live_activity_payload_for_booking(booking: Booking, status: dict | None = None) -> dict:
    status = status or {}
    current_temp_f = status.get("temperatureF")
    if current_temp_f is None and status.get("temperature") is not None:
        current_temp_f = c_to_f(status["temperature"])
    target_temp_f = c_to_f(booking.target_temp) if booking.target_temp is not None else status.get("targetTempF")
    # endsAtMillis lets the widget run a self-ticking countdown — the remaining
    # time stays correct even if update pushes stop arriving.
    end_local = _booking_end_datetime(booking)
    ends_at_millis = int(end_local.replace(tzinfo=ZoneInfo(APP_TIMEZONE)).timestamp() * 1000)
    return {
        "bookingId": booking.id,
        "memberId": booking.member_id,
        "bookingName": f"{booking.member.name}'s Session" if booking.member else "Sauna Session",
        "currentTempF": current_temp_f,
        "targetTempF": target_temp_f,
        "remainingMinutes": _booking_remaining_minutes(booking),
        "endsAtMillis": ends_at_millis,
        "heatOn": bool(status.get("active", True)),
        "active": booking.status in ("active", "preheating", "scheduled"),
        "updatedAtMillis": int(_time.time() * 1000),
    }


def _current_live_booking(db, member: FamilyMember | None = None) -> Booking | None:
    now = app_now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    current_time = now.time()

    q = db.query(Booking)
    filters = [Booking.status.in_(["active", "preheating"])]
    if member is not None:
        q = q.outerjoin(BookingParticipant, BookingParticipant.booking_id == Booking.id)
        filters.append((Booking.member_id == member.id) | (BookingParticipant.member_id == member.id))

    return (
        q.filter(
            *filters,
            (
                (Booking.date == today)
                | (
                    (Booking.date == yesterday)
                    & (Booking.end_time < Booking.start_time)
                    & (Booking.end_time > current_time)
                )
            ),
        )
        .order_by(Booking.date.desc(), Booking.start_time.desc())
        .first()
    )


def _live_activity_recipient_tokens(db, booking: Booking) -> list[LiveActivityToken]:
    """Tokens that should receive a push for this booking.

    The booking owner's devices plus any admin opted into all-session
    visibility. Matching is by member, not booking_id — devices may have
    started activities locally without a server-supplied booking_id.
    """
    eligible_member_ids = {booking.member_id}
    eligible_member_ids.update(p.member_id for p in (booking.participants or []))
    admins = (
        db.query(FamilyMember)
        .filter_by(is_admin=1, status="approved", live_activity_all_sessions=1)
        .all()
    )
    eligible_member_ids.update(a.id for a in admins)
    return (
        db.query(LiveActivityToken)
        .filter(LiveActivityToken.member_id.in_(eligible_member_ids))
        .all()
    )


def _live_activity_push_to_start_recipients(
    db,
    booking: Booking,
    exclude_native_device_id: str | None = None,
) -> list[LiveActivityPushToStartToken]:
    """Push-to-start tokens that should get a remote-start for this booking."""
    eligible_member_ids = {booking.member_id}
    eligible_member_ids.update(p.member_id for p in (booking.participants or []))
    admins = (
        db.query(FamilyMember)
        .filter_by(is_admin=1, status="approved", live_activity_all_sessions=1)
        .all()
    )
    eligible_member_ids.update(a.id for a in admins)

    q = (
        db.query(LiveActivityPushToStartToken)
        .filter(LiveActivityPushToStartToken.member_id.in_(eligible_member_ids))
    )
    if exclude_native_device_id:
        q = q.filter(LiveActivityPushToStartToken.native_device_id != exclude_native_device_id)
    return q.all()


def _push_live_activity_start_to_tokens(
    db,
    tokens: list[LiveActivityPushToStartToken],
    content_state: dict,
    alert_title: str,
    alert_body: str,
) -> dict:
    """Remote-start Live Activities. Returns send/delete/error counts."""
    result = {"sent": 0, "failed": 0, "deleted": 0, "last_error": None}
    if not tokens or not _apns_is_configured():
        return result

    device_env = _device_env_map(db, [t.native_device_id for t in tokens])
    for token_row in tokens:
        payload = _live_activity_start_payload(
            content_state,
            native_device_id=token_row.native_device_id,
            alert_title=alert_title,
            alert_body=alert_body,
        )
        ok, status_code, body = _send_live_activity_apns(
            token_row.token, payload,
            environment=device_env.get(token_row.native_device_id),
        )
        if ok:
            result["sent"] += 1
            continue
        result["failed"] += 1
        result["last_error"] = {
            "status": status_code,
            "body": body,
            "tokenId": token_row.id,
        }
        is_stale = status_code in (400, 410) and isinstance(body, str) and (
            "BadDeviceToken" in body
            or "Unregistered" in body
            or "ExpiredToken" in body
            or "DeviceTokenNotForTopic" in body
        )
        if is_stale:
            db.delete(token_row)
            result["deleted"] += 1
            logger.info("Removed stale push-to-start token id=%s status=%s", token_row.id, status_code)
        else:
            logger.warning(
                "Live Activity remote start failed token_id=%s status=%s body=%s",
                token_row.id, status_code, body,
            )
    if result["deleted"]:
        db.commit()
    return result


def _ended_live_activity_state() -> dict:
    """A generic 'session over' ContentState — every key the widget's Codable
    struct requires must be present or iOS silently drops the push."""
    return {
        "bookingId": None,
        "memberId": None,
        "bookingName": "Sauna Session",
        "currentTempF": None,
        "targetTempF": None,
        "remainingMinutes": 0,
        "endsAtMillis": None,
        "heatOn": False,
        "active": False,
        "updatedAtMillis": int(_time.time() * 1000),
    }


def _end_stale_activities_for_devices(db, native_device_ids: list[str]) -> None:
    """End any still-registered Live Activities on these devices.

    A push-to-start always creates a NEW activity — without this sweep the old
    (possibly broken) activity stays on the Lock Screen next to the new one.
    Runs right before a remote start so each device ends up with exactly one.
    """
    if not native_device_ids:
        return
    rows = (
        db.query(LiveActivityToken)
        .filter(LiveActivityToken.native_device_id.in_(native_device_ids))
        .all()
    )
    if not rows:
        return
    # Capture plain values first — every row is getting deleted below, and ORM
    # instances go unusable once removed.
    targets = [(row.token, row.native_device_id) for row in rows]
    device_env = _device_env_map(db, [ndid for _, ndid in targets])
    payload = {
        "aps": {
            "timestamp": int(_time.time()),
            "event": "end",
            "dismissal-date": int(_time.time()),
            "content-state": _ended_live_activity_state(),
        }
    }
    for token, ndid in targets:
        _send_live_activity_apns(token, payload, environment=device_env.get(ndid))
    (
        db.query(LiveActivityToken)
        .filter(LiveActivityToken.token.in_([t for t, _ in targets]))
        .delete(synchronize_session=False)
    )
    db.commit()


def _fanout_live_activity_start(
    booking_id: int,
    exclude_native_device_id: str | None = None,
    status: dict | None = None,
) -> dict:
    """Remote-start Live Activities for eligible native devices."""
    result = {"sent": 0, "failed": 0, "tokens": 0, "deleted": 0, "skipped": None, "last_error": None}
    if not _apns_is_configured():
        result["skipped"] = "APNs is not configured"
        return result
    try:
        with SessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                result["skipped"] = "booking not found"
                return result
            tokens = _live_activity_push_to_start_recipients(db, booking, exclude_native_device_id)
            result["tokens"] = len(tokens)
            if not tokens:
                result["skipped"] = "no eligible push-to-start tokens"
                return result
            # End whatever activity each target device is already showing —
            # the start push below always creates a fresh one.
            _end_stale_activities_for_devices(db, [t.native_device_id for t in tokens])
            content_state = _live_activity_payload_for_booking(booking, status)
            title = "Sauna is on"
            body = f"{booking.member.name if booking.member else 'Someone'} started a session."
            send_result = _push_live_activity_start_to_tokens(db, tokens, content_state, title, body)
            result.update({**send_result, "skipped": None})
            if result["sent"] <= 0 and result["failed"] > 0:
                result["skipped"] = "APNs rejected all push-to-start sends"
            return result
    except Exception as exc:
        logger.error("_fanout_live_activity_start(booking_id=%s) failed: %s", booking_id, exc)
        result["skipped"] = str(exc)
        return result


def _push_live_activity_to_tokens(
    db,
    tokens: list[LiveActivityToken],
    content_state: dict,
    event: str = "update",
    dismissal_date: int | None = None,
) -> int:
    """Send one APNs Live Activity push to each token. Deletes stale tokens. Returns delete count."""
    if not tokens or not _apns_is_configured():
        return 0

    aps: dict = {
        "timestamp": int(_time.time()),
        "event": event,
        "content-state": content_state,
    }
    if dismissal_date is not None:
        aps["dismissal-date"] = dismissal_date
    payload = {"aps": aps}

    deleted = 0
    device_env = _device_env_map(db, [t.native_device_id for t in tokens])
    for token_row in tokens:
        ok, status_code, body = _send_live_activity_apns(
            token_row.token, payload,
            environment=device_env.get(token_row.native_device_id),
        )
        if ok:
            continue
        is_stale = status_code in (400, 410) and isinstance(body, str) and (
            "BadDeviceToken" in body
            or "Unregistered" in body
            or "ExpiredToken" in body
            or "DeviceTokenNotForTopic" in body
        )
        if is_stale:
            db.delete(token_row)
            deleted += 1
            logger.info("Removed stale Live Activity token id=%s status=%s", token_row.id, status_code)
        else:
            logger.warning(
                "Live Activity push failed token_id=%s status=%s body=%s",
                token_row.id, status_code, body,
            )
    if deleted:
        db.commit()
    return deleted


def _fanout_live_activity_end(booking_id: int, reason: str = "ended") -> None:
    """Send an `end` push for the booking and drop its tokens. Safe to call any time."""
    if not _apns_is_configured():
        return
    try:
        with SessionLocal() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                return
            tokens = _live_activity_recipient_tokens(db, booking)
            # Snapshot ids before any push: a stale-token response inside
            # _push_live_activity_to_tokens deletes+commits rows, after which
            # the ORM instances can't be touched again.
            doomed_ids = [
                t.id for t in tokens
                if t.booking_id == booking.id or t.booking_id is None
            ]
            covered = {t.native_device_id for t in tokens}
            content_state = _live_activity_payload_for_booking(booking)
            content_state["heatOn"] = False
            content_state["active"] = False
            content_state["remainingMinutes"] = 0
            content_state["endsAtMillis"] = None
            if tokens:
                _push_live_activity_to_tokens(
                    db, tokens, content_state,
                    event="end",
                    dismissal_date=int(_time.time()),
                )
            # Also broadcast the end via push-to-start tokens — that's the only
            # handle we have on activities remote-started while the app was
            # closed. Harmless no-op on devices with nothing running. (Stale
            # rows are pruned inside on BadDeviceToken/Unregistered; valid p2s
            # rows are long-lived and must NOT be dropped wholesale.)
            p2s_tokens = [
                t for t in _live_activity_push_to_start_recipients(db, booking)
                if t.native_device_id not in covered
            ]
            if p2s_tokens:
                _push_live_activity_to_tokens(
                    db, p2s_tokens, content_state,
                    event="end",
                    dismissal_date=int(_time.time()),
                )
            # Tokens for this booking are dead weight now; surviving tokens for
            # other bookings stay. Match by booking_id when set; treat NULL as
            # legacy/test rows and drop them too.
            if doomed_ids:
                (
                    db.query(LiveActivityToken)
                    .filter(LiveActivityToken.id.in_(doomed_ids))
                    .delete(synchronize_session=False)
                )
            db.commit()
    except Exception as exc:
        logger.error("_fanout_live_activity_end(booking_id=%s) failed: %s", booking_id, exc)


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
# rsvp_public_respond is authorised by the unguessable share token in its path
_CSRF_EXEMPT = {"login", "signup", "migrate", "forgot_password", "reset_password", "static", "rsvp_public_respond"}


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

def _safe_turn_off(reason: str = "") -> bool:
    """Turn the sauna off and VERIFY it actually went off. Retries once and
    alerts admins if it stays on — because a session that outruns its booking
    is the one failure mode that actually matters here. Never raises.

    Returns True if confirmed off (or off command sent and unverifiable)."""
    global _last_app_off_ts
    # Abort any staged-but-not-yet-activated start so a background activation
    # can't re-ignite the sauna after this off.
    _cancel_pending_activations()
    # A deliberate off resolves any outstanding "didn't start" warning.
    _clear_start_failure()
    client = get_harvia()
    for attempt in (1, 2):
        try:
            client.turn_off()
            _last_app_off_ts = _time.time()
        except Exception as exc:
            logger.error("turn_off command failed (attempt %d, %s): %s", attempt, reason, exc)
            _time.sleep(2)
            continue
        _time.sleep(3)
        try:
            reported = client.get_device_state().get("reported", {})
        except Exception as exc:
            logger.warning("Could not verify off state (%s): %s — assuming off", reason, exc)
            return True
        if not reported.get("active"):
            logger.info("Sauna confirmed off (%s)", reason)
            return True
        logger.warning("Sauna still active after off command (attempt %d, %s)", attempt, reason)
        _time.sleep(2)

    logger.error("CRITICAL: sauna failed to turn off (%s)", reason)
    _notify_admins({
        "title": "⚠️ Sauna may still be ON",
        "body": "Automatic shut-off could not confirm the sauna turned off. Please check it manually.",
        "tag": "sauna-shutoff-failed",
        "thread_id": "sauna-safety",
        "interruption_level": "time-sensitive",
        "url": "/",
    })
    return False


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

        # Capture IDs *before* commit so we can fan out end pushes after the
        # transaction lands. SQLAlchemy may detach instances after commit.
        ended_booking_ids = [b.id for b in past_bookings]

        if newly_active or past_bookings:
            db.commit()

        for ended_id in ended_booking_ids:
            _fanout_live_activity_end(ended_id, reason="auto-shutoff")

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
                _safe_turn_off("auto-shutoff after booking ended")


def check_session_ending():
    """Send a push notification to the session owner 15 minutes before their booking ends."""
    if not VAPID_PRIVATE_KEY:
        return

    now = app_now()
    today = now.date()
    notify_before = 15  # minutes

    with SessionLocal() as db:
        active = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.status.in_(["active", "preheating", "scheduled"]),
                Booking.session_ending_notified_at.is_(None),
            )
            .all()
        )

        for booking in active:
            end_dt = datetime.combine(booking.date, booking.end_time)
            notify_at = end_dt - timedelta(minutes=notify_before)

            if now < notify_at or now >= end_dt:
                continue

            # Check member's session_ending pref
            member_prefs = booking.member.get_notification_prefs() if booking.member else {}
            if not member_prefs.get("session_ending", True):
                booking.session_ending_notified_at = now
                db.commit()
                continue

            booking.session_ending_notified_at = now
            end_fmt = booking.end_time.strftime("%I:%M %p").lstrip("0")
            _notify_member(booking.member_id, {
                "title": "⏱️ 15 minutes left!",
                "body": f"Your session ends at {end_fmt}. Extend or wind down.",
                "tag": f"session-ending-{booking.id}",
                "thread_id": "session-ending",
                "interruption_level": "time-sensitive",
                "url": "/?tab=controls",
                "bookingId": booking.id,
            })
            logger.info(
                "Session-ending reminder dispatched for booking %d (member_id=%d)",
                booking.id, booking.member_id,
            )
            db.commit()


def refresh_harvia_token():
    """Proactively refresh the Harvia Cognito token every 30 min so it never
    expires mid-request.  Runs independently of any user activity."""
    if harvia:
        harvia.proactive_refresh()


# Observability: poll device state every 60s to build a timeseries during sessions
# and catch "the device shut itself off and we don't know why" cases.  The previous
# code only logged what we POSTed, leaving the device side opaque.
_device_state_lock = threading.Lock()
_last_device_active: int | None = None  # last polled `active` value
_last_app_off_ts: float = 0.0           # monotonic ts of last app-initiated turn_off

# Shared cache of the most recent full Harvia status, populated by the 60s
# device-state job (which polls Harvia unconditionally).  Lets read-only clients
# poll `/api/sauna/status?cached=1` during a session without adding Harvia hits.
_status_cache_lock = threading.Lock()
_status_cache: dict = {"status": None, "ts": 0.0}


def _store_status_cache(status: dict | None) -> None:
    """Record the latest full status dict (raw, pre-status_with_f) with a timestamp."""
    if not status:
        return
    with _status_cache_lock:
        _status_cache["status"] = status
        _status_cache["ts"] = _time.time()


def _get_cached_status(max_age: float = 90.0) -> tuple[dict | None, float]:
    """Return (status, age_seconds) when the cached status is fresher than max_age, else (None, age)."""
    with _status_cache_lock:
        status = _status_cache["status"]
        ts = _status_cache["ts"]
    age = _time.time() - ts
    if status is not None and age <= max_age:
        return status, age
    return None, age


# Outdoor temperature cache (Open-Meteo, keyless).  Refreshed at most every
# 15 min; failures return the stale value (or None) so the device tick never
# blocks or raises on weather problems.
_outdoor_temp_lock = threading.Lock()
_outdoor_temp_cache: dict = {"temp": None, "ts": 0.0}
_OUTDOOR_TEMP_TTL = 900.0


def _get_outdoor_temp() -> float | None:
    if not WEATHER_LAT or not WEATHER_LON:
        return None
    with _outdoor_temp_lock:
        if _time.time() - _outdoor_temp_cache["ts"] < _OUTDOOR_TEMP_TTL:
            return _outdoor_temp_cache["temp"]
    try:
        import requests
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "current": "temperature_2m",
            },
            timeout=5,
        )
        resp.raise_for_status()
        temp = resp.json()["current"]["temperature_2m"]
    except Exception as exc:
        logger.warning("outdoor temp fetch failed: %s", exc)
        with _outdoor_temp_lock:
            return _outdoor_temp_cache["temp"]
    with _outdoor_temp_lock:
        _outdoor_temp_cache["temp"] = float(temp)
        _outdoor_temp_cache["ts"] = _time.time()
    return float(temp)


def _record_device_state(s: dict, active: int) -> None:
    """Persist one telemetry row for the preheat-estimate timeseries."""
    db = SessionLocal()
    try:
        db.add(DeviceStateLog(
            ts=app_now(),
            temperature=s.get("temperature"),
            target_temp=s.get("targetTemp"),
            active=active,
            heat_on=1 if s.get("heatOn") else 0,
            remaining_time=s.get("remainingTime"),
            outdoor_temp=_get_outdoor_temp(),
        ))
        db.commit()
    except Exception as exc:
        logger.warning("device state log insert failed: %s", exc)
        db.rollback()
    finally:
        db.close()


def log_device_state():
    if not harvia:
        return
    global _last_device_active
    try:
        s = harvia.get_full_status()
    except Exception as exc:
        logger.warning("device tick: status fetch failed: %s", exc)
        return

    _store_status_cache(s)

    active = s.get("active") or 0
    prev = _last_device_active

    # Persist telemetry while heating, plus one trailing row on the 1→0
    # transition so heating segments terminate cleanly in the timeseries.
    if active == 1 or prev == 1:
        _record_device_state(s, active)

    if active == 1:
        logger.info(
            "device tick: active=1 onTime=%s maxOnTime=%s remainingTime=%s "
            "targetTemp=%s temp=%s heatOn=%s doorSafety=%s statusCodes=%s errorCodes=%s online=%s",
            s.get("onTime"), s.get("maxOnTime"), s.get("remainingTime"),
            s.get("targetTemp"), s.get("temperature"), s.get("heatOn"),
            s.get("doorSafetyState"), s.get("statusCodes"), s.get("errorCodes"),
            s.get("online"),
        )

    if prev == 1 and active == 0 and s.get("online"):
        # Active just went 1→0 and the device is still online.  If we didn't fire
        # a turn_off in the last ~90s, the device ended the session on its own —
        # log loudly with surrounding state so the next outage is easy to debug.
        recently_app_off = (_time.time() - _last_app_off_ts) < 90
        if recently_app_off:
            logger.info("device tick: active 1→0 (app-initiated, within 90s of /sauna/off)")
        else:
            logger.warning(
                "device tick: SELF-SHUTOFF — active 1→0 with no recent app off. "
                "onTime=%s maxOnTime=%s remainingTime=%s targetTemp=%s temp=%s "
                "heatOn=%s doorSafety=%s statusCodes=%s errorCodes=%s online=%s",
                s.get("onTime"), s.get("maxOnTime"), s.get("remainingTime"),
                s.get("targetTemp"), s.get("temperature"), s.get("heatOn"),
                s.get("doorSafetyState"), s.get("statusCodes"), s.get("errorCodes"),
                s.get("online"),
            )

    with _device_state_lock:
        _last_device_active = active


def push_live_activity_updates():
    """Send Live Activity update pushes to subscribed devices during an active session.

    Returns immediately when there's no active/preheating booking — keeps
    the per-tick cost near zero outside of sessions.
    """
    if not _apns_is_configured():
        return
    try:
        with SessionLocal() as db:
            booking = _current_live_booking(db)
            if not booking:
                return
            tokens = _live_activity_recipient_tokens(db, booking)
            # Devices whose activity was remote-started while the app was
            # closed never upload a per-activity token — but a push-to-start
            # token also delivers updates to the activity it started
            # (input-push-token), so fall back to it for those devices.
            covered = {t.native_device_id for t in tokens}
            p2s_tokens = [
                t for t in _live_activity_push_to_start_recipients(db, booking)
                if t.native_device_id not in covered
            ]
            if not tokens and not p2s_tokens:
                return
            status = None
            if harvia:
                try:
                    status = status_with_f(harvia.get_full_status())
                except Exception as exc:
                    logger.warning("Live Activity update: Harvia status fetch failed: %s", exc)
            content_state = _live_activity_payload_for_booking(booking, status)
            _push_live_activity_to_tokens(db, tokens, content_state, event="update")
            if p2s_tokens:
                _push_live_activity_to_tokens(db, p2s_tokens, content_state, event="update")
    except Exception as exc:
        logger.error("push_live_activity_updates failed: %s", exc)


def cleanup_device_state_log():
    """Trim telemetry rows past the retention window (caps table growth)."""
    cutoff = app_now() - timedelta(days=DEVICE_LOG_RETENTION_DAYS)
    db = SessionLocal()
    try:
        deleted = db.query(DeviceStateLog).filter(DeviceStateLog.ts < cutoff).delete()
        db.commit()
        if deleted:
            logger.info("device state log cleanup: removed %s rows older than %s days", deleted, DEVICE_LOG_RETENTION_DAYS)
    except Exception as exc:
        logger.warning("device state log cleanup failed: %s", exc)
        db.rollback()
    finally:
        db.close()


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_and_auto_shutoff,    "interval", seconds=60,  id="auto_shutoff")
# Preheat reminders removed: Live Activity carries the session on the Lock Screen
# and Dynamic Island, and the family preferred fewer proactive alerts.
scheduler.add_job(check_session_ending,      "interval", seconds=60,  id="session_ending")
scheduler.add_job(refresh_harvia_token,      "interval", minutes=30,  id="token_refresh")
scheduler.add_job(log_device_state,          "interval", seconds=60,  id="device_state_log")
scheduler.add_job(push_live_activity_updates,"interval", seconds=60,  id="live_activity_updates")
scheduler.add_job(cleanup_device_state_log,  "interval", hours=24,    id="device_state_cleanup")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    import re
    body = request.get_json(silent=True) or {}
    name     = body.get("name", "").strip()
    email    = body.get("email", "").strip().lower()
    password = body.get("password", "")
    color    = body.get("color", "#F97316")

    if not name:
        return err("Name is required")
    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return err("A valid email address is required")
    pw_err = _validate_password(password)
    if pw_err:
        return err(pw_err)

    db = SessionLocal()
    try:
        # Check email uniqueness
        if db.query(FamilyMember).filter(FamilyMember.email == email).first():
            return err("An account with that email already exists", 409)

        # First-ever signup is auto-approved as admin
        existing_count = db.query(FamilyMember).count()
        is_first = existing_count == 0

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        member = FamilyMember(
            name=name,
            email=email,
            password_hash=password_hash,
            status="approved" if is_first else "pending",
            is_admin=1 if is_first else 0,
            color=color,
        )
        db.add(member)
        db.commit()
        db.refresh(member)

        session.permanent = True
        session["member_id"] = member.id
        csrf = _generate_csrf_token()

        if is_first:
            return jsonify({"ok": True, "status": "approved", "member": member.to_dict(), "csrf_token": csrf}), 201

        _notify_admins({
            "title": "👤 New signup",
            "body": f"{name} wants to join — approve them in Settings.",
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
    email    = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return err("Email and password are required")

    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter(FamilyMember.email == email).first()
        if not member or not member.password_hash:
            _record_failed_attempt(ip)
            return err("Invalid email or password", 401)
        if member.status == "pending":
            return err("Your account is waiting for approval", 403)
        if member.status == "rejected":
            return err("Your account request was not approved", 403)
        if not bcrypt.checkpw(password.encode(), member.password_hash.encode()):
            remaining = _record_failed_attempt(ip)
            msg = "Invalid email or password"
            if remaining <= 3:
                msg += f" — {remaining} attempt{'s' if remaining != 1 else ''} remaining before lockout"
            return err(msg, 401)

        _clear_attempts(ip)
        session.permanent = True
        session["member_id"] = member.id
        return jsonify({"ok": True, "member": member.to_dict(), "csrf_token": _generate_csrf_token()})
    finally:
        db.close()


@app.route("/api/auth/migrate", methods=["POST"])
def migrate():
    """Let an existing PIN-only user link email/password to their account."""
    import re
    body      = request.get_json(silent=True) or {}
    member_id = body.get("member_id")
    pin       = str(body.get("pin", "")).strip()
    email     = body.get("email", "").strip().lower()
    password  = body.get("password", "")

    if not member_id or not pin:
        return err("member_id and pin are required")
    try:
        member_id = int(member_id)
    except (ValueError, TypeError):
        return err("Invalid member_id", 400)

    if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return err("A valid email address is required")
    pw_err = _validate_password(password)
    if pw_err:
        return err(pw_err)

    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member or not member.pin_hash:
            return err("Invalid credentials", 401)
        if member.email is not None:
            return err("Account already migrated — please log in with your email", 409)
        if not bcrypt.checkpw(pin.encode(), member.pin_hash.encode()):
            return err("Incorrect PIN", 401)

        # Check email uniqueness
        if db.query(FamilyMember).filter(FamilyMember.email == email).first():
            return err("An account with that email already exists", 409)

        member.email         = email
        member.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        member.pin_hash      = None
        db.commit()
        db.refresh(member)

        session.permanent = True
        session["member_id"] = member.id
        return jsonify({"ok": True, "member": member.to_dict(), "csrf_token": _generate_csrf_token()})
    finally:
        db.close()


@app.route("/api/auth/forgot-password", methods=["POST"])
def forgot_password():
    """Initiate a password reset — always returns 200 to avoid revealing whether email exists."""
    body  = request.get_json(silent=True) or {}
    email = body.get("email", "").strip().lower()

    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter(FamilyMember.email == email).first()
        if member:
            token = secrets.token_urlsafe(32)
            member.reset_token         = token
            member.reset_token_expires = app_now() + timedelta(hours=1)
            db.commit()
            reset_link = f"{APP_URL}/?reset_token={token}"
            # Send in background so slow/failing SMTP doesn't block the response
            threading.Thread(
                target=_send_email,
                args=(email, "Reset your Sweat Box password",
                    f"Hi {member.name},\n\n"
                    f"Click the link below to reset your Sweat Box password. "
                    f"This link expires in 1 hour.\n\n"
                    f"{reset_link}\n\n"
                    f"If you didn't request this, you can safely ignore this email.\n"
                ),
                daemon=True,
            ).start()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    """Complete a password reset using a token from the reset email."""
    body         = request.get_json(silent=True) or {}
    token        = body.get("token", "").strip()
    new_password = body.get("new_password", "")

    pw_err = _validate_password(new_password)
    if pw_err:
        return err(pw_err)
    if not token:
        return err("Reset token is required", 400)

    db = SessionLocal()
    try:
        member = db.query(FamilyMember).filter(FamilyMember.reset_token == token).first()
        if not member or not member.reset_token_expires or member.reset_token_expires < app_now():
            return err("Reset link is invalid or has expired", 400)

        member.password_hash        = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        member.reset_token          = None
        member.reset_token_expires  = None
        db.commit()
        db.refresh(member)

        session.permanent = True
        session["member_id"] = member.id
        return jsonify({"ok": True, "member": member.to_dict(), "csrf_token": _generate_csrf_token()})
    finally:
        db.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    # If the client says which native device it's on, drop that device row so
    # the previous member's APNs/LiveActivity tokens can't keep hitting this
    # phone. Cascade-deletes the related LiveActivityToken /
    # LiveActivityPushToStartToken rows via ON DELETE CASCADE.
    member_id = session.get("member_id")
    native_device_id = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        native_device_id = (body.get("nativeDeviceId") or body.get("native_device_id") or "").strip() or None

    if member_id and native_device_id:
        db = SessionLocal()
        try:
            device = (
                db.query(NativeDevice)
                .filter_by(native_device_id=native_device_id, member_id=member_id)
                .first()
            )
            if device:
                db.delete(device)
                db.commit()
        except Exception as exc:
            logger.warning("Failed to clean up native device on logout: %s", exc)
        finally:
            db.close()

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
        if not member or member.status == "rejected":
            session.clear()
            return jsonify({"member": None})
        # Client uses this to gate the Settings notification UI: on the native
        # app there's no Web Push subscription, so `has_apns_token` is what
        # tells the frontend "notifications are wired up for this account".
        has_apns_token = (
            db.query(NativeDevice)
            .filter(NativeDevice.member_id == member.id, NativeDevice.apns_token.isnot(None))
            .first() is not None
        )
        return jsonify({
            "member": member.to_dict(),
            "has_apns_token": has_apns_token,
            "csrf_token": _generate_csrf_token(),
        })
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
        _notify_member(approved_id, {
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


@app.route("/api/admin/members/<int:member_id>/reset-pin", methods=["POST"])
def admin_reset_pin(member_id: int):
    db, _, error = require_admin()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    new_pin = str(body.get("new_pin", "")).strip()
    if not new_pin or len(new_pin) != 4 or not new_pin.isdigit():
        return err("new_pin must be exactly 4 digits")
    try:
        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)
        member.pin_hash = bcrypt.hashpw(new_pin.encode(), bcrypt.gensalt()).decode()
        db.commit()
        return jsonify({"ok": True})
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


@app.route("/api/admin/members/<int:member_id>/set-credentials", methods=["PUT"])
def admin_set_credentials(member_id: int):
    """Admin escape hatch — manually set email + password for a member who cannot self-migrate."""
    import re
    db, _, error = require_admin()
    if error:
        return error
    try:
        body     = request.get_json(silent=True) or {}
        email    = body.get("email", "").strip().lower()
        password = body.get("password", "")

        if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return err("A valid email address is required")
        pw_err = _validate_password(password)
        if pw_err:
            return err(pw_err)

        member = db.query(FamilyMember).filter_by(id=member_id).first()
        if not member:
            return err("Member not found", 404)

        # Check email uniqueness (excluding this member)
        existing = db.query(FamilyMember).filter(
            FamilyMember.email == email, FamilyMember.id != member_id
        ).first()
        if existing:
            return err("An account with that email already exists", 409)

        member.email         = email
        member.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        member.pin_hash      = None
        db.commit()
        db.refresh(member)
        return jsonify({"ok": True, "member": member.to_dict()})
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


@app.route("/api/members/<int:member_id>/change-password", methods=["POST"])
def change_password(member_id: int):
    """Authenticated users can change their own password."""
    db, member, error = require_auth()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    current_password = body.get("current_password", "")
    new_password     = body.get("new_password", "")
    if not current_password or not new_password:
        return err("current_password and new_password are required")
    pw_err = _validate_password(new_password)
    if pw_err:
        return err(pw_err)
    try:
        if member.id != member_id:
            return err("Cannot change another member's password", 403)
        if not member.password_hash:
            return err("No password set — use the migration flow to create one", 400)
        if not bcrypt.checkpw(current_password.encode(), member.password_hash.encode()):
            return err("Current password is incorrect", 401)
        member.password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sauna control routes (require auth)
# ---------------------------------------------------------------------------

@app.route("/api/sauna/status")
def sauna_status():
    # Status is readable without auth (shown on login screen too)
    #
    # ?cached=1 serves the last status polled by the 60s device-state job
    # (≤~60s old) without hitting Harvia — used by clients that auto-refresh
    # every minute during an active session.  Falls back to a live fetch if the
    # cache is stale or empty.
    if request.args.get("cached") == "1":
        cached, _age = _get_cached_status()
        if cached is not None:
            return jsonify(_status_with_start_failure(cached))
    try:
        status = get_harvia().get_full_status()
        _store_status_cache(status)
        return jsonify(_status_with_start_failure(status))
    except Exception as exc:
        logger.error("Status fetch failed: %s", exc)
        return err(str(exc), 502)


def _status_with_start_failure(status: dict) -> dict:
    """Status payload plus a recent unconfirmed-start marker, so a client that
    optimistically rendered "starting…" can correct itself without relying on
    the push notification landing."""
    out = status_with_f(status)
    failure = _get_start_failure()
    if failure and not out.get("active"):
        out["startFailure"] = {
            "message": failure["message"],
            "at": int(failure["at"] * 1000),
        }
    return out


# Heating-rate model (two-phase).  The heater runs flat out at a near-constant
# °C/min until it closes to within _TAIL_ZONE_C of the setpoint, then the
# controller starts cycling and the last few degrees crawl.  Telemetry backs
# this up: instantaneous rate sits at ~2 °C/min for every gap band above 10 °C
# and collapses to ~0 below it, and the time to cover that final zone is
# near-constant (~7 min) regardless of how high the target is.  So we fit two
# numbers from DeviceStateLog — the linear-phase rate and the tail duration —
# rather than one slope across the whole curve.
_HEAT_RATE_CACHE_TTL = 600.0
_heat_rate_lock = threading.Lock()
_heat_rate_cache: dict = {"ts": 0.0, "model": None}

# Segment extraction thresholds
_SEGMENT_GAP_SECONDS = 180       # rows further apart than this start a new segment
_SEGMENT_MIN_MINUTES = 5.0
_SEGMENT_MIN_DELTA_C = 5.0
_TAIL_ZONE_C = 10                # within this of target, the approach is no longer linear
_MIN_SEGMENT_POINTS = 6          # linear phase needs enough rows to be a real ramp
_MIN_MODEL_SAMPLES = 3
_ESTIMATE_FLOOR_MINUTES = 3      # never promise sooner than the observed floor

# Fallbacks for a cold install with no telemetry yet.  Only used to fill in one
# half of the model when the other half has samples; with no samples at all the
# estimate stays None and the UI shows nothing.
_DEFAULT_RATE_C_PER_MIN = 1.9
_DEFAULT_TAIL_MINUTES = 7.0


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def _split_heating_segments(rows: list) -> list[list]:
    """Group telemetry rows into contiguous runs of an active heating session."""
    segments: list[list] = []
    current: list = []
    prev_ts = None
    for r in rows:
        gap = (r.ts - prev_ts).total_seconds() if prev_ts else 0
        prev_ts = r.ts
        if not r.active or gap > _SEGMENT_GAP_SECONDS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(r)
    if current:
        segments.append(current)
    return segments


def _segment_linear_rate(seg: list) -> float | None:
    """°C/min over the full-power ramp — the run *before* the first approach to target.

    Takes a contiguous prefix and stops at the first crossing into the tail zone.
    Filtering point-by-point instead would let a late dip below the threshold
    (temperature oscillates around the setpoint for the rest of the session)
    stretch the window across the whole plateau and halve the apparent rate.
    """
    pts = []
    for p in seg:
        if p.temperature is None or p.target_temp is None:
            continue
        if p.temperature >= p.target_temp - _TAIL_ZONE_C:
            break
        pts.append(p)
    if len(pts) < _MIN_SEGMENT_POINTS:
        return None
    minutes = (pts[-1].ts - pts[0].ts).total_seconds() / 60
    delta = pts[-1].temperature - pts[0].temperature
    if minutes < _SEGMENT_MIN_MINUTES or delta < _SEGMENT_MIN_DELTA_C:
        return None
    return delta / minutes


def _segment_tail_minutes(seg: list) -> float | None:
    """Minutes from entering the tail zone to actually reaching target."""
    hit = next(
        (p for p in seg
         if p.temperature is not None and p.target_temp is not None
         and p.temperature >= p.target_temp),
        None,
    )
    if hit is None:
        return None
    cross = next(
        (p for p in seg
         if p.temperature is not None and p.temperature >= hit.target_temp - _TAIL_ZONE_C),
        None,
    )
    if cross is None or cross.ts >= hit.ts:
        return None
    return (hit.ts - cross.ts).total_seconds() / 60


def _compute_heating_model(db) -> dict | None:
    """Fit (linear rate, tail duration) from the last 30 days of telemetry.

    None until there's enough history to fit at least one of the two halves.
    """
    now = _time.time()
    with _heat_rate_lock:
        if now - _heat_rate_cache["ts"] > _HEAT_RATE_CACHE_TTL:
            rows = (
                db.query(DeviceStateLog)
                .filter(DeviceStateLog.ts >= app_now() - timedelta(days=30))
                .order_by(DeviceStateLog.ts)
                .all()
            )
            rates: list[float] = []
            tails: list[float] = []
            for seg in _split_heating_segments(rows):
                rate = _segment_linear_rate(seg)
                if rate is not None:
                    rates.append(rate)
                tail = _segment_tail_minutes(seg)
                if tail is not None:
                    tails.append(tail)

            model = None
            if len(rates) >= _MIN_MODEL_SAMPLES or len(tails) >= _MIN_MODEL_SAMPLES:
                model = {
                    "rate_c_per_min": _median(rates) if len(rates) >= _MIN_MODEL_SAMPLES
                                      else _DEFAULT_RATE_C_PER_MIN,
                    "tail_minutes": _median(tails) if len(tails) >= _MIN_MODEL_SAMPLES
                                    else _DEFAULT_TAIL_MINUTES,
                    "samples": max(len(rates), len(tails)),
                }
            _heat_rate_cache["model"] = model
            _heat_rate_cache["ts"] = now

        return _heat_rate_cache["model"]


def _estimate_minutes_to_target(gap_c: float, model: dict) -> int:
    """Minutes to close `gap_c` °C: linear ramp, then the sub-linear approach.

    The tail is sqrt-shaped rather than linear because the final degrees flatten
    out — at a 1 °C gap the observed median is still ~2 min, where a straight
    line would claim well under one.  Floored for the same reason.
    """
    if gap_c <= 0:
        return 0
    ramp = max(0.0, gap_c - _TAIL_ZONE_C) / model["rate_c_per_min"]
    tail = model["tail_minutes"] * math.sqrt(min(gap_c, _TAIL_ZONE_C) / _TAIL_ZONE_C)
    return max(_ESTIMATE_FLOOR_MINUTES, round(ramp + tail))


@app.route("/api/sauna/heat-estimate")
def sauna_heat_estimate():
    db, member, error = require_auth()
    if error:
        return error
    try:
        target = request.args.get("target", type=int)
        target_f = request.args.get("target_f", type=int)
        if target is None and target_f is not None:
            target = f_to_c(target_f)
        if target is None:
            return err("target (°C) or target_f required")

        current_temp = None
        status, _age = _get_cached_status()
        if status is None and harvia:
            try:
                status = harvia.get_full_status()
                _store_status_cache(status)
            except Exception as exc:
                logger.warning("heat-estimate: status fetch failed: %s", exc)
        if status is not None:
            current_temp = status.get("temperature")

        outdoor = _get_outdoor_temp()
        model = _compute_heating_model(db)

        minutes = None
        if model and current_temp is not None:
            minutes = _estimate_minutes_to_target(target - current_temp, model)

        return jsonify({
            "minutes_to_target": minutes,
            "rate_c_per_min": round(model["rate_c_per_min"], 2) if model else None,
            "tail_minutes": round(model["tail_minutes"], 1) if model else None,
            "current_temp": current_temp,
            "target_temp": target,
            "outdoor_temp": outdoor,
            "samples": model["samples"] if model else 0,
        })
    finally:
        db.close()


def _complete_running_bookings() -> None:
    """Mark today's running tracking bookings completed when the sauna is turned off.

    The physical sauna serves one session at a time, so turning it off ends the
    current session.  Completing the booking removes it from the auto-shutoff and
    session-ending scheduler queries (both filter on status), preventing stray
    '15 minutes left' alerts and stale auto-shutoffs.  Best-effort — never raises.
    """
    try:
        now = app_now()
        today = now.date()
        current_time = now.time()
        yesterday = today - timedelta(days=1)

        db = SessionLocal()
        try:
            running = (
                db.query(Booking)
                .filter(
                    Booking.date == today,
                    Booking.status.in_(["active", "preheating"]),
                )
                .all()
            )
            # Midnight-spanning sessions started yesterday, still running now.
            running += (
                db.query(Booking)
                .filter(
                    Booking.date == yesterday,
                    Booking.end_time < Booking.start_time,
                    Booking.end_time > current_time,
                    Booking.status.in_(["active", "preheating"]),
                )
                .all()
            )
            for b in running:
                b.status = "completed"
                logger.info("Completed booking #%d — sauna turned off", b.id)
            if running:
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Completing running bookings failed (non-fatal): %s", exc)


def _auto_create_booking(member_id: int, member_name: str, target_c: int, on_time_mins: int) -> int | None:
    """Create a tracking booking when the sauna is turned on.

    Best-effort — never raises.  Turning the sauna on starts a new physical
    session, so any booking the member already has running today is completed
    first and a fresh tracking booking is created with the new end time.  This
    keeps the auto-shutoff / session-ending scheduler aligned with the device.
    """
    try:
        now = app_now()
        today = now.date()
        start = now.time().replace(second=0, microsecond=0)
        end_dt = now + timedelta(minutes=on_time_mins)
        end = end_dt.time().replace(second=0, microsecond=0)

        db = SessionLocal()
        try:
            prior = (
                db.query(Booking)
                .filter(
                    Booking.member_id == member_id,
                    Booking.date == today,
                    Booking.status.in_(["scheduled", "preheating", "active"]),
                )
                .all()
            )
            for b in prior:
                b.status = "completed"
                logger.info(
                    "Completed prior booking #%d for %s — new session starting",
                    b.id, member_name,
                )

            booking = Booking(
                member_id=member_id,
                date=today,
                start_time=start,
                end_time=end,
                target_temp=target_c,
                on_time=on_time_mins,
                status="preheating",
            )
            db.add(booking)
            db.commit()
            booking_id = booking.id
            logger.info(
                "Auto-booking created for %s: %s–%s %d°C %d min",
                member_name, start.strftime("%H:%M"), end.strftime("%H:%M"), target_c, on_time_mins,
            )
            return booking_id
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Auto-booking creation failed (non-fatal): %s", exc)
        return None


def _abandon_failed_start_booking(booking_id: int | None, reason: str) -> None:
    """Undo the optimistic session state after an unconfirmed start.

    Cancels the booking (it only exists because we assumed the heater lit) and
    ends any Live Activity showing it, so the Lock Screen stops counting down a
    session that never began.  Never raises — it runs on the activation thread.
    """
    if booking_id is None:
        return
    try:
        with SessionLocal() as db:
            booking = db.query(Booking).filter_by(id=booking_id).first()
            if booking and booking.status in ("scheduled", "preheating", "active"):
                booking.status = "cancelled"
                db.commit()
                logger.warning("Cancelled booking #%d — sauna never confirmed it started (%s)",
                               booking_id, reason)
    except Exception as exc:
        logger.error("Could not cancel booking #%s after a failed start: %s", booking_id, exc)
    _fanout_live_activity_end(booking_id, reason="start-failed")


def _revert_failed_preheat(booking_id: int, reason: str) -> None:
    """Preheat variant: the booking is real and still upcoming, so put it back
    to `scheduled` (preheat can be retried) rather than cancelling it."""
    try:
        with SessionLocal() as db:
            booking = db.query(Booking).filter_by(id=booking_id).first()
            if booking and booking.status == "preheating":
                booking.status = "scheduled"
                db.commit()
                logger.warning("Reverted booking #%d to scheduled — preheat never confirmed (%s)",
                               booking_id, reason)
    except Exception as exc:
        logger.error("Could not revert booking #%s after a failed preheat: %s", booking_id, exc)
    _fanout_live_activity_end(booking_id, reason="start-failed")


def _activate_sauna_async(
    target_c: int,
    device_minutes: int,
    member_id: int,
    context: str,
    failure_body: str,
    after_activate=None,
    on_failure=None,
) -> None:
    """Finish a staged turn-on in the background (B1 — see the activation
    generation notes above).

    The endpoint has already run turn_on_stage(); this waits for the device to
    commit onTime, sends active=1 and *verifies* the device reports it, all off
    the request thread.  `after_activate` runs post-activation work that should
    only fire once the sauna is actually on (admin alerts, Live Activity
    fanout).  `on_failure` unwinds the optimistic state the endpoint already
    committed (booking status, Live Activity) when it isn't.

    Everything downstream of a start is optimistic — the response, the booking
    row, the Live Activity — so an unconfirmed activation MUST land here loudly:
    time-sensitive alert to the member and the admins, a status flag for the
    polling UI, and the caller's unwind.
    """
    gen = _next_activation_generation()

    def fail(reason: str, detail: str) -> None:
        if not _activation_still_current(gen):
            # A turn-off (or a newer start) superseded us — the sauna being off
            # is the intended outcome, not a failure worth alarming anyone over.
            logger.info("Ignoring unconfirmed start (%s) — superseded: %s", context, detail)
            return
        logger.error("Sauna start NOT confirmed (%s): %s", context, detail)
        _record_start_failure(reason, context)
        if on_failure is not None:
            try:
                on_failure(reason)
            except Exception as exc:
                logger.error("Start-failure unwind failed (%s): %s", context, exc)
        alert = {
            "title": "⚠️ Sauna did not start",
            "body": f"{failure_body} {reason}",
            "tag": "sauna-start-failed",
            "thread_id": "sauna-safety",
            "interruption_level": "time-sensitive",
            "url": "/",
        }
        _notify_member(member_id, alert)
        _notify_admins(alert, pref_key="sauna_control", exclude_member_id=member_id)

    def run():
        try:
            result = get_harvia().turn_on_activate(
                target_c, device_minutes,
                should_activate=lambda: _activation_still_current(gen),
            )
        except SaunaStartUnconfirmed as exc:
            fail(str(exc), f"{type(exc).__name__}: {exc}")
            return
        except SaunaOffline as exc:
            fail(str(exc), f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:
            fail(
                "The sauna could not be reached to confirm it started.",
                f"{type(exc).__name__}: {exc}",
            )
            return
        if result is None:
            logger.info("Sauna activation cancelled (%s) — superseded by turn-off/newer start", context)
            return
        _clear_start_failure()
        if after_activate is not None:
            try:
                after_activate()
            except Exception as exc:
                logger.error("Post-activation work failed (%s): %s", context, exc)

    threading.Thread(target=run, daemon=True, name=f"sauna-activate-{gen}").start()


@app.route("/api/sauna/on", methods=["POST"])
def sauna_on():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        logger.info("sauna/on request body: %s", body)
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
        if "onTime" in body and body["onTime"] is not None:
            on_time = int(body["onTime"])
        else:
            logger.warning("sauna/on missing onTime — defaulting to 60 min (body: %s)", body)
            on_time = 60
        mid, mname = member.id, member.name
        native_device_id = (body.get("nativeDeviceId") or body.get("native_device_id") or "").strip() or None
    finally:
        db.close()
    # Stage synchronously (fast — one Harvia call, proves the device is
    # reachable), then finish the slow commit-wait + activate in the
    # background so the response returns in ~1s instead of 20-45s.
    try:
        logger.info("sauna/on → staging (target_c=%d °C / %d °F, on_time=%d min)", target_c, c_to_f(target_c), on_time)
        device_minutes = get_harvia().turn_on_stage(target_c, on_time)
    except SaunaOffline as exc:
        logger.warning("Turn on refused — device offline: %s", exc)
        return err(str(exc), 503)
    except Exception as exc:
        logger.error("Turn on failed: %s", exc)
        return err(str(exc), 502)

    _clear_start_failure()
    booking_id = _auto_create_booking(mid, mname, target_c, on_time)
    _log_sauna_action(mid, mname, "on", target_temp=target_c, on_time=on_time,
                      notes=json.dumps({"target_f": c_to_f(target_c)}))

    def _after_on():
        if booking_id is not None:
            _fanout_live_activity_start(booking_id, exclude_native_device_id=native_device_id)
        _notify_admins({
            "title": "🔥 Sauna is on",
            "body": f"{mname} started a session — {c_to_f(target_c)}°F ({target_c}°C) for {on_time} min.",
            "tag": "sauna-on",
            "url": "/",
        }, pref_key="sauna_control")

    def _on_start_failed(reason: str):
        # The booking exists only because we assumed the start worked; drop it
        # and tear down any Live Activity the phone started optimistically, so
        # nothing is left claiming a session is running.
        _abandon_failed_start_booking(booking_id, reason)

    _activate_sauna_async(
        target_c, device_minutes, mid,
        context=f"sauna/on by {mname}",
        failure_body="The start command was accepted but the sauna never switched on.",
        after_activate=_after_on,
        on_failure=_on_start_failed,
    )
    return jsonify({
        "ok": True,
        "pending": True,  # activation continues in the background
        "bookingId": booking_id,
        "targetTemp": target_c,
        "targetTempF": c_to_f(target_c),
        "onTime": on_time,
    })


@app.route("/api/sauna/off", methods=["POST"])
def sauna_off():
    db, member, error = require_auth()
    if error:
        return error
    try:
        mid, mname = member.id, member.name
        running = _current_live_booking(db)
        running_booking_id = running.id if running else None
    finally:
        db.close()
    try:
        _safe_turn_off(f"manual off by {mname}")
        _complete_running_bookings()
        _log_sauna_action(mid, mname, "off")
        if running_booking_id is not None:
            _fanout_live_activity_end(running_booking_id, reason="manual-off")
        _notify_admins({
            "title": "❄️ Sauna turned off",
            "body": f"Turned off by {mname}.",
            "tag": "sauna-off",
            "url": "/",
        }, pref_key="sauna_control")
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Turn off failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/sauna/extend", methods=["POST"])
def sauna_extend():
    """Add time to the running session.

    The Harvia device only reads onTime when active transitions 0→1, so
    the only reliable way to update remaining time mid-session is to turn
    off then immediately turn back on with the new onTime.  The booking
    is NOT modified — this controls the physical sauna only.
    """
    db, member, error = require_auth()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    try:
        add_minutes = max(1, min(30, int(data.get("minutes", 15))))
    except (TypeError, ValueError):
        add_minutes = 15

    try:
        now = app_now()
        today = now.date()
        active_booking = (
            db.query(Booking)
            .filter(
                Booking.date == today,
                Booking.status.in_(["active", "preheating", "scheduled"]),
            )
            .first()
        )
        if not active_booking:
            return err("No active session to extend")
        if not _can_control_booking(db, active_booking, member):
            return err("Join this session before extending it", 403)
        mid, mname = member.id, member.name
    finally:
        db.close()

    try:
        # Use values supplied by the frontend to avoid an extra API round-trip.
        client_remaining   = data.get("remaining")
        client_target_temp = data.get("targetTemp")

        if client_remaining is not None and client_target_temp is not None:
            new_on_time = int(client_remaining) + add_minutes
            target_temp = int(client_target_temp)
        else:
            status = get_harvia().get_full_status()
            target_temp = status.get("targetTemp") or 90
            remaining = status.get("remainingTime") or status.get("onTime") or add_minutes
            new_on_time = int(remaining) + add_minutes

        logger.info(
            "sauna/extend → cycling off→on: target_c=%d °C / %d °F, new_on_time=%d min (remaining=%s + add=%d, no booking modified)",
            target_temp, c_to_f(target_temp), new_on_time, client_remaining, add_minutes,
        )
        # Cycle off → stage synchronously (both fast), then finish the slow
        # commit-wait + reactivate in the background so the device resets its
        # countdown to new_on_time without blowing the client timeout.
        get_harvia().turn_off()
        _time.sleep(1.5)
        device_minutes = get_harvia().turn_on_stage(target_temp, new_on_time)
        _clear_start_failure()
        _activate_sauna_async(
            target_temp, device_minutes, mid,
            context=f"sauna/extend by {mname}",
            failure_body="Extending turned the sauna off but couldn't confirm it restarted — check the sauna.",
        )
        _log_sauna_action(mid, mname, "extend", target_temp=target_temp, on_time=new_on_time,
                          notes=json.dumps({"added_mins": add_minutes, "prev_remaining": client_remaining,
                                            "target_f": c_to_f(target_temp)}))
        return jsonify({"ok": True, "pending": True, "addedMinutes": add_minutes, "newOnTime": new_on_time})
    except Exception as exc:
        logger.error("Extend session failed: %s", exc)
        return err(str(exc), 502)


@app.route("/api/bookings/<int:booking_id>/extend", methods=["POST"])
def extend_booking_time(booking_id: int):
    """Extend the app's booking window without touching the Harvia timer."""
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    body = request.get_json(silent=True) or {}
    try:
        add_minutes = max(1, min(30, int(body.get("minutes", 15))))
    except (TypeError, ValueError):
        add_minutes = 15

    def booking_bounds(b: Booking):
        start_dt = datetime.combine(b.date, b.start_time)
        end_date = b.date + timedelta(days=1) if b.end_time <= b.start_time else b.date
        end_dt = datetime.combine(end_date, b.end_time)
        return start_dt, end_dt

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if not _can_control_booking(db, booking, member):
            return err("Join this session before extending it", 403)
        if booking.status not in ("scheduled", "preheating", "active"):
            return err(f"Cannot extend a booking with status '{booking.status}'")

        start_dt, end_dt = booking_bounds(booking)
        now = app_now().replace(tzinfo=None)
        if end_dt <= now:
            return err("Cannot extend a session that has already ended")

        new_end_dt = end_dt + timedelta(minutes=add_minutes)
        new_end_time = new_end_dt.time().replace(second=0, microsecond=0)
        check_start = start_dt - timedelta(minutes=COOLDOWN_MINUTES)
        check_end = new_end_dt + timedelta(minutes=COOLDOWN_MINUTES)

        nearby = (
            db.query(Booking)
            .filter(
                Booking.id != booking.id,
                Booking.status != "cancelled",
                Booking.date >= booking.date - timedelta(days=1),
                Booking.date <= booking.date + timedelta(days=1),
            )
            .all()
        )
        for other in nearby:
            other_start, other_end = booking_bounds(other)
            other_start -= timedelta(minutes=COOLDOWN_MINUTES)
            other_end += timedelta(minutes=COOLDOWN_MINUTES)
            if check_start < other_end and check_end > other_start:
                return err(
                    f"Extension would overlap with {other.member.name if other.member else 'another booking'} "
                    f"({other.start_time.strftime('%H:%M')}–{other.end_time.strftime('%H:%M')})"
                )

        booking.end_time = new_end_time
        booking.on_time = int((new_end_dt - start_dt).total_seconds() / 60)
        db.commit()
        db.refresh(booking)

        _log_sauna_action(
            member.id,
            member.name,
            "extend",
            target_temp=booking.target_temp,
            on_time=booking.on_time,
            notes=json.dumps({"added_mins": add_minutes, "booking_only": True}),
        )
        return jsonify({"ok": True, "addedMinutes": add_minutes, "booking": _booking_to_dict_for(booking, member)})
    finally:
        db.close()


@app.route("/api/sauna/set", methods=["POST"])
def sauna_set():
    """Update sauna settings mid-session.

    If onTime is included (the Update button sends both temp + duration),
    the device requires an off→on cycle to register the new timer value.
    Temperature-only changes are sent directly via set_state.
    """
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        logger.info("sauna/set request body: %s", body)
        allowed = {"active", "targetTemp", "onTime", "maxOnTime", "maxTemp", "light", "fan", "steamEn", "targetRh"}
        payload = {k: v for k, v in body.items() if k in allowed}
        if "targetTempF" in body and "targetTemp" not in payload:
            payload["targetTemp"] = f_to_c(float(body["targetTempF"]))
        if "targetTemp" in payload:
            payload["targetTemp"] = max(40, min(110, int(payload["targetTemp"])))
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
        diag = {**({"target_f": c_to_f(log_target)} if log_target else {}), **extra}
        notes = json.dumps(diag) if diag else None

        pending = False
        if "onTime" in payload and "targetTemp" in payload:
            # Device only registers onTime changes on an active 0→1 transition.
            # Cycle off → stage synchronously, reactivate in the background.
            target_temp = payload["targetTemp"]
            on_time     = int(payload["onTime"])
            logger.info("sauna/set → cycling off→on: target_c=%d °C / %d °F, on_time=%d min (no booking modified)", target_temp, c_to_f(target_temp), on_time)
            get_harvia().turn_off()
            _time.sleep(1.5)
            device_minutes = get_harvia().turn_on_stage(target_temp, on_time)
            _clear_start_failure()
            _activate_sauna_async(
                target_temp, device_minutes, mid,
                context=f"sauna/set by {mname}",
                failure_body="Updating settings turned the sauna off but couldn't confirm it restarted — check the sauna.",
            )
            pending = True
        else:
            # Temperature-only or other field changes — send directly.
            get_harvia().set_state(payload)

        _log_sauna_action(mid, mname, "set", target_temp=log_target, on_time=log_on_time, notes=notes)
        return jsonify({"ok": True, "pending": pending, "applied": payload})
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
        native_device_id = (request.get_json(silent=True) or {}).get("nativeDeviceId")
        native_device_id = (native_device_id or "").strip() or None
    finally:
        db.close()
    # maxOnTime must match onTime — device firmware clamps actual timer to maxOnTime.
    payload = {"targetTemp": p_temp, "onTime": p_time, "maxOnTime": p_time, "active": 1}
    if p_steam:
        payload["steamEn"] = 1
        if p_rh:
            payload["targetRh"] = p_rh
    try:
        get_harvia().set_state(payload)
        booking_id = _auto_create_booking(mid, mname, p_temp, p_time)
        _log_sauna_action(mid, mname, "preset", target_temp=p_temp, on_time=p_time, preset_name=p_name)
        if booking_id is not None:
            _fanout_live_activity_start(booking_id, exclude_native_device_id=native_device_id)
        _notify_admins({
            "title": "🔥 Sauna is on",
            "body": f"{mname} started '{p_name}' — {c_to_f(p_temp)}°F ({p_temp}°C) for {p_time} min.",
            "tag": "sauna-on",
            "url": "/",
        }, pref_key="sauna_control")
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


@app.route("/api/config")
def get_config():
    return jsonify({"booking_window_days": BOOKING_WINDOW_DAYS})


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
        limit  = min(int(request.args.get("limit",   5)), 200)
        offset = max(int(request.args.get("offset",  0)),   0)
        total  = db.query(ControlLog).count()
        logs   = (
            db.query(ControlLog)
            .order_by(ControlLog.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return jsonify({
            "entries": [l.to_dict() for l in logs],
            "total":   total,
            "offset":  offset,
            "limit":   limit,
        })
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
        for key in ("session_ending", "sauna_control", "signup", "booking", "approval"):
            if key in body:
                prefs[key] = bool(body[key])
        if "live_activity_all_sessions" in body:
            if not target.is_admin:
                return err("Live Activities for all sessions are admin-only", 403)
            target.live_activity_all_sessions = 1 if body["live_activity_all_sessions"] else 0
        target.notification_prefs = json.dumps(prefs)
        db.commit()
        return jsonify({
            "ok": True,
            "notification_prefs": prefs,
            "live_activity_all_sessions": bool(target.live_activity_all_sessions),
        })
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
    """Send a test notification via whichever transport is available (APNS or Web Push).

    Returns diagnostic detail so the UI can tell a stuck registration ("no
    transport") apart from a failing send ("APNs rejected it")."""
    db, member, error = require_auth()
    if error:
        return error
    try:
        has_apns = (
            db.query(NativeDevice)
            .filter(NativeDevice.member_id == member.id, NativeDevice.apns_token.isnot(None))
            .first() is not None
        )
        has_web = db.query(PushSubscription).filter_by(member_id=member.id).first() is not None
        if not has_apns and not has_web:
            return err("No notification transport found — enable notifications first")

        result = _dispatch_alert_to_member(db, member.id, {
            "title": "🛖 Sweat Box test",
            "body": f"Hey {member.name}! Push notifications are working.",
            "tag": "push-test",
            "thread_id": "test",
            "url": "/",
        })
        db.commit()

        if result["sent"] == 0:
            return err(
                f"{result['transport'] or 'notification'} delivery failed "
                f"({result['failed']} attempt{'s' if result['failed'] != 1 else ''}) — "
                "check server logs",
                502,
            )
        return jsonify({
            "ok": True,
            "transport": result["transport"],
            "sent": result["sent"],
            "failed": result["failed"],
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Native iOS / ActivityKit routes
# ---------------------------------------------------------------------------

def _upsert_native_device(db, member: FamilyMember, body: dict) -> NativeDevice | None:
    native_device_id = (body.get("nativeDeviceId") or body.get("native_device_id") or "").strip()
    if not native_device_id:
        return None

    device = db.query(NativeDevice).filter_by(native_device_id=native_device_id).first()
    if not device:
        device = NativeDevice(native_device_id=native_device_id, member_id=member.id)
        db.add(device)
    device.member_id = member.id
    device.platform = (body.get("platform") or "ios").strip() or "ios"
    device.app_version = (body.get("appVersion") or body.get("app_version") or device.app_version)
    apns_token = (body.get("apnsToken") or body.get("apns_token") or "").strip()
    if apns_token:
        device.apns_token = apns_token
    # Client is authoritative: only accept "sandbox" or "production" and default
    # to production when unset — dev builds MUST tell us they're sandbox or the
    # token will bounce off the wrong APNs endpoint with BadDeviceToken.
    apns_env = (body.get("apnsEnvironment") or body.get("apns_environment") or "").strip().lower()
    if apns_env in ("sandbox", "production"):
        device.apns_environment = apns_env
    device.last_seen_at = datetime.utcnow()
    # Flush so the parent row lands before any child token rows reference it.
    # SQLAlchemy's flush ordering wasn't placing the NativeDevice INSERT before
    # LiveActivityToken / LiveActivityPushToStartToken INSERTs (no `relationship()`
    # declared between them, only raw ForeignKey), so SQLite's immediate FK check
    # was failing at commit.
    db.flush()
    return device


@app.route("/api/native/device-token", methods=["POST"])
def native_device_token():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        device = _upsert_native_device(db, member, body)
        if not device:
            return err("nativeDeviceId is required")
        db.commit()
        return jsonify({"ok": True, "nativeDeviceId": device.native_device_id})
    finally:
        db.close()


@app.route("/api/native/live-activity/push-to-start-token", methods=["POST"])
def native_push_to_start_token():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        device = _upsert_native_device(db, member, body)
        if not device:
            return err("nativeDeviceId is required")
        if not token:
            return err("token is required")

        existing = db.query(LiveActivityPushToStartToken).filter_by(native_device_id=device.native_device_id).first()
        if not existing:
            existing = db.query(LiveActivityPushToStartToken).filter_by(token=token).first()
        if existing:
            existing.member_id = member.id
            existing.native_device_id = device.native_device_id
            existing.token = token
            existing.updated_at = datetime.utcnow()
        else:
            db.add(LiveActivityPushToStartToken(
                member_id=member.id,
                native_device_id=device.native_device_id,
                token=token,
                updated_at=datetime.utcnow(),
            ))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/native/live-activity/token", methods=["POST"])
def native_live_activity_token():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        state = (body.get("state") or body.get("activityState") or "").strip()
        booking_id = body.get("bookingId") or body.get("booking_id")
        device = _upsert_native_device(db, member, body)
        if not device:
            return err("nativeDeviceId is required")
        if not token:
            return err("token is required")

        if state in ("ended", "dismissed"):
            db.query(LiveActivityToken).filter_by(token=token).delete()
            db.commit()
            return jsonify({"ok": True, "removed": True})

        existing = db.query(LiveActivityToken).filter_by(token=token).first()
        if existing:
            existing.member_id = member.id
            existing.native_device_id = device.native_device_id
            existing.booking_id = int(booking_id) if booking_id is not None else None
            existing.activity_state = state or existing.activity_state
            existing.updated_at = datetime.utcnow()
        else:
            db.add(LiveActivityToken(
                member_id=member.id,
                native_device_id=device.native_device_id,
                booking_id=int(booking_id) if booking_id is not None else None,
                token=token,
                activity_state=state or None,
                updated_at=datetime.utcnow(),
            ))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/native/live-activity/status", methods=["POST"])
def native_live_activity_status():
    """Logs Live Activity status events forwarded from the iOS bridge.

    Useful for diagnosing why a push token never arrived (e.g. push-token-failed
    fallback) without needing to attach Safari Web Inspector to the device.
    """
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        status = (body.get("status") or "").strip() or "unknown"
        activity_id = body.get("activityId")
        booking_id = body.get("bookingId")
        message = body.get("message")
        logger.info(
            "Live Activity status from member_id=%s: status=%s booking_id=%s activity_id=%s message=%s",
            member.id, status, booking_id, activity_id, message,
        )
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/native/live-activity/token", methods=["DELETE"])
def delete_native_live_activity_token():
    db, member, error = require_auth()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        native_device_id = (body.get("nativeDeviceId") or body.get("native_device_id") or "").strip()
        q = db.query(LiveActivityToken).filter_by(member_id=member.id)
        if token:
            q = q.filter_by(token=token)
        elif native_device_id:
            q = q.filter_by(native_device_id=native_device_id)
        else:
            return err("token or nativeDeviceId is required")
        removed = q.delete()
        db.commit()
        return jsonify({"ok": True, "removed": removed})
    finally:
        db.close()


@app.route("/api/native/live-session")
def native_live_session():
    """The Live Activity content-state for the caller's current session.

    Lets the app start an activity *on this device* through ActivityKit
    directly, instead of going out to APNs and back. Reads the cached device
    status (populated by the 60s job) so the activity opens with real temps
    rather than placeholders — no Harvia call on this path.
    """
    db, member, error = require_auth()
    if error:
        return error
    try:
        booking = _current_live_booking(db, member)
        if not booking and member.is_admin and member.live_activity_all_sessions:
            booking = _current_live_booking(db)
        if not booking:
            return jsonify({"session": None})
        cached, _age = _get_cached_status()
        status = status_with_f(cached) if cached else None
        return jsonify({"session": _live_activity_payload_for_booking(booking, status)})
    finally:
        db.close()


@app.route("/api/admin/native/live-activity/test", methods=["POST"])
def admin_test_live_activity_push():
    db, member, error = require_admin()
    if error:
        return error
    try:
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        environment: str | None = None
        if token:
            environment = (body.get("environment") or body.get("apnsEnvironment") or "").strip().lower() or None
        else:
            stored = (
                db.query(LiveActivityToken)
                .order_by(LiveActivityToken.updated_at.desc())
                .first()
            )
            token = stored.token if stored else ""
            if stored and stored.native_device_id:
                environment = _device_env_map(db, [stored.native_device_id]).get(stored.native_device_id)
        if not token:
            return err("No Live Activity token available")

        booking = _current_live_booking(db, member) or _current_live_booking(db)
        state = _live_activity_payload_for_booking(booking) if booking else {
            "bookingId": None,
            "memberId": member.id,
            "bookingName": "Test Session",
            "currentTempF": int(body.get("currentTempF") or 188),
            "targetTempF": int(body.get("targetTempF") or 194),
            "remainingMinutes": int(body.get("remainingMinutes") or 12),
            "heatOn": True,
            "active": True,
            "updatedAtMillis": int(_time.time() * 1000),
        }
        payload = {
            "aps": {
                "timestamp": int(_time.time()),
                "event": "update",
                "content-state": state,
            }
        }
        ok, status_code, response_body = _send_live_activity_apns(token, payload, environment=environment)
        if not ok:
            return err(response_body or "Live Activity push failed", status_code or 502)
        return jsonify({"ok": True, "status": status_code})
    finally:
        db.close()


# The manual "start current session Live Activity" admin endpoint was removed.
# It could only express "remote-start on every eligible device except this one"
# (exclude_native_device_id) — the semantics /api/sauna/on needs, because the
# starting phone has already created its own activity locally. Driving a manual
# button through it meant pushing to everyone else and never to yourself, while
# reporting APNs 200s as "sent to N devices". The button now starts the activity
# on its own device via ActivityKit (GET /api/native/live-session +
# NativeBridge.startSaunaSession). Remote fanout remains where it belongs:
# _fanout_live_activity_start(), called after a *confirmed* sauna start.


# ---------------------------------------------------------------------------
# Booking routes (require auth)
# ---------------------------------------------------------------------------

@app.route("/api/bookings")
def list_bookings():
    db_auth, member, error = require_auth()
    if error:
        return error
    db_auth.close()

    date_str      = request.args.get("date")
    date_from_str = request.args.get("date_from")
    date_to_str   = request.args.get("date_to")
    db = SessionLocal()
    try:
        q = (
            db.query(Booking)
            .options(selectinload(Booking.participants), selectinload(Booking.invites))
            .filter(Booking.status != "cancelled")
        )
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
        return jsonify([_booking_to_dict_for(b, member) for b in bookings])
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
        invite_member_ids = body.get("invite_member_ids") or []
        if isinstance(invite_member_ids, list) and all(isinstance(m, int) for m in invite_member_ids):
            _create_invites(db, booking, member, invite_member_ids)
        if not member.is_admin:
            day = booking_date.day
            month = booking_date.strftime("%b")
            weekday = booking_date.strftime("%a")
            start_fmt = start.strftime("%I:%M %p").lstrip("0")
            end_fmt = end.strftime("%I:%M %p").lstrip("0")
            _notify_admins({
                "title": "📅 New booking",
                "body": f"{member.name} booked {weekday} {day} {month}, {start_fmt}–{end_fmt}",
                "tag": f"booking-new-{booking.id}",
                "url": "/",
            }, pref_key="booking")
        return jsonify(_booking_to_dict_for(booking, member)), 201
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
                _cancel_pending_activations()
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

        # Date change
        new_date = booking.date
        if "date" in body:
            try:
                new_date = date.fromisoformat(body["date"])
            except ValueError as exc:
                return err(f"Invalid date: {exc}")

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
            on_time = int((datetime.combine(new_date, end) - datetime.combine(new_date, start)).seconds / 60)
        else:
            on_time = int((datetime.combine(new_date + timedelta(days=1), end) - datetime.combine(new_date, start)).seconds / 60)

        # Temperature
        target_temp = booking.target_temp
        if "target_temp_f" in body:
            target_temp = max(40, min(110, int(f_to_c(float(body["target_temp_f"])))))
        elif "target_temp" in body:
            target_temp = max(40, min(110, int(body["target_temp"])))

        # Overlap check — exclude the booking being edited, use new_date
        cooldown_start = (datetime.combine(new_date, start) - timedelta(minutes=COOLDOWN_MINUTES)).time()
        cooldown_end   = (datetime.combine(new_date, end)   + timedelta(minutes=COOLDOWN_MINUTES)).time()
        overlap = (
            db.query(Booking)
            .filter(
                Booking.id != booking_id,
                Booking.date == new_date,
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

        booking.date       = new_date
        booking.start_time = start
        booking.end_time   = end
        booking.on_time    = on_time
        booking.target_temp = target_temp
        db.commit()
        db.refresh(booking)
        if not member.is_admin:
            day = new_date.day
            month = new_date.strftime("%b")
            weekday = new_date.strftime("%a")
            start_fmt = start.strftime("%I:%M %p").lstrip("0")
            end_fmt = end.strftime("%I:%M %p").lstrip("0")
            _notify_admins({
                "title": "✏️ Booking updated",
                "body": f"{member.name}'s booking on {weekday} {day} {month} moved to {start_fmt}–{end_fmt}",
                "tag": f"booking-edit-{booking.id}",
                "url": "/",
            }, pref_key="booking")
        return jsonify(_booking_to_dict_for(booking, member))
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

    body = request.get_json(silent=True) or {}
    native_device_id = (body.get("nativeDeviceId") or body.get("native_device_id") or "").strip() or None

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if not _can_control_booking(db, booking, member):
            return err("Join this session before preheating it", 403)
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

        b_temp = booking.target_temp or 90
        b_time = booking.on_time or 60
        # Stage synchronously (fast reachability check), activate in the
        # background — turn_on's commit-wait takes 20-45s, past the client timeout.
        try:
            device_minutes = get_harvia().turn_on_stage(b_temp, b_time)
        except SaunaOffline as exc:
            logger.warning("Preheat refused — device offline: %s", exc)
            return err(str(exc), 503)
        except Exception as exc:
            logger.error("Preheat API call failed: %s", exc)
            return err(f"Harvia API error: {exc}", 502)

        _clear_start_failure()
        booking.status = "preheating"
        db.commit()
        booking_id = booking.id
        mid, mname = member.id, member.name

        def _after_preheat():
            _notify_admins({
                "title": "🔥 Sauna is preheating",
                "body": f"{mname} started preheat — {c_to_f(b_temp)}°F ({b_temp}°C) for {b_time} min.",
                "tag": "sauna-on",
                "url": "/",
            }, pref_key="sauna_control")
            _fanout_live_activity_start(booking_id, exclude_native_device_id=native_device_id)

        _activate_sauna_async(
            b_temp, device_minutes, mid,
            context=f"preheat booking #{booking_id} by {mname}",
            failure_body="Preheat was accepted but the sauna never switched on.",
            after_activate=_after_preheat,
            on_failure=lambda reason: _revert_failed_preheat(booking_id, reason),
        )
        return jsonify({"ok": True, "pending": True, "status": "preheating"})
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>/join", methods=["POST"])
def join_booking(booking_id: int):
    db, member, error = require_auth()
    if error:
        return error
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.status == "cancelled":
            return err("Cannot join a cancelled session")

        _add_participant(db, booking, member.id)
        # Joining answers any outstanding invite
        invite = db.query(BookingInvite).filter_by(booking_id=booking.id, member_id=member.id).first()
        if invite and invite.status != "yes":
            invite.status = "yes"
            invite.responded_at = app_now()
            db.commit()
            db.refresh(booking)

        return jsonify({"ok": True, "booking": _booking_to_dict_for(booking, member)})
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>/invites", methods=["POST"])
def invite_to_booking(booking_id: int):
    db, member, error = require_auth()
    if error:
        return error
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.member_id != member.id and not member.is_admin:
            return err("Only the booking owner can invite people", 403)
        if booking.status in ("cancelled", "completed"):
            return err(f"Cannot invite to a {booking.status} session")

        body = request.get_json(silent=True) or {}
        member_ids = body.get("member_ids") or []
        if not isinstance(member_ids, list) or not all(isinstance(m, int) for m in member_ids):
            return err("member_ids must be a list of member ids")

        _create_invites(db, booking, member, member_ids)
        return jsonify({"ok": True, "booking": _booking_to_dict_for(booking, member)})
    finally:
        db.close()


def _apply_member_rsvp(db, booking: Booking, member: FamilyMember, response: str) -> None:
    """Record a member's RSVP: upsert the invite row, sync participant status,
    and notify the host. Caller has already validated booking/response."""
    # Any approved member may RSVP — an uninvited RSVP (e.g. via a share
    # link) creates its own invite row so the host sees it.
    invite = db.query(BookingInvite).filter_by(booking_id=booking.id, member_id=member.id).first()
    if not invite:
        invite = BookingInvite(
            booking_id=booking.id,
            member_id=member.id,
            invited_by=None,
            invited_at=app_now(),
        )
        db.add(invite)
    prev_status = invite.status
    invite.status = response
    invite.responded_at = app_now()
    db.commit()

    if response == "yes":
        _add_participant(db, booking, member.id)
    elif prev_status == "yes" and booking.status == "scheduled":
        # Withdrawing a yes revokes control — but only before the session
        # starts, so nobody loses extend/off rights mid-session.
        participant = (
            db.query(BookingParticipant)
            .filter_by(booking_id=booking.id, member_id=member.id)
            .first()
        )
        if participant:
            db.delete(participant)
            db.commit()
    db.refresh(booking)

    label = {"yes": "Yes 🎉", "no": "No", "maybe": "Maybe"}[response]
    _notify_member(booking.member_id, {
        "title": "📅 RSVP",
        "body": f"{member.name} responded {label} — {_booking_when_text(booking)}",
        "tag": f"rsvp-{booking.id}-{member.id}",
        "thread_id": "booking-rsvp",
        "url": f"/?booking={booking.id}",
        "bookingId": booking.id,
    }, pref_key="rsvp")


# ---------------------------------------------------------------------------
# Public share links & guest RSVP
# ---------------------------------------------------------------------------

GUEST_RSVP_MAX_PER_WINDOW = 20      # per-IP posts per window
GUEST_RSVP_WINDOW_SECONDS = 900
GUEST_RSVP_MAX_GUESTS = 20          # per booking
GUEST_NAME_MAX_LEN = 40

_guest_rsvp_lock = threading.Lock()
_guest_rsvp_attempts: dict[str, list[float]] = collections.defaultdict(list)


def _guest_rsvp_rate_limited(ip: str) -> bool:
    now = _time.time()
    with _guest_rsvp_lock:
        attempts = _guest_rsvp_attempts[ip]
        _guest_rsvp_attempts[ip] = [t for t in attempts if now - t < GUEST_RSVP_WINDOW_SECONDS]
        if len(_guest_rsvp_attempts[ip]) >= GUEST_RSVP_MAX_PER_WINDOW:
            return True
        _guest_rsvp_attempts[ip].append(now)
        return False


def _booking_is_past(booking: Booking) -> bool:
    now = app_now()
    end_date = booking.date
    if booking.end_time < booking.start_time:  # midnight-spanning
        end_date = booking.date + timedelta(days=1)
    return datetime.combine(end_date, booking.end_time) < now


@app.route("/api/bookings/<int:booking_id>/share", methods=["POST"])
def share_booking(booking_id: int):
    db, member, error = require_auth()
    if error:
        return error
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if not _can_control_booking(db, booking, member):
            return err("Join this session before sharing it", 403)
        if booking.status in ("cancelled", "completed"):
            return err(f"Cannot share a {booking.status} session")

        if not booking.share_token:
            booking.share_token = secrets.token_urlsafe(16)
            db.commit()
        return jsonify({
            "ok": True,
            "token": booking.share_token,
            "url": f"{APP_URL}/rsvp/{booking.share_token}",
        })
    finally:
        db.close()


def _public_rsvp_payload(db, booking: Booking, viewer: FamilyMember | None) -> dict:
    """Booking details safe to show anyone holding the share link."""
    guests = db.query(GuestRsvp).filter_by(booking_id=booking.id).order_by(GuestRsvp.created_at).all()
    participants = [p.member.name for p in (booking.participants or []) if p.member is not None]
    going_count = 1 + len(participants) + sum(1 for g in guests if g.status == "yes")
    data = {
        "booking_id": booking.id,
        "date": booking.date.isoformat(),
        "start_time": booking.start_time.strftime("%H:%M"),
        "end_time": booking.end_time.strftime("%H:%M"),
        "status": booking.status,
        "is_past": _booking_is_past(booking),
        "host_name": booking.member.name if booking.member else None,
        "host_color": booking.member.color if booking.member else None,
        "participants": participants,
        "guests": [g.to_dict() for g in guests],
        "going_count": going_count,
        "viewer": None,
    }
    if viewer is not None:
        data["viewer"] = {
            "id": viewer.id,
            "name": viewer.name,
            "is_host": viewer.id == booking.member_id,
            "my_invite_status": next(
                (i.status for i in (booking.invites or []) if i.member_id == viewer.id), None
            ),
        }
    return data


@app.route("/api/rsvp/<token>")
def rsvp_public_view(token: str):
    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(share_token=token).first()
        if not booking:
            return err("Invite not found", 404)
        # Past/cancelled sessions still resolve so the page can say so
        return jsonify(_public_rsvp_payload(db, booking, current_member(db)))
    finally:
        db.close()


@app.route("/api/rsvp/<token>", methods=["POST"])
def rsvp_public_respond(token: str):
    # CSRF-exempt: the unguessable share token in the path is the authorisation.
    if _guest_rsvp_rate_limited(_get_client_ip()):
        return err("Too many RSVPs from this address — try again later", 429)

    db = SessionLocal()
    try:
        booking = db.query(Booking).filter_by(share_token=token).first()
        if not booking:
            return err("Invite not found", 404)
        if booking.status == "cancelled":
            return err("This session was cancelled")
        if booking.status == "completed" or _booking_is_past(booking):
            return err("This session has already happened")

        body = request.get_json(silent=True) or {}
        response = (body.get("response") or "").strip().lower()
        if response not in ("yes", "no", "maybe"):
            return err("response must be yes, no or maybe")

        # Signed-in members get the member flow — no ghost guest rows.
        member = current_member(db)
        if member is not None:
            if booking.member_id == member.id:
                return err("You're the host of this session")
            _apply_member_rsvp(db, booking, member, response)
            return jsonify({"ok": True, **_public_rsvp_payload(db, booking, member)})

        name = (body.get("name") or "").strip()[:GUEST_NAME_MAX_LEN]
        if not name:
            return err("Name is required")

        guest = None
        guest_secret = (body.get("guest_secret") or "").strip()
        if guest_secret:
            guest = db.query(GuestRsvp).filter_by(booking_id=booking.id, guest_secret=guest_secret).first()
        if guest is None:
            guest_count = db.query(GuestRsvp).filter_by(booking_id=booking.id).count()
            if guest_count >= GUEST_RSVP_MAX_GUESTS:
                return err("This session has too many guest RSVPs already")
            guest = GuestRsvp(
                booking_id=booking.id,
                name=name,
                status=response,
                guest_secret=secrets.token_urlsafe(16),
            )
            db.add(guest)
        else:
            guest.name = name
            guest.status = response
            guest.updated_at = app_now()
        db.commit()

        label = {"yes": "Yes 🎉", "no": "No", "maybe": "Maybe"}[response]
        _notify_member(booking.member_id, {
            "title": "🎟 Guest RSVP",
            "body": f"{name} responded {label} — {_booking_when_text(booking)}",
            "tag": f"guest-rsvp-{booking.id}-{guest.id}",
            "thread_id": "booking-rsvp",
            "url": f"/?booking={booking.id}",
            "bookingId": booking.id,
        }, pref_key="rsvp")

        return jsonify({
            "ok": True,
            "guest_secret": guest.guest_secret,
            "status": guest.status,
            **_public_rsvp_payload(db, booking, None),
        })
    finally:
        db.close()


@app.route("/api/bookings/<int:booking_id>/rsvp", methods=["POST"])
def rsvp_booking(booking_id: int):
    db, member, error = require_auth()
    if error:
        return error
    try:
        booking = db.query(Booking).filter_by(id=booking_id).first()
        if not booking:
            return err("Booking not found", 404)
        if booking.status in ("cancelled", "completed"):
            return err(f"Cannot RSVP to a {booking.status} session")
        if booking.member_id == member.id:
            return err("You're the host of this session")

        body = request.get_json(silent=True) or {}
        response = (body.get("response") or "").strip().lower()
        if response not in ("yes", "no", "maybe"):
            return err("response must be yes, no or maybe")

        _apply_member_rsvp(db, booking, member, response)
        return jsonify({"ok": True, "booking": _booking_to_dict_for(booking, member)})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Booking history & member stats
# ---------------------------------------------------------------------------

@app.route("/api/bookings/history")
def booking_history():
    db, member, error = require_auth()
    if error:
        return error
    member_id_param = request.args.get("member_id")
    include_joinable = request.args.get("include_joinable") == "1"
    try:
        q = db.query(Booking).filter(Booking.status != "cancelled")
        if member_id_param:
            req_id = int(member_id_param)
            if req_id != member.id and not member.is_admin:
                return err("Cannot view another member's history", 403)
        else:
            req_id = member.id
        q = q.outerjoin(
            BookingParticipant,
            BookingParticipant.booking_id == Booking.id,
        )
        if not include_joinable or req_id != member.id:
            q = q.filter(
                (Booking.member_id == req_id) | (BookingParticipant.member_id == req_id)
            )
        q = q.distinct()
        bookings = q.order_by(Booking.date.desc(), Booking.start_time.desc()).limit(500).all()
        return jsonify([_booking_to_dict_for(b, member) for b in bookings])
    finally:
        db.close()


@app.route("/api/members/<int:member_id>/stats")
def member_stats(member_id: int):
    db, member, error = require_auth()
    if error:
        return error
    if member.id != member_id and not member.is_admin:
        return err("Cannot view another member's stats", 403)
    try:
        completed = db.query(Booking).filter(
            Booking.member_id == member_id,
            Booking.status == "completed",
        ).all()
        total_sessions = len(completed)
        total_mins = sum(b.on_time for b in completed)
        temps = [b.target_temp for b in completed if b.target_temp]
        fav_temp_c = max(set(temps), key=temps.count) if temps else None
        return jsonify({
            "total_sessions": total_sessions,
            "total_hours": round(total_mins / 60, 1),
            "favourite_temp_c": fav_temp_c,
            "favourite_temp_f": c_to_f(fav_temp_c) if fav_temp_c else None,
        })
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

        # The browser sends every field as a string (from <input> values).
        # Coerce each value to the correct Python type before setattr so
        # SQLAlchemy 2.0's strict type processing doesn't blow up.
        col_types = {
            c.key: c.columns[0].type
            for c in sa_inspect(model).mapper.column_attrs
        }
        for field, value in body.items():
            if field not in editable:
                continue
            col_type = col_types.get(field)
            if value == "" or value is None:
                coerced = None
            elif isinstance(col_type, _SAInteger) and isinstance(value, str):
                coerced = int(value)
            elif isinstance(col_type, _SADate) and isinstance(value, str):
                coerced = date.fromisoformat(value)
            elif isinstance(col_type, _SATime) and isinstance(value, str):
                parts = value.split(":")
                coerced = time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
            elif isinstance(col_type, _SABoolean) and isinstance(value, str):
                coerced = value.lower() in ("true", "1", "yes")
            else:
                coerced = value
            setattr(row, field, coerced)

        db.commit()
        db.refresh(row)
        return jsonify(row.to_dict())
    except Exception as exc:
        logger.error("db_update failed for %s #%d: %s", table, row_id, exc)
        return err(str(exc), 500)
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


@app.route("/rsvp/<token>")
def serve_rsvp_page(token):
    # Explicit rule: Flask's built-in static route (/<path:filename>, from
    # static_url_path="") outranks the catch-all for unknown paths and 404s,
    # so the public RSVP page needs its own non-path-converter route.
    return send_from_directory(app.static_folder, "index.html")


@app.route("/open")
def serve_open_page():
    # The iOS Live Activity's tap target is sweatbox://open, which app builds
    # map to /open — same static-route-outranks-catch-all rule as /rsvp above,
    # so without this the tap lands on Flask's bare 404 page.
    return send_from_directory(app.static_folder, "index.html")


@app.route("/.well-known/apple-app-site-association")
def apple_app_site_association():
    """Universal Links manifest — lets share links (/rsvp/*) open the iOS app.

    Apple's CDN fetches this at app install; it must be HTTPS with no
    redirects and Content-Type application/json (jsonify handles that).
    Only /rsvp/* is claimed so normal web links stay in the browser.
    """
    if not APPLE_TEAM_ID:
        return err("Universal Links not configured", 404)
    return jsonify({
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appID": f"{APPLE_TEAM_ID}.{APNS_BUNDLE_ID}",
                    "paths": ["/rsvp/*"],
                }
            ],
        }
    })


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
