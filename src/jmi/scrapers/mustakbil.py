"""Mustakbil scraper - Pakistan job listings.

A fifth source with a fifth shape. Where the others hand over a feed, this one
publishes a **sitemap** and embeds schema.org ``JobPosting`` JSON-LD in each job
page. So the crawl is:

    sitemap index -> job sitemaps -> newest job URLs -> one page each -> JSON-LD

Two things make it worth having beyond geography:

* it is the politest possible crawl - the site's own sitemap says what to fetch
  and when each entry last changed, so nothing is guessed or brute-forced;
* salaries arrive as ``PKR ... /MONTH``, which exercises the currency and pay
  period conversion that a US-only corpus never touches.

One request per posting is unavoidable here, so the crawl is deliberately capped
(``JMI_MUSTAKBIL_MAX_JOBS``) and runs behind the same per-domain rate limiter as
every other source.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from dateutil import parser as date_parser

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

SITEMAP_INDEX = "https://sitemaps.mustakbil.com/sitemaps"
PAKISTAN_HOST = "www.mustakbil.com"

_LD_JSON_RE = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I
)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.S)
_URLSET_ENTRY_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>(.*?)</lastmod>", re.S)

EMPLOYMENT_TYPES = {
    "FULL_TIME": "full_time",
    "PART_TIME": "part_time",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "VOLUNTEER": "internship",
    "PER_DIEM": "contract",
    "OTHER": None,
}

# schema.org publishes pay periods in caps; our salary module speaks lowercase.
PAY_PERIODS = {
    "HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year",
}

# ISO country codes seen in addressCountry, expanded so the location parser -
# which works on names, not codes - can resolve them. Kept local because two
# letter codes collide with US state abbreviations in the global tables.
COUNTRY_CODES = {
    "PK": "Pakistan", "AE": "UAE", "SA": "Saudi Arabia", "QA": "Qatar",
    "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman", "IN": "India",
    "BD": "Bangladesh", "LK": "Sri Lanka", "US": "United States",
    "GB": "United Kingdom", "CA": "Canada", "AU": "Australia", "DE": "Germany",
    "MY": "Malaysia", "SG": "Singapore", "PH": "Philippines", "ID": "Indonesia",
    "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya", "EG": "Egypt",
    "TR": "Turkey", "CN": "China", "JP": "Japan",
}


class MustakbilScraper(BaseScraper):
    name = "mustakbil"
    display_name = "Mustakbil (Pakistan)"
    homepage = "https://www.mustakbil.com"
    attribution = "Pakistan job listings from Mustakbil.com (https://www.mustakbil.com)"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        urls = await self._recent_job_urls(client)
        if not urls:
            self.log.warning("no job URLs found in sitemap")
            return []

        limit = self.settings.mustakbil_max_jobs
        self.log.info("sitemap listed %d Pakistan postings; fetching newest %d", len(urls), min(limit, len(urls)))

        jobs: list[RawJob] = []
        for url in urls[:limit]:
            try:
                job = await self._fetch_job(client, url)
            except Exception as exc:  # one dead posting must not end the crawl
                self.log.debug("skipping %s: %s", url, exc)
                continue
            if job is not None:
                jobs.append(job)
        return jobs

    async def _recent_job_urls(self, client: HttpClient) -> list[str]:
        """Newest-first job URLs for the Pakistan site, taken from the sitemap."""
        index = await client.get(SITEMAP_INDEX)
        job_sitemaps = [
            loc.strip() for loc in _LOC_RE.findall(index.text) if "/jobs/" in loc
        ]
        if not job_sitemaps:
            return []

        entries: list[tuple[str, str]] = []
        for sitemap_url in job_sitemaps[: self.settings.mustakbil_sitemap_pages]:
            result = await client.get(sitemap_url)
            client.archive(f"mustakbil-sitemap-{len(entries)}", result.text, extension="xml")
            for block in _URLSET_ENTRY_RE.findall(result.text):
                loc_match = _LOC_RE.search(block)
                if loc_match is None:
                    continue
                loc = loc_match.group(1).strip()
                # Regional subdomains (ae., sa., us. ...) are other countries.
                if urlparse(loc).netloc != PAKISTAN_HOST:
                    continue
                lastmod = _LASTMOD_RE.search(block)
                entries.append((loc, lastmod.group(1).strip() if lastmod else ""))

        entries.sort(key=lambda pair: pair[1], reverse=True)
        return [loc for loc, _ in entries]

    async def _fetch_job(self, client: HttpClient, url: str) -> RawJob | None:
        result = await client.get(url)
        posting = _extract_job_posting(result.text)
        if posting is None:
            return None
        return self._parse(posting, url)

    def _parse(self, posting: dict[str, Any], url: str) -> RawJob | None:
        title = (posting.get("title") or "").strip()
        if not title:
            return None

        organisation = posting.get("hiringOrganization") or {}
        company = (organisation.get("name") or "").strip() or "Unknown"

        description_html = posting.get("description") or ""
        location, is_remote = _parse_location(posting)
        minimum, maximum, currency, period = _parse_salary(posting.get("baseSalary"))

        identifier = posting.get("identifier") or {}
        job_id = str(identifier.get("value") or url.rstrip("/").rsplit("/", 1)[-1])

        return RawJob(
            source=self.name,
            source_job_id=job_id,
            title=title[:300],
            company=company[:200],
            url=posting.get("url") or url,
            apply_url=posting.get("url") or url,
            location=location,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=_parse_date(posting.get("datePosted")),
            employment_type=EMPLOYMENT_TYPES.get(
                str(posting.get("employmentType") or "").upper()
            ),
            remote_hint=is_remote,
            salary_min=minimum,
            salary_max=maximum,
            salary_currency=currency,
            salary_period=period,
            raw={
                "valid_through": posting.get("validThrough"),
                "job_location_type": posting.get("jobLocationType"),
                "direct_apply": posting.get("directApply"),
                "organisation_logo": organisation.get("logo"),
            },
        )


def _extract_job_posting(html: str) -> dict[str, Any] | None:
    """Pull the JobPosting object out of a page's JSON-LD blocks."""
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for candidate in _iter_nodes(data):
            if candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _iter_nodes(data: Any):
    """Walk JSON-LD, which may be an object, a list, or an @graph wrapper."""
    if isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_nodes(data["@graph"])
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from _iter_nodes(item)


def _parse_location(posting: dict[str, Any]) -> tuple[str | None, bool | None]:
    remote = str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE"

    job_location = posting.get("jobLocation")
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None

    parts: list[str] = []
    if isinstance(job_location, dict):
        address = job_location.get("address") or {}
        if isinstance(address, dict):
            for key in ("addressLocality", "addressRegion"):
                value = address.get(key)
                if value and str(value).strip():
                    parts.append(str(value).strip())
                    break
            country = str(address.get("addressCountry") or "").strip()
            if country:
                parts.append(COUNTRY_CODES.get(country.upper(), country))

    if remote and not parts:
        # Remote postings state who may apply rather than where the desk is.
        requirement = posting.get("applicantLocationRequirements") or {}
        if isinstance(requirement, list):
            requirement = requirement[0] if requirement else {}
        if isinstance(requirement, dict) and requirement.get("name"):
            parts.append(f"Remote ({requirement['name']})")
        else:
            parts.append("Remote")

    return (", ".join(parts) or None), (True if remote else None)


def _parse_salary(
    base_salary: Any,
) -> tuple[float | None, float | None, str | None, str | None]:
    if not isinstance(base_salary, dict):
        return None, None, None, None

    currency = (base_salary.get("currency") or "").upper() or None
    value = base_salary.get("value")
    if not isinstance(value, dict):
        return None, None, currency, None

    period = PAY_PERIODS.get(str(value.get("unitText") or "").upper(), "year")
    minimum = _to_float(value.get("minValue"))
    maximum = _to_float(value.get("maxValue"))
    if minimum is None and maximum is None:
        single = _to_float(value.get("value"))
        minimum = maximum = single
    return minimum, maximum, currency, period


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.isoparse(str(value))
    except (ValueError, TypeError):
        return None
