"""Parser tests for the Lever, Himalayas and Arbeitnow sources.

All fixtures are trimmed from real API responses, so a shape change upstream
shows up here rather than as silently missing data in the corpus.
"""

from __future__ import annotations

import pytest

from jmi.pipeline.salary import from_structured
from jmi.scrapers.arbeitnow import ArbeitnowScraper
from jmi.scrapers.himalayas import HimalayasScraper
from jmi.scrapers.lever import LeverScraper

# --- Lever --------------------------------------------------------------

LEVER_POSTING = {
    "id": "ac978161-6f46-4f6b-ad9e-a258e642751c",
    "text": "Administrative Business Partner",
    "categories": {
        "commitment": "Full-time",
        "location": "London, United Kingdom",
        "team": "Administrative",
        "allLocations": ["London, United Kingdom"],
    },
    "country": "GB",
    "workplaceType": "hybrid",
    "createdAt": 1711403416463,  # milliseconds, not seconds
    "description": "<div>Our team of Administrative Business Partners uses Excel daily.</div>",
    "hostedUrl": "https://jobs.lever.co/palantir/ac978161",
    "applyUrl": "https://jobs.lever.co/palantir/ac978161/apply",
    "salaryRange": None,
}


def test_lever_parses_a_posting(settings) -> None:
    job = LeverScraper(settings)._parse(LEVER_POSTING, "palantir")

    assert job is not None
    assert job.title == "Administrative Business Partner"
    assert job.company == "Palantir"
    assert job.location == "London, United Kingdom"
    assert job.employment_type == "full_time"
    assert job.apply_url.endswith("/apply")
    assert job.tags == ["Administrative"]


def test_lever_timestamps_are_milliseconds(settings) -> None:
    """Reading Lever's createdAt as seconds would date every posting to 1970."""
    job = LeverScraper(settings)._parse(LEVER_POSTING, "palantir")
    assert job.posted_at.year == 2024


def test_lever_hybrid_is_not_remote(settings) -> None:
    job = LeverScraper(settings)._parse(LEVER_POSTING, "palantir")
    assert job.remote_hint is False

    remote = dict(LEVER_POSTING, workplaceType="remote")
    assert LeverScraper(settings)._parse(remote, "palantir").remote_hint is True


def test_lever_reads_salary_when_present(settings) -> None:
    entry = dict(LEVER_POSTING, salaryRange={
        "min": 120000, "max": 160000, "currency": "USD", "interval": "per-year-salary"})
    job = LeverScraper(settings)._parse(entry, "palantir")
    assert (job.salary_min, job.salary_max, job.salary_period) == (120000, 160000, "year")


def test_lever_skips_a_posting_without_a_title(settings) -> None:
    assert LeverScraper(settings)._parse({"id": "x"}, "palantir") is None


# --- Himalayas ----------------------------------------------------------

HIMALAYAS_POSTING = {
    "title": "Senior Data Engineer",
    "companyName": "Codest",
    "companySlug": "codest",
    "employmentType": "Full Time",
    "minSalary": 20000,
    "maxSalary": 28000,
    "currency": "PLN",
    "salaryPeriod": "monthly",
    "seniority": ["Senior"],
    "locationRestrictions": ["Poland"],
    "categories": ["Data-Engineering", "Python"],
    "pubDate": 1787641715,
    "description": "<p>Build pipelines with Python and Spark.</p>",
    "guid": "https://himalayas.app/companies/codest/jobs/senior-data-engineer-123",
    "applicationLink": "https://himalayas.app/companies/codest/jobs/senior-data-engineer-123",
}


def test_himalayas_parses_a_posting(settings) -> None:
    job = HimalayasScraper(settings)._parse(HIMALAYAS_POSTING)

    assert job is not None
    assert job.company == "Codest"
    assert job.location == "Poland"
    assert job.remote_hint is True          # the board is remote-only
    assert job.employment_type == "full_time"
    assert "Python" in job.tags


def test_himalayas_keeps_the_local_currency_and_period(settings) -> None:
    job = HimalayasScraper(settings)._parse(HIMALAYAS_POSTING)
    assert (job.salary_min, job.salary_currency, job.salary_period) == (20000, "PLN", "month")


def test_himalayas_monthly_pln_annualises_sensibly(settings) -> None:
    job = HimalayasScraper(settings)._parse(HIMALAYAS_POSTING)
    salary = from_structured(job.salary_min, job.salary_max, job.salary_currency, job.salary_period)
    assert salary is not None
    # 20k PLN/month is roughly $60k a year, not $20k and not $240k.
    assert 45_000 < salary.min_usd < 75_000


def test_himalayas_annual_period_is_mapped(settings) -> None:
    entry = dict(HIMALAYAS_POSTING, salaryPeriod="annual", currency="USD",
                 minSalary=155000, maxSalary=170000)
    job = HimalayasScraper(settings)._parse(entry)
    assert job.salary_period == "year"


def test_himalayas_tolerates_a_missing_salary(settings) -> None:
    entry = dict(HIMALAYAS_POSTING)
    entry.pop("minSalary")
    entry.pop("maxSalary")
    job = HimalayasScraper(settings)._parse(entry)
    assert job.salary_min is None and job.salary_max is None


def test_himalayas_skips_incomplete_entries(settings) -> None:
    assert HimalayasScraper(settings)._parse({"title": "No company"}) is None


# --- Arbeitnow ----------------------------------------------------------

ARBEITNOW_POSTING = {
    "slug": "senior-platform-engineer-kafka-berlin-105281",
    "company_name": "wolt",
    "title": "Senior Platform Engineer, Event Streaming Platform (Kafka)",
    "description": "<p>Work on Kafka and Kubernetes in Berlin.</p>",
    "remote": False,
    "url": "https://www.arbeitnow.com/jobs/companies/wolt/senior-platform-engineer",
    "tags": ["Core Engineering"],
    "job_types": ["full time"],
    "location": "Berlin",
    "created_at": 1787637326,
}


def test_arbeitnow_parses_a_posting(settings) -> None:
    job = ArbeitnowScraper(settings)._parse(ARBEITNOW_POSTING)

    assert job is not None
    assert job.company == "wolt"
    assert job.location == "Berlin"
    assert job.remote_hint is False
    assert job.employment_type == "full_time"
    assert job.posted_at.year >= 2026


def test_arbeitnow_maps_german_job_types(settings) -> None:
    entry = dict(ARBEITNOW_POSTING, job_types=["Vollzeit"])
    assert ArbeitnowScraper(settings)._parse(entry).employment_type == "full_time"


def test_arbeitnow_flags_remote(settings) -> None:
    entry = dict(ARBEITNOW_POSTING, remote=True)
    assert ArbeitnowScraper(settings)._parse(entry).remote_hint is True


def test_arbeitnow_skips_entries_without_a_slug(settings) -> None:
    entry = dict(ARBEITNOW_POSTING)
    entry.pop("slug")
    assert ArbeitnowScraper(settings)._parse(entry) is None


# --- registry -----------------------------------------------------------


@pytest.mark.parametrize("name", ["lever", "himalayas", "arbeitnow"])
def test_new_sources_are_registered(name: str) -> None:
    from jmi.scrapers.registry import SCRAPERS, get_scraper

    assert name in SCRAPERS
    scraper = get_scraper(name)
    assert scraper.attribution, "every source must credit its origin"
    assert scraper.homepage.startswith("https://")
