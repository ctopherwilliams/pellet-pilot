# Pellet Pilot — Product Build Handoff

**For:** the LLM/agent implementing features on the repo  
**Source of truth:** https://github.com/ctopherwilliams/pellet-pilot (always `git pull origin main`)  
**Goal:** become the most popular Traeger open-source repo — pitmasters star it because it solves mid-cook decisions, not because it controls the grill.

---

## North star

**Positioning (use everywhere):**  
> *The pitmaster copilot Traeger won't build — tells you when to wrap, when it's done, and remembers every cook.*

**Tagline options (pick one for README hero):**
- *"Your grill forgets. Pellet Pilot doesn't."*
- *"Numbers from the app. Decisions from the curve."*
- *"Wrap at 165. Rest at 203. We'll do the math."*

**Hard constraints (never violate):**
- **Read-only grill access** — command `90` (status refresh) only. No start/stop/set-temp.
- **Terminal-first** — no full mobile app; optional thin wrappers (menubar) later.
- **Local-first** — cook data stays on disk (`cook_log.csv`); no required cloud.
- **Minimal diffs** — match existing code style; extend `tests/smoke.py` for behavior changes.

---

## What's already shipped (do not rebuild)

- Live poll + `--watch` loop with refresh-token re-auth
- ETA / stall-aware forecast (`forecast.py`)
- Multi-probe + multi-stage cooks (`--stage`, `.cook_plan.json`)
- `history.py`, `plot.py` (incl. probe forecast chart), `export.py`
- Remote alarms: Pushover, ntfy, webhook (`alarms.py`)
- `--speak` every-tick (macOS)
- Security hardening (PR #7+)

**Gap:** features exist but onboarding is hard (10 separate `.py` scripts). Tier 1 fixes that.

---

## Build order (3 features — ship in this sequence)

### Feature 1: `pellet` unified CLI + cook presets
### Feature 2: Cook Report (shareable HTML)
### Feature 3: Wrap Coach (rule-based coaching line in `poll.py` / `trend.py`)

After each feature: update README, add smoke tests, keep `bandit`/`pip-audit` clean.

---

# Feature 1 — `pellet` CLI + cook presets

## Problem
Users must memorize `poll.py --watch 30 --stage 165:wrap --stage 203:done`. Persona "Brisket Saturday" bounces.

## Solution
Single entry point `pellet` (or `python -m pellet_pilot`) with named cook presets.

## CLI surface

```bash
# Primary UX — one command starts a full cook session
pellet cook brisket              # preset: watch 30s, stages, log, alarms
pellet cook pork-shoulder
pellet cook turkey
pellet cook ribs
pellet cook chicken

# Overrides
pellet cook brisket --watch 60
pellet cook brisket --probe 2     # if preset supports it
pellet cook custom --stage 165:wrap --stage 203:done   # escape hatch

# Existing tools become subcommands (thin wrappers — delegate to existing modules)
pellet watch [--watch 30] [...]  # alias for poll.py --watch
pellet status                    # one-shot poll.py
pellet trend [--probe N] [--window M]
pellet history list|show|summary
pellet plot [--html] [--out path]
pellet report [--cook N]         # Feature 2
```

## Implementation sketch

```
pellet_pilot/
  __main__.py          # entry point: python -m pellet_pilot
  cli.py               # argparse subcommands
  presets/
    brisket.yaml
    pork-shoulder.yaml
    turkey.yaml
    ribs.yaml
    chicken.yaml
```

**OR** (simpler, matches current flat layout):
- Add `pellet.py` at repo root
- Add `presets/` directory with YAML files
- `pyproject.toml` with `[project.scripts] pellet = "pellet:main"` for `pip install -e .`

Prefer **minimal new structure** — a `pellet.py` + `presets/*.yaml` is fine if it matches repo style.

## Preset file format (`presets/brisket.yaml`)

```yaml
name: brisket
description: "Low & slow packer brisket — wrap in peach paper at 165°, pull at 203°"
watch_interval: 30
stages:
  - temp: 165
    label: wrap
  - temp: 203
    label: done
alarms: []              # stages auto-arm; optional extra thresholds
notes: "Expect 10-16h. Stall 150-170° is normal."
speak: false            # maps to --speak if true
```

## Built-in presets (ship these 5)

| Preset | Stages | Watch | Notes |
|--------|--------|-------|-------|
| `brisket` | 165 wrap → 203 done | 30s | Default hero demo |
| `pork-shoulder` | 165 wrap → 205 done | 30s | Similar stall behavior |
| `ribs` | 175 done | 60s | Shorter cook, less frequent poll OK |
| `turkey` | 150 check → 165 done | 60s | Breast safety gate |
| `chicken` | 165 done | 45s | Shorter cook |

User overrides: `~/.pellet-pilot/presets/<name>.yaml` merges over bundled presets (document in README).

## `pellet cook` behavior

1. Load preset YAML (user dir overrides bundled)
2. Build equivalent of:
   ```
   poll.py --watch <interval> --stage <stages...> [--speak] [--alarm ...]
   ```
3. Print preset `description` + `notes` at start
4. Optionally write `cook_meta.json` (see Feature 2) with `preset`, `started_at`, `cut`

## Acceptance criteria

- [ ] `pellet cook brisket` runs identical session to manual `--stage` flags
- [ ] `pellet --help` lists all subcommands
- [ ] `pip install -e .` installs `pellet` console script (add minimal `pyproject.toml`)
- [ ] Smoke test: preset loads, stages parse, delegates to poll without network
- [ ] README Quickstart becomes 3 lines: clone → install → `pellet cook brisket`

---

# Feature 2 — Cook Report (shareable HTML)

## Problem
Users want to post cook results to r/pelletgrills, Discord, iMessage. Traeger app has no export. Viral loop = stars.

## Solution
`pellet report` generates a **single self-contained HTML file** (no CDN, no external JS) with embedded SVG chart + cook stats.

## CLI

```bash
pellet report                    # latest session → cook-report.html
pellet report --cook 2           # specific session (history.py index)
pellet report --out my-brisket.html
pellet report --open             # macOS: open file after write (optional)
```

## Output file structure

Self-contained HTML. Inline CSS. Embed the SVG from `plot.py` (probe forecast chart if `--probe 1` data exists).

### Report sections (top to bottom)

```
┌─────────────────────────────────────────────────────────┐
│  🔥 Pellet Pilot Cook Report                            │
│  Brisket · July 4, 2026 · 14h 22m total                │
├─────────────────────────────────────────────────────────┤
│  [EMBEDDED SVG CHART — probe temp + grill + stages]     │
├─────────────────────────────────────────────────────────┤
│  SUMMARY                                                │
│  • Started 6:12 AM → Finished 8:34 PM                   │
│  • Probe peak: 203° · Grill peak: 275°                  │
│  • Stall: 2h 18m in 150–175° band (detected)            │
│  • Rise rate (post-stall): +1.2°/min avg                │
├─────────────────────────────────────────────────────────┤
│  STAGES                                                 │
│  ✓ Wrap  165° reached 1:47 PM (4h 35m in)              │
│  ✓ Done  203° reached 8:34 PM                        │
├─────────────────────────────────────────────────────────┤
│  NOTES (from cook_meta.json if present)                 │
│  "Prime brisket, peach paper, Aaron Franklin method"    │
├─────────────────────────────────────────────────────────┤
│  footer: generated by Pellet Pilot · github link        │
│  disclaimer: unofficial, not affiliated with Traeger    │
└─────────────────────────────────────────────────────────┘
```

## Visual design (match existing brand)

- Background: `#161310` (same as `plot.py` SVG)
- Accent: `#ff8c1a` (Traeger-adjacent orange, already in COLORS)
- Text: `#d8cbbf` body, `#8a7d70` muted
- Font: `ui-sans-serif, system-ui` — no web fonts (offline-safe)
- Max width: 800px, centered
- Mobile-readable (pitmasters check phone in backyard)

## Data sources

- `history.py` — session split, summarize, stage timestamps
- `plot.py` — `render_svg()` or probe forecast variant
- `forecast.py` — stall duration detection (time in STALL_LO–STALL_HI with rate < 0.05°/min)
- `cook_meta.json` (new, gitignored) — optional user notes, preset name, post-cook rating

## `cook_meta.json` schema (gitignore it)

```json
{
  "preset": "brisket",
  "cut": "brisket",
  "started_at": "2026-07-04T06:12:00",
  "note": "Prime, peach paper",
  "rating": null
}
```

Written by `pellet cook` at session start. Optional `--rate 5` on `pellet report` to fill rating post-cook.

## Acceptance criteria

- [ ] HTML opens offline, no external requests
- [ ] SVG validates (reuse smoke test pattern: `minidom.parseString`)
- [ ] Report includes stage timestamps when `.cook_plan.json` was used
- [ ] Stall duration computed and shown when applicable
- [ ] Smoke test: synthetic `cook_log.csv` rows → report HTML contains key strings
- [ ] README: screenshot placeholder + "share your cook report" section

---

# Feature 3 — Wrap Coach (rule-based)

## Problem
Stall detection says *what*. Users want *what to do*: "wrap now or wait?"

## Solution
One coaching line printed in `poll.py` forecast output and `trend.py`, rule-based from **user's own history** (no ML v1).

## Output examples

```text
  🧠 WRAP COACH: stalled 2h 14m at 153°. In your last 3 briskets, wrapping near
     here saved ~1.8h. → Recommendation: WRAP NOW (based on your cooks).
```

```text
  🧠 WRAP COACH: rising through 158° at +0.4°/min — stall may not have started.
     → Recommendation: WAIT (no wrap yet).
```

```text
  🧠 WRAP COACH: no past brisket cooks in your log yet. General rule: if flat
     150-170° for 2+ hours, wrap to push through. → Recommendation: CONSIDER WRAP.
```

## Logic (implement in new `coach.py`)

```python
def wrap_coach(current_temp, rate, stall_duration_min, history_sessions, cut="brisket"):
    """
    Returns: {recommendation: "WRAP NOW"|"WAIT"|"CONSIDER WRAP",
              reason: str, confidence: "high"|"medium"|"low"}
    """
```

**Rules (v1):**
1. If `rate >= 0.3` and `temp < 150`: WAIT
2. If `150 <= temp <= 175` and `rate < 0.05` and `stall_duration >= 90min`:
   - If history has ≥2 same-cut cooks with wrap stage: compare time-to-done wrapped vs not → recommend WRAP NOW (medium confidence)
   - Else: CONSIDER WRAP with general rule text (low confidence)
3. If `temp > 175` or `rate >= 0.2` in plateau band: WAIT (past stall or recovering)
4. Always append disclaimer in docstring/README: *recommendation, not guarantee*

**History mining:** use `history.py` sessions + `summarize()` + stage data from logs. Match on `cook_meta.json` preset/cut if present, else infer from stage labels containing "wrap".

## Integration points

- `poll.py` `print_forecasts()` — append coach line when in stall band
- `trend.py` — same when status is `stalled`
- `pellet report` — include coach summary in HTML if stall occurred
- Env kill-switch: `PELLET_PILOT_COACH=0` to disable

## Acceptance criteria

- [ ] Coach line never crashes if history is empty (fallback to general rule)
- [ ] Coach never calls network
- [ ] Smoke tests for: no history, stalled + history, not stalled
- [ ] README "Wrap Coach" section with example output

---

# README additions (ship with Feature 1)

## "Pellet Pilot vs Traeger app" table

| | Traeger app | Pellet Pilot |
|---|---|---|
| Live temp | ✅ | ✅ |
| **Temp history after cook** | ❌ gone forever | ✅ `cook_log.csv` forever |
| **"When will it be done?"** | ❌ | ✅ live ETA, stall-aware |
| **Wrap / done stage alerts** | ❌ one threshold | ✅ labeled multi-stage |
| **Stall coaching** | ❌ | ✅ Wrap Coach |
| **Shareable cook report** | ❌ | ✅ HTML export |
| **Works in terminal / scripts** | ❌ | ✅ pipe, grep, Grafana |
| Remote start/stop | ✅ | ❌ intentionally (read-only) |
| Official / supported | ✅ | ❌ community tool |

## New Quickstart (replace or supplement existing)

```bash
git clone https://github.com/ctopherwilliams/pellet-pilot.git && cd pellet-pilot
python3 -m venv venv && ./venv/bin/pip install -e .
cp .env.example .env   # Traeger app email + password

./venv/bin/pellet cook brisket   # that's it — logs, predicts, alerts on wrap & done
```

## 30-second README GIF (human records; LLM documents placeholder)

Show: terminal `pellet cook brisket` → live ETA lines → `WRAP IT` notification → `pellet report` → HTML chart.

---

# Updated roadmap (replace completed checklist)

```markdown
## Roadmap

### Now
- [ ] `pellet` CLI + cook presets (brisket, pork, turkey, ribs, chicken)
- [ ] Cook Report — shareable self-contained HTML
- [ ] Wrap Coach — rule-based wrap/wait recommendation from your cook history

### Next
- [ ] Cook Library — tags, compare sessions, personal bests (`pellet compare brisket`)
- [ ] Home Assistant read-only sensor bridge (document + export; don't fork ha-traeger)
- [ ] Grafana dashboard template (`grafana/pellet-pilot.json`)
- [ ] Voice pacing — `--speak` every N minutes, not every tick

### Later
- [ ] Menubar glance (macOS) — ETA without terminal
- [ ] Ambient-adjusted ETA (outdoor temp from log)
- [ ] Community cook pack registry (PR-based `presets/`)
```

---

# Testing requirements (all features)

- Extend `tests/smoke.py` — no network mocks for coach/report/preset parsing
- `bandit -c bandit.yaml -r . -ll` clean
- `pip-audit -r requirements.txt` clean
- Preserve read-only design — grep for command codes, only `"90"` allowed

---

# Files likely touched

| Feature | New/modified files |
|---------|-------------------|
| CLI + presets | `pellet.py`, `presets/*.yaml`, `pyproject.toml`, `README.md`, `tests/smoke.py`, `.gitignore` (+ `cook_meta.json`) |
| Cook Report | `report.py` (or `pellet report` in `pellet.py`), `cook_meta.json` schema, `README.md`, `tests/smoke.py` |
| Wrap Coach | `coach.py`, `poll.py`, `trend.py`, `report.py`, `tests/smoke.py`, `README.md` |

---

# Personas (build for these)

1. **Brisket Saturday** — wants `pellet cook brisket`, wrap alert, done ETA → presets + coach
2. **Set and forget** — wants ntfy/Pushover when done → preset arms stages + document env vars
3. **Data nerd** — wants history, plots, Grafana → report + export (already have base)
4. **Claude Code user** — conversational babysitting → README Option B (already strong; add preset names Claude can invoke)

---

# What NOT to build

- Remote grill control (start/stop/set temp)
- Full mobile app
- ML "perfect brisket" model
- In-app social network / cook feed
- Cloud account / telemetry upload (local-first)

---

# Success metrics (3 months post-ship)

- README has GIF + vs-app table + 3-line quickstart
- `pellet cook brisket` is the documented primary command
- At least 5 bundled presets + user override path documented
- Cook Report HTML shared in README example
- GitHub Discussions or issue template: "Share your cook report"

---

# Suggested PR stack (Graphite-style or sequential)

1. `feat/pellet-cli-presets` — pyproject.toml, pellet.py, presets/, smoke tests, README quickstart
2. `feat/cook-meta` — cook_meta.json write on cook start, gitignore
3. `feat/cook-report` — report HTML, smoke test, README screenshot section
4. `feat/wrap-coach` — coach.py, poll/trend integration, report section
5. `docs/product-positioning` — vs-app table, roadmap update, GIF placeholder

Merge order: 1 → 2 → 3 → 4 → 5 (or 1+2 together, then 3, then 4).

---

*End of handoff. Questions about Traeger protocol → read `traeger_client.py` and SECURITY.md. Questions about security → read SECURITY.md and run `tests/smoke.py`.*