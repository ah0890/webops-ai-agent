"""Shared API dependencies."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from app.config.settings import get_settings
from app.workflows.run_service import RunService


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Mutating endpoints require X-API-Key when API_KEY is configured."""
    expected = get_settings().api_key
    if expected is None:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected.get_secret_value()):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


def run_service(request: Request) -> RunService:
    return request.app.state.run_service
