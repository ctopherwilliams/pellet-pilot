#!/usr/bin/env python3
"""
Poll the Traeger and append readings to cook_log.csv.

Usage:
  ./venv/bin/python poll.py            # one reading, printed + logged
  ./venv/bin/python poll.py --watch 30 # log every 30s until Ctrl-C
  ./venv/bin/python poll.py --watch 30 --speak  # + a spoken update every tick
  ./venv/bin/python poll.py --watch 30 --speak --stage 165:wrap --stage 205:done \
      --chart cook.html    # set it and forget it: logs, speaks, and keeps a
                            # self-refreshing chart at cook.html up to date --
                            # open that file once in a browser and walk away.

Credentials come from environment or a local .env file (see .env.example):
  TRAEGER_USERNAME, TRAEGER_PASSWORD
"""

import csv
import datetime as dt
import os
import re
import subprocess
import sys
import time

import plan
from alarms import notify_remote
from forecast import describe, describe_stages, forecast, forecast_stages
from traeger_client import Traeger, TraegerError, parse_status

KEYCHAIN_SERVICE = "traeger-wifire"
HERE = os.path.dirname(os.path.abspath(__file__))
BW_SESSION_FILE = os.path.join(HERE, ".bw_session")
_MAX_BACKOFF_S = 600  # cap re-auth retry backoff at 10 minutes


def _backoff_seconds(interval, consecutive_failures):
    """Exponential backoff after repeated re-auth failures, capped at _MAX_BACKOFF_S.

    Keeps a persistently-failing --watch loop from hammering Cognito with an
    auth attempt every `interval` seconds indefinitely.
    """
    return min(interval * (2 ** consecutive_failures), _MAX_BACKOFF_S)

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


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _applescript_escape(text):
    """Escape user-influenced strings before embedding in AppleScript.

    Strips control characters (including newlines, which AppleScript string
    literals can't contain) before escaping backslashes/quotes, so a crafted
    stage label (--stage, PROBE_STAGES, .cook_plan.json) can't break out of
    the quoted string or corrupt the script passed to osascript.
    """
    text = _CONTROL_CHARS.sub(" ", text).replace("\n", " ").replace("\r", " ")
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


def speak(text):
    """Best-effort spoken update via macOS `say` -- no banner, no bell.

    Unlike notify(), this is meant to run every tick (not just on alarm/stage
    crossings), so it's deliberately quieter: speech only. No-op elsewhere or
    if `say` isn't installed.
    """
    clean = _CONTROL_CHARS.sub(" ", text).replace("\n", " ").replace("\r", " ")
    try:
        subprocess.run(["say", clean], capture_output=True)
    except FileNotFoundError:
        pass


def _speech_eta(status, eta_min, now):
    """Spoken ETA fragment appended after the target/stage phrase.

    Mirrors the same recent-window, stall-aware forecast used for the printed
    prediction (see print_forecasts/forecast) -- never invents an ETA through
    a stall.
    """
    if status == "on_track" and eta_min is not None:
        clock = ""
        if now is not None:
            clock = f", around {(now + dt.timedelta(minutes=eta_min)).strftime('%-I:%M %p')}"
        return f", about {eta_min:.0f} minutes away{clock}"
    if status == "stalled":
        return ", but it's stalled, no estimate right now"
    return ""


def speech_for_probes(row, stages):
    """Build a short spoken summary of every connected probe: temp, next
    stage/target, and an ETA -- using the same live sample buffer and forecast
    logic that drives the printed prediction (print_forecasts must have run
    first this tick so _eta_samples is up to date; one_shot() guarantees that).
    """
    now = dt.datetime.fromisoformat(row["ts"])
    parts = []
    for i in range(1, MAX_PROBES + 1):
        temp = row.get(f"probe{i}_temp")
        if not row.get(f"probe{i}_connected") or temp is None:
            continue
        samples = _eta_samples.get(i, [])
        if samples:
            t0 = samples[0][0]
            mins = [(t - t0).total_seconds() / 60 for t, _ in samples]
            temps = [v for _, v in samples]
        else:
            mins, temps = [], []
        if stages.get(i):
            nxt = next(((t_, lbl) for t_, lbl in stages[i] if temp < t_), None)
            if not nxt:
                parts.append(f"probe {i}, {int(temp)} degrees, all stages done")
                continue
            eta = ""
            if len(temps) >= 2:
                fcs = forecast_stages(mins, temps, stages[i])
                if fcs["next"]:
                    eta = _speech_eta(fcs["next"]["status"], fcs["next"]["eta_min"], now)
            parts.append(f"probe {i}, {int(temp)} degrees, next {nxt[1]} at {int(nxt[0])}{eta}")
        else:
            tgt = row.get(f"probe{i}_set")
            if not tgt:
                parts.append(f"probe {i}, {int(temp)} degrees")
                continue
            eta = ""
            if len(temps) >= 2:
                fc = forecast(mins, temps, float(tgt))
                eta = _speech_eta(fc["status"], fc["eta_min"], now)
            parts.append(f"probe {i}, {int(temp)} degrees, target {int(float(tgt))}{eta}")
    return ". ".join(parts)


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
        # Pass the session key via BW_SESSION (which `bw` reads automatically),
        # not --session -- a CLI arg is visible to other local users/processes
        # via `ps`/`/proc/<pid>/cmdline` for the life of the subprocess.
        out = subprocess.run(
            ["bw", "get", "password", item],
            capture_output=True, text=True,
            env={**os.environ, "BW_SESSION": sess},
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


_fired = set()          # (probe_index, threshold) pairs already triggered this run
_eta_samples = {}       # probe index -> [(datetime, temp)] for the live prediction


def print_forecasts(row, stages=None):
    """Live 'when's it done' line per connected probe — stage-aware if a plan exists."""
    stages = stages or {}
    now = dt.datetime.fromisoformat(row["ts"])
    for i in range(1, MAX_PROBES + 1):
        temp = row.get(f"probe{i}_temp")
        if not row.get(f"probe{i}_connected") or temp is None:
            continue
        samples = _eta_samples.setdefault(i, [])
        samples.append((now, float(temp)))
        t0 = samples[0][0]
        mins = [(t - t0).total_seconds() / 60 for t, _ in samples]
        temps = [v for _, v in samples]
        if len(temps) < 2:
            continue  # wait for a second reading before predicting
        if stages.get(i):
            print(f"  ⏱  P{i} {describe_stages(forecast_stages(mins, temps, stages[i]), now=now)}")
        elif row.get(f"probe{i}_set"):
            target = float(row[f"probe{i}_set"])
            fc = forecast(mins, temps, target)
            if fc["status"] != "insufficient":
                print(f"  ⏱  P{i} {describe(fc, target, now=now)}")


_STAGE_ACTIONS = {"wrap": "WRAP IT", "done": "DONE — rest it", "rest": "RESTING done"}


def check_stage_alarms(row, stages):
    """Fire a labeled alarm once when a probe crosses each stage temp."""
    for probe, stagelist in stages.items():
        temp = row.get(f"probe{probe}_temp")
        if temp is None:
            continue
        for stemp, label in stagelist:
            key = ("stage", probe, stemp)
            if temp >= stemp and key not in _fired:
                _fired.add(key)
                action = _STAGE_ACTIONS.get(label.lower(), label.upper())
                msg = f"Probe {probe} hit {int(stemp)}° — {action}"
                notify("Traeger stage", msg)
                notify_remote("Traeger stage", msg)
                print(f"  🔔 STAGE: probe {probe} {int(stemp)}° — {action}")


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


def one_shot(t, alarms=None, stages=None, speak_every_tick=False, chart_path=None, chart_probe=1):
    alarms = alarms or {}
    stages = stages or {}
    status = t.poll()
    for thing, doc in status.items():
        reading = parse_status(thing, doc)
        row = row_from(reading)
        append(row)
        state = decode_status(row["system_status"], row["probe1_connected"], row["grill"])
        parts = []
        for i in range(1, MAX_PROBES + 1):
            if not row.get(f"probe{i}_connected"):
                continue
            if stages.get(i):
                plan_txt = " → ".join(f"{lbl} {int(t_)}°" for t_, lbl in stages[i])
                parts.append(f"P{i} {row[f'probe{i}_temp']}° → {plan_txt}")
            else:
                tgt = row.get(f"probe{i}_set")
                parts.append(f"P{i} {row[f'probe{i}_temp']}°" + (f"→{tgt}°" if tgt else ""))
        probes_txt = "  ".join(parts) if parts else "no probes"
        print(f"[{row['ts']}] grill {row['grill']}° (set {row['set']}°)  {probes_txt}  [{state}]")
        print_forecasts(row, stages)  # live "when's it done" prediction (stage-aware)
        if speak_every_tick:
            text = speech_for_probes(row, stages)
            if text:
                speak(f"Update. {text}. Grill {int(row['grill'])} degrees.")
        if chart_path:
            # Local import: plot.py imports MAX_PROBES from this module, so a
            # top-level `import plot` here would be circular. Best-effort --
            # a chart-write hiccup must never interrupt the cook log.
            try:
                import plot
                plot.write_chart(chart_path, chart_probe, stages)
            except Exception as e:
                print(f"  chart write skipped: {e}")
        if stages:
            check_stage_alarms(row, stages)
        # plain alarms: explicit --alarm, else auto-arm probe targets (unless stages cover it)
        active = alarms
        if not active and not stages:
            active = {i: [row[f"probe{i}_set"]]
                      for i in range(1, MAX_PROBES + 1)
                      if row.get(f"probe{i}_connected") and row.get(f"probe{i}_set")}
        if active:
            check_alarms(row, active)


def resolve_password(user):
    """Resolve the Traeger password: env override, then Bitwarden, then Keychain.

    Pops TRAEGER_PASSWORD out of the process environment as soon as it's read,
    so it isn't inherited by every child subprocess spawned afterward (bw,
    security, osascript, say) for the rest of the run. Returns (password, source)
    or (None, None) if nothing is configured.
    """
    pw = os.environ.pop("TRAEGER_PASSWORD", None)
    if pw:
        return pw, "env"
    pw = bitwarden_password(os.environ.get("TRAEGER_BW_ITEM"))
    if pw:
        return pw, "bitwarden"
    pw = keychain_password(user)
    if pw:
        return pw, "keychain"
    return None, None


def reauth(t, user):
    """Renew the session for a long --watch cook.

    Tries the refresh token first (no password required -- login() already
    wiped it from memory). Falls back to a full re-login only if the refresh
    token itself is rejected, which needs the password re-resolved. Note: if
    the password's source was the plain env var, resolve_password() already
    consumed it at startup (see its docstring), so that fallback path won't
    have anything to re-resolve for env-only setups -- use Bitwarden or
    Keychain if you want a --watch cook to survive a refresh-token failure.
    """
    try:
        t.refresh()
        t.load_grills()
        return "refresh token"
    except Exception as refresh_err:
        pw, _ = resolve_password(user)
        if not pw:
            raise TraegerError(
                f"Refresh failed ({refresh_err}) and no password available for a "
                "full re-login."
            ) from refresh_err
        t.password = pw
        t.login()
        t.load_grills()
        return "full login"


def main():
    load_env()
    user = os.environ.get("TRAEGER_USERNAME")
    if not user:
        sys.exit("Missing TRAEGER_USERNAME. Set it in .env.")

    pw, src = resolve_password(user)
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

    # Cook stages: --stage [PROBE:]TEMP[:LABEL] and/or PROBE_STAGES env; persisted to
    # .cook_plan.json (reused by trend.py/history.py) unless --no-plan.
    stage_specs = [sys.argv[j + 1] for j, a in enumerate(sys.argv)
                   if a == "--stage" and j + 1 < len(sys.argv)]
    stage_specs += (os.environ.get("PROBE_STAGES") or "").split(",")
    stages = plan.build_plan(stage_specs)
    if stages and "--no-plan" not in sys.argv:
        plan.save_plan(stages)
    elif not stages:
        stages = plan.load_plan()  # reuse a persisted plan if none given
    if stages:
        desc = "; ".join("P%d: %s" % (p, " → ".join(f"{lbl} {int(t_)}°" for t_, lbl in s))
                         for p, s in sorted(stages.items()))
        print(f"Cook plan — {desc}")

    # Spoken updates every tick (not just alarm/stage crossings): --speak or
    # PELLET_PILOT_SPEAK=1. Off by default so it doesn't talk over you unasked.
    speak_every_tick = "--speak" in sys.argv or \
        os.environ.get("PELLET_PILOT_SPEAK", "").lower() in ("1", "true", "yes")
    if speak_every_tick:
        print("Speaking an update every tick (--speak).")

    # Set-it-and-forget-it chart: --chart PATH (or PELLET_PILOT_CHART) rewrites
    # a self-refreshing HTML forecast chart every tick -- open it once in a
    # browser and walk away, no server required (see plot.write_chart).
    chart_path = None
    if "--chart" in sys.argv:
        i = sys.argv.index("--chart")
        chart_path = sys.argv[i + 1] if i + 1 < len(sys.argv) else "cook.html"
    chart_path = chart_path or os.environ.get("PELLET_PILOT_CHART")
    chart_probe = int(sys.argv[sys.argv.index("--chart-probe") + 1]) \
        if "--chart-probe" in sys.argv else 1
    if chart_path:
        print(f"Writing a live chart to {chart_path} every tick -- open it once and leave it.")

    if interval is None:
        one_shot(t, alarms, stages, speak_every_tick, chart_path, chart_probe)
        return

    print(f"Watching every {interval}s. Ctrl-C to stop.")
    consecutive_failures = 0
    try:
        while True:
            try:
                one_shot(t, alarms, stages, speak_every_tick, chart_path, chart_probe)
                consecutive_failures = 0
                time.sleep(interval)
            except Exception as e:
                print(f"  poll error (will retry): {e}")
                # token/signed-URL likely expired (~1h) -- re-auth for long cooks
                try:
                    how = reauth(t, user)
                    print(f"  re-authenticated ({how})")
                    consecutive_failures = 0
                    time.sleep(interval)
                except Exception as e2:
                    consecutive_failures += 1
                    backoff = _backoff_seconds(interval, consecutive_failures)
                    print(f"  re-auth failed: {e2}")
                    print(f"  backing off {backoff:.0f}s (attempt {consecutive_failures}) "
                          "before retrying, to avoid hammering Cognito")
                    time.sleep(backoff)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
