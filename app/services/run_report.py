"""Assemble a run's business report from the database (used by both the API and the Excel exporter)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC

from sqlmodel import Session, col, select

from app.database.models import (
    TRUSTED_STATUSES,
    Alert,
    ExtractionStatus,
    Observation,
    Product,
    Website,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.services.intelligence import compute_stats, pct_change, previous_trusted, trusted_points


def _iso(dt) -> str | None:
    """UTC timestamp without offset suffix (columns are labelled UTC)."""
    if not dt:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="seconds")


def build_run_report(session: Session, run_id: int, history_limit: int = 100_000) -> dict:
    run = session.get(WorkflowRun, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    wf = session.get(Workflow, run.workflow_id)
    obs = session.exec(select(Observation).where(Observation.run_id == run_id).order_by(Observation.id)).all()
    products = {p.id: p for p in session.exec(select(Product).where(
        col(Product.id).in_([o.product_id for o in obs] or [-1]))).all()}
    sites = {w.id: w for w in session.exec(select(Website)).all()}
    alerts = session.exec(select(Alert).where(Alert.run_id == run_id)).all()
    alerted = {(a.product_id, a.alert_type) for a in alerts}

    current, changes, availability, errors = [], [], [], []
    for o in obs:
        p, w = products.get(o.product_id), sites.get(o.website_id)
        pname, wname = (p.name if p else str(o.product_id)), (w.name if w else str(o.website_id))
        trusted = o.extraction_status in TRUSTED_STATUSES
        prev = previous_trusted(session, o.product_id, exclude_run_id=run_id)
        stats = compute_stats(trusted_points(session, o.product_id, until_id=o.id)) if trusted else None
        row = {
            "website": wname, "product": pname, "sku": p.sku if p else None, "url": o.url,
            "price": o.price, "currency": o.currency, "availability": o.availability,
            "status": o.extraction_status, "method": o.extraction_method, "observed_at": _iso(o.observed_at),
            "previous_price": prev.price if prev else None,
            "absolute_change": round(o.price - prev.price, 4) if trusted and prev and o.price is not None and prev.price is not None else None,
            "percentage_change": pct_change(o.price, prev.price) if trusted and prev else None,
            "change_7d_pct": stats.change_7d_pct if stats else None,
            "change_30d_pct": stats.change_30d_pct if stats else None,
            "lowest_price": stats.lowest_price if stats else None,
            "highest_price": stats.highest_price if stats else None,
            "average_price": stats.average_price if stats else None,
            "trend": stats.trend if stats else None,
            "observations": stats.observations if stats else None,
            "screenshot": o.screenshot_path, "product_id": o.product_id, "observation_id": o.id,
        }
        current.append(row)
        if trusted and prev and row["percentage_change"] not in (None, 0) and \
                (not prev.currency or prev.currency == o.currency):
            pct = row["percentage_change"]
            changes.append({
                "website": wname, "product": pname, "sku": row["sku"], "previous_price": prev.price,
                "current_price": o.price, "currency": o.currency, "absolute_change": row["absolute_change"],
                "percentage_change": pct, "direction": "increase" if pct > 0 else "decrease",
                "threshold": (p.price_change_threshold if p and p.price_change_threshold else
                              (wf.definition.get("price_change_threshold") if wf else None)),
                "alerted": (o.product_id, "price_increase" if pct > 0 else "price_decrease") in alerted,
                "previous_observed_at": _iso(prev.observed_at), "observed_at": row["observed_at"],
            })
        if trusted and prev and prev.availability != o.availability and "unknown" not in (prev.availability, o.availability):
            availability.append({"website": wname, "product": pname, "url": o.url, "previous": prev.availability,
                                 "current": o.availability, "previous_observed_at": _iso(prev.observed_at),
                                 "observed_at": row["observed_at"]})
        if not trusted:
            errors.append({"website": wname, "product": pname, "url": o.url, "status": o.extraction_status,
                           "issues": "; ".join(f"{i['code']}: {i['message']}" for i in o.issues),
                           "observed_at": row["observed_at"], "screenshot": o.screenshot_path})

    failed_steps = session.exec(select(WorkflowStep).where(
        WorkflowStep.run_id == run_id, WorkflowStep.status == "failed").order_by(WorkflowStep.seq)).all()
    product_ids = list(products)
    hist_rows = session.exec(select(Observation).where(col(Observation.product_id).in_(product_ids or [-1]))
                             .order_by(Observation.product_id, Observation.observed_at).limit(history_limit)).all()
    history = [{
        "website": sites[h.website_id].name if h.website_id in sites else h.website_id,
        "product": products[h.product_id].name if h.product_id in products else h.product_id,
        "observed_at": _iso(h.observed_at), "price": h.price, "currency": h.currency,
        "availability": h.availability, "status": h.extraction_status, "method": h.extraction_method,
        "run_id": h.run_id, "url": h.url,
    } for h in hist_rows]

    status_counts = Counter(o.extraction_status for o in obs)
    sev = Counter(a.severity for a in alerts)
    duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None
    summary = {
        "run_id": run.id, "workflow": wf.name if wf else None, "status": run.status, "trigger": run.trigger,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "duration_seconds": round(duration, 1) if duration is not None else None,
        "websites_checked": len({o.website_id for o in obs}), "products_checked": len(obs),
        "products_total": run.products_total,
        "verified": status_counts[ExtractionStatus.VERIFIED.value],
        "likely_valid": status_counts[ExtractionStatus.LIKELY_VALID.value],
        "needs_review": status_counts[ExtractionStatus.NEEDS_REVIEW.value],
        "failed": status_counts[ExtractionStatus.FAILED.value],
        "price_increases": sum(1 for c in changes if c["direction"] == "increase"),
        "price_decreases": sum(1 for c in changes if c["direction"] == "decrease"),
        "availability_changes": len(availability),
        "alerts": len(alerts), "alerts_high": sev["high"], "alerts_medium": sev["medium"], "alerts_low": sev["low"],
        "error": run.error,
    }
    top_movers = sorted(changes, key=lambda c: abs(c["percentage_change"]), reverse=True)[:10]
    return {
        "summary": summary, "top_movers": top_movers, "current": current, "changes": changes,
        "availability": availability, "errors": errors, "history": history,
        "failed_steps": [{"stage": s.stage, "product_id": s.product_id, "attempt": s.attempt, "message": s.message,
                          "at": _iso(s.created_at)} for s in failed_steps],
        "alerts": [{"type": a.alert_type, "severity": a.severity, "reason": a.reason, "previous": a.previous_value,
                    "current": a.current_value, "pct": a.percentage_change} for a in alerts],
    }
