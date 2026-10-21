"""Run endpoints: status, audit trail, logs, alerts, report JSON and Excel export."""

from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, col, select

from app.api.serializers import row
from app.config.settings import get_settings
from app.database.models import (
    Alert,
    ExecutionLog,
    Observation,
    Product,
    Report,
    Screenshot,
    Website,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.database.session import get_session
from app.services.run_report import build_run_report

router = APIRouter(prefix="/api/runs", tags=["runs"])
PIPELINE = ["open_website", "locate_product", "extract", "validate", "store", "compare", "alert"]


def _run_or_404(session: Session, run_id: int) -> WorkflowRun:
    run = session.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.get("")
def list_runs(limit: int = 30, session: Session = Depends(get_session)) -> list[dict]:
    runs = session.exec(select(WorkflowRun).order_by(col(WorkflowRun.id).desc()).limit(min(limit, 200))).all()
    names = {w.id: w.name for w in session.exec(select(Workflow)).all()}
    return [row(r, workflow_name=names.get(r.workflow_id)) for r in runs]


@router.get("/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = _run_or_404(session, run_id)
    wf = session.get(Workflow, run.workflow_id)
    report = session.exec(select(Report).where(Report.run_id == run_id).order_by(col(Report.id).desc())).first()
    return row(run, workflow_name=wf.name if wf else None, report=row(report) if report else None)


@router.get("/{run_id}/steps")
def run_steps(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _run_or_404(session, run_id)
    return [row(s) for s in session.exec(select(WorkflowStep).where(WorkflowStep.run_id == run_id)
                                         .order_by(WorkflowStep.seq)).all()]


@router.get("/{run_id}/products")
def run_products(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    """Execution monitor: per-product pipeline checklist derived from the audit trail."""
    _run_or_404(session, run_id)
    steps = session.exec(select(WorkflowStep).where(WorkflowStep.run_id == run_id,
                                                    col(WorkflowStep.product_id).is_not(None))
                         .order_by(WorkflowStep.seq)).all()
    obs = {o.product_id: o for o in session.exec(select(Observation).where(Observation.run_id == run_id)).all()}
    shots = session.exec(select(Screenshot).where(Screenshot.run_id == run_id).order_by(Screenshot.id)).all()
    pids = {s.product_id for s in steps} | set(obs)
    products = {p.id: p for p in session.exec(select(Product).where(col(Product.id).in_(pids or {-1}))).all()}
    sites = {w.id: w.name for w in session.exec(select(Website)).all()}
    out: OrderedDict[int, dict] = OrderedDict()
    for s in steps:
        p = products.get(s.product_id)
        entry = out.setdefault(s.product_id, {  # type: ignore[arg-type]
            "product_id": s.product_id, "product": p.name if p else None, "url": p.url if p else None,
            "website": sites.get(s.website_id), "stages": {k: None for k in PIPELINE}, "attempts": 1,
            "messages": {}, "screenshots": [],
        })
        if s.stage in PIPELINE:
            prev = entry["stages"][s.stage]
            # Later attempts overwrite earlier ones; an alert "ok" beats a "skipped".
            if not (s.stage == "alert" and prev == "ok"):
                entry["stages"][s.stage] = s.status
                entry["messages"][s.stage] = s.message
        elif s.stage in ("selector_repair", "recovery"):
            entry["messages"][s.stage] = s.message
        entry["attempts"] = max(entry["attempts"], s.attempt)
    for sh in shots:
        if sh.product_id in out:
            out[sh.product_id]["screenshots"].append({"id": sh.id, "kind": sh.kind})  # type: ignore[index]
    for pid, entry in out.items():
        o = obs.get(pid)
        entry["observation"] = row(o, raw=None) if o else None
    return list(out.values())


@router.get("/{run_id}/logs")
def run_logs(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _run_or_404(session, run_id)
    return [row(x) for x in session.exec(select(ExecutionLog).where(ExecutionLog.run_id == run_id)
                                         .order_by(ExecutionLog.id)).all()]


@router.get("/{run_id}/alerts")
def run_alerts(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _run_or_404(session, run_id)
    return [row(a) for a in session.exec(select(Alert).where(Alert.run_id == run_id).order_by(Alert.id)).all()]


@router.get("/{run_id}/observations")
def run_observations(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _run_or_404(session, run_id)
    return [row(o) for o in session.exec(select(Observation).where(Observation.run_id == run_id)
                                         .order_by(Observation.id)).all()]


@router.get("/{run_id}/screenshots")
def run_screenshots(run_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _run_or_404(session, run_id)
    return [row(s, url=f"/api/screenshots/{s.id}") for s in
            session.exec(select(Screenshot).where(Screenshot.run_id == run_id).order_by(Screenshot.id)).all()]


@router.get("/{run_id}/report")
def run_report(run_id: int, session: Session = Depends(get_session)) -> dict:
    _run_or_404(session, run_id)
    data = build_run_report(session, run_id, history_limit=5000)
    return data


@router.get("/{run_id}/export")
def run_export(run_id: int, regenerate: bool = False, session: Session = Depends(get_session)) -> FileResponse:
    run = _run_or_404(session, run_id)
    if run.status in ("queued", "running"):
        raise HTTPException(409, "run still in progress")
    settings = get_settings()
    report = session.exec(select(Report).where(Report.run_id == run_id).order_by(col(Report.id).desc())).first()
    path = settings.reports_dir / report.path if report else None
    if regenerate or path is None or not path.exists():
        from app.reporting.excel import generate_run_report

        path = generate_run_report(run_id, settings)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
