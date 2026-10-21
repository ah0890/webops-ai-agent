"""Natural language → validated WorkflowDefinition + human-readable execution plan.

The AI decides WHAT (which configured sites, threshold, schedule, which changes matter).
It can only choose among websites that already exist in the user's configuration —
it can never introduce a new domain.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent.llm import LLMProvider, LLMUnavailable
from app.config.schema import ScheduleSpec, WorkflowDefinition

log = logging.getLogger(__name__)


class PlannedWorkflow(BaseModel):
    """Schema Claude fills in. Kept flat and simple for structured outputs."""

    name: str = Field(description="Short workflow name, e.g. 'Daily supplier price watch'")
    description: str
    websites: list[str] = Field(description="Exact names from the available list, or ['*'] for all")
    skus: list[str] | None = Field(None, description="Only if the user restricted to specific SKUs")
    price_change_threshold: float | None = Field(None, description="Percent, e.g. 5 for 5%")
    detect_increases: bool = True
    detect_decreases: bool = True
    detect_availability: bool = True
    generate_excel: bool = True
    schedule_kind: Literal["manual", "interval", "daily", "weekly"] = "manual"
    every_hours: float | None = None
    at: str | None = Field(None, description="24h local time HH:MM")
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    rationale: str = Field(description="One or two sentences explaining the interpretation")
    unsupported_requests: list[str] = Field(
        default_factory=list, description="Parts of the request this price-monitoring platform cannot do")


class WorkflowPlan(BaseModel):
    name: str
    description: str
    instruction: str
    planner: str  # claude | rule-based
    rationale: str
    warnings: list[str] = Field(default_factory=list)
    definition: WorkflowDefinition
    execution_plan: list[str]


SYSTEM = """You configure a price & availability monitoring workflow for an authorized browser-automation \
platform. Map the user's request onto the given fields only. Choose websites strictly from the provided list \
(exact names) or use ["*"] when the user means all of them. Never invent websites, URLs or products. \
If something requested is outside price/availability monitoring, list it in unsupported_requests."""


def _to_definition(p: PlannedWorkflow, available: list[str], warnings: list[str]) -> WorkflowDefinition:
    lookup = {a.lower(): a for a in available}
    sites: list[str] = []
    for w in p.websites:
        if w == "*":
            sites = ["*"]
            break
        if w.lower() in lookup:
            sites.append(lookup[w.lower()])
        else:
            warnings.append(f"ignored unknown website {w!r} (not in configuration)")
    if not sites:
        sites = ["*"]
        warnings.append("no configured website matched; workflow will cover all configured websites")
    try:
        schedule = ScheduleSpec(kind=p.schedule_kind, every_hours=p.every_hours, at=p.at, weekday=p.weekday)
    except ValidationError as e:
        warnings.append(f"schedule not understood ({e.errors()[0]['msg']}); set to manual")
        schedule = ScheduleSpec()
    threshold = p.price_change_threshold
    if threshold is not None and not 0 < threshold <= 100:
        warnings.append(f"threshold {threshold} out of range; using default")
        threshold = None
    return WorkflowDefinition(
        websites=sites, skus=p.skus or None, price_change_threshold=threshold,
        detect_increases=p.detect_increases, detect_decreases=p.detect_decreases,
        detect_availability=p.detect_availability, generate_excel=p.generate_excel, schedule=schedule,
    )


_WEEKDAY_WORDS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
                  "friday": "fri", "saturday": "sat", "sunday": "sun"}


def rule_based_parse(instruction: str, available: list[str]) -> PlannedWorkflow:
    """Deterministic interpretation used when no LLM is configured."""
    text = instruction.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", text)
    threshold = float(m.group(1)) if m else None

    at = None
    tm = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if tm:
        h, mi, ap = int(tm.group(1)), int(tm.group(2) or 0), tm.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h < 24 and 0 <= mi < 60:
            at = f"{h:02d}:{mi:02d}"
    kind, every, weekday = "manual", None, None
    if im := re.search(r"every\s+(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", text):
        kind, every = "interval", float(im.group(1))
    elif re.search(r"\b(hourly|every hour)\b", text):
        kind, every = "interval", 1.0
    elif day := next((v for k, v in _WEEKDAY_WORDS.items() if f"every {k}" in text), None):
        kind, weekday, at = "weekly", day, at or "08:00"
    elif re.search(r"\b(weekly|every week)\b", text):
        kind, weekday, at = "weekly", "mon", at or "08:00"
    elif re.search(r"\b(daily|every day|every morning|each morning|every evening)\b", text) or at:
        kind = "daily"
        at = at or ("18:00" if "evening" in text else "08:00")

    inc = bool(re.search(r"\b(increase|increases|rise|rises|go up|goes up|more expensive)\b", text))
    dec = bool(re.search(r"\b(decrease|decreases|drop|drops|fall|falls|cheaper|go down|discount)\b", text))
    either = bool(re.search(r"\bchange|changes|move|moves\b", text)) or not (inc or dec)
    sites = [a for a in available if a.lower() in text] or ["*"]
    return PlannedWorkflow(
        name=("Price monitor" + (f" ({threshold:g}% threshold)" if threshold else "")),
        description=instruction.strip()[:500],
        websites=sites, price_change_threshold=threshold,
        detect_increases=inc or either, detect_decreases=dec or either,
        detect_availability=True, generate_excel=True,
        schedule_kind=kind, every_hours=every, at=at, weekday=weekday,  # type: ignore[arg-type]
        rationale="Parsed with deterministic rules (no LLM configured): threshold, schedule keywords and "
                  "website names mentioned in the instruction.",
    )


def execution_plan(defn: WorkflowDefinition, site_names: list[str], product_count: int,
                   default_threshold: float, llm_fallback: bool) -> list[str]:
    sites = ", ".join(site_names) if site_names else "no websites configured yet"
    thr = defn.price_change_threshold or default_threshold
    kinds = [k for k, on in (("increases", defn.detect_increases), ("decreases", defn.detect_decreases)) if on]
    strategies = "configured selectors → schema.org JSON-LD → microdata → product meta tags"
    if llm_fallback:
        strategies += " → Claude fallback (evidence-checked)"
    steps = [
        f"Load {product_count} active product(s) from: {sites}",
        "Check each site's robots.txt and domain allowlist; log in only where an authorized login is configured",
        "Launch Chromium; process websites in parallel (bounded) and products sequentially per site with polite delays",
        "For each product: open the product URL → run configured pre-extraction actions → wait for content",
        "Capture a screenshot before extraction",
        f"Extract name, price, currency, availability and SKU via {strategies}",
        "Validate: price format, currency, SKU/name match, redirects, cross-source agreement, plausibility",
        "Store the observation (append-only) with its data-quality status; capture a screenshot after extraction",
        f"Compare with the previous trusted observation; alert on price {' and '.join(kinds) or 'changes'} ≥ {thr:g}%"
        + (" and on availability changes" if defn.detect_availability else ""),
        "On failure: screenshot, inspect page state, try a safe recovery (reload / dismiss overlay / scroll), "
        "retry within limits, then continue with the next product",
    ]
    if defn.generate_excel:
        steps.append("Generate the Excel report (summary, current prices, changes, history, availability, errors)")
    steps.append(f"Schedule: {defn.schedule.describe()}")
    return steps


def plan_workflow(instruction: str, available_sites: list[str], product_count: int, default_threshold: float,
                  provider: LLMProvider | None, llm_fallback: bool = False) -> WorkflowPlan:
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("instruction must not be empty")
    warnings: list[str] = []
    planner = "rule-based"
    parsed: PlannedWorkflow | None = None
    if provider is not None:
        prompt = (f"Available websites (exact names): {available_sites or '[] (none configured yet)'}\n"
                  f"Default threshold: {default_threshold}%\n\nUser request:\n{instruction}")
        try:
            parsed = provider.structured(SYSTEM, prompt, PlannedWorkflow, max_tokens=4000)
            planner = provider.name
        except LLMUnavailable as e:
            warnings.append(f"LLM planner unavailable ({e}); used rule-based planner")
    if parsed is None:
        parsed = rule_based_parse(instruction, available_sites)
    warnings.extend(f"not supported: {u}" for u in parsed.unsupported_requests)
    defn = _to_definition(parsed, available_sites, warnings)
    covered = available_sites if defn.websites == ["*"] else defn.websites
    return WorkflowPlan(
        name=parsed.name[:200], description=parsed.description[:1000], instruction=instruction,
        planner=planner, rationale=parsed.rationale, warnings=warnings, definition=defn,
        execution_plan=execution_plan(defn, covered, product_count, default_threshold, llm_fallback),
    )
