"""Smoke + regression suite — runs in CI before any PR can merge.

Exercises the parts static analysis can't see: imports, status parsing, the paho
MQTT client construction, multi-probe logging, history segmentation, plotting,
Grafana export, the remote-alarm SSRF guard (+ DNS-pinning), refresh-token
re-auth, credential hygiene, and thingName validation. A dependency bump or
refactor that breaks any of this fails here (branch protection) instead of at
runtime. No network required (SSRF checks use IP literals; auth flows use
mocked HTTP responses).
"""
import contextlib
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import types
import xml.dom.minidom as minidom

# Make the repo root importable regardless of how this file is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt  # noqa: E402

import alarms  # noqa: E402
import cook_notes  # noqa: E402
import export  # noqa: E402
import forecast as fc_mod  # noqa: E402
import history  # noqa: E402
import pellet  # noqa: E402
import plan  # noqa: E402
import plot  # noqa: E402
import poll  # noqa: E402,F401
import presets  # noqa: E402
import probe_names  # noqa: E402
import report  # noqa: E402
import traeger_client as tc  # noqa: E402
import trend  # noqa: E402,F401
import wrap_coach  # noqa: E402


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


def test_parse_status_includes_pellet_and_temp_errors():
    doc = _doc()
    doc["status"]["pellet_level"] = 35
    doc["usage"] = {"error_stats": {"overheat": 2, "lowtemp": 0, "bad_thermocouple": 1}}
    r = tc.parse_status("g", doc)
    assert r["pellet_level"] == 35, r
    assert r["error_overheat"] == 2 and r["error_bad_thermocouple"] == 1, r

    # a grill with no "usage" block at all (or an older firmware without
    # error_stats) must not crash -- just come back as None
    r2 = tc.parse_status("g", _doc())
    assert r2["pellet_level"] is None and r2["error_overheat"] is None, r2


def test_mqtt_client_builds():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
    c.tls_set_context(tc._mqtt_tls_context("example.com"))
    c.ws_set_options(path="/mqtt?x=1", headers={"Host": "example.com"})


def test_status_decode():
    assert poll.decode_status(99, True, 277) == "Running"
    assert poll.decode_status(99, False, 0) == "Offline"


def test_row_from_multiprobe():
    row = poll.row_from(tc.parse_status("g", _doc(temps=((150, 203), (120, 165)))))
    assert len(poll.FIELDS) == len(poll._BASE_FIELDS) + 4 * 4, poll.FIELDS
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


def test_history_session_key_stable_and_unique():
    a = _rows(dt.datetime(2026, 7, 3, 8, 0), 5)
    b = _rows(dt.datetime(2026, 7, 4, 8, 0), 5)
    assert history.session_key(a) == history.session_key(a)  # stable
    assert history.session_key(a) != history.session_key(b)  # unique per cook


def test_stage_hits_ignores_probe_reinsertion_spike():
    # Real-world bug this guards against: pulling the probe to wrap it, then
    # reinserting, makes it briefly read grill-ambient air (a spike well
    # above the true meat temp) before settling back down. A naive "first
    # reading >= threshold" check reports the stage reached the moment that
    # transient spike crosses it -- hours before the meat actually got there.
    t0 = dt.datetime(2026, 7, 4, 14, 0)
    # climbs to 165 (wrap), spikes to 231 (probe-out artifact), settles back
    # to 165-170 (post-wrap reality), then genuinely climbs to 205 much later.
    temps = [160, 165, 192, 219, 226, 231, 213, 170, 167, 165, 165, 165] + \
        [165 + i for i in range(1, 41)]  # slow real climb: 166 -> 205 over 40 ticks
    rows = []
    for i, temp in enumerate(temps):
        t = t0 + dt.timedelta(minutes=i)
        rows.append({"ts": t.isoformat(), "_ts": t, "thing": "g", "grill": "270", "set": "275",
                     "ambient": "100", "system_status": "6",
                     "probe1_temp": str(temp), "probe1_set": "205",
                     "probe1_connected": "True", "probe1_alarm": "0"})
    stages = {1: [(165.0, "wrap"), (205.0, "done")]}
    hits = history.stage_hits(rows, 1, stages)
    wrap_hit = next(h for lbl, _, h in hits if lbl == "wrap")
    done_hit = next(h for lbl, _, h in hits if lbl == "done")
    # wrap genuinely was reached early (160->165 is a real climb, not a spike)
    assert wrap_hit == t0 + dt.timedelta(minutes=1), wrap_hit
    # done must NOT be the momentary spike at minute 3-5 -- it's the real,
    # sustained crossing much later in the slow climb back up to 205.
    assert done_hit is not None and done_hit >= t0 + dt.timedelta(minutes=40), done_hit


def test_cook_notes_save_load_merge_and_size_cap():
    path = "/tmp/.pellet_pilot_test_cook_notes.json"
    try:
        cook_notes.save_note("2026-07-04T11:13:09", path=path, cut="pork butt", weight_lb=8.5)
        note = cook_notes.get_note("2026-07-04T11:13:09", path=path)
        assert note == {"cut": "pork butt", "weight_lb": 8.5}, note

        # a later call merges in new fields without clobbering existing ones
        cook_notes.save_note("2026-07-04T11:13:09", path=path, verdict="amazing")
        note = cook_notes.get_note("2026-07-04T11:13:09", path=path)
        assert note["cut"] == "pork butt" and note["verdict"] == "amazing", note

        # unknown fields are ignored rather than silently stored
        cook_notes.save_note("2026-07-04T11:13:09", path=path, bogus_field="nope")
        note = cook_notes.get_note("2026-07-04T11:13:09", path=path)
        assert "bogus_field" not in note, note

        # size cap
        with open(path, "w") as f:
            f.write('{"x": "' + "y" * cook_notes._MAX_NOTES_FILE_BYTES + '"}')
        try:
            cook_notes.load_notes(path)
            assert False, "expected ValueError for an oversized notes file"
        except ValueError:
            pass
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_history_note_cli_and_show_display():
    path = "/tmp/.pellet_pilot_test_cook_notes_cli.json"
    orig_notes_file = cook_notes.NOTES_FILE
    cook_notes.NOTES_FILE = path
    try:
        rows = _rows(dt.datetime(2026, 7, 4, 8, 15), 10, p1=160, p1_set=205)
        groups = [rows]
        history.cmd_note(groups, 1, ["--cut", "pork butt", "--weight", "8.5",
                                      "--on-grill", "8:15 AM", "--verdict", "amazing"])
        note = cook_notes.get_note(history.session_key(rows), path=path)
        assert note["cut"] == "pork butt" and note["weight_lb"] == 8.5, note
        assert note["on_grill"].startswith("2026-07-04T08:15"), note

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            history.cmd_show(groups, 1)
        out = buf.getvalue()
        assert "pork butt" in out and "8.5 lb" in out, out
        assert "amazing" in out, out
    finally:
        cook_notes.NOTES_FILE = orig_notes_file
        if os.path.exists(path):
            os.remove(path)


def test_parse_time_of_day_accepts_common_formats():
    d = dt.date(2026, 7, 4)
    assert history._parse_time_of_day("8:15 AM", d) == dt.datetime(2026, 7, 4, 8, 15)
    assert history._parse_time_of_day("08:15", d) == dt.datetime(2026, 7, 4, 8, 15)
    try:
        history._parse_time_of_day("not a time", d)
        assert False, "expected ValueError for an unparseable time"
    except ValueError:
        pass


def test_plot_svg():
    svg = plot.render_svg(_rows(dt.datetime(2026, 7, 4, 9, 0), 12, p2=100))
    minidom.parseString(svg)  # well-formed XML
    assert svg.count("<polyline") >= 3, "grill + 2 probes expected"


def test_html_wrap_refresh_tag():
    # Default (no refresh) must be unchanged -- no meta-refresh tag at all.
    plain = plot.html_wrap("<svg></svg>", "t")
    assert "http-equiv" not in plain, plain
    # "Set it and forget it": refresh=N adds a self-reloading meta tag.
    refreshing = plot.html_wrap("<svg></svg>", "t", refresh=30)
    assert '<meta http-equiv="refresh" content="30">' in refreshing, refreshing


def test_write_chart_self_refreshing():
    # The "set it and forget it" chart: written atomically, self-refreshing,
    # valid SVG inside, stage lines present.
    rows = _rows(dt.datetime(2026, 7, 4, 9, 0), 15, p1=140, p1_set=205)
    orig_load_rows = plot.load_rows
    plot.load_rows = lambda: rows
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cook.html")
            ok = plot.write_chart(path, 1, {1: [(165.0, "wrap"), (205.0, "done")]}, refresh=15)
            assert ok is True
            content = open(path).read()
            assert '<meta http-equiv="refresh" content="15">' in content, content
            svg_start = content.index("<svg")
            minidom.parseString(content[svg_start:content.index("</svg>") + 6])
            assert "wrap 165" in content and "done 205" in content, content
            assert not os.path.exists(path + ".tmp"), "temp file must be cleaned up (atomic rename)"
    finally:
        plot.load_rows = orig_load_rows


def test_write_chart_insufficient_data_does_not_crash():
    # render_forecast_svg() used to sys.exit() on insufficient data -- fatal
    # if called every --watch tick. write_chart() must degrade to False instead.
    orig_load_rows = plot.load_rows
    plot.load_rows = lambda: _rows(dt.datetime(2026, 7, 4, 9, 0), 1)  # only 1 row
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cook.html")
            ok = plot.write_chart(path, 1, {})
            assert ok is False
            assert not os.path.exists(path)
    finally:
        plot.load_rows = orig_load_rows


def test_one_shot_chart_write_failure_does_not_crash_tick():
    # A chart-write error must never interrupt the cook log -- best-effort,
    # same philosophy as remote-alarm delivery.
    class _FakeTraeger:
        def poll(self):
            return {"g": {"status": {
                "grill": 250, "set": 250, "ambient": 70, "system_status": 6,
                "connected": True, "units": 1,
                "acc": [{"type": "probe", "con": 1, "uuid": "p1",
                         "probe": {"get_temp": 168, "set_temp": 205, "alarm_fired": 0}}],
            }}}

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    fake_plot = types.SimpleNamespace(write_chart=_boom)
    orig_module = sys.modules.get("plot")
    sys.modules["plot"] = fake_plot  # poll.one_shot() does a local `import plot`
    orig_append, poll.append = poll.append, lambda row: None
    poll._eta_samples.clear()
    try:
        poll.one_shot(_FakeTraeger(), chart_path="cook.html")  # must not raise
    finally:
        sys.modules["plot"] = orig_module
        poll.append = orig_append


def test_export_influx():
    lp = export.to_influx(_rows(dt.datetime(2026, 7, 4, 9, 0), 3))
    assert lp.startswith("pellet_pilot,thing=g "), lp[:40]
    assert "probe1=140.0" in lp and "grill=250.0" in lp, lp


def test_forecast():
    on = fc_mod.forecast(list(range(10)), [150.0 + i for i in range(10)], 180)
    assert on["status"] == "on_track" and on["eta_min"] > 0, on
    described = fc_mod.describe(on, 180, now=dt.datetime(2026, 7, 4, 13, 0))
    assert "min to 180" in described
    assert "≈" in described and ("AM" in described or "PM" in described), described
    # recent window: a fast early rise that then flattens must NOT project a soon-ETA
    flat = fc_mod.forecast(list(range(60)), [130 + min(i, 20) for i in range(60)], 165)
    assert flat["status"] in ("stalled", "not_rising"), flat
    assert fc_mod.forecast([0, 1], [165, 166], 165)["status"] == "done"
    assert fc_mod.forecast([0], [150], 165)["status"] == "insufficient"


def test_forecast_zero_variance_window():
    # Two (or more) samples landing at the identical timestamp (e.g. two ticks
    # in the same second) must degrade to "insufficient", not crash polyfit on
    # a singular/zero-variance x -- this reproduces a real LinAlgError seen
    # when --watch's live sample buffer picks up a same-second duplicate.
    fc = fc_mod.forecast([5.0, 5.0, 5.0], [160.0, 161.0, 162.0], 205)
    assert fc["status"] == "insufficient", fc
    assert fc["rate"] is None and fc["eta_min"] is None, fc


def test_describe_stages_includes_clock_for_both_next_and_final():
    # Every ETA must carry a clock time, not just minutes-remaining -- this
    # locks in a real bug where the "final stage" phrase (e.g. "then done 205
    # ~46 min") dropped the clock even though `now` was available.
    mins = list(range(20))
    temps = [140.0 + i for i in mins]  # +1 deg/min
    fcs = fc_mod.forecast_stages(mins, temps, [(165, "wrap"), (190, "mid"), (205, "done")])
    text = fc_mod.describe_stages(fcs, now=dt.datetime(2026, 7, 4, 13, 0))
    assert text.count("≈") == 2, text  # both "next:" and "then:" must show a clock
    assert "AM" in text or "PM" in text, text


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


def test_notify_remote_sanitizes_control_chars():
    # RT-NEW-1: control characters (incl. newlines) must be stripped from
    # title/message before any provider is called -- ntfy puts `title`
    # straight into an HTTP header, so this closes a header-injection primitive.
    captured = {}

    def fake_pushover(title, message):
        captured["title"], captured["message"] = title, message
        return True

    orig = alarms.pushover
    alarms.pushover = fake_pushover
    try:
        alarms.notify_remote("Ti\ntle\r", "Mess\r\nage\x07here")
        assert "\n" not in captured["title"] and "\r" not in captured["title"], captured
        assert "\n" not in captured["message"] and "\x07" not in captured["message"], captured
    finally:
        alarms.pushover = orig


def test_stage_alarm_sanitizes_end_to_end():
    # Integration: poll.check_stage_alarms's real (unmocked) notify_remote is
    # alarms.notify_remote itself -- confirm a crafted stage label is
    # sanitized by the time it reaches a provider, through the real wiring.
    poll._fired.clear()
    captured = {}

    def fake_pushover(title, message):
        captured["message"] = message
        return True

    orig_notify, poll.notify = poll.notify, lambda t, m: None  # skip real osascript/say
    orig_pushover, alarms.pushover = alarms.pushover, fake_pushover
    try:
        poll.check_stage_alarms({"probe1_temp": 166}, {1: [(165.0, "wrap\ninjected")]})
    finally:
        poll.notify, alarms.pushover = orig_notify, orig_pushover
    assert "message" in captured, "notify_remote was not reached"
    assert "\n" not in captured["message"], captured


def test_plan_file_size_cap():
    # RT-NEW-2: .cook_plan.json is capped on load -- a real plan is tiny, so an
    # oversized file (corrupted or otherwise) is refused rather than silently
    # parsed.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"1": [[165.0, "wrap"]]}, f)
        small_path = f.name
    try:
        assert plan.load_plan(small_path) == {1: [(165.0, "wrap")]}
    finally:
        os.unlink(small_path)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(json.dumps({"1": [[float(i), "x"] for i in range(50_000)]}))
        big_path = f.name
    try:
        try:
            plan.load_plan(big_path)
            raise AssertionError("should have refused an oversized plan file")
        except ValueError:
            pass
    finally:
        os.unlink(big_path)


def test_mqtt_topic_prefix_verified():
    # RT-NEW-3: on_message must verify the "prod/thing/update/" prefix before
    # slicing, ignoring a message on any other topic instead of producing a
    # garbage dict key.
    class _Msg:
        pass

    class _FakeMQTTClient:
        def __init__(self, *a, **k):
            self.on_connect = self.on_subscribe = self.on_message = None

        def tls_set_context(self, ctx):
            pass

        def ws_set_options(self, **k):
            pass

        def subscribe(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

        def loop_start(self):
            self.on_connect(self, None, None, 0, None)
            self.on_subscribe(self, None, None, None, None)
            bad = _Msg()
            bad.topic, bad.payload = "some/other/topic", b'{"status":{}}'
            good = _Msg()
            good.topic, good.payload = "prod/thing/update/AB12CD34EF56", b'{"status":{}}'
            self.on_message(self, None, bad)   # must be ignored, not crash
            self.on_message(self, None, good)  # must be recorded

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    orig_client_cls, tc.mqtt.Client = tc.mqtt.Client, _FakeMQTTClient
    orig_signed_url = tc.Traeger._mqtt_signed_url
    tc.Traeger._mqtt_signed_url = lambda self: "wss://example.com/mqtt?x=1"
    orig_post = tc.requests.post  # avoid a real network call from _refresh_command
    tc.requests.post = lambda *a, **k: None
    try:
        client = tc.Traeger("u", "p")
        client.token = "t"
        client.grills = [{"thingName": "AB12CD34EF56"}]
        result = client.poll(timeout=1)
        assert set(result.keys()) == {"AB12CD34EF56"}, result
    finally:
        tc.mqtt.Client = orig_client_cls
        tc.Traeger._mqtt_signed_url = orig_signed_url
        tc.requests.post = orig_post


def test_dns_pin_redirects_connection():
    # RT-3: the connection actually used at send-time must be the pinned IP,
    # not whatever the hostname resolves to at that moment (closes the
    # check-time-vs-connect-time DNS gap).
    if alarms._urllib3_connection is None:
        return  # degrade gracefully, matching _pin_dns's own fallback
    calls = []

    def fake_create_connection(address, *a, **k):
        calls.append(address)
        return "FAKESOCKET"

    orig = alarms._urllib3_connection.create_connection
    alarms._urllib3_connection.create_connection = fake_create_connection
    try:
        with alarms._pin_dns("203.0.113.5"):
            result = alarms._urllib3_connection.create_connection(("example.com", 443))
        assert calls == [("203.0.113.5", 443)], calls
        assert result == "FAKESOCKET"
        assert alarms._urllib3_connection.create_connection is fake_create_connection, \
            "must restore the previous create_connection on exit"
    finally:
        alarms._urllib3_connection.create_connection = orig


def test_reauth_refresh_token_flow():
    # RT-1: login() must store a refresh_token, and refresh() must renew the
    # IdToken via REFRESH_TOKEN_AUTH without needing the (already-wiped) password.
    calls = []

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["AuthFlow"])
        if json["AuthFlow"] == "USER_PASSWORD_AUTH":
            assert json["AuthParameters"]["PASSWORD"] == "p1"
            return _Resp({"AuthenticationResult": {"IdToken": "tok1", "RefreshToken": "rtok1"}})
        assert "PASSWORD" not in json["AuthParameters"], "refresh must not need the password"
        assert json["AuthParameters"]["REFRESH_TOKEN"] == "rtok1"
        return _Resp({"AuthenticationResult": {"IdToken": "tok2", "RefreshToken": "rtok2"}})

    orig_post = tc.requests.post
    tc.requests.post = fake_post
    try:
        client = tc.Traeger("user", "p1")
        client.login()
        assert client.token == "tok1" and client.refresh_token == "rtok1"
        assert client.password is None, "password must still be wiped after login"
        client.refresh()
        assert client.token == "tok2" and client.refresh_token == "rtok2"
        assert calls == ["USER_PASSWORD_AUTH", "REFRESH_TOKEN_AUTH"]
    finally:
        tc.requests.post = orig_post


def test_reauth_falls_back_to_full_login():
    # RT-1: when the refresh token itself is rejected, poll.reauth() must fall
    # back to a full re-login using a freshly re-resolved password.
    class _Resp:
        def __init__(self, code, body=None):
            self.status_code = code
            self._body = body or {}

        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, timeout=None):
        if json["AuthFlow"] == "REFRESH_TOKEN_AUTH":
            return _Resp(400)
        return _Resp(200, {"AuthenticationResult": {"IdToken": "tokF", "RefreshToken": "rtokF"}})

    def fake_get(url, headers=None, timeout=None):
        class _G:
            def raise_for_status(self):
                pass

            def json(self):
                return {"things": [{"thingName": "ABC123"}]}
        return _G()

    orig_post, orig_get = tc.requests.post, tc.requests.get
    orig_resolve = poll.resolve_password
    tc.requests.post, tc.requests.get = fake_post, fake_get
    poll.resolve_password = lambda user: ("newpw", "test")
    try:
        client = tc.Traeger("user", "p1")
        client.login()
        client.refresh_token = "stale"  # simulate a token Cognito will now reject
        how = poll.reauth(client, "user")
        assert how == "full login", how
        assert client.token == "tokF"
    finally:
        tc.requests.post, tc.requests.get = orig_post, orig_get
        poll.resolve_password = orig_resolve


def test_thing_name_validated():
    # RT-6: an unexpected thingName (path/MQTT-wildcard characters) from the API
    # must be rejected before it reaches a URL path or MQTT topic.
    class _G:
        def __init__(self, things):
            self._t = things

        def raise_for_status(self):
            pass

        def json(self):
            return {"things": self._t}

    orig_get = tc.requests.get
    try:
        tc.requests.get = lambda *a, **k: _G([{"thingName": "AB12CD34EF56"}])
        c = tc.Traeger("u", "p")
        c.token = "t"
        c.load_grills()
        assert c.grills[0]["thingName"] == "AB12CD34EF56"

        for bad_name in ("a/../b", "x#y", "has space", "a" * 100):
            tc.requests.get = lambda *a, name=bad_name, **k: _G([{"thingName": name}])
            c2 = tc.Traeger("u", "p")
            c2.token = "t"
            try:
                c2.load_grills()
                raise AssertionError(f"should have rejected thingName {bad_name!r}")
            except tc.TraegerError:
                pass
    finally:
        tc.requests.get = orig_get


def test_applescript_escape_control_chars():
    # RT-2: newlines/control characters must be neutralized, not just quotes/backslashes.
    raw = 'He said "hi"\nand\x07beeped\\ok'
    safe = poll._applescript_escape(raw)
    assert "\n" not in safe and "\x07" not in safe, safe
    assert '\\"' in safe and "\\\\" in safe, safe


def test_password_env_popped():
    # RT-7: TRAEGER_PASSWORD must not linger in os.environ (inherited by every
    # child subprocess) once it's been read.
    os.environ["TRAEGER_PASSWORD"] = "s3cr3t"
    try:
        pw, src = poll.resolve_password("someuser")
        assert pw == "s3cr3t" and src == "env", (pw, src)
        assert "TRAEGER_PASSWORD" not in os.environ
    finally:
        os.environ.pop("TRAEGER_PASSWORD", None)


def test_bitwarden_session_via_env_not_argv():
    # RT-8: the vault session key must travel via BW_SESSION env, not --session
    # argv (visible to other processes via ps/procfs for the subprocess's life).
    captured = {}

    def fake_run(argv, capture_output=None, text=None, env=None):
        captured["argv"] = argv
        captured["env"] = env

        class _R:
            returncode = 0
            stdout = "vaultpw\n"
            stderr = ""
        return _R()

    orig_run, orig_bw = poll.subprocess.run, poll._bw_session
    poll.subprocess.run = fake_run
    poll._bw_session = lambda: "SESSIONKEY"
    try:
        pw = poll.bitwarden_password("item-id")
        assert pw == "vaultpw"
        assert "--session" not in captured["argv"], captured["argv"]
        assert captured["env"]["BW_SESSION"] == "SESSIONKEY", captured["env"]
    finally:
        poll.subprocess.run, poll._bw_session = orig_run, orig_bw


def test_speak_every_tick():
    # New: --speak/PELLET_PILOT_SPEAK announces every tick via macOS `say`,
    # not just alarm/stage crossings (off by default).
    class _FakeTraeger:
        def poll(self):
            return {"g": {"status": {
                "grill": 250, "set": 250, "ambient": 70, "system_status": 6,
                "connected": True, "units": 1,
                "acc": [{"type": "probe", "con": 1, "uuid": "p1",
                         "probe": {"get_temp": 168, "set_temp": 205, "alarm_fired": 0}}],
            }}}

    spoken = []
    orig_speak, poll.speak = poll.speak, lambda text: spoken.append(text)
    orig_append, poll.append = poll.append, lambda row: None  # avoid writing cook_log.csv
    poll._eta_samples.clear()
    poll._fired.clear()
    poll._last_state.clear()
    try:
        poll.one_shot(_FakeTraeger(), speak_every_tick=False)
        assert spoken == [], "must stay silent when speak_every_tick is off"
        poll.one_shot(_FakeTraeger(), speak_every_tick=True)
        assert len(spoken) == 1, spoken
        assert "168" in spoken[0] and "205" in spoken[0], spoken
        assert spoken[0].startswith("Update "), spoken  # "Update N", not "Tick"
    finally:
        poll.speak, poll.append = orig_speak, orig_append


def test_speech_for_probes_stage_aware():
    poll._eta_samples.clear()
    poll._last_state.clear()
    now = dt.datetime(2026, 7, 4, 12, 0, 0)
    row = {"ts": now.isoformat(), "probe1_temp": 168, "probe1_connected": True, "probe1_set": 205,
           "probe2_temp": 210, "probe2_connected": True, "probe2_set": 205}
    text_no_stages = poll.speech_for_probes(row, {})
    assert "targeting 205" in text_no_stages, text_no_stages
    text_staged = poll.speech_for_probes(row, {1: [(165.0, "wrap"), (205.0, "done")]})
    assert "heading to wrap" not in text_staged  # 168 already past 165 -> next stage is done
    assert "heading to done at 205" in text_staged, text_staged


_CLOCK_RE = re.compile(r"\d{1,2}:\d{2} [AP]M")


def test_speech_for_probes_includes_eta():
    # The spoken update must carry an ETA computed the same way as the printed
    # prediction (recent-window forecast), not just bare temp/target. Wording
    # is now variable (a bank of natural phrasings, not one fixed template),
    # so assert on the underlying guarantee -- a real clock time is present --
    # rather than one exact sentence.
    poll._eta_samples.clear()
    poll._last_state.clear()
    t0 = dt.datetime(2026, 7, 4, 12, 0, 0)
    t1 = t0 + dt.timedelta(minutes=10)
    poll._eta_samples[1] = [(t0, 150.0), (t1, 160.0)]  # +1 deg/min
    row = {"ts": t1.isoformat(), "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}

    text = poll.speech_for_probes(row, {})
    assert "targeting 205" in text, text
    assert _CLOCK_RE.search(text), text  # (205-160)/1 = 45 min -> a real clock time, always present

    staged = poll.speech_for_probes(row, {1: [(165.0, "wrap"), (205.0, "done")]})
    assert "heading to wrap at 165" in staged, staged
    assert _CLOCK_RE.search(staged), staged  # (165-160)/1 = 5 min -> different, still a real clock time

    # a stalled probe must NOT get a fabricated minutes-ETA -- but must still
    # say so explicitly, never silently blank.
    poll._eta_samples[1] = [(t0, 160.0), (t1, 160.0)]  # flat
    row2 = {"ts": t1.isoformat(), "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}
    flat_text = poll.speech_for_probes(row2, {})
    assert "stalled" in flat_text, flat_text
    assert not _CLOCK_RE.search(flat_text), flat_text  # no fabricated clock time through a stall


def test_speech_for_probes_eta_never_blank():
    # The whole point of --speak: the ETA/status phrase must always be present,
    # even on the very first-ever sample (insufficient data), never silently
    # dropped like it used to be.
    poll._eta_samples.clear()
    poll._last_state.clear()
    now = dt.datetime(2026, 7, 4, 12, 0, 0)
    row = {"ts": now.isoformat(), "probe1_temp": 140, "probe1_connected": True, "probe1_set": 205}
    text = poll.speech_for_probes(row, {})  # first-ever sample: only 1 point so far
    assert text.endswith(".") and "--" in text, text
    after_dash = text.split("--", 1)[1]
    assert after_dash.strip() not in ("", "."), f"ETA phrase must not be blank: {text!r}"
    assert "gathering data" in text, text


def test_speech_for_probes_update_numbering():
    # "Update N", not "Tick N" -- increments across calls within a run.
    poll._eta_samples.clear()
    poll._last_state.clear()
    poll._update_count = 0
    row = {"ts": dt.datetime(2026, 7, 4, 12, 0).isoformat(),
           "probe1_temp": 150, "probe1_connected": True, "probe1_set": 205}
    first = poll.speech_for_probes(row, {})
    second = poll.speech_for_probes(row, {})
    assert first.startswith("Update 1."), first
    assert second.startswith("Update 2."), second
    assert "Tick" not in first and "Tick" not in second


def test_categorize_stall_transitions_and_new_eta():
    # The core of the "natural, comparative" announcement feature: narrate
    # what CHANGED tick-to-tick -- a stall starting/breaking, and whether the
    # projected finish *clock time* actually moved (not just eta_min ticking
    # down, which happens every tick even at a constant rate).
    now = dt.datetime(2026, 7, 4, 12, 0, 0)

    cat, finish = poll._categorize(None, "on_track", 1.0, 45.0, now)
    assert cat == "first_on_track" and finish is not None, (cat, finish)

    prev = {"status": "on_track", "finish_at": finish}
    cat2, _ = poll._categorize(prev, "on_track", 1.0, 44.0, now + dt.timedelta(minutes=1))
    assert cat2 == "steady", cat2  # finish clock barely moved -> steady, even though eta_min dropped

    cat3, fin3 = poll._categorize(prev, "stalled", 0.0, None, now)
    assert cat3 == "entering_stall" and fin3 is None, (cat3, fin3)

    cat4, _ = poll._categorize({"status": "stalled", "finish_at": None}, "stalled", 0.0, None, now)
    assert cat4 == "still_stalled", cat4

    cat5, fin5 = poll._categorize({"status": "stalled", "finish_at": None}, "on_track", 2.0, 10.0, now)
    assert cat5 == "breaking_stall" and fin5 is not None, (cat5, fin5)

    later_prev = {"status": "on_track", "finish_at": now}
    cat6, _ = poll._categorize(later_prev, "on_track", 0.5, 30.0, now)  # finishes 30 min after prev
    assert cat6 == "new_eta_later", cat6

    sooner_prev = {"status": "on_track", "finish_at": now + dt.timedelta(minutes=60)}
    cat7, _ = poll._categorize(sooner_prev, "on_track", 2.0, 5.0, now)  # finishes way before prev
    assert cat7 == "new_eta_sooner", cat7


def test_speech_commentary_deterministic_not_random():
    # Variant choice must be deterministic (cycled by update/probe index), not
    # random.choice -- otherwise this can't be tested or reasoned about during
    # a live cook. Same inputs -> same output, and cycling through `n` visits
    # more than one variant.
    now = dt.datetime(2026, 7, 4, 12, 0, 0)
    a = poll._speech_commentary(1, "probe 1", 3, "steady", "on_track", 1.2, 20.0, 180, now)
    b = poll._speech_commentary(1, "probe 1", 3, "steady", "on_track", 1.2, 20.0, 180, now)
    assert a == b, (a, b)
    variants = {poll._speech_commentary(1, "probe 1", k, "steady", "on_track", 1.2, 20.0, 180, now)
                for k in range(6)}
    assert len(variants) >= 2, variants


def test_probe_names_parse_and_build():
    assert probe_names.parse_name("pork butt") == (1, "pork butt")
    assert probe_names.parse_name("2:brisket") == (2, "brisket")
    assert probe_names.parse_name("  ") is None
    assert probe_names.parse_name("x:name") is None  # non-numeric probe -> ignored

    names = probe_names.build_names(["pork butt", "2:brisket", ""])
    assert names == {1: "pork butt", 2: "brisket"}, names


def test_probe_names_label_fallback():
    assert probe_names.label(1, None) == "probe 1"
    assert probe_names.label(1, {}) == "probe 1"
    assert probe_names.label(1, {1: "pork butt"}) == "the pork butt"
    assert probe_names.label(2, {1: "pork butt"}) == "probe 2"  # only probe 1 named


def test_probe_names_persist_round_trip():
    path = "/tmp/.pellet_pilot_test_probe_names.json"
    try:
        names = {1: "pork butt", 2: "brisket point"}
        probe_names.save_names(names, path=path)
        assert probe_names.load_names(path=path) == names
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_probe_names_size_cap():
    # Mirrors plan.py's plan-file cap -- a malformed/huge config file should
    # be refused outright, not handed to json.load.
    path = "/tmp/.pellet_pilot_test_probe_names_huge.json"
    try:
        with open(path, "w") as f:
            f.write("x" * (probe_names._MAX_NAMES_FILE_BYTES + 1))
        try:
            probe_names.load_names(path=path)
            assert False, "expected ValueError for an oversized names file"
        except ValueError:
            pass
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_speech_for_probes_uses_probe_name():
    # The whole point: spoken text should say what's actually on the probe,
    # not a bare index nobody remembers.
    poll._eta_samples.clear()
    poll._last_state.clear()
    row = {"ts": dt.datetime(2026, 7, 4, 12, 0).isoformat(),
           "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}
    named = poll.speech_for_probes(row, {}, {1: "pork butt"})
    assert "the pork butt" in named, named
    assert "probe 1" not in named, named

    unnamed = poll.speech_for_probes(row, {}, None)
    assert "probe 1" in unnamed, unnamed


def test_check_alarms_uses_probe_name():
    fired = []
    orig_notify, poll.notify = poll.notify, lambda title, msg: fired.append(msg)
    orig_remote, poll.notify_remote = poll.notify_remote, lambda title, msg: None
    poll._fired.clear()
    try:
        row = {"probe1_temp": 205}
        poll.check_alarms(row, {1: [203]}, {1: "pork butt"})
        assert len(fired) == 1, fired
        assert fired[0].startswith("The pork butt reached"), fired[0]
    finally:
        poll.notify, poll.notify_remote = orig_notify, orig_remote


def test_presets_load_known():
    for name in ("brisket", "pork-butt", "ribs", "chicken"):
        p = presets.load_preset(name)
        assert p["stage_specs"], (name, p)
        assert p["name_specs"], (name, p)


def test_presets_list_includes_shipped_files():
    names = presets.list_presets()
    for expected in ("brisket", "pork-butt", "ribs", "chicken"):
        assert expected in names, names


def test_presets_rejects_path_traversal_and_unknown():
    for bad in ("../etc/passwd", "..", ".", "a/b"):
        try:
            presets.load_preset(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
    try:
        presets.load_preset("no-such-preset")
        assert False, "expected ValueError for an unknown preset"
    except ValueError as e:
        assert "Unknown preset" in str(e), e


def test_presets_size_cap():
    path = "/tmp/whatever.yaml"
    try:
        with open(path, "w") as f:
            f.write("x: " + "y" * (presets._MAX_PRESET_FILE_BYTES + 1))
        try:
            presets.load_preset("whatever", presets_dir="/tmp")
            assert False, "expected ValueError for an oversized preset file"
        except ValueError as e:
            assert "refusing to parse" in str(e), e
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_pellet_cli_preset_expands_to_stage_and_name_flags():
    # --preset must expand to the exact --stage/--probe-name spec strings
    # plan.py/probe_names.py already parse -- no separate preset schema to
    # keep in sync with the real thing.
    captured = {}
    orig_main = poll.main
    poll.main = lambda: captured.setdefault("argv", list(sys.argv))
    orig_argv = sys.argv
    try:
        sys.argv = ["pellet", "watch", "--preset", "brisket", "--speak"]
        pellet.main()
        argv = captured["argv"]
        assert "--stage" in argv and "165:wrap" in argv, argv
        assert "--stage" in argv and "203:done" in argv, argv
        assert "--probe-name" in argv and "brisket" in argv, argv
        assert "--speak" in argv, argv
        assert "--watch" in argv, argv  # `pellet watch` implies continuous watch by default
    finally:
        poll.main = orig_main
        sys.argv = orig_argv


def test_pellet_cli_dispatches_to_each_subcommand():
    orig_argv = sys.argv
    calls = []
    originals = {}
    mods = ((history, "history"), (trend, "trend"), (plot, "plot"),
            (report, "report"), (export, "export"))
    for mod, name in mods:
        originals[name] = mod.main
        mod.main = (lambda n: lambda: calls.append(n))(name)
    try:
        for cmd, name in (("history", "history"), ("trend", "trend"), ("chart", "plot"),
                          ("report", "report"), ("export", "export")):
            sys.argv = ["pellet", cmd]
            pellet.main()
        assert calls == ["history", "trend", "plot", "report", "export"], calls
    finally:
        for mod, name in mods:
            mod.main = originals[name]
        sys.argv = orig_argv


def test_report_includes_chart_and_probe_stats():
    sess = _rows(dt.datetime(2026, 7, 4, 9, 0), 12, p1=140, p1_set=203, p2=100)
    html = report.build_report(sess, stages={}, names={1: "pork butt"})
    assert "<svg " in html and "</svg>" in html, html[:200]
    assert "the pork butt" in html, html
    assert "140°" in html  # probe 1 start temp
    assert "probe 2" in html  # unnamed probe falls back to generic label


def test_report_omits_device_thing_id():
    # The report is meant to leave the machine -- the raw grill thingName
    # (a device identifier) must never appear in it, unlike history.py's
    # local-only CLI output.
    secret_thing = "SECRETDEVICEID123"
    sess = [dict(r, thing=secret_thing) for r in _rows(dt.datetime(2026, 7, 4, 9, 0), 5)]
    html = report.build_report(sess)
    assert secret_thing not in html, html


def test_report_shows_stage_crossing_times():
    sess = _rows(dt.datetime(2026, 7, 4, 9, 0), 12, p1=160, p1_set=203)  # climbs 160 -> 171
    html = report.build_report(sess, stages={1: [(165.0, "wrap"), (203.0, "done")]})
    assert "wrap (165°)" in html, html
    assert "not reached" in html  # 203 ("done") never reached in this short synthetic climb


def test_report_includes_manual_note_and_on_grill_override():
    path = "/tmp/.pellet_pilot_test_report_notes.json"
    orig_notes_file = cook_notes.NOTES_FILE
    cook_notes.NOTES_FILE = path
    try:
        sess = _rows(dt.datetime(2026, 7, 4, 11, 0), 12, p1=160, p1_set=203)
        key = history.session_key(sess)
        # on-grill 3 hours before logging started -- the headline duration
        # must reflect that, not just the logged span.
        cook_notes.save_note(key, cut="pork butt", weight_lb=8.5,
                              on_grill="2026-07-04T08:00:00", verdict="amazing")
        html = report.build_report(sess, stages={})
        assert "pork butt" in html and "8.5 lb" in html, html
        assert "amazing" in html, html
        # 08:00 -> 09:22 (last reading, 11:00 + 11*2min) = 3h22m = 3.4h
        assert "3.4h cook" in html, html
    finally:
        cook_notes.NOTES_FILE = orig_notes_file
        if os.path.exists(path):
            os.remove(path)


def test_stall_minutes():
    # Flat readings inside the classic 150-175F stall band count; a normal
    # climb through the same range does not.
    stalled = _rows(dt.datetime(2026, 7, 4, 9, 0), 6, p1=160, p1_set=None)
    for r in stalled:
        r["probe1_temp"] = "160"  # flat, well inside the stall band
    assert report._stall_minutes(stalled, 1) > 0, stalled

    climbing = _rows(dt.datetime(2026, 7, 4, 9, 0), 6, p1=160, p1_set=None)  # 160..165, still climbing
    assert report._stall_minutes(climbing, 1) == 0, "a genuine climb shouldn't count as stalled"


def test_wrap_coach_current_stall_streak_not_total():
    # A climb that only later flattens should report the CURRENT streak's
    # length, not the whole cook's elapsed time.
    mins = list(range(0, 40, 2))              # 20 readings, 2 min apart
    temps = [130.0 + 2 * i for i in range(10)] + [160.0] * 10  # climb, then flat at 160 (in-band)
    stall_min = wrap_coach.current_stall_minutes(mins, temps)
    assert 0 < stall_min <= 20, stall_min      # only the trailing ~18 min flat stretch, not 38


def test_wrap_coach_recommend_categories():
    mins = list(range(20))
    assert wrap_coach.recommend(mins[:1], [140.0])["status"] == "insufficient"

    # rate 1 deg/min, still 46 min out at index 19 (159 -> target 205) -> no rush
    climbing = [140.0 + i for i in range(20)]
    rec = wrap_coach.recommend(mins, climbing, target=205)
    assert rec["status"] == "on_track" and rec["urgency"] == "info", rec

    # rate 1 deg/min, only 1 min out (204 -> target 205) -> time to plan the rest
    near_done = [185.0 + i for i in range(20)]
    rec = wrap_coach.recommend(mins, near_done, target=205)
    assert rec["status"] == "on_track" and rec["urgency"] == "suggest", rec
    assert "rest time" in rec["advice"], rec

    done = wrap_coach.recommend(mins, [200.0 + i for i in range(20)], target=205)
    assert done["status"] == "done" and "pull it" in done["advice"], done

    flat_170 = wrap_coach.recommend(mins, [170.0] * 20, target=205)
    assert flat_170["status"] == "stalled" and flat_170["urgency"] == "suggest", flat_170

    flat_100 = wrap_coach.recommend(mins, [100.0] * 20, target=205)
    assert flat_100["status"] == "not_rising" and flat_100["urgency"] == "suggest", flat_100


def test_wrap_coach_long_stall_and_wrapped_escalation():
    long_stall_mins = list(range(0, 200, 2))
    long_stall_temps = [160.0] * len(long_stall_mins)

    unwrapped = wrap_coach.recommend(long_stall_mins, long_stall_temps, target=205, wrapped=False)
    assert unwrapped["urgency"] == "urgent" and "Wrap now" in unwrapped["advice"], unwrapped

    wrapped_long = wrap_coach.recommend(long_stall_mins, long_stall_temps, target=205, wrapped=True)
    assert wrapped_long["urgency"] == "urgent" and "bumping the grill temp" in wrapped_long["advice"], wrapped_long

    short_stall_mins = list(range(0, 20, 2))
    short_stall_temps = [160.0] * len(short_stall_mins)
    wrapped_short = wrap_coach.recommend(short_stall_mins, short_stall_temps, target=205, wrapped=True)
    assert wrapped_short["urgency"] == "info", wrapped_short


def test_wrap_coach_recommend_for_probe_auto_detects_wrapped():
    # Past the "wrap" stage temp and stalled -> auto-detected as wrapped,
    # so advice should NOT tell you to wrap something you already wrapped.
    mins = list(range(0, 200, 2))
    temps = [170.0] * len(mins)  # past the 165 wrap stage, stalled
    stages_for_probe = [(165.0, "wrap"), (205.0, "done")]
    rec = wrap_coach.recommend_for_probe(mins, temps, stages_for_probe)
    assert "already wrapped" in rec["advice"].lower(), rec

    not_yet_wrapped_temps = [160.0] * len(mins)  # below 165 -> not wrapped yet
    rec2 = wrap_coach.recommend_for_probe(mins, not_yet_wrapped_temps, stages_for_probe)
    assert "wrap now" in rec2["advice"].lower(), rec2


def test_print_coach_smoke():
    poll._eta_samples.clear()
    t0 = dt.datetime(2026, 7, 4, 12, 0, 0)
    t1 = t0 + dt.timedelta(minutes=10)
    poll._eta_samples[1] = [(t0, 150.0), (t1, 160.0)]
    row = {"ts": t1.isoformat(), "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        poll.print_coach(row, {}, {1: "pork butt"})
    out = buf.getvalue()
    assert "pork butt" in out, out


def test_check_pellet_alarm_fires_once_and_rearms():
    poll._pellet_alarm_fired = False
    fired = []
    orig, poll.notify = poll.notify, lambda t, m: fired.append(m)
    orig_r, poll.notify_remote = poll.notify_remote, lambda t, m: None
    try:
        poll.check_pellet_alarm({"pellet_level": 25})   # above threshold -- no alert
        assert not fired, fired
        poll.check_pellet_alarm({"pellet_level": 20})   # at threshold -- fires
        poll.check_pellet_alarm({"pellet_level": 15})   # still low -- no repeat
        assert len(fired) == 1, fired
        poll.check_pellet_alarm({"pellet_level": 25})   # refilled, but below re-arm point yet
        poll.check_pellet_alarm({"pellet_level": 10})   # drop again -- must NOT re-fire yet
        assert len(fired) == 1, fired
        poll.check_pellet_alarm({"pellet_level": 30})   # refilled past the re-arm point
        poll.check_pellet_alarm({"pellet_level": 15})   # drops again -- fires again
        assert len(fired) == 2, fired
        # a grill with no pellet sensor (field absent) must never crash or alert
        poll.check_pellet_alarm({})
        assert len(fired) == 2, fired
    finally:
        poll.notify, poll.notify_remote = orig, orig_r


def test_check_temp_anomaly_requires_sustained_deviation():
    poll._grill_low_since = None
    poll._grill_anomaly_fired = False
    fired = []
    orig, poll.notify = poll.notify, lambda t, m: fired.append(m)
    orig_r, poll.notify_remote = poll.notify_remote, lambda t, m: None
    t0 = dt.datetime(2026, 7, 4, 12, 0, 0)
    try:
        # deviation just starting -- must not fire immediately
        poll.check_temp_anomaly({"ts": t0.isoformat(), "grill": 200, "set": 275}, "Manual cook")
        assert not fired, fired
        # 5 min later, still deviated -- still under the sustained window
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=5)).isoformat(), "grill": 200, "set": 275}, "Manual cook")
        assert not fired, fired
        # 12 min in -- past the sustained window, fires
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=12)).isoformat(), "grill": 200, "set": 275}, "Manual cook")
        assert len(fired) == 1, fired
        # recovers -- resets, a later fresh long deviation can fire again
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=14)).isoformat(), "grill": 270, "set": 275}, "Manual cook")
        assert poll._grill_low_since is None, "should reset once the grill recovers"
        # a normal momentary dip that recovers quickly must never fire at all
        poll._grill_low_since, poll._grill_anomaly_fired = None, False
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=20)).isoformat(), "grill": 200, "set": 275}, "Manual cook")
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=21)).isoformat(), "grill": 270, "set": 275}, "Manual cook")
        assert len(fired) == 1, fired  # unchanged -- the brief dip never sustained past 10 min
        # not actively cooking (e.g. Idle) -- never fires even if "deviated"
        poll._grill_low_since, poll._grill_anomaly_fired = None, False
        poll.check_temp_anomaly({"ts": t0.isoformat(), "grill": 70, "set": 275}, "Idle")
        poll.check_temp_anomaly(
            {"ts": (t0 + dt.timedelta(minutes=20)).isoformat(), "grill": 70, "set": 275}, "Idle")
        assert len(fired) == 1, fired
    finally:
        poll.notify, poll.notify_remote = orig, orig_r


def test_check_error_counters_fires_on_increment_only():
    poll._error_counter_baseline.clear()
    fired = []
    orig, poll.notify = poll.notify, lambda t, m: fired.append(m)
    orig_r, poll.notify_remote = poll.notify_remote, lambda t, m: None
    try:
        # first-ever reading establishes the baseline -- must NOT alert on it,
        # even if the lifetime count is already nonzero from long before this run
        poll.check_error_counters({"error_overheat": 3, "error_lowtemp": 0, "error_bad_thermocouple": 0})
        assert not fired, fired
        # unchanged -- no alert
        poll.check_error_counters({"error_overheat": 3, "error_lowtemp": 0, "error_bad_thermocouple": 0})
        assert not fired, fired
        # a NEW overheat event during this run -- alerts
        poll.check_error_counters({"error_overheat": 4, "error_lowtemp": 0, "error_bad_thermocouple": 0})
        assert len(fired) == 1 and "overheat" in fired[0], fired
        # a NEW bad-thermocouple event -- alerts too
        poll.check_error_counters({"error_overheat": 4, "error_lowtemp": 0, "error_bad_thermocouple": 1})
        assert len(fired) == 2 and "thermocouple" in fired[1], fired
    finally:
        poll.notify, poll.notify_remote = orig, orig_r


def test_migrate_log_schema_preserves_old_rows_and_adds_new_columns():
    path = "/tmp/.pellet_pilot_test_migrate.csv"
    old_fields = ["ts", "thing", "grill", "set", "ambient", "system_status"] + [
        f"probe{i}_{suffix}" for i in range(1, poll.MAX_PROBES + 1)
        for suffix in ("temp", "set", "connected", "alarm")]
    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=old_fields)
            w.writeheader()
            w.writerow({k: "" for k in old_fields} | {
                "ts": "2026-07-04T12:00:00", "thing": "g", "grill": "250", "set": "250",
                "ambient": "70", "system_status": "6", "probe1_temp": "160"})

        poll.migrate_log_schema(path)

        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == poll.FIELDS, reader.fieldnames
            rows = list(reader)
        assert len(rows) == 1, rows
        assert rows[0]["grill"] == "250" and rows[0]["probe1_temp"] == "160", rows[0]
        assert rows[0]["pellet_level"] == "", rows[0]  # new column, blank for old data

        # already-current schema -- no-op, and no data loss on a second call
        poll.migrate_log_schema(path)
        with open(path, newline="") as f:
            rows2 = list(csv.DictReader(f))
        assert rows2 == rows, (rows2, rows)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_github_workflows_are_valid_yaml():
    # A broken workflow file fails silently (no CI run at all, or a run that
    # never triggers) rather than raising anywhere obvious -- catch a syntax
    # mistake here instead of discovering it only when a real PR/push doesn't
    # get the CI run it was supposed to.
    import yaml
    workflows_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  ".github", "workflows")
    files = [f for f in os.listdir(workflows_dir) if f.endswith((".yml", ".yaml"))]
    assert files, "expected at least one workflow file"
    for fname in files:
        with open(os.path.join(workflows_dir, fname)) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict), f"{fname}: not a YAML mapping"
        assert data.get("jobs"), f"{fname}: no jobs defined"


def test_backoff_seconds():
    # RT-4: exponential backoff, capped, so a persistent re-auth failure doesn't
    # hammer Cognito every `interval` seconds forever.
    assert poll._backoff_seconds(30, 1) == 60
    assert poll._backoff_seconds(30, 2) == 120
    assert poll._backoff_seconds(30, 20) == poll._MAX_BACKOFF_S


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
