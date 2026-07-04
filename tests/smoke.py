"""Smoke + regression suite — runs in CI before any PR can merge.

Exercises the parts static analysis can't see: imports, status parsing, the paho
MQTT client construction, multi-probe logging, history segmentation, plotting,
Grafana export, and the remote-alarm SSRF guard. A dependency bump or refactor
that breaks any of this fails here (branch protection) instead of at runtime.
No network required (SSRF checks use IP literals).
"""
import datetime as dt
import os
import sys
import xml.dom.minidom as minidom

# Make the repo root importable regardless of how this file is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt  # noqa: E402

import alarms  # noqa: E402
import export  # noqa: E402
import forecast as fc_mod  # noqa: E402
import history  # noqa: E402
import plan  # noqa: E402
import plot  # noqa: E402
import poll  # noqa: E402,F401
import traeger_client as tc  # noqa: E402
import trend  # noqa: E402,F401


def _doc(temps=((150, 203),), grill=225):
    acc = [{"type": "probe", "con": 1, "uuid": f"p{i}",
            "probe": {"get_temp": t, "set_temp": s, "alarm_fired": 0}}
           for i, (t, s) in enumerate(temps)]
    return {"status": {"grill": grill, "set": 225, "ambient": 70, "system_status": 6,
                       "connected": True, "units": 1, "acc": acc}}


def _rows(start, n, p1=140, p1_set=203, p2=None):
    out = []
    for i in range(n):
        t = start + dt.timedelta(minutes=2 * i)
        r = {"ts": t.isoformat(), "_ts": t, "thing": "g", "grill": "250", "set": "250",
             "ambient": "70", "system_status": "6",
             "probe1_temp": str(p1 + i), "probe1_set": str(p1_set),
             "probe1_connected": "True", "probe1_alarm": "0"}
        if p2 is not None:
            r.update(probe2_temp=str(p2 + 2 * i), probe2_set="165",
                     probe2_connected="True", probe2_alarm="0")
        out.append(r)
    return out


def test_parse_status():
    r = tc.parse_status("g", _doc())
    assert r["grill"] == 225 and r["units"] == "F", r
    assert r["probes"][0]["get_temp"] == 150 and r["probes"][0]["set_temp"] == 203, r


def test_mqtt_client_builds():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
    c.tls_set_context(tc._mqtt_tls_context("example.com"))
    c.ws_set_options(path="/mqtt?x=1", headers={"Host": "example.com"})


def test_status_decode():
    assert poll.decode_status(99, True, 277) == "Running"
    assert poll.decode_status(99, False, 0) == "Offline"


def test_row_from_multiprobe():
    row = poll.row_from(tc.parse_status("g", _doc(temps=((150, 203), (120, 165)))))
    assert len(poll.FIELDS) == 6 + 4 * 4, poll.FIELDS
    assert row["probe1_temp"] == 150 and row["probe2_temp"] == 120, row
    assert row["probe3_temp"] is None, row


def test_alarm_latch():
    poll._fired.clear()
    fired = []
    orig, poll.notify = poll.notify, lambda t, m: fired.append(m)
    orig_r, poll.notify_remote = poll.notify_remote, lambda t, m: None
    try:
        row = {"probe1_temp": 150, "probe2_temp": 120}
        poll.check_alarms(row, {1: [145], 2: [118]})
        poll.check_alarms(row, {1: [145]})  # no repeat
    finally:
        poll.notify, poll.notify_remote = orig, orig_r
    assert sum("145" in m for m in fired) == 1, fired
    assert any("Probe 2" in m for m in fired), fired


def test_history_sessions():
    rows = _rows(dt.datetime(2026, 7, 3, 8, 0), 10, p2=100) + \
        _rows(dt.datetime(2026, 7, 3, 14, 0), 5)  # 6h gap -> 2 sessions
    groups = history.sessions(rows, gap_min=20)
    assert len(groups) == 2, len(groups)
    s = history.summarize(groups[0])
    assert 1 in s["probes"] and 2 in s["probes"], s["probes"]


def test_plot_svg():
    svg = plot.render_svg(_rows(dt.datetime(2026, 7, 4, 9, 0), 12, p2=100))
    minidom.parseString(svg)  # well-formed XML
    assert svg.count("<polyline") >= 3, "grill + 2 probes expected"


def test_export_influx():
    lp = export.to_influx(_rows(dt.datetime(2026, 7, 4, 9, 0), 3))
    assert lp.startswith("pellet_pilot,thing=g "), lp[:40]
    assert "probe1=140.0" in lp and "grill=250.0" in lp, lp


def test_forecast():
    on = fc_mod.forecast(list(range(10)), [150.0 + i for i in range(10)], 180)
    assert on["status"] == "on_track" and on["eta_min"] > 0, on
    assert "min to 180" in fc_mod.describe(on, 180)
    # recent window: a fast early rise that then flattens must NOT project a soon-ETA
    flat = fc_mod.forecast(list(range(60)), [130 + min(i, 20) for i in range(60)], 165)
    assert flat["status"] in ("stalled", "not_rising"), flat
    assert fc_mod.forecast([0, 1], [165, 166], 165)["status"] == "done"
    assert fc_mod.forecast([0], [150], 165)["status"] == "insufficient"


def test_stages():
    p = plan.build_plan(["203:done", "165:wrap", "2:170"])
    assert p[1] == [(165.0, "wrap"), (203.0, "done")], p
    assert p[2][0][1] == "done", p  # single unlabeled stage -> "done"
    assert plan.parse_stage("2:170:wrap") == (2, 170.0, "wrap")
    # stage-aware forecast advances past a crossed stage (current 169 > wrap 165)
    fcs = fc_mod.forecast_stages(list(range(10)), [160.0 + i for i in range(10)],
                                 [(165, "wrap"), (203, "done")])
    assert fcs["next"]["label"] == "done", fcs
    # a labeled stage crossing fires once
    poll._fired.clear()
    fired = []
    on, poll.notify = poll.notify, lambda t, m: fired.append(m)
    onr, poll.notify_remote = poll.notify_remote, lambda t, m: None
    try:
        poll.check_stage_alarms({"probe1_temp": 166}, {1: [(165.0, "wrap")]})
        poll.check_stage_alarms({"probe1_temp": 170}, {1: [(165.0, "wrap")]})
    finally:
        poll.notify, poll.notify_remote = on, onr
    assert sum("WRAP IT" in m for m in fired) == 1, fired


def test_forecast_chart():
    from plot import clean_and_events, project, render_forecast_svg
    xs = [float(i) for i in range(10)]
    temps = [150.0, 152, 231, 220, 154, 156, 158, 160, 162, 164]  # probe-out spike
    cx, cy, ev = clean_and_events(xs, temps)
    assert ev and max(cy) < 175, (ev, cy)  # spike detected + removed
    rate, eta = project([0, 1, 2, 3, 4], [150.0, 152, 154, 156, 158], 205)
    assert rate > 0 and eta > 0, (rate, eta)
    sess = [{"_ts": dt.datetime(2026, 7, 4, 10) + dt.timedelta(minutes=2 * i),
             "probe1_temp": str(150 + i), "probe1_set": "205"} for i in range(12)]
    svg = render_forecast_svg(sess, 1, {1: [(165.0, "wrap"), (205.0, "done")]})
    minidom.parseString(svg)
    assert "done ~" in svg and "wrap 165" in svg and "pulled/wrapped" not in svg, svg[:200]


def test_ssrf_guard():
    ok = alarms.assert_safe_url("https://8.8.8.8/hook")  # public HTTPS
    assert ok is True
    for bad in ("http://8.8.8.8", "https://127.0.0.1", "https://169.254.169.254/latest",
                "https://10.1.2.3", "https://[::1]/x"):
        try:
            alarms.assert_safe_url(bad)
            raise AssertionError(f"should have blocked {bad}")
        except alarms.UnsafeURL:
            pass


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print("smoke:", "PASS" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
