"""Selector self-healing: propose a replacement when a configured selector stops working.

A proposal is only made when there is an independent *anchor* for the expected value:
  * the page's own structured data (JSON-LD / microdata / meta) — strong anchor, or
  * the product's previous trusted price — weaker anchor (price may legitimately have changed), or
  * for name / availability text without structured data: wording Claude copied from the page,
    accepted only if it appears verbatim on the page and passes field checks — medium anchor.
Every candidate selector is verified against the live DOM (it must select the element showing
the anchor value). Proposals are never applied automatically; a human approves them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Page
from pydantic import BaseModel, Field

from app.agent.llm import LLMProvider, LLMUnavailable
from app.config.schema import AvailabilityRules, SelectorSet
from app.models.extraction import ExtractionResult
from app.services.price_parser import parse_price
from app.services.structured_data import normalise_availability

log = logging.getLogger(__name__)

_STRUCTURED = ("json_ld", "microdata", "meta")
_AGREE_PCT = 0.5

# Collects visible leaf-ish elements and, for each, several candidate selectors with live match counts.
_CANDIDATES_JS = r"""
(args) => {
  const {mode, needle} = args;
  // Generated CSS-in-JS names ("sc-AxjAm", "css-1q2w3e") have digits or mixed case; plain words like
  // "availability" or "amount" are meaningful and must survive.
  const hashed = c => (/\d/.test(c) && /^[a-z]{1,4}-?[A-Za-z0-9]{5,}$/.test(c))
                      || /^[a-z]{1,4}-[A-Za-z0-9]*[A-Z][A-Za-z0-9]*$/.test(c);
  const stableClass = c => /^[A-Za-z][\w-]{1,40}$/.test(c) && !/\d{3,}|^(active|hover|selected|is-|js-|css-|sc-|jsx-)/i.test(c)
                          && !hashed(c);
  // Layout/utility classes (grid, spacing, framework helpers) say nothing about meaning and change often.
  const layout = c => /^(col|row|container|wrapper|inner|clearfix|d-|flex|grid|span\d|pull-|float-|[mp][trblxy]?-\d|w-\d|h-\d|text-(left|right|center)|hidden|visible|block|inline)/i.test(c);
  const goodClass = c => stableClass(c) && !layout(c);
  const esc = s => CSS.escape(s);
  const tagCls = el => {
    const cls = [...el.classList].filter(goodClass).slice(0, 2);
    return el.tagName.toLowerCase() + cls.map(c => '.' + esc(c)).join('');
  };
  const anchorOf = el => {
    let p = el.parentElement, depth = 0;
    while (p && p !== document.body && depth < 4) {
      if (p.id && /^[A-Za-z][\w-]*$/.test(p.id) && !/\d{4,}/.test(p.id)) return '#' + esc(p.id);
      const cls = [...p.classList].filter(goodClass);
      if (cls.length) return p.tagName.toLowerCase() + '.' + esc(cls[0]);
      p = p.parentElement; depth++;
    }
    return null;
  };
  const selectorsFor = el => {
    const out = [];
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id) && !/\d{4,}/.test(el.id)) out.push(['id', '#' + esc(el.id)]);
    for (const a of el.getAttributeNames()) {
      if (a === 'itemprop') out.push(['itemprop', `[itemprop="${el.getAttribute(a)}"]`]);
      else if (/^data-(test|testid|qa|cy|price|automation|component|role)/i.test(a)) {
        const v = el.getAttribute(a);
        out.push(['data', v && v.length < 60 && !/\d{5,}/.test(v) ? `[${a}="${v}"]` : `[${a}]`]);
      }
    }
    const tc = tagCls(el);
    const anc = anchorOf(el);
    if (anc) out.push(['scoped', `${anc} ${tc}`]);
    if (tc.includes('.')) out.push(['class', tc]);
    return out;
  };
  const results = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.offsetParent === null || el.children.length > 3) continue;
    const text = (el.innerText || '').trim();
    if (!text || text.length > (mode === 'price' ? 40 : 200)) continue;
    if (mode === 'price' && !/\d/.test(text)) continue;
    if (mode === 'text' && text.replace(/\s+/g, ' ').toLowerCase() !== needle) continue;
    if (mode === 'contains' && !text.replace(/\s+/g, ' ').toLowerCase().includes(needle)) continue;
    const st = getComputedStyle(el);
    const sels = selectorsFor(el).map(([kind, sel]) => {
      let n = 0, first = false;
      try { const all = document.querySelectorAll(sel); n = all.length; first = all[0] === el; } catch (e) { n = -1; }
      return {kind, selector: sel, matches: n, selects_element: first};
    }).filter(s => s.selects_element);
    if (!sels.length) continue;
    results.push({text, font_size: parseFloat(st.fontSize),
                  struck: st.textDecorationLine.includes('line-through') || !!el.closest('del,s,strike'),
                  selectors: sels});
    if (results.length > 400) break;
  }
  return results;
}
"""

_KIND_SCORE = {"id": 0.95, "itemprop": 0.9, "data": 0.9, "scoped": 0.85, "class": 0.75}


@dataclass
class Anchor:
    field: str
    value: float | str
    source: str  # json_ld | microdata | meta | structured | previous_observation | llm
    strength: str  # strong | medium | weak


@dataclass
class SelectorProposal:
    field: str
    old_selector: str | None
    selector: str
    kind: str
    sample_text: str
    anchor_value: str
    anchor_source: str
    confidence: float
    alternatives: list[str] = field(default_factory=list)


def _structured_price(extraction: ExtractionResult) -> tuple[float, str] | None:
    vals = [(c.price, c.source) for c in extraction.candidates
            if c.source in _STRUCTURED and c.price is not None and not c.ambiguous and c.price > 0]
    if not vals:
        return None
    ref = vals[0][0]
    if any(abs(v - ref) / ref * 100 > _AGREE_PCT for v, _ in vals):
        return None  # structured sources disagree with each other — no reliable anchor
    return ref, vals[0][1]


def find_anchors(extraction: ExtractionResult, selectors: SelectorSet | None,
                 previous_price: float | None) -> list[tuple[Anchor, str | None]]:
    """Which configured selectors look broken, and what value should they select instead?"""
    if not selectors:
        return []
    out: list[tuple[Anchor, str | None]] = []
    sel_cand = next((c for c in extraction.candidates if c.source == "selector"), None)
    if selectors.price:
        structured = _structured_price(extraction)
        sel_price = sel_cand.price if sel_cand else None
        broken = "price" in extraction.selector_missing or sel_price is None or (
            structured is not None and abs(sel_price - structured[0]) / structured[0] * 100 > _AGREE_PCT)
        if broken:
            if structured:
                out.append((Anchor("price", structured[0], structured[1], "strong"), selectors.price))
            elif previous_price and "price" in extraction.selector_missing:
                out.append((Anchor("price", previous_price, "previous_observation", "weak"), selectors.price))
    if selectors.product_name and "product_name" in extraction.selector_missing:
        name = next((c.product_name for c in extraction.candidates if c.source in _STRUCTURED and c.product_name), None)
        if name:
            out.append((Anchor("product_name", name, "structured", "strong"), selectors.product_name))
    return out


_TEXT_FIELDS = ("product_name", "availability")


def unanchored_fields(extraction: ExtractionResult, selectors: SelectorSet | None,
                      anchors: list[tuple[Anchor, str | None]]) -> list[str]:
    """Broken text selectors that structured data could not anchor — candidates for an LLM anchor."""
    if not selectors:
        return []
    have = {a.field for a, _ in anchors}
    return [f for f in _TEXT_FIELDS
            if getattr(selectors, f) and f in extraction.selector_missing and f not in have]


class LLMTextAnchors(BaseModel):
    product_name: str | None = Field(None, description="Main product's name exactly as written on the page")
    availability_text: str | None = Field(
        None, description="The main product's stock/availability wording exactly as written on the page")


_LLM_ANCHOR_SYSTEM = """You read the visible text of a single product page. Copy, verbatim and character for \
character, the requested values for the MAIN product only: its name as shown in the page heading, and the short \
phrase stating its stock availability (e.g. "In stock (12 available)", "Sold out"). Never paraphrase, translate, \
combine separate lines or guess. Use null for anything not clearly shown."""


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 3}


@dataclass
class LLMHealBudget:
    """Bounds LLM anchor calls per website per run; a field stops asking once it has a proposal."""

    remaining: int = 3
    satisfied: set[str] = field(default_factory=set)


def llm_anchors(provider: LLMProvider, page_text: str, fields: list[str], *, product_name: str,
                extraction: ExtractionResult, rules: AvailabilityRules | None) -> list[Anchor]:
    """Ask the LLM for verbatim name/availability wording and keep only values that pass evidence checks."""
    text = page_text[:12000]
    wanted = ", ".join("name" if f == "product_name" else "availability wording" for f in fields)
    prompt = f"Expected product: {product_name}\nReport: {wanted}\n\n--- PAGE TEXT ---\n{text}"
    try:
        out = provider.structured(_LLM_ANCHOR_SYSTEM, prompt, LLMTextAnchors, max_tokens=1000)
    except LLMUnavailable as e:
        log.info("LLM selector anchor unavailable: %s", e)
        return []
    page = _squash(text)
    anchors: list[Anchor] = []
    if "product_name" in fields and out.product_name:
        value = out.product_name.strip()
        expected = _tokens(product_name)
        if _squash(value) not in page:
            log.warning("discarded LLM name anchor: not on page (%r)", value)
        elif expected and not expected & _tokens(value):
            log.warning("discarded LLM name anchor: unrelated to %r (%r)", product_name, value)
        else:
            anchors.append(Anchor("product_name", value, "llm", "medium"))
    if "availability" in fields and out.availability_text:
        value = out.availability_text.strip()
        status = normalise_availability(value, rules)
        structured = next((c.availability for c in extraction.candidates
                           if c.source in _STRUCTURED and c.availability not in (None, "unknown")), None)
        if _squash(value) not in page or len(value) > 120:
            log.warning("discarded LLM availability anchor: not on page (%r)", value)
        elif status == "unknown" or (structured and structured != status):
            log.warning("discarded LLM availability anchor: %r reads as %s (structured: %s)", value, status, structured)
        else:
            anchors.append(Anchor("availability", value, "llm", "medium"))
    return anchors


def rank(raw: list[dict], anchor: Anchor, old_selector: str | None, expected_currency: str | None) -> SelectorProposal | None:
    """Pure ranking of DOM candidates (unit-testable)."""
    scored: list[tuple[float, dict, dict]] = []
    max_font = max((r["font_size"] for r in raw), default=0) or 1
    needle = _squash(str(anchor.value))
    for r in raw:
        precision = 0.0
        if anchor.field == "price":
            p = parse_price(r["text"], expected_currency)
            if p.amount is None or p.ambiguous or abs(p.amount - float(anchor.value)) > 0.005 * float(anchor.value):
                continue
            if r["struck"]:
                continue  # old/crossed-out price
        else:
            text = _squash(r["text"])
            if needle not in text:
                continue
            precision = 0.1 * (1 - len(needle) / len(text))  # wrapper elements with extra text rank lower
        for s in r["selectors"]:
            if s["selector"] == old_selector:
                continue
            score = _KIND_SCORE.get(s["kind"], 0.6) - precision
            if s["matches"] > 1:
                score -= 0.15
            score += 0.05 * (r["font_size"] / max_font)
            if anchor.strength == "weak":
                score -= 0.2
            elif anchor.strength == "medium":
                score -= 0.1
            scored.append((score, s, r))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], len(t[1]["selector"])))
    best, s, r = scored[0]
    alts = []
    for _, s2, _ in scored[1:]:
        if s2["selector"] not in alts and s2["selector"] != s["selector"]:
            alts.append(s2["selector"])
    return SelectorProposal(
        field=anchor.field, old_selector=old_selector, selector=s["selector"], kind=s["kind"],
        sample_text=r["text"][:200], anchor_value=str(anchor.value), anchor_source=anchor.source,
        confidence=round(max(0.05, min(best, 0.99)), 2), alternatives=alts[:4],
    )


async def propose(page: Page, extraction: ExtractionResult, selectors: SelectorSet | None,
                  previous_price: float | None, expected_currency: str | None, *,
                  provider: LLMProvider | None = None, budget: LLMHealBudget | None = None,
                  product_name: str = "", rules: AvailabilityRules | None = None) -> list[SelectorProposal]:
    anchors = find_anchors(extraction, selectors, previous_price)
    if provider and budget and selectors:
        fields = [f for f in unanchored_fields(extraction, selectors, anchors) if f not in budget.satisfied]
        if fields and budget.remaining > 0:
            budget.remaining -= 1
            text = await page.evaluate("document.body ? document.body.innerText.slice(0, 12000) : ''")
            found = await asyncio.to_thread(llm_anchors, provider, text, fields, product_name=product_name,
                                            extraction=extraction, rules=rules)
            anchors += [(a, getattr(selectors, a.field)) for a in found]
    proposals: list[SelectorProposal] = []
    for anchor, old in anchors:
        # Availability wording usually sits inside a longer phrase ("In stock (12 available)").
        mode = "price" if anchor.field == "price" else ("contains" if anchor.field == "availability" else "text")
        raw = await page.evaluate(_CANDIDATES_JS, {"mode": mode, "needle": _squash(str(anchor.value))})
        prop = rank(raw, anchor, old, expected_currency)
        if prop:
            proposals.append(prop)
            if budget is not None and anchor.source == "llm":
                budget.satisfied.add(anchor.field)
    return proposals
