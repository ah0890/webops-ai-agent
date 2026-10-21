"""Store, approve and apply selector repair suggestions.

Approved repairs are written to `config/selector_overrides.yaml` (git-ignored) instead of
rewriting the user's commented `websites.yaml`. During config sync an override is applied only
while the YAML still contains the selector it replaced — if the user edits the selector by hand,
their edit wins.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from sqlmodel import Session, select

from app.browser.selector_healer import SelectorProposal
from app.database.models import Product, SelectorSuggestion, Website, utcnow


def record(session: Session, *, website: Website, product: Product, run_id: int,
           proposal: SelectorProposal) -> tuple[SelectorSuggestion, bool]:
    """Insert or bump a pending suggestion. Returns (suggestion, is_new)."""
    scope = "product" if (product.selectors or {}).get(proposal.field) == proposal.old_selector else "website"
    q = select(SelectorSuggestion).where(
        SelectorSuggestion.website_id == website.id, SelectorSuggestion.field == proposal.field,
        SelectorSuggestion.proposed_selector == proposal.selector, SelectorSuggestion.status == "pending")
    if scope == "product":
        q = q.where(SelectorSuggestion.product_id == product.id)
    existing = session.exec(q).first()
    if existing:
        existing.occurrences += 1
        existing.last_seen_at = utcnow()
        existing.run_id = run_id
        existing.confidence = max(existing.confidence, proposal.confidence)
        session.add(existing)
        session.commit()
        return existing, False
    s = SelectorSuggestion(
        website_id=website.id, product_id=product.id, run_id=run_id, field=proposal.field, scope=scope,  # type: ignore[arg-type]
        old_selector=proposal.old_selector, proposed_selector=proposal.selector, alternatives=proposal.alternatives,
        sample_text=proposal.sample_text, anchor_value=proposal.anchor_value, anchor_source=proposal.anchor_source,
        confidence=proposal.confidence,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s, True


def _load_overrides(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("overrides") or [])


def _save_overrides(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Selector repairs approved in WebOps AI Agent. Applied on top of websites.yaml while the\n"
              "# 'replaces' selector is still the one configured there. Safe to edit or delete.\n")
    path.write_text(header + yaml.safe_dump({"overrides": items}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def approve(session: Session, suggestion_id: int, overrides_file: Path, selector: str | None = None) -> SelectorSuggestion:
    """Apply the suggestion (optionally an alternative selector) to the DB and persist it as an override."""
    s = session.get(SelectorSuggestion, suggestion_id)
    if s is None:
        raise LookupError("suggestion not found")
    if s.status != "pending":
        raise ValueError(f"suggestion is already {s.status}")
    chosen = selector or s.proposed_selector
    if chosen != s.proposed_selector and chosen not in (s.alternatives or []):
        raise ValueError("selector must be the proposal or one of its alternatives")
    site = session.get(Website, s.website_id)
    product = session.get(Product, s.product_id) if s.product_id else None
    assert site
    if s.scope == "product" and product:
        product.selectors = {**(product.selectors or {}), s.field: chosen}
        session.add(product)
    else:
        cfg = dict(site.config or {})
        cfg["selectors"] = {**(cfg.get("selectors") or {}), s.field: chosen}
        site.config = cfg
        session.add(site)
    s.status, s.decided_at, s.proposed_selector = "approved", utcnow(), chosen
    session.add(s)
    for other in session.exec(select(SelectorSuggestion).where(
            SelectorSuggestion.website_id == s.website_id, SelectorSuggestion.field == s.field,
            SelectorSuggestion.status == "pending", SelectorSuggestion.id != s.id)).all():
        if s.scope == "website" or other.product_id == s.product_id:
            other.status, other.decided_at = "superseded", utcnow()
            session.add(other)
    session.commit()

    items = [o for o in _load_overrides(overrides_file)
             if not (o.get("website") == site.name and o.get("field") == s.field
                     and o.get("product_url") == (product.url if s.scope == "product" and product else None))]
    items.append({"website": site.name, "product_url": product.url if s.scope == "product" and product else None,
                  "field": s.field, "selector": chosen, "replaces": s.old_selector,
                  "approved_at": utcnow().isoformat(timespec="seconds"), "suggestion_id": s.id})
    _save_overrides(overrides_file, items)
    return s


def reject(session: Session, suggestion_id: int) -> SelectorSuggestion:
    s = session.get(SelectorSuggestion, suggestion_id)
    if s is None:
        raise LookupError("suggestion not found")
    if s.status != "pending":
        raise ValueError(f"suggestion is already {s.status}")
    s.status, s.decided_at = "rejected", utcnow()
    session.add(s)
    session.commit()
    return s


def apply_overrides(website_name: str, site_selectors: dict, product_selectors: dict[str, dict],
                    overrides_file: Path) -> tuple[dict, dict[str, dict]]:
    """Merge approved overrides into selectors loaded from YAML (pure; used by config sync)."""
    site_selectors = dict(site_selectors)
    product_selectors = {k: dict(v) for k, v in product_selectors.items()}
    for o in _load_overrides(overrides_file):
        if o.get("website") != website_name or not o.get("field") or not o.get("selector"):
            continue
        field, url = o["field"], o.get("product_url")
        target = product_selectors.get(url) if url else site_selectors
        if target is None:
            continue
        if target.get(field) == o.get("replaces"):  # user hasn't changed it since the repair
            target[field] = o["selector"]
    return site_selectors, product_selectors
