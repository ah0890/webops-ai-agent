"""Turn a raw page snapshot into extraction candidates (deterministic, no LLM).

Strategy order mirrors reliability: user-configured selectors > schema.org JSON-LD >
microdata > OpenGraph/product meta tags.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config.schema import AvailabilityRules
from app.models.extraction import Candidate, PageSnapshot
from app.services.price_parser import parse_price

_SCHEMA_AVAIL = {
    "instock": "in_stock", "limitedavailability": "in_stock", "instoreonly": "in_stock",
    "onlineonly": "in_stock", "outofstock": "out_of_stock", "soldout": "out_of_stock",
    "discontinued": "out_of_stock", "preorder": "preorder", "presale": "preorder", "backorder": "preorder",
}


def normalise_availability(raw: str | None, rules: AvailabilityRules | None = None) -> str:
    if not raw:
        return "unknown"
    key = re.sub(r"[^a-z]", "", raw.rsplit("/", 1)[-1].lower())
    if key in _SCHEMA_AVAIL:
        return _SCHEMA_AVAIL[key]
    rules = rules or AvailabilityRules()
    text = raw.lower()
    # Order matters: "not available" contains "available".
    if any(p.lower() in text for p in rules.out_of_stock):
        return "out_of_stock"
    if any(p.lower() in text for p in rules.preorder):
        return "preorder"
    if any(p.lower() in text for p in rules.in_stock):
        return "in_stock"
    return "unknown"


def _iter_nodes(obj: Any):
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_nodes(x)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in obj:
                yield from _iter_nodes(obj[k])


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.lower() in ("product", "productgroup") for x in types)


def _first_offer(offers: Any) -> dict | None:
    if isinstance(offers, list):
        return next((o for o in offers if isinstance(o, dict)), None)
    if isinstance(offers, dict):
        if isinstance(offers.get("offers"), (list, dict)) and "price" not in offers:  # AggregateOffer.offers
            inner = _first_offer(offers["offers"])
            if inner:
                return inner
        return offers
    return None


def from_json_ld(blocks: list[str], expected_currency: str | None, rules: AvailabilityRules) -> list[Candidate]:
    out: list[Candidate] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_nodes(data):
            if not _is_product(node):
                continue
            offer = _first_offer(node.get("offers")) or {}
            price_val = offer.get("price", offer.get("lowPrice"))
            if price_val is None and isinstance(offer.get("priceSpecification"), dict):
                price_val = offer["priceSpecification"].get("price")
            ccy = offer.get("priceCurrency") or expected_currency
            parsed = parse_price(str(price_val) if price_val is not None else None, ccy)
            avail_raw = offer.get("availability")
            cand = Candidate(
                source="json_ld",
                product_name=_s(node.get("name")),
                price_text=_s(price_val),
                price=parsed.amount,
                currency=(offer.get("priceCurrency") or parsed.currency),
                availability_raw=_s(avail_raw),
                availability=normalise_availability(_s(avail_raw), rules) if avail_raw else None,
                sku=_s(node.get("sku") or node.get("mpn") or offer.get("sku")),
                ambiguous=parsed.ambiguous,
                notes=parsed.notes,
            )
            if offer.get("@type") == "AggregateOffer" and offer.get("lowPrice") != offer.get("highPrice"):
                cand.notes.append("AggregateOffer price range; using lowPrice")
                cand.ambiguous = True
            out.append(cand)
    return out


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return None
    s = " ".join(str(v).split())
    return s or None


def from_microdata(md: dict[str, str | None], expected_currency: str | None, rules: AvailabilityRules) -> list[Candidate]:
    if not md or not md.get("price"):
        return []
    ccy = md.get("priceCurrency") or expected_currency
    parsed = parse_price(md["price"], ccy)
    return [Candidate(
        source="microdata", product_name=_s(md.get("name")), price_text=md["price"], price=parsed.amount,
        currency=md.get("priceCurrency") or parsed.currency, availability_raw=md.get("availability"),
        availability=normalise_availability(md.get("availability"), rules) if md.get("availability") else None,
        sku=_s(md.get("sku")), ambiguous=parsed.ambiguous, notes=parsed.notes,
    )]


def from_meta(meta: dict[str, str], expected_currency: str | None, rules: AvailabilityRules) -> list[Candidate]:
    price = meta.get("product:price:amount") or meta.get("og:price:amount")
    if not price:
        return []
    ccy = meta.get("product:price:currency") or meta.get("og:price:currency") or expected_currency
    parsed = parse_price(price, ccy)
    avail = meta.get("product:availability") or meta.get("og:availability")
    return [Candidate(
        source="meta", product_name=_s(meta.get("og:title")), price_text=price, price=parsed.amount,
        currency=ccy or parsed.currency, availability_raw=avail,
        availability=normalise_availability(avail, rules) if avail else None,
        sku=_s(meta.get("product:retailer_item_id")), ambiguous=parsed.ambiguous, notes=parsed.notes,
    )]


def from_selectors(values: dict[str, str | None], expected_currency: str | None, rules: AvailabilityRules) -> list[Candidate]:
    if not values or not any(values.values()):
        return []
    price_text = values.get("price")
    ccy_hint = values.get("currency")
    parsed = parse_price(price_text, expected_currency)
    currency = parsed.currency
    if ccy_hint:
        c2 = parse_price(f"{ccy_hint} 1", expected_currency).currency
        currency = c2 or currency
    avail_raw = values.get("availability")
    return [Candidate(
        source="selector", product_name=_s(values.get("product_name")), price_text=price_text,
        price=parsed.amount if price_text else None, currency=currency or (expected_currency if price_text and
        expected_currency in parsed.currency_candidates else None),
        availability_raw=avail_raw, availability=normalise_availability(avail_raw, rules) if avail_raw else None,
        sku=_s(values.get("sku")), ambiguous=parsed.ambiguous, notes=parsed.notes if price_text else [],
    )]


def candidates_from_snapshot(snap: PageSnapshot, expected_currency: str | None,
                             rules: AvailabilityRules | None = None) -> list[Candidate]:
    rules = rules or AvailabilityRules()
    return (
        from_selectors(snap.selector_values, expected_currency, rules)
        + from_json_ld(snap.json_ld, expected_currency, rules)
        + from_microdata(snap.microdata, expected_currency, rules)
        + from_meta(snap.meta, expected_currency, rules)
    )


def choose(candidates: list[Candidate]) -> Candidate | None:
    """Pick the best candidate with a price; fill gaps (name/sku/availability) from the others."""
    priced = [c for c in candidates if c.price is not None]
    if not priced:
        base = next(iter(candidates), None)
        if base is None:
            return None
        chosen = base.model_copy(deep=True)
    else:
        chosen = priced[0].model_copy(deep=True)
    for c in candidates:
        chosen.product_name = chosen.product_name or c.product_name
        chosen.sku = chosen.sku or c.sku
        chosen.currency = chosen.currency or c.currency
        if not chosen.availability or chosen.availability == "unknown":
            chosen.availability = c.availability or chosen.availability
            chosen.availability_raw = chosen.availability_raw or c.availability_raw
    return chosen
