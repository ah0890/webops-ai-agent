"""DataExtractor: one DOM snapshot → deterministic candidates → optional evidence-checked LLM fallback."""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page
from pydantic import BaseModel, Field

from app.agent.llm import LLMProvider, LLMUnavailable
from app.config.schema import AvailabilityRules, SelectorSet
from app.models.extraction import Candidate, ExtractionResult, PageSnapshot
from app.services.price_parser import parse_price
from app.services.structured_data import candidates_from_snapshot, choose, normalise_availability

log = logging.getLogger(__name__)

_SNAPSHOT_JS = """
(sel) => {
  const pick = (spec) => {
    if (!spec) return {value: null, missing: false, configured: false};
    let css = spec, attr = null;
    const at = spec.lastIndexOf('@');
    if (at > 0 && /^[A-Za-z_:][\\w:.-]*$/.test(spec.slice(at + 1))) { css = spec.slice(0, at); attr = spec.slice(at + 1); }
    let el = null;
    try { el = document.querySelector(css); } catch (e) { return {value: null, missing: true, configured: true, invalid: true}; }
    if (!el) return {value: null, missing: true, configured: true};
    const v = attr ? el.getAttribute(attr) : (el.innerText || el.textContent || '');
    return {value: v ? v.trim() : null, missing: false, configured: true};
  };
  const selector_values = {}, selector_missing = [];
  for (const [k, spec] of Object.entries(sel || {})) {
    const r = pick(spec);
    selector_values[k] = r.value;
    if (r.missing) selector_missing.push(k);
  }
  const json_ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => s.textContent || '');
  const scope = document.querySelector('[itemtype*="schema.org/Product" i]');
  const md = {};
  if (scope) {
    const get = (p) => {
      const el = scope.querySelector(`[itemprop="${p}"]`);
      if (!el) return null;
      return (el.getAttribute('content') || el.getAttribute('href') || el.innerText || el.textContent || '').trim() || null;
    };
    for (const p of ['name', 'price', 'priceCurrency', 'availability', 'sku', 'lowPrice']) md[p] = get(p);
    if (!md.price && md.lowPrice) md.price = md.lowPrice;
  }
  const meta = {};
  for (const m of document.querySelectorAll('meta[property], meta[name]')) {
    const k = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    if (/price|availability|retailer_item_id|^og:title$/.test(k)) meta[k] = m.getAttribute('content') || '';
  }
  return {
    url: location.href, title: document.title || null, json_ld, microdata: md, meta,
    selector_values, selector_missing,
    text_excerpt: (document.body && document.body.innerText || '').slice(0, 20000),
  };
}
"""


class LLMExtraction(BaseModel):
    found: bool = Field(description="False if the page does not show this product's price")
    product_name: str | None = None
    price_text: str | None = Field(None, description="Price exactly as written on the page, verbatim")
    currency: str | None = Field(None, description="ISO 4217 code if determinable")
    availability_text: str | None = Field(None, description="Availability wording exactly as on the page")
    sku: str | None = None
    evidence: str | None = Field(None, description="Short verbatim snippet from the page containing the price")


_LLM_SYSTEM = """You read the visible text of a single product page and report the CURRENT selling price of the \
main product. Copy values verbatim from the text — never compute, convert or guess. If several prices appear \
(e.g. was/now, bundles, related products) report the current price of the main product only. If you are unsure, \
set found=false."""


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class DataExtractor:
    def __init__(self, provider: LLMProvider | None = None, llm_fallback: bool = True):
        self.provider = provider if llm_fallback else None

    async def snapshot(self, page: Page, selectors: SelectorSet | None) -> PageSnapshot:
        sel = selectors.model_dump(exclude_none=True) if selectors else {}
        data = await page.evaluate(_SNAPSHOT_JS, sel)
        return PageSnapshot.model_validate(data)

    async def extract(self, page: Page, *, requested_url: str, http_status: int | None,
                      selectors: SelectorSet | None, rules: AvailabilityRules, expected_currency: str | None,
                      product_name: str) -> ExtractionResult:
        result = ExtractionResult(url=requested_url, http_status=http_status)
        try:
            snap = await self.snapshot(page, selectors)
        except Exception as e:
            result.error = f"could not read page: {str(e).splitlines()[0]}"
            return result
        result.final_url, result.page_title = snap.url, snap.title
        result.selector_missing = snap.selector_missing
        result.candidates = candidates_from_snapshot(snap, expected_currency, rules)
        if not any(c.price is not None for c in result.candidates) and self.provider and snap.text_excerpt:
            llm = self._llm_candidate(snap, product_name, expected_currency, rules)
            if llm:
                result.candidates.append(llm)
        result.chosen = choose(result.candidates)
        return result

    def _llm_candidate(self, snap: PageSnapshot, product_name: str, expected_currency: str | None,
                       rules: AvailabilityRules) -> Candidate | None:
        text = snap.text_excerpt[:12000]
        prompt = (f"Expected product: {product_name}\nURL: {snap.url}\nPage title: {snap.title}\n"
                  f"Expected currency (if known): {expected_currency or 'unknown'}\n\n--- PAGE TEXT ---\n{text}")
        try:
            out = self.provider.structured(_LLM_SYSTEM, prompt, LLMExtraction, max_tokens=2000)  # type: ignore[union-attr]
        except LLMUnavailable as e:
            log.info("LLM extraction fallback unavailable: %s", e)
            return None
        if not out.found or not out.price_text:
            return None
        page_text = _squash(text)
        # Anti-hallucination: the price (and evidence) must literally appear on the page.
        if _squash(out.price_text) not in page_text or (out.evidence and _squash(out.evidence) not in page_text):
            log.warning("discarded LLM extraction: value not found verbatim on page (%r)", out.price_text)
            return None
        parsed = parse_price(out.price_text, out.currency or expected_currency)
        return Candidate(
            source="llm", product_name=out.product_name, price_text=out.price_text, price=parsed.amount,
            currency=parsed.currency or (out.currency.upper() if out.currency else None),
            availability_raw=out.availability_text,
            availability=normalise_availability(out.availability_text, rules) if out.availability_text else None,
            sku=out.sku, ambiguous=parsed.ambiguous, notes=parsed.notes + [f"evidence: {out.evidence!r}"],
        )
