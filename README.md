<p align="center">
  <img src="assets/banner.svg" alt="Pellet Pilot" width="100%">
</p>

<p align="center">
  <b>Real-time telemetry, live probe trend lines, cook ETAs, and temperature alarms for WiFi pellet grills.</b><br>
  <sub>Pull grill &amp; probe temps straight from the cloud · analyze your cook right in the terminal.</sub>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
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

## 🔐 Credentials — three secure options

Your password is **never** stored in the repo, in code, or in plaintext logs. Pick the level that suits you (resolution order shown):

| # | Source | How | Security |
|---|--------|-----|----------|
| 1 | **Bitwarden** | Unlock your vault into a local session; set `TRAEGER_BW_ITEM` | 🟢 Best — pulled in-memory at runtime |
| 2 | **macOS Keychain** | `security add-generic-password -s pellet-pilot -a you@email -w` | 🟢 Encrypted at rest |
| 3 | **`.env` file** | `TRAEGER_PASSWORD=...` (gitignored) | 🟡 Fine on a private machine |

See [SECURITY.md](SECURITY.md) for the full model and what is *never* written to disk.

---

## 🧩 How it works

```
┌────────────┐  1. Cognito login (email+pass)   ┌──────────────────────┐
│  poll.py   │ ───────────────────────────────► │  Grill cloud (AWS)   │
│            │ ◄─────────────  IdToken           │  Cognito + IoT MQTT  │
│            │  2. GET /users/self  (your grill) │                      │
│  trend.py  │  3. POST /mqtt-connections        │                      │
│            │  4. MQTT-over-WSS  ◄── live status│                      │
└─────┬──────┘                                    └──────────────────────┘
      │ appends
      ▼
  cook_log.csv  ──►  trend.py  ──►  rate · ETA · stall · sparkline
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

---

## 🙌 Credits

Built on protocol research from the Home Assistant Traeger integration community
([sebirdman](https://github.com/sebirdman/hass_traeger), [lymanepp](https://github.com/lymanepp/ha-traeger), [johnvoipguy](https://github.com/johnvoipguy/Traeger-WiFire)). Huge thanks to those maintainers.

## 📄 License

[MIT](LICENSE) © 2026 ctopherwilliams. Not affiliated with Traeger Inc.
