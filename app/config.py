"""Typed application settings.

Every value has a working local-development default: the service must boot with
no environment variables at all (SQLite file fallback, no Turso token), and pick
up Render's env vars when they exist.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration.

    ``validation_alias`` lists the env var names accepted for each field, so both
    the names documented for Render (``TURSO_DB_URL``) and the names used by
    Turso's own dashboard (``TURSO_DATABASE_URL``) work.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- identity -----------------------------------------------------------
    app_name: str = "EyeSearch Gateway"
    app_version: str = "1.0.0"
    environment: Literal["development", "production", "test"] = Field(
        default="development",
        validation_alias=AliasChoices("ENVIRONMENT", "APP_ENV"),
    )

    # --- server -------------------------------------------------------------
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT"))
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("LOG_LEVEL"))

    # --- database -----------------------------------------------------------
    turso_db_url: str = Field(
        default="file:./local.db",
        validation_alias=AliasChoices("TURSO_DB_URL", "TURSO_DATABASE_URL", "LIBSQL_URL"),
    )
    turso_auth_token: str = Field(
        default="",
        validation_alias=AliasChoices("TURSO_AUTH_TOKEN", "TURSO_DB_TOKEN"),
    )
    db_timeout_s: float = Field(default=4.0, validation_alias=AliasChoices("DB_TIMEOUT_S"))

    # --- cache --------------------------------------------------------------
    cache_ttl_hours: int = Field(default=24, validation_alias=AliasChoices("CACHE_TTL_HOURS"))
    cache_min_results: int = Field(default=1, validation_alias=AliasChoices("CACHE_MIN_RESULTS"))

    # --- retrieval ----------------------------------------------------------
    search_region: str = Field(default="in-en", validation_alias=AliasChoices("SEARCH_REGION"))
    safesearch: Literal["on", "moderate", "off"] = Field(
        default="moderate", validation_alias=AliasChoices("SAFESEARCH")
    )
    ddgs_budget_s: float = Field(default=3.5, validation_alias=AliasChoices("DDGS_BUDGET_S"))
    bing_budget_s: float = Field(default=3.0, validation_alias=AliasChoices("BING_BUDGET_S"))
    html_budget_s: float = Field(default=3.0, validation_alias=AliasChoices("HTML_BUDGET_S"))
    lite_budget_s: float = Field(default=2.5, validation_alias=AliasChoices("LITE_BUDGET_S"))
    chain_budget_s: float = Field(default=7.0, validation_alias=AliasChoices("CHAIN_BUDGET_S"))
    enable_bing_scrape: bool = Field(
        default=False, validation_alias=AliasChoices("ENABLE_BING_SCRAPE")
    )
    max_concurrent_live: int = Field(default=4, validation_alias=AliasChoices("MAX_CONCURRENT_LIVE"))
    breaker_threshold: int = Field(default=5, validation_alias=AliasChoices("BREAKER_THRESHOLD"))
    breaker_reset_s: float = Field(default=60.0, validation_alias=AliasChoices("BREAKER_RESET_S"))

    # --- http client --------------------------------------------------------
    http_connect_timeout_s: float = 2.0
    http_read_timeout_s: float = 6.0
    http_write_timeout_s: float = 2.0
    http_pool_timeout_s: float = 1.0
    http_max_connections: int = 20
    http_max_keepalive: int = 10

    # --- contract limits ----------------------------------------------------
    default_limit: int = Field(default=10, validation_alias=AliasChoices("DEFAULT_LIMIT"))
    max_limit: int = Field(default=50, validation_alias=AliasChoices("MAX_LIMIT"))
    snippet_max_chars: int = Field(default=260, validation_alias=AliasChoices("SNIPPET_MAX_CHARS"))
    ai_snippet_max_chars: int = Field(
        default=200, validation_alias=AliasChoices("AI_SNIPPET_MAX_CHARS")
    )
    cors_origins: str = Field(default="*", validation_alias=AliasChoices("CORS_ORIGINS"))
    # Absolute origin advertised in /opensearch.xml; empty means "derive from the
    # incoming request", which is right behind Render's proxy.
    public_base_url: str = Field(default="", validation_alias=AliasChoices("PUBLIC_BASE_URL"))
    # Where the web client is deployed — used by /opensearch.xml for the human
    # search URL and the icon, which live with the UI rather than the API.
    web_base_url: str = Field(
        default="https://eyesearch.github.io/Eyesearch/web",
        validation_alias=AliasChoices("WEB_BASE_URL"),
    )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("max_limit", "default_limit")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limit settings must be >= 1")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list; ``*`` keeps the frontend able to connect."""
        raw = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        return raw or ["*"]

    @property
    def allow_credentials(self) -> bool:
        """Wildcard origins and credentialed requests are mutually exclusive."""
        return "*" not in self.cors_origin_list

    @property
    def is_remote_db(self) -> bool:
        return not self.turso_db_url.startswith("file:")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton — import this, do not construct Settings directly."""
    return Settings()
