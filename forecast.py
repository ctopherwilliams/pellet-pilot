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
