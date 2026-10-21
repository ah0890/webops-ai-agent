"""FastAPI application: REST API, dashboard (static), scheduler startup."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_data, routes_runs, routes_workflows
from app.config.loader import ConfigError, load_websites, load_workflows
from app.config.settings import PROJECT_ROOT, get_settings
from app.database.session import init_db, session_scope
from app.logging_setup import configure_logging
from app.scheduler.scheduler import LocalScheduler
from app.services.config_sync import sync_websites, sync_workflows
from app.workflows.run_service import RunService

log = logging.getLogger(__name__)
FRONTEND = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    try:
        with session_scope() as s:
            sync_websites(s, load_websites(settings.websites_config))
            sync_workflows(s, load_workflows(settings.workflows_config))
    except ConfigError as e:
        log.error("configuration not loaded: %s", e)
    svc = RunService(settings)
    if n := svc.recover_stale_runs():
        log.warning("marked %s interrupted run(s) as failed", n)
    app.state.run_service = svc
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = LocalScheduler(lambda wf_id, trig: svc.start_run(wf_id, trig), settings.scheduler_poll_seconds)
        scheduler.start()
    app.state.scheduler = scheduler
    yield
    if scheduler:
        scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="WebOps AI Agent",
        version="0.1.0",
        description="AI-planned, deterministic browser automation for authorized real-world price & availability "
                    "monitoring. All data originates from real browser executions stored in the database.",
        lifespan=lifespan,
    )
    app.include_router(routes_workflows.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_data.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND / "index.html")

    return app


app = create_app()
