# Contributing to Pellet Pilot

Thanks for your interest! This is an unofficial, community project — see the disclaimer in the [README](README.md).

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
python -c "import traeger_client, poll, trend"          # import smoke test
pip-audit -r requirements.txt                           # dependency CVEs
bandit -c bandit.yaml -r poll.py traeger_client.py trend.py -ll   # static analysis
```

- Match the surrounding code style (stdlib-only where practical, small focused functions).
- Update docs when behavior changes.
- One logical change per PR.

## Dependencies

Dependency and GitHub Actions updates are handled by Dependabot and auto-merge on green CI. Please don't bump pinned versions manually unless a change requires it.
