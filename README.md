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
[2026-07-04T13:22:07] grill 277° (set 275°)  probe 168° (set 203°)  [Running]

=== probe1_temp trend ===
points:   34  over 61.0 min
current:  168°   (min 122°, max 168°)
trend:    ▁▁▂▂▃▃▄▄▅▅▆▆▇▇██
rate:     +0.75 °/min   (+45 °/hr)
target:   203°  ->  ~47 min away (≈ 4:45 PM)
```

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

## 🗺 Roadmap

- [ ] `history.py` — browse & re-plot past cooks, per-session summaries
- [ ] Multi-probe support in the trend view
- [ ] PNG/interactive plot export
- [ ] Pushover / ntfy / webhook alarm targets
- [ ] Grafana-friendly export
- [ ] Issue autopilot — triage open issues, draft a fix plan, and open a **human-reviewed** PR that resolves the issue or implements the request (label-gated, treats issue text as untrusted, never auto-merged)

---

## 🙌 Credits

Built on protocol research from the Home Assistant Traeger integration community
([sebirdman](https://github.com/sebirdman/hass_traeger), [lymanepp](https://github.com/lymanepp/ha-traeger), [johnvoipguy](https://github.com/johnvoipguy/Traeger-WiFire)). Huge thanks to those maintainers.

## 📄 License

[MIT](LICENSE) © 2026 ctopherwilliams. Not affiliated with Traeger Inc.
