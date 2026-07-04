<p align="center">
  <img src="assets/banner.svg" alt="Pellet Pilot" width="100%">
</p>

<p align="center">
  <b>Real-time telemetry, live probe trend lines, cook ETAs, and temperature alarms for WiFi pellet grills.</b><br>
  <sub>Pull grill &amp; probe temps straight from the cloud · analyze your cook right in the terminal.</sub>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-e8461e">
  <img alt="Status" src="https://img.shields.io/badge/status-active-ff8c1a">
  <img alt="PRs" src="https://img.shields.io/badge/PRs-welcome-ffd166">
</p>

> [!IMPORTANT]
> **Unofficial project.** Pellet Pilot is an independent, community-built tool. It is **not affiliated with, endorsed by, or sponsored by Traeger Inc.** "Traeger" and "WiFIRE" are trademarks of their respective owner and are used here only to describe compatibility. It talks to the same cloud the mobile app uses via a reverse-engineered protocol, which may break at any time and may be against the vendor's Terms of Service. Use at your own risk.

---

## 🔥 Why Pellet Pilot

The mobile app shows you a number. Pellet Pilot gives you the **curve** — and does the math a pitmaster actually wants mid-cook:

- **📈 Live probe trend line** — rate of rise in °/min, with a sparkline of the climb.
- **⏱ Time-to-target ETA** — "your probe hits 203° at ~4:45 PM," updated every reading.
- **🔔 Temperature alarms** — desktop notification + spoken alert when the probe crosses your thresholds.
- **🗒 Your own cook history** — every reading logged to CSV, because the cloud keeps none. Query and re-plot past cooks any time.
- **🧠 Stall detection** — flags the classic 150–170° brisket/pork-shoulder plateau so you don't panic (or wrap early).
- **🖥 Terminal-native** — no app, no dashboard server. Pipe it, grep it, graph it.

---

## ⚡ Quickstart

```bash
git clone https://github.com/ctopherwilliams/pellet-pilot.git
cd pellet-pilot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

cp .env.example .env          # add your grill account email
# store your password securely (choose one) — see "Credentials" below

./venv/bin/python poll.py                       # one live reading
./venv/bin/python poll.py --watch 30            # log every 30s during a cook
./venv/bin/python poll.py --watch 30 --alarm 203  # ...and ping me at 203°F
./venv/bin/python trend.py                      # rate + ETA to target
```

```text
[13:22:07] grill 277° (set 275°)  P1 168°→203°  [Running]
  ⏱  P1 ~47 min to 203° (≈ 4:45 PM) · +0.75°/min
```

---

## 🔮 When will it be done?

This is the headline feature: Pellet Pilot watches the climb and tells you **when
each probe will hit its target** — live, as it polls.

```bash
./venv/bin/python poll.py --watch 30 --alarm 203
```

```text
[13:22:07] grill 277° (set 275°)  P1 168°→203°  [Running]
  ⏱  P1 ~47 min to 203° (≈ 4:45 PM) · +0.75°/min
```

Read that as: **probe 1 reaches 203° in about 47 minutes, ~4:45 PM, rising 0.75°/min.**
The estimate refreshes every reading and is fit from a **recent window** of data, so
it tracks the cook speeding up or slowing down instead of lagging on a whole-cook average.

**During the stall** — the 150–175° plateau where a big cut parks for a while — the
rate flattens, so instead of guessing a wild time it tells you plainly:

```text
  ⏱  P1 stalled near 152° · hold, or wrap to push through  (+0.03°/min)
```

Want a one-off check without the live loop? `trend.py` prints the same prediction
from your log at any time:

```bash
./venv/bin/python trend.py             # probe 1
./venv/bin/python trend.py --probe 2   # a second probe
./venv/bin/python trend.py --window 15 # base it on just the last 15 min
```

```text
=== probe1_temp trend ===
points:   34  over 61.0 min
current:  168°   (min 122°, max 168°)
trend:    ▁▁▂▂▃▃▄▄▅▅▆▆▇▇██
rate:     +0.75 °/min   (+45 °/hr, recent)
done:     ~47 min to 203° (≈ 4:45 PM) · +0.75°/min
```

> How it works: a least-squares fit over the recent window gives °/min, then
> `(target − current) ÷ rate` gives the minutes remaining. Below ~0.05°/min it
> reports *stalled* (in the plateau band) or *not rising* rather than a bogus ETA.

---

## 🔐 Credentials

Your password is **never** committed, hard-coded, or written to a plaintext log. Pick whichever fits your setup — the program tries them in order and uses the first that works.

### Basic setup — any OS (Linux · Windows · macOS)

The simplest option: drop your login into a local `.env` file (already gitignored).

```bash
cp .env.example .env
```

Then edit `.env`:

```ini
TRAEGER_USERNAME=you@example.com
TRAEGER_PASSWORD=your-grill-account-password
```

That's all — run `poll.py`. This is the same email &amp; password you use in the grill's mobile app.

<details>
<summary>Prefer an environment variable over a file?</summary>

**Linux / macOS (bash · zsh):**
```bash
export TRAEGER_USERNAME="you@example.com"
export TRAEGER_PASSWORD="your-password"
```

**Windows (PowerShell):**
```powershell
$env:TRAEGER_USERNAME = "you@example.com"
$env:TRAEGER_PASSWORD = "your-password"
```
</details>

### Advanced — keep the password out of any file

| Source | How | Notes |
|--------|-----|-------|
| 🔑 **Bitwarden** (any OS) | Unlock your vault into a session, set `TRAEGER_BW_ITEM` to the item name/id | Fetched in-memory at runtime |
| 🍎 **macOS Keychain** | `security add-generic-password -s traeger-wifire -a you@email -w` | Encrypted at rest |

See [SECURITY.md](SECURITY.md) for the full model and what is *never* written to disk.

---

## 🧩 How it works

```mermaid
flowchart LR
    subgraph local["Your machine"]
        P["poll.py"]
        C[("cook_log.csv")]
        T["trend.py"]
        OUT(["terminal analytics"])
    end
    subgraph cloud["Grill cloud · AWS"]
        COG["Cognito auth"]
        API["REST API"]
        MQ["IoT MQTT broker"]
    end
    P -->|"① email + password"| COG
    COG -->|"IdToken"| P
    P -->|"② GET /users/self"| API
    P -->|"③ POST /mqtt-connections"| API
    P <-->|"④ live grill + probe temps"| MQ
    P -->|"append reading"| C
    C --> T
    T -->|"rate · ETA · stall"| OUT

    classDef fire fill:#2b211a,stroke:#ff8c1a,color:#ffb454;
    classDef store fill:#241a14,stroke:#3d2e22,color:#c9b8a8;
    class P,T,COG,API,MQ fire
    class C,OUT store
```

- **`traeger_client.py`** — auth, grill discovery, one-shot MQTT status read, status parser.
- **`poll.py`** — poll & log; `--watch` loop with auto re-auth for long cooks; probe alarms.
- **`trend.py`** — linear trend, rise rate, time-to-target, stall detector.

### Controller status codes

| Code | Meaning | | Code | Meaning |
|---|---|---|---|---|
| 99 | Running¹ | | 5 | Preheating |
| 9 | Shutting down | | 4 | Igniting |
| 8 | Cool-down | | 3 | Idle |
| 7 | Custom cook | | 2 | Sleeping |
| 6 | Manual cook | | | |

<sub>¹ On newer controllers `99` is the normal running state; on older D2 controllers it meant "offline." Pellet Pilot trusts live connection + temps over the raw code.</sub>

---

## 📥 Note on historical data

The grill cloud does **not** expose past temperatures — the in-app graph is drawn live and never stored server-side. That's why Pellet Pilot logs everything itself: **`cook_log.csv` is your history.** Start `--watch` at the beginning of a cook and you'll have the complete curve to analyze afterward.

---

## 🧰 More tools

```bash
# Multiple probes: alarms and trends target a specific probe
./venv/bin/python poll.py --watch 30 --alarm 1:203 --alarm 2:165
./venv/bin/python trend.py --probe 2

# Browse past cooks (sessions split on gaps in the log)
./venv/bin/python history.py list
./venv/bin/python history.py show 1
./venv/bin/python history.py summary

# Chart a cook — SVG (no deps), interactive HTML, or PNG (matplotlib)
./venv/bin/python plot.py --out cook.svg
./venv/bin/python plot.py --html --out cook.html
./venv/bin/python plot.py --png --out cook.png     # pip install -r requirements-plot.txt

# Grafana-friendly export (local files) or a localhost Prometheus endpoint
./venv/bin/python export.py --format influx --out cook.lp
./venv/bin/python export.py --serve                # http://127.0.0.1:9109/metrics
```

**Remote alarms** (in addition to the local macOS notification) — set any of these
env vars and probe-crossing alerts are delivered there too:

| Provider | Env |
|---|---|
| Pushover | `PUSHOVER_TOKEN`, `PUSHOVER_USER` |
| ntfy | `NTFY_TOPIC` (server via `NTFY_SERVER`) |
| Webhook | `ALARM_WEBHOOK_URL` (POSTed JSON) |

HTTPS is required and the generic webhook is SSRF-guarded (private/loopback/metadata
addresses are refused). See [SECURITY.md](SECURITY.md).

**Issue autopilot** — apply the `autofix` label to an issue for an AI-drafted fix
plan, then `autofix-approved` to get a human-reviewed PR. Label-gated, never
auto-merged; requires an `ANTHROPIC_API_KEY` repo secret. See [SECURITY.md](SECURITY.md).

---

## 🗺 Roadmap

- [x] `history.py` — browse & re-plot past cooks, per-session summaries
- [x] Multi-probe support in the trend view
- [x] PNG/interactive plot export
- [x] Pushover / ntfy / webhook alarm targets
- [x] Grafana-friendly export
- [x] Issue autopilot — triage issues, draft a fix, open a human-reviewed PR (label-gated, untrusted issue text, never auto-merged)

---

## 🙌 Credits

Built on protocol research from the Home Assistant Traeger integration community
([sebirdman](https://github.com/sebirdman/hass_traeger), [lymanepp](https://github.com/lymanepp/ha-traeger), [johnvoipguy](https://github.com/johnvoipguy/Traeger-WiFire)). Huge thanks to those maintainers.

## 📄 License

[MIT](LICENSE) © 2026 ctopherwilliams. Not affiliated with Traeger Inc.
