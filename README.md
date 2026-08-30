# poe-twitch-rebind

> [!WARNING]
> **FOR EDUCATIONAL AND RESEARCH PURPOSES ONLY — DISCLAIMER**
>
> This project is intended solely for learning, research, and personal study
> of browser automation and anti-bot techniques. It is **NOT** affiliated with,
> endorsed by, or approved by Grinding Gear Games, Path of Exile, or Twitch.
>
> Using automated tools to interact with game websites and services may violate
> their Terms of Service and may result in **permanent account bans or other
> penalties**. The authors and contributors of this project assume **NO
> responsibility and NO liability** for any account bans, suspensions, data
> loss, or other consequences arising from the use or misuse of this software.
>
> Use at your own risk. You are solely responsible for your own accounts and
> actions.

Path of Exile × Twitch account auto-rebind tool. Rebuilt from scratch on the
tech stack recovered by reverse-engineering the original binary.

## Tech Stack

| Layer | Technology |
|---|---|
| Language / runtime | Python 3.13 + asyncio |
| Browser automation | nodriver 0.50.3 (real Chrome via CDP, no webdriver fingerprint) |
| WebSocket | websockets 17.1 (nodriver ↔ Chrome DevTools communication) |
| Screenshots | mss 10.2.0 (Cloudflare template capture) |
| CF template matching | opencv-python-headless (verify_cf checkbox location) |
| Config | poe.env + argparse CLI overrides |
| Packaging | PyInstaller (later steps) |

## Project Structure

```
poe_rebind/
├── rebind.py         # main program (single file, like the original)
├── poe.env           # PoE account config
├── requirements.txt
├── pyproject.toml
└── README.md
```

## Current Progress — Step 1 (Done)

1. Launch a real Chrome (Edge as fallback).
2. Open https://www.pathofexile.com/login/email.
3. Dismiss the cookie banner automatically.
4. Wait for / pass the Cloudflare challenge (auto-click the Turnstile
   checkbox; fall back to manual verification with a hint).
5. Stop at the email+password form — no input is performed.

Verified (2026-08-30, Edge / Windows): under a full-page
"Just a moment..." challenge, `verify_cf` template matching clicked the
checkbox automatically and the login form appeared within ~10 seconds.

## Usage

```powershell
uv venv --python 3.13 .venv
uv pip install -r requirements.txt
.venv\Scripts\python.exe rebind.py
```

Options:

``+--chrome <path>``      browser executable path
``--poe-env <path>``     config file (default: poe.env)
``--poe-email <email>``  PoE account email
``--poe-password <pw>``  PoE password
``--verbose``            debug logging
``--no-hold``            exit right after reaching the login page (testing)
``--screenshot <path>``  save a screenshot when using ``--no-hold``

## Roadmap

- Step 2: fill in the PoE email/password and log in
- Step 3: unlink the old Twitch (`button[value='twitch_remove']`)
- Step 4: link a new Twitch (OAuth: auth.twitch.tv/authorize)
- Step 5: batch Twitch account rotation + result validation (Drops eligible)
- PyInstaller packaging for release

## Known Issues & Fixes

- nodriver 0.50.3: `evaluate()` conflicts `serialization_options` with
  `returnByValue`, wrapping object results in a RemoteObject. This project
  consistently returns `JSON.stringify(...)` strings from JS and parses them.
- nodriver shutdown: a race in `Connection.aclose` raises AttributeError.
  A safe patch (local socket reference) is applied at startup.
- The CF Turnstile template is English-only: the browser launches with
  `--lang=en-US` so the widget language matches.
