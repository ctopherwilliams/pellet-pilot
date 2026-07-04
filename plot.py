#!/usr/bin/env python3
"""Render a cook as a chart. Dependency-free SVG by default; interactive HTML
(native hover tooltips) with --html; optional PNG with --png (needs matplotlib).

Usage:
  plot.py                       # latest cook -> cook.svg
  plot.py --cook 2 --out c.svg  # a specific session (see history.py list)
  plot.py --probe 1             # just probe 1 (default: grill + all probes)
  plot.py --html --out c.html   # self-contained interactive HTML
  plot.py --png --out c.png     # PNG (pip install -r requirements-plot.txt)
"""
import html as _html
import os
import sys

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


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    only_probe = int(opt("--probe")) if "--probe" in argv else None
    cook_id = int(opt("--cook")) if "--cook" in argv else None
    groups = sessions(load_rows())
    if not groups:
        sys.exit("No cook sessions found yet.")
    sess = groups[cook_id - 1] if cook_id else groups[-1]

    if "--png" in argv:
        out = opt("--out", "cook.png")
        render_png(sess, out, only_probe)
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
