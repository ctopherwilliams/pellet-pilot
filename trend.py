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
            st = r.get("probe1_set") if col == "probe1_temp" else r.get("set")
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

    # linear fit: temp = slope * minutes + intercept
    slope, intercept = np.polyfit(mins, val, 1)
    span = mins[-1] - mins[0]

    print(f"=== {col} trend ===")
    print(f"points:   {len(val)}  over {span:.1f} min")
    print(f"current:  {val[-1]:.0f}°   (min {val.min():.0f}°, max {val.max():.0f}°)")
    print(f"trend:    {sparkline(val)}")
    print(f"rate:     {slope:+.2f} °/min   ({slope*60:+.0f} °/hr)")

    if target and target > 0:
        remaining = target - val[-1]
        if slope > 0.01 and remaining > 0:
            eta_min = remaining / slope
            eta_clock = (ts[-1] + dt.timedelta(minutes=eta_min)).strftime("%-I:%M %p")
            print(f"target:   {target:.0f}°  ->  ~{eta_min:.0f} min away (≈ {eta_clock})")
        elif remaining <= 0:
            print(f"target:   {target:.0f}°  ->  reached ✅")
        else:
            print(f"target:   {target:.0f}°  ->  not rising; no ETA")

    # stall detector for probes (the classic brisket/pork-shoulder plateau)
    if col == "probe1_temp" and span >= 15 and abs(slope) < 0.3 and 150 <= val[-1] <= 175:
        print("note:     flat rise in the 150–170° band — likely the stall. Normal; ride it out or wrap.")


if __name__ == "__main__":
    main()
