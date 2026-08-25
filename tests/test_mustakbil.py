"""Tests for the Pakistan source: JSON-LD parsing and low-wage salary handling."""

from __future__ import annotations

import json

import pytest

from jmi.pipeline.salary import from_structured, parse_salary_text
from jmi.scrapers.mustakbil import (
    MustakbilScraper,
    _extract_job_posting,
    _parse_location,
    _parse_salary,
)

# Trimmed from a real www.mustakbil.com posting.
ONSITE_POSTING = {
    "@context": "https://schema.org/",
    "@type": "JobPosting",
    "title": "Business Development Executive",
    "description": "<b>Job Description</b>: Drive B2B sales across Karachi.",
    "identifier": {"@type": "PropertyValue", "name": "VDigi Pro", "value": "1433795"},
    "datePosted": "2026-08-04T19:45:00+00:00",
    "employmentType": "FULL_TIME",
    "hiringOrganization": {"@type": "Organization", "name": "VDigi Pro"},
    "jobLocation": {
        "@type": "Place",
        "address": {
            "@type": "PostalAddress",
            "addressLocality": "Karachi",
            "addressCountry": "PK",
        },
    },
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "PKR",
        "value": {"@type": "QuantitativeValue", "minValue": 12000, "maxValue": 25000,
                  "unitText": "MONTH"},
    },
    "url": "https://www.mustakbil.com/jobs/job/1433795",
}

REMOTE_POSTING = {
    "@type": "JobPosting",
    "title": "Shopify Developer",
    "description": "Build Shopify storefronts.",
    "employmentType": "INTERN",
    "hiringOrganization": {"@type": "Organization", "name": "Logic Gigs Private Limited"},
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": {"@type": "Country", "name": "Pakistan"},
    "baseSalary": {
        "currency": "PKR",
        "value": {"minValue": 25000, "maxValue": 65000, "unitText": "MONTH"},
    },
    "url": "https://www.mustakbil.com/jobs/job/1418728",
}


def page_with(posting: dict) -> str:
    return (
        "<html><head><script type='application/ld+json'>"
        + json.dumps({"@type": "Organization", "name": "Mustakbil"})
        + "</script><script type=\"application/ld+json\">"
        + json.dumps(posting)
        + "</script></head><body>…</body></html>"
    )


# --- JSON-LD extraction -------------------------------------------------


def test_finds_the_job_posting_among_other_ld_blocks() -> None:
    found = _extract_job_posting(page_with(ONSITE_POSTING))
    assert found is not None
    assert found["title"] == "Business Development Executive"


def test_returns_none_when_no_job_posting_present() -> None:
    assert _extract_job_posting("<html><body>no structured data</body></html>") is None
    assert _extract_job_posting("<script type='application/ld+json'>{bad json</script>") is None


def test_handles_graph_wrapped_json_ld() -> None:
    page = (
        "<script type='application/ld+json'>"
        + json.dumps({"@graph": [{"@type": "WebSite"}, ONSITE_POSTING]})
        + "</script>"
    )
    assert _extract_job_posting(page)["title"] == "Business Development Executive"


# --- location -----------------------------------------------------------


def test_onsite_location_expands_the_country_code() -> None:
    location, remote = _parse_location(ONSITE_POSTING)
    assert location == "Karachi, Pakistan"
    assert remote is None


def test_remote_posting_uses_applicant_location() -> None:
    location, remote = _parse_location(REMOTE_POSTING)
    assert location == "Remote (Pakistan)"
    assert remote is True


# --- salary -------------------------------------------------------------


def test_parses_structured_pkr_monthly_salary() -> None:
    minimum, maximum, currency, period = _parse_salary(ONSITE_POSTING["baseSalary"])
    assert (minimum, maximum, currency, period) == (12000, 25000, "PKR", "month")


def test_missing_salary_is_not_invented() -> None:
    assert _parse_salary(None) == (None, None, None, None)
    assert _parse_salary({"currency": "PKR"}) == (None, None, "PKR", None)


def test_pkr_monthly_converts_to_annual_usd() -> None:
    """PKR 25,000/month is a real wage worth roughly $1,000 a year."""
    salary = from_structured(25000, 65000, "PKR", "month")
    assert salary is not None
    assert 900 < salary.min_usd < 1_300
    assert 2_500 < salary.max_usd < 3_200


def test_low_wage_markets_survive_the_structured_floor() -> None:
    """The US-shaped $8k floor would have discarded every Pakistani salary."""
    assert from_structured(12000, 25000, "PKR", "month") is not None
    # ...but free-text parsing keeps the strict floor, where precision matters.
    assert parse_salary_text("$500") is None


def test_absurd_values_are_still_rejected() -> None:
    assert from_structured(1, 2, "PKR", "year") is None          # under any floor
    assert from_structured(99_000_000, None, "USD", "year") is None  # over the ceiling


# --- end-to-end parse ---------------------------------------------------


def test_builds_a_raw_job_from_a_posting(settings) -> None:
    scraper = MustakbilScraper(settings)
    job = scraper._parse(ONSITE_POSTING, ONSITE_POSTING["url"])

    assert job is not None
    assert job.source == "mustakbil"
    assert job.source_job_id == "1433795"
    assert job.company == "VDigi Pro"
    assert job.location == "Karachi, Pakistan"
    assert job.employment_type == "full_time"
    assert (job.salary_min, job.salary_currency, job.salary_period) == (12000, "PKR", "month")
    assert job.apply_url == ONSITE_POSTING["url"]
    assert "B2B sales" in job.description_text


def test_intern_employment_type_is_mapped(settings) -> None:
    job = MustakbilScraper(settings)._parse(REMOTE_POSTING, REMOTE_POSTING["url"])
    assert job.employment_type == "internship"
    assert job.remote_hint is True


def test_posting_without_a_title_is_skipped(settings) -> None:
    assert MustakbilScraper(settings)._parse({"@type": "JobPosting"}, "https://x/1") is None


@pytest.mark.parametrize(
    ("location", "expected_country"),
    [("Karachi, Pakistan", "Pakistan"), ("Lahore, Pakistan", "Pakistan"),
     ("Islamabad, Pakistan", "Pakistan")],
)
def test_pakistani_cities_resolve_to_pakistan(location: str, expected_country: str) -> None:
    from jmi.pipeline.normalize import parse_location

    country, region = parse_location(location)
    assert country == expected_country
    assert region == "Asia"
