# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 poe_rebind contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file for details.
"""PoE Twitch rebind tool - Step 1: open the login page and pass Cloudflare.

Stack: Python 3.13 + asyncio + nodriver (real Chrome via CDP).
Goals:
  1. Launch a real Chrome (or Edge).
  2. Open https://www.pathofexile.com/login/email.
  3. Wait for / pass the Cloudflare challenge.
  4. Stop at the email+password form; do nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import nodriver as uc

LOG = logging.getLogger("rebind")


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

POE_LOGIN_URL = "https://www.pathofexile.com/login/email"

EMAIL_SEL = "input[name='login_email']"
PASSWORD_SEL = "input[name='login_password']"
SUBMIT_SEL = "input.sign-in__submit"

CF_IFRAME_SEL = "iframe[src*='challenges.cloudflare.com']"
CF_WAIT_SECONDS = 60.0  # max wait for the CF challenge

COOKIE_CONTINUE_SEL = "button#onetrust-accept-btn-handler"

DEFAULT_POE_ENV = Path(__file__).resolve().parent / "poe.env"

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


def resolve_poe(args: argparse.Namespace) -> PoeAccount:
    """Resolve credentials: CLI args > env file > environment variables."""
    env = load_env_file(Path(args.poe_env)) if args.poe_env else {}
    email = args.poe_email or env.get("POE_EMAIL") or os.environ.get("POE_EMAIL") or ""
    password = args.poe_password or env.get("POE_PASSWORD") or os.environ.get("POE_PASSWORD") or ""
    if not email or not password:
        raise RebindError("缺少 PoE 账号: 请在 poe.env 或 CLI(--poe-email/--poe-password)中提供")
    return PoeAccount(email, password)


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
    hasSubmit: !!document.querySelector('input.sign-in__submit'),
    cfFrame: !!document.querySelector('iframe[src*=\\'challenges.cloudflare.com\\']'),
    cookieBanner: !!document.querySelector('button#onetrust-accept-btn-handler'),
}))())
"""


async def page_state(tab: uc.Tab) -> dict:
    """Read the page state in one shot (CF iframe / login form / cookie banner)."""
    result = await tab.evaluate(PAGE_STATE_JS, return_by_value=False)
    if isinstance(result, str):
        return json.loads(result)
    dsv = getattr(result, "deep_serialized_value", None)
    if dsv is not None and isinstance(dsv.value, str):
        return json.loads(dsv.value)
    raise RebindError(f"page_state 返回无法解析的结果: {type(result)!r}")


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
    chrome_path = find_chrome(args.chrome)
    LOG.info("启动浏览器: %s", chrome_path)

    config = uc.Config(
        browser_executable_path=chrome_path,
        headless=False,
        # nodriver ships an English-only CF template; force English UI to match
        browser_args=[*ISOLATION_BROWSER_ARGS, "--lang=en-US"],
    )
    browser = await uc.start(config=config)

    try:
        tab = await open_login_page(browser)
        state = await page_state(tab)
        if args.no_hold:
            shot = Path(args.screenshot) if args.screenshot else None
            if shot:
                await tab.save_screenshot(str(shot))
                LOG.info("已保存截图: %s", shot)
            browser.stop()
            return 0
        print()
        print("=" * 62)
        print("[OK] 第一步完成: 已通过 CF 检测, 停留在账号密码输入页")
        print(f"  URL   : {state.get('url')}")
        print(f"  标题  : {state.get('title')}")
        print(f"  邮箱框: {state.get('hasEmail')}")
        print(f"  密码框: {state.get('hasPassword')}")
        print(f"  提交钮: {state.get('hasSubmit')}")
        print("=" * 62)
        print("浏览器保持打开, 手动查看/登录, 关闭浏览器窗口后脚本退出。")

        await wait_browser_closed(browser)
        return 0
    finally:
        try:
            browser.stop()
        except Exception:
            pass


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rebind",
        description="PoE Twitch 换绑工具 —— 第一步: 进入官网登录页并通过 CF 检测",
    )
    parser.add_argument("--chrome", help="Chrome/Edge 可执行文件路径")
    parser.add_argument("--poe-env", default=str(DEFAULT_POE_ENV), help="配置文件路径(默认 poe.env)")
    parser.add_argument("--poe-email", help="PoE 账号邮箱(覆盖 poe.env)")
    parser.add_argument("--poe-password", help="PoE 密码(覆盖 poe.env)")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--no-hold", action="store_true", help="到达登录页后直接退出(测试用)")
    parser.add_argument("--screenshot", help="--no-hold 时保存截图的路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    _patch_nodriver_shutdown_race()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Step 1 never logs in; missing credentials are only a warning
    try:
        resolve_poe(args)
    except RebindError as exc:
        LOG.warning("%s (第一步仅停留在输入页, 可继续运行)", exc)

    try:
        return asyncio.run(run(args))
    except RebindError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
