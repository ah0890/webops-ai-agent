"""Local scheduler: a single daemon thread that triggers due workflows.

`next_run_at` lives in the database, so schedules survive restarts. The `Trigger`
callable is the only coupling to execution, which keeps this swappable for
APScheduler / Celery beat / a cloud scheduler later.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlmodel import col, select

from app.config.schema import ScheduleSpec
from app.database.models import Workflow, utcnow
from app.database.session import session_scope
from app.scheduler.schedule import next_run_after

log = logging.getLogger(__name__)
Trigger = Callable[[int, str], object]  # (workflow_id, trigger) -> run


class LocalScheduler:
    def __init__(self, trigger: Trigger, poll_seconds: int = 30):
        self.trigger = trigger
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="webops-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (poll every %ss)", self.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception:  # never let the scheduler thread die
                log.exception("scheduler tick failed")

    def tick(self) -> list[int]:
        now = utcnow()
        fired: list[int] = []
        with session_scope() as s:
            due = s.exec(select(Workflow).where(
                Workflow.enabled == True,  # noqa: E712
                col(Workflow.next_run_at).is_not(None),
                col(Workflow.next_run_at) <= now,
            )).all()
            for wf in due:
                spec = ScheduleSpec.model_validate(wf.definition.get("schedule", {}))
                wf.next_run_at = next_run_after(spec, now)
                s.add(wf)
                s.commit()
                try:
                    self.trigger(wf.id, "schedule")  # type: ignore[arg-type]
                    fired.append(wf.id)  # type: ignore[arg-type]
                    log.info("scheduled run triggered for workflow %s; next at %s UTC", wf.name, wf.next_run_at)
                except Exception as e:
                    log.warning("could not trigger workflow %s: %s", wf.name, e)
        return fired
