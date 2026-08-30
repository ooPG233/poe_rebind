# poe_rebind

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

## Current Progress — Steps 2-4 (Implemented)

1. Log into PoE with the credentials from `poe.env` (or `--poe-email` /
   `--poe-password`) — typed char-by-char with human-like delays.
2. Unlink the old Twitch binding (`button[value='twitch_remove']`), skipped
   automatically when nothing is linked.
3. Wipe Twitch-domain cookies/localStorage via CDP
   (`Storage.clearDataForOrigin`) so the residual session cannot silently
   re-link the old account — an improvement over the original tool.
4. Click connect (`button[value='twitch_add']`) and wait for the Twitch
   login form (`#login-username` / `#password-input`).

### PoE login captcha findings

- The "reCAPTCHA" on the PoE login form is actually **Cloudflare Turnstile
  in `compat=recaptcha` mode** (`turnstile/v0/api.js?compat=recaptcha`).
- The visual widget renders inside a **shadow root** — light-DOM iframe
  selectors cannot see it. `solve_login_turnstile()` walks shadow roots to
  locate the iframe, falling back to the `.sign-in__captcha` container rect,
  then clicks the checkbox by viewport coordinates.
- Precise clicking is vision-based: `locate_checkbox_visual()` screenshots
  the viewport and finds the checkbox via Canny edge detection (the dark
  theme border is too low-contrast for adaptive thresholding), restricted
  to squares strictly inside the captcha container rect, taking the
  leftmost candidate (before the label text and the Cloudflare logo).
  Verified: vision located the checkbox at (492,579) vs the imprecise
  container anchor (501,580) that only had a 6px margin inside the box.
- **Captcha handling is gated on DOM signatures** (a captcha does not appear
  on every login): first check for `.sign-in__captcha` / `.recaptcha`
  containers, hidden `cf-turnstile-response` inputs, and the turnstile
  script tag. Only when a signature exists does the solver enter the
  shadow-DOM piercing + coordinate-click path; when no signature is found
  the login submits directly. Login success is confirmed by rendered page
  content (the Twitch add/remove button), never by the URL alone — a failed
  login transiently flashes the target URL during the redirect chain.
- Verified end-to-end (2026-08-30): checkbox click at the container anchor
  produced an 837-char token, login succeeded, the old Twitch binding was
  removed, Twitch storage was wiped, and the flow stopped at the Twitch
  OAuth login form as designed.
- **Network requirement**: on a flagged/datacenter exit IP the invisible
  Turnstile silently refuses to issue a token (POST returns 302 but the
  session is never established and the page bounces back with "Please
  complete the reCAPTCHA"). A clean residential proxy is required — this is
  exactly why the original tool's instructions say "自己弄好网络".
- **Do not hammer the login**: repeated failed submissions escalate the IP
  risk score at Cloudflare and make the token refusal worse. The script only
  re-submits after a real Turnstile token has been observed; never blindly.
- Use `--proxy http://127.0.0.1:7890` to route the browser through your own
  proxy.

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

## License

This project is licensed under the **GNU General Public License v3.0 or
later** ([GPL-3.0-or-later](LICENSE)). Any derivative work that is distributed
must be released under the same license and with source code available.

## Known Issues & Fixes

- nodriver 0.50.3: `evaluate()` conflicts `serialization_options` with
  `returnByValue`, wrapping object results in a RemoteObject. This project
  consistently returns `JSON.stringify(...)` strings from JS and parses them.
- nodriver shutdown: a race in `Connection.aclose` raises AttributeError.
  A safe patch (local socket reference) is applied at startup.
- The CF Turnstile template is English-only: the browser launches with
  `--lang=en-US` so the widget language matches.
- Browser isolation: disables extensions, sync and browser sign-in so Edge
  never inherits the account or opens plugin post-install pages.
