"""Test fixtures: an isolated on-disk SQLite database per test session."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the app at a throwaway database before anything imports the engine."""
    data_dir = tmp_path_factory.mktemp("jmi-data")
    os.environ["JMI_DATABASE_URL"] = f"sqlite:///{(data_dir / 'test.db').as_posix()}"
    os.environ["JMI_DATA_DIR"] = str(data_dir)
    os.environ["JMI_ARCHIVE_RAW"] = "false"
    os.environ["JMI_RESPECT_ROBOTS"] = "false"

    from jmi.config import get_settings

    get_settings.cache_clear()

    import jmi.db as db

    db._engine = None
    db._SessionFactory = None
    db.init_db()
    yield


@pytest.fixture
def session(_isolated_settings) -> Iterator:
    """A clean database for each test."""
    from jmi.db import get_session_factory, reset_db

    reset_db()
    db_session = get_session_factory()()
    try:
        yield db_session
        db_session.commit()
    finally:
        db_session.close()


@pytest.fixture
def settings():
    from jmi.config import get_settings

    return get_settings()


def make_raw_job(**overrides):
    """A RawJob with sensible defaults; override only what the test cares about."""
    from datetime import datetime

    from jmi.scrapers.base import RawJob

    defaults = dict(
        source="testsource",
        source_job_id="1",
        title="Senior Backend Engineer",
        company="Acme Inc.",
        url="https://example.com/jobs/1",
        location="Berlin, Germany",
        description_html="<p>We use Python, PostgreSQL and Docker.</p>",
        description_text="We use Python, PostgreSQL and Docker. Salary: $150,000 - $180,000 per year.",
        posted_at=datetime(2026, 8, 1, tzinfo=UTC),
        tags=["python"],
    )
    defaults.update(overrides)
    return RawJob(**defaults)
