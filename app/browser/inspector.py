"""Site inspection helper: inspect a real product page *before* writing selectors.

Reports structured data (JSON-LD / microdata / meta), price-looking elements with a
suggested CSS selector, availability hints and login/challenge signals.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from app.browser.extractor import DataExtractor
from app.browser.manager import BrowserManager
from app.browser.observer import PageObserver
from app.config.schema import AvailabilityRules
from app.config.settings import Settings
from app.security.url_guard import UrlGuard
from app.services.structured_data import candidates_from_snapshot

_PRICE_ELEMENTS_JS = r"""
() => {
  const re = /(?:[$€£¥₹₨]|\b(?:USD|EUR|GBP|PKR|INR|AED|SAR|CAD|AUD|Rs\.?)\b)\s?\d[\d.,\s]*|\d[\d.,]*\s?(?:[€£₹]|\b(?:USD|EUR|GBP|PKR|zł|kr)\b)/i;
  const cssPath = (el) => {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return `#${el.id}`;
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 4) {
      let p = el.tagName.toLowerCase();
      const cls = [...el.classList].filter(c => /^[A-Za-z][\w-]*$/.test(c) && !/active|hover|selected|js-/.test(c)).slice(0, 2);
      if (cls.length) p += '.' + cls.join('.');
      if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) { parts.unshift(`#${el.id}`); break; }
      parts.unshift(p);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 2 || el.offsetParent === null) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40 || !re.test(t)) continue;
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    out.push({text: t, selector: cssPath(el), font_size: parseFloat(st.fontSize), top: Math.round(r.top + scrollY),
              strike: st.textDecorationLine.includes('line-through') || !!el.closest('del,s,strike')});
  }
  out.sort((a, b) => (a.strike - b.strike) || (b.font_size - a.font_size) || (a.top - b.top));
  const avail = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 && e.offsetParent !== null)
     .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 60 && /stock|available|sold out|pre-?order/i.test(t)).slice(0, 8);
  return {price_elements: out.slice(0, 12), availability_texts: [...new Set(avail)]};
}
"""


@dataclass
class InspectionReport:
    url: str
    final_url: str = ""
    title: str = ""
    http_status: int | None = None
    page_state: dict = field(default_factory=dict)
    structured_candidates: list[dict] = field(default_factory=list)
    price_elements: list[dict] = field(default_factory=list)
    availability_texts: list[str] = field(default_factory=list)
    screenshot: str | None = None
    recommendation: str = ""


async def inspect_url(url: str, settings: Settings) -> InspectionReport:
    host = urlparse(url).hostname or ""
    guard = UrlGuard([host])
    rep = InspectionReport(url=url)
    async with BrowserManager(settings) as bm:
        ctx = await bm.new_context(f"inspect-{host}", guard, use_saved_session=False)
        page = await ctx.new_page()
        resp = await page.goto(url, wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=15000)
        rep.http_status = resp.status if resp else None
        rep.final_url, rep.title = page.url, await page.title()
        rep.page_state = (await PageObserver().observe(page, rep.http_status)).summary()
        snap = await DataExtractor().snapshot(page, None)
        rep.structured_candidates = [c.model_dump(exclude_none=True)
                                     for c in candidates_from_snapshot(snap, None, AvailabilityRules())]
        scan = await page.evaluate(_PRICE_ELEMENTS_JS)
        rep.price_elements, rep.availability_texts = scan["price_elements"], scan["availability_texts"]
        shot = settings.screenshots_dir / "inspect" / f"{datetime.now():%Y%m%d-%H%M%S}_{host}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(shot))
        rep.screenshot = str(shot)
        await ctx.close()
    if any(c.get("price") is not None for c in rep.structured_candidates):
        rep.recommendation = ("Structured data exposes the price — no selectors required. Optionally add a `price` "
                              "selector from the list below for VERIFIED (corroborated) observations.")
    elif rep.price_elements:
        rep.recommendation = (f"No structured price data. Configure selectors.price, e.g. "
                              f"{rep.price_elements[0]['selector']!r} (verify on several products).")
    else:
        rep.recommendation = ("No price found. The page may need login, cookie consent, or client-side rendering; "
                              "check the screenshot.")
    return rep
