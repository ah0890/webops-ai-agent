"""Products, websites, dashboard metrics, alerts, screenshots, import and config sync."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, col, func, select

from app.api.deps import require_api_key
from app.api.serializers import iso, row
from app.config.loader import ConfigError, load_websites, load_workflows
from app.config.settings import get_settings
from app.database.models import (
    TRUSTED_STATUSES,
    Alert,
    ExtractionStatus,
    Observation,
    Product,
    Screenshot,
    Website,
    Workflow,
    WorkflowRun,
    utcnow,
)
from app.database.session import get_session
from app.services.config_sync import sync_websites, sync_workflows
from app.services.importer import import_products
from app.services.intelligence import PricePoint, compute_stats

router = APIRouter(prefix="/api", tags=["data"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _latest_obs(session: Session) -> dict[int, Observation]:
    sub = select(Observation.product_id, func.max(Observation.id).label("mid")).group_by(Observation.product_id).subquery()
    rows = session.exec(select(Observation).join(sub, Observation.id == sub.c.mid)).all()
    return {o.product_id: o for o in rows}


def _trusted_series(session: Session, product_ids: list[int] | None = None) -> dict[int, list[PricePoint]]:
    q = select(Observation).where(col(Observation.extraction_status).in_(TRUSTED_STATUSES))
    if product_ids is not None:
        q = q.where(col(Observation.product_id).in_(product_ids or [-1]))
    series: dict[int, list[PricePoint]] = defaultdict(list)
    for o in session.exec(q.order_by(Observation.observed_at, Observation.id)).all():
        series[o.product_id].append(PricePoint(o.observed_at, o.price, o.currency, o.availability))
    return series


@router.get("/websites")
def websites(session: Session = Depends(get_session)) -> list[dict]:
    counts = Counter(p.website_id for p in session.exec(select(Product).where(Product.active == True)).all())  # noqa: E712
    out = []
    for w in session.exec(select(Website).order_by(Website.name)).all():
        cfg = w.config or {}
        out.append(row(w, config=None, active_products=counts.get(w.id, 0),
                       has_selectors=bool(cfg.get("selectors")), requires_login=bool(cfg.get("login"))))
    return out


@router.get("/products")
def products(website_id: int | None = None, session: Session = Depends(get_session)) -> list[dict]:
    q = select(Product)
    if website_id:
        q = q.where(Product.website_id == website_id)
    items = session.exec(q.order_by(Product.website_id, Product.name)).all()
    sites = {w.id: w.name for w in session.exec(select(Website)).all()}
    latest = _latest_obs(session)
    series = _trusted_series(session, [p.id for p in items])  # type: ignore[misc]
    out = []
    for p in items:
        lo = latest.get(p.id)  # type: ignore[arg-type]
        out.append(row(p, website=sites.get(p.website_id),
                       stats=compute_stats(series.get(p.id, [])).as_dict(),  # type: ignore[arg-type]
                       latest_status=lo.extraction_status if lo else None,
                       latest_observed_at=iso(lo.observed_at) if lo else None,
                       latest_issues=lo.issues if lo else []))
    return out


@router.get("/products/{product_id}/history")
def product_history(product_id: int, limit: int = 500, session: Session = Depends(get_session)) -> dict:
    p = session.get(Product, product_id)
    if not p:
        raise HTTPException(404, "product not found")
    obs = session.exec(select(Observation).where(Observation.product_id == product_id)
                       .order_by(col(Observation.observed_at).desc()).limit(min(limit, 5000))).all()
    series = _trusted_series(session, [product_id]).get(product_id, [])
    w = session.get(Website, p.website_id)
    return {
        "product": row(p, website=w.name if w else None),
        "stats": compute_stats(series).as_dict(),
        "series": [{"t": iso(pt.observed_at), "price": pt.price, "currency": pt.currency,
                    "availability": pt.availability} for pt in series],
        "observations": [row(o, raw=None) for o in obs],
    }


@router.get("/dashboard/overview")
def overview(session: Session = Depends(get_session)) -> dict:
    active_products = session.exec(select(Product).join(Website, Product.website_id == Website.id)
                                   .where(Product.active == True, Website.active == True)).all()  # noqa: E712
    status_counts = dict(session.exec(select(Observation.extraction_status, func.count())
                                      .group_by(Observation.extraction_status)).all())
    since = utcnow() - timedelta(days=30)
    series = _trusted_series(session)
    changes_30d = increases_30d = decreases_30d = 0
    for pts in series.values():
        priced = [p for p in pts if p.price is not None]
        for a, b in zip(priced, priced[1:], strict=False):
            if b.observed_at >= since and a.price != b.price and (a.currency == b.currency or not a.currency):
                changes_30d += 1
                increases_30d += b.price > a.price
                decreases_30d += b.price < a.price
    alert_counts = dict(session.exec(select(Alert.status, func.count()).group_by(Alert.status)).all())
    last = session.exec(select(WorkflowRun).order_by(col(WorkflowRun.id).desc())).first()
    last_wf = session.get(Workflow, last.workflow_id) if last else None
    recent = session.exec(select(WorkflowRun).order_by(col(WorkflowRun.id).desc()).limit(14)).all()
    return {
        "websites_monitored": len({p.website_id for p in active_products}),
        "products_monitored": len(active_products),
        "checks_total": sum(status_counts.values()),
        "successful_checks": sum(status_counts.get(s, 0) for s in TRUSTED_STATUSES),
        "verified_checks": status_counts.get(ExtractionStatus.VERIFIED.value, 0),
        "needs_review_checks": status_counts.get(ExtractionStatus.NEEDS_REVIEW.value, 0),
        "failed_checks": status_counts.get(ExtractionStatus.FAILED.value, 0),
        "price_changes_30d": changes_30d, "price_increases_30d": increases_30d, "price_decreases_30d": decreases_30d,
        "alerts_open": alert_counts.get("open", 0), "alerts_total": sum(alert_counts.values()),
        "workflows": len(session.exec(select(Workflow.id)).all()),
        "last_execution": row(last, workflow_name=last_wf.name if last_wf else None) if last else None,
        "recent_runs": [{"id": r.id, "status": r.status, "started_at": iso(r.started_at),
                         "succeeded": r.products_succeeded, "needs_review": r.products_needs_review,
                         "failed": r.products_failed, "alerts": r.alerts_count} for r in reversed(recent)],
    }


@router.get("/alerts")
def alerts(status: str | None = None, limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    q = select(Alert)
    if status:
        q = q.where(Alert.status == status)
    items = session.exec(q.order_by(col(Alert.id).desc()).limit(min(limit, 1000))).all()
    products_ = {p.id: p.name for p in session.exec(select(Product)).all()}
    sites = {w.id: w.name for w in session.exec(select(Website)).all()}
    return [row(a, product=products_.get(a.product_id), website=sites.get(a.website_id)) for a in items]


class AlertPatch(BaseModel):
    status: str


@router.patch("/alerts/{alert_id}", dependencies=[Depends(require_api_key)])
def update_alert(alert_id: int, body: AlertPatch, session: Session = Depends(get_session)) -> dict:
    if body.status not in ("open", "acknowledged", "resolved"):
        raise HTTPException(422, "status must be open, acknowledged or resolved")
    a = session.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.status = body.status
    session.add(a)
    session.commit()
    return row(a)


@router.get("/errors")
def errors(limit: int = 200, session: Session = Depends(get_session)) -> list[dict]:
    items = session.exec(select(Observation).where(col(Observation.extraction_status).in_(
        [ExtractionStatus.FAILED.value, ExtractionStatus.NEEDS_REVIEW.value]))
        .order_by(col(Observation.id).desc()).limit(min(limit, 1000))).all()
    products_ = {p.id: p.name for p in session.exec(select(Product)).all()}
    sites = {w.id: w.name for w in session.exec(select(Website)).all()}
    shots = {s.path: s.id for s in session.exec(select(Screenshot).where(
        col(Screenshot.path).in_([o.screenshot_path for o in items if o.screenshot_path] or [""]))).all()}
    return [row(o, raw=None, product=products_.get(o.product_id), website=sites.get(o.website_id),
                screenshot_id=shots.get(o.screenshot_path)) for o in items]


@router.get("/screenshots/{screenshot_id}")
def screenshot(screenshot_id: int, session: Session = Depends(get_session)) -> FileResponse:
    s = session.get(Screenshot, screenshot_id)
    if not s:
        raise HTTPException(404, "screenshot not found")
    root = get_settings().screenshots_dir.resolve()
    path = (root / s.path).resolve()
    if root not in path.parents or not path.exists():  # path traversal / deleted file
        raise HTTPException(404, "screenshot file missing")
    return FileResponse(path, media_type="image/png")


@router.post("/import/products", dependencies=[Depends(require_api_key)])
async def import_products_endpoint(file: UploadFile = File(...), dry_run: bool = False,
                                   session: Session = Depends(get_session)) -> dict:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 10 MB)")
    try:
        summary = import_products(session, file.filename or "upload.csv", content, get_settings().currency_set, dry_run)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return summary.as_dict()


@router.post("/config/sync", dependencies=[Depends(require_api_key)])
def config_sync(session: Session = Depends(get_session)) -> dict:
    s = get_settings()
    try:
        return {**sync_websites(session, load_websites(s.websites_config)),
                **sync_workflows(session, load_workflows(s.workflows_config))}
    except ConfigError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/system")
def system() -> dict:
    s = get_settings()
    return {
        "llm_enabled": s.llm_available, "llm_model": s.llm_model if s.llm_available else None,
        "scheduler_enabled": s.scheduler_enabled, "browser_headless": s.browser_headless,
        "websites_config_present": s.websites_config.exists(), "workflows_config_present": s.workflows_config.exists(),
        "default_threshold": s.price_change_threshold, "api_key_required": s.api_key is not None,
        "respect_robots_txt": s.respect_robots_txt, "max_concurrency": s.max_concurrency,
    }
