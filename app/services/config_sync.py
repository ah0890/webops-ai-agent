"""Upsert the user's YAML configuration into the database (never deletes history)."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from app.config.schema import WebsiteConfig, WebsitesFile, WorkflowsFile
from app.database.models import Product, Website, Workflow, utcnow


def website_runtime_config(w: WebsiteConfig) -> dict:
    return w.model_dump(mode="json", include={
        "selectors", "availability_rules", "pre_extract_actions", "login",
        "request_delay_seconds", "respect_robots_txt",
    }, exclude_none=True)


def upsert_website(session: Session, w: WebsiteConfig, source: str = "config") -> tuple[Website, bool]:
    row = session.exec(select(Website).where(Website.name == w.name)).first()
    created = row is None
    if row is None:
        row = Website(name=w.name, base_url=w.base_url, source=source)
    row.base_url = w.base_url
    row.allowed_domains = w.allowed_domains
    row.currency = w.currency
    row.active = w.active
    row.config = website_runtime_config(w)
    row.updated_at = utcnow()
    session.add(row)
    session.flush()
    return row, created


def upsert_product(session: Session, website: Website, *, name: str, url: str, sku: str | None,
                   currency: str | None, threshold: float | None, active: bool,
                   expected_name_contains: str | None = None, selectors: dict | None = None,
                   source: str = "config") -> tuple[Product, bool]:
    row = session.exec(select(Product).where(Product.url == url)).first()
    created = row is None
    if row is None:
        row = Product(website_id=website.id, name=name, url=url, source=source)
    row.website_id = website.id  # type: ignore[assignment]
    row.name, row.sku, row.currency = name, sku, currency
    row.price_change_threshold, row.active = threshold, active
    row.expected_name_contains = expected_name_contains
    if selectors is not None:
        row.selectors = selectors
    row.updated_at = utcnow()
    session.add(row)
    return row, created


def sync_websites(session: Session, cfg: WebsitesFile, overrides_file: Path | None = None) -> dict:
    from app.config.settings import get_settings
    from app.services.selector_repair import apply_overrides

    overrides_file = overrides_file or get_settings().selector_overrides_file
    s = {"websites_created": 0, "websites_updated": 0, "products_created": 0, "products_updated": 0}
    for w in cfg.websites:
        site, created = upsert_website(session, w)
        s["websites_created" if created else "websites_updated"] += 1
        site_sel, prod_sel = apply_overrides(
            w.name, site.config.get("selectors") or {},
            {p.url: p.selectors.model_dump(exclude_none=True) if p.selectors else {} for p in w.products},
            overrides_file)
        if site_sel:
            site.config = {**site.config, "selectors": site_sel}
            session.add(site)
        for p in w.products:
            _, pc = upsert_product(
                session, site, name=p.name, url=p.url, sku=p.sku, currency=p.currency or w.currency,
                threshold=p.price_change_threshold, active=p.active,
                expected_name_contains=p.expected_name_contains,
                selectors=prod_sel.get(p.url, {}),
            )
            s["products_created" if pc else "products_updated"] += 1
    session.commit()
    return s


def sync_workflows(session: Session, cfg: WorkflowsFile) -> dict:
    from app.scheduler.schedule import next_run_after

    s = {"workflows_created": 0, "workflows_updated": 0}
    for wf in cfg.workflows:
        row = session.exec(select(Workflow).where(Workflow.name == wf.name)).first()
        created = row is None
        if row is None:
            row = Workflow(name=wf.name, source="config")
        row.description, row.instruction, row.enabled = wf.description, wf.instruction, wf.enabled
        new_def = wf.definition.model_dump(mode="json")
        if created or row.definition.get("schedule") != new_def["schedule"]:
            row.next_run_at = next_run_after(wf.definition.schedule, utcnow())
        row.definition = new_def
        row.updated_at = utcnow()
        session.add(row)
        s["workflows_created" if created else "workflows_updated"] += 1
    session.commit()
    return s
