"""Storage-level tests: deduplication, idempotency and failure isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from jmi.models import Company, Job, JobSkill
from jmi.pipeline.dedupe import SIMHASH_MAX_DISTANCE, title_similarity
from jmi.pipeline.ingest import store_raw_jobs
from jmi.utils import company_key, hamming_distance, simhash

from .conftest import make_raw_job


def store(session, settings, source, jobs):
    return store_raw_jobs(session, source, jobs, settings)


# --- ingest basics ------------------------------------------------------


def test_stores_and_enriches_a_posting(session, settings) -> None:
    report = store(session, settings, "testsource", [make_raw_job()])
    assert (report.created, report.failed) == (1, 0)

    job = session.execute(select(Job)).scalar_one()
    assert job.title == "Senior Backend Engineer"
    assert job.seniority == "senior"
    assert job.country == "Germany"
    assert job.region == "Europe"
    assert job.salary_min_usd == pytest.approx(150_000)
    assert {link.skill.slug for link in job.skill_links} >= {"python", "postgresql", "docker"}


def test_reingest_is_idempotent(session, settings) -> None:
    store(session, settings, "testsource", [make_raw_job()])
    report = store(session, settings, "testsource", [make_raw_job()])

    assert (report.created, report.updated) == (0, 1)
    assert session.scalar(select(func.count(Job.id))) == 1


def test_company_names_are_normalised_to_one_row(session, settings) -> None:
    store(session, settings, "testsource", [
        make_raw_job(source_job_id="1", company="Acme Inc."),
        make_raw_job(source_job_id="2", company="ACME, LLC", title="Data Engineer"),
        make_raw_job(source_job_id="3", company="acme", title="Product Manager"),
    ])
    assert session.scalar(select(func.count(Company.id))) == 1


def test_a_failing_posting_does_not_discard_the_batch(session, settings) -> None:
    """One bad row rolls back to its savepoint; its neighbours still persist."""
    # A raw payload that cannot be JSON-encoded fails at flush time. (An
    # over-long title would not work here: SQLite ignores VARCHAR limits.)
    broken = make_raw_job(source_job_id="bad", raw={"unserialisable": object()})
    jobs = [make_raw_job(source_job_id="a"), broken, make_raw_job(source_job_id="c", title="Data Engineer")]

    report = store(session, settings, "testsource", jobs)

    assert report.created == 2
    assert report.failed == 1
    assert session.scalar(select(func.count(Job.id))) == 2


def test_feed_level_duplicate_ids_are_ignored(session, settings) -> None:
    report = store(session, settings, "testsource", [make_raw_job(), make_raw_job()])
    assert report.created == 1


# --- deduplication ------------------------------------------------------


def test_same_role_on_two_boards_is_deduplicated(session, settings) -> None:
    store(session, settings, "boardA", [make_raw_job(source="boardA", source_job_id="a1")])
    report = store(session, settings, "boardB", [make_raw_job(source="boardB", source_job_id="b1")])

    assert report.duplicates == 1
    jobs = list(session.execute(select(Job).order_by(Job.id)).scalars())
    assert jobs[0].duplicate_of_id is None      # first seen stays canonical
    assert jobs[1].duplicate_of_id == jobs[0].id


def test_different_roles_at_one_company_stay_separate(session, settings) -> None:
    store(session, settings, "boardA", [
        make_raw_job(source_job_id="1", title="Senior Backend Engineer"),
        make_raw_job(source_job_id="2", title="Marketing Manager",
                     description_text="Own our brand campaigns and social channels."),
    ])
    assert session.scalar(select(func.count(Job.id)).where(Job.duplicate_of_id.isnot(None))) == 0


def test_same_title_in_different_cities_is_not_a_duplicate(session, settings) -> None:
    """Location is part of the identity key, so Berlin and Bengaluru stay distinct."""
    store(session, settings, "boardA", [
        make_raw_job(source_job_id="1", location="Berlin, Germany"),
        make_raw_job(source_job_id="2", location="Bengaluru, India"),
    ])
    assert session.scalar(select(func.count(Job.id)).where(Job.duplicate_of_id.isnot(None))) == 0


def test_near_duplicate_titles_are_merged(session, settings) -> None:
    body = "We are hiring a backend engineer to build our payments platform in Python. " * 12
    store(session, settings, "boardA", [
        make_raw_job(source="boardA", source_job_id="a1", title="Senior Backend Engineer",
                     location="Remote", description_text=body)
    ])
    report = store(session, settings, "boardB", [
        make_raw_job(source="boardB", source_job_id="b1",
                     title="Senior Backend Engineer (Remote)", location="Remote",
                     description_text=body + " Apply today.")
    ])
    assert report.duplicates == 1


def test_title_similarity_scoring() -> None:
    assert title_similarity("senior backend engineer", "senior backend engineer") == 1.0
    assert title_similarity("senior backend engineer", "senior backend developer") > 0.4
    assert title_similarity("senior backend engineer", "marketing manager") == 0.0


def test_simhash_tracks_textual_similarity() -> None:
    base = "We are hiring a backend engineer to work on distributed payment systems in Python."
    near = base + " Some extra words at the end."
    far = "Looking for a graphic designer to lead brand and illustration work."

    near_distance = hamming_distance(simhash(base), simhash(near))
    far_distance = hamming_distance(simhash(base), simhash(far))

    # A small edit must stay inside the threshold the pipeline actually uses.
    assert near_distance <= SIMHASH_MAX_DISTANCE
    assert far_distance > 25
    assert near_distance < far_distance


def test_simhash_column_survives_postgres() -> None:
    """A 64-bit SimHash needs BIGINT; Postgres INTEGER is only 4 bytes.

    SQLite sizes integers dynamically, so this class of bug is invisible locally
    and fails on the first insert after switching to Postgres.
    """
    from sqlalchemy import create_mock_engine

    from jmi.models import Base
    from jmi.utils import signed_64, simhash

    statements: list[str] = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *a, **kw: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    Base.metadata.create_all(engine, checkfirst=False)

    jobs_ddl = next(s for s in statements if "CREATE TABLE jobs" in s)
    simhash_column = next(line for line in jobs_ddl.splitlines() if "simhash" in line)
    assert "BIGINT" in simhash_column, simhash_column

    value = signed_64(simhash("a realistic job description with a good number of words"))
    assert abs(value) > 2_147_483_647, "test value must actually exceed 32-bit range"


def test_company_key_collapses_legal_suffixes() -> None:
    assert company_key("Acme Inc.") == company_key("ACME, LLC") == company_key("acme")


# --- lifecycle ----------------------------------------------------------


def test_missing_postings_are_retired_not_deleted(session, settings) -> None:
    old = datetime.now(UTC) - timedelta(days=settings.stale_after_days + 5)
    store(session, settings, "testsource", [make_raw_job(source_job_id="1"),
                                            make_raw_job(source_job_id="2", title="Data Engineer")])
    # Age both rows past the staleness window.
    for job in session.execute(select(Job)).scalars():
        job.first_seen_at = old
    session.flush()

    report = store(session, settings, "testsource", [make_raw_job(source_job_id="1")])

    assert report.deactivated == 1
    assert session.scalar(select(func.count(Job.id))) == 2  # nothing deleted
    retired = session.execute(select(Job).where(Job.source_job_id == "2")).scalar_one()
    assert retired.is_active is False


def test_empty_fetch_does_not_retire_everything(session, settings) -> None:
    """A source returning nothing usually means it broke, not that hiring stopped."""
    store(session, settings, "testsource", [make_raw_job()])
    report = store(session, settings, "testsource", [])

    assert report.deactivated == 0
    assert session.scalar(select(func.count(Job.id)).where(Job.is_active.is_(True))) == 1


def test_skill_links_are_replaced_not_duplicated_on_update(session, settings) -> None:
    store(session, settings, "testsource", [make_raw_job()])
    before = session.scalar(select(func.count(JobSkill.id)))

    store(session, settings, "testsource", [
        make_raw_job(description_text="Now we use Go, Kubernetes and Terraform instead.",
                     tags=[])
    ])
    after = list(session.execute(select(JobSkill)).scalars())

    assert before > 0
    assert len(after) == len({link.skill_id for link in after})  # no duplicate links
