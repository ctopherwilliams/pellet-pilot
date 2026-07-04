#!/usr/bin/env python3
"""Export cook_log.csv to Grafana-ingestible formats. Local file output only —
no network — except the optional --serve endpoint, which binds 127.0.0.1 only.

Usage:
  export.py --format influx --out cook.lp     # InfluxDB line protocol
  export.py --format json   --out cook.json   # JSON time-series
  export.py --serve [--port 9109]             # localhost Prometheus /metrics
"""
import json
import sys

from history import load_rows, _num
from poll import MAX_PROBES

MEASUREMENT = "pellet_pilot"


def _fields(r):
    fields = {}
    for k in ("grill", "set", "ambient"):
        v = _num(r.get(k))
        if v is not None:
            fields[k] = v
    for i in range(1, MAX_PROBES + 1):
        v = _num(r.get(f"probe{i}_temp"))
        s = _num(r.get(f"probe{i}_set"))
        if v is not None:
            fields[f"probe{i}"] = v
        if s:
            fields[f"probe{i}_target"] = s
    return fields


def to_influx(rows):
    """InfluxDB line protocol. Timestamps in ns (naive ts treated as local)."""
    out = []
    for r in rows:
        fields = _fields(r)
        if not fields:
            continue
        thing = str(r.get("thing") or "unknown").replace(" ", "_").replace(",", "_")
        ts = int(r["_ts"].timestamp() * 1_000_000_000)
        fstr = ",".join(f"{k}={v}" for k, v in fields.items())
        out.append(f"{MEASUREMENT},thing={thing} {fstr} {ts}")
    return "\n".join(out) + "\n"


def to_json(rows):
    out = []
    for r in rows:
        d = {"time": r["_ts"].isoformat(), "thing": r.get("thing")}
        d.update(_fields(r))
        out.append(d)
    return json.dumps(out, indent=2)


def to_prometheus(row):
    lines = [f"pellet_pilot_{k} {v}" for k, v in _fields(row).items()]
    return "\n".join(lines) + "\n"


def serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            rows = load_rows()
            body = to_prometheus(rows[-1]).encode() if rows else b""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    # Bound to loopback only — no external exposure, so no auth surface.
    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Prometheus metrics on http://127.0.0.1:{port}/metrics  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    if "--serve" in argv:
        serve(int(opt("--port", "9109")))
        return

    fmt = opt("--format", "influx")
    rows = load_rows()
    if fmt == "influx":
        content, default_out = to_influx(rows), "cook.lp"
    elif fmt == "json":
        content, default_out = to_json(rows), "cook.json"
    else:
        sys.exit("Unknown --format; use 'influx' or 'json'.")
    out = opt("--out", default_out)
    with open(out, "w") as f:
        f.write(content)
    print(f"wrote {out} ({len(rows)} readings)")


if __name__ == "__main__":
    main()
