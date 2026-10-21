"""Structured (key=value / JSON) logging with secret masking on every handler."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from app.security.secrets import SecretMaskingFilter


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("run_id", "website", "product", "stage"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_webops_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    handler.addFilter(SecretMaskingFilter())
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "anthropic", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._webops_configured = True  # type: ignore[attr-defined]
