"""Safe recovery strategies for failed extractions.

Deterministic strategies come first. Only the last one optionally asks Claude for a
suggestion, and its answer is restricted to non-navigating actions (click/scroll/wait/press)
that pass the normal action validation.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent.llm import LLMProvider, LLMUnavailable
from app.models.actions import BrowserAction, parse_action

log = logging.getLogger(__name__)

_CONSENT_BUTTONS = (
    "button:has-text('Accept'), button:has-text('I agree'), button:has-text('Agree'), "
    "button:has-text('Got it'), button:has-text('OK'), [aria-label='Close'], button.close"
)


def deterministic_strategies() -> list[tuple[str, list[dict]]]:
    return [
        ("reload_and_wait", [
            {"action": "wait", "load_state": "load", "optional": True},
            {"action": "wait", "milliseconds": 2500},
        ]),
        ("dismiss_overlays_and_scroll", [
            {"action": "press", "key": "Escape"},
            {"action": "click", "selector": _CONSENT_BUTTONS, "optional": True},
            {"action": "scroll", "to": "bottom"},
            {"action": "wait", "milliseconds": 1500},
            {"action": "scroll", "to": "top"},
            {"action": "wait", "load_state": "networkidle", "optional": True},
        ]),
        ("wait_longer", [
            {"action": "wait", "load_state": "networkidle", "optional": True},
            {"action": "wait", "milliseconds": 5000},
        ]),
    ]


class RecoveryStep(BaseModel):
    action: Literal["click", "scroll", "wait", "press"]
    selector: str | None = None
    key: Literal["Escape", "Enter", "PageDown", "End", "Home"] | None = None
    milliseconds: int | None = None


class RecoverySuggestion(BaseModel):
    diagnosis: str = Field(description="Why the price is probably not visible")
    steps: list[RecoveryStep] = Field(default_factory=list, description="At most 4 steps")


def ai_strategy(provider: LLMProvider, page_state: dict, text_excerpt: str, error: str) -> tuple[str, list[BrowserAction]]:
    prompt = (f"Extraction failed: {error}\nPage state: {page_state}\n\nVisible text (truncated):\n{text_excerpt[:4000]}\n\n"
              "Suggest up to 4 safe steps (click/scroll/wait/press) that would reveal the product price, "
              "e.g. closing a modal or expanding a section. Never suggest logging in or solving challenges.")
    try:
        s = provider.structured("You help a browser automation recover from a page that did not show a price.",
                                prompt, RecoverySuggestion, max_tokens=1500)
    except LLMUnavailable as e:
        return f"ai_unavailable: {e}", []
    actions: list[BrowserAction] = []
    for st in s.steps[:4]:
        raw = {"action": st.action}
        if st.action == "click":
            raw |= {"selector": st.selector or "", "optional": True}
        elif st.action == "press":
            raw |= {"key": st.key or "Escape"}
        elif st.action == "wait":
            raw |= {"milliseconds": min(st.milliseconds or 1000, 10000)}
        elif st.action == "scroll":
            raw |= {"to": "bottom"}
        try:
            actions.append(parse_action(raw))
        except ValidationError:
            log.info("dropped invalid AI recovery step %s", raw)
    return f"ai_suggested: {s.diagnosis[:200]}", actions
