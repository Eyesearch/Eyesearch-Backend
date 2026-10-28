"""Runtime configuration for the EyeSearch backend.

One `Settings` object is built from the environment (and `.env` in development)
and handed to everything that needs it. Nothing reads `os.environ` directly, so
a value that has to change per deployment lives in exactly one place.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    """Every knob the backend reads, with the production default in view."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    turso_db_url: str = ""
    turso_database_url: str = ""
    turso_auth_token: str = ""
    local_db_path: str = "./local.db"
    allow_local_db: bool = True
    # Drop a pre-rebuild schema on startup. Without this a warm database keeps
    # its old columns and every write fails on a missing column.
    migrate_legacy_schema: bool = True

    # --- Server -------------------------------------------------------------
    environment: str = "development"
    port: int = 8000
    log_level: str = "INFO"
    cors_origins: str = "*"
    public_base_url: str = ""

    # --- Storage safety -----------------------------------------------------
    #: Upper bound on schema/bootstrap work before the app starts serving.
    #: The port only opens after this, so an unbounded wait here is a failed
    #: deploy rather than a slow one.
    boot_db_timeout_seconds: float = 20.0

    storage_page_limit: int = 300_000
    storage_count_ttl_seconds: float = 60.0

    # --- Turso request shaping ---------------------------------------------
    # One shared limiter covers crawl, revalidation, and search. 0.1s allows
    # up to 10 HTTP pipeline requests/second by default; deployments can raise
    # this value to match a stricter database plan or provider throttling.
    libsql_min_interval_seconds: float = 0.05

    # --- Crawl pipeline -----------------------------------------------------
    # Tuned for throughput on a single 512 MB instance. `crawl_batch_size`
    # bounds how much HTML is held in memory at once: the fetcher returns every
    # body in the batch before any of it is parsed, so the peak is roughly
    # batch_size x crawl_max_body_bytes. Every value here is overridable from
    # the environment, so a smaller instance can be dialled back without a code
    # change.
    crawler_enabled: bool = True
    #: Throughput is (batch_size / per-batch database floor), so this scales the
    #: crawl directly. It is also the memory ceiling: the fetcher holds every
    #: body in a batch before parsing, so peak ~= batch_size x max_body_bytes.
    crawl_batch_size: int = 60
    #: Outbound links kept from one page before scope filtering. A page can
    #: carry thousands of hrefs; the batch holds every one of them at once.
    max_outbound_links_per_page: int = 100
    crawl_batch_sleep_seconds: float = 0.005
    crawl_empty_backoff_seconds: float = 1.0
    # A batch waits for its slowest member, so this bounds the whole batch, not
    # just one request. 4s fails fast on dead hosts; some Indian government
    # portals legitimately take longer, so raise it if the failure rate climbs.
    crawl_timeout_seconds: float = 7.0

    # --- Turso HTTP timeouts ------------------------------------------------
    #: A cold TLS handshake to Turso can take longer than a warm read; keeping
    #: them separate stops a slow connect from being reported as a slow query.
    turso_connect_timeout_seconds: float = 10.0
    turso_read_timeout_seconds: float = 20.0
    crawl_max_body_bytes: int = 512_000
    crawl_concurrency: int = 60
    #: How many discovered URLs go into one pipeline request. A batch of 120
    #: pages can discover hundreds of links; sending them as one request
    #: overruns the HTTP response window and fails with a ReadTimeout.
    crawl_enqueue_chunk_size: int = 50

    # --- robots.txt ---------------------------------------------------------
    #: Honour robots.txt. A missing, slow or erroring file means "crawl": the
    #: per-domain cap is what bounds damage, and refusing because a 2s fetch
    #: timed out would silently drop whole sites.
    robots_enabled: bool = True
    robots_timeout_seconds: float = 2.0
    robots_cache_entries: int = 2000
    robots_max_concurrent: int = 10

    # --- queue retention ----------------------------------------------------
    #: Terminal queue rows older than this are pruned, a bounded number at a
    #: time, so crawl_queue does not grow without limit.
    queue_retain_days: int = 30
    queue_prune_limit: int = 5000
    #: Follow links to ordinary global domains (.com, .org, .io, ...) as well
    #: as Indian ones. Off by default: the crawl targets the Indian web, and an
    #: open global ring dilutes the index and re-opens the spider-trap surface.
    #: ALLOW_GLOBAL_DOMAINS=true turns it back on.
    allow_global_domains: bool = False
    # When the frontier runs dry and every seed is already indexed, the
    # stalest crawled URLs return to 'pending' after this cooldown.
    rearm_cooldown_seconds: int = 6 * 3600
    rearm_batch_size: int = 25
    crawl_user_agent: str = DESKTOP_USER_AGENT

    # --- Revalidation -------------------------------------------------------
    revalidation_enabled: bool = True
    revalidation_interval_seconds: int = 3600
    revalidation_jitter_seconds: int = 30
    revalidation_batch_size: int = 5
    revalidation_stale_after_seconds: int = 7 * 24 * 3600

    # --- Search -------------------------------------------------------------
    #: No domain may hold more than this many indexed pages, so a single
    #: large portal cannot crowd out the rest of the index. The index ceiling is
    #: roughly cap x distinct domains, so this also bounds how long the crawl
    #: can keep finding new pages: at 10 the frontier starved after ~2,300 pages
    #: against a 300,000-page storage budget.
    max_pages_per_domain: int = 25

    search_default_limit: int = 10
    search_max_limit: int = 50
    search_max_page: int = 20

    @property
    def database_url(self) -> str:
        """The configured database URL, whichever alias the deployment used."""
        return (self.turso_db_url or self.turso_database_url or "").strip()

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def uses_local_database(self) -> bool:
        url = self.database_url
        return not url or url.startswith("file:")

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if not raw or raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings — used by tests that patch the environment."""
    get_settings.cache_clear()


def describe_database_target(settings: Settings) -> str:
    """A short, loggable description of the database this process will use."""
    if settings.uses_local_database:
        return f"local:{settings.database_url or settings.local_db_path}"
    return f"remote:{settings.database_url}"
