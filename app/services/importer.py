"""Bulk onboarding of the user's real product list from CSV / Excel / YAML / JSON.

Every input row is accounted for in the summary — rows are never silently dropped.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml
from sqlmodel import Session, select

from app.database.models import Product, Website
from app.security.url_guard import host_allowed
from app.services.config_sync import upsert_product

COLUMNS = ["supplier", "product_name", "sku", "product_url", "currency", "price_change_threshold", "active"]
REQUIRED = ("supplier", "product_name", "product_url")
# Accepted header names per column, in priority order (normalised: lower-case, spaces/dashes -> "_").
ALIASES: dict[str, tuple[str, ...]] = {
    "supplier": ("supplier", "supplier_name", "vendor", "vendor_name", "brand", "store", "store_name", "seller",
                 "merchant", "retailer", "shop", "website"),
    "product_name": ("product_name", "name", "title", "product_title", "product", "item_name", "item"),
    "sku": ("sku", "first_sku", "variant_sku", "item_code", "product_code", "article_number", "mpn", "code"),
    "product_url": ("product_url", "url", "product_link", "link", "page_url", "product_page"),
    "currency": ("currency", "currency_code", "ccy"),
    "price_change_threshold": ("price_change_threshold", "threshold", "alert_threshold", "threshold_pct"),
    "active": ("active", "enabled", "monitor"),
}
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
    warnings: list[dict] = field(default_factory=list)
    source: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def normalise_url(url: str) -> str:
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))


def _norm(h: object) -> str:
    return re.sub(r"[\s\-]+", "_", str(h or "").strip().lower())


def _column_map(headers: list) -> dict[str, int]:
    """Map canonical columns to header positions using ALIASES."""
    norm = [_norm(h) for h in headers]
    out: dict[str, int] = {}
    for canon, names in ALIASES.items():
        for n in names:
            if n in norm:
                out[canon] = norm.index(n)
                break
    return out


def _table_rows(table: list[list], label: str) -> tuple[list[tuple[int, dict]], str] | None:
    """Find the header row (within the first 15 rows) and return [(row_number, canonical_row)]."""
    for hi, header in enumerate(table[:15]):
        cmap = _column_map(list(header))
        if all(c in cmap for c in REQUIRED):
            rows = []
            for ri, r in enumerate(table[hi + 1:], start=hi + 2):
                if not any(v not in (None, "") for v in r):
                    continue
                rows.append((ri, {c: ("" if i >= len(r) or r[i] is None else str(r[i]).strip()) for c, i in cmap.items()}))
            used = ", ".join(f"{c} <- '{header[i]}'" for c, i in cmap.items())
            return rows, f"{label}, header row {hi + 1} ({used})"
    return None


def read_rows(filename: str, content: bytes) -> tuple[list[tuple[int, dict]], str]:
    """Return ([(row_number, {canonical column: value})], description of where the data came from)."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".csv", ".txt"):
        text = content.decode("utf-8-sig")
        table = list(csv.reader(io.StringIO(text)))
        found = _table_rows(table, "CSV")
        if found:
            return found
        raise ValueError(_no_columns_message([("CSV", table[0] if table else [])]))
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        candidates, seen_headers = [], []
        for ws in wb.worksheets:
            table = [list(r) for r in ws.iter_rows(values_only=True)]
            found = _table_rows(table, f"sheet '{ws.title}'")
            if found:
                candidates.append(found)
            elif table:
                seen_headers.append((f"sheet '{ws.title}'", table[0]))
        if candidates:
            return max(candidates, key=lambda c: len(c[0]))  # the sheet with the most product rows
        raise ValueError(_no_columns_message(seen_headers))
    if suffix in (".yaml", ".yml", ".json"):
        data = json.loads(content) if suffix == ".json" else yaml.safe_load(content)
        if isinstance(data, dict):
            data = data.get("products", [])
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValueError("expected a list of product rows (or {products: [...]})")
        keys = list(dict.fromkeys(k for r in data for k in r))
        table = [keys] + [[r.get(k) for k in keys] for r in data]
        found = _table_rows(table, suffix.lstrip(".").upper())
        if found:
            return found
        raise ValueError(_no_columns_message([(suffix.lstrip(".").upper(), keys)]))
    raise ValueError(f"unsupported file type {suffix!r}; use CSV, XLSX, YAML or JSON")


def _no_columns_message(seen: list[tuple[str, list]]) -> str:
    parts = "; ".join(f"{label}: {[h for h in hdr if h not in (None, '')][:12]}" for label, hdr in seen) or "file is empty"
    return ("Could not find the required columns supplier, product_name and product_url (accepted names include "
            "supplier_name/vendor/brand, title/name, url/link). Found headers -> " + parts)


def import_products(session: Session, filename: str, content: bytes, supported_currencies: set[str],
                    dry_run: bool = False) -> ImportSummary:
    summary = ImportSummary(dry_run=dry_run)
    rows, summary.source = read_rows(filename, content)
    summary.total_rows = len(rows)
    seen: dict[str, int] = {}
    sites = {w.name: w for w in session.exec(select(Website)).all()}

    for idx, r in rows:
        errs: list[str] = []
        supplier, name, url = r.get("supplier", ""), r.get("product_name", ""), r.get("product_url", "")
        if not supplier:
            errs.append("missing supplier")
        if not name:
            errs.append("missing product_name")
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            errs.append(f"invalid product_url {url!r}")
        currency = r.get("currency", "").strip().upper() or None
        if currency and not re.fullmatch(r"[A-Z]{3}", currency):
            summary.warnings.append({"row": idx, "product_url": url, "warning":
                                     f"currency {r['currency']!r} is not an ISO code; using the supplier's currency if known, otherwise the one shown on the page"})
            currency = None
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
