"""
Harvia MyHarvia cloud API client.
Handles Cognito auth (SRP), token refresh, and all GraphQL calls.
"""
import json
import logging
import threading
import time

import requests
from pycognito import Cognito

logger = logging.getLogger(__name__)

REGION = "eu-west-1"
BASE_URL = "https://prod.myharvia-cloud.net"
TOKEN_REFRESH_INTERVAL = 50 * 60  # refresh every 50 min (tokens expire at ~60 min)


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
        logger.info("Harvia authenticated successfully")

    def _ensure_token(self):
        """Refresh token if it's close to expiry."""
        with self._lock:
            if time.monotonic() - self._last_refresh > TOKEN_REFRESH_INTERVAL:
                try:
                    self._cognito.check_token(renew=True)
                    self._id_token = self._cognito.id_token
                    self._last_refresh = time.monotonic()
                    logger.info("Harvia token refreshed")
                except Exception:
                    logger.warning("Token refresh failed, re-authenticating")
                    self._authenticate()

    def _headers(self) -> dict:
        self._ensure_token()
        return {"authorization": self._id_token}

    def _graphql(self, service: str, query: dict) -> dict:
        endpoint = self._endpoints[service]["endpoint"]
        resp = requests.post(endpoint, json=query, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data

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
