#!/usr/bin/env python3
"""Render a cook as a chart. Dependency-free SVG by default; interactive HTML
(native hover tooltips) with --html; optional PNG with --png (needs matplotlib).

Usage:
  plot.py                       # latest cook -> cook.svg
  plot.py --cook 2 --out c.svg  # a specific session (see history.py list)
  plot.py --probe 1             # meat-probe forecast chart: auto-detects the
                                #   pull/wrap, draws stage lines + a projected finish
  plot.py --html --out c.html   # self-contained interactive HTML
  plot.py --png --out c.png     # PNG (pip install -r requirements-plot.txt)
"""
import datetime as dt
import html as _html
import os
import sys

import numpy as np

import plan
from history import load_rows, sessions, _num
from poll import MAX_PROBES

W, H = 900, 460
ML, MR, MT, MB = 60, 160, 30, 50
COLORS = {"grill": "#e8461e", "P1": "#ff8c1a", "P2": "#ffd166",
          "P3": "#4aa3ff", "P4": "#7ee081"}


def _series(sess, only_probe=None):
    t0 = sess[0]["_ts"]
    xs = [(r["_ts"] - t0).total_seconds() / 60 for r in sess]
    series = {}
    if only_probe is None:
        grill = [_num(r.get("grill")) for r in sess]
        if any(v is not None for v in grill):
            series["grill"] = ("Grill", grill, None)
    for i in range(1, MAX_PROBES + 1):
        if only_probe is not None and i != only_probe:
            continue
        temps = [_num(r.get(f"probe{i}_temp")) for r in sess]
        if any(v is not None for v in temps):
            target = next((_num(r.get(f"probe{i}_set")) for r in sess
                           if _num(r.get(f"probe{i}_set"))), None)
            series[f"P{i}"] = (f"Probe {i}", temps, target)
    return xs, series


def render_svg(sess, only_probe=None):
    xs, series = _series(sess, only_probe)
    if not series:
        sys.exit("Nothing to plot for that selection.")
    vals = [v for _, vv, _ in series.values() for v in vv if v is not None]
    targets = [t for _, _, t in series.values() if t]
    lo, hi = min(vals + targets), max(vals + targets)
    pad = max(5.0, (hi - lo) * 0.1)
    lo, hi = lo - pad, hi + pad
    xmax = max(xs) or 1.0

    def X(m):
        return ML + (m / xmax) * (W - ML - MR)

    def Y(v):
        return H - MB - (v - lo) / (hi - lo) * (H - MT - MB)

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="ui-sans-serif,Helvetica,Arial,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" rx="10" fill="#161310"/>')
    for k in range(5):  # y grid + labels
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        p.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{W-MR}" y2="{y:.1f}" stroke="#2c2620"/>')
        p.append(f'<text x="{ML-8}" y="{y+4:.1f}" fill="#8a7d70" font-size="12" '
                 f'text-anchor="end">{v:.0f}°</text>')
    for k in range(5):  # x labels
        m = xmax * k / 4
        p.append(f'<text x="{X(m):.1f}" y="{H-MB+18}" fill="#8a7d70" font-size="12" '
                 f'text-anchor="middle">{m:.0f}m</text>')
    for key, (label, vv, target) in series.items():
        col = COLORS.get(key, "#ffffff")
        if target and lo <= target <= hi:
            p.append(f'<line x1="{ML}" y1="{Y(target):.1f}" x2="{W-MR}" y2="{Y(target):.1f}" '
                     f'stroke="{col}" stroke-dasharray="4 4" opacity="0.5"/>')
        pts = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, vv) if v is not None)
        p.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2"/>')
        for x, v in zip(xs, vv):  # hover points (native <title> tooltip)
            if v is None:
                continue
            p.append(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="3" fill="{col}" '
                     f'opacity="0.01"><title>{label}: {v:.0f}° @ {x:.0f} min</title></circle>')
    ly = MT + 12
    for key, (label, vv, target) in series.items():
        col = COLORS.get(key, "#ffffff")
        cur = next((v for v in reversed(vv) if v is not None), None)
        p.append(f'<rect x="{W-MR+10}" y="{ly-9}" width="12" height="12" rx="2" fill="{col}"/>')
        p.append(f'<text x="{W-MR+28}" y="{ly+2}" fill="#d8cbbf" font-size="13">'
                 f'{label} {int(cur) if cur is not None else "?"}°</text>')
        ly += 24
    p.append("</svg>")
    return "\n".join(p)


def html_wrap(svg, title):
    return (f"<!doctype html><meta charset=utf-8><title>{_html.escape(title)}</title>"
            "<body style='margin:0;background:#0d0b09;display:flex;justify-content:center'>"
            f"<div style='max-width:960px;width:100%'>{svg}</div>")


def render_png(sess, out, only_probe=None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("PNG needs matplotlib:  pip install -r requirements-plot.txt")
    xs, series = _series(sess, only_probe)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for key, (label, vv, target) in series.items():
        pts = [(x, v) for x, v in zip(xs, vv) if v is not None]
        ax.plot([x for x, _ in pts], [v for _, v in pts], label=label,
                color=COLORS.get(key))
        if target:
            ax.axhline(target, ls="--", lw=1, color=COLORS.get(key), alpha=0.5)
    ax.set_xlabel("minutes")
    ax.set_ylabel("°F")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=120)


def clean_and_events(xs, temps, jump=15.0):
    """Drop probe-out spikes (probe pulled from the meat) and record where they start.

    A rise of >jump above the last kept temp marks a 'pull'; readings stay dropped
    until the temp settles back near the pre-spike value. Returns (xs, temps, event_xs).
    """
    cx, cy, events = [], [], []
    last = None
    i, n = 0, len(temps)
    while i < n:
        v = temps[i]
        if v is None:
            i += 1
            continue
        if last is not None and v - last > jump:
            events.append(xs[i - 1] if i > 0 else xs[i])
            while i < n and temps[i] is not None and temps[i] > last + jump * 0.5:
                i += 1
            continue
        cx.append(xs[i])
        cy.append(v)
        last = v
        i += 1
    return cx, cy, events


def project(cx, cy, target, window=20.0, early=60.0):
    """Return (rate, eta_min_from_last) toward target.

    Uses the recent-window rate; if flat/negative (e.g. a stall), falls back to the
    early-cook climb rate so a projection can still be drawn. eta_min is None if no
    positive rate can be found.
    """
    if len(cy) < 2 or not target:
        return None, None
    x, y = np.array(cx, float), np.array(cy, float)
    cur = y[-1]
    if cur >= target:
        return 0.0, 0.0

    def rate(mask):
        return float(np.polyfit(x[mask], y[mask], 1)[0]) if mask.sum() >= 2 else None

    r = rate(x >= x[-1] - window)
    if r is None or r < 0.05:
        r = rate(x <= x[0] + early)  # healthy early climb as the expected resumed rate
    if not r or r < 0.05:
        return r, None
    return r, (target - cur) / r


def render_forecast_svg(sess, probe, stages=None):
    """Meat-probe chart with pull/wrap markers, target line, and a projected finish."""
    stages = stages or {}
    t0 = sess[0]["_ts"]
    xs = [(r["_ts"] - t0).total_seconds() / 60 for r in sess]
    cx, cy, events = clean_and_events(xs, [_num(r.get(f"probe{probe}_temp")) for r in sess])
    if len(cy) < 2:
        sys.exit(f"Not enough probe {probe} data to plot.")

    plist = sorted(stages.get(probe, []))
    if plist:
        target, label = plist[-1]          # project to the final stage ("done")
        stage_lines = plist
    else:
        target = next((_num(r.get(f"probe{probe}_set")) for r in sess
                       if _num(r.get(f"probe{probe}_set"))), None)
        label = "done"
        stage_lines = [(target, label)] if target else []

    rate, eta = project(cx, cy, target)
    proj = None
    if target and eta:
        ex = cx[-1] + eta
        proj = (ex, target, (t0 + dt.timedelta(minutes=ex)).strftime("%-I:%M %p"))

    def clock(m):
        return (t0 + dt.timedelta(minutes=m)).strftime("%-I:%M")

    xmax = (proj[0] if proj else cx[-1]) or 1.0
    vals = cy + ([target] if target else [])
    lo, hi = min(vals), max(vals)
    pad = max(5.0, (hi - lo) * 0.08)
    lo, hi = lo - pad, hi + pad

    def X(m):
        return ML + (m / xmax) * (W - ML - MR)

    def Y(v):
        return H - MB - (v - lo) / (hi - lo) * (H - MT - MB)

    meat, tgt, evc = "#ff8c1a", "#5dcaa5", "#8a8079"
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="ui-sans-serif,Helvetica,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" rx="10" fill="#161310"/>']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        p.append(f'<line x1="{ML}" y1="{Y(v):.1f}" x2="{W-MR}" y2="{Y(v):.1f}" stroke="#2c2620"/>')
        p.append(f'<text x="{ML-8}" y="{Y(v)+4:.1f}" fill="#8a7d70" font-size="12" text-anchor="end">{v:.0f}°</text>')
    for k in range(5):
        m = xmax * k / 4
        p.append(f'<text x="{X(m):.1f}" y="{H-MB+18}" fill="#8a7d70" font-size="12" text-anchor="middle">{clock(m)}</text>')
    for st, slbl in stage_lines:
        if lo <= st <= hi:
            p.append(f'<line x1="{ML}" y1="{Y(st):.1f}" x2="{W-MR}" y2="{Y(st):.1f}" stroke="{tgt}" stroke-dasharray="5 5" opacity="0.7"/>')
            p.append(f'<text x="{ML+4}" y="{Y(st)-6:.1f}" fill="{tgt}" font-size="12">{slbl} {int(st)}°</text>')
    for ex in events:
        p.append(f'<line x1="{X(ex):.1f}" y1="{MT}" x2="{X(ex):.1f}" y2="{H-MB}" stroke="{evc}" stroke-dasharray="4 4"/>')
        p.append(f'<text x="{X(ex)+5:.1f}" y="{MT+12}" fill="#c9b8a8" font-size="12">pulled/wrapped · {clock(ex)}</text>')
    pts = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(cx, cy))
    p.append(f'<polyline points="{pts}" fill="none" stroke="{meat}" stroke-width="2"/>')
    for x, v in zip(cx, cy):
        p.append(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="3" fill="{meat}" opacity="0.01">'
                 f'<title>{v:.0f}° @ {clock(x)}</title></circle>')
    if proj:
        ex, ey, eclock = proj
        p.append(f'<polyline points="{X(cx[-1]):.1f},{Y(cy[-1]):.1f} {X(ex):.1f},{Y(ey):.1f}" '
                 f'fill="none" stroke="{meat}" stroke-width="2" stroke-dasharray="6 6"/>')
        p.append(f'<circle cx="{X(ex):.1f}" cy="{Y(ey):.1f}" r="4" fill="{tgt}"/>')
        p.append(f'<text x="{X(ex)-6:.1f}" y="{Y(ey)-10:.1f}" fill="{tgt}" font-size="13" text-anchor="end">done ~{eclock} (est)</text>')
    p.append(f'<circle cx="{X(cx[-1]):.1f}" cy="{Y(cy[-1]):.1f}" r="4" fill="{meat}"/>')
    p.append(f'<text x="{X(cx[-1]):.1f}" y="{Y(cy[-1])+20:.1f}" fill="{meat}" font-size="12" text-anchor="middle">{int(cy[-1])}° now</text>')
    p.append("</svg>")
    return "\n".join(p)


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    only_probe = int(opt("--probe")) if "--probe" in argv else None
    cook_id = int(opt("--cook")) if "--cook" in argv else None
    if only_probe is not None and cook_id is None:
        sess = load_rows()  # forecast chart spans the whole log (one continuous cook)
        if len(sess) < 2:
            sys.exit("No cook data yet.")
    else:
        groups = sessions(load_rows())
        if not groups:
            sys.exit("No cook sessions found yet.")
        sess = groups[cook_id - 1] if cook_id else groups[-1]

    if "--png" in argv:
        out = opt("--out", "cook.png")
        render_png(sess, out, only_probe)
    else:
        if only_probe is not None:  # single meat probe -> annotated forecast chart
            svg = render_forecast_svg(sess, only_probe, plan.load_plan())
        else:
            svg = render_svg(sess, only_probe)
        if "--html" in argv:
            out = opt("--out", "cook.html")
            content = html_wrap(svg, "Pellet Pilot cook")
        else:
            out = opt("--out", "cook.svg")
            content = svg
        with open(out, "w") as f:
            f.write(content)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
