"""
Harvia MyHarvia cloud API client.
Handles Cognito auth (SRP), token refresh, and all GraphQL calls.
"""
import collections
import json
import logging
import threading
import time

import requests
from pycognito import Cognito

logger = logging.getLogger(__name__)

REGION = "eu-west-1"
BASE_URL = "https://prod.myharvia-cloud.net"
TOKEN_REFRESH_INTERVAL = 30 * 60  # refresh every 30 min — tokens expire at ~60 min,
                                  # but pycognito check_token() only renews if already
                                  # expired so we call renew_access_token() directly
                                  # and keep a comfortable 30-min cadence

# Maximum recent-call entries kept in memory for the stats endpoint
_CALL_LOG_MAX = 100


class HarviaClient:
    def __init__(self, username: str, password: str, device_id: str):
        self.username = username
        self.password = password
        self.device_id = device_id

        self._endpoints: dict = {}
        self._cognito: Cognito | None = None
        self._id_token: str | None = None
        self._lock = threading.Lock()
        self._last_refresh: float = 0.0

        # ── API call tracking ──────────────────────────────────────────
        self._stats_lock = threading.Lock()
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._auth_count: int = 0        # full re-auths
        self._refresh_count: int = 0     # token refreshes
        self._last_auth_ts: float | None = None
        self._last_error: str | None = None
        self._call_log: collections.deque = collections.deque(maxlen=_CALL_LOG_MAX)
        self._server_start: float = time.time()

    # ------------------------------------------------------------------
    # Initialisation / auth
    # ------------------------------------------------------------------

    def init(self):
        """Fetch endpoints and authenticate.  Call once at startup."""
        self._fetch_endpoints()
        self._authenticate()

    def _fetch_endpoints(self):
        for service in ("users", "device", "data"):
            resp = requests.get(f"{BASE_URL}/{service}/endpoint", timeout=10)
            resp.raise_for_status()
            self._endpoints[service] = resp.json()
        logger.info("Harvia endpoints fetched: %s", list(self._endpoints.keys()))

    def _authenticate(self):
        ep = self._endpoints["users"]
        self._cognito = Cognito(
            ep["userPoolId"],
            ep["clientId"],
            username=self.username,
            user_pool_region=REGION,
        )
        self._cognito.authenticate(password=self.password)
        self._id_token = self._cognito.id_token
        self._last_refresh = time.monotonic()
        with self._stats_lock:
            self._auth_count += 1
            self._last_auth_ts = time.time()
        logger.info("Harvia authenticated successfully (auth #%d)", self._auth_count)

    def _ensure_token(self):
        """Refresh token if it's due.  Called lazily before every API request."""
        with self._lock:
            if time.monotonic() - self._last_refresh > TOKEN_REFRESH_INTERVAL:
                self._do_refresh()

    def _do_refresh(self):
        """Force-refresh the Cognito token using the refresh token.
        Must be called with self._lock held (or before the lock is needed)."""
        try:
            # renew_access_token() uses the long-lived refresh token to obtain
            # a new ID token unconditionally — unlike check_token(renew=True)
            # which only renews if the token is already expired.
            self._cognito.renew_access_token()
            self._id_token = self._cognito.id_token
            self._last_refresh = time.monotonic()
            with self._stats_lock:
                self._refresh_count += 1
            logger.info("Harvia token refreshed (refresh #%d)", self._refresh_count)
        except Exception as exc:
            logger.warning("Token refresh failed (%s), falling back to full re-auth", exc)
            self._authenticate()

    def proactive_refresh(self):
        """Called by the background scheduler to keep the token warm.
        Ensures a fresh token is always ready even during idle periods."""
        with self._lock:
            self._do_refresh()

    def _headers(self) -> dict:
        self._ensure_token()
        return {"authorization": self._id_token}

    def _graphql(self, service: str, query: dict) -> dict:
        operation = query.get("operationName", service)
        t0 = time.time()
        endpoint = self._endpoints[service]["endpoint"]
        try:
            resp = requests.post(endpoint, json=query, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            duration_ms = int((time.time() - t0) * 1000)
            with self._stats_lock:
                self._total_calls += 1
                self._call_log.append({
                    "ts": t0, "op": operation, "service": service,
                    "ok": True, "ms": duration_ms,
                })
            return data
        except Exception as exc:
            duration_ms = int((time.time() - t0) * 1000)
            err_str = f"{type(exc).__name__}: {exc}"
            with self._stats_lock:
                self._total_calls += 1
                self._total_errors += 1
                self._last_error = err_str
                self._call_log.append({
                    "ts": t0, "op": operation, "service": service,
                    "ok": False, "ms": duration_ms, "err": err_str,
                })
            logger.error("Harvia API call failed [%s/%s] %dms — %s", service, operation, duration_ms, err_str)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_device_state(self) -> dict:
        """Return the reported device state as a dict."""
        query = {
            "operationName": "Query",
            "variables": {"deviceId": self.device_id},
            "query": (
                "query Query($deviceId: ID!) {\n"
                "  getDeviceState(deviceId: $deviceId) {\n"
                "    desired\n    reported\n    timestamp\n    __typename\n"
                "  }\n}\n"
            ),
        }
        data = self._graphql("device", query)
        state = data["data"]["getDeviceState"]
        reported_raw = state.get("reported", "{}")
        reported = json.loads(reported_raw) if isinstance(reported_raw, str) else reported_raw
        desired_raw = state.get("desired", "{}")
        desired = json.loads(desired_raw) if isinstance(desired_raw, str) else desired_raw
        return {
            "reported": reported,
            "desired": desired,
            "timestamp": state.get("timestamp"),
        }

    def get_latest_telemetry(self) -> dict:
        """Return live telemetry data as a dict."""
        query = {
            "operationName": "Query",
            "variables": {"deviceId": self.device_id},
            "query": (
                "query Query($deviceId: String!) {\n"
                "  getLatestData(deviceId: $deviceId) {\n"
                "    deviceId\n    timestamp\n    sessionId\n    type\n    data\n    __typename\n"
                "  }\n}\n"
            ),
        }
        data = self._graphql("data", query)
        latest = data["data"]["getLatestData"]
        if not latest:
            return {}
        raw = latest.get("data", "{}")
        telemetry = json.loads(raw) if isinstance(raw, str) else raw
        telemetry["_timestamp"] = latest.get("timestamp")
        return telemetry

    def set_state(self, payload: dict) -> dict:
        """Send a state-change mutation.  payload keys are writable field names."""
        query = {
            "operationName": "Mutation",
            "variables": {
                "deviceId": self.device_id,
                "state": json.dumps(payload),
                "getFullState": True,
            },
            "query": (
                "mutation Mutation($deviceId: ID!, $state: AWSJSON!, $getFullState: Boolean) {\n"
                "  requestStateChange(deviceId: $deviceId, state: $state, getFullState: $getFullState)\n"
                "}\n"
            ),
        }
        return self._graphql("device", query)

    def turn_on(self, target_temp_c: int, on_time_minutes: int) -> dict:
        return self.set_state({"active": 1, "targetTemp": target_temp_c, "onTime": on_time_minutes})

    def turn_off(self) -> dict:
        return self.set_state({"active": 0})

    def get_stats(self) -> dict:
        """Return API call counters and recent call log for the admin panel."""
        now = time.time()
        with self._stats_lock:
            # calls in the last 60 min
            cutoff = now - 3600
            calls_1h = sum(1 for e in self._call_log if e["ts"] >= cutoff)
            errors_1h = sum(1 for e in self._call_log if e["ts"] >= cutoff and not e["ok"])
            recent = list(self._call_log)[-20:]  # last 20 entries

        token_age_mins = int((time.monotonic() - self._last_refresh) / 60) if self._last_refresh else None
        next_refresh_mins = max(0, int((TOKEN_REFRESH_INTERVAL - (time.monotonic() - self._last_refresh)) / 60)) if self._last_refresh else None

        return {
            "uptime_mins": int((now - self._server_start) / 60),
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "calls_last_1h": calls_1h,
            "errors_last_1h": errors_1h,
            "auth_count": self._auth_count,
            "refresh_count": self._refresh_count,
            "last_auth_ts": self._last_auth_ts,
            "token_age_mins": token_age_mins,
            "next_refresh_mins": next_refresh_mins,
            "last_error": self._last_error,
            "recent_calls": [
                {"time": e["ts"], "op": e["op"], "service": e["service"],
                 "ok": e["ok"], "ms": e["ms"], **({"err": e["err"]} if not e["ok"] else {})}
                for e in recent
            ],
        }

    def get_full_status(self) -> dict:
        """Merge device state + telemetry into a single status dict for the frontend."""
        try:
            state = self.get_device_state()
            reported = state["reported"]
        except Exception as exc:
            logger.error("Failed to get device state: %s", exc)
            reported = {}

        try:
            telemetry = self.get_latest_telemetry()
        except Exception as exc:
            logger.error("Failed to get telemetry: %s", exc)
            telemetry = {}

        return {
            "online": reported.get("online", False),
            "active": reported.get("active", 0),
            "targetTemp": reported.get("targetTemp"),
            "onTime": reported.get("onTime"),
            "maxOnTime": reported.get("maxOnTime"),
            "maxTemp": reported.get("maxTemp"),
            "light": reported.get("light", 0),
            "fan": reported.get("fan", 0),
            "steamEn": reported.get("steamEn", 0),
            "targetRh": reported.get("targetRh"),
            "displayName": reported.get("displayName"),
            "statusCodes": reported.get("statusCodes"),
            "errorCodes": reported.get("errorCodes"),
            # live telemetry
            "temperature": telemetry.get("temperature"),
            "humidity": telemetry.get("humidity"),
            "heatOn": telemetry.get("heatOn"),
            "remainingTime": telemetry.get("remainingTime"),
            "doorSafetyState": telemetry.get("doorSafetyState"),
            "steamOn": telemetry.get("steamOn"),
            "wifiRSSI": telemetry.get("wifiRSSI"),
            "lightState": telemetry.get("lightState"),
            "fanState": telemetry.get("fanState"),
            "telemetryTimestamp": telemetry.get("_timestamp"),
        }
