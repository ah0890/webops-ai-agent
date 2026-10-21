"""PageObserver: classifies the current page state so the runner can react safely.

Detection only — when a CAPTCHA / bot challenge or login wall is detected the product is
marked as blocked. The agent never attempts to solve or bypass these.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Page

_OBSERVE_JS = """
() => {
  const t = (document.body && document.body.innerText || '').slice(0, 6000).toLowerCase();
  const title = (document.title || '').toLowerCase();
  const q = s => !!document.querySelector(s);
  const visiblePassword = [...document.querySelectorAll('input[type=password]')].some(e => e.offsetParent !== null);
  return {
    title: document.title || '',
    text_len: (document.body && document.body.innerText || '').length,
    captcha: q('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], '
             + '#challenge-form, #cf-challenge-running, .g-recaptcha, .h-captcha')
             || /verify you are (a )?human|are you a robot|unusual traffic|press (and|&) hold/.test(t)
             || /^just a moment|attention required|access denied/.test(title),
    password_field: visiblePassword,
    not_found: /\\b404\\b|page not found|no longer available|product not found/.test(title)
             || /^(404|page not found)/.test(t.trim()),
    overlay: q('[id*="cookie" i] button, [class*="cookie" i] button, [id*="consent" i] button, [role=dialog]'),
  };
}
"""


@dataclass
class PageState:
    url: str
    title: str = ""
    http_status: int | None = None
    text_len: int = 0
    captcha: bool = False
    login_wall: bool = False
    not_found: bool = False
    overlay: bool = False
    off_allowlist_navigations: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.captcha or self.login_wall

    @property
    def blank(self) -> bool:
        return self.text_len < 50

    def summary(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False, [], "")}


class PageObserver:
    async def observe(self, page: Page, http_status: int | None = None, expects_login: bool = False) -> PageState:
        try:
            info = await page.evaluate(_OBSERVE_JS)
        except Exception:  # page crashed / navigated mid-evaluate
            info = {}
        blocked = getattr(page.context, "webops_blocked_navigations", [])
        return PageState(
            url=page.url,
            title=info.get("title", ""),
            http_status=http_status,
            text_len=info.get("text_len", 0),
            captcha=bool(info.get("captcha")),
            # A visible password field on a product page usually means we were bounced to a login page.
            login_wall=bool(info.get("password_field")) and not expects_login,
            not_found=bool(info.get("not_found")) or http_status == 404,
            overlay=bool(info.get("overlay")),
            off_allowlist_navigations=list(blocked),
        )
