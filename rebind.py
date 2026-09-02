# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 poe_rebind contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file for details.
"""PoE Twitch rebind tool - auto rebind Twitch accounts on the PoE account.

Stack: Python 3.13 + asyncio + nodriver (real Chrome via CDP).
Goals:
  1. Launch a real Chrome (or Edge) and pass Cloudflare.
  2. Log into the PoE account (email + password).
  3. Unlink the old Twitch binding (twitch_remove).
  4. Clear Twitch-domain storage, click connect (twitch_add).
  5. Log into Twitch with each account from twitch_accounts.txt, authorize
     the OAuth prompt and verify the binding — looping until the list is
     exhausted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import nodriver as uc

LOG = logging.getLogger("rebind")


def app_dir() -> Path:
    """Directory where the script or frozen exe lives.

    When packaged with PyInstaller, ``__file__`` points to a temporary
    extraction directory; ``sys.executable`` is the real location instead.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class _ColorFormatter(logging.Formatter):
    """Colourise the level name for console output (file stays plain)."""

    _COLORS = {
        logging.DEBUG:    "\033[36m",    # cyan
        logging.INFO:     "\033[32m",    # green
        logging.WARNING:  "\033[33m",    # yellow
        logging.ERROR:    "\033[31m",    # red
        logging.CRITICAL: "\033[1;41m",  # bold white on red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        orig = record.levelname
        color = self._COLORS.get(record.levelno, "")
        if color:
            record.levelname = f"{color}{orig}{self._RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = orig


def _enable_ansi() -> None:
    """Enable ANSI escape sequences on Windows legacy consoles."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def _setup_logging(verbose: bool = False) -> Path:
    """Configure console + file logging.

    Console: INFO (or DEBUG with --verbose), human-friendly short timestamps.
    File   : INFO by default, DEBUG with --verbose; full timestamps + module
             name, written next to the script so tracebacks survive after the
             console closes.
             Rotates at 5 MB, keeps 3 backups (rebind.log.1 ~ .3), so total
             disk usage is capped at ~20 MB.
    Returns the log file path.
    """
    log_dir = app_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "rebind.log"
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    root.handlers.clear()

    console_fmt = _ColorFormatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _enable_ansi()
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(console_fmt)
    root.addHandler(console)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(file_fmt)
    root.addHandler(fh)

    return log_file


# --- nodriver compat patch --------------------------------------------------


def _patch_nodriver_shutdown_race() -> None:
    """Fix a shutdown race in nodriver 0.50.3.

    Two concurrent aclose() tasks can interleave: the first sets self.socket
    to None, the second then calls wait_closed() on it and raises
    AttributeError. Take a local reference before awaiting.
    """
    from nodriver.core import connection as _conn

    if getattr(_conn.Connection, "_patched_safe_aclose", False):
        return

    async def _safe_aclose(self):  # noqa: ANN001
        self._fail_pending_futures(ConnectionError("Connection closing"))
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        sock = self.socket
        self.socket = None
        if sock is not None:
            await sock.close()
            with contextlib.suppress(Exception):
                await sock.wait_closed()

    _conn.Connection.aclose = _safe_aclose
    _conn.Connection._patched_safe_aclose = True


# --- Constants (mirroring the original tool) --------------------------------

POE_LOGIN_URL = "https://www.pathofexile.com/login/email?redir=/my-account/connections"

EMAIL_SEL = "input[name='login_email']"
PASSWORD_SEL = "input[name='login_password']"
SUBMIT_SEL = "input.sign-in__submit"

CF_WAIT_SECONDS = 60.0  # max wait for the CF challenge

COOKIE_CONTINUE_SEL = "button#onetrust-accept-btn-handler"

DEFAULT_POE_ENV = app_dir() / "poe.env"

# --- Step 2/3/4: Twitch binding flow (mirroring the original tool) ----------

TWITCH_REMOVE_SEL = "button[value='twitch_remove']"
TWITCH_ADD_SEL = "button[value='twitch_add']"
TWITCH_USER_SEL = "#login-username"
TWITCH_PASS_SEL = "#password-input"
TWITCH_LOGIN_BTN_SEL = "button[data-a-target='passport-login-button']"

TYPE_DELAY_MS = 90.0          # base delay for human-like typing
LOGIN_WAIT_SECONDS = 45.0     # max wait for the PoE login redirect
UNBIND_WAIT_SECONDS = 20.0    # max wait for twitch_remove to take effect
TWITCH_LOGIN_WAIT_SECONDS = 30.0  # max wait for the Twitch login form
AUTHORIZE_WAIT_SECONDS = 30.0     # max wait for the OAuth authorize/redirect
CAPTCHA_DETECT_SECONDS = 8.0  # window to decide whether a login captcha exists

DEFAULT_TWITCH_FILE = app_dir() / "twitch_accounts.txt"

# Suppress the "access your local network" permission bubble — it is
# browser UI that automation cannot click.
LNA_DISABLE_FEATURES = ",".join((
    "LocalNetworkAccessPermissionRequired",
    "LocalNetworkAccessPermission",
    "LocalNetworkAccessChecks",
    "LocalNetworkAccessForNavigations",
    "LocalNetworkAccessForSubframeNavigations",
    "LocalNetworkAccessForWorkers",
))

BACKGROUND_HARDENING_ARGS = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)

# Storage origins wiped before clicking connect, to prevent the residual
# Twitch session from silently re-linking the OLD account.
TWITCH_COOKIE_ORIGINS = (
    "https://twitch.tv",
    "https://www.twitch.tv",
    "https://auth.twitch.tv",
    "https://id.twitch.tv",
    "https://passport.twitch.tv",
)

# Storage.clearDataForOrigin can miss domain-level cookies; delete the
# known Twitch session cookies by name as well (auth-token IS the session).
TWITCH_SESSION_COOKIES = (
    "auth-token",
    "login",
    "persistent",
    "unique_id",
    "twilight-user",
    "device_id",
    "server_session_id",
)
# Avoid Edge account sync / inherited extensions and their post-install tabs.
ISOLATION_BROWSER_ARGS = (
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-sync",
    "--browser-signin=0",
    "--no-first-run",
    "--no-default-browser-check",
)


# --- Models -----------------------------------------------------------------


@dataclass(slots=True)
class PoeAccount:
    email: str
    password: str


@dataclass(slots=True)
class TwitchAccount:
    username: str
    password: str


class RebindError(RuntimeError):
    """Expected business error in the rebind flow."""


# --- Browser discovery ------------------------------------------------------

WINDOWS = os.name == "nt"

CHROME_CANDIDATES = (
    (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    )
    if WINDOWS
    else (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    )
)

EDGE_CANDIDATES = (
    (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    )
    if WINDOWS
    else (
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    )
)


def find_chrome(cli_path: str | None) -> str:
    """Detect Chrome first, then Edge."""
    if cli_path:
        if not Path(cli_path).exists():
            raise RebindError(f"指定的浏览器不存在: {cli_path}")
        return cli_path

    for browser_name, candidates in (("Chrome", CHROME_CANDIDATES), ("Edge", EDGE_CANDIDATES)):
        for p in candidates:
            if Path(p).exists():
                LOG.debug("探测到 %s: %s", browser_name, p)
                return p
    raise RebindError("未找到 Chrome/Edge, 请用 --chrome 指定浏览器路径")


# --- Config -----------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser for POE_EMAIL / POE_PASSWORD."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def resolve_poe(args: argparse.Namespace) -> PoeAccount | None:
    """Resolve credentials: CLI args > env file > environment variables.

    Returns None when credentials are not configured — the caller will
    open the browser and wait for the user to log in manually.
    """
    env = load_env_file(Path(args.poe_env)) if args.poe_env else {}
    email = args.poe_email or env.get("POE_EMAIL") or os.environ.get("POE_EMAIL") or ""
    password = args.poe_password or env.get("POE_PASSWORD") or os.environ.get("POE_PASSWORD") or ""
    if not email or not password:
        return None
    return PoeAccount(email, password)


MANUAL_LOGIN_WAIT_SECONDS = 600.0  # 10 min for the user to log in by hand


async def wait_manual_login(tab: uc.Tab) -> None:
    """Wait for the user to manually log into PoE in the browser.

    Polls until the connections page is actually rendered. Failed login
    attempts (wrong password etc.) are logged as reminders — the user can
    simply correct and retry on the page. Times out after 10 minutes.
    """
    LOG.info("[2/4] 请在浏览器中手动登录 PoE (最长等待 %d 分钟) ...",
             int(MANUAL_LOGIN_WAIT_SECONDS / 60))
    deadline = time.monotonic() + MANUAL_LOGIN_WAIT_SECONDS
    last_error_log = 0.0
    while time.monotonic() < deadline:
        try:
            state = await poe_page_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        url = str(state.get("url") or "")
        body = str(state.get("bodyText") or "")
        now = time.monotonic()

        # Success: connections page with Twitch section rendered.
        if "/my-account/connections" in url and (state.get("hasAdd") or state.get("hasRemove")):
            LOG.info("[2/4] 手动登录成功, 已到达账号连接页")
            return

        # Gentle reminder on failed login — do NOT raise, let the user retry.
        if "/login" in url:
            if re.search(r"incorrect|invalid credentials|failed to sign in", body, re.I):
                if now - last_error_log > 15.0:
                    LOG.warning("[2/4] 检测到登录失败, 请检查邮箱/密码后重试 ...")
                    last_error_log = now

        await asyncio.sleep(1.0)

    raise RebindError("等待手动登录超时 (10 分钟), 请重新运行")


def resolve_twitch_accounts(path: Path) -> list[TwitchAccount]:
    """Parse twitch_accounts.txt: one `username:password` per line.

    - The FIRST colon separates; everything after it is the password.
    - Blank lines and lines starting with '#' are skipped.
    """
    if not path.exists():
        raise RebindError(f"Twitch 账号文件不存在: {path}")
    accounts: list[TwitchAccount] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            raise RebindError(f"{path.name} 第 {lineno} 行格式错误, 需要 username:password")
        username = parts[0].strip()
        password = ":".join(parts[1:]).strip()
        if not username or not password:
            raise RebindError(f"{path.name} 第 {lineno} 行用户名或密码为空")
        accounts.append(TwitchAccount(username, password))
    if not accounts:
        raise RebindError(f"{path.name} 中没有可用账号")
    return accounts


# --- Cloudflare / page waiting ----------------------------------------------

# nodriver 0.50.3: serialization_options conflict with returnByValue, so an
# object result gets wrapped in RemoteObject instead of a dict. Return a JSON
# string instead and parse it here.
PAGE_STATE_JS = """
JSON.stringify((() => ({
    url: location.href,
    title: document.title,
    hasEmail: !!document.querySelector('input[name=\\'login_email\\']'),
    hasPassword: !!document.querySelector('input[name=\\'login_password\\']'),
    cfFrame: !!document.querySelector('iframe[src*=\\'challenges.cloudflare.com\\']'),
}))())
"""


async def page_state(tab: uc.Tab) -> dict:
    """Read the page state in one shot (CF iframe / login form)."""
    return await _eval_json(tab, PAGE_STATE_JS)


async def wait_login_page(tab: uc.Tab) -> None:
    """Wait until CF clears and the email+password form appears.

    - A real browser usually passes CF silently.
    - On a Turnstile challenge, locate and click the checkbox via template.
    - Fallback: prompt for manual verification and keep polling.
    """
    deadline = time.monotonic() + CF_WAIT_SECONDS
    last_verify = 0.0
    manual_hinted = False
    last_log = 0.0

    while time.monotonic() < deadline:
        try:
            state = await page_state(tab)
        except Exception as exc:
            LOG.debug("page_state 失败: %s", exc)
            await asyncio.sleep(1.0)
            continue

        if state.get("hasEmail") and state.get("hasPassword"):
            LOG.info("已到达 PoE 登录输入页: %s", state.get("url"))
            return

        now = time.monotonic()
        if now - last_log >= 5.0:
            last_log = now
            LOG.info(
                "等待中: url=%s title=%r email=%s pwd=%s cf=%s",
                state.get("url"),
                state.get("title"),
                state.get("hasEmail"),
                state.get("hasPassword"),
                state.get("cfFrame"),
            )

        title = str(state.get("title") or "")
        is_cf = bool(
            state.get("cfFrame")
            or "just a moment" in title.lower()
            or "请稍候" in title
            or "attention required" in title.lower()
        )
        if is_cf and time.monotonic() - last_verify >= 5.0:
            LOG.warning("检测到 Cloudflare 挑战框, 尝试自动点击验证 ...")
            last_verify = time.monotonic()
            try:
                await tab.verify_cf(flash=False)
                await asyncio.sleep(2.0)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("verify_cf 失败(可能无感通过): %s", exc)

        if is_cf and last_verify > 0 and not manual_hinted:
            manual_hinted = True
            LOG.warning("CF 自动点击未能放行, 请在浏览器窗口中手动完成验证(脚本会继续等待)")

        await asyncio.sleep(1.5)

    try:
        shot = Path("cf_timeout_debug.png").resolve()
        await tab.save_screenshot(str(shot))
        LOG.error("超时截图已保存: %s", shot)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("保存超时截图失败: %s", exc)
    raise RebindError(
        f"等待登录页面超时({CF_WAIT_SECONDS:.0f}s), 未能通过 Cloudflare 或页面未出现输入框"
    )


async def click_cookie_continue(tab: uc.Tab) -> None:
    """Dismiss the cookie banner if present; never block the main flow."""
    try:
        btn = await tab.wait_for(COOKIE_CONTINUE_SEL, timeout=3)
        await btn.click()
        LOG.info("已关闭 Cookie 横幅")
    except Exception:
        pass


POE_PAGE_STATE_JS = """
JSON.stringify((() => ({
    url: location.href,
    title: document.title,
    hasEmail: !!document.querySelector('input[name=\\'login_email\\']'),
    hasPassword: !!document.querySelector('input[name=\\'login_password\\']'),
    hasRemove: !!document.querySelector('button[value=\\'twitch_remove\\']'),
    hasAdd: !!document.querySelector('button[value=\\'twitch_add\\']'),
    bodyText: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 1200),
}))())
"""

LOGIN_TURNSTILE_STATE_JS = """
JSON.stringify((() => {
    const tok = document.querySelector('input[name=\\'cf-turnstile-response\\'], input[name=\\'g-recaptcha-response\\']');
    const tokenLen = tok && tok.value ? tok.value.length : 0;
    // DOM signatures that decide whether this login carries a captcha at all.
    const features = {
        container: !!document.querySelector('.sign-in__captcha, .recaptcha'),
        hiddenInputs: !!tok,
        scriptTag: !!document.querySelector(
            'script[src*=\\'challenges.cloudflare.com/turnstile\\'], script[src*=\\'recaptcha\\']'
        ),
    };
    // The Turnstile visual lives inside a SHADOW ROOT: light-DOM iframe
    // selectors miss it. Walk shadow roots explicitly.
    const deepQuery = (root, sel) => {
        const el = root.querySelector(sel);
        if (el) return el;
        for (const child of root.querySelectorAll('*')) {
            if (child.shadowRoot) {
                const found = deepQuery(child.shadowRoot, sel);
                if (found) return found;
            }
        }
        return null;
    };
    const f = deepQuery(document, 'iframe[src*=\\'challenges.cloudflare.com\\']')
           || deepQuery(document, '.sign-in__captcha iframe');
    const r = f ? f.getBoundingClientRect() : null;
    return {
        features: features,
        hasFeatures: features.container || features.hiddenInputs || features.scriptTag,
        tokenLen: tokenLen,
        frame: r ? {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)} : null,
        // Fallback click anchor: the .sign-in__captcha container itself.
        box: (() => {
            const b = document.querySelector('.sign-in__captcha, .recaptcha');
            const br = b ? b.getBoundingClientRect() : null;
            return br ? {x: Math.round(br.x), y: Math.round(br.y), w: Math.round(br.width), h: Math.round(br.height)} : null;
        })(),
    };
})())
"""


async def login_turnstile_state(tab: uc.Tab) -> dict:
    """State of the Turnstile widget embedded in the PoE login form."""
    return await _eval_json(tab, LOGIN_TURNSTILE_STATE_JS)


async def locate_checkbox_visual(tab: uc.Tab, box: dict | None) -> tuple[int, int] | None:
    """Locate the Turnstile checkbox in a viewport screenshot via OpenCV.

    The widget visual may live in a CLOSED shadow root, which no DOM query
    can pierce — but it is always visible on screen. Vision-based location
    works regardless of how the DOM hides the iframe.

    Returns viewport CSS coordinates of the checkbox center, or None.
    """
    import cv2  # type: ignore[import-not-found]

    probe = Path("_ts_probe.png").resolve()
    try:
        await tab.save_screenshot(str(probe))
        img = cv2.imread(str(probe))
        if img is None:
            return None
        h_px, w_px = img.shape[:2]
        try:
            inner = await tab.evaluate(
                "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
                return_by_value=False,
            )
            inner = json.loads(inner) if isinstance(inner, str) else {}
        except Exception:
            inner = {}
        sx = w_px / max(1, int(inner.get("w") or w_px))
        sy = h_px / max(1, int(inner.get("h") or h_px))

        # The widget is dark-themed: the checkbox border is too low-contrast
        # for adaptive thresholding, but Canny edges isolate it reliably.
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 30, 100)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        def in_region(cx: float, cy: float) -> bool:
            # Screenshot pixels. Candidates must sit STRICTLY INSIDE the
            # captcha container rect — expanding the region catches the
            # "Remember me" checkbox above the widget.
            if box:
                x0 = (box["x"] + 2) * sx
                x1 = (box["x"] + box["w"] - 2) * sx
                y0 = (box["y"] + 2) * sy
                y1 = (box["y"] + box["h"] - 2) * sy
                return x0 <= cx <= x1 and y0 <= cy <= y1
            return False  # no container anchor -> let the DOM anchor click

        candidates: list[tuple[float, float, float]] = []
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if not (10 <= cw <= 40 and 10 <= ch <= 40 and abs(cw - ch) <= 8):
                continue
            cx, cy = x + cw / 2, y + ch / 2
            if not in_region(cx, cy):
                continue
            candidates.append((cx, cy, cw * ch))
        if not candidates:
            return None
        # The checkbox is the LEFTMOST element of the widget (before the
        # "Verify you are human" text and the Cloudflare logo).
        cx, cy, _ = min(candidates, key=lambda t: t[0])
        return (round(cx / sx), round(cy / sy))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("视觉定位失败: %s", exc)
        return None
    finally:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass


async def solve_login_turnstile(tab: uc.Tab, timeout: float = 30.0) -> bool:
    """Conditionally handle the login-form Turnstile (compat=recaptcha mode).

    A captcha is NOT shown on every login. Gate on DOM signatures first:
      1. token already issued -> submit right away;
      2. captcha features present (.sign-in__captcha / .recaptcha container,
         hidden cf-turnstile-response inputs, turnstile script tag) ->
         enter the shadow-DOM piercing + coordinate-click path;
      3. no features at all within the detection window -> no captcha on
         this login, skip solving and submit directly.
    Returns True when it is safe to proceed with the submit (False only
    means "no token yet" — the caller may still submit and let the server
    bounce back, which is handled by wait_connections_page).
    """
    # --- Feature-detection phase: does this login carry a captcha? ---
    detect_deadline = time.monotonic() + CAPTCHA_DETECT_SECONDS
    features_found = False
    while time.monotonic() < detect_deadline:
        try:
            state = await login_turnstile_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        if state.get("tokenLen", 0) > 20:
            LOG.info("Turnstile token 已自动签发(长度 %s), 无需处理", state.get("tokenLen"))
            return True

        if state.get("hasFeatures"):
            LOG.info(
                "检测到验证码特征 %s, 进入 shadow DOM 探测 + 坐标点击流程",
                state.get("features"),
            )
            features_found = True
            break

        await asyncio.sleep(1.2)

    if not features_found:
        LOG.info(
            "登录表单无验证码特征(%.0fs 内无脚本/无组件/无 token), 跳过处理",
            CAPTCHA_DETECT_SECONDS,
        )
        return True

    # --- Solve phase: click the checkbox, wait for the token ---
    # Click priority: 1) vision-located checkbox (works even inside a
    # CLOSED shadow root); 2) shadow-DOM iframe rect; 3) the container box.
    deadline = time.monotonic() + timeout
    last_click = 0.0
    while time.monotonic() < deadline:
        try:
            state = await login_turnstile_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        if state.get("tokenLen", 0) > 20:
            LOG.info("登录表单 Turnstile 已通过(token 长度 %s)", state.get("tokenLen"))
            return True

        if time.monotonic() - last_click >= 5.0:
            last_click = time.monotonic()
            target = await locate_checkbox_visual(tab, state.get("box"))
            if target:
                LOG.info("视觉定位复选框 @(%s,%s), 点击 ...", *target)
            else:
                anchor = state.get("frame") or state.get("box")
                if anchor and anchor.get("w", 0) > 5:
                    target = (anchor["x"] + 30, anchor["y"] + anchor["h"] // 2)
                    LOG.info("DOM 锚点坐标点击 @(%s,%s) ...", *target)
            if target:
                try:
                    await tab.mouse_click(*target)
                    await asyncio.sleep(2.0)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("坐标点击失败: %s", exc)

        await asyncio.sleep(1.2)
    LOG.warning("登录表单 Turnstile 未在 %.0fs 内通过, 继续尝试提交", timeout)
    return False


TWITCH_LOGIN_STATE_JS = """
JSON.stringify((() => ({
    url: location.href,
    title: document.title,
    hasUser: !!document.querySelector('#login-username'),
    hasPass: !!document.querySelector('#password-input'),
    hasLoginBtn: !!document.querySelector('button[data-a-target=\\'passport-login-button\\']'),
    authorizeBtn: (() => {
        const b = Array.from(document.querySelectorAll('button')).find(
            x => (x.innerText || '').trim().toLowerCase() === 'authorize'
        );
        if (!b) return null;
        const r = b.getBoundingClientRect();
        return {disabled: !!b.disabled,
                x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height)};
    })(),
    hasAuthorize: !!Array.from(document.querySelectorAll('button')).find(
        b => (b.innerText || '').trim().toLowerCase() === 'authorize'
    ),
    bodyText: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 300),
}))())
"""


async def _eval_json(tab: uc.Tab, js: str) -> dict:
    """Evaluate a JSON-returning JS payload (see PAGE_STATE_JS note)."""
    result = await tab.evaluate(js, return_by_value=False)
    if isinstance(result, str):
        return json.loads(result)
    dsv = getattr(result, "deep_serialized_value", None)
    if dsv is not None and isinstance(dsv.value, str):
        return json.loads(dsv.value)
    raise RebindError(f"_eval_json 无法解析结果: {type(result)!r}")


async def poe_page_state(tab: uc.Tab) -> dict:
    """PoE page state: login form / twitch buttons / login error."""
    return await _eval_json(tab, POE_PAGE_STATE_JS)


async def twitch_login_state(tab: uc.Tab) -> dict:
    """Twitch OAuth page state: login form vs authorize button."""
    return await _eval_json(tab, TWITCH_LOGIN_STATE_JS)


async def human_type(element, text: str, delay_ms: float = TYPE_DELAY_MS) -> None:
    """Type char by char with a randomized delay to mimic a human."""
    for ch in text:
        await element.send_keys(ch)
        await asyncio.sleep(random.uniform(delay_ms * 0.5, delay_ms * 1.5) / 1000.0)


# --- Step 2: PoE login -------------------------------------------------------


async def login_poe(tab: uc.Tab, account: PoeAccount | None) -> None:
    """Fill the PoE email/password form and submit (or wait for manual login)."""
    if account is None:
        await wait_manual_login(tab)
        return
    LOG.info("[2/4] 输入 PoE 账号密码并登录 ...")
    email_el = await tab.select(EMAIL_SEL, timeout=15)
    await email_el.click()
    await email_el.clear_input()
    await human_type(email_el, account.email)

    pass_el = await tab.select(PASSWORD_SEL, timeout=10)
    await pass_el.click()
    await pass_el.clear_input()
    await human_type(pass_el, account.password)

    # PoE embeds a Turnstile widget (compat=recaptcha) in the login form;
    # solve it before submitting or the POST bounces back with
    # "Please complete the reCAPTCHA".
    await solve_login_turnstile(tab)

    submit = await tab.select(SUBMIT_SEL, timeout=10)
    await submit.click()
    await wait_connections_page(tab)


async def wait_connections_page(tab: uc.Tab) -> None:
    """Wait until the connections page is REALLY rendered; handle the captcha.

    A failed login still flashes the target URL during the redirect chain
    (POST -> 302 -> GET /my-account/connections -> bounce to /login), so a
    URL check alone gives false positives. Success requires the Twitch
    section to be actually rendered (twitch_add / twitch_remove button) and
    stable for 2 seconds. On a captcha bounce: solve once, and re-submit
    ONLY when a real Turnstile token exists (blind retries escalate the IP
    risk score).
    """
    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    last_log = 0.0
    captcha_hinted = False
    resubmitted_with_token = False
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            state = await poe_page_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        url = str(state.get("url") or "")
        now = time.monotonic()
        body = str(state.get("bodyText") or "")

        # Content-based success: the Twitch section is actually rendered.
        if "/my-account/connections" in url and (state.get("hasAdd") or state.get("hasRemove")):
            if stable_since is None:
                stable_since = now
            if now - stable_since >= 2.0:
                LOG.info("[2/4] PoE 登录成功, 已到达账号连接页")
                return
        else:
            stable_since = None

        if "/login" in url:
            if re.search(r"incorrect|invalid credentials|failed to sign in", body, re.I):
                raise RebindError(f"PoE 登录失败: {body[:160]}")
            if "recaptcha" in body.lower():
                if not captcha_hinted:
                    captcha_hinted = True
                    LOG.warning(
                        "登录被 reCAPTCHA 拦截: 如浏览器弹出图片/复选框验证, 请手动完成;"
                        " 脚本会等待 token 生成后再重试(不做盲目重复提交, 避免加重风控)"
                    )
                # Re-submit ONLY when the Turnstile token actually exists;
                # blind retries escalate the IP risk score.
                if not resubmitted_with_token:
                    await solve_login_turnstile(tab, timeout=10.0)
                    try:
                        ts_state = await login_turnstile_state(tab)
                    except Exception:
                        ts_state = {}
                    if ts_state.get("tokenLen", 0) > 20:
                        resubmitted_with_token = True
                        LOG.info("检测到 Turnstile token 已生成, 重新提交登录 ...")
                        try:
                            submit = await tab.select(SUBMIT_SEL, timeout=5)
                            if submit:
                                await submit.click()
                        except Exception as exc:  # noqa: BLE001
                            LOG.debug("重提交失败: %s", exc)

        if now - last_log >= 5.0:
            last_log = now
            LOG.info(
                "登录等待中: url=%s title=%r recaptcha=%s",
                url, state.get("title"), captcha_hinted,
            )

        await asyncio.sleep(1.2)

    try:
        shot = Path("login_timeout_debug.png").resolve()
        await tab.save_screenshot(str(shot))
        LOG.error("登录超时截图: %s", shot)
        body = await tab.evaluate(
            "JSON.stringify(document.body ? document.body.innerText.slice(0, 600) : '')",
            return_by_value=False,
        )
        if isinstance(body, str):
            LOG.error("页面文本: %s", body)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("超时诊断失败: %s", exc)
    raise RebindError(
        f"等待 PoE 登录超时({LOGIN_WAIT_SECONDS:.0f}s): "
        + (
            "验证码未能通过(未能定位复选框或 token 未签发), 请检查网络或稍后重试"
            if captcha_hinted
            else "未能到达账号连接页"
        )
    )


# --- Step 3: unlink old Twitch ------------------------------------------------


async def unbind_old_twitch(tab: uc.Tab) -> None:
    """Click twitch_remove and wait until the binding is gone."""
    state = await poe_page_state(tab)
    if "/login" in str(state.get("url") or ""):
        raise RebindError("会话已失效(被弹回登录页), 无法执行解绑")
    if not state.get("hasRemove"):
        LOG.info("[3/4] 未检测到已绑定的 Twitch, 跳过解绑")
        return

    LOG.info("[3/4] 点击解除 Twitch 绑定 ...")
    btn = await tab.select(TWITCH_REMOVE_SEL, timeout=10)
    await btn.scroll_into_view()
    await btn.click()

    deadline = time.monotonic() + UNBIND_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            state = await poe_page_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if not state.get("hasRemove"):
            LOG.info("[3/4] 解绑完成")
            return
        await asyncio.sleep(1.0)
    raise RebindError(f"等待解绑生效超时({UNBIND_WAIT_SECONDS:.0f}s)")


# --- Step 4: connect new Twitch (stop at the login form) ----------------------


async def clear_twitch_cookies(tab: uc.Tab) -> None:
    """Wipe Twitch-domain cookies/localStorage so the residual session
    cannot silently re-link the old account (an improvement over the
    original tool, which relied on force_verify alone)."""
    from nodriver import cdp

    cleared = 0
    deleted = 0
    for origin in TWITCH_COOKIE_ORIGINS:
        try:
            await tab.send(cdp.storage.clear_data_for_origin(origin, "all"))
            cleared += 1
        except Exception as exc:  # noqa: BLE001
            LOG.debug("清理 %s 失败: %s", origin, exc)
    # Storage.clearDataForOrigin can miss domain-level cookies — delete the
    # known Twitch session cookies by name (auth-token IS the session).
    try:
        await tab.send(cdp.network.enable())
    except Exception:
        pass
    for name in TWITCH_SESSION_COOKIES:
        try:
            await tab.send(cdp.network.delete_cookies(name=name, domain=".twitch.tv"))
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            LOG.debug("删除 cookie %s 失败: %s", name, exc)
    LOG.info(
        "[4/4] 已清理 Twitch 域: storage %d/%d 源, 会话 cookie %d/%d 个",
        cleared, len(TWITCH_COOKIE_ORIGINS), deleted, len(TWITCH_SESSION_COOKIES),
    )


async def bind_new_twitch(tab: uc.Tab) -> dict:
    """Clear Twitch storage, click connect, wait for the Twitch login form."""
    btn = await tab.select(TWITCH_ADD_SEL, timeout=10)
    if btn is None:
        state = await poe_page_state(tab)
        if "/login" in str(state.get("url") or ""):
            raise RebindError("会话已失效(被弹回登录页), 未找到 Twitch 绑定按钮")
        raise RebindError(
            f"未找到 Twitch 绑定按钮({TWITCH_ADD_SEL}): "
            f"url={state.get('url')} body={str(state.get('bodyText'))[:120]}"
        )
    await clear_twitch_cookies(tab)
    await btn.scroll_into_view()
    await btn.click()
    LOG.info("[4/4] 已点击绑定 Twitch, 等待 Twitch 登录表单 ...")

    deadline = time.monotonic() + TWITCH_LOGIN_WAIT_SECONDS
    started = time.monotonic()
    residue_handled = False
    while time.monotonic() < deadline:
        try:
            state = await twitch_login_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        if state.get("hasUser") and state.get("hasPass"):
            LOG.info("[4/4] 已到达 Twitch 登录表单: %s", state.get("url"))
            return state

        url = str(state.get("url") or "")
        if (
            "auth.twitch.tv" in url
            and state.get("hasAuthorize")
            and not state.get("hasUser")
            and not residue_handled
        ):
            # Session residue: the Authorize page appeared directly. Wipe
            # Twitch storage again and reload so the login form shows up.
            LOG.warning("检测到会话残留(Authorize 直接出现), 重新清理并刷新 ...")
            residue_handled = True
            await clear_twitch_cookies(tab)
            await tab.reload()
        elif (
            _is_poe_url(url)
            and "/my-account" in url
            and time.monotonic() - started > 5.0
            and not residue_handled
        ):
            # OAuth completed WITHOUT a login form: the residual Twitch
            # session re-linked the OLD account silently. Wipe the session
            # cookies by name, go back and click connect again.
            LOG.warning("检测到 OAuth 未经登录直接完成(会话残留), 清理会话后重试 ...")
            residue_handled = True
            await clear_twitch_cookies(tab)
            await tab.get("https://www.pathofexile.com/my-account/connections")
            await asyncio.sleep(1.5)
            retry_btn = await tab.select(TWITCH_ADD_SEL, timeout=10)
            if retry_btn is not None:
                await retry_btn.click()

        await asyncio.sleep(1.2)
    try:
        diag = await twitch_login_state(tab)
    except Exception:
        diag = {}
    raise RebindError(
        f"等待 Twitch 登录表单超时({TWITCH_LOGIN_WAIT_SECONDS:.0f}s): "
        f"url={diag.get('url')} body={str(diag.get('bodyText'))[:150]}"
    )


# --- Step 5: Twitch login + OAuth authorize + verify -------------------------


async def twitch_login(tab: uc.Tab, account: TwitchAccount) -> None:
    """Type the Twitch credentials into the login form and submit.

    Handles both single-page (username+password together) and two-step
    (username first, password field appears after submit) layouts.
    """
    LOG.info("[5/5] 输入 Twitch 账号 %s ...", account.username)
    user_el = await tab.select(TWITCH_USER_SEL, timeout=TWITCH_LOGIN_WAIT_SECONDS)
    if user_el is None:
        raise RebindError(f"未出现 Twitch 用户名输入框({TWITCH_USER_SEL})")
    await user_el.click()
    await user_el.clear_input()
    await human_type(user_el, account.username)

    # Password may already be on the page, or appear after submitting username.
    pass_el = await tab.select(TWITCH_PASS_SEL, timeout=3)
    if pass_el is None:
        btn = await tab.select(TWITCH_LOGIN_BTN_SEL, timeout=5)
        if btn is not None:
            await btn.click()
        pass_el = await tab.select(TWITCH_PASS_SEL, timeout=10)
    if pass_el is None:
        raise RebindError(f"未出现 Twitch 密码输入框({TWITCH_PASS_SEL})")
    await pass_el.click()
    await pass_el.clear_input()
    await human_type(pass_el, account.password)

    btn = await tab.select(TWITCH_LOGIN_BTN_SEL, timeout=5)
    if btn is None:
        raise RebindError(f"未找到 Twitch 登录按钮({TWITCH_LOGIN_BTN_SEL})")
    await btn.click()
    LOG.info("[5/5] 已提交 Twitch 登录, 等待 OAuth 授权 ...")


def _is_poe_url(url: str) -> bool:
    """True only for real PoE page URLs.

    The OAuth authorize URL contains the (double-encoded) redirect_uri
    `www.pathofexile.com` as a QUERY PARAMETER — a plain substring check
    would false-positive while still sitting on auth.twitch.tv.
    """
    from urllib.parse import urlparse

    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host == "pathofexile.com" or host.endswith(".pathofexile.com")


async def click_blank_area(tab: uc.Tab) -> None:
    """Click an empty part of the page.

    Twitch's consent page keeps the Authorize button disabled until the
    page receives a real user gesture — a trusted CDP click on blank
    space activates it.
    """
    try:
        dims = await tab.evaluate(
            "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
            return_by_value=False,
        )
        d = json.loads(dims) if isinstance(dims, str) else {}
        x = int(d.get("w") or 1200) * 20 // 100
        y = int(d.get("h") or 800) * 30 // 100
        await tab.mouse_click(x, y)
        LOG.debug("blank click @(%s,%s)", x, y)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("blank click 失败: %s", exc)


async def authorize_and_verify(tab: uc.Tab, account: TwitchAccount) -> bool:
    """Wait for the OAuth flow to finish and verify the binding on PoE.

    Phase 1: on twitch.tv — click blank space to activate the Authorize
    button (Twitch keeps it disabled until a user gesture), click it, and
    wait for the redirect back to pathofexile.com; detect login errors.
    Phase 2: on pathofexile.com — wait for the Twitch section to render
    (twitch_remove present) and confirm the bound username in the page text.
    """
    deadline = time.monotonic() + AUTHORIZE_WAIT_SECONDS
    authorize_clicked = False
    while time.monotonic() < deadline:
        try:
            state = await twitch_login_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        url = str(state.get("url") or "")
        body = str(state.get("bodyText") or "")

        if _is_poe_url(url):
            break  # real redirect back to PoE (not the query-param echo)

        if "twitch.tv" in url and re.search(r"incorrect|log.?in failed|wrong password", body, re.I):
            raise RebindError(f"Twitch 登录被拒绝(账号或密码错误): {account.username}")

        if not authorize_clicked and state.get("hasAuthorize"):
            authorize_clicked = True
            LOG.info("[5/5] 点击页面空白处激活 Authorize 按钮 ...")
            await click_blank_area(tab)
            await asyncio.sleep(1.0)
            LOG.info("[5/5] 点击 Authorize 授权 ...")
            try:
                btn = await tab.find("Authorize", best_match=True)
                if btn is not None:
                    await btn.click()
                else:
                    raise RuntimeError("find returned None")
            except Exception:  # noqa: BLE001 — fallback to coordinate click
                rect = state.get("authorizeBtn") or {}
                if rect.get("w", 0) > 0:
                    cx = rect["x"] + rect["w"] // 2
                    cy = rect["y"] + rect["h"] // 2
                    await tab.mouse_click(cx, cy)
                else:
                    await tab.evaluate(
                        "(() => { const b = Array.from(document.querySelectorAll('button'))"
                        ".find(x => (x.innerText || '').trim().toLowerCase() === 'authorize');"
                        "if (b) { b.click(); return true; } return false; })()"
                    )
            await asyncio.sleep(2.0)

        await asyncio.sleep(1.2)
    else:
        raise RebindError(f"等待 OAuth 授权/回跳超时({AUTHORIZE_WAIT_SECONDS:.0f}s)")

    # Phase 2: verify the binding on the PoE side.
    # The OAuth callback lands on /my-account/twitch, which does not show
    # the linked username reliably — navigate to the connections page for
    # a deterministic check (twitch_remove present + username in the text).
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            state = await poe_page_state(tab)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        url = str(state.get("url") or "")
        if _is_poe_url(url):
            if "/my-account/connections" not in url:
                await tab.get("https://www.pathofexile.com/my-account/connections")
                await asyncio.sleep(1.5)
                continue
        else:
            await asyncio.sleep(1.2)
            continue
        if state.get("hasRemove"):
            body = str(state.get("bodyText") or "")
            if account.username.lower() in body.lower():
                LOG.info("[5/5] 绑定校验通过: %s 已连接", account.username)
                return True
            LOG.warning(
                "[5/5] 已有绑定但页面文本中未找到 %s(可能绑定了其他账号)",
                account.username,
            )
            return False
        await asyncio.sleep(1.2)
    raise RebindError("等待 PoE 绑定结果超时")


async def ensure_poe_session(tab: uc.Tab, account: PoeAccount | None) -> None:
    """Make sure the tab is on a logged-in PoE page.

    - Anywhere other than the connections page (a foreign domain like
      auth.twitch.tv after a failed account, or the /my-account/twitch
      rewards page): navigate back to /my-account/connections.
    - Bounced to /login (session lost): log into PoE again.
    """
    try:
        state = await poe_page_state(tab)
    except Exception:
        return
    url = str(state.get("url") or "")
    if url and "/my-account/connections" not in url:
        LOG.info("当前不在连接页(%s), 导航回 /my-account/connections ...", url[:60])
        await tab.get("https://www.pathofexile.com/my-account/connections")
        await asyncio.sleep(2.0)
        try:
            state = await poe_page_state(tab)
        except Exception:
            return
        url = str(state.get("url") or "")
    if "/login" in url:
        await tab.get(POE_LOGIN_URL)
        if account is None:
            LOG.info("PoE 会话失效, 请在浏览器中手动重新登录 ...")
            await wait_manual_login(tab)
        else:
            LOG.info("PoE 会话失效, 重新登录 ...")
            await wait_login_page(tab)
            await login_poe(tab, account)


# --- Main flow --------------------------------------------------------------


async def open_login_page(browser: uc.Browser) -> uc.Tab:
    """Open the PoE login page and wait until the input form is ready."""
    tab = await browser.get(POE_LOGIN_URL)
    await click_cookie_continue(tab)
    await wait_login_page(tab)
    return tab


async def wait_browser_closed(browser: uc.Browser) -> None:
    process = getattr(browser, "_process", None)
    if process is None:
        return
    while process.returncode is None:
        await asyncio.sleep(1.0)


async def run(args: argparse.Namespace) -> int:
    # Validate credentials BEFORE launching the browser.
    batch_start = time.monotonic()
    account = resolve_poe(args)
    if account is None:
        LOG.info("未检测到 poe.env, 将打开浏览器等待手动登录")
    twitch_accounts = resolve_twitch_accounts(Path(args.twitch_file))
    chrome_path = find_chrome(args.chrome)
    LOG.info("批次开始: 共 %d 个 Twitch 账号待绑定", len(twitch_accounts))
    LOG.info("启动浏览器: %s", chrome_path)

    browser_args = ["--lang=en-US"]
    if args.proxy:
        browser_args.append(f"--proxy-server={args.proxy}")
        LOG.info("使用代理: %s", args.proxy)
    browser_args.append(f"--disable-features={LNA_DISABLE_FEATURES}")
    browser_args.extend(BACKGROUND_HARDENING_ARGS)

    config = uc.Config(
        browser_executable_path=chrome_path,
        headless=False,
        # nodriver ships an English-only CF template; force English UI to match
        browser_args=[*ISOLATION_BROWSER_ARGS, *browser_args],
    )
    browser = await uc.start(config=config)

    try:
        tab = await open_login_page(browser)
        from nodriver import cdp

        try:
            await tab.send(cdp.emulation.set_focus_emulation_enabled(True))
            LOG.info("已启用焦点模拟(切换窗口不影响流程)")
        except Exception as exc:  # noqa: BLE001
            LOG.debug("焦点模拟启用失败: %s", exc)
        await login_poe(tab, account)

        results: list[tuple[str, bool]] = []
        total = len(twitch_accounts)
        for index, twitch_account in enumerate(twitch_accounts, 1):
            LOG.info("=" * 56)
            LOG.info("[%d/%d] 绑定 Twitch 账号: %s", index, total, twitch_account.username)
            try:
                await ensure_poe_session(tab, account)
                await unbind_old_twitch(tab)
                await bind_new_twitch(tab)
                await twitch_login(tab, twitch_account)
                ok = await authorize_and_verify(tab, twitch_account)
                results.append((twitch_account.username, ok))
                LOG.info(
                    "[%d/%d] %s → %s",
                    index, total, twitch_account.username,
                    "绑定成功" if ok else "已绑定但未确认",
                )
            except Exception as exc:  # noqa: BLE001 — keep the batch running
                LOG.exception(
                    "[%d/%d] %s 失败: %s: %s",
                    index, total, twitch_account.username, type(exc).__name__, exc,
                )
                results.append((twitch_account.username, False))

        ok_count = sum(1 for _, ok in results if ok)

        elapsed = time.monotonic() - batch_start
        mins, secs = divmod(int(elapsed), 60)
        fail_count = total - ok_count
        LOG.info("-" * 56)
        LOG.info("[批次汇总] 用时 %d分%02d秒 | 共 %d 个账号: %d 成功 / %d 失败",
                 mins, secs, total, ok_count, fail_count)
        if fail_count:
            failed = [u for u, ok in results if not ok]
            LOG.warning("[批次汇总] 失败账号 (%d): %s", fail_count, ", ".join(failed))
        else:
            LOG.info("[批次汇总] 全部绑定成功 ✓")
        LOG.info("-" * 56)

        if args.no_hold:
            shot = Path(args.screenshot) if args.screenshot else None
            if shot:
                await tab.save_screenshot(str(shot))
                LOG.info("已保存截图: %s", shot)
            browser.stop()
            return 0 if ok_count == total else 1
        print()
        print("=" * 62)
        print(f"[结果] 全部 {total} 个账号处理完成: {ok_count} 成功 / {total - ok_count} 失败")
        for username, ok in results:
            print(f"  {'[OK]  ' if ok else '[FAIL]'} {username}")
        print("=" * 62)
        print("浏览器保持打开, 手动查看/输入, 关闭浏览器窗口后脚本退出。")

        await wait_browser_closed(browser)
        return 0 if ok_count == total else 1
    finally:
        try:
            browser.stop()
            # Await the browser subprocess so its pipes close cleanly and
            # asyncio does not spam "I/O operation on closed pipe" at exit.
            proc = getattr(browser, "_process", None)
            if proc is not None:
                with contextlib.suppress(Exception):
                    await proc.wait()
        except Exception:
            pass


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rebind",
        description="PoE Twitch 换绑工具: 登录 PoE → 解绑旧 Twitch → 绑定新 Twitch, 停在 Twitch 登录表单",
    )
    parser.add_argument("--chrome", help="Chrome/Edge 可执行文件路径")
    parser.add_argument("--poe-env", default=str(DEFAULT_POE_ENV), help="配置文件路径(默认 poe.env)")
    parser.add_argument("--poe-email", help="PoE 账号邮箱(覆盖 poe.env)")
    parser.add_argument("--poe-password", help="PoE 密码(覆盖 poe.env)")
    parser.add_argument("--twitch-file", default=str(DEFAULT_TWITCH_FILE), help="Twitch 账号列表文件(默认 twitch_accounts.txt)")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--proxy", help="HTTP/SOCKS 代理(如 http://127.0.0.1:7890), 传给浏览器")
    parser.add_argument("--no-hold", action="store_true", help="到达登录页后直接退出(测试用)")
    parser.add_argument("--screenshot", help="--no-hold 时保存截图的路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    _patch_nodriver_shutdown_race()
    args = build_parser().parse_args(argv)
    log_file = _setup_logging(args.verbose)
    LOG.info("日志文件: %s", log_file)
    try:
        return asyncio.run(run(args))
    except RebindError as exc:
        LOG.exception("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("已中断")
        return 130
    except Exception:
        LOG.exception("未预期的错误")
        return 1


if __name__ == "__main__":
    sys.exit(main())
