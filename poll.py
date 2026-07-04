#!/usr/bin/env python3
"""
Poll the Traeger and append readings to cook_log.csv.

Usage:
  ./venv/bin/python poll.py            # one reading, printed + logged
  ./venv/bin/python poll.py --watch 30 # log every 30s until Ctrl-C

Credentials come from environment or a local .env file (see .env.example):
  TRAEGER_USERNAME, TRAEGER_PASSWORD
"""

import csv
import datetime as dt
import os
import subprocess
import sys
import time

from traeger_client import Traeger, parse_status

KEYCHAIN_SERVICE = "traeger-wifire"
HERE = os.path.dirname(os.path.abspath(__file__))
BW_SESSION_FILE = os.path.join(HERE, ".bw_session")

LOG = os.path.join(os.path.dirname(__file__), "cook_log.csv")
FIELDS = ["ts", "thing", "grill", "set", "ambient", "system_status",
          "probe1_temp", "probe1_set", "probe1_connected", "probe1_alarm"]

# Controller status codes (from the WiFire protocol). On newer Timberline
# controllers 99 is the normal running state, not "offline" -- so we trust
# connected + live temps over the raw code.
STATUS_MAP = {
    2: "Sleeping", 3: "Idle", 4: "Igniting", 5: "Preheating",
    6: "Manual cook", 7: "Custom cook", 8: "Cool-down", 9: "Shutting down",
    99: "Running",
}


def decode_status(code, connected, grill_temp):
    name = STATUS_MAP.get(code, f"code {code}")
    if code == 99 and not (connected and (grill_temp or 0) > 120):
        return "Offline"          # old-D2 meaning, only when clearly not cooking
    return name


def _applescript_escape(text):
    """Escape user-influenced strings before embedding in AppleScript."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title, message):
    """Best-effort macOS notification + spoken alert. No-op elsewhere."""
    safe_title = _applescript_escape(title)
    safe_message = _applescript_escape(message)
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_message}" with title "{safe_title}" sound name "Glass"'],
            capture_output=True,
        )
        subprocess.run(["say", safe_message], capture_output=True)
    except FileNotFoundError:
        pass
    print("\a", end="")  # terminal bell


def load_env():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def keychain_password(account):
    """Read the Traeger password from the macOS Keychain, if stored there."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", account,
             "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except FileNotFoundError:
        pass  # not macOS / no `security` binary
    return None


def _bw_session():
    """Get an unlocked Bitwarden session key from env or the local session file."""
    sess = os.environ.get("BW_SESSION")
    if sess:
        return sess
    if os.path.exists(BW_SESSION_FILE):
        mode = os.stat(BW_SESSION_FILE).st_mode & 0o777
        if mode != 0o600:
            print(
                f"  warning: {BW_SESSION_FILE} is mode {mode:o}; "
                "chmod 600 recommended (grants vault access)"
            )
        with open(BW_SESSION_FILE) as f:
            return f.read().strip()
    return None


def bitwarden_password(item):
    """Fetch the Traeger password field from a Bitwarden vault item (by name or id)."""
    if not item:
        return None
    sess = _bw_session()
    if not sess:
        return None
    try:
        out = subprocess.run(
            ["bw", "get", "password", item, "--session", sess],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None  # bw CLI not installed
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    # surface a useful hint (locked vault, wrong item, stale session)
    msg = (out.stderr or out.stdout).strip()
    if msg:
        print(f"  Bitwarden: {msg}")
    return None


def row_from(reading):
    p = reading["probes"][0] if reading["probes"] else {}
    return {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "thing": reading["thing"],
        "grill": reading["grill"],
        "set": reading["set"],
        "ambient": reading["ambient"],
        "system_status": reading["system_status"],
        "probe1_temp": p.get("get_temp"),
        "probe1_set": p.get("set_temp"),
        "probe1_connected": p.get("connected"),
        "probe1_alarm": p.get("alarm_fired"),
    }


def append(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


_fired = set()  # alarm thresholds already triggered this run


def check_alarms(row, alarms):
    """Fire once when the probe rises to/through each threshold."""
    temp = row["probe1_temp"]
    if temp is None:
        return
    for thr in alarms:
        if temp >= thr and thr not in _fired:
            _fired.add(thr)
            notify("Traeger probe", f"Probe reached {int(thr)}°F (now {int(temp)}°F)")
            print(f"  🔔 ALARM: probe crossed {int(thr)}°F")


def one_shot(t, alarms=()):
    status = t.poll()
    for thing, doc in status.items():
        reading = parse_status(thing, doc)
        row = row_from(reading)
        append(row)
        probe = f"{row['probe1_temp']}°" if row["probe1_temp"] is not None else "--"
        state = decode_status(row["system_status"], row["probe1_connected"], row["grill"])
        print(f"[{row['ts']}] grill {row['grill']}° (set {row['set']}°)  "
              f"probe {probe} (set {row['probe1_set']}°)  [{state}]")
        # auto-arm the probe's own target if the user didn't specify thresholds
        active = list(alarms) if alarms else (
            [row["probe1_set"]] if row["probe1_set"] else [])
        check_alarms(row, active)


def main():
    load_env()
    user = os.environ.get("TRAEGER_USERNAME")
    if not user:
        sys.exit("Missing TRAEGER_USERNAME. Set it in .env.")

    # Password resolution order:
    #   1. TRAEGER_PASSWORD env (explicit override)
    #   2. Bitwarden vault item (TRAEGER_BW_ITEM), via an unlocked bw session
    #   3. macOS Keychain
    pw = os.environ.get("TRAEGER_PASSWORD")
    src = "env"
    if not pw:
        pw = bitwarden_password(os.environ.get("TRAEGER_BW_ITEM"))
        src = "bitwarden"
    if not pw:
        pw = keychain_password(user)
        src = "keychain"
    if not pw:
        sys.exit(
            "No password found. Options:\n"
            "  - Bitwarden: unlock the vault into .bw_session and set TRAEGER_BW_ITEM in .env\n"
            f'  - Keychain:  security add-generic-password -a "{user}" -s {KEYCHAIN_SERVICE} -w\n'
            "  - or set TRAEGER_PASSWORD in .env"
        )
    if os.environ.get("PELLET_PILOT_VERBOSE"):
        print(f"(password from {src})")

    t = Traeger(user, pw)
    t.login()
    t.load_grills()
    names = ", ".join(g["thingName"] for g in t.grills)
    print(f"Connected to Traeger account. Grill(s): {names}\nLogging to {LOG}")

    interval = None
    if "--watch" in sys.argv:
        i = sys.argv.index("--watch")
        interval = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 30

    # Alarm thresholds: repeatable --alarm N, and/or env PROBE_ALARMS="160,165".
    # If none given, one_shot auto-arms the probe's own target temp.
    alarms = []
    for j, a in enumerate(sys.argv):
        if a == "--alarm" and j + 1 < len(sys.argv):
            alarms.append(float(sys.argv[j + 1]))
    for v in (os.environ.get("PROBE_ALARMS") or "").split(","):
        v = v.strip()
        if v:
            alarms.append(float(v))
    alarms = sorted(set(alarms))
    if alarms:
        print(f"Alarms armed at: {', '.join(str(int(a)) for a in alarms)}°F")

    if interval is None:
        one_shot(t, alarms)
        return

    print(f"Watching every {interval}s. Ctrl-C to stop.")
    try:
        while True:
            try:
                one_shot(t, alarms)
            except Exception as e:
                print(f"  poll error (will retry): {e}")
                # token/signed-URL likely expired (~1h) -- re-auth for long cooks
                try:
                    t.login()
                    t.load_grills()
                    print("  re-authenticated")
                except Exception as e2:
                    print(f"  re-auth failed: {e2}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
