"""Structured browser actions.

Everything the browser does is expressed as one of these validated models. The LLM may
*propose* actions (e.g. a recovery step) but they are parsed through `parse_action`
before the executor will touch them, and URLs are additionally checked against the
website's domain allowlist at execution time.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

MAX_SELECTOR_LEN = 500
EXTRACTABLE_FIELDS = {"product_name", "price", "currency", "availability", "sku", "metadata"}


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(None, max_length=200)


def _check_selector(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("selector must not be empty")
    if len(v) > MAX_SELECTOR_LEN:
        raise ValueError("selector too long")
    if v.lower().startswith("javascript:"):
        raise ValueError("javascript: selectors are not allowed")
    return v


def _check_url(v: str) -> str:
    if not v.lower().startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs are allowed")
    return v


Selector = Annotated[str, AfterValidator(_check_selector)]
HttpUrlStr = Annotated[str, Field(max_length=2048), AfterValidator(_check_url)]


class GotoAction(_Action):
    action: Literal["goto"] = "goto"
    url: HttpUrlStr
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "domcontentloaded"


class ClickAction(_Action):
    action: Literal["click"] = "click"
    selector: Selector
    optional: bool = Field(False, description="Do not fail if the element is absent (e.g. cookie banners)")


class FillAction(_Action):
    action: Literal["fill"] = "fill"
    selector: Selector
    value: str = Field(max_length=2000, description="May reference env vars as ${NAME}; resolved at run time")
    secret: bool = False


class SelectAction(_Action):
    action: Literal["select"] = "select"
    selector: Selector
    value: str = Field(max_length=500)


class ScrollAction(_Action):
    action: Literal["scroll"] = "scroll"
    to: Literal["bottom", "top", "selector", "pixels"] = "bottom"
    selector: Selector | None = None
    pixels: int = Field(800, ge=-20000, le=20000)


class WaitAction(_Action):
    action: Literal["wait"] = "wait"
    selector: Selector | None = None
    milliseconds: int | None = Field(None, ge=0, le=30000)
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = None
    optional: bool = False


class ExtractAction(_Action):
    action: Literal["extract"] = "extract"
    selector: Selector | None = Field(None, description="Optional container to scope extraction")
    fields: list[str] = Field(default_factory=lambda: ["product_name", "price", "currency", "availability", "sku"])

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, v: list[str]) -> list[str]:
        unknown = set(v) - EXTRACTABLE_FIELDS
        if unknown:
            raise ValueError(f"unknown extract fields: {sorted(unknown)}")
        return v


class ScreenshotAction(_Action):
    action: Literal["screenshot"] = "screenshot"
    name: str = Field("page", pattern=r"^[A-Za-z0-9_\-]{1,60}$")
    full_page: bool = False


class DownloadAction(_Action):
    action: Literal["download"] = "download"
    selector: Selector


class NavigateBackAction(_Action):
    action: Literal["navigate_back"] = "navigate_back"


class NewTabAction(_Action):
    action: Literal["new_tab"] = "new_tab"
    url: HttpUrlStr


class SwitchTabAction(_Action):
    action: Literal["switch_tab"] = "switch_tab"
    index: int = Field(ge=0, le=20)


class PressAction(_Action):
    action: Literal["press"] = "press"
    key: Literal["Escape", "Enter", "Tab", "PageDown", "End", "Home"]


BrowserAction = Annotated[
    GotoAction
    | ClickAction
    | FillAction
    | SelectAction
    | ScrollAction
    | WaitAction
    | ExtractAction
    | ScreenshotAction
    | DownloadAction
    | NavigateBackAction
    | NewTabAction
    | SwitchTabAction
    | PressAction,
    Field(discriminator="action"),
]

_adapter: TypeAdapter[BrowserAction] = TypeAdapter(BrowserAction)
_list_adapter: TypeAdapter[list[BrowserAction]] = TypeAdapter(list[BrowserAction])


def parse_action(data: dict) -> BrowserAction:
    """Validate an untrusted action dict (from config, API or LLM). Raises pydantic.ValidationError."""
    return _adapter.validate_python(data)


def parse_actions(data: list[dict]) -> list[BrowserAction]:
    return _list_adapter.validate_python(data)
