"""Tests for `jmi db copy`, the local-to-hosted transfer.

The interesting failure is not "did rows move" but "did the relationships
survive": jobs.duplicate_of_id points at another row in the same table, so a
naive copy either violates the foreign key or silently drops the links.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from typer.testing import CliRunner

from jmi.cli import app
from jmi.models import Job, JobSkill
from jmi.pipeline.ingest import store_raw_jobs

from .conftest import make_raw_job

runner = CliRunner()


@pytest.fixture
def populated(session, settings):
    """A corpus that contains a genuine cross-source duplicate."""
    store_raw_jobs(session, "boardA", [
        make_raw_job(source="boardA", source_job_id="a1"),
        make_raw_job(source="boardA", source_job_id="a2", title="Data Engineer",
                     description_text="Airflow and dbt pipelines.", tags=[]),
    ], settings)
    store_raw_jobs(session, "boardB", [
        make_raw_job(source="boardB", source_job_id="b1"),   # duplicate of a1
    ], settings)
    session.commit()
    return session


def counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    with engine.connect() as conn:
        return {
            "jobs": conn.execute(select(func.count()).select_from(Job.__table__)).scalar(),
            "links": conn.execute(select(func.count()).select_from(JobSkill.__table__)).scalar(),
            "duplicates": conn.execute(
                select(func.count()).select_from(Job.__table__)
                .where(Job.__table__.c.duplicate_of_id.isnot(None))
            ).scalar(),
        }


def test_copy_moves_everything(populated, tmp_path) -> None:
    target = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    result = runner.invoke(app, ["db", "copy", "--to", target])

    assert result.exit_code == 0, result.output
    source_counts = counts(populated.get_bind().url.render_as_string(hide_password=False))
    assert counts(target) == source_counts
    assert source_counts["jobs"] == 3


def test_self_referential_links_survive(populated, tmp_path) -> None:
    """The duplicate points at a row that may not exist yet mid-copy."""
    target = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    runner.invoke(app, ["db", "copy", "--to", target])

    assert counts(target)["duplicates"] == 1

    # and the link must point at a row that actually exists
    engine = create_engine(target)
    with engine.connect() as conn:
        jobs = Job.__table__
        orphans = conn.execute(
            select(func.count()).select_from(jobs).where(
                jobs.c.duplicate_of_id.isnot(None),
                jobs.c.duplicate_of_id.notin_(select(jobs.c.id)),
            )
        ).scalar()
    assert orphans == 0


def test_copy_refuses_to_clobber(populated, tmp_path) -> None:
    target = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    runner.invoke(app, ["db", "copy", "--to", target])

    second = runner.invoke(app, ["db", "copy", "--to", target])
    assert "already holds" in second.output
    assert counts(target)["jobs"] == 3, "destination must be left untouched"


def test_wipe_allows_replacement(populated, tmp_path) -> None:
    target = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    runner.invoke(app, ["db", "copy", "--to", target])
    result = runner.invoke(app, ["db", "copy", "--to", target, "--wipe"])

    assert result.exit_code == 0, result.output
    assert counts(target)["jobs"] == 3, "wipe then copy must not duplicate rows"


def test_copy_to_itself_is_rejected(populated) -> None:
    from jmi.config import get_settings

    result = runner.invoke(app, ["db", "copy", "--to", get_settings().database_url])
    assert result.exit_code == 1
    assert "same database" in result.output
