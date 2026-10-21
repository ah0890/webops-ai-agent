from datetime import UTC, datetime, timedelta

import pytest

from app.models.extraction import Issue, ValidationOutcome
from app.services.alerts import AlertRules, evaluate
from app.services.intelligence import PricePoint, compute_stats, pct_change
from app.services.validation import PreviousObservation

NOW = datetime(2026, 6, 30, 12, tzinfo=UTC)


def pts(*items):
    return [PricePoint(NOW - timedelta(days=d), p, "USD", a) for d, p, a in items]


def test_pct_change_formula():
    assert pct_change(105, 100) == 5.0
    assert pct_change(95, 100) == -5.0
    assert pct_change(10, 0) is None and pct_change(None, 10) is None


def test_stats():
    s = compute_stats(pts((40, 100, "in_stock"), (8, 110, "in_stock"), (3, 90, "out_of_stock"), (0, 99, "in_stock")), NOW)
    assert s.current_price == 99 and s.previous_price == 90
    assert s.absolute_change == 9 and s.percentage_change == pytest.approx(10.0)
    assert s.lowest_price == 90 and s.highest_price == 110 and s.average_price == pytest.approx(99.75)
    assert s.change_7d_pct == pytest.approx(-10.0)   # vs 110 (8 days ago)
    assert s.change_30d_pct == pytest.approx(-1.0)   # vs 100 (40 days ago)
    assert s.availability_changes == 2


def test_windowed_change_needs_enough_history():
    s = compute_stats(pts((2, 100, "in_stock"), (0, 101, "in_stock")), NOW)
    assert s.change_7d_pct is None and s.change_30d_pct is None
    assert s.trend == "insufficient_data"


def test_trend():
    assert compute_stats(pts(*[(10 - i, 100 + i * 2, "in_stock") for i in range(6)]), NOW).trend == "up"
    assert compute_stats(pts(*[(10 - i, 100, "in_stock") for i in range(6)]), NOW).trend == "flat"


def outcome(price, status="VERIFIED", availability="in_stock", issues=()):
    return ValidationOutcome(status=status, price=price, currency="USD", availability=availability, issues=list(issues))


RULES = AlertRules(threshold_pct=5)


def test_increase_and_decrease_alerts():
    up = evaluate(outcome(106), PreviousObservation(100, "USD", "in_stock"), RULES, "W")
    assert [a.alert_type for a in up] == ["price_increase"] and up[0].percentage_change == 6.0
    assert up[0].severity == "medium"
    down = evaluate(outcome(85), PreviousObservation(100, "USD", "in_stock"), RULES, "W")
    assert down[0].alert_type == "price_decrease" and down[0].severity == "high"


def test_below_threshold_no_alert():
    assert evaluate(outcome(104.9), PreviousObservation(100, "USD", "in_stock"), RULES, "W") == []


def test_direction_filters():
    rules = AlertRules(threshold_pct=5, detect_decreases=False)
    assert evaluate(outcome(80), PreviousObservation(100, "USD", "in_stock"), rules, "W") == []


def test_first_observation_is_baseline_only():
    assert evaluate(outcome(100), None, RULES, "W") == []


def test_availability_transitions():
    gone = evaluate(outcome(None, "LIKELY_VALID", "out_of_stock"), PreviousObservation(100, "USD", "in_stock"), RULES, "W")
    assert gone[0].alert_type == "unavailable"
    back = evaluate(outcome(100), PreviousObservation(100, "USD", "out_of_stock"), RULES, "W")
    assert back[0].alert_type == "available"


def test_failed_and_review_outcomes():
    f = evaluate(outcome(None, "FAILED", issues=[Issue(code="x", severity="fatal", message="boom")]), None, RULES, "W")
    assert f[0].alert_type == "extraction_failure"
    r = evaluate(outcome(500, "NEEDS_REVIEW", issues=[Issue(code="implausible_change", severity="review", message="big")]),
                 PreviousObservation(100, "USD", "in_stock"), RULES, "W")
    assert r[0].alert_type == "suspicious_change" and r[0].severity == "high"


def test_no_price_alert_across_currencies():
    o = ValidationOutcome(status="VERIFIED", price=200, currency="EUR", availability="in_stock")
    assert evaluate(o, PreviousObservation(100, "USD", "in_stock"), RULES, "W") == []
