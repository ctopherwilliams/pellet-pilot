"""Predict when a probe will reach its target ("when's the meat done?").

The estimate uses a RECENT window of readings, not the whole cook. A whole-cook
average badly overshoots through the stall (the 150-175deg plateau), so a recent
rate is both more responsive and more honest. During a stall the rate goes flat
and we say so instead of projecting a fake time.
"""
import datetime as dt

import numpy as np

STALL_LO, STALL_HI = 150, 175      # deg F — the classic brisket/pork plateau
DEFAULT_WINDOW_MIN = 20.0
_FLAT = 0.05                       # deg/min below which we treat rise as "not rising"


def forecast(times_min, temps, target, window_min=DEFAULT_WINDOW_MIN):
    """Fit a recent-window rate and project minutes-to-target.

    times_min : ascending minutes-from-start
    temps     : parallel temperatures
    target    : target temp (deg F) or None
    Returns dict: rate (deg/min|None), eta_min (float|None), status, current.
    status in {insufficient, no_target, done, stalled, not_rising, on_track}.
    """
    t = np.asarray(times_min, dtype=float)
    v = np.asarray(temps, dtype=float)
    if len(v) < 2:
        return {"rate": None, "eta_min": None, "status": "insufficient",
                "current": float(v[-1]) if len(v) else None}
    cur = float(v[-1])
    keep = t >= (t[-1] - window_min)
    tw, vw = (t[keep], v[keep]) if keep.sum() >= 2 else (t, v)
    if tw.max() == tw.min():
        # All samples in the window share one timestamp (e.g. two ticks landing
        # in the same second) -- no elapsed time to fit a rate from; polyfit on
        # a zero-variance x would hit a singular/non-converging least-squares.
        return {"rate": None, "eta_min": None, "status": "insufficient", "current": cur}
    rate = float(np.polyfit(tw, vw, 1)[0])
    if not target or target <= 0:
        return {"rate": rate, "eta_min": None, "status": "no_target", "current": cur}
    if cur >= target:
        return {"rate": rate, "eta_min": 0.0, "status": "done", "current": cur}
    if rate < _FLAT:
        status = "stalled" if STALL_LO <= cur <= STALL_HI else "not_rising"
        return {"rate": rate, "eta_min": None, "status": status, "current": cur}
    return {"rate": rate, "eta_min": (target - cur) / rate, "status": "on_track",
            "current": cur}


def forecast_stages(times_min, temps, stages, window_min=DEFAULT_WINDOW_MIN):
    """Predict the next unreached stage and the final stage.

    stages: [(temp, label)] (any order). Returns dict:
      current, rate, next (stage dict|None), final (stage dict|None), done (bool).
    Each stage dict: {temp, label, eta_min, status}. `final` is None when the next
    stage IS the final one (nothing extra to show).
    """
    stages = sorted(stages, key=lambda s: s[0])
    cur = float(temps[-1]) if len(temps) else None
    base = forecast(times_min, temps, stages[-1][0] if stages else None, window_min)
    out = {"current": cur, "rate": base["rate"], "next": None, "final": None, "done": False}
    if cur is None or not stages:
        return out
    remaining = [(t, lbl) for t, lbl in stages if cur < t]
    if not remaining:
        out["done"] = True
        return out

    def stage_fc(temp, label):
        fc = forecast(times_min, temps, temp, window_min)
        return {"temp": temp, "label": label, "eta_min": fc["eta_min"], "status": fc["status"]}

    out["next"] = stage_fc(*remaining[0])
    if remaining[-1][0] != remaining[0][0]:
        out["final"] = stage_fc(*remaining[-1])
    return out


def _stage_phrase(s, now):
    label = s["label"].upper()
    if s["status"] == "on_track" and s["eta_min"] is not None:
        clock = ""
        if now is not None:
            clock = " (≈ " + (now + dt.timedelta(minutes=s["eta_min"])).strftime("%-I:%M %p") + ")"
        return f"{label} at {int(s['temp'])}° in ~{s['eta_min']:.0f} min{clock}"
    if s["status"] == "stalled":
        return f"{label} at {int(s['temp'])}° — stalled, hold or wrap"
    if s["status"] == "not_rising":
        return f"{label} at {int(s['temp'])}° — not rising"
    return f"{label} at {int(s['temp'])}°"


def describe_stages(fcs, now=None):
    """Human 'next: ... · then: ...' line from a forecast_stages dict."""
    if fcs["current"] is None:
        return "no probe data"
    if fcs["done"]:
        return "all stages reached ✅"
    parts = ["next: " + _stage_phrase(fcs["next"], now)]
    if fcs["final"]:
        fin = fcs["final"]
        if fin["status"] == "on_track" and fin["eta_min"] is not None:
            parts.append(f"then {fin['label']} {int(fin['temp'])}° ~{fin['eta_min']:.0f} min (est)")
        else:
            parts.append(f"then {fin['label']} {int(fin['temp'])}°")
    return "  ·  ".join(parts)


def describe(fc, target, now=None):
    """One-line human summary of a forecast dict. `now` = datetime for a clock time."""
    rate = fc["rate"]
    r = f"{rate:+.2f}°/min" if rate is not None else "—"
    status = fc["status"]
    if status == "insufficient":
        return "need a couple more readings"
    if status == "no_target":
        return f"{r} · no target set"
    if status == "done":
        return f"done — reached {int(target)}° ✅"
    if status == "stalled":
        return f"stalled near {int(fc['current'])}° · hold, or wrap to push through  ({r})"
    if status == "not_rising":
        return f"not rising toward {int(target)}°  ({r})"
    eta = fc["eta_min"]
    clock = ""
    if now is not None:
        clock = " (≈ " + (now + dt.timedelta(minutes=eta)).strftime("%-I:%M %p") + ")"
    return f"~{eta:.0f} min to {int(target)}°{clock} · {r}"
