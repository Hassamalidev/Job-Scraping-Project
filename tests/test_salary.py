"""Salary parsing tests.

The `REJECT` cases are not hypothetical - each one is a string that was found in
the live corpus producing a nonsense salary before it was fixed.
"""

from __future__ import annotations

import pytest

from jmi.pipeline.salary import from_structured, parse_salary_text


@pytest.mark.parametrize(
    ("text", "expected_min", "expected_max"),
    [
        ("$170-240K + equity", 170_000, 240_000),
        ("$160k-$210k + equity", 160_000, 210_000),
        ("$192k - $261k", 192_000, 261_000),
        ("$160–180k", 160_000, 180_000),  # en dash, k only on the upper bound
        ("Compensation: $130,000 to $175,000 annually", 130_000, 175_000),
        ("150k-200k USD", 150_000, 200_000),
        ("pay range 100k-140k", 100_000, 140_000),
        ("Salary 90k", 90_000, 90_000),
    ],
)
def test_parses_common_ranges(text: str, expected_min: float, expected_max: float) -> None:
    salary = parse_salary_text(text)
    assert salary is not None, f"failed to parse {text!r}"
    assert salary.min_usd == pytest.approx(expected_min, rel=0.01)
    assert salary.max_usd == pytest.approx(expected_max, rel=0.01)


def test_converts_hourly_to_annual() -> None:
    salary = parse_salary_text("$75/hr")
    assert salary is not None
    assert salary.period == "hour"
    assert salary.min_usd == pytest.approx(75 * 2080)


def test_converts_monthly_to_annual() -> None:
    salary = parse_salary_text("$8,500/month")
    assert salary is not None
    assert salary.min_usd == pytest.approx(8_500 * 12)


def test_converts_foreign_currency_to_usd() -> None:
    salary = parse_salary_text("£45,000 - £55,000 DOE")
    assert salary is not None
    assert salary.currency == "GBP"
    assert salary.min_usd > 45_000  # GBP -> USD is an increase


def test_up_to_yields_only_an_upper_bound() -> None:
    salary = parse_salary_text("up to $200K")
    assert salary is not None
    assert salary.min_usd is None
    assert salary.max_usd == pytest.approx(200_000)


@pytest.mark.parametrize(
    "text",
    [
        "$2M pre-seed backed by founders of Ramp",       # funding round
        "manage this $1M+/year BPO business unit's P&L",  # business revenue
        "$1 minimum payout threshold sent to wallets",    # "m" of "minimum"
        "We raised $50M Series B funding",
        "serving 50k customers monthly",
        "our team of 200 engineers ships daily",
        "no compensation information here",
    ],
)
def test_rejects_numbers_that_are_not_pay(text: str) -> None:
    assert parse_salary_text(text) is None


def test_word_boundaries_do_not_block_real_salaries() -> None:
    """'arr' inside 'arrays' must not trip the funding-language filter."""
    salary = parse_salary_text("arrays and data structures, $120k-$150k salary")
    assert salary is not None
    assert salary.min_usd == pytest.approx(120_000)


def test_implausible_values_are_dropped() -> None:
    assert parse_salary_text("$3 per year") is None          # below the floor
    assert parse_salary_text("$99,000,000 per year") is None  # above the ceiling


def test_structured_salary_is_flagged_as_such() -> None:
    salary = from_structured(120_000, 150_000, "USD", "year")
    assert salary is not None
    assert salary.origin == "structured"
    assert (salary.min_usd, salary.max_usd) == (120_000, 150_000)


def test_structured_salary_swaps_inverted_bounds() -> None:
    salary = from_structured(150_000, 120_000, "USD", "year")
    assert salary is not None
    assert salary.min_usd == 120_000
    assert salary.max_usd == 150_000


def test_structured_zero_means_undisclosed() -> None:
    assert from_structured(None, None) is None
