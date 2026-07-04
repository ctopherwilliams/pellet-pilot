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


def test_forecast_zero_variance_window():
    # Two (or more) samples landing at the identical timestamp (e.g. two ticks
    # in the same second) must degrade to "insufficient", not crash polyfit on
    # a singular/zero-variance x -- this reproduces a real LinAlgError seen
    # when --watch's live sample buffer picks up a same-second duplicate.
    fc = fc_mod.forecast([5.0, 5.0, 5.0], [160.0, 161.0, 162.0], 205)
    assert fc["status"] == "insufficient", fc
    assert fc["rate"] is None and fc["eta_min"] is None, fc


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
    try:
        poll.one_shot(_FakeTraeger(), speak_every_tick=False)
        assert spoken == [], "must stay silent when speak_every_tick is off"
        poll.one_shot(_FakeTraeger(), speak_every_tick=True)
        assert len(spoken) == 1, spoken
        assert "168" in spoken[0] and "205" in spoken[0], spoken
    finally:
        poll.speak, poll.append = orig_speak, orig_append


def test_speech_for_probes_stage_aware():
    poll._eta_samples.clear()
    now = dt.datetime(2026, 7, 4, 12, 0, 0)
    row = {"ts": now.isoformat(), "probe1_temp": 168, "probe1_connected": True, "probe1_set": 205,
           "probe2_temp": 210, "probe2_connected": True, "probe2_set": 205}
    text_no_stages = poll.speech_for_probes(row, {})
    assert "target 205" in text_no_stages, text_no_stages
    text_staged = poll.speech_for_probes(row, {1: [(165.0, "wrap"), (205.0, "done")]})
    assert "next wrap" not in text_staged  # 168 already past 165 -> next stage is done
    assert "next done at 205" in text_staged, text_staged


def test_speech_for_probes_includes_eta():
    # The spoken update must carry an ETA computed the same way as the printed
    # prediction (recent-window forecast), not just bare temp/target.
    poll._eta_samples.clear()
    t0 = dt.datetime(2026, 7, 4, 12, 0, 0)
    t1 = t0 + dt.timedelta(minutes=10)
    poll._eta_samples[1] = [(t0, 150.0), (t1, 160.0)]  # +1 deg/min
    row = {"ts": t1.isoformat(), "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}

    text = poll.speech_for_probes(row, {})
    assert "target 205" in text, text
    assert "minutes away" in text and "around" in text, text  # (205-160)/1 = 45 min

    staged = poll.speech_for_probes(row, {1: [(165.0, "wrap"), (205.0, "done")]})
    assert "next wrap at 165" in staged, staged
    assert "minutes away" in staged, staged  # (165-160)/1 = 5 min

    # a stalled probe must NOT get a fabricated ETA
    poll._eta_samples[1] = [(t0, 160.0), (t1, 160.0)]  # flat
    row2 = {"ts": t1.isoformat(), "probe1_temp": 160, "probe1_connected": True, "probe1_set": 205}
    flat_text = poll.speech_for_probes(row2, {})
    assert "minutes away" not in flat_text, flat_text


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
