"""Persistent browser context for Claude.ai session management.

Uses Playwright to maintain a dedicated browser profile per Inkwell profile,
independent of the user's main browser. Cookies persist across extractions
and survive the user logging out of their personal Chrome.
"""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from lup.types import EnvVars, JsonValue, StringMap

from inkwell.devtools.setup import (
    PROFILES_DIR,
    PROJECT_ROOT,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Page, Playwright
    from pyvirtualdisplay.display import Display

logger = logging.getLogger(__name__)

BROWSER_CONTEXTS_DIR = PROJECT_ROOT / "credentials" / "browser"
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


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


# ---------------------------------------------------------------------------
# Streamed remote login — drive a server-side login browser from afar
# ---------------------------------------------------------------------------

SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 1,
}


def virtual_display() -> "Display | None":
    """Start an off-screen X display for a headed login browser, or None.

    The streamed login runs headed — claude.ai's Google SSO rejects headless
    Chromium — but a headed browser must render somewhere, and rendering on the
    host's real display pops a window on the server, defeating the
    stream-to-the-viewer design. So it renders on a throwaway Xvfb display
    instead, seen only by the screencast. Returns None when pyvirtualdisplay or
    Xvfb is unavailable, so the caller can fall back to headless.
    """
    try:
        from pyvirtualdisplay.abstractdisplay import XStartError, XStartTimeoutError
        from pyvirtualdisplay.display import Display
    except ImportError:
        logger.warning(
            "pyvirtualdisplay not installed — streamed login falls back to "
            "headless (run: uv add pyvirtualdisplay)"
        )
        return None
    try:
        display = Display(visible=False, size=(1280, 800), manage_global_env=False)
        display.start()
    except (FileNotFoundError, XStartError, XStartTimeoutError) as exc:
        logger.warning(
            "no virtual display (%s) — streamed login falls back to headless; "
            "install Xvfb to render the login browser off-screen",
            exc,
        )
        return None
    return display


class InputEvent(BaseModel):
    """One pointer or keyboard action from a remote viewer, in frame pixels."""

    type: Literal["move", "down", "up", "click", "wheel", "key"]
    x: float = 0
    y: float = 0
    button: Literal["left", "middle", "right"] = "left"
    dx: float = 0
    dy: float = 0
    key: str = ""
    text: str = ""


class ScreencastFrame(BaseModel):
    """The payload of a CDP ``Page.screencastFrame`` event (extra keys ignored)."""

    data: str = ""
    sessionId: int = 0


class StreamingLoginSession:
    """A claude.ai login browser streamed frame-by-frame to a remote viewer.

    Drives the per-profile persistent context as a queue of base64 JPEG frames
    plus an ``apply_input`` sink, so a WebSocket route can let someone log in
    from their own browser — no server display and no pasted cookie. A captured
    ``sessionKey`` leaves the durable session in the profile's context dir,
    exactly where ``extract_cookies`` reads it. Popups (e.g. Google SSO) are
    followed by re-pointing the screencast at the newest page.
    """

    def __init__(self, profile: str | None = None, *, max_frames: int = 2) -> None:
        self.context_dir = browser_context_dir(profile)
        self.frames: asyncio.Queue[str] = asyncio.Queue(maxsize=max_frames)
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.active_page: Page | None = None
        self.cdp: CDPSession | None = None
        self.display: Display | None = None
        self.error_cls: type[BaseException] = RuntimeError

    async def start(self) -> None:
        """Launch the browser, open claude.ai's login page, and begin streaming.

        Raises ``RuntimeError`` when Playwright is unavailable so the route can
        report it to the viewer rather than failing opaquely.
        """
        self.context_dir.mkdir(parents=True, exist_ok=True)
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright not installed — run: uv add playwright && "
                "uv run playwright install chromium"
            ) from exc
        self.error_cls = Error
        self.playwright = await async_playwright().start()
        self.display = virtual_display()
        # The virtual display's own variables — DISPLAY and whatever else it sets.
        launch_env: EnvVars | None = None
        if self.display is not None:
            launch_env = {k: v for k, v in self.display.env().items()}
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.context_dir),
            headless=self.display is None,
            args=LAUNCH_ARGS,
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
            env={**launch_env} if launch_env is not None else None,
        )
        self.context.on("page", self.on_new_page)
        page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        await self.attach(page)
        existing = await self.context.cookies("https://claude.ai")
        if any(BrowserCookie.model_validate(c).name == "sessionKey" for c in existing):
            await self.context.clear_cookies()
        await page.goto(
            "https://claude.ai/login", wait_until="domcontentloaded", timeout=30_000
        )

    async def attach(self, page: "Page") -> None:
        """Point the screencast at ``page``, replacing any previous one."""
        if self.context is None:
            return
        await self.detach_cdp()
        self.active_page = page
        cdp = await self.context.new_cdp_session(page)

        async def on_frame(params: JsonValue) -> None:
            frame = ScreencastFrame.model_validate(params)
            self.enqueue(frame.data)
            try:
                await cdp.send(
                    "Page.screencastFrameAck", {"sessionId": frame.sessionId}
                )
            except self.error_cls:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", SCREENCAST_PARAMS)
        self.cdp = cdp

    def enqueue(self, data: str) -> None:
        """Buffer a frame, dropping the oldest when the viewer lags behind."""
        if not data:
            return
        if self.frames.full():
            try:
                self.frames.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.frames.put_nowait(data)

    def on_new_page(self, page: "Page") -> None:
        """Follow a login popup (e.g. Google SSO) by streaming it instead."""
        page.on("close", self.on_popup_close)
        asyncio.create_task(self.attach(page))

    def on_popup_close(self, _page: "Page") -> None:
        """When a popup closes, return the stream to the main login page."""
        if self.context is not None and self.context.pages:
            asyncio.create_task(self.attach(self.context.pages[0]))

    async def apply_input(self, event: InputEvent) -> None:
        """Replay one viewer pointer/keyboard action into the live page."""
        page = self.active_page
        if page is None:
            return
        try:
            match event.type:
                case "move":
                    await page.mouse.move(event.x, event.y)
                case "down":
                    await page.mouse.move(event.x, event.y)
                    await page.mouse.down(button=event.button)
                case "up":
                    await page.mouse.up(button=event.button)
                case "click":
                    await page.mouse.click(event.x, event.y, button=event.button)
                case "wheel":
                    await page.mouse.wheel(event.dx, event.dy)
                case "key":
                    await self.apply_key(page, event)
        except self.error_cls:
            pass

    async def apply_key(self, page: "Page", event: InputEvent) -> None:
        """Type a printable character, or press a named key (Enter, Backspace, …)."""
        if len(event.text) == 1 and event.text.isprintable():
            await page.keyboard.insert_text(event.text)
        elif event.key:
            await page.keyboard.press(event.key)

    async def has_session(self) -> bool:
        """Whether claude.ai has set a ``sessionKey`` cookie in the live context."""
        if self.context is None:
            return False
        cookies = await self.context.cookies("https://claude.ai")
        return any(
            BrowserCookie.model_validate(c).name == "sessionKey" for c in cookies
        )

    async def wait_for_session(self, *, poll_seconds: float = 1.5) -> None:
        """Block until login completes and a ``sessionKey`` cookie appears."""
        while not await self.has_session():
            await asyncio.sleep(poll_seconds)

    async def detach_cdp(self) -> None:
        """Stop and release the current screencast session, if any."""
        cdp, self.cdp = self.cdp, None
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except self.error_cls:
            pass

    async def aclose(self) -> None:
        """Tear down the screencast, browser, Playwright driver, and display."""
        await self.detach_cdp()
        ctx, self.context = self.context, None
        if ctx is not None:
            try:
                await ctx.close()
            except self.error_cls:
                pass
        pw, self.playwright = self.playwright, None
        if pw is not None:
            await pw.stop()
        display, self.display = self.display, None
        if display is not None:
            display.stop()
