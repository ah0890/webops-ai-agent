"""Workflow endpoints: create (from natural language or structured), list, inspect, run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, col, select

from app.agent.llm import get_provider
from app.agent.planner import WorkflowPlan, plan_workflow
from app.api.deps import require_api_key, run_service
from app.api.serializers import row
from app.config.schema import ScheduleSpec, WorkflowDefinition
from app.config.settings import get_settings
from app.database.models import Product, Website, Workflow, WorkflowRun, utcnow
from app.database.session import get_session
from app.scheduler.schedule import next_run_after
from app.workflows.run_service import RunConflict, RunService

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    instruction: str | None = Field(None, description="Natural-language request; the AI planner builds the definition")
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    definition: WorkflowDefinition | None = Field(None, description="Structured definition (skips the planner)")
    enabled: bool = True


class WorkflowPatch(BaseModel):
    enabled: bool | None = None
    schedule: ScheduleSpec | None = None
    price_change_threshold: float | None = Field(None, gt=0, le=100)


class PlanRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=4000)


def _plan(session: Session, instruction: str) -> WorkflowPlan:
    settings = get_settings()
    sites = [w.name for w in session.exec(select(Website).where(Website.active == True)).all()]  # noqa: E712
    n = len(session.exec(select(Product.id).where(Product.active == True)).all())  # noqa: E712
    return plan_workflow(instruction, sites, n, settings.price_change_threshold, get_provider(settings),
                         settings.llm_extraction_fallback and settings.llm_available)


def _wf_dict(session: Session, wf: Workflow) -> dict:
    last = session.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == wf.id)
                        .order_by(col(WorkflowRun.id).desc())).first()
    sched = ScheduleSpec.model_validate(wf.definition.get("schedule") or {})
    return row(wf, schedule_text=sched.describe(),
               last_run=row(last) if last else None)


@router.post("/plan")
def preview_plan(body: PlanRequest, session: Session = Depends(get_session)) -> dict:
    """Preview what the AI planner would create, without saving."""
    return _plan(session, body.instruction).model_dump(mode="json")


@router.post("", dependencies=[Depends(require_api_key)], status_code=201)
def create_workflow(body: WorkflowCreate, session: Session = Depends(get_session)) -> dict:
    plan_meta: dict = {}
    if body.definition is not None:
        defn = body.definition
        name = body.name or "Price monitor"
        description = body.description
    elif body.instruction:
        plan = _plan(session, body.instruction)
        defn, name = plan.definition, body.name or plan.name
        description = body.description or plan.description
        plan_meta = plan.model_dump(mode="json", exclude={"definition"})
    else:
        raise HTTPException(422, "provide either `instruction` or `definition`")
    base, i = name, 2
    while session.exec(select(Workflow).where(Workflow.name == name)).first():
        name, i = f"{base} ({i})", i + 1
    wf = Workflow(name=name, description=description, instruction=body.instruction,
                  definition=defn.model_dump(mode="json"), plan=plan_meta, enabled=body.enabled,
                  source="planner" if plan_meta else "api",
                  next_run_at=next_run_after(defn.schedule, utcnow()) if body.enabled else None)
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return _wf_dict(session, wf)


@router.get("")
def list_workflows(session: Session = Depends(get_session)) -> list[dict]:
    return [_wf_dict(session, wf) for wf in session.exec(select(Workflow).order_by(Workflow.id)).all()]


@router.get("/{workflow_id}")
def get_workflow(workflow_id: int, session: Session = Depends(get_session)) -> dict:
    wf = session.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    runs = session.exec(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
                        .order_by(col(WorkflowRun.id).desc()).limit(20)).all()
    return _wf_dict(session, wf) | {"runs": [row(r) for r in runs]}


@router.patch("/{workflow_id}", dependencies=[Depends(require_api_key)])
def update_workflow(workflow_id: int, body: WorkflowPatch, session: Session = Depends(get_session)) -> dict:
    wf = session.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    d = dict(wf.definition)
    if body.schedule is not None:
        d["schedule"] = body.schedule.model_dump(mode="json")
    if body.price_change_threshold is not None:
        d["price_change_threshold"] = body.price_change_threshold
    try:
        defn = WorkflowDefinition.model_validate(d)
    except ValidationError as e:
        raise HTTPException(422, str(e)) from e
    wf.definition = defn.model_dump(mode="json")
    if body.enabled is not None:
        wf.enabled = body.enabled
    wf.next_run_at = next_run_after(defn.schedule, utcnow()) if wf.enabled else None
    wf.updated_at = utcnow()
    session.add(wf)
    session.commit()
    session.refresh(wf)
    return _wf_dict(session, wf)


@router.post("/{workflow_id}/run", dependencies=[Depends(require_api_key)], status_code=202)
def run_workflow(workflow_id: int, svc: RunService = Depends(run_service)) -> dict:
    try:
        run = svc.start_run(workflow_id, "manual")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except RunConflict as e:
        raise HTTPException(409, str(e)) from e
    return row(run)
