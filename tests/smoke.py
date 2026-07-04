"""Smoke + regression suite — runs in CI before any PR can merge.

Exercises the parts static analysis can't see: imports, status parsing, the paho
MQTT client construction, multi-probe logging, history segmentation, plotting,
Grafana export, the remote-alarm SSRF guard (+ DNS-pinning), refresh-token
re-auth, credential hygiene, and thingName validation. A dependency bump or
refactor that breaks any of this fails here (branch protection) instead of at
runtime. No network required (SSRF checks use IP literals; auth flows use
mocked HTTP responses).
"""
import datetime as dt
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
    a = poll._speech_commentary(1, 3, "steady", "on_track", 1.2, 20.0, 180, now)
    b = poll._speech_commentary(1, 3, "steady", "on_track", 1.2, 20.0, 180, now)
    assert a == b, (a, b)
    variants = {poll._speech_commentary(1, k, "steady", "on_track", 1.2, 20.0, 180, now) for k in range(6)}
    assert len(variants) >= 2, variants


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
