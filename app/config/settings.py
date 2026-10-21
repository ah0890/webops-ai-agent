"""Runtime settings loaded from environment variables / `.env`.

Secrets (API keys, site passwords) are only ever read from the environment and are
held as `SecretStr` so they never appear in reprs or logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'webops.db').as_posix()}"
    screenshots_dir: Path = PROJECT_ROOT / "screenshots"
    reports_dir: Path = PROJECT_ROOT / "reports"
    sessions_dir: Path = PROJECT_ROOT / "data" / "sessions"
    downloads_dir: Path = PROJECT_ROOT / "data" / "downloads"

    # Real-data configuration files
    websites_config: Path = PROJECT_ROOT / "config" / "websites.yaml"
    workflows_config: Path = PROJECT_ROOT / "config" / "workflows.yaml"

    # Price intelligence
    price_change_threshold: float = Field(5.0, gt=0, le=100)
    max_plausible_change_pct: float = Field(80.0, gt=0, description="Bigger moves are flagged NEEDS_REVIEW")
    supported_currencies: str = "USD,EUR,GBP,PKR,INR,AED,SAR,CAD,AUD,JPY,CNY,CHF,SEK,NOK,DKK,PLN,TRY,ZAR,SGD,NZD"

    # Browser
    browser_headless: bool = True
    browser_slow_mo_ms: int = Field(0, ge=0, le=5000)
    navigation_timeout_ms: int = Field(30000, ge=1000, le=180000)
    action_timeout_ms: int = Field(10000, ge=500, le=120000)
    user_agent_suffix: str = "WebOpsAgent/0.1"

    # Execution limits
    max_concurrency: int = Field(2, ge=1, le=16, description="Websites processed in parallel")
    default_request_delay_seconds: float = Field(3.0, ge=0, description="Pause between pages on one site")
    max_retries: int = Field(2, ge=0, le=5)
    max_products_per_run: int = Field(1000, ge=1)
    run_timeout_seconds: int = Field(3600, ge=60)
    respect_robots_txt: bool = True

    # LLM (optional — the platform runs deterministically without it)
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_extraction_fallback: bool = True
    llm_refusal_fallbacks: bool = True

    # API / scheduler
    api_key: SecretStr | None = Field(None, description="If set, mutating API calls require X-API-Key")
    scheduler_enabled: bool = True
    scheduler_poll_seconds: int = Field(30, ge=5)
    alert_webhook_url: SecretStr | None = None

    @property
    def currency_set(self) -> set[str]:
        return {c.strip().upper() for c in self.supported_currencies.split(",") if c.strip()}

    @property
    def llm_available(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip())

    def ensure_dirs(self) -> None:
        for d in (self.screenshots_dir, self.reports_dir, self.sessions_dir, self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
