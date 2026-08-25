"""Application settings, loaded from environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Every knob the pipeline exposes. Override with JMI_-prefixed env vars."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="JMI_", extra="ignore", case_sensitive=False
    )

    # --- storage -------------------------------------------------------
    database_url: str = Field(default=f"sqlite:///{(DATA_DIR / 'jobs.db').as_posix()}")
    data_dir: Path = DATA_DIR
    archive_raw: bool = True

    # --- polite crawling ----------------------------------------------
    user_agent: str = (
        "JobMarketIntel/1.0 (+https://github.com/yourname/job-market-intel; "
        "portfolio project; contact: you@example.com)"
    )
    request_timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 0.8
    rate_limit_per_second: float = 0.75
    respect_robots: bool = True

    # --- sources -------------------------------------------------------
    enabled_sources: list[str] = [
        "remoteok", "weworkremotely", "hackernews", "greenhouse", "lever",
        "mustakbil", "himalayas", "arbeitnow",
    ]
    hn_threads: int = 2
    greenhouse_boards: list[str] = [
        "stripe", "figma", "databricks", "discord", "anthropic",
        "airtable", "gitlab", "reddit", "robinhood", "coinbase",
    ]
    greenhouse_max_per_board: int = 250
    # Mustakbil needs one request per posting, so the cap is deliberately low.
    mustakbil_max_jobs: int = 140
    mustakbil_sitemap_pages: int = 2
    lever_boards: list[str] = ["palantir", "gopuff", "angellist"]
    lever_max_per_board: int = 250
    himalayas_max_jobs: int = 300
    arbeitnow_pages: int = 3

    # --- scheduler -----------------------------------------------------
    scrape_interval_minutes: int = 360
    stale_after_days: int = 45

    log_level: str = "INFO"

    @property
    def safe_database_url(self) -> str:
        """The connection string with the password removed.

        Managed hosts keep container logs indefinitely and show them to anyone
        with dashboard access, so a connection string must never be logged
        verbatim. Everything that prints the database target goes through here.
        """
        return mask_url(self.database_url)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.raw_dir):
            path.mkdir(parents=True, exist_ok=True)


def mask_url(url: str) -> str:
    """Replace the password in a URL with ``***``, leaving the rest readable."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable database url>"
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:***@{host}" if parts.username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
