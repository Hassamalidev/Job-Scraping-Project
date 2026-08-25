"""Configuration tests, mostly about not leaking credentials."""

from __future__ import annotations

import pytest

from jmi.config import Settings, mask_url

NEON = "postgresql+psycopg://neondb_owner:npg_SECRET123@ep-x.aws.neon.tech/neondb?sslmode=require"


def test_password_is_masked() -> None:
    masked = mask_url(NEON)
    assert "npg_SECRET123" not in masked
    assert "***" in masked


def test_masking_keeps_everything_useful() -> None:
    """A masked URL still has to be diagnosable at a glance."""
    masked = mask_url(NEON)
    assert masked.startswith("postgresql+psycopg://")
    assert "neondb_owner" in masked          # which user
    assert "ep-x.aws.neon.tech" in masked    # which host
    assert "/neondb" in masked               # which database
    assert "sslmode=require" in masked       # whether TLS was asked for


def test_urls_without_a_password_are_untouched() -> None:
    assert mask_url("sqlite:///data/jobs.db") == "sqlite:///data/jobs.db"
    assert mask_url("postgresql://host/db") == "postgresql://host/db"


def test_port_is_preserved() -> None:
    masked = mask_url("postgresql+psycopg://user:pw@localhost:5432/jmi")
    assert "localhost:5432" in masked
    assert "pw" not in masked


@pytest.mark.parametrize("url", ["", "not a url", "postgresql://"])
def test_malformed_urls_do_not_raise(url: str) -> None:
    assert isinstance(mask_url(url), str)


def test_settings_expose_the_safe_form() -> None:
    settings = Settings(database_url=NEON)
    assert "npg_SECRET123" not in settings.safe_database_url
    assert settings.database_url == NEON, "the real URL must still be usable"
