#!/usr/bin/env python3
"""
Trend analysis on the probe temperature in cook_log.csv.

Fits a linear trend over a recent window, reports the rise rate (deg/min),
and — if a probe target is set — estimates time-to-target.

Usage:
  ./venv/bin/python trend.py                # all data, probe1
  ./venv/bin/python trend.py --window 20    # last 20 minutes only
  ./venv/bin/python trend.py --col grill    # trend the grill temp instead
"""

import csv
import datetime as dt
import os
import sys

import numpy as np

import plan
from forecast import describe, describe_stages, forecast, forecast_stages

LOG = os.path.join(os.path.dirname(__file__), "cook_log.csv")


def load(col):
    if not os.path.exists(LOG):
        sys.exit(f"No log yet at {LOG}. Run poll.py --watch first.")
    ts, val, target = [], [], None
    with open(LOG) as f:
        for r in csv.DictReader(f):
            v = r.get(col)
            if v in (None, "", "None"):
                continue
            ts.append(dt.datetime.fromisoformat(r["ts"]))
            val.append(float(v))
            if col.startswith("probe") and col.endswith("_temp"):
                st = r.get(col.replace("_temp", "_set"))
            else:
                st = r.get("set")
            if st not in (None, "", "None"):
                target = float(st)
    if len(val) < 2:
        sys.exit(f"Need at least 2 readings for '{col}'; have {len(val)}.")
    return ts, np.array(val), target


def sparkline(v):
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = v.min(), v.max()
    if hi == lo:
        return blocks[0] * len(v)
    idx = ((v - lo) / (hi - lo) * (len(blocks) - 1)).round().astype(int)
    return "".join(blocks[i] for i in idx)


def main():
    col = "probe1_temp"
    window_min = None
    if "--probe" in sys.argv:
        col = f"probe{int(sys.argv[sys.argv.index('--probe') + 1])}_temp"
    if "--col" in sys.argv:
        col = sys.argv[sys.argv.index("--col") + 1]
        if col == "probe":
            col = "probe1_temp"
    if "--window" in sys.argv:
        window_min = float(sys.argv[sys.argv.index("--window") + 1])

    ts, val, target = load(col)
    t0 = ts[0]
    mins = np.array([(t - t0).total_seconds() / 60.0 for t in ts])

    if window_min is not None:
        keep = mins >= (mins[-1] - window_min)
        mins, val = mins[keep], val[keep]
        if len(val) < 2:
            sys.exit("Not enough points in that window.")

    span = mins[-1] - mins[0]
    fc = forecast(mins, val, target)      # recent-window, stall-aware prediction
    rate = fc["rate"]

    print(f"=== {col} trend ===")
    print(f"points:   {len(val)}  over {span:.1f} min")
    print(f"current:  {val[-1]:.0f}°   (min {val.min():.0f}°, max {val.max():.0f}°)")
    print(f"trend:    {sparkline(val)}")
    if rate is not None:
        print(f"rate:     {rate:+.2f} °/min   ({rate*60:+.0f} °/hr, recent)")

    # stage-aware if a plan exists for this probe (--stage overrides .cook_plan.json)
    stage_specs = [sys.argv[i + 1] for i, a in enumerate(sys.argv)
                   if a == "--stage" and i + 1 < len(sys.argv)]
    stages = plan.build_plan(stage_specs) or plan.load_plan()
    probe_idx = (int(col.replace("probe", "").replace("_temp", ""))
                 if col.startswith("probe") and col.endswith("_temp") else None)
    if probe_idx and stages.get(probe_idx):
        print(f"plan:     {describe_stages(forecast_stages(mins, val, stages[probe_idx]), now=ts[-1])}")
    elif target:
        print(f"done:     {describe(fc, target, now=ts[-1])}")


if __name__ == "__main__":
    main()
