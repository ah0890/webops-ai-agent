"""Bulk onboarding of the user's real product list from CSV / Excel / YAML / JSON.

Every input row is accounted for in the summary — rows are never silently dropped.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml
from sqlmodel import Session, select

from app.database.models import Product, Website
from app.security.url_guard import host_allowed
from app.services.config_sync import upsert_product

COLUMNS = ["supplier", "product_name", "sku", "product_url", "currency", "price_change_threshold", "active"]
_ALIASES = {"website": "supplier", "name": "product_name", "url": "product_url", "threshold": "price_change_threshold"}
_TRUE = {"1", "true", "yes", "y", "active", ""}
_FALSE = {"0", "false", "no", "n", "inactive"}


@dataclass
class ImportSummary:
    total_rows: int = 0
    valid: int = 0
    imported: int = 0
    updated: int = 0
    duplicates: int = 0
    invalid: int = 0
    websites_created: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def normalise_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))


def read_rows(filename: str, content: bytes) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    if suffix in (".csv", ".txt"):
        text = content.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h or "").strip() for h in rows[0]]
        return [{header[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(header)}
                for r in rows[1:] if any(v not in (None, "") for v in r)]
    if suffix in (".yaml", ".yml", ".json"):
        data = json.loads(content) if suffix == ".json" else yaml.safe_load(content)
        if isinstance(data, dict):
            data = data.get("products", [])
        if not isinstance(data, list):
            raise ValueError("expected a list of product rows (or {products: [...]})")
        return [dict(r) for r in data]
    raise ValueError(f"unsupported file type {suffix!r}; use CSV, XLSX, YAML or JSON")


def _canon(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        key = str(k or "").strip().lower().replace(" ", "_")
        out[_ALIASES.get(key, key)] = "" if v is None else str(v).strip()
    return out


def import_products(session: Session, filename: str, content: bytes, supported_currencies: set[str],
                    dry_run: bool = False) -> ImportSummary:
    summary = ImportSummary(dry_run=dry_run)
    rows = read_rows(filename, content)
    summary.total_rows = len(rows)
    seen: dict[str, int] = {}
    sites = {w.name: w for w in session.exec(select(Website)).all()}

    for idx, raw in enumerate(rows, start=2):  # row 1 is the header
        r = _canon(raw)
        errs: list[str] = []
        supplier, name, url = r.get("supplier", ""), r.get("product_name", ""), r.get("product_url", "")
        if not supplier:
            errs.append("missing supplier")
        if not name:
            errs.append("missing product_name")
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            errs.append(f"invalid product_url {url!r}")
        currency = r.get("currency", "").upper() or None
        if currency and currency not in supported_currencies:
            errs.append(f"unsupported currency {currency!r}")
        threshold = None
        if r.get("price_change_threshold"):
            try:
                threshold = float(r["price_change_threshold"].rstrip("%"))
                if not 0 < threshold <= 100:
                    raise ValueError
            except ValueError:
                errs.append(f"invalid price_change_threshold {r['price_change_threshold']!r} (0 < x ≤ 100)")
        active_s = r.get("active", "").lower()
        if active_s not in _TRUE | _FALSE:
            errs.append(f"invalid active value {r['active']!r}")
        site = sites.get(supplier)
        if site and not errs and not host_allowed(p.hostname or "", site.allowed_domains):
            errs.append(f"URL host {p.hostname} is outside allowed_domains {site.allowed_domains} of {supplier!r}")

        if errs:
            summary.invalid += 1
            summary.errors.append({"row": idx, "product_url": url, "errors": errs})
            continue
        key = normalise_url(url)
        if key in seen:
            summary.duplicates += 1
            summary.errors.append({"row": idx, "product_url": url,
                                   "errors": [f"duplicate of row {seen[key]}"]})
            continue
        seen[key] = idx
        summary.valid += 1
        if dry_run:
            continue
        if site is None:
            host = p.hostname or ""
            site = Website(name=supplier, base_url=f"{p.scheme}://{p.netloc}",
                           allowed_domains=[host.removeprefix("www.")], currency=currency, source="import")
            session.add(site)
            session.flush()
            sites[supplier] = site
            summary.websites_created.append(supplier)
        existing = session.exec(select(Product).where(Product.url == url)).first()
        upsert_product(session, site, name=name, url=url, sku=r.get("sku") or None,
                       currency=currency or site.currency, threshold=threshold,
                       active=active_s not in _FALSE,
                       selectors=existing.selectors if existing else {}, source="import")
        if existing:
            summary.updated += 1
        else:
            summary.imported += 1
    if not dry_run:
        session.commit()
    return summary
