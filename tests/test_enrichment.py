"""Tests for skill extraction, normalisation and the Hacker News header parser."""

from __future__ import annotations

import pytest

from jmi.pipeline.normalize import (
    classify_employment_type,
    classify_seniority,
    detect_remote,
    normalize_title,
    parse_location,
)
from jmi.pipeline.skills import extract_skills
from jmi.scrapers.hackernews import parse_hn_header

# --- skill extraction ---------------------------------------------------


def slugs(description, title=None, tags=None, company=None):
    return {m.slug for m in extract_skills(description, title, tags, company)}


def test_extracts_stack_from_description() -> None:
    found = slugs("We use React, TypeScript, Node.js, PostgreSQL and Docker on AWS.")
    assert {"react", "typescript", "nodejs", "postgresql", "docker", "aws"} <= found


def test_java_does_not_match_inside_javascript() -> None:
    assert slugs("We are a JavaScript shop.") == {"javascript"}
    assert "java" in slugs("We use Java 17 and Spring Boot.")


def test_c_variants_are_distinguished() -> None:
    found = slugs("Build with C/C++ on embedded Linux. C# also welcome.")
    assert {"clang", "cpp", "csharp", "linux"} <= found


def test_ambiguous_words_need_a_qualifier() -> None:
    # "go" and "rust" as ordinary English must not register as languages.
    assert slugs("Come rust-proof our go-to-market strategy.") == set()
    assert "golang" in slugs("You will write Go services and gRPC APIs.", "Go Developer")


def test_r_requires_programming_context() -> None:
    assert "rlang" in slugs("Analysis in R programming and Python.")
    assert "rlang" not in slugs("Our HR team is growing.")


def test_tags_outrank_description_mentions() -> None:
    matches = {m.slug: m for m in extract_skills("We mention python once.", tags=["python"])}
    assert matches["python"].matched_via == "tag"
    assert matches["python"].confidence == 1.0


def test_employer_name_is_not_counted_as_a_skill() -> None:
    """Scraping Figma's own board must not make Figma the top skill."""
    text = "At Figma we build design tools. Figma is used by millions."
    assert "figma" in slugs(text)
    assert "figma" not in slugs(text, company="Figma")


# --- seniority ----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer", "senior"),
        ("Sr. Backend Developer", "senior"),
        ("Staff Engineer", "staff"),
        ("Principal Architect", "principal"),
        ("Engineering Manager", "manager"),
        ("Director of Engineering", "director"),
        ("VP of Product", "executive"),
        ("Software Engineering Intern", "intern"),
        ("Junior Developer", "junior"),
        ("Software Engineer II", "mid"),
    ],
)
def test_seniority_classification(title: str, expected: str) -> None:
    assert classify_seniority(title) == expected


def test_management_titles_outrank_senior() -> None:
    """'Senior Engineering Manager' is a management role, not a senior IC one."""
    assert classify_seniority("Senior Engineering Manager") == "manager"


def test_seniority_is_none_when_unstated() -> None:
    assert classify_seniority("Software Engineer") is None


# --- remote / location --------------------------------------------------


def test_remote_detection_prefers_explicit_location_text() -> None:
    assert detect_remote("Remote (US)") is True
    assert detect_remote("San Francisco, CA — onsite") is False


def test_source_hint_used_when_location_is_silent() -> None:
    assert detect_remote("Berlin", hint=True) is True
    assert detect_remote("Berlin", hint=False) is False


def test_hybrid_is_not_remote() -> None:
    assert detect_remote("London, Hybrid 3 days/week") is False


@pytest.mark.parametrize(
    ("raw", "country", "region"),
    [
        ("Berlin, Germany", "Germany", "Europe"),
        ("San Francisco, CA", "United States", "North America"),
        ("Bengaluru, India", "India", "Asia"),
        ("London", "United Kingdom", "Europe"),
        ("Anywhere in the World", None, "Worldwide"),
    ],
)
def test_location_parsing(raw: str, country: str | None, region: str | None) -> None:
    assert parse_location(raw) == (country, region)


def test_multi_location_resolves_on_the_first_entry() -> None:
    assert parse_location("Berlin, Germany • New York, NY")[0] == "Germany"


def test_employment_type_detection() -> None:
    assert classify_employment_type("Contract Backend Developer") == "contract"
    assert classify_employment_type("Marketing Intern") == "internship"
    assert classify_employment_type("Engineer", "This is a full-time role.") == "full_time"


def test_title_normalisation_strips_noise() -> None:
    assert normalize_title("Senior Engineer (Remote)") == normalize_title("Senior Engineer")
    assert normalize_title("Backend Developer (m/f/d)") == "backend developer"


# --- Hacker News header parsing ----------------------------------------


def test_hn_header_full_form() -> None:
    parsed = parse_hn_header("Spade | Data Engineer | ONSITE NYC or REMOTE (US) | $170-240K")
    assert parsed["company"] == "Spade"
    assert parsed["title"] == "Data Engineer"
    assert "$170-240K" in parsed["salary_text"]
    assert parsed["remote_hint"] is True


def test_hn_header_without_company() -> None:
    """Some posts open with the role; the first segment is then not a company."""
    parsed = parse_hn_header("Senior Software Engineer, Frontend | New York, NY | $160k-$210k")
    assert parsed["title"] == "Senior Software Engineer, Frontend"
    assert parsed["company"] is None


def test_hn_header_detects_employment_type() -> None:
    parsed = parse_hn_header("Olli | Founding Engineer | REMOTE (US) | Contract | Equity")
    assert parsed["employment_type"] == "contract"


def test_hn_header_handles_empty_input() -> None:
    parsed = parse_hn_header("")
    assert parsed["company"] is None and parsed["title"] is None
