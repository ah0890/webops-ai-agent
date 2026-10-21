"""Small helpers turning ORM rows into JSON-friendly dicts."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import SQLModel


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat(timespec="seconds")


def row(obj: SQLModel, **extra) -> dict:
    d = obj.model_dump()
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = iso(v)
    d.update(extra)
    return d
