# Security

Pellet Pilot handles the credentials for your grill account, so it is built to keep them off disk and out of the repo.

## What is never written to disk or committed

- Your account **password** — resolved in-memory at runtime (Bitwarden / Keychain / env) and never logged.
- Your Bitwarden **session key** (`.bw_session`), account **email** (`.env`), and **cook logs** (`*.csv`) are all gitignored.
- The repo ships only source code and the **public** Traeger app client id (the same value present in every community integration).

## Credential resolution order

1. `TRAEGER_PASSWORD` environment variable (explicit override)
2. **Bitwarden** item named by `TRAEGER_BW_ITEM`, via an unlocked `bw` session
3. **macOS Keychain** (`security` service `pellet-pilot`)

The first source that yields a value wins. If none do, the program exits with instructions — it never prompts for or echoes a password.

## Recommendations

- Prefer **Bitwarden** or **Keychain** over a plaintext `.env`.
- Keep `.bw_session` mode `600` (owner-only); it grants vault access until locked.
- This tool is **read-only** against the grill (status polling only — no remote start/stop), which limits blast radius if credentials were ever misused.

## Reporting a vulnerability

Open a private security advisory via the repository's **Security → Advisories** tab, or email the maintainer. Please do not file public issues for sensitive reports.
