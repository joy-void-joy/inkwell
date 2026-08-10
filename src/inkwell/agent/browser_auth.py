"""Persistent browser context for Claude.ai session management.

Uses Playwright to maintain a dedicated browser profile per Inkwell profile,
independent of the user's main browser. Cookies persist across extractions
and survive the user logging out of their personal Chrome.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lup.types import StringMap

from inkwell.devtools.setup import (
    PROFILES_DIR,
    PROJECT_ROOT,
)

logger = logging.getLogger(__name__)

BROWSER_CONTEXTS_DIR = PROJECT_ROOT / "credentials" / "browser"


class BrowserCookie(BaseModel):
    """One cookie the browser holds, as this project reads it.

    Playwright declares every field of a cookie optional, so reading one means
    deciding what an absent field means. This decides once, here, rather than
    at each of the places that ask a cookie its name.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = ""
    value: str = ""


def cookie_value(cookies: StringMap, name: str) -> str:
    """The value a cookie jar carries for `name`, empty where it carries none.

    A jar holds whatever the browser put in it, so a miss is ordinary and
    every caller here treats absent and blank the same way.
    """
    return cookies[name] if name in cookies else ""


def browser_context_dir(profile: str | None = None) -> Path:
    """Return the Playwright persistent context directory for a profile."""
    if profile:
        return PROFILES_DIR / profile / "browser"
    return BROWSER_CONTEXTS_DIR


async def context_has_cookies(profile: str | None = None) -> bool:
    """Check whether a browser context has a valid Claude.ai session cookie."""
    ctx_dir = browser_context_dir(profile)
    if not ctx_dir.exists():
        return False
    cookies = await extract_cookies(profile)
    return "sessionKey" in cookies


async def extract_cookies(profile: str | None = None) -> StringMap:
    """Read Claude.ai cookies from the persistent browser context.

    Launches a headless browser with the stored context, navigates to
    claude.ai to activate cookies, then extracts them. Returns a dict
    of cookie name -> value for the claude.ai domain.
    """
    ctx_dir = browser_context_dir(profile)
    if not ctx_dir.exists():
        logger.debug("No browser context at %s", ctx_dir)
        return {}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "playwright not installed — run: uv add playwright && playwright install chromium"
        )
        return {}

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(ctx_dir),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            await page.goto(
                "https://claude.ai", wait_until="domcontentloaded", timeout=15_000
            )
            held = [
                BrowserCookie.model_validate(cookie)
                for cookie in await browser.cookies("https://claude.ai")
            ]
            cookies = {cookie.name: cookie.value for cookie in held if cookie.name}
            if cookies:
                logger.info(
                    "Extracted %d cookies from persistent browser context", len(cookies)
                )
            return cookies
        finally:
            await browser.close()


async def extract_cookie_header(profile: str | None = None) -> str | None:
    """Build a Cookie header string from the persistent browser context."""
    cookies = await extract_cookies(profile)
    if not cookies:
        return None
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def extract_org_uuid(profile: str | None = None) -> str | None:
    """Read the lastActiveOrg cookie from the persistent browser context."""
    return cookie_value(await extract_cookies(profile), "lastActiveOrg") or None


async def login_interactive(profile: str | None = None) -> bool:
    """Open a visible browser window for the user to log into claude.ai.

    The browser context is saved to disk, so subsequent calls to
    extract_cookies() will have the session without needing the user's
    main browser.

    Returns True if the login appears successful (session cookies present).
    """
    ctx_dir = browser_context_dir(profile)
    ctx_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "playwright not installed — run: uv add playwright && playwright install chromium"
        )
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(ctx_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()

            had_session = any(
                BrowserCookie.model_validate(cookie).name == "sessionKey"
                for cookie in await browser.cookies("https://claude.ai")
            )
            if had_session:
                await browser.clear_cookies()

            await page.goto("https://claude.ai", wait_until="domcontentloaded")

            logger.info("Waiting for user to log in at claude.ai...")
            import asyncio

            deadline = asyncio.get_event_loop().time() + 300
            logged_in = False
            while asyncio.get_event_loop().time() < deadline:
                all_cookies = await browser.cookies("https://claude.ai")
                if any(
                    BrowserCookie.model_validate(cookie).name == "sessionKey"
                    for cookie in all_cookies
                ):
                    logged_in = True
                    break
                await page.wait_for_timeout(2000)
        finally:
            await browser.close()

    if logged_in:
        logger.info("Login successful — session saved to %s", ctx_dir)
    else:
        logger.warning("Login may not have completed — few cookies found")

    return logged_in


def clear_context(profile: str | None = None) -> None:
    """Remove the stored browser context for a profile."""
    import shutil

    ctx_dir = browser_context_dir(profile)
    if ctx_dir.exists():
        shutil.rmtree(ctx_dir)
        logger.info("Cleared browser context at %s", ctx_dir)
