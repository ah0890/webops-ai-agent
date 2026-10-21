"""Data shapes passed between extraction, validation and storage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["selector", "json_ld", "microdata", "meta", "llm"]


class Candidate(BaseModel):
    """What one extraction strategy found on the page (values are raw, not yet trusted)."""

    source: Source
    product_name: str | None = None
    price_text: str | None = None
    price: float | None = None
    currency: str | None = None
    availability_raw: str | None = None
    availability: str | None = None  # normalised: in_stock | out_of_stock | preorder | unknown
    sku: str | None = None
    ambiguous: bool = False
    notes: list[str] = Field(default_factory=list)


class PageSnapshot(BaseModel):
    """Raw material collected from the live DOM in a single evaluate() call."""

    url: str
    title: str | None = None
    json_ld: list[str] = Field(default_factory=list)
    microdata: dict[str, str | None] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)
    selector_values: dict[str, str | None] = Field(default_factory=dict)
    selector_missing: list[str] = Field(default_factory=list)
    text_excerpt: str = ""


class ExtractionResult(BaseModel):
    url: str
    final_url: str | None = None
    page_title: str | None = None
    http_status: int | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    chosen: Candidate | None = None
    selector_missing: list[str] = Field(default_factory=list)
    error: str | None = None

    def raw_dump(self) -> dict:
        return {
            "candidates": [c.model_dump(exclude_none=True) for c in self.candidates],
            "selector_missing": self.selector_missing,
            "chosen_source": self.chosen.source if self.chosen else None,
        }


class Issue(BaseModel):
    code: str
    severity: Literal["info", "warning", "review", "fatal"]
    message: str


class ValidationOutcome(BaseModel):
    status: str  # ExtractionStatus value
    issues: list[Issue] = Field(default_factory=list)
    price: float | None = None
    currency: str | None = None
    availability: str = "unknown"
    product_name: str | None = None
    sku: str | None = None
    price_text: str | None = None
    method: str | None = None
