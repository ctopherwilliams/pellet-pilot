#!/usr/bin/env python3
"""Browse and summarize past cooks recorded in cook_log.csv.

A "session" is a continuous run of readings; a new session begins when the gap
between consecutive readings exceeds --gap minutes (default 20).

Usage:
  history.py                    # same as `list`
  history.py list               # one row per past cook
  history.py show <id>          # detail + probe trend for one cook
  history.py summary            # aggregate stats across all cooks
  history.py list --gap 30      # custom session gap (minutes)
  history.py note <id> --cut "pork butt" --weight 8.5 --on-grill "8:15 AM" \
      --verdict amazing --notes "hot and fast, 276 avg pit temp"
      # attach manual notes -- see cook_notes.py. --on-grill corrects the
      # start time when sensor logging began after the meat actually went on.
"""
import csv
import datetime as dt
import os
import sys

import numpy as np

import cook_notes
import plan
from poll import MAX_PROBES
from trend import sparkline

LOG = os.path.join(os.path.dirname(__file__), "cook_log.csv")


def _num(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(path=LOG):
    if not os.path.exists(path):
        sys.exit(f"No log at {path}. Run poll.py --watch first.")
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = dt.datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows


def sessions(rows, gap_min=20.0):
    """Split time-ordered rows into sessions on gaps > gap_min minutes."""
    if not rows:
        return []
    gap = dt.timedelta(minutes=gap_min)
    groups, cur = [], [rows[0]]
    for prev, r in zip(rows, rows[1:]):
        if r["_ts"] - prev["_ts"] > gap:
            groups.append(cur)
            cur = []
        cur.append(r)
    groups.append(cur)
    return groups


def summarize(sess):
    start, end = sess[0]["_ts"], sess[-1]["_ts"]
    grills = [g for g in (_num(r.get("grill")) for r in sess) if g is not None]
    probes = {}
    for i in range(1, MAX_PROBES + 1):
        pairs = [(_num(r.get(f"probe{i}_temp")), _num(r.get(f"probe{i}_set"))) for r in sess]
        temps = [t for t, _ in pairs if t is not None]
        if not temps:
            continue
        target = next((s for _, s in pairs if s), None)
        probes[i] = {
            "start": temps[0], "final": temps[-1], "peak": max(temps),
            "target": target,
            "reached": bool(target) and max(temps) >= target,
            "temps": temps,
        }
    return {
        "start": start, "end": end,
        "duration_min": (end - start).total_seconds() / 60,
        "thing": sess[0].get("thing"),
        "readings": len(sess),
        "max_grill": max(grills) if grills else None,
        "probes": probes,
    }


def session_key(sess):
    """Stable key tying a manual note (cook_notes.py) to this session -- the
    session's own logged start timestamp. That logged start can itself be
    later than when the meat actually went on (that's what --on-grill
    corrects for), but it's still a unique, stable key per cook."""
    return sess[0]["_ts"].isoformat()


def stage_hits(sess, probe, stages):
    """[(label, temp, datetime|None)] -- when each stage was first reached.

    Uses a spike-cleaned temperature series (plot.py's clean_and_events, the
    same logic the chart already relies on) so a brief probe-reinsertion
    spike -- e.g. the probe reading grill-ambient air for a few ticks right
    after being pulled out for a wrap, before settling back into the meat --
    isn't mistaken for the real crossing. A naive "first reading >= stemp"
    check would report the stage reached the moment that transient spike
    passes through the threshold, which can be hours before the meat
    genuinely got there.
    """
    stagelist = stages.get(probe)
    if not stagelist:
        return []
    import plot  # local import: plot.py imports from this module, so a
                 # top-level import here would be circular (same pattern
                 # poll.py already uses for its own plot import)
    t0 = sess[0]["_ts"]
    xs = [(r["_ts"] - t0).total_seconds() / 60 for r in sess]
    temps = [_num(r.get(f"probe{probe}_temp")) for r in sess]
    cx, cy, _ = plot.clean_and_events(xs, temps)
    hits = []
    for stemp, label in stagelist:
        hit_x = next((x for x, y in zip(cx, cy) if y >= stemp), None)
        hit = (t0 + dt.timedelta(minutes=hit_x)) if hit_x is not None else None
        hits.append((label, stemp, hit))
    return hits


def cmd_list(groups):
    print(f"{'#':>2}  {'date':<14}  {'dur':>5}  {'grill':>6}  probes")
    for i, sess in enumerate(groups, 1):
        s = summarize(sess)
        pdesc = ", ".join(
            f"P{k} {int(v['start'])}→{int(v['final'])}°" + ("✅" if v["reached"] else "")
            for k, v in s["probes"].items()) or "—"
        print(f"{i:>2}  {s['start'].strftime('%m-%d %H:%M'):<14}  "
              f"{s['duration_min']:>4.0f}m  {int(s['max_grill'] or 0):>5}°  {pdesc}")


def cmd_show(groups, idx):
    if idx < 1 or idx > len(groups):
        sys.exit(f"No cook #{idx}; have 1..{len(groups)}")
    sess = groups[idx - 1]
    s = summarize(sess)
    note = cook_notes.get_note(session_key(sess)) or {}

    header = f"=== cook #{idx}"
    if note.get("cut"):
        header += f" — {note['cut']}"
        if note.get("weight_lb"):
            header += f" ({note['weight_lb']:g} lb)"
    print(header + " ===")

    on_grill = note.get("on_grill")
    if on_grill:
        on_grill_dt = dt.datetime.fromisoformat(on_grill)
        total_min = (s["end"] - on_grill_dt).total_seconds() / 60
        print(f"on the grill: {on_grill_dt:%Y-%m-%d %-I:%M %p} → {s['end']:%-I:%M %p}  "
              f"({total_min:.0f} min total -- logging started {s['start']:%-I:%M %p})")
    else:
        print(f"{s['start']:%Y-%m-%d %H:%M} → {s['end']:%H:%M}  "
              f"({s['duration_min']:.0f} min, {s['readings']} readings)")
    print(f"grill peak: {int(s['max_grill'] or 0)}°   thing: {s['thing']}")
    if note.get("verdict"):
        print(f"verdict: {note['verdict']}")
    if note.get("notes"):
        print(f"notes: {note['notes']}")

    cook_plan = plan.load_plan()
    t0 = sess[0]["_ts"]
    for k, v in s["probes"].items():
        arr = np.array(v["temps"], dtype=float)
        xm = np.array([(r["_ts"] - t0).total_seconds() / 60
                       for r in sess if _num(r.get(f"probe{k}_temp")) is not None])
        rate = f"{np.polyfit(xm, arr, 1)[0]:+.2f}°/min" if len(arr) >= 2 else "n/a"
        tgt = (f"target {int(v['target'])}° "
               + ("reached ✅" if v["reached"] else "not reached")) if v["target"] else "no target"
        print(f"P{k}: {int(v['start'])}→{int(v['final'])}° (peak {int(v['peak'])}°)  {rate}  {tgt}")
        print(f"     {sparkline(arr)}")
        for label, stemp, hit in stage_hits(sess, k, cook_plan):
            when = hit.strftime("%-I:%M %p") if hit else "not reached"
            print(f"     • {label} {int(stemp)}° reached {when}")


def _parse_time_of_day(spec, on_date):
    """Parse "8:15 AM" or "08:15" and combine with on_date (a date, not a
    datetime) -- --on-grill corrects the start TIME of a cook already in the
    log, not an arbitrary date."""
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            t = dt.datetime.strptime(spec.strip(), fmt).time()
            return dt.datetime.combine(on_date, t)
        except ValueError:
            continue
    raise ValueError(f'Could not parse time {spec!r} -- try "8:15 AM" or "08:15"')


def cmd_note(groups, idx, argv):
    if idx < 1 or idx > len(groups):
        sys.exit(f"No cook #{idx}; have 1..{len(groups)}")
    sess = groups[idx - 1]

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None

    fields = {}
    if opt("--cut"):
        fields["cut"] = opt("--cut")
    if opt("--weight"):
        fields["weight_lb"] = float(opt("--weight"))
    if opt("--on-grill"):
        fields["on_grill"] = _parse_time_of_day(opt("--on-grill"), sess[0]["_ts"].date()).isoformat()
    if opt("--verdict"):
        fields["verdict"] = opt("--verdict")
    if opt("--notes"):
        fields["notes"] = opt("--notes")
    if not fields:
        sys.exit("Nothing to save -- pass at least one of "
                  "--cut/--weight/--on-grill/--verdict/--notes")

    note = cook_notes.save_note(session_key(sess), **fields)
    print(f"Saved note for cook #{idx}: {note}")


def cmd_summary(groups):
    total_min = sum((s[-1]["_ts"] - s[0]["_ts"]).total_seconds() / 60 for s in groups)
    print(f"cooks:            {len(groups)}")
    print(f"total cook time:  {total_min / 60:.1f} h")
    print(f"readings logged:  {sum(len(s) for s in groups)}")


def main():
    argv = sys.argv[1:]
    gap = 20.0
    if "--gap" in argv:
        gi = argv.index("--gap")
        gap = float(argv[gi + 1])
        del argv[gi:gi + 2]
    cmd = argv[0] if argv else "list"
    groups = sessions(load_rows(), gap)
    if not groups:
        sys.exit("No cook sessions found in the log yet.")
    if cmd == "list":
        cmd_list(groups)
    elif cmd == "show":
        cmd_show(groups, int(argv[1]))
    elif cmd == "note":
        cmd_note(groups, int(argv[1]), argv[2:])
    elif cmd == "summary":
        cmd_summary(groups)
    else:
        sys.exit(f"Unknown command '{cmd}'. Use: list | show <id> | note <id> [...] | summary")


if __name__ == "__main__":
    main()
