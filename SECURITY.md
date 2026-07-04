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