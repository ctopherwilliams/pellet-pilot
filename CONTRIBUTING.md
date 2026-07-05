# Contributing to Pellet Pilot

Thanks for your interest! This is an unofficial, community project — see the disclaimer in the [README](README.md).

## How to contribute

This repo is public and takes contributions from anyone via the standard GitHub flow — no special access needed:

1. **Fork** the repo (button top-right on GitHub).
2. **Clone your fork** and create a branch: `git checkout -b my-change`.
3. Make your change, following the ground rules and checklist below.
4. Push to your fork and **open a pull request** against `ctopherwilliams/pellet-pilot:main`.
5. CI (`tests/smoke.py`, `pip-audit`, `bandit`) runs automatically against your PR. If this is your first PR here, GitHub may hold the workflow run for a maintainer to approve before it starts — that's a standard GitHub anti-abuse default, not a rejection.
6. If you're not already a collaborator, an **automated AI review** posts one advisory comment on your PR — a second pair of eyes before a maintainer looks. It never approves or merges anything; a maintainer still makes the actual merge decision, same as any other PR here.

Only maintainers can push branches directly to this repository — that's normal for a public repo, and the fork-based flow above doesn't need write access at all.

## Ground rules

- **Never** commit secrets: no Traeger passwords, `.env`, `.bw_session`, `cook_log.csv`, tokens, signed URLs, or device thing-names. `.gitignore` covers the common cases; double-check your diff.
- **Keep it read-only.** This client only reads grill status (`command:"90"`). PRs that add start/stop/set-temp or other control commands will not be merged — that's a deliberate safety boundary.
- Report **security vulnerabilities privately** via [Security Advisories](https://github.com/ctopherwilliams/pellet-pilot/security/advisories/new), not public issues. See [SECURITY.md](SECURITY.md).

## Dev setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install pip-audit bandit
```

## Before opening a PR

Run what CI runs:

```bash
python tests/smoke.py                     # full regression suite (no network needed)
pip-audit -r requirements.txt             # dependency CVEs
bandit -c bandit.yaml -r . -ll            # static analysis, whole repo
```

- Match the surrounding code style (stdlib-only where practical, small focused functions).
- Update docs (`README.md`, `SECURITY.md`) when behavior changes.
- Add/update a test in `tests/smoke.py` for any new or changed behavior.
- One logical change per PR.

## Dependencies

Dependency and GitHub Actions updates are handled by Dependabot and auto-merge on green CI. Please don't bump pinned versions manually unless a change requires it.
