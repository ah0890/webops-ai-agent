"""Alert rules. Price alerts are only raised between two *trusted* observations in the same currency."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.database.models import Alert, ExtractionStatus
from app.models.extraction import ValidationOutcome
from app.services.intelligence import pct_change
from app.services.validation import PreviousObservation

log = logging.getLogger(__name__)


@dataclass
class AlertDraft:
    alert_type: str
    severity: str
    reason: str
    previous_value: str | None = None
    current_value: str | None = None
    percentage_change: float | None = None


@dataclass
class AlertRules:
    threshold_pct: float
    detect_increases: bool = True
    detect_decreases: bool = True
    detect_availability: bool = True


def _fmt(price: float | None, ccy: str | None) -> str | None:
    return None if price is None else f"{price:,.2f} {ccy or ''}".strip()


def evaluate(outcome: ValidationOutcome, previous: PreviousObservation | None, rules: AlertRules,
             product_name: str) -> list[AlertDraft]:
    drafts: list[AlertDraft] = []
    status = outcome.status

    if status == ExtractionStatus.FAILED.value:
        reason = "; ".join(i.message for i in outcome.issues if i.severity == "fatal") or "extraction failed"
        return [AlertDraft("extraction_failure", "medium", f"{product_name}: {reason}")]
    if status == ExtractionStatus.NEEDS_REVIEW.value:
        reasons = [i for i in outcome.issues if i.severity == "review"]
        kind = "suspicious_change" if any(i.code == "implausible_change" for i in reasons) else "needs_review"
        pct = None
        if previous and previous.price and outcome.price:
            pct = pct_change(outcome.price, previous.price)
        return [AlertDraft(kind, "high" if kind == "suspicious_change" else "low",
                           f"{product_name}: " + "; ".join(i.message for i in reasons),
                           _fmt(previous.price, previous.currency) if previous else None,
                           _fmt(outcome.price, outcome.currency), pct)]

    if previous is None:
        return drafts  # first trusted observation: baseline only

    if rules.detect_availability and previous.availability != outcome.availability and \
            "unknown" not in (previous.availability, outcome.availability):
        if outcome.availability == "out_of_stock":
            drafts.append(AlertDraft("unavailable", "high", f"{product_name} became unavailable",
                                     previous.availability, outcome.availability))
        elif previous.availability == "out_of_stock":
            drafts.append(AlertDraft("available", "medium", f"{product_name} is available again",
                                     previous.availability, outcome.availability))

    same_ccy = not previous.currency or not outcome.currency or previous.currency == outcome.currency
    pct = pct_change(outcome.price, previous.price)
    if pct is not None and same_ccy and abs(pct) >= rules.threshold_pct:
        up = pct > 0
        if (up and rules.detect_increases) or (not up and rules.detect_decreases):
            sev = "high" if abs(pct) >= 2 * rules.threshold_pct else "medium"
            drafts.append(AlertDraft(
                "price_increase" if up else "price_decrease", sev,
                f"{product_name} price {'rose' if up else 'fell'} {abs(pct):.2f}% "
                f"(threshold {rules.threshold_pct:g}%)",
                _fmt(previous.price, previous.currency), _fmt(outcome.price, outcome.currency), round(pct, 2)))
    return drafts


def notify(alert: Alert, webhook_url: str | None, context: dict) -> None:
    """Optional outbound notification (e.g. Slack/Teams/n8n incoming webhook)."""
    if not webhook_url:
        return
    payload = {
        "type": alert.alert_type, "severity": alert.severity, "reason": alert.reason,
        "previous": alert.previous_value, "current": alert.current_value,
        "percentage_change": alert.percentage_change, **context,
    }
    try:
        httpx.post(webhook_url, json=payload, timeout=10).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("alert webhook failed: %s", e)
