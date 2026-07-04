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


_update_count = 0

# Per-probe memory of the last tick's status + projected finish time, so the
# spoken update can narrate what CHANGED (a new ETA, a stall starting/ending)
# instead of reading the same flat template every ten seconds. Keyed by
# 1-based probe index; cleared implicitly whenever a probe stops reporting
# forecastable data (see speech_for_probes).
_last_state = {}

_ETA_EPS_MIN = 3.0  # minutes -- a smaller swing in the *finish clock* than this reads as "steady"


def _next_update_number():
    """Incrementing counter for spoken updates, e.g. "Update 12" -- not tied
    to poll count, so it stays meaningful even across a re-auth or a tick
    that had nothing new to say."""
    global _update_count
    _update_count += 1
    return _update_count


def _fmt_clock(now, eta_min):
    return (now + dt.timedelta(minutes=eta_min)).strftime("%-I:%M %p")


def _pick(bank, key):
    """Deterministic phrasing choice -- cycles through variants by tick/probe
    index rather than random.choice, so the same inputs always produce the
    same output (required for tests, and for sane debugging of a live cook).
    """
    return bank[key % len(bank)]


def _categorize(prev, status, rate, eta_min, now):
    """Classify this tick relative to the PREVIOUS tick for this probe.

    The interesting thing to say out loud isn't "here's the ETA" (that's
    covered by every branch already) -- it's whether anything actually
    changed: did the stall just start or just break, did the projected finish
    *clock time* actually move, or is it holding steady. Comparing finish
    clock times (not raw eta_min) is deliberate: eta_min counts down every
    tick even at a perfectly constant rate, since the probe is simply closer
    to target -- that's not a "new ETA", it's just time passing.

    Returns (category, finish_at) -- finish_at is the absolute predicted-done
    time to remember for next tick's comparison, or None when this tick has
    no on-track ETA (nothing to compare against later).
    """
    prev_status = prev.get("status") if prev else None
    if status in ("insufficient", "no_target"):
        return "opening", None
    if status == "done":
        return "done", None
    if status == "not_rising":
        return "not_rising", None
    if status == "stalled":
        return ("still_stalled" if prev_status == "stalled" else "entering_stall"), None
    # status == "on_track" -- forecast()/forecast_stages() guarantee eta_min is not None here.
    finish_at = now + dt.timedelta(minutes=eta_min)
    if prev_status == "stalled":
        return "breaking_stall", finish_at
    prev_finish = prev.get("finish_at") if prev else None
    if prev_finish is None:
        return "first_on_track", finish_at
    delta_min = (finish_at - prev_finish).total_seconds() / 60
    if delta_min <= -_ETA_EPS_MIN:
        return "new_eta_sooner", finish_at
    if delta_min >= _ETA_EPS_MIN:
        return "new_eta_later", finish_at
    return "steady", finish_at


# ---- phrasing banks -----------------------------------------------------
# Warm, plainspoken pitmaster voice -- a few natural variants per situation,
# not one fixed template. Every on-track-family variant names the {clock}
# time so the headline feature (never silently drop the ETA) holds no matter
# which variant gets picked; every stall variant says "stalled" outright.

_OPENING = [
    "still gathering data on probe {i}, need another read or two before I can call a time",
    "probe {i}'s too early to read yet -- gathering data before I'll guess a time",
    "hang on, still gathering data on probe {i} -- give me a couple more readings",
]

_DONE = [
    "probe {i}'s there -- pull it, that one's done",
    "that's a wrap, probe {i} hit target -- go get it",
    "probe {i}'s cooked through -- time to pull",
]

_NOT_RISING = [
    "probe {i}'s holding flat at {cur} right now -- not enough climb to call a time",
    "probe {i}'s leveled off at {cur}, outside the usual stall range -- I'll wait for it to move "
    "before guessing",
    "no real climb on probe {i} at the moment, sitting around {cur} -- too flat to call a time yet",
]

_ENTERING_STALL = [
    "oh, we're stalled -- probe {i}'s sitting at {cur}, no time estimate 'til it breaks through",
    "we just hit the stall on probe {i}, {cur} degrees and holding -- that's normal, it's pushing "
    "moisture, no ETA for now, it's stalled",
    "probe {i} just stalled out at {cur} -- won't fight it, just wait, no time estimate right now",
]

_STILL_STALLED = [
    "probe {i}'s still stalled at {cur} -- patience, it'll break",
    "still parked in that stall on probe {i}, {cur} degrees -- no new time yet",
    "probe {i} hasn't budged off the stall, still {cur} -- hang in there",
]

_BREAKING_STALL = [
    "good news -- probe {i} broke through the stall, climbing again, should get there in about "
    "{eta} minutes, around {clock}",
    "there it goes -- probe {i}'s moving again after that stall, new call is about {clock}",
    "probe {i} pushed past the stall -- back on track, looking at about {clock}",
]

_FIRST_ON_TRACK = [
    "probe {i}'s at {cur}, climbing about {rate} degrees a minute -- should get there in about "
    "{eta} minutes, around {clock}",
    "probe {i}'s moving along at {rate} a minute -- first call is about {clock}",
    "probe {i}'s climbing steady, {rate} a minute -- looks like about {clock}",
]

_NEW_ETA_SOONER = [
    "got a new ETA -- probe {i}'s picked up speed, {rate} a minute, now looking at about {clock} instead",
    "we're moving faster on probe {i} now -- that pulls the finish up to about {clock}",
    "good news, probe {i} sped up -- new call is about {clock}, sooner than before",
]

_NEW_ETA_LATER = [
    "got a new ETA -- probe {i}'s slowed down a touch, {rate} a minute, now looking at about {clock} instead",
    "probe {i} eased off the pace -- that pushes the finish back to about {clock}",
    "we're climbing slower on probe {i} now -- new call is about {clock}, a bit later than before",
]

_STEADY = [
    "got a new ETA? nope -- probe {i}'s steady, {rate} a minute, still about {clock}",
    "no change on probe {i} -- same pace, still tracking for about {clock}",
    "probe {i}'s right on the same track -- about {clock} still looks right",
]

_BANK_BY_CATEGORY = {
    "breaking_stall": _BREAKING_STALL,
    "first_on_track": _FIRST_ON_TRACK,
    "new_eta_sooner": _NEW_ETA_SOONER,
    "new_eta_later": _NEW_ETA_LATER,
    "steady": _STEADY,
}


def _speech_commentary(i, n, category, status, rate, eta_min, cur, now):
    """Pick a natural-language line for this probe's category. `n` seeds the
    deterministic variant choice (see _pick) -- pass the update number so
    phrasing varies tick to tick without being random.
    """
    cur_i = int(cur) if cur is not None else None
    if category == "opening":
        return _pick(_OPENING, n + i).format(i=i)
    if category == "done":
        return _pick(_DONE, n + i).format(i=i)
    if category == "not_rising":
        return _pick(_NOT_RISING, n + i).format(i=i, cur=cur_i)
    if category in ("entering_stall", "still_stalled"):
        bank = _ENTERING_STALL if category == "entering_stall" else _STILL_STALLED
        return _pick(bank, n + i).format(i=i, cur=cur_i)
    # on-track family: eta_min/rate are always real numbers here (see _categorize)
    clock = _fmt_clock(now, eta_min)
    eta_txt = f"{eta_min:.0f}"
    rate_txt = f"{rate:.1f}" if rate is not None else "?"
    return _pick(_BANK_BY_CATEGORY[category], n + i).format(
        i=i, cur=cur_i, rate=rate_txt, eta=eta_txt, clock=clock)


def speech_for_probes(row, stages):
    """Build the full, ready-to-speak update for every connected probe: temp,
    next stage/target, and a commentary phrase that always says *something*
    concrete about timing (an ETA, a "still gathering data", or an explicit
    stall/no-estimate) -- using the same live sample buffer and forecast
    logic that drives the printed prediction (print_forecasts must have run
    first this tick so _eta_samples is up to date; one_shot() guarantees
    that). The commentary also compares against last tick's state for this
    probe (_last_state) so it can call out what changed -- a fresh ETA, a
    stall starting or breaking -- rather than repeating a static readout.
    Returns "" if no probe has data.
    """
    now = dt.datetime.fromisoformat(row["ts"])
    parts = []
    n = _next_update_number()
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
                parts.append(f"probe {i} is at {int(temp)} degrees and every stage is done")
                _last_state.pop(i, None)
                continue
            fcs = forecast_stages(mins, temps, stages[i]) if len(temps) >= 2 else None
            status = fcs["next"]["status"] if fcs and fcs["next"] else "insufficient"
            eta_min = fcs["next"]["eta_min"] if fcs and fcs["next"] else None
            rate = fcs["rate"] if fcs else None
            dest = f"heading to {nxt[1]} at {int(nxt[0])}"
        else:
            tgt = row.get(f"probe{i}_set")
            if not tgt:
                parts.append(f"probe {i} is at {int(temp)} degrees")
                _last_state.pop(i, None)
                continue
            fc = forecast(mins, temps, float(tgt)) if len(temps) >= 2 else None
            status = fc["status"] if fc else "insufficient"
            eta_min = fc["eta_min"] if fc else None
            rate = fc["rate"] if fc else None
            dest = f"targeting {int(float(tgt))}"

        prev = _last_state.get(i)
        category, finish_at = _categorize(prev, status, rate, eta_min, now)
        commentary = _speech_commentary(i, n, category, status, rate, eta_min, temp, now)
        parts.append(f"probe {i} is at {int(temp)} degrees, {dest} -- {commentary}")
        _last_state[i] = {"status": status, "finish_at": finish_at}
    if not parts:
        return ""
    grill = row.get("grill")
    grill_phrase = f" Grill is at {int(float(grill))} degrees." if grill not in (None, "", "None") else ""
    return f"Update {n}. " + ". ".join(parts) + f".{grill_phrase}"


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
            text = speech_for_probes(row, stages)  # full sentence, incl. "Update N" + grill temp
            if text:
                speak(text)
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
