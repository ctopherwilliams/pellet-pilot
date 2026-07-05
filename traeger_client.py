"""
Minimal Traeger WiFire client.

Unofficial. Talks to Traeger's cloud the same way the phone app does:
  1. Log in to AWS Cognito with your Traeger email + password  -> IdToken
  2. GET /prod/users/self                                       -> your grill(s)
  3. POST /prod/mqtt-connections                                -> signed WebSocket URL
  4. Connect MQTT-over-WSS, subscribe to the grill's status topic,
     nudge the grill to publish a fresh reading (command "90"),
     read one status message, disconnect.

This is derived from the reverse-engineered protocol used by the Home Assistant
`ha-traeger` integration. There is no official Traeger API; this can break if
Traeger changes their backend, and it is technically against their ToS.

Only reads status here (no start/stop/set-temp) -- monitoring, not control.
"""

import json
import os
import re
import ssl
import threading
import time
import urllib.parse

import paho.mqtt.client as mqtt
import requests

# Cap MQTT payloads to avoid memory exhaustion from a poisoned broker message.
_MAX_MQTT_PAYLOAD = 256 * 1024
_TOPIC_PREFIX = "prod/thing/update/"

# Traeger thingNames observed in practice are short hex/alnum device ids (e.g.
# "AB12CD34EF56"). Enforce that shape before using the value in a URL path or
# MQTT topic -- MQTT wildcard characters ("+", "#") or path separators in an
# unexpected thingName could otherwise widen a subscription or break a request.
_THING_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

CLIENT_ID = "4id473dsrcq4kevlgrikukqn2a"  # Traeger app Cognito client (rotated; old: 2fuohjtqv1e63dckp5v84rau0j)
COGNITO_URL = "https://cognito-idp.us-west-2.amazonaws.com/"
# Traeger retired the old AWS API Gateway (1ywgyc65d1.execute-api.../prod, now NXDOMAIN)
# and moved to their own domain; paths no longer carry the /prod prefix.
API = "https://mobile-iot-api.iot.traegergrills.io"


class TraegerError(RuntimeError):
    pass


def _mqtt_tls_context(hostname):
    """TLS for AWS IoT WSS. Secure by default; set TRAEGER_INSECURE_TLS=1 to opt out."""
    if os.environ.get("TRAEGER_INSECURE_TLS", "").lower() in ("1", "true", "yes"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    # paho passes server_hostname on connect(); this pins verification intent.
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _clear_secret(value):
    """Best-effort wipe of a credential string held in memory."""
    if isinstance(value, bytearray):
        for i in range(len(value)):
            value[i] = 0
    # CPython str objects are immutable; dropping references is the practical limit.


class Traeger:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.token = None
        self.refresh_token = None
        self.grills = []          # list of {"thingName": ...}
        self._status = {}         # thingName -> full thing document

    def clear_credentials(self):
        """Drop password from memory after authentication."""
        _clear_secret(self.password)
        self.password = None

    # ---- REST ----------------------------------------------------------
    def login(self):
        r = requests.post(
            COGNITO_URL,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
            json={
                "ClientMetadata": {},
                "AuthParameters": {"USERNAME": self.username, "PASSWORD": self.password},
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": CLIENT_ID,
            },
            timeout=30,
        )
        if r.status_code != 200:
            # Never echo Cognito response bodies — they can leak account/MFA hints.
            raise TraegerError(
                f"Cognito login failed ({r.status_code}). "
                "Check TRAEGER_USERNAME / TRAEGER_PASSWORD."
            )
        auth = r.json()["AuthenticationResult"]
        self.token = auth["IdToken"]
        self.refresh_token = auth.get("RefreshToken", self.refresh_token)
        self.clear_credentials()
        return self.token

    def refresh(self):
        """Renew the IdToken using the stored refresh token -- no password needed.

        Long --watch cooks outlive the ~1h IdToken; this is the correct way to
        renew it since login() wipes self.password immediately after use. Falls
        back to a full login() (which needs the password re-supplied by the
        caller) only if the refresh token itself is rejected.
        """
        if not self.refresh_token:
            raise TraegerError("No refresh token available; call login() first.")
        r = requests.post(
            COGNITO_URL,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
            json={
                "ClientMetadata": {},
                "AuthParameters": {"REFRESH_TOKEN": self.refresh_token},
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": CLIENT_ID,
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise TraegerError(f"Token refresh failed ({r.status_code}).")
        auth = r.json()["AuthenticationResult"]
        self.token = auth["IdToken"]
        self.refresh_token = auth.get("RefreshToken", self.refresh_token)
        return self.token

    def load_grills(self):
        r = requests.get(f"{API}/users/self", headers={"authorization": self.token}, timeout=30)
        r.raise_for_status()
        things = r.json().get("things", [])
        for g in things:
            name = g.get("thingName", "")
            if not _THING_NAME_RE.match(name):
                raise TraegerError(f"Unexpected grill identifier from the API; refusing to use it: {name!r}")
        self.grills = things
        if not self.grills:
            raise TraegerError("No grills found on this Traeger account.")
        return self.grills

    def _mqtt_signed_url(self):
        r = requests.post(f"{API}/mqtt-connections", headers={"Authorization": self.token}, timeout=30)
        r.raise_for_status()
        return r.json()["signedUrl"]

    def _refresh_command(self, thing_name):
        # Command "90" asks the grill to publish its current state now.
        requests.post(
            f"{API}/things/{thing_name}/commands",
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "Accept-Language": "en-us",
                "User-Agent": "Traeger/11 CFNetwork/1209 Darwin/20.2.0",
            },
            json={"command": "90"},
            timeout=30,
        )

    # ---- one-shot poll -------------------------------------------------
    def poll(self, timeout=25):
        """Connect, grab one fresh status per grill, disconnect. Returns {thingName: status_doc}."""
        signed = self._mqtt_signed_url()
        parts = urllib.parse.urlparse(signed)
        got = threading.Event()
        result = {}

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
        client.tls_set_context(_mqtt_tls_context(parts.hostname or parts.netloc))
        client.ws_set_options(path=f"{parts.path}?{parts.query}", headers={"Host": parts.netloc})

        # paho-mqtt v2 callback signatures.
        def on_connect(c, u, flags, reason_code, properties):
            for g in self.grills:
                c.subscribe((f"{_TOPIC_PREFIX}{g['thingName']}", 1))

        def on_subscribe(c, u, mid, reason_codes, properties):
            for g in self.grills:
                try:
                    self._refresh_command(g["thingName"])
                except requests.RequestException:
                    pass  # retained message may still arrive

        def on_message(c, u, msg):
            # Only ever expect topics we subscribed to on a trusted AWS IoT
            # broker, but verify the prefix before slicing rather than
            # assuming it -- a mismatched topic is silently ignored instead
            # of producing a garbage dict key.
            if not msg.topic.startswith(_TOPIC_PREFIX):
                return
            if len(msg.payload) > _MAX_MQTT_PAYLOAD:
                return
            tn = msg.topic[len(_TOPIC_PREFIX):]
            try:
                result[tn] = json.loads(msg.payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if len(result) >= len(self.grills):
                got.set()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message

        client.connect(parts.netloc, 443, keepalive=300)
        client.loop_start()
        got.wait(timeout=timeout)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

        if not result:
            raise TraegerError("Connected but no status received (grill may be offline/unplugged).")
        self._status = result
        return result


# ---- parsing helpers ---------------------------------------------------
def parse_status(thing_name, doc):
    """Flatten a thing document into a simple reading dict."""
    st = doc.get("status", {})
    # "usage" is a sibling of "status" in the raw doc, not nested inside it.
    # error_stats are cumulative lifetime counters (not a live flag), so a
    # caller must diff against the previous reading to detect a NEW error --
    # see poll.py's check_error_counters().
    error_stats = doc.get("usage", {}).get("error_stats", {})
    reading = {
        "thing": thing_name,
        "grill": st.get("grill"),          # current grill temp
        "set": st.get("set"),              # grill target temp
        "ambient": st.get("ambient"),      # ambient/outdoor probe
        "system_status": st.get("system_status"),
        "connected": st.get("connected"),
        "units": "C" if st.get("units") == 0 else "F",
        "pellet_level": st.get("pellet_level"),        # 0-100, hopper sensor (if the grill has one)
        "error_overheat": error_stats.get("overheat"),
        "error_lowtemp": error_stats.get("lowtemp"),
        "error_bad_thermocouple": error_stats.get("bad_thermocouple"),
        "probes": [],
    }
    for acc in st.get("acc", []):
        if acc.get("type") == "probe" or "probe" in acc:
            p = acc.get("probe", {})
            reading["probes"].append({
                "uuid": acc.get("uuid"),
                "connected": bool(acc.get("con")),
                "get_temp": p.get("get_temp"),   # current probe temp
                "set_temp": p.get("set_temp"),   # probe target (0 = none set)
                "alarm_fired": p.get("alarm_fired"),
            })
    return reading
