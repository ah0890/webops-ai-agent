"""SQLModel tables. Observations are append-only: every check of every product is a new row."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class ExtractionStatus(StrEnum):
    VERIFIED = "VERIFIED"
    LIKELY_VALID = "LIKELY_VALID"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


TRUSTED_STATUSES = (ExtractionStatus.VERIFIED.value, ExtractionStatus.LIKELY_VALID.value)


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class Website(SQLModel, table=True):
    __tablename__ = "websites"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str
    allowed_domains: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    currency: str | None = None
    active: bool = True
    config: dict = Field(default_factory=dict, sa_column=Column(JSON), description="Selectors, login, rules")
    source: str = "config"  # config | import | api
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Product(SQLModel, table=True):
    __tablename__ = "products"
    id: int | None = Field(default=None, primary_key=True)
    website_id: int = Field(foreign_key="websites.id", index=True)
    name: str
    sku: str | None = Field(default=None, index=True)
    url: str = Field(unique=True)
    currency: str | None = None
    price_change_threshold: float | None = None
    expected_name_contains: str | None = None
    selectors: dict = Field(default_factory=dict, sa_column=Column(JSON))
    active: bool = True
    source: str = "config"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Workflow(SQLModel, table=True):
    __tablename__ = "workflows"
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    description: str | None = None
    instruction: str | None = None
    definition: dict = Field(default_factory=dict, sa_column=Column(JSON))
    plan: dict = Field(default_factory=dict, sa_column=Column(JSON), description="Planner output + rationale")
    enabled: bool = True
    source: str = "config"  # config | api | planner
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class WorkflowRun(SQLModel, table=True):
    __tablename__ = "workflow_runs"
    id: int | None = Field(default=None, primary_key=True)
    workflow_id: int = Field(foreign_key="workflows.id", index=True)
    status: str = RunStatus.QUEUED.value
    trigger: str = "manual"  # manual | schedule | cli
    started_at: datetime | None = None
    finished_at: datetime | None = None
    products_total: int = 0
    products_succeeded: int = 0
    products_needs_review: int = 0
    products_failed: int = 0
    alerts_count: int = 0
    error: str | None = None
    summary: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class WorkflowStep(SQLModel, table=True):
    """One audited unit of work: a browser action or a pipeline stage for one product."""

    __tablename__ = "workflow_steps"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflow_runs.id", index=True)
    website_id: int | None = Field(default=None, foreign_key="websites.id")
    product_id: int | None = Field(default=None, foreign_key="products.id")
    seq: int = 0
    stage: str  # login | open_website | locate_product | extract | validate | store | compare | alert | recovery
    action: str | None = None  # browser action name, if any
    action_detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "ok"  # ok | failed | skipped | warning
    message: str | None = None
    attempt: int = 1
    duration_ms: int | None = None
    screenshot_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ExecutionLog(SQLModel, table=True):
    __tablename__ = "execution_logs"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflow_runs.id", index=True)
    level: str = "INFO"
    message: str
    context: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Observation(SQLModel, table=True):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("run_id", "product_id", name="uq_observation_run_product"),)
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflow_runs.id", index=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    website_id: int = Field(foreign_key="websites.id", index=True)
    url: str
    final_url: str | None = None
    observed_at: datetime = Field(default_factory=utcnow, index=True)
    price: float | None = None
    price_text: str | None = None
    currency: str | None = None
    availability: str = Availability.UNKNOWN.value
    product_name_seen: str | None = None
    sku_seen: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    extraction_method: str | None = None
    extraction_status: str = ExtractionStatus.FAILED.value
    issues: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    raw: dict = Field(default_factory=dict, sa_column=Column(JSON), description="All candidate values per strategy")
    screenshot_path: str | None = None


class Alert(SQLModel, table=True):
    __tablename__ = "alerts"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int | None = Field(default=None, foreign_key="workflow_runs.id", index=True)
    website_id: int | None = Field(default=None, foreign_key="websites.id")
    product_id: int | None = Field(default=None, foreign_key="products.id")
    alert_type: str  # price_increase | price_decrease | unavailable | available | extraction_failure | ...
    severity: str  # low | medium | high
    reason: str
    previous_value: str | None = None
    current_value: str | None = None
    percentage_change: float | None = None
    status: str = "open"  # open | acknowledged | resolved
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Screenshot(SQLModel, table=True):
    __tablename__ = "screenshots"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflow_runs.id", index=True)
    website_id: int | None = Field(default=None, foreign_key="websites.id")
    product_id: int | None = Field(default=None, foreign_key="products.id")
    kind: str  # before_extraction | after_extraction | error | login | action
    path: str  # relative to screenshots_dir
    created_at: datetime = Field(default_factory=utcnow)


class Report(SQLModel, table=True):
    __tablename__ = "reports"
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="workflow_runs.id", index=True)
    format: str = "xlsx"
    path: str
    summary: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
