"""Smoke test — runs in CI before a Dependabot PR can auto-merge.

Exercises the parts static analysis can't see: real imports, status parsing, and
the paho MQTT client construction that a major paho bump would break. If a
dependency update breaks the API this file uses, CI fails and the PR does not
merge (branch protection), so the breakage is caught here instead of at runtime.
"""
import os
import sys

# Make the repo root importable regardless of how this file is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt

import poll  # noqa: E402,F401  (import must not crash)
import trend  # noqa: E402,F401
import traeger_client as tc  # noqa: E402


def test_parse_status():
    doc = {"status": {
        "grill": 225, "set": 225, "ambient": 70, "system_status": 6,
        "connected": True, "units": 1,
        "acc": [{"uuid": "p1", "type": "probe", "con": 1,
                 "probe": {"get_temp": 150, "set_temp": 203, "alarm_fired": 0}}],
    }}
    r = tc.parse_status("grill-x", doc)
    assert r["grill"] == 225, r
    assert r["units"] == "F", r
    assert r["probes"][0]["get_temp"] == 150, r
    assert r["probes"][0]["set_temp"] == 203, r


def test_mqtt_client_builds():
    # Mirror how poll() builds the client — catches paho callback-API breaks.
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
    c.tls_set_context(tc._mqtt_tls_context("example.com"))
    c.ws_set_options(path="/mqtt?x=1", headers={"Host": "example.com"})


def test_status_decode():
    assert poll.decode_status(99, True, 277) == "Running"
    assert poll.decode_status(99, False, 0) == "Offline"


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
