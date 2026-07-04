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

from alarms import notify_remote
from traeger_client import Traeger, parse_status

KEYCHAIN_SERVICE = "traeger-wifire"
HERE = os.path.dirname(os.path.abspath(__file__))
BW_SESSION_FILE = os.path.join(HERE, ".bw_session")

LOG = os.path.join(os.path.dirname(__file__), "cook_log.csv")

MAX_PROBES = 4  # widen the log to support multiple meat probes
_BASE_FIELDS = ["ts", "thing", "grill", "set", "ambient", "system_status"]
FIELDS = _BASE_FIELDS + [
    f"probe{i}_{suffix}"
    for i in range(1, MAX_PROBES + 1)
    for suffix in ("temp", "set", "connected", "alarm")
]

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
    row = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "thing": reading["thing"],
        "grill": reading["grill"],
        "set": reading["set"],
        "ambient": reading["ambient"],
        "system_status": reading["system_status"],
    }
    probes = reading["probes"]
    for i in range(1, MAX_PROBES + 1):
        p = probes[i - 1] if i - 1 < len(probes) else {}
        row[f"probe{i}_temp"] = p.get("get_temp")
        row[f"probe{i}_set"] = p.get("set_temp")
        row[f"probe{i}_connected"] = p.get("connected")
        row[f"probe{i}_alarm"] = p.get("alarm_fired")
    return row


def append(row):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


_fired = set()  # (probe_index, threshold) pairs already triggered this run


def check_alarms(row, alarms):
    """Fire once when a probe rises to/through each of its thresholds.

    `alarms` maps a 1-based probe index to an iterable of threshold temps.
    """
    for probe, thresholds in alarms.items():
        temp = row.get(f"probe{probe}_temp")
        if temp is None:
            continue
        for thr in thresholds:
            key = (probe, thr)
            if temp >= thr and key not in _fired:
                _fired.add(key)
                msg = f"Probe {probe} reached {int(thr)}°F (now {int(temp)}°F)"
                notify("Traeger probe", msg)          # local desktop/voice
                notify_remote("Traeger probe", msg)   # Pushover/ntfy/webhook, if configured
                print(f"  🔔 ALARM: probe {probe} crossed {int(thr)}°F")


def one_shot(t, alarms=None):
    alarms = alarms or {}
    status = t.poll()
    for thing, doc in status.items():
        reading = parse_status(thing, doc)
        row = row_from(reading)
        append(row)
        state = decode_status(row["system_status"], row["probe1_connected"], row["grill"])
        parts = []
        for i in range(1, MAX_PROBES + 1):
            if row.get(f"probe{i}_connected"):
                tgt = row.get(f"probe{i}_set")
                parts.append(f"P{i} {row[f'probe{i}_temp']}°" + (f"→{tgt}°" if tgt else ""))
        probes_txt = "  ".join(parts) if parts else "no probes"
        print(f"[{row['ts']}] grill {row['grill']}° (set {row['set']}°)  {probes_txt}  [{state}]")
        # auto-arm each connected probe's own target if no explicit alarms were given
        active = alarms
        if not active:
            active = {i: [row[f"probe{i}_set"]]
                      for i in range(1, MAX_PROBES + 1)
                      if row.get(f"probe{i}_connected") and row.get(f"probe{i}_set")}
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

    # Alarm thresholds: repeatable --alarm [PROBE:]TEMP and/or env PROBE_ALARMS.
    #   "203"   -> probe 1 at 203°      "2:203" -> probe 2 at 203°
    # If none given, one_shot auto-arms each connected probe's own target temp.
    alarms = {}

    def _add_alarm(spec):
        spec = spec.strip()
        if not spec:
            return
        probe, temp = (spec.split(":", 1) if ":" in spec else ("1", spec))
        alarms.setdefault(int(probe), set()).add(float(temp))

    for j, a in enumerate(sys.argv):
        if a == "--alarm" and j + 1 < len(sys.argv):
            _add_alarm(sys.argv[j + 1])
    for v in (os.environ.get("PROBE_ALARMS") or "").split(","):
        _add_alarm(v)
    alarms = {p: sorted(ts) for p, ts in alarms.items()}
    if alarms:
        desc = "; ".join(f"P{p} {'/'.join(str(int(x)) for x in ts)}"
                         for p, ts in sorted(alarms.items()))
        print(f"Alarms armed — {desc} °F")

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
