from datetime import UTC, datetime, timedelta

from app.agent.llm import LLMProvider, LLMUnavailable
from app.agent.planner import PlannedWorkflow, plan_workflow, rule_based_parse
from app.config.schema import ScheduleSpec
from app.database.models import Workflow, utcnow
from app.scheduler.schedule import next_run_after
from app.scheduler.scheduler import LocalScheduler

SITES = ["Alpha Parts", "Beta Supply"]


def test_rule_based_linkedin_instruction():
    p = rule_based_parse("Monitor these supplier websites every morning and alert me when prices change by more than 5%.", SITES)
    assert p.price_change_threshold == 5 and p.schedule_kind == "daily" and p.at == "08:00"
    assert p.websites == ["*"] and p.detect_increases and p.detect_decreases


def test_rule_based_specifics():
    p = rule_based_parse("Check Beta Supply every 6 hours for price increases above 3.5 percent", SITES)
    assert (p.schedule_kind, p.every_hours, p.price_change_threshold) == ("interval", 6.0, 3.5)
    assert p.websites == ["Beta Supply"] and p.detect_increases and not p.detect_decreases
    w = rule_based_parse("every friday at 7:30pm check prices", SITES)
    assert (w.schedule_kind, w.weekday, w.at) == ("weekly", "fri", "19:30")


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, out=None, fail=False):
        self.out, self.fail = out, fail

    def structured(self, system, prompt, schema, max_tokens=8000):
        if self.fail:
            raise LLMUnavailable("down")
        return self.out


def test_llm_cannot_introduce_unknown_websites():
    out = PlannedWorkflow(name="x", description="d", websites=["Alpha Parts", "evil.example.com"], rationale="r",
                          schedule_kind="daily", at="09:00", price_change_threshold=5)
    plan = plan_workflow("watch prices", SITES, 3, 5.0, FakeProvider(out))
    assert plan.planner == "fake" and plan.definition.websites == ["Alpha Parts"]
    assert any("evil.example.com" in w for w in plan.warnings)


def test_llm_bad_schedule_falls_back_to_manual():
    out = PlannedWorkflow(name="x", description="d", websites=["*"], rationale="r", schedule_kind="daily", at="25:99")
    plan = plan_workflow("watch", SITES, 3, 5.0, FakeProvider(out))
    assert plan.definition.schedule.kind == "manual" and plan.warnings


def test_llm_failure_uses_rules():
    plan = plan_workflow("daily 10% alerts", SITES, 3, 5.0, FakeProvider(fail=True))
    assert plan.planner == "rule-based" and plan.definition.price_change_threshold == 10


def test_next_run_after():
    t = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    assert next_run_after(ScheduleSpec(), t) is None
    assert next_run_after(ScheduleSpec(kind="interval", every_hours=6), t) == t + timedelta(hours=6)
    d = next_run_after(ScheduleSpec(kind="daily", at="08:00"), t)
    assert t < d <= t + timedelta(days=1)
    assert d.astimezone().strftime("%H:%M") == "08:00"
    w = next_run_after(ScheduleSpec(kind="weekly", weekday="mon", at="08:00"), t)
    assert t < w <= t + timedelta(days=7) and w.astimezone().weekday() == 0


def test_scheduler_triggers_due_workflow(session):
    wf = Workflow(name="due", definition={"schedule": {"kind": "interval", "every_hours": 6}},
                  next_run_at=utcnow() - timedelta(minutes=1))
    off = Workflow(name="disabled", enabled=False, definition={"schedule": {"kind": "interval", "every_hours": 1}},
                   next_run_at=utcnow() - timedelta(minutes=1))
    session.add_all([wf, off])
    session.commit()
    fired = []
    sched = LocalScheduler(lambda wid, trig: fired.append((wid, trig)))
    assert sched.tick() == [wf.id]
    assert fired == [(wf.id, "schedule")]
    session.refresh(wf)
    assert wf.next_run_at > utcnow() + timedelta(hours=5)
    assert sched.tick() == []  # not due again
