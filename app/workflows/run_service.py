"""Creates runs and executes them in background threads (one asyncio loop per run).

Keeping execution behind `start_run` makes it straightforward to move to a task queue later.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from sqlmodel import col, select

from app.agent.llm import get_provider
from app.config.settings import Settings, get_settings
from app.database.models import RunStatus, Workflow, WorkflowRun, utcnow
from app.database.session import session_scope
from app.workflows.price_monitor import PriceMonitorRunner

log = logging.getLogger(__name__)


class RunConflict(Exception):
    pass


class RunService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}

    def recover_stale_runs(self) -> int:
        """Runs left 'running' by a crashed/stopped process are marked failed on startup."""
        with session_scope() as s:
            stale = s.exec(select(WorkflowRun).where(
                col(WorkflowRun.status).in_([RunStatus.RUNNING.value, RunStatus.QUEUED.value]))).all()
            for r in stale:
                r.status, r.error, r.finished_at = RunStatus.FAILED.value, "interrupted (process stopped)", utcnow()
                s.add(r)
            s.commit()
            return len(stale)

    def _active_run(self, workflow_id: int) -> WorkflowRun | None:
        with session_scope() as s:
            return s.exec(select(WorkflowRun).where(
                WorkflowRun.workflow_id == workflow_id,
                col(WorkflowRun.status).in_([RunStatus.RUNNING.value, RunStatus.QUEUED.value]))).first()

    def create_run(self, workflow_id: int, trigger: str) -> WorkflowRun:
        with self._lock:
            with session_scope() as s:
                wf = s.get(Workflow, workflow_id)
                if wf is None:
                    raise LookupError(f"workflow {workflow_id} not found")
            if active := self._active_run(workflow_id):
                raise RunConflict(f"workflow {workflow_id} already has run {active.id} in progress")
            with session_scope() as s:
                run = WorkflowRun(workflow_id=workflow_id, trigger=trigger)
                s.add(run)
                s.commit()
                s.refresh(run)
                return run

    def execute(self, run_id: int) -> WorkflowRun:
        """Blocking execution (CLI / thread target)."""
        runner = PriceMonitorRunner(self.settings, get_provider(self.settings))
        return asyncio.run(runner.run(run_id))

    def start_run(self, workflow_id: int, trigger: str = "manual") -> WorkflowRun:
        run = self.create_run(workflow_id, trigger)

        def target() -> None:
            try:
                self.execute(run.id)  # type: ignore[arg-type]
            except Exception:
                log.exception("run %s failed", run.id)
                with session_scope() as s:
                    r = s.get(WorkflowRun, run.id)
                    if r and r.status in (RunStatus.RUNNING.value, RunStatus.QUEUED.value):
                        r.status, r.error, r.finished_at = RunStatus.FAILED.value, "unexpected error; see logs", utcnow()
                        s.add(r)
                        s.commit()
            finally:
                self._threads.pop(run.id, None)  # type: ignore[arg-type]

        t = threading.Thread(target=target, name=f"run-{run.id}", daemon=True)
        self._threads[run.id] = t  # type: ignore[index]
        t.start()
        return run
