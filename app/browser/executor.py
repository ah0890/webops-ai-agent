"""BrowserExecutor: runs validated BrowserActions against a live page and reports structured results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Response
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.models.actions import (
    BrowserAction,
    ClickAction,
    DownloadAction,
    ExtractAction,
    FillAction,
    GotoAction,
    NavigateBackAction,
    NewTabAction,
    PressAction,
    ScreenshotAction,
    ScrollAction,
    SelectAction,
    SwitchTabAction,
    WaitAction,
)
from app.security.secrets import mask, resolve_env_refs
from app.security.url_guard import UrlGuard, UrlNotAllowed


@dataclass
class ActionResult:
    action: str
    ok: bool
    duration_ms: int
    detail: dict = field(default_factory=dict)
    error: str | None = None
    screenshot: str | None = None  # path relative to the screenshots root
    response: Response | None = None
    data: Any = None


class BrowserExecutor:
    def __init__(self, context: BrowserContext, guard: UrlGuard, screenshots_root: Path, shot_prefix: str,
                 downloads_dir: Path):
        self.context = context
        self.guard = guard
        self.screenshots_root = screenshots_root
        self.shot_prefix = shot_prefix  # e.g. "run_12/acme"
        self.downloads_dir = downloads_dir
        self.pages: list[Page] = []
        self.page: Page | None = None

    async def ensure_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            self.pages.append(self.page)
        return self.page

    def _public(self, action: BrowserAction) -> dict:
        d = action.model_dump(exclude_none=True)
        if isinstance(action, FillAction):
            d["value"] = "****" if action.secret else mask(action.value)
        return d

    async def screenshot(self, name: str, full_page: bool = False) -> str:
        page = await self.ensure_page()
        rel = Path(self.shot_prefix) / f"{datetime.now():%Y%m%d-%H%M%S-%f}_{name}.png"
        out = self.screenshots_root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out), full_page=full_page, timeout=15000)
        return rel.as_posix()

    async def execute(self, action: BrowserAction) -> ActionResult:
        """Execute one action. Never raises for page-level failures; returns ok=False with an error instead."""
        start = time.perf_counter()
        res = ActionResult(action=action.action, ok=True, duration_ms=0, detail=self._public(action))
        try:
            await self._dispatch(action, res)
        except UrlNotAllowed as e:
            res.ok, res.error = False, f"blocked by domain allowlist: {e}"
        except PlaywrightTimeout as e:
            res.ok, res.error = False, f"timeout: {str(e).splitlines()[0]}"
        except PlaywrightError as e:
            res.ok, res.error = False, mask(str(e).splitlines()[0])
        res.duration_ms = int((time.perf_counter() - start) * 1000)
        return res

    async def _dispatch(self, a: BrowserAction, res: ActionResult) -> None:
        page = await self.ensure_page()
        match a:
            case GotoAction():
                self.guard.check(a.url)
                res.response = await page.goto(a.url, wait_until=a.wait_until)
                res.detail["status"] = res.response.status if res.response else None
                res.detail["final_url"] = page.url
            case ClickAction():
                loc = page.locator(a.selector).first
                if a.optional and await loc.count() == 0:
                    res.detail["skipped"] = "element not present"
                    return
                await loc.click(timeout=3000 if a.optional else None)
            case FillAction():
                value = resolve_env_refs(a.value)
                await page.locator(a.selector).first.fill(value)
            case SelectAction():
                await page.locator(a.selector).first.select_option(a.value)
            case ScrollAction():
                if a.to == "bottom":
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                elif a.to == "top":
                    await page.evaluate("window.scrollTo(0, 0)")
                elif a.to == "selector" and a.selector:
                    await page.locator(a.selector).first.scroll_into_view_if_needed()
                else:
                    await page.mouse.wheel(0, a.pixels)
            case WaitAction():
                try:
                    if a.selector:
                        await page.locator(a.selector).first.wait_for(state="visible")
                    if a.load_state:
                        await page.wait_for_load_state(a.load_state)
                    if a.milliseconds:
                        await page.wait_for_timeout(a.milliseconds)
                except PlaywrightTimeout:
                    if not a.optional:
                        raise
                    res.detail["skipped"] = "wait condition not met (optional)"
            case ExtractAction():
                res.data = await page.locator(a.selector or "body").first.inner_text()
            case ScreenshotAction():
                res.screenshot = await self.screenshot(a.name, a.full_page)
            case DownloadAction():
                async with page.expect_download() as dl_info:
                    await page.locator(a.selector).first.click()
                dl = await dl_info.value
                self.downloads_dir.mkdir(parents=True, exist_ok=True)
                target = self.downloads_dir / Path(dl.suggested_filename).name
                await dl.save_as(str(target))
                res.detail["saved_to"] = str(target)
            case NavigateBackAction():
                await page.go_back()
                if not self.guard.is_allowed(page.url):
                    raise UrlNotAllowed(page.url)
            case NewTabAction():
                self.guard.check(a.url)
                self.page = await self.context.new_page()
                self.pages.append(self.page)
                res.response = await self.page.goto(a.url)
            case SwitchTabAction():
                if a.index >= len(self.pages):
                    raise PlaywrightError(f"no tab {a.index}")
                self.page = self.pages[a.index]
                await self.page.bring_to_front()
            case PressAction():
                await page.keyboard.press(a.key)
