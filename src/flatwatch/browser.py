"""Persistent Chrome context + block/checkpoint detection.

Account safety: this module only opens pages. It never fills forms, never
touches passwords and never clicks anything.
"""

from __future__ import annotations

import plistlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from loguru import logger
from playwright.async_api import BrowserContext, Page, async_playwright

from .config import DATA_DIR

PROFILE_DIR = DATA_DIR / "chrome-profile"
CHROME_PLIST = Path("/Applications/Google Chrome.app/Contents/Info.plist")

LOGIN_COOKIES = {
    "facebook": ("https://www.facebook.com", "c_user"),
    "reddit": ("https://www.reddit.com", "reddit_session"),
}
HOME_URLS = {"facebook": "https://www.facebook.com/", "reddit": "https://www.reddit.com/login/"}

class BlockedError(RuntimeError):
    """Login wall / checkpoint / block page. Caller must pause the source."""


BLOCK_TEXT = (
    "you're temporarily blocked",
    "you’re temporarily blocked",
    "your account has been locked",
    "confirm your identity",
    "we suspended your account",
    "security check",
    "blocked by network security",
    "whoa there, pardner",
)


def _headful_user_agent() -> str | None:
    """A normal (non-"HeadlessChrome") UA matching the installed Chrome version."""
    try:
        version = plistlib.loads(CHROME_PLIST.read_bytes())["CFBundleShortVersionString"]
    except Exception:
        return None
    major = version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


@asynccontextmanager
async def open_context(*, headless: bool, offscreen: bool = False) -> AsyncIterator[BrowserContext]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled"]
    if offscreen and not headless:
        args += ["--window-position=-2400,-2400"]
    kwargs: dict = dict(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1280, "height": 900},
    )
    if headless and (ua := _headful_user_agent()):
        kwargs["user_agent"] = ua
    logger.info("opening Chrome profile {} (headless={}, offscreen={})", PROFILE_DIR, headless, offscreen)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        try:
            yield ctx
        finally:
            await ctx.close()


async def is_logged_in(ctx: BrowserContext, site: str) -> bool:
    """Facebook: `c_user` cookie. Reddit: the session cookie isn't reliable any more
    (logged-out visitors also get token_v2/loid), so ask /api/me.json from a page."""
    origin, name = LOGIN_COOKIES[site]
    if any(c["name"] == name and c.get("value") for c in await ctx.cookies(origin)):
        return True
    if site != "reddit":
        return False
    page = await ctx.new_page()
    try:
        await page.goto(f"{origin}/", wait_until="domcontentloaded", timeout=30000)
        res = await page.evaluate(
            """async () => { try { const r = await fetch('/api/me.json', {credentials: 'include'});
                 const j = await r.json(); return !!(j && j.data && j.data.name); } catch (e) { return false; } }"""
        )
        return bool(res)
    except Exception:
        return False
    finally:
        await page.close()


async def detect_block(page: Page) -> str | None:
    """Return a reason string if the page is a login wall / checkpoint / block page."""
    url = page.url.lower()
    if "/checkpoint" in url:
        return f"checkpoint URL ({page.url.split('?')[0]})"
    if "/login" in url or "login.php" in url:
        return f"login URL ({page.url.split('?')[0]})"
    if "captcha" in url:
        return "captcha URL"
    try:
        if await page.locator('input[name="pass"], input[type="password"], #login_form').count():
            return "login form on page"
        # Block phrases could appear inside ordinary post text, so only look
        # for them when there is no feed on the page.
        if await page.locator('[role="feed"], shreddit-post').count():
            return None
        body = (await page.locator("body").inner_text(timeout=5000)).lower()[:20000]
    except Exception:
        return None
    for needle in BLOCK_TEXT:
        if needle in body:
            return f"block text on page: {needle!r}"
    return None
