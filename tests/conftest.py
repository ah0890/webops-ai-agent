"""Test fixtures. Everything here is synthetic test data, isolated in a temporary database —
it never touches the real observation database or any external website."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app.config import settings as settings_mod
from app.database import session as db_session
from app.database.models import Observation, Product, Website, Workflow, WorkflowRun


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("SCREENSHOTS_DIR", str(tmp_path / "shots"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("WEBSITES_CONFIG", str(tmp_path / "websites.yaml"))
    monkeypatch.setenv("WORKFLOWS_CONFIG", str(tmp_path / "workflows.yaml"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    settings_mod.get_settings.cache_clear()
    s = settings_mod.get_settings()
    s.ensure_dirs()
    yield s
    settings_mod.get_settings.cache_clear()


@pytest.fixture()
def engine(settings):
    eng = db_session.make_engine(settings.database_url)
    db_session.set_engine(eng)
    db_session.init_db(eng)
    yield eng
    db_session.set_engine(None)  # type: ignore[arg-type]
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def seeded(session):
    """One site, two products, one workflow and two completed runs of test observations."""
    site = Website(name="Test Supplier", base_url="https://shop.test", allowed_domains=["shop.test"], currency="USD")
    session.add(site)
    session.flush()
    p1 = Product(website_id=site.id, name="Widget A", sku="WA-1", url="https://shop.test/a", currency="USD")
    p2 = Product(website_id=site.id, name="Widget B", sku="WB-1", url="https://shop.test/b", currency="USD")
    session.add_all([p1, p2])
    wf = Workflow(name="Test monitor", definition={"websites": ["*"], "price_change_threshold": 5,
                                                   "schedule": {"kind": "manual"}})
    session.add(wf)
    session.flush()
    t0 = datetime.now(UTC) - timedelta(days=10)
    r1 = WorkflowRun(workflow_id=wf.id, status="completed", started_at=t0, finished_at=t0 + timedelta(minutes=1))
    r2 = WorkflowRun(workflow_id=wf.id, status="completed_with_errors", started_at=t0 + timedelta(days=10),
                     finished_at=t0 + timedelta(days=10, minutes=1))
    session.add_all([r1, r2])
    session.flush()

    def obs(run, prod, at, price, status="VERIFIED", availability="in_stock", issues=None):
        return Observation(run_id=run.id, product_id=prod.id, website_id=site.id, url=prod.url, observed_at=at,
                           price=price, currency="USD" if price is not None else None, availability=availability,
                           extraction_status=status, extraction_method="selector", issues=issues or [])

    session.add_all([
        obs(r1, p1, t0, 100.0), obs(r1, p2, t0, 50.0),
        obs(r2, p1, t0 + timedelta(days=10), 110.0),
        obs(r2, p2, t0 + timedelta(days=10), None, status="FAILED", availability="unknown",
            issues=[{"code": "missing_price", "severity": "fatal", "message": "price not found"}]),
    ])
    session.commit()
    return {"site": site, "p1": p1, "p2": p2, "wf": wf, "r1": r1, "r2": r2}
