"""Decide how much an extracted value can be trusted.

Status rules:
  * any `fatal` issue          → FAILED        (never used for comparisons)
  * any `review` issue         → NEEDS_REVIEW  (stored, flagged, never used for comparisons)
  * any `warning`              → LIKELY_VALID
  * clean + corroborated       → VERIFIED      (configured selector, or ≥2 independent sources agree)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.database.models import ExtractionStatus
from app.models.extraction import ExtractionResult, Issue, ValidationOutcome


@dataclass
class ProductContext:
    name: str
    url: str
    sku: str | None = None
    currency: str | None = None
    expected_name_contains: str | None = None


@dataclass
class PreviousObservation:
    price: float | None
    currency: str | None
    availability: str


@dataclass
class ValidationPolicy:
    supported_currencies: set[str]
    max_plausible_change_pct: float = 80.0
    disagreement_tolerance_pct: float = 0.5


def _norm_sku(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _norm_path(url: str) -> str:
    return (urlparse(url).path or "/").rstrip("/").lower() or "/"


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}


def validate_extraction(
    result: ExtractionResult,
    product: ProductContext,
    previous: PreviousObservation | None,
    policy: ValidationPolicy,
) -> ValidationOutcome:
    issues: list[Issue] = []

    def add(code: str, severity: str, message: str) -> None:
        issues.append(Issue(code=code, severity=severity, message=message))  # type: ignore[arg-type]

    c = result.chosen
    if result.error:
        add("extraction_error", "fatal", result.error)
    if result.http_status and result.http_status >= 400:
        add("http_error", "fatal", f"page returned HTTP {result.http_status}")
    if c is None:
        add("no_data", "fatal", "no product data found on page (no selector, JSON-LD, microdata or meta match)")
        return _finish(issues, ValidationOutcome(status=ExtractionStatus.FAILED.value, issues=issues))

    availability = c.availability or "unknown"
    price, currency = c.price, c.currency

    # --- price ---
    if price is None:
        if availability == "out_of_stock":
            add("price_missing_unavailable", "warning", "no price shown; product is out of stock")
        else:
            add("missing_price", "fatal", f"price not found or unparseable ({'; '.join(c.notes) or 'no text'})")
    elif price < 0:
        add("negative_price", "fatal", f"negative price {price}")
    elif price == 0:
        add("zero_price", "review", "price parsed as 0")
    if c.ambiguous:
        add("ambiguous_price", "review", "; ".join(c.notes) or "price text is ambiguous")

    # --- currency ---
    if price is not None:
        if not currency:
            if product.currency:
                currency = product.currency
                add("currency_assumed", "warning", f"currency not shown on page; assumed configured {currency}")
            else:
                add("currency_unknown", "review", "currency not detected and none configured")
        if currency and currency not in policy.supported_currencies:
            add("unsupported_currency", "review", f"currency {currency} is not in SUPPORTED_CURRENCIES")
        if currency and product.currency and currency != product.currency:
            add("currency_mismatch", "review", f"page shows {currency}, configured {product.currency}")
        if previous and previous.currency and currency and previous.currency != currency:
            add("currency_changed", "review", f"currency changed {previous.currency} → {currency}")

    # --- plausibility vs history ---
    if price and previous and previous.price and (not previous.currency or previous.currency == currency):
        pct = (price - previous.price) / previous.price * 100
        if abs(pct) > policy.max_plausible_change_pct:
            add("implausible_change", "review",
                f"{pct:+.1f}% vs previous {previous.price:g} exceeds plausibility limit "
                f"±{policy.max_plausible_change_pct:g}%")

    # --- cross-source agreement ---
    priced = [x for x in result.candidates if x.price is not None and x.source != "llm"]
    distinct_sources = {x.source for x in priced}
    if price and priced:
        far = [x for x in priced if abs(x.price - price) / price * 100 > policy.disagreement_tolerance_pct]
        if far:
            add("sources_disagree", "review", "extraction sources disagree: " + ", ".join(
                f"{x.source}={x.price:g}" for x in priced))

    # --- right page / right product ---
    if result.final_url and _norm_path(result.final_url) != _norm_path(product.url):
        add("redirected", "review", f"page redirected to {result.final_url}")
    seen_name = c.product_name or ""
    if product.expected_name_contains and product.expected_name_contains.lower() not in seen_name.lower():
        add("wrong_product", "review",
            f"page product name {seen_name!r} does not contain {product.expected_name_contains!r}")
    elif seen_name and _tokens(product.name) and not (_tokens(product.name) & _tokens(seen_name)):
        add("name_mismatch", "warning", f"page name {seen_name!r} shares no words with configured {product.name!r}")
    if product.sku:
        if c.sku and _norm_sku(c.sku) != _norm_sku(product.sku):
            add("sku_mismatch", "review", f"page SKU {c.sku!r} differs from configured {product.sku!r}")
        elif not c.sku:
            add("sku_not_on_page", "info", "configured SKU not exposed on page")

    # --- page structure / method ---
    if result.selector_missing:
        add("selector_missing", "warning",
            f"configured selector(s) not found: {', '.join(result.selector_missing)} — page structure may have changed")
    if c.source == "llm":
        add("llm_extracted", "warning", "value extracted by the LLM fallback (evidence-checked against page text)")

    outcome = ValidationOutcome(
        status="", issues=issues, price=price, currency=currency, availability=availability,
        product_name=c.product_name, sku=c.sku, price_text=c.price_text, method=c.source,
    )
    corroborated = c.source == "selector" or len(distinct_sources) >= 2
    return _finish(issues, outcome, corroborated)


def _finish(issues: list[Issue], outcome: ValidationOutcome, corroborated: bool = False) -> ValidationOutcome:
    sev = {i.severity for i in issues}
    if "fatal" in sev:
        outcome.status = ExtractionStatus.FAILED.value
    elif "review" in sev:
        outcome.status = ExtractionStatus.NEEDS_REVIEW.value
    elif "warning" in sev or not corroborated:
        outcome.status = ExtractionStatus.LIKELY_VALID.value
    else:
        outcome.status = ExtractionStatus.VERIFIED.value
    outcome.issues = issues
    return outcome
