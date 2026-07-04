# Security

Pellet Pilot handles credentials for your Traeger grill account. This document describes the threat model, what stays off disk, and how to report issues.

## Threat model

| Asset | Risk | Mitigation |
|-------|------|------------|
| Traeger account password | Credential theft → account/grill access | Never committed; prefer Keychain/Bitwarden; cleared from memory after Cognito login; popped from `os.environ` once read so it isn't inherited by child subprocesses |
| Cognito IdToken (~1h) | Session hijack → read grill status, issue command `90` (status refresh only) | Held in memory only; renewed via **refresh token** on expiry (no password re-sent) during `--watch`, with backoff if renewal keeps failing |
| Bitwarden session (`.bw_session`) | Full vault access | Gitignored; warn if file mode ≠ `600`; session key passed to `bw` via `BW_SESSION` env, not `--session` argv (avoids exposure via `ps`/procfs) |
| Cook logs (`cook_log.csv`) | Privacy (temps, timing, thing names) | Gitignored |
| Local machine | AppleScript injection via alarm text | User-influenced strings stripped of control characters (incl. newlines) and escaped before `osascript` |
| Network path | MITM on MQTT WSS | TLS verification **on by default**; `TRAEGER_INSECURE_TLS=1` only as last resort |
| Grill identifier (`thingName`, from the Traeger API) | Path/MQTT-topic injection if the upstream API ever returned an unexpected value | Validated against an alphanumeric pattern before use in any URL path or MQTT topic |

**Blast radius:** This tool is **read-only** against the grill — it polls status and sends command `90` (force status publish). It does **not** expose start/stop/set-temp. A stolen token cannot remotely ignite or change targets through this client.

**Out of scope:** Traeger cloud-side security, grill firmware, physical access, and compliance with Traeger's Terms of Service.

## Remote alarm egress (`alarms.py`)

Optional probe alarms can POST to Pushover, ntfy, or a generic webhook. Controls:

- **HTTPS required** — non-`https://` targets are refused.
- **SSRF guard** on the generic webhook (and ntfy server): the host is resolved and
  the request is refused if any resolved address is private, loopback, link-local
  (incl. cloud metadata `169.254.169.254`), reserved, multicast, or unspecified.
  The validated IP is then **pinned for the actual connection**, so a DNS answer
  that changes between the check and the request (a rebind) can't bypass the guard.
- **No redirects** — a `3xx` cannot bounce the request to an internal host.
- **Config via env only** (`PUSHOVER_*`, `NTFY_TOPIC`, `ALARM_WEBHOOK_URL`); tokens are never logged.
- `ALARM_ALLOW_PRIVATE=1` relaxes the private-IP check for self-hosted LAN targets — opt-in, and it lowers SSRF protection.
- **Sanitized before send** — title/message are stripped of control characters (incl. newlines) at the `notify_remote()` choke point before reaching any provider. This matters most for ntfy, which puts `title` in an HTTP header.

## Local exports & endpoints

- **`export.py --serve`** binds a Prometheus `/metrics` endpoint to `127.0.0.1` only — no external network exposure, so it intentionally has no authentication. Don't put it behind a reverse proxy without adding auth of your own.
- **`.cook_plan.json`** (`plan.py`) is capped at 256KB on load as a sanity check — a real plan is a few dozen bytes. It's written only by this tool's own `--stage` flow, so this is a defensive ceiling, not a response to an untrusted-input path.
- **MQTT topic handling** verifies the `prod/thing/update/` prefix before parsing a message, rather than assuming it.

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
- **Refresh-token fallback for `env`-sourced passwords:** `--watch` renews its session via refresh token (no password needed) and only falls back to a full re-login if that's ever rejected. If your password source is a plain `TRAEGER_PASSWORD` env var, it's popped from the environment right after the first login (see the table above), so that fallback has nothing to re-resolve for an `env`-only setup. This is expected to be rare in practice — Cognito refresh tokens comfortably outlive a single cook — but if you want a `--watch` cook to survive a refresh-token failure too, use Bitwarden or Keychain instead of `.env`/`TRAEGER_PASSWORD`.

## Reporting a vulnerability

Open a **private** security advisory via the repository **Security → Advisories** tab. Do not file public issues for sensitive reports.