"""Wrap Coach -- rule-based, mid-cook advice on whether to hold or wrap.

No ML, no network: a handful of readable thresholds layered on the SAME
recent-window forecast (forecast.py) and stall band the rest of Pellet Pilot
already uses for its ETA, so the advice never contradicts what the printed
prediction is already telling you.
"""
from forecast import _FLAT, STALL_HI, STALL_LO, forecast

# How long the CURRENT, ongoing stall has to run before advice escalates.
_LONG_STALL_MIN = 90.0
# How close to target before it's time to start thinking about the rest.
_NEAR_DONE_MIN = 30.0


def current_stall_minutes(times_min, temps):
    """Minutes of the current, ongoing stall streak, walking backward from
    the most recent sample -- NOT total stall time across the whole cook.
    Zero as soon as the most recent step leaves the stall band or the rate
    stops being flat (same threshold forecast.py uses to call it "stalled").
    """
    total = 0.0
    for i in range(len(temps) - 1, 0, -1):
        v0, v1 = temps[i - 1], temps[i]
        t0, t1 = times_min[i - 1], times_min[i]
        if v0 is None or v1 is None:
            break
        if not (STALL_LO <= v0 <= STALL_HI and STALL_LO <= v1 <= STALL_HI):
            break
        elapsed = t1 - t0
        if elapsed <= 0:
            break
        if (v1 - v0) / elapsed >= _FLAT:
            break
        total += elapsed
    return total


def recommend(times_min, temps, target=None, wrapped=False):
    """One coaching line + an urgency ('info'|'suggest'|'urgent').

    Mirrors forecast()'s own status so advice never disagrees with the
    printed ETA line for the same tick.
    """
    if len(temps) < 2:
        return {"advice": "Not enough data yet to coach this cook.",
                "urgency": "info", "status": "insufficient"}

    fc = forecast(times_min, temps, target)
    status = fc["status"]

    if status in ("insufficient", "no_target"):
        return {"advice": "Not enough data yet to coach this cook.",
                "urgency": "info", "status": status}

    if status == "done":
        return {"advice": "At target -- pull it and rest at least 30-60 min before slicing.",
                "urgency": "info", "status": status}

    if status == "not_rising":
        return {"advice": "Not climbing, and it's outside the usual 150-175 stall range -- "
                           "worth checking the fire, pellets, or lid seal.",
                "urgency": "suggest", "status": status}

    if status == "stalled":
        stall_min = current_stall_minutes(times_min, temps)
        if wrapped:
            if stall_min >= _LONG_STALL_MIN:
                return {"advice": f"Already wrapped and still stalled after {stall_min:.0f} min -- "
                                   "consider bumping the grill temp 10-15 degrees.",
                        "urgency": "urgent", "status": status}
            return {"advice": f"Wrapped and holding at the stall ({stall_min:.0f} min so far) -- "
                               "normal, give it more time.",
                    "urgency": "info", "status": status}
        if stall_min >= _LONG_STALL_MIN:
            return {"advice": f"{stall_min:.0f} minutes into the stall -- that's a long one. "
                               "Wrap now to push through, unless you're deliberately going low and slow.",
                    "urgency": "urgent", "status": status}
        return {"advice": f"{stall_min:.0f} minutes into the stall -- normal, bark's still setting. "
                           "Hold for more bark, or wrap now to push through faster.",
                "urgency": "suggest", "status": status}

    # on_track -- rate/eta_min are always real numbers here
    eta_min = fc["eta_min"]
    if eta_min is not None and eta_min <= _NEAR_DONE_MIN:
        return {"advice": f"Getting close (~{eta_min:.0f} min out) -- start planning your rest time.",
                "urgency": "suggest", "status": status}
    return {"advice": f"Climbing steadily at {fc['rate']:.1f} deg/min -- no action needed.",
            "urgency": "info", "status": status}


def _wrap_stage_temp(stages_for_probe):
    for temp, label in stages_for_probe or []:
        if "wrap" in label.lower():
            return temp
    return None


def recommend_for_probe(times_min, temps, stages_for_probe=None, target=None):
    """High-level entry point: auto-detects wrap status from a stage plan
    (a 'wrap' stage that's already been crossed) if one is given, projecting
    to the final stage temp; otherwise falls back to a plain probe target
    with wrapped=False.
    """
    cur = temps[-1] if temps else None
    wrap_temp = _wrap_stage_temp(stages_for_probe)
    wrapped = bool(wrap_temp is not None and cur is not None and cur >= wrap_temp)
    if stages_for_probe:
        final_temp = max(t for t, _ in stages_for_probe)
        return recommend(times_min, temps, final_temp, wrapped=wrapped)
    return recommend(times_min, temps, target, wrapped=False)
