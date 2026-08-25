"""Salary parsing: free text in, comparable annual USD out.

Compensation is the single most valuable field in a job dataset and the worst
formatted. Real strings this has to survive:

    "$170-240K + equity"          "€80,000 — €100,000 per year"
    "$160–180k"                   "up to $200K"
    "150k-200k USD"               "$75/hr"
    "£45,000 - £55,000 DOE"       "₹25,00,000"

Everything is converted to an annual figure in USD so postings from different
countries and pay periods can be compared on one axis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Approximate FX rates. A production deployment would refresh these from an FX
# API on a schedule; they are pinned here so results stay reproducible.
FX_TO_USD: dict[str, float] = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
    "INR": 0.012, "CHF": 1.13, "SEK": 0.095, "NOK": 0.094, "DKK": 0.145,
    "PLN": 0.25, "BRL": 0.18, "SGD": 0.74, "JPY": 0.0067, "NZD": 0.61,
    "ZAR": 0.055, "MXN": 0.050, "ILS": 0.27, "AED": 0.27,
    # South / South-East Asia, Middle East and Africa - needed once the corpus
    # stops being US-centric.
    "PKR": 0.0036, "BDT": 0.0084, "LKR": 0.0033, "NPR": 0.0075,
    "PHP": 0.017, "IDR": 0.000062, "VND": 0.000039, "THB": 0.029,
    "MYR": 0.22, "SAR": 0.267, "QAR": 0.275, "KWD": 3.26, "BHD": 2.65,
    "OMR": 2.60, "JOD": 1.41, "EGP": 0.021, "NGN": 0.00065, "KES": 0.0077,
    "MAD": 0.10, "TRY": 0.029, "RUB": 0.011, "UAH": 0.024, "CZK": 0.043,
    "HUF": 0.0028, "RON": 0.22,
}
FX_RATES_AS_OF = "2026-01"

SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY", "₪": "ILS"}

# Multipliers used to annualise a non-yearly rate.
PERIOD_MULTIPLIER = {
    "year": 1.0,
    "month": 12.0,
    "week": 52.0,
    "day": 260.0,   # working days
    "hour": 2080.0,  # 40h * 52w
}

PERIOD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hour", re.compile(r"(?:/|\bper\s*|\ban\s*)h(?:r|our)\b|\bhourly\b", re.I)),
    ("day", re.compile(r"(?:/|\bper\s*|\ba\s*)day\b|\bdaily\b|\bday rate\b", re.I)),
    ("week", re.compile(r"(?:/|\bper\s*|\ba\s*)w(?:k|eek)\b|\bweekly\b", re.I)),
    ("month", re.compile(r"(?:/|\bper\s*|\ba\s*)mo(?:nth)?\b|\bmonthly\b|\bpcm\b", re.I)),
    ("year", re.compile(r"(?:/|\bper\s*|\ba\s*)(?:yr|year|annum)\b|\bannually\b|\bp\.?a\.?\b", re.I)),
)

CURRENCY_CODE_RE = re.compile(
    r"\b(USD|EUR|GBP|CAD|AUD|INR|CHF|SEK|NOK|DKK|PLN|BRL|SGD|JPY|NZD|ZAR|MXN|ILS|AED)\b", re.I
)

# A money token: optional symbol, digits with , . or Indian grouping, optional k/m.
# The trailing lookahead is load-bearing: without it "$1 minimum payout"
# parses the "m" of "minimum" as a millions suffix and yields $1,000,000.
_AMOUNT = r"(?:[$€£₹¥₪]\s?)?(\d{1,3}(?:[, \s]\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kKmM])?(?![a-zA-Z])"
RANGE_RE = re.compile(
    _AMOUNT + r"\s*(?:-|–|—|~|\bto\b|\bup\s+to\b)\s*" + _AMOUNT, re.UNICODE
)
SINGLE_RE = re.compile(r"[$€£₹¥₪]\s?\d[\d,. ]*\s?[kKmM]?(?![a-zA-Z])|\b\d{2,3}[kK]\b", re.UNICODE)

# Plausibility window for an annual USD salary. Anything outside is a parse
# artefact (a phone number, a headcount, a funding round) rather than pay.
#
# Two floors, because trust differs. A number scraped out of free prose needs a
# high floor to stay precise. A number the source published as a structured
# salary field is trustworthy, and holding it to a US-shaped floor would discard
# every real salary from lower-income markets - PKR 25,000/month is a genuine
# Pakistani wage worth about $1,080 a year.
MIN_ANNUAL_USD = 8_000.0
MIN_STRUCTURED_ANNUAL_USD = 300.0
MAX_ANNUAL_USD = 2_000_000.0

# Keyword anchor for currency-less figures such as "salary: 90k".
_COMP_KEYWORD_RE = re.compile(
    r"\b(salary|salaries|compensation|comp\b|base pay|base salary|pay range|"
    r"total comp|remuneration|package)\b",
    re.I,
)
_K_AMOUNT_RE = re.compile(r"\b\d{2,3}\s?[kK]\b")

# Phrases whose numbers are never compensation.
_NEGATIVE_CONTEXT = re.compile(
    r"\b(raised|raise|funding|funded|backed|pre-?seed|seed round|valuation|"
    r"revenue|arr|mrr|gmv|run rate|p&l|series\s+[a-e]|customers|users|"
    r"employees|headcount|budget|market cap|investment|savings|fund|payout|"
    r"book of business|bootstrapped)\b",
    re.I,
)


@dataclass(slots=True)
class SalaryRange:
    min_usd: float | None
    max_usd: float | None
    currency: str
    period: str
    origin: str  # "structured" (source-provided) or "parsed" (from text)

    @property
    def is_empty(self) -> bool:
        return self.min_usd is None and self.max_usd is None


def detect_currency(text: str) -> str:
    code = CURRENCY_CODE_RE.search(text)
    if code:
        return code.group(1).upper()
    for symbol, currency in SYMBOL_CURRENCY.items():
        if symbol in text:
            # "CA$" / "A$" disambiguation for the dollar sign.
            if symbol == "$":
                lowered = text.lower()
                if "ca$" in lowered or "cad" in lowered:
                    return "CAD"
                if "a$" in lowered or "aud" in lowered:
                    return "AUD"
            return currency
    return "USD"


def detect_period(text: str) -> str:
    for period, pattern in PERIOD_PATTERNS:
        if pattern.search(text):
            return period
    return "year"


def _to_number(digits: str, suffix: str | None) -> float | None:
    cleaned = digits.replace(",", "").replace(" ", "").replace(" ", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if suffix:
        value *= 1_000 if suffix.lower() == "k" else 1_000_000
    return value


def _is_millions(suffix: str | None) -> bool:
    return bool(suffix) and suffix.lower() == "m"


def annualise(amount: float, period: str, currency: str) -> float:
    rate = FX_TO_USD.get(currency.upper(), 1.0)
    return amount * PERIOD_MULTIPLIER.get(period, 1.0) * rate


def parse_salary_text(text: str | None, window: int = 220) -> SalaryRange | None:
    """Extract the first plausible compensation range from free text."""
    if not text:
        return None

    snippet = _salary_snippet(text, window)
    if snippet is None:
        return None
    if _NEGATIVE_CONTEXT.search(snippet):
        return None

    currency = detect_currency(snippet)
    period = detect_period(snippet)

    match = RANGE_RE.search(snippet)
    if match:
        # "$2M pre-seed", "$1M+/year revenue": compensation is never written in
        # millions in a job posting, so an M suffix means this is not pay.
        if _is_millions(match.group(2)) or _is_millions(match.group(4)):
            return None
        low = _to_number(match.group(1), match.group(2))
        high = _to_number(match.group(3), match.group(4))
        # "$160-180k": the k on the upper bound applies to the lower one too.
        if low is not None and high is not None:
            if match.group(2) is None and match.group(4) is not None and low < high / 100:
                low = _to_number(match.group(1), match.group(4))
            if low is not None and high is not None and low > high:
                low, high = high, low
        return _finalise(low, high, currency, period)

    singles = SINGLE_RE.findall(snippet)
    if singles:
        single_match = re.search(_AMOUNT, singles[0])
        if single_match:
            if _is_millions(single_match.group(2)):
                return None
            value = _to_number(single_match.group(1), single_match.group(2))
            if "up to" in snippet.lower():
                return _finalise(None, value, currency, period)
            return _finalise(value, value, currency, period)
    return None


def _salary_snippet(text: str, window: int) -> str | None:
    """Narrow to the neighbourhood of a money marker before parsing.

    Scanning a 6,000 character job description whole guarantees false positives;
    the numbers that matter sit right next to a currency symbol, a currency code,
    or a compensation keyword ("salary: 90k", common on Hacker News).
    """
    marker = re.search(r"[$€£₹¥₪]|\b(?:USD|EUR|GBP|CAD|AUD|INR)\b", text, re.I)
    if marker is not None:
        start = max(0, marker.start() - 60)
        return text[start : marker.start() + window]

    keyword = _COMP_KEYWORD_RE.search(text)
    if keyword is None:
        return None
    tail = text[keyword.start() : keyword.start() + window]
    # Only trust a keyword anchor when a k-denominated figure sits next to it.
    return tail if _K_AMOUNT_RE.search(tail) else None


def _finalise(
    low: float | None, high: float | None, currency: str, period: str
) -> SalaryRange | None:
    low_usd = annualise(low, period, currency) if low is not None else None
    high_usd = annualise(high, period, currency) if high is not None else None

    def valid(value: float | None) -> float | None:
        if value is None:
            return None
        return value if MIN_ANNUAL_USD <= value <= MAX_ANNUAL_USD else None

    low_usd, high_usd = valid(low_usd), valid(high_usd)
    if low_usd is None and high_usd is None:
        return None
    return SalaryRange(
        min_usd=round(low_usd, 2) if low_usd else None,
        max_usd=round(high_usd, 2) if high_usd else None,
        currency=currency.upper(),
        period=period,
        origin="parsed",
    )


def from_structured(
    minimum: float | None,
    maximum: float | None,
    currency: str | None = "USD",
    period: str | None = "year",
) -> SalaryRange | None:
    """Wrap salary fields a source already provided as numbers."""
    if minimum is None and maximum is None:
        return None
    currency = (currency or "USD").upper()
    period = period or "year"
    low = annualise(minimum, period, currency) if minimum else None
    high = annualise(maximum, period, currency) if maximum else None
    if low and high and low > high:
        low, high = high, low
    if low is not None and not (MIN_STRUCTURED_ANNUAL_USD <= low <= MAX_ANNUAL_USD):
        low = None
    if high is not None and not (MIN_STRUCTURED_ANNUAL_USD <= high <= MAX_ANNUAL_USD):
        high = None
    if low is None and high is None:
        return None
    return SalaryRange(
        min_usd=round(low, 2) if low else None,
        max_usd=round(high, 2) if high else None,
        currency=currency,
        period=period,
        origin="structured",
    )
