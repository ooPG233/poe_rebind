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

Path of Exile × Twitch account auto-rebind tool: log into PoE, rotate the
linked Twitch account through a list, and verify each binding.

## Usage

### Packaged executable (recommended for end users)

1. Download and extract `poe_rebind_vX.Y.Z_win64.zip`.
2. Configure credentials (see below).
3. Double-click `poe_rebind.exe`.

No Python installation required. Chrome or Edge must be present on the
system (auto-detected).

### Run from source

```powershell
uv venv --python 3.13 .venv
uv pip install -r requirements.txt
.venv\Scripts\python.exe rebind.py
```

### Configuration

**`poe.env`** (optional — if absent, the browser opens the PoE login page
and waits for you to log in manually, up to 10 minutes):

```ini
POE_EMAIL=your@example.com
POE_PASSWORD=your-password
```

**`twitch_accounts.txt`** (required) — one account per line, `username:password`.
The first colon separates; colons inside passwords are safe. Blank and `#`
lines are skipped. See `twitch_accounts.example.txt` for reference.

Both files are gitignored — never commit real credentials.

### CLI options

| Flag | Description |
|------|-------------|
| `--chrome <path>` | Browser executable (auto-detects Chrome, then Edge) |
| `--poe-env <path>` | PoE config file (default: `poe.env`) |
| `--poe-email <email>` | Override PoE email |
| `--poe-password <pw>` | Override PoE password |
| `--twitch-file <path>` | Twitch account list (default: `twitch_accounts.txt`) |
| `--proxy <url>` | HTTP/SOCKS proxy (e.g. `http://127.0.0.1:7890`) |
| `--verbose` | Debug logging |
| `--no-hold` | Exit after the batch instead of keeping the browser open |
| `--screenshot <path>` | Save a final screenshot when using `--no-hold` |

## Development

### Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Chrome or Edge

### Setup

```powershell
git clone <repo-url>
cd poe_rebind
uv venv --python 3.13 .venv
uv pip install -r requirements.txt
```

### Build the executable

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller poe_rebind.spec --noconfirm
```

Output goes to `dist\poe_rebind\`. Create a release zip:

```powershell
Compress-Archive -Path dist\poe_rebind -DestinationPath poe_rebind_v0.1.0_win64.zip -Force
```

### Known issues

- nodriver 0.50.3: `evaluate()` conflicts `serialization_options` with
  `returnByValue`. This project returns `JSON.stringify(...)` strings from JS
  and parses them.
- nodriver shutdown: a race in `Connection.aclose` raises AttributeError.
  A safe patch (local socket reference) is applied at startup.
- The CF Turnstile template is English-only: the browser launches with
  `--lang=en-US` so the widget language matches.

## License

This project is licensed under the **GNU General Public License v3.0 or
later** ([GPL-3.0-or-later](LICENSE)). Any derivative work that is distributed
must be released under the same license and with source code available.
