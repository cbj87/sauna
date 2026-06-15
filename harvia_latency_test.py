#!/usr/bin/env python3
"""
Harvia commit-latency test harness
==================================
Automated, timestamped probing of how the Harvia cloud shadow commits writes,
so we can distinguish a *time-based* commit ("backend updates every X min") from
an *edge-gated* one ("only commits on an activation").

SAFETY: This harness NEVER turns the heater on (never writes active=1). It only
reads state/telemetry and writes config fields while the sauna is OFF. Heater
activation must be done by a human who is present.

Every observation is appended to a CSV with a precise timestamp so we can graph
the lag between a write and when `reported` reflects it.

Usage:
    .venv/bin/python3 harvia_latency_test.py poll       --minutes 15 --interval 15
    .venv/bin/python3 harvia_latency_test.py commit-test --value 120 --minutes 15 --interval 15

Credentials are read from .env.local (HARVIA_USERNAME / HARVIA_PASSWORD / HARVIA_DEVICE_ID).
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from pycognito import Cognito

REGION = "eu-west-1"
BASE_URL = "https://prod.myharvia-cloud.net"
LOG_DIR = "latency_logs"

# Fields we track on every sample.
SHADOW_FIELDS = ["active", "targetTemp", "maxTemp", "onTime", "maxOnTime"]


# ---------------------------------------------------------------------------
# Credentials / auth
# ---------------------------------------------------------------------------

def load_env(path=".env.local") -> dict:
    """Prefer real environment variables (e.g. injected by `railway run`),
    fall back to .env.local for local dev."""
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    # os.environ wins (so `railway run` provides the secrets without a file)
    for k in ("HARVIA_USERNAME", "HARVIA_PASSWORD", "HARVIA_DEVICE_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in ("HARVIA_USERNAME", "HARVIA_PASSWORD", "HARVIA_DEVICE_ID") if not env.get(k)]
    if missing:
        sys.exit(f"Missing creds: {missing}. Run via `railway run` or add them to {path}.")
    return env


def fetch_endpoints() -> dict:
    eps = {}
    for ep in ("users", "device", "data"):
        r = requests.get(f"{BASE_URL}/{ep}/endpoint", timeout=10)
        r.raise_for_status()
        eps[ep] = r.json()
    return eps


def authenticate(eps: dict, username: str, password: str) -> Cognito:
    u = Cognito(eps["users"]["userPoolId"], eps["users"]["clientId"],
                username=username, user_pool_region=REGION)
    u.authenticate(password=password)
    return u


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def gql(eps: dict, token: str, service: str, query: dict) -> dict:
    r = requests.post(eps[service]["endpoint"], json=query,
                      headers={"authorization": token}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data


def read_shadow(eps, token, device_id):
    q = {"operationName": "Query", "variables": {"deviceId": device_id},
         "query": "query Query($deviceId: ID!){getDeviceState(deviceId: $deviceId){desired reported timestamp __typename}}"}
    gds = gql(eps, token, "device", q)["data"]["getDeviceState"] or {}
    rep = gds.get("reported", "{}")
    des = gds.get("desired") or "{}"
    reported = json.loads(rep) if isinstance(rep, str) else (rep or {})
    desired = json.loads(des) if isinstance(des, str) else (des or {})
    return reported, desired, gds.get("timestamp")


def read_remaining(eps, token, device_id):
    q = {"operationName": "Query", "variables": {"deviceId": device_id},
         "query": "query Query($deviceId: String!){getLatestData(deviceId: $deviceId){timestamp data __typename}}"}
    latest = gql(eps, token, "data", q)["data"]["getLatestData"] or {}
    raw = latest.get("data", "{}")
    tele = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return tele.get("remainingTime"), latest.get("timestamp")


def write_field(eps, token, device_id, field, value):
    """Write a single shadow field. NEVER call with field='active', value=1."""
    if field == "active" and value == 1:
        raise RuntimeError("Refusing to activate the heater from the latency harness.")
    q = {"operationName": "Mutation",
         "variables": {"deviceId": device_id, "state": json.dumps({field: value}), "getFullState": True},
         "query": "mutation Mutation($deviceId: ID!, $state: AWSJSON!, $getFullState: Boolean){requestStateChange(deviceId: $deviceId, state: $state, getFullState: $getFullState)}"}
    return gql(eps, token, "device", q)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def open_log(tag: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    fname = os.path.join(LOG_DIR, f"{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    f = open(fname, "w", newline="")
    w = csv.writer(f)
    w.writerow(["wall_clock", "epoch", "event", "elapsed_s",
                "rep_active", "rep_onTime", "rep_maxOnTime",
                "des_onTime", "des_maxOnTime", "live_remaining", "shadow_ts"])
    return f, w, fname


def sample(eps, token, device_id, w, t0, event=""):
    now = time.time()
    try:
        reported, desired, sts = read_shadow(eps, token, device_id)
    except Exception as e:
        reported, desired, sts = {}, {}, f"ERR:{e}"
    try:
        live, _ = read_remaining(eps, token, device_id)
    except Exception as e:
        live = f"ERR:{e}"
    row = [datetime.now().strftime("%H:%M:%S"), round(now, 1), event, round(now - t0, 1),
           reported.get("active"), reported.get("onTime"), reported.get("maxOnTime"),
           desired.get("onTime"), desired.get("maxOnTime"), live, sts]
    w.writerow(row)
    print(f"  [{row[0]}] {event:14} rep_onTime={row[5]} des_onTime={row[7]} "
          f"rep_maxOnTime={row[6]} des_maxOnTime={row[8]} live={row[9]}")
    return reported, desired


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

def cmd_poll(eps, token, device_id, minutes, interval):
    f, w, fname = open_log("poll")
    print(f"Polling every {interval}s for {minutes} min → {fname}")
    t0 = time.time()
    end = t0 + minutes * 60
    try:
        while time.time() < end:
            sample(eps, token, device_id, w, t0, "poll")
            f.flush()
            time.sleep(interval)
    finally:
        f.close()
    print(f"Done. Log: {fname}")


def cmd_commit_test(eps, token, device_id, value, minutes, interval):
    """Write onTime=<value> while OFF, then poll — WITHOUT any activation —
    to see if/when reported.onTime commits on its own (time-based) or never
    (edge-gated)."""
    reported, _, _ = read_shadow(eps, token, device_id)
    if reported.get("active") == 1:
        sys.exit("Sauna is ON — run this only while OFF. Aborting (won't touch a running heater).")

    f, w, fname = open_log(f"commit_onTime_{value}")
    print(f"Commit test: write onTime={value} while OFF, poll {interval}s for {minutes} min → {fname}")
    t0 = time.time()

    sample(eps, token, device_id, w, t0, "baseline")
    write_field(eps, token, device_id, "onTime", value)
    sample(eps, token, device_id, w, t0, f"WROTE_onTime_{value}")
    f.flush()

    committed_at = None
    end = t0 + minutes * 60
    try:
        while time.time() < end:
            time.sleep(interval)
            rep, _ = sample(eps, token, device_id, w, t0, "poll")
            f.flush()
            if committed_at is None and rep.get("onTime") == value:
                committed_at = round(time.time() - t0, 1)
                sample(eps, token, device_id, w, t0, "COMMITTED")
                print(f"\n  *** reported.onTime committed to {value} after {committed_at}s "
                      f"with NO activation → TIME-BASED commit ***")
                f.flush()
                # keep polling a bit to confirm stability, then stop
                for _ in range(3):
                    time.sleep(interval)
                    sample(eps, token, device_id, w, t0, "post-commit")
                    f.flush()
                break
    finally:
        f.close()

    if committed_at is None:
        print(f"\n  *** reported.onTime NEVER committed to {value} in {minutes} min with no "
              f"activation → EDGE-GATED (not time-based) ***")
    print(f"Done. Log: {fname}")


def cmd_prime_watch(eps, token, device_id, value, interval):
    """Write onTime=<value> while OFF, poll until reported.onTime commits,
    then PROMPT the human to physically turn the sauna on, and log what the
    live session length becomes. The harness never activates the heater."""
    reported, _, _ = read_shadow(eps, token, device_id)
    if reported.get("active") == 1:
        sys.exit("Sauna is ON — run only while OFF.")

    f, w, fname = open_log(f"prime_watch_{value}")
    print(f"Prime+watch onTime={value} → {fname}")
    t0 = time.time()

    sample(eps, token, device_id, w, t0, "baseline")
    write_field(eps, token, device_id, "onTime", value)
    sample(eps, token, device_id, w, t0, f"WROTE_onTime_{value}")
    f.flush()

    print(f"\n  Waiting for reported.onTime to commit to {value}...")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(interval)
        rep, _ = sample(eps, token, device_id, w, t0, "wait-commit")
        f.flush()
        if rep.get("onTime") == value:
            print(f"\n  ✅ COMMITTED to {value} after {round(time.time()-t0,1)}s.")
            print(f"  >>> NOW turn the sauna ON (app or panel) within ~60s. I'm logging. <<<\n")
            break
    else:
        print("  reported.onTime never committed in 3 min — aborting.")
        f.close(); return

    # Watch for the human activation and capture the resulting live session length.
    end = time.time() + 6 * 60
    saw_active = False
    while time.time() < end:
        time.sleep(interval)
        rep, _ = sample(eps, token, device_id, w, t0, "watch-on")
        f.flush()
        if rep.get("active") == 1 and not saw_active:
            saw_active = True
            print("  >>> activation detected — capturing live remainingTime <<<")
        if saw_active and rep.get("active") == 0:
            print("  sauna turned off — stopping.")
            break
    f.close()
    print(f"Done. Log: {fname}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("poll", help="continuously log device state")
    pp.add_argument("--minutes", type=float, default=15)
    pp.add_argument("--interval", type=float, default=15)

    pc = sub.add_parser("commit-test", help="write onTime while OFF and watch for a time-based commit")
    pc.add_argument("--value", type=int, default=120)
    pc.add_argument("--minutes", type=float, default=15)
    pc.add_argument("--interval", type=float, default=15)

    pw = sub.add_parser("prime-watch", help="write onTime, wait for commit, prompt human to activate, log result")
    pw.add_argument("--value", type=int, default=90)
    pw.add_argument("--interval", type=float, default=10)

    args = p.parse_args()
    env = load_env()
    eps = fetch_endpoints()
    cog = authenticate(eps, env["HARVIA_USERNAME"], env["HARVIA_PASSWORD"])
    token = cog.id_token
    device_id = env["HARVIA_DEVICE_ID"]
    print(f"Authenticated. Device: {device_id}\n")

    if args.cmd == "poll":
        cmd_poll(eps, token, device_id, args.minutes, args.interval)
    elif args.cmd == "commit-test":
        cmd_commit_test(eps, token, device_id, args.value, args.minutes, args.interval)
    elif args.cmd == "prime-watch":
        cmd_prime_watch(eps, token, device_id, args.value, args.interval)


if __name__ == "__main__":
    main()
