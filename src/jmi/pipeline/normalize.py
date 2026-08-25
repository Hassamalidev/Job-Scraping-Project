"""Turning source-specific text into comparable dimensions.

Four things get derived here, all of them from noisy input:

* **seniority** - resolved by priority, not by first match. "Senior Engineering
  Manager" is a manager role, not a senior IC one, so management titles are
  tested before ``senior``.
* **employment type** - full-time / part-time / contract / internship.
* **remote** - reconciles the source's own hint against text markers, because
  boards that call themselves "remote" still list hybrid and onsite roles.
* **location** - a raw string like "Berlin, Germany • New York, NY" becomes a
  country and a coarse region for grouping.
"""

from __future__ import annotations

import re

from ..utils import normalize_whitespace

SENIORITY_ORDER = (
    "executive", "director", "manager", "principal", "staff",
    "lead", "senior", "mid", "junior", "intern",
)

# Order matters: the first rule that matches wins.
SENIORITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("executive", re.compile(r"\b(chief|c[teofi]o\b|vp\b|vice president|svp|evp|head of|founder)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("manager", re.compile(r"\b(manager|mgr|people lead|engineering lead)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\bstaff\b", re.I)),
    ("lead", re.compile(r"\b(lead|leader|tech lead|team lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|snr|iii|iv)\b", re.I)),
    ("intern", re.compile(r"\b(intern|internship|co-?op|trainee|apprentice)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|grad\b)\b", re.I)),
    ("mid", re.compile(r"\b(mid[- ]level|intermediate|\bii\b)\b", re.I)),
)

EMPLOYMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internship", re.compile(r"\b(intern|internship|co-?op)\b", re.I)),
    ("contract", re.compile(r"\b(contract|contractor|freelance|fractional|consultant|b2b|1099)\b", re.I)),
    ("part_time", re.compile(r"\bpart[- ]time\b", re.I)),
    ("full_time", re.compile(r"\bfull[- ]time\b|\bpermanent\b|\bfte\b", re.I)),
)

REMOTE_POSITIVE = re.compile(
    r"\b(remote|work from home|wfh|anywhere|worldwide|distributed team|fully remote|remote-first)\b",
    re.I,
)
REMOTE_NEGATIVE = re.compile(
    r"\b(on-?site|in-?office|in person|hybrid|must relocate|relocation required|"
    r"no remote|not remote|onsite only)\b",
    re.I,
)

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "wales": "United Kingdom",
    "united kingdom": "United Kingdom", "great britain": "United Kingdom",
    "canada": "Canada", "germany": "Germany", "deutschland": "Germany",
    "france": "France", "spain": "Spain", "portugal": "Portugal",
    "italy": "Italy", "netherlands": "Netherlands", "holland": "Netherlands",
    "belgium": "Belgium", "switzerland": "Switzerland", "austria": "Austria",
    "poland": "Poland", "sweden": "Sweden", "norway": "Norway",
    "denmark": "Denmark", "finland": "Finland", "ireland": "Ireland",
    "india": "India", "china": "China", "japan": "Japan", "singapore": "Singapore",
    "australia": "Australia", "new zealand": "New Zealand", "brazil": "Brazil",
    "mexico": "Mexico", "argentina": "Argentina", "colombia": "Colombia",
    "chile": "Chile", "israel": "Israel", "south africa": "South Africa",
    "nigeria": "Nigeria", "kenya": "Kenya", "egypt": "Egypt", "uae": "UAE",
    "united arab emirates": "UAE", "pakistan": "Pakistan", "bangladesh": "Bangladesh",
    "philippines": "Philippines", "indonesia": "Indonesia", "vietnam": "Vietnam",
    "thailand": "Thailand", "malaysia": "Malaysia", "south korea": "South Korea",
    "korea": "South Korea", "turkey": "Turkey", "ukraine": "Ukraine",
    "romania": "Romania", "czech republic": "Czechia", "czechia": "Czechia",
    "hungary": "Hungary", "greece": "Greece", "serbia": "Serbia",
    "bulgaria": "Bulgaria", "croatia": "Croatia", "estonia": "Estonia",
    "lithuania": "Lithuania", "latvia": "Latvia",
}

CITY_TO_COUNTRY = {
    "san francisco": "United States", "sf": "United States", "new york": "United States",
    "nyc": "United States", "seattle": "United States", "austin": "United States",
    "boston": "United States", "chicago": "United States", "denver": "United States",
    "los angeles": "United States", "atlanta": "United States", "miami": "United States",
    "portland": "United States", "san diego": "United States", "dallas": "United States",
    "houston": "United States", "philadelphia": "United States", "phoenix": "United States",
    "london": "United Kingdom", "manchester": "United Kingdom", "edinburgh": "United Kingdom",
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany", "cologne": "Germany",
    "paris": "France", "lyon": "France", "amsterdam": "Netherlands",
    "rotterdam": "Netherlands", "madrid": "Spain", "barcelona": "Spain",
    "lisbon": "Portugal", "porto": "Portugal", "milan": "Italy", "rome": "Italy",
    "dublin": "Ireland", "zurich": "Switzerland", "geneva": "Switzerland",
    "vienna": "Austria", "stockholm": "Sweden", "oslo": "Norway",
    "copenhagen": "Denmark", "helsinki": "Finland", "warsaw": "Poland",
    "krakow": "Poland", "prague": "Czechia", "budapest": "Hungary",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada",
    "ottawa": "Canada", "bangalore": "India", "bengaluru": "India",
    "mumbai": "India", "delhi": "India", "hyderabad": "India", "pune": "India",
    "chennai": "India", "tokyo": "Japan", "osaka": "Japan", "beijing": "China",
    "shanghai": "China", "shenzhen": "China", "hong kong": "Hong Kong",
    "sydney": "Australia", "melbourne": "Australia", "auckland": "New Zealand",
    "sao paulo": "Brazil", "são paulo": "Brazil", "rio de janeiro": "Brazil",
    "mexico city": "Mexico", "buenos aires": "Argentina", "bogota": "Colombia",
    "tel aviv": "Israel", "dubai": "UAE", "abu dhabi": "UAE",
    "cape town": "South Africa", "johannesburg": "South Africa",
    "lagos": "Nigeria", "nairobi": "Kenya", "cairo": "Egypt",
    "karachi": "Pakistan", "lahore": "Pakistan", "islamabad": "Pakistan",
    "manila": "Philippines", "jakarta": "Indonesia", "singapore": "Singapore",
    "seoul": "South Korea", "istanbul": "Turkey", "kyiv": "Ukraine",
    "bucharest": "Romania", "belgrade": "Serbia",
}

REGION_BY_COUNTRY = {
    "United States": "North America", "Canada": "North America", "Mexico": "North America",
    "United Kingdom": "Europe", "Germany": "Europe", "France": "Europe",
    "Spain": "Europe", "Portugal": "Europe", "Italy": "Europe",
    "Netherlands": "Europe", "Belgium": "Europe", "Switzerland": "Europe",
    "Austria": "Europe", "Poland": "Europe", "Sweden": "Europe", "Norway": "Europe",
    "Denmark": "Europe", "Finland": "Europe", "Ireland": "Europe",
    "Czechia": "Europe", "Hungary": "Europe", "Greece": "Europe",
    "Romania": "Europe", "Serbia": "Europe", "Bulgaria": "Europe",
    "Croatia": "Europe", "Estonia": "Europe", "Lithuania": "Europe",
    "Latvia": "Europe", "Ukraine": "Europe", "Turkey": "Europe",
    "India": "Asia", "China": "Asia", "Japan": "Asia", "Singapore": "Asia",
    "South Korea": "Asia", "Hong Kong": "Asia", "Pakistan": "Asia",
    "Bangladesh": "Asia", "Philippines": "Asia", "Indonesia": "Asia",
    "Vietnam": "Asia", "Thailand": "Asia", "Malaysia": "Asia",
    "Israel": "Middle East", "UAE": "Middle East",
    "Australia": "Oceania", "New Zealand": "Oceania",
    "Brazil": "South America", "Argentina": "South America",
    "Colombia": "South America", "Chile": "South America",
    "South Africa": "Africa", "Nigeria": "Africa", "Kenya": "Africa", "Egypt": "Africa",
}

# Noise stripped from titles before they are used as a dedupe key.
_TITLE_NOISE = re.compile(
    r"\((?:remote|hybrid|onsite|on-site|contract|full[- ]time|part[- ]time|m/f/d|m/w/d|"
    r"h/f|f/m/d|all genders|us|usa|uk|emea|apac)\)|"
    r"\b(?:m/f/d|m/w/d|f/m/x|all genders)\b|"
    r"[-–—|,]\s*(?:remote|hybrid|onsite|full[- ]time|part[- ]time)\s*$",
    re.I,
)
_TITLE_PUNCT = re.compile(r"[^a-z0-9+#/. ]+")


def classify_seniority(title: str, description: str | None = None) -> str | None:
    """Resolve seniority from the title, falling back to the description."""
    for level, pattern in SENIORITY_RULES:
        if pattern.search(title or ""):
            return level
    if description:
        head = description[:600]  # requirements usually lead the description
        for level, pattern in SENIORITY_RULES:
            if level in ("senior", "junior", "intern", "staff", "principal") and pattern.search(head):
                return level
    return None


def classify_employment_type(
    title: str, description: str | None = None, hint: str | None = None
) -> str | None:
    if hint:
        return hint
    haystack = f"{title or ''} {(description or '')[:800]}"
    for kind, pattern in EMPLOYMENT_RULES:
        if pattern.search(haystack):
            return kind
    return None


def detect_remote(
    location: str | None, description: str | None = None, hint: bool | None = None
) -> bool:
    """Reconcile the source hint with what the posting actually says."""
    location = location or ""
    if REMOTE_NEGATIVE.search(location) and not REMOTE_POSITIVE.search(location):
        return False
    if REMOTE_POSITIVE.search(location):
        return True
    if hint is not None:
        return hint
    head = (description or "")[:1200]
    return bool(REMOTE_POSITIVE.search(head) and not REMOTE_NEGATIVE.search(head))


def parse_location(location_raw: str | None) -> tuple[str | None, str | None]:
    """Best-effort ``(country, region)`` from a free-form location string."""
    if not location_raw:
        return None, None

    text = normalize_whitespace(location_raw).lower()
    # Multi-location strings ("Berlin, Germany • New York, NY") resolve on the first entry.
    primary = re.split(r"[•|;/]| or ", text)[0].strip()
    parts = [p.strip(" .") for p in primary.split(",") if p.strip(" .")]

    for part in reversed(parts):
        if part in COUNTRY_ALIASES:
            country = COUNTRY_ALIASES[part]
            return country, REGION_BY_COUNTRY.get(country)
        if part in US_STATES or (len(part) == 2 and part in US_STATES):
            return "United States", "North America"

    for part in parts:
        if part in CITY_TO_COUNTRY:
            country = CITY_TO_COUNTRY[part]
            return country, REGION_BY_COUNTRY.get(country)

    for city, country in CITY_TO_COUNTRY.items():
        if re.search(rf"\b{re.escape(city)}\b", primary):
            return country, REGION_BY_COUNTRY.get(country)

    # A country named inside a phrase: "Remote (Pakistan)", "Remote - Germany".
    # Only names of 5+ characters, because short aliases like "us" and "uk"
    # match ordinary prose ("join us") far too easily.
    for alias, country in COUNTRY_ALIASES.items():
        if len(alias) >= 5 and re.search(rf"\b{re.escape(alias)}\b", text):
            return country, REGION_BY_COUNTRY.get(country)

    if re.search(r"\b(anywhere|worldwide|global|remote)\b", text):
        return None, "Worldwide"
    return None, None


def normalize_title(title: str) -> str:
    """Canonical form of a title, used as part of the duplicate key."""
    cleaned = _TITLE_NOISE.sub(" ", (title or "").lower())
    cleaned = _TITLE_PUNCT.sub(" ", cleaned)
    return " ".join(cleaned.split())[:200]
