"""Opt-in end-to-end test against a real public website (the books.toscrape.com scraping sandbox).

    WEBOPS_RUN_INTEGRATION=1 pytest tests/integration -q

Not part of the default suite: it depends on an external site being reachable.
"""

import os
from pathlib import Path

import pytest
from sqlmodel import select

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("WEBOPS_RUN_INTEGRATION") != "1", reason="set WEBOPS_RUN_INTEGRATION=1")]
HERE = Path(__file__).parent


def test_real_run_end_to_end(session, settings):
    from app.config.loader import load_websites, load_workflows
    from app.database.models import Observation, Screenshot, Workflow
    from app.services.config_sync import sync_websites, sync_workflows
    from app.workflows.run_service import RunService

    sync_websites(session, load_websites(HERE / "toscrape.websites.yaml"))
    sync_workflows(session, load_workflows(HERE / "toscrape.workflows.yaml"))
    wf = session.exec(select(Workflow)).one()
    svc = RunService(settings)

    run1 = svc.execute(svc.create_run(wf.id, "test").id)
    assert run1.products_total == 5 and run1.products_succeeded == 4 and run1.products_failed == 1
    obs = session.exec(select(Observation).where(Observation.run_id == run1.id)).all()
    good = [o for o in obs if o.extraction_status == "VERIFIED"]
    assert len(good) == 4 and all(o.currency == "GBP" and o.price > 0 for o in good)
    assert session.exec(select(Screenshot).where(Screenshot.run_id == run1.id)).all()

    run2 = svc.execute(svc.create_run(wf.id, "test").id)
    assert run2.products_succeeded == 4
    assert len(list(settings.reports_dir.glob("*.xlsx"))) == 2
