"""Cook plan — per-probe stages (target temp + action label), with persistence.

A stage is a milestone in a cook: a target temp and what to do at it. Example
brisket plan: 165deg -> wrap, 203deg -> done. Up to a handful of stages per probe.

Stage spec syntax  [PROBE:]TEMP[:LABEL]:
  "203"          -> probe 1, 203deg, default label
  "165:wrap"     -> probe 1, 165deg, label "wrap"
  "2:170"        -> probe 2, 170deg, default label
  "2:170:wrap"   -> probe 2, 170deg, label "wrap"

Unlabeled top stage defaults to "done"; others to "stage N". The active plan is
saved to .cook_plan.json so trend.py / history.py reuse it automatically.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_FILE = os.path.join(HERE, ".cook_plan.json")
_MAX_PLAN_FILE_BYTES = 256 * 1024  # a real plan is a few dozen bytes; same cap as the MQTT payload guard


def parse_stage(spec):
    """Return (probe:int, temp:float, label:str|None), or None for a blank spec."""
    spec = spec.strip()
    if not spec:
        return None
    parts = spec.split(":")
    if len(parts) == 1:
        return (1, float(parts[0]), None)
    if len(parts) == 2:
        a, b = parts
        try:
            return (int(a), float(b), None)     # "2:170" -> probe:temp
        except ValueError:
            return (1, float(a), b or None)     # "165:wrap" -> temp:label
    return (int(parts[0]), float(parts[1]), ":".join(parts[2:]) or None)


def build_plan(specs):
    """Iterable of stage specs -> {probe:int -> [(temp, label), ...]} sorted ascending."""
    plan = {}
    for spec in specs:
        parsed = parse_stage(spec)
        if parsed:
            probe, temp, label = parsed
            plan.setdefault(probe, []).append([temp, label])
    for stages in plan.values():
        stages.sort(key=lambda s: s[0])
        for i, s in enumerate(stages):
            if not s[1]:
                s[1] = "done" if i == len(stages) - 1 else f"stage {i + 1}"
    return {p: [(t, l) for t, l in s] for p, s in plan.items()}


def save_plan(plan, path=PLAN_FILE):
    with open(path, "w") as f:
        json.dump({str(p): s for p, s in plan.items()}, f)


def load_plan(path=PLAN_FILE):
    if not os.path.exists(path):
        return {}
    size = os.path.getsize(path)
    if size > _MAX_PLAN_FILE_BYTES:
        raise ValueError(
            f"{path} is {size} bytes, over the {_MAX_PLAN_FILE_BYTES}-byte sanity "
            "cap for a cook plan; refusing to load. If this file is legitimately "
            "this large something's wrong -- delete it and re-run with --stage."
        )
    with open(path) as f:
        raw = json.load(f)
    return {int(p): [(t, l) for t, l in s] for p, s in raw.items()}
