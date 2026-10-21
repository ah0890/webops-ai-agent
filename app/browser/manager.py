"""BrowserManager: owns the Playwright/Chromium lifecycle and per-website browser contexts."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, Route, async_playwright

from app.config.settings import Settings
from app.security.url_guard import UrlGuard

log = logging.getLogger(__name__)


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower() or "site"


class BrowserManager:
    """Usage: `async with BrowserManager(settings) as bm: ctx = await bm.new_context(...)`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self._user_agent: str | None = None

    async def __aenter__(self) -> BrowserManager:
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.settings.browser_headless, slow_mo=self.settings.browser_slow_mo_ms or None
        )
        log.info("Chromium %s launched (headless=%s)", self.browser.version, self.settings.browser_headless)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def _default_user_agent(self) -> str:
        """Chromium's real UA plus an identifying suffix — we do not impersonate other browsers."""
        if self._user_agent is None:
            assert self.browser
            ctx = await self.browser.new_context()
            page = await ctx.new_page()
            ua = await page.evaluate("navigator.userAgent")
            await ctx.close()
            suffix = self.settings.user_agent_suffix.strip()
            self._user_agent = f"{ua} {suffix}" if suffix else ua
        return self._user_agent

    def session_path(self, website_name: str) -> Path:
        return self.settings.sessions_dir / f"{slug(website_name)}.json"

    async def new_context(self, website_name: str, guard: UrlGuard, use_saved_session: bool = True) -> BrowserContext:
        assert self.browser, "BrowserManager not started"
        state = self.session_path(website_name)
        ctx = await self.browser.new_context(
            user_agent=await self._default_user_agent(),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            accept_downloads=True,
            storage_state=str(state) if use_saved_session and state.exists() else None,
        )
        ctx.set_default_timeout(self.settings.action_timeout_ms)
        ctx.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
        blocked: list[str] = []
        ctx.webops_blocked_navigations = blocked  # type: ignore[attr-defined]

        async def _guard(route: Route) -> None:
            req = route.request
            # Sub-resources (images, CDN scripts) may live elsewhere; *navigations* must stay on the allowlist.
            if req.is_navigation_request() and not guard.is_allowed(req.url):
                blocked.append(req.url)
                log.warning("blocked navigation outside allowlist: %s", req.url)
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await ctx.route("**/*", _guard)
        return ctx

    async def save_session(self, ctx: BrowserContext, website_name: str) -> None:
        path = self.session_path(website_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(path))
