"""Tests for the advisory layer: match scoring, skill gap, momentum, percentiles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from jmi.analytics.advisor import (
    match_scores,
    salary_distribution,
    skill_gap,
    skill_momentum,
)
from jmi.analytics.queries import JobFilters
from jmi.models import Job
from jmi.pipeline.ingest import store_raw_jobs

from .conftest import make_raw_job


def seed(session, settings, jobs):
    store_raw_jobs(session, "testsource", jobs, settings)
    session.flush()


@pytest.fixture
def corpus(session, settings):
    """A small corpus with known skill sets, so expectations are exact."""
    seed(session, settings, [
        # python + postgresql + docker  (from the default fixture description)
        make_raw_job(source_job_id="1", title="Backend Engineer"),
        # python + react + typescript
        make_raw_job(source_job_id="2", title="Fullstack Engineer",
                     description_text="We use Python, React and TypeScript.",
                     tags=[]),
        # react + typescript
        make_raw_job(source_job_id="3", title="Frontend Engineer",
                     description_text="React and TypeScript only.", tags=[]),
        # kubernetes + docker + aws
        make_raw_job(source_job_id="4", title="Platform Engineer",
                     description_text="Kubernetes, Docker and AWS work.", tags=[]),
    ])
    return session


# --- match scoring ------------------------------------------------------


def test_match_score_is_share_of_required_skills(corpus) -> None:
    job = corpus.execute(select(Job).where(Job.source_job_id == "3")).scalar_one()
    scores = match_scores(corpus, [job.id], ["react"])

    result = scores[job.id]
    assert result["required"] == 2          # react, typescript
    assert result["matched"] == 1
    assert result["score"] == pytest.approx(0.5)
    assert "TypeScript" in result["missing"]


def test_full_match_scores_one(corpus) -> None:
    job = corpus.execute(select(Job).where(Job.source_job_id == "3")).scalar_one()
    scores = match_scores(corpus, [job.id], ["react", "typescript"])
    assert scores[job.id]["score"] == pytest.approx(1.0)
    assert scores[job.id]["missing"] == []


def test_empty_profile_matches_nothing(corpus) -> None:
    job = corpus.execute(select(Job).where(Job.source_job_id == "3")).scalar_one()
    assert match_scores(corpus, [job.id], [])[job.id]["score"] == 0.0


def test_match_scores_handles_no_job_ids(corpus) -> None:
    assert match_scores(corpus, [], ["python"]) == {}


def test_substantial_match_outranks_a_trivial_one(corpus, settings) -> None:
    """A job where only one skill was detected must not outrank a real match.

    Raw coverage scores "1 of 1" at a perfect 100%, which put the weakest
    evidence in the corpus at the top of the list. The smoothing prior fixes it.
    """
    from jmi.analytics.queries import search_jobs

    seed(corpus, settings, [
        make_raw_job(source_job_id="thin", title="Python Scripter", company="Thin Co",
                     description_text="Python.", tags=[]),
        make_raw_job(source_job_id="rich", title="Platform Lead", company="Rich Co",
                     description_text="Python, Docker, Kubernetes, AWS and PostgreSQL.", tags=[]),
    ])

    have = ["python", "docker", "kubernetes", "aws", "postgresql"]
    jobs, _ = search_jobs(corpus, JobFilters(), limit=10, sort="match", have=have)
    order = [job.source_job_id for job in jobs]

    assert order.index("rich") < order.index("thin")


# --- skill gap ----------------------------------------------------------


def test_gap_recommends_the_skill_that_unlocks_most(corpus) -> None:
    """With React alone, TypeScript is the single best thing to learn."""
    gap = skill_gap(corpus, ["react"], JobFilters(), threshold=1.0)

    top = gap["recommendations"][0]
    assert top["slug"] == "typescript"
    assert top["unlocks"] >= 1


def test_gap_counts_what_you_already_qualify_for(corpus) -> None:
    gap = skill_gap(corpus, ["react", "typescript"], JobFilters(), threshold=1.0)
    # Job 3 needs exactly react + typescript, so it is already covered.
    assert gap["qualified_now"] >= 1
    assert gap["profile_size"] == 2


def test_gap_never_recommends_a_skill_you_have(corpus) -> None:
    gap = skill_gap(corpus, ["react", "typescript"], JobFilters())
    assert "react" not in {r["slug"] for r in gap["recommendations"]}
    assert "typescript" not in {r["slug"] for r in gap["recommendations"]}


def test_empty_profile_still_returns_advice(corpus) -> None:
    gap = skill_gap(corpus, [], JobFilters(), threshold=0.5)
    assert gap["analysed_jobs"] > 0
    assert gap["qualified_now"] == 0


def test_lower_threshold_qualifies_you_for_more(corpus) -> None:
    strict = skill_gap(corpus, ["react"], JobFilters(), threshold=1.0)
    loose = skill_gap(corpus, ["react"], JobFilters(), threshold=0.5)
    assert loose["qualified_now"] >= strict["qualified_now"]


def test_gap_respects_filters(corpus) -> None:
    """Filtering to a country with no postings leaves nothing to analyse."""
    gap = skill_gap(corpus, ["python"], JobFilters(country="Atlantis"))
    assert gap["analysed_jobs"] == 0
    assert gap["recommendations"] == []


# --- momentum -----------------------------------------------------------


def test_momentum_compares_two_windows(session, settings) -> None:
    now = datetime.now(UTC)
    # Distinct companies, otherwise these collapse into one canonical row -
    # which is the deduplicator working correctly, not a momentum bug.
    seed(session, settings, [
        make_raw_job(source_job_id=f"recent-{i}", title="Go Engineer", company=f"Recent Co {i}",
                     description_text="We write Go services with gRPC.", tags=[],
                     posted_at=now - timedelta(days=5))
        for i in range(6)
    ])
    seed(session, settings, [
        make_raw_job(source_job_id=f"old-{i}", title="Perl Engineer", company=f"Legacy Co {i}",
                     description_text="Legacy Perl maintenance.", tags=[],
                     posted_at=now - timedelta(days=45))
        for i in range(6)
    ])

    momentum = skill_momentum(session, window_days=30, min_sample=3)
    rising = {r["slug"] for r in momentum["rising"]}
    falling = {r["slug"] for r in momentum["falling"]}

    assert "golang" in rising
    assert "perl" in falling


def test_momentum_ignores_thin_samples(session, settings) -> None:
    seed(session, settings, [make_raw_job(source_job_id="lonely", title="Elixir Dev",
                                          description_text="Elixir work.", tags=[])])
    momentum = skill_momentum(session, window_days=30, min_sample=50)
    assert momentum["rising"] == []


# --- salary distribution -------------------------------------------------


def test_percentiles_are_ordered(corpus) -> None:
    dist = salary_distribution(corpus, JobFilters())
    p = dist["percentiles"]
    assert p["p10"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p90"]
    assert dist["count"] > 0


def test_histogram_covers_every_posting(corpus) -> None:
    dist = salary_distribution(corpus, JobFilters(), buckets=5)
    assert len(dist["buckets"]) == 5
    assert sum(b["count"] for b in dist["buckets"]) == dist["count"]


def test_distribution_is_empty_without_salaries(session, settings) -> None:
    seed(session, settings, [make_raw_job(source_job_id="nopay",
                                          description_text="No pay mentioned here.")])
    dist = salary_distribution(session, JobFilters(with_salary_only=True))
    assert dist["count"] == 0
    assert dist["buckets"] == []
