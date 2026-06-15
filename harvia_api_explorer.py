#!/usr/bin/env python3
"""
Harvia MyHarvia Cloud API Explorer
===================================
Standalone script to query the Harvia cloud API and dump device state and
telemetry, and to interactively send state-change commands.

Use this to figure out what your Xenio panel actually honors via the cloud API
(e.g. whether you can get a session longer than 60 minutes).

Based on the reverse-engineered API from:
https://github.com/RubenHarms/ha-harvia-xenio-wifi

Requirements:
    pip install boto3 pycognito requests

Usage:
    python harvia_api_explorer.py

You'll be prompted for your MyHarvia app username (email) and password.
"""

import json
import sys
import getpass
import time
import requests
from pycognito import Cognito

# Harvia cloud config (from the HA integration)
REGION = "eu-west-1"
BASE_URL = "https://prod.myharvia-cloud.net"
ENDPOINTS_TO_FETCH = ["users", "device", "events", "data"]


def print_section(title: str):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def print_json(data, indent=2):
    print(json.dumps(data, indent=indent, default=str))


def fetch_endpoints() -> dict:
    """Fetch all API endpoint URLs from the Harvia cloud."""
    endpoints = {}
    for ep in ENDPOINTS_TO_FETCH:
        url = f"{BASE_URL}/{ep}/endpoint"
        resp = requests.get(url)
        resp.raise_for_status()
        endpoints[ep] = resp.json()
    return endpoints


def authenticate(endpoints: dict, username: str, password: str) -> tuple:
    """Authenticate via AWS Cognito and return (cognito_client, token_data)."""
    user_pool_id = endpoints["users"]["userPoolId"]
    client_id = endpoints["users"]["clientId"]

    u = Cognito(
        user_pool_id,
        client_id,
        username=username,
        user_pool_region=REGION,
    )
    u.authenticate(password=password)

    token_data = {
        "access_token": u.access_token,
        "refresh_token": u.refresh_token,
        "id_token": u.id_token,
    }
    return u, token_data


def api_query(endpoint_url: str, id_token: str, query: dict) -> dict:
    """Make a GraphQL query to a Harvia API endpoint."""
    headers = {"authorization": id_token}
    resp = requests.post(endpoint_url, json=query, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_user_details(endpoints: dict, id_token: str) -> dict:
    query = {
        "operationName": "Query",
        "variables": {},
        "query": (
            "query Query {\n"
            "  getCurrentUserDetails {\n"
            "    email\n"
            "    organizationId\n"
            "    admin\n"
            "    given_name\n"
            "    family_name\n"
            "    superAdmin\n"
            "    rdUser\n"
            "    appSettings\n"
            "    __typename\n"
            "  }\n"
            "}\n"
        ),
    }
    data = api_query(endpoints["users"]["endpoint"], id_token, query)
    return data.get("data", {}).get("getCurrentUserDetails", {})


def get_device_tree(endpoints: dict, id_token: str) -> list:
    query = {
        "operationName": "Query",
        "variables": {},
        "query": "query Query {\n  getDeviceTree\n}\n",
    }
    data = api_query(endpoints["device"]["endpoint"], id_token, query)
    tree_raw = data.get("data", {}).get("getDeviceTree", "[]")
    return json.loads(tree_raw) if isinstance(tree_raw, str) else tree_raw


def get_device_state(endpoints: dict, id_token: str, device_id: str) -> dict:
    """Get the full device state (reported + desired)."""
    query = {
        "operationName": "Query",
        "variables": {"deviceId": device_id},
        "query": (
            "query Query($deviceId: ID!) {\n"
            "  getDeviceState(deviceId: $deviceId) {\n"
            "    desired\n"
            "    reported\n"
            "    timestamp\n"
            "    __typename\n"
            "  }\n"
            "}\n"
        ),
    }
    return api_query(endpoints["device"]["endpoint"], id_token, query)


def get_latest_data(endpoints: dict, id_token: str, device_id: str) -> dict:
    """Get the latest telemetry data from the device."""
    query = {
        "operationName": "Query",
        "variables": {"deviceId": device_id},
        "query": (
            "query Query($deviceId: String!) {\n"
            "  getLatestData(deviceId: $deviceId) {\n"
            "    deviceId\n"
            "    timestamp\n"
            "    sessionId\n"
            "    type\n"
            "    data\n"
            "    __typename\n"
            "  }\n"
            "}\n"
        ),
    }
    return api_query(endpoints["data"]["endpoint"], id_token, query)


def try_state_change(endpoints: dict, id_token: str, device_id: str, payload: dict) -> dict:
    """Send a state change mutation.

    WARNING: This actually sends a command to your sauna!
    """
    query = {
        "operationName": "Mutation",
        "variables": {
            "deviceId": device_id,
            "state": json.dumps(payload),
            "getFullState": True,
        },
        "query": (
            "mutation Mutation($deviceId: ID!, $state: AWSJSON!, $getFullState: Boolean) {\n"
            "  requestStateChange(deviceId: $deviceId, state: $state, getFullState: $getFullState)\n"
            "}\n"
        ),
    }
    return api_query(endpoints["device"]["endpoint"], id_token, query)


# ---------------------------------------------------------------------------
# Interactive write console helpers
# ---------------------------------------------------------------------------

# Shadow timing fields that can get stuck in `desired`. We never null
# active/targetTemp/maxTemp — only the duration-related ones.
CLEARABLE_FIELDS = ["onTime", "maxOnTime", "maxTime", "remainingTime", "heatUpTime"]


def _read_state(endpoints: dict, id_token: str, device_id: str) -> tuple:
    """Return (reported, desired) dicts from the device shadow."""
    gds = (get_device_state(endpoints, id_token, device_id)
           .get("data", {}).get("getDeviceState", {}) or {})
    rep_raw = gds.get("reported", "{}")
    reported = json.loads(rep_raw) if isinstance(rep_raw, str) else (rep_raw or {})
    des_raw = gds.get("desired")
    desired = {}
    if des_raw:
        desired = json.loads(des_raw) if isinstance(des_raw, str) else des_raw
    return reported, desired


def _live_remaining(endpoints: dict, id_token: str, device_id: str):
    """The live remainingTime comes from the TELEMETRY stream (getLatestData),
    not the device shadow — so it must be read separately from reported/desired.
    This is the ground truth for what the device is actually doing."""
    try:
        resp = get_latest_data(endpoints, id_token, device_id)
        raw = (resp.get("data", {}).get("getLatestData", {}) or {}).get("data", "{}")
        tele = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return tele.get("remainingTime")
    except Exception as e:
        print(f"    (telemetry read failed: {e})")
        return None


def print_state_block(endpoints: dict, id_token: str, device_id: str, requested=None):
    """Print device state, judging reality by LIVE telemetry — not the shadow.

    On this unit the shadow reported.maxOnTime/onTime fields are stale and do
    NOT reflect the real timer, so they're shown for reference only. The live
    telemetry remainingTime is the ground truth. `requested` (minutes) optionally
    prints a pass/fail verdict.
    """
    reported, desired = _read_state(endpoints, id_token, device_id)

    print(f"  [{time.strftime('%H:%M:%S')}]")
    print("  shadow fields (reference only — unreliable for timing):")
    for k in ("active", "targetTemp", "maxTemp", "onTime", "maxOnTime"):
        rep = reported.get(k, "—")
        des = desired.get(k)
        if des is not None and des != reported.get(k):
            print(f"    {k:12} reported={rep}  desired={des}")
        else:
            print(f"    {k:12} reported={rep}")

    live = _live_remaining(endpoints, id_token, device_id)
    print(f"\n  >>> LIVE remainingTime = {live} min   (the real countdown) <<<")

    if requested is not None and isinstance(live, (int, float)):
        # Right after a fresh ON the device sets remainingTime to the session
        # length; allow slack for telemetry lag / a minute elapsing.
        if abs(live - requested) <= 3:
            print(f"  ✅ matches your requested {requested} min — the device took it.")
        elif live > requested + 3:
            print(f"  ⚠️ live={live} is HIGHER than requested {requested} — a stale "
                  f"desired value is likely overriding. Try Clear, then ON again.")
        else:
            print(f"  ⚠️ live={live} is LOWER than requested {requested} — device "
                  f"capped it, or telemetry is still catching up. Re-check with "
                  f"option 1 in ~1 min.")


def show_state(endpoints: dict, id_token: str, device_id: str):
    print("\n  --- device state ---")
    print_state_block(endpoints, id_token, device_id)


def dump_all(endpoints: dict, id_token: str, device_id: str):
    """Dump EVERYTHING the API exposes — every field from every endpoint —
    so we don't stay blind to a field we haven't been looking at."""
    print_section(f"FULL DUMP — {device_id}")

    # 1. Device shadow: full reported + desired (every key)
    try:
        reported, desired = _read_state(endpoints, id_token, device_id)
        print("  [getDeviceState] reported — every field:")
        for k in sorted(reported.keys()):
            print(f"    {k:22} = {reported[k]}")
        print("\n  [getDeviceState] desired — every field:")
        if desired:
            for k in sorted(desired.keys()):
                print(f"    {k:22} = {desired[k]}")
        else:
            print("    (empty)")
    except Exception as e:
        print(f"  getDeviceState failed: {e}")

    # 2. Telemetry: full getLatestData payload (every key)
    try:
        resp = get_latest_data(endpoints, id_token, device_id)
        latest = (resp.get("data", {}).get("getLatestData", {}) or {})
        meta = {k: latest.get(k) for k in ("timestamp", "sessionId", "type")}
        raw = latest.get("data", "{}")
        tele = json.loads(raw) if isinstance(raw, str) else (raw or {})
        print(f"\n  [getLatestData] meta: {meta}")
        print("  [getLatestData] telemetry — every field:")
        for k in sorted(tele.keys()):
            print(f"    {k:22} = {tele[k]}")
    except Exception as e:
        print(f"  getLatestData failed: {e}")

    # 3. Raw responses too, in case parsing hid something
    print("\n  --- RAW getDeviceState response ---")
    try:
        print_json(get_device_state(endpoints, id_token, device_id))
    except Exception as e:
        print(f"  failed: {e}")
    print("\n  --- RAW getLatestData response ---")
    try:
        print_json(get_latest_data(endpoints, id_token, device_id))
    except Exception as e:
        print(f"  failed: {e}")


def write_console(endpoints: dict, id_token: str, devices: list):
    """Interactive menu for sending state changes and watching what the device
    actually does (via live telemetry).

    WARNING: every action here sends REAL commands to your sauna."""
    print_section("Interactive Write Console")
    print("  WARNING: these send REAL commands to your sauna!")

    device_id = devices[0] if len(devices) == 1 else input(f"  Device ID {devices}: ").strip()

    MENU = [
        ("Show state (live countdown)", "state"),
        ("DUMP ALL fields (every endpoint)", "dump"),
        ("Turn sauna ON", "on"),
        ("Turn ON + remainingTime (edge test)", "on_remaining"),
        ("Set remainingTime (test longer session)", "remaining"),
        ("Raw write (one field=value, isolates variables)", "raw"),
        ("Turn sauna OFF", "off"),
        ("Clear stale desired timing fields", "clear"),
        ("Quit", "quit"),
    ]

    def prompt_int(label: str, default):
        raw = input(f"    {label} [{default}]: ").strip()
        return int(raw) if raw else default

    def send(payload: dict, requested=None):
        print(f"\n  SENT:     {payload}")
        confirm = input("  Confirm? (y/N): ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return
        try_state_change(endpoints, id_token, device_id, payload)
        time.sleep(2)
        print_state_block(endpoints, id_token, device_id, requested=requested)

    while True:
        print("\n  Choose an action:")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"    {i}. {label}")
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
            print("  Enter a number from the menu.")
            continue
        action = MENU[int(choice) - 1][1]

        if action == "quit":
            break
        elif action == "state":
            show_state(endpoints, id_token, device_id)
        elif action == "dump":
            dump_all(endpoints, id_token, device_id)
        elif action == "on":
            temp = prompt_int("targetTemp (°C)", 80)
            on_time = prompt_int("onTime (min)", 120)
            max_raw = input("    maxOnTime (min) [blank=omit]: ").strip()
            payload = {"active": 1, "targetTemp": temp, "onTime": on_time}
            if max_raw:
                payload["maxOnTime"] = int(max_raw)
            send(payload, requested=on_time)
        elif action == "on_remaining":
            # Send remainingTime IN THE SAME payload as the active 0->1 edge.
            # Device must be OFF first for this to be a real activation edge.
            reported, _ = _read_state(endpoints, id_token, device_id)
            if reported.get("active") == 1:
                print("  Sauna is already ON — turn it OFF first so this is a real 0->1 edge.")
                continue
            temp = prompt_int("targetTemp (°C)", 98)
            rt = prompt_int("remainingTime (min)", 120)
            include_on = input("    also include onTime same value? (y/N): ").strip().lower() == "y"
            payload = {"active": 1, "targetTemp": temp, "remainingTime": rt}
            if include_on:
                payload["onTime"] = rt
            send(payload, requested=rt)
        elif action == "remaining":
            reported, _ = _read_state(endpoints, id_token, device_id)
            if reported.get("active") != 1:
                print("  Note: sauna is not active. Turn it ON first, then set remainingTime.")
            live = _live_remaining(endpoints, id_token, device_id)
            print(f"  Current live remainingTime = {live} min")
            new_remaining = prompt_int("new remainingTime (min)", 120)
            send({"remainingTime": new_remaining}, requested=new_remaining)
        elif action == "raw":
            # Send exactly ONE field so trials isolate a single variable.
            field = input("    field name (e.g. maxOnTime, onTime, active): ").strip()
            if not field:
                print("  No field given.")
                continue
            raw_val = input(f"    value for {field} (number, or 'null' to clear): ").strip()
            if raw_val.lower() in ("null", "none", ""):
                value = None
            else:
                try:
                    value = int(raw_val)
                except ValueError:
                    value = raw_val
            req = value if field in ("onTime", "maxOnTime", "remainingTime") and isinstance(value, int) else None
            send({field: value}, requested=req)
        elif action == "off":
            send({"active": 0})
        elif action == "clear":
            # A leftover desired value (e.g. maxOnTime the device never applied)
            # is cleared by writing null for that key — AWS IoT shadow deletes
            # desired fields set to null.
            _, desired = _read_state(endpoints, id_token, device_id)
            stale = {k: desired[k] for k in CLEARABLE_FIELDS if k in desired}
            if not stale:
                print("  No timing fields stuck in desired — nothing to clear.")
                continue
            print(f"  Stale desired timing fields: {stale}")
            send({k: None for k in stale})


def main():
    print_section("Harvia MyHarvia Cloud API Explorer")

    # --- Credentials ---
    username = input("MyHarvia email: ").strip()
    password = getpass.getpass("MyHarvia password: ")

    # --- Fetch endpoints ---
    print("\n[1/5] Fetching API endpoints...")
    endpoints = fetch_endpoints()
    print("  Endpoints discovered:")
    for name, ep in endpoints.items():
        url = ep.get("endpoint", "N/A")
        print(f"    {name}: {url}")

    # --- Authenticate ---
    print("\n[2/5] Authenticating with AWS Cognito...")
    try:
        cognito, tokens = authenticate(endpoints, username, password)
        print("  Authentication successful!")
    except Exception as e:
        print(f"  Authentication FAILED: {e}")
        sys.exit(1)

    id_token = tokens["id_token"]

    # --- User details ---
    print("\n[3/5] Fetching user details...")
    user = get_user_details(endpoints, id_token)
    print_section("User Details")
    print_json(user)

    # --- Device tree ---
    print("\n[4/5] Fetching device tree...")
    tree = get_device_tree(endpoints, id_token)

    # --- Extract device IDs ---
    devices = []
    if tree and isinstance(tree, list) and len(tree) > 0:
        for top_level in tree:
            if "c" in top_level:
                for child in top_level["c"]:
                    if "i" in child and "name" in child["i"]:
                        devices.append(child["i"]["name"])

    if not devices:
        print("\n  No devices found! Check your MyHarvia account.")
        sys.exit(1)

    print(f"\n  Found {len(devices)} device(s): {devices}")

    # --- Show current state for each device ---
    for device_id in devices:
        print_section(f"Device: {device_id}")
        print_state_block(endpoints, id_token, device_id)

    # --- Interactive write console ---
    test = input("\n  Open the interactive write console? (y/N): ").strip().lower()
    if test == "y":
        write_console(endpoints, id_token, devices)

    print_section("Done!")


if __name__ == "__main__":
    main()
