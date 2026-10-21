"""Schema for the user-supplied real-world configuration (`config/websites.yaml`, `config/workflows.yaml`).

Nothing in here contains supplier data — it only describes the *shape* of what the
user provides. Validation errors are raised with the offending path so a typo in a
YAML file is reported, not silently ignored.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.actions import BrowserAction


class SelectorSet(BaseModel):
    """CSS selectors for a product page. Append `@attr` to read an attribute (e.g. `meta[itemprop=price]@content`)."""

    model_config = ConfigDict(extra="forbid")
    price: str | None = None
    product_name: str | None = None
    availability: str | None = None
    sku: str | None = None
    currency: str | None = None

    def merged_over(self, base: SelectorSet | None) -> SelectorSet:
        if base is None:
            return self
        return SelectorSet(**{**base.model_dump(exclude_none=True), **self.model_dump(exclude_none=True)})


class AvailabilityRules(BaseModel):
    """Case-insensitive text patterns used to classify the availability element's text."""

    model_config = ConfigDict(extra="forbid")
    in_stock: list[str] = Field(default_factory=lambda: ["in stock", "available", "add to cart", "add to basket"])
    out_of_stock: list[str] = Field(
        default_factory=lambda: ["out of stock", "sold out", "unavailable", "currently unavailable", "not available"]
    )
    preorder: list[str] = Field(default_factory=lambda: ["pre-order", "preorder", "backorder"])


class LoginConfig(BaseModel):
    """Legitimate, user-authorized login. Credentials are referenced as ${ENV_VAR} — never written in YAML."""

    model_config = ConfigDict(extra="forbid")
    url: str
    steps: list[BrowserAction]
    success_selector: str | None = Field(None, description="Element that only exists when logged in")
    reuse_session: bool = True

    @model_validator(mode="after")
    def _no_literal_secrets(self) -> LoginConfig:
        for step in self.steps:
            if getattr(step, "action", None) == "fill" and step.secret and "${" not in step.value:
                raise ValueError("secret fill values must reference an environment variable, e.g. ${SUPPLIER_PASSWORD}")
        return self


class ProductConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    url: str
    sku: str | None = Field(None, max_length=120)
    currency: str | None = Field(None, min_length=3, max_length=3)
    price_change_threshold: float | None = Field(None, gt=0, le=100)
    active: bool = True
    expected_name_contains: str | None = Field(None, description="Guard against redirects to the wrong product")
    selectors: SelectorSet | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError(f"invalid product URL: {v!r}")
        return v

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class WebsiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    base_url: str
    allowed_domains: list[str] = Field(default_factory=list)
    currency: str | None = Field(None, min_length=3, max_length=3)
    request_delay_seconds: float | None = Field(None, ge=0, le=600)
    respect_robots_txt: bool | None = None
    active: bool = True
    selectors: SelectorSet | None = None
    availability_rules: AvailabilityRules = Field(default_factory=AvailabilityRules)
    pre_extract_actions: list[BrowserAction] = Field(
        default_factory=list, description="e.g. dismiss a cookie banner (use optional: true)"
    )
    login: LoginConfig | None = None
    products: list[ProductConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _domains(self) -> WebsiteConfig:
        host = urlparse(self.base_url).hostname
        if not host:
            raise ValueError(f"invalid base_url for {self.name!r}")
        if not self.allowed_domains:
            self.allowed_domains = [host.removeprefix("www.")]
        self.allowed_domains = [d.lower().strip().lstrip(".") for d in self.allowed_domains]
        from app.security.url_guard import host_allowed  # local import avoids a cycle

        for p in self.products:
            if not host_allowed(urlparse(p.url).hostname or "", self.allowed_domains):
                raise ValueError(
                    f"product URL {p.url} is outside allowed_domains {self.allowed_domains} of {self.name!r}"
                )
        if self.currency:
            self.currency = self.currency.upper()
        return self


class WebsitesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    websites: list[WebsiteConfig] = Field(default_factory=list)


class ScheduleSpec(BaseModel):
    """manual | interval (every_hours) | daily (at HH:MM) | weekly (weekday + at)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["manual", "interval", "daily", "weekly"] = "manual"
    every_hours: float | None = Field(None, gt=0, le=24 * 31)
    at: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None

    @model_validator(mode="after")
    def _complete(self) -> ScheduleSpec:
        if self.kind == "interval" and not self.every_hours:
            raise ValueError("interval schedules need every_hours")
        if self.kind in ("daily", "weekly") and not self.at:
            raise ValueError(f"{self.kind} schedules need at: 'HH:MM'")
        if self.kind == "weekly" and not self.weekday:
            raise ValueError("weekly schedules need weekday")
        return self

    def describe(self) -> str:
        if self.kind == "interval":
            return f"Every {self.every_hours:g} hours"
        if self.kind == "daily":
            return f"Daily at {self.at}"
        if self.kind == "weekly":
            return f"Weekly on {(self.weekday or '').title()} at {self.at}"
        return "Manual (Run Now)"


class WorkflowDefinition(BaseModel):
    """What a price-monitoring workflow should do. Produced from YAML, the API, or the AI planner."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["price_monitor"] = "price_monitor"
    websites: list[str] = Field(default_factory=lambda: ["*"], description="Website names, or ['*'] for all")
    skus: list[str] | None = Field(None, description="Restrict to these SKUs (optional)")
    price_change_threshold: float | None = Field(None, gt=0, le=100)
    detect_increases: bool = True
    detect_decreases: bool = True
    detect_availability: bool = True
    generate_excel: bool = True
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    instruction: str | None = Field(None, description="Original natural-language request, if any")
    enabled: bool = True
    definition: WorkflowDefinition = Field(default_factory=WorkflowDefinition)


class WorkflowsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflows: list[WorkflowConfig] = Field(default_factory=list)
