# Security

Pellet Pilot handles credentials for your Traeger grill account. This document describes the threat model, what stays off disk, and how to report issues.

## Threat model

| Asset | Risk | Mitigation |
|-------|------|------------|
| Traeger account password | Credential theft → account/grill access | Never committed; prefer Keychain/Bitwarden; cleared from memory after Cognito login |
| Cognito IdToken (~1h) | Session hijack → read grill status, issue command `90` (status refresh only) | Held in memory only; auto re-auth on expiry |
| Bitwarden session (`.bw_session`) | Full vault access | Gitignored; warn if file mode ≠ `600` |
| Cook logs (`cook_log.csv`) | Privacy (temps, timing, thing names) | Gitignored |
| Local machine | AppleScript injection via alarm text | User-influenced strings escaped before `osascript` |
| Network path | MITM on MQTT WSS | TLS verification **on by default**; `TRAEGER_INSECURE_TLS=1` only as last resort |

**Blast radius:** This tool is **read-only** against the grill — it polls status and sends command `90` (force status publish). It does **not** expose start/stop/set-temp. A stolen token cannot remotely ignite or change targets through this client.

**Out of scope:** Traeger cloud-side security, grill firmware, physical access, and compliance with Traeger's Terms of Service.

## Remote alarm egress (`alarms.py`)

Optional probe alarms can POST to Pushover, ntfy, or a generic webhook. Controls:

- **HTTPS required** — non-`https://` targets are refused.
- **SSRF guard** on the generic webhook (and ntfy server): the host is resolved and
  the request is refused if any resolved address is private, loopback, link-local
  (incl. cloud metadata `169.254.169.254`), reserved, multicast, or unspecified.
- **No redirects** — a `3xx` cannot bounce the request to an internal host.
- **Config via env only** (`PUSHOVER_*`, `NTFY_TOPIC`, `ALARM_WEBHOOK_URL`); tokens are never logged.
- `ALARM_ALLOW_PRIVATE=1` relaxes the private-IP check for self-hosted LAN targets — opt-in, and it lowers SSRF protection.

## Issue autopilot trust model (`.github/workflows/issue-autopilot.yml`)

An optional workflow drafts fixes for issues. It is designed to be safe on a public repo:

- **Label-gated** — triggers only on the `autofix` / `autofix-approved` labels, and
  applying labels requires triage/write permission, so untrusted issue authors cannot start it.
- **Untrusted input** — issue text is treated as data; the agent is instructed to ignore embedded instructions.
- **Two-phase** — `autofix` posts a plan; `autofix-approved` implements. Human checkpoint between.
- **PR-only, human-reviewed, never auto-merged** — output must pass the required `audit` check and be merged by a human. The auto-merge automation only acts on `dependabot[bot]`.
- **Least privilege** — the job token is limited to `contents`/`pull-requests`/`issues: write`; the model key is a repo secret.

## What is never written to disk or committed

- Account **password** — resolved in-memory at runtime and cleared after login.
- Cognito **IdToken** — memory only.
- Bitwarden **session key** (`.bw_session`), account email (`.env`), and cook logs (`*.csv`) — all gitignored.
- The repo ships only source and the **public** Traeger app Cognito client id (same value used by community integrations).

## Credential resolution order

1. `TRAEGER_PASSWORD` environment variable (explicit override)
2. **Bitwarden** item named by `TRAEGER_BW_ITEM`, via an unlocked `bw` session
3. **macOS Keychain** (`security` service `traeger-wifire`)

The first source that yields a value wins. The program never prompts for or echoes a password.

## Recommendations

- Prefer **Bitwarden** or **Keychain** over a plaintext `.env`.
- If using `.env`: `chmod 600 .env` (owner read/write only).
- Keep `.bw_session` mode `600`; it grants vault access until locked.
- Do not set `TRAEGER_INSECURE_TLS=1` unless TLS verification blocks connectivity on your network.
- Set `PELLET_PILOT_VERBOSE=1` only when debugging credential source — never in shared logs.

## Known limitations

- **MFA:** Accounts with Cognito MFA enabled may fail `USER_PASSWORD_AUTH`. There is no interactive MFA flow.
- **Client id rotation:** Traeger may rotate the mobile app Cognito client id; monitor upstream HA integrations if auth suddenly fails.

## Reporting a vulnerability

Open a **private** security advisory via the repository **Security → Advisories** tab. Do not file public issues for sensitive reports.