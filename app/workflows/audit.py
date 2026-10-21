"""AuditRecorder: persists every step, log line and screenshot of a run (answering "what exactly happened?")."""

from __future__ import annotations

import logging
from itertools import count

from app.database.models import ExecutionLog, Screenshot, WorkflowStep
from app.database.session import session_scope
from app.security.secrets import mask

log = logging.getLogger("webops.run")


class AuditRecorder:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self._seq = count(1)

    def step(self, stage: str, *, status: str = "ok", message: str | None = None, action: str | None = None,
             detail: dict | None = None, website_id: int | None = None, product_id: int | None = None,
             attempt: int = 1, duration_ms: int | None = None, screenshot_path: str | None = None) -> None:
        msg = mask(message) if message else None
        with session_scope() as s:
            s.add(WorkflowStep(
                run_id=self.run_id, seq=next(self._seq), stage=stage, status=status, message=msg, action=action,
                action_detail=detail or {}, website_id=website_id, product_id=product_id, attempt=attempt,
                duration_ms=duration_ms, screenshot_path=screenshot_path,
            ))
            s.commit()
        level = logging.WARNING if status == "failed" else logging.INFO
        log.log(level, "[run %s] %s %s%s", self.run_id, stage, status, f" — {msg}" if msg else "")

    def log(self, message: str, level: str = "INFO", **context) -> None:
        with session_scope() as s:
            s.add(ExecutionLog(run_id=self.run_id, level=level, message=mask(message), context=context))
            s.commit()
        log.log(getattr(logging, level, logging.INFO), "[run %s] %s", self.run_id, mask(message))

    def screenshot(self, path: str, kind: str, website_id: int | None, product_id: int | None) -> None:
        with session_scope() as s:
            s.add(Screenshot(run_id=self.run_id, website_id=website_id, product_id=product_id, kind=kind, path=path))
            s.commit()
