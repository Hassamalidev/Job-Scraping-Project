"""Hacker News "Ask HN: Who is hiring?" scraper.

This is the messy source, and the interesting one. There is no schema at all -
each top-level comment is free text written by a human. The community convention
is a pipe-delimited header:

    Company | Role | Location | Salary | extra notes

but in practice the field order varies, segments are missing, and plenty of
posts open with the role and never name the company. So instead of assuming
positions we *classify* each segment (salary? location? employment type? role?)
and fall back to deriving the company from the first link's domain.

Only top-level comments are postings; replies (``parent_id != story_id``) are
discussion and are dropped.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from dateutil import parser as date_parser

from ..utils import html_to_text, normalize_whitespace
from .base import BaseScraper, HttpClient, RawJob

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
PAGE_SIZE = 100
MAX_PAGES = 12

ROLE_WORDS = (
    "engineer", "developer", "programmer", "scientist", "designer", "architect",
    "manager", "analyst", "researcher", "devops", "sre", "founding", "lead",
    "director", "intern", "consultant", "administrator", "specialist", "head of",
    "cto", "vp ", "product", "recruiter", "marketer", "writer", "support",
)
LOCATION_WORDS = (
    "remote", "onsite", "on-site", "hybrid", "anywhere", "worldwide", "usa",
    "us)", "uk", "eu", "europe", "canada", "india", "germany", "berlin", "london",
    "nyc", "new york", "san francisco", "sf", "seattle", "austin", "boston",
    "toronto", "amsterdam", "paris", "tokyo", "singapore", "australia", "brazil",
)
EMPLOYMENT_WORDS = {
    "full-time": "full_time", "full time": "full_time", "fulltime": "full_time",
    "part-time": "part_time", "part time": "part_time",
    "contract": "contract", "contractor": "contract", "freelance": "contract",
    "fractional": "contract", "intern": "internship", "internship": "internship",
}

_SALARY_RE = re.compile(
    r"(?:[$€£₹]|\bUSD\b|\bEUR\b|\bGBP\b)\s?\d[\d,.]*\s?[kK]?"
    r"(?:\s?[-–—to]+\s?[$€£₹]?\s?\d[\d,.]*\s?[kK]?)?",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_TAG_RE = re.compile(r"<[^>]+>")
_PARA_SPLIT_RE = re.compile(r"</?p>", re.IGNORECASE)

# Domains that appear in postings but never identify the hiring company.
_GENERIC_DOMAINS = {
    "news.ycombinator.com", "github.com", "linkedin.com", "twitter.com", "x.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "notion.so",
    "docs.google.com", "youtube.com", "medium.com", "gmail.com",
}


class HackerNewsScraper(BaseScraper):
    name = "hackernews"
    display_name = 'HN "Who is hiring?"'
    homepage = "https://news.ycombinator.com"
    attribution = "Posts from Hacker News via the Algolia HN Search API"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        threads = await self._recent_threads(client)
        if not threads:
            self.log.warning("no 'Who is hiring?' threads found")
            return []

        jobs: list[RawJob] = []
        for story_id, story_title in threads:
            self.log.info("reading thread %s (%s)", story_id, story_title)
            jobs.extend(await self._thread_postings(client, story_id, story_title))
        return jobs

    async def _recent_threads(self, client: HttpClient) -> list[tuple[str, str]]:
        payload = await client.get_json(
            SEARCH_URL, params={"tags": "story,author_whoishiring", "hitsPerPage": 20}
        )
        threads = []
        for hit in payload.get("hits", []):
            title = hit.get("title") or ""
            if "who is hiring" in title.lower():
                threads.append((str(hit["objectID"]), title))
            if len(threads) >= self.settings.hn_threads:
                break
        return threads

    async def _thread_postings(
        self, client: HttpClient, story_id: str, story_title: str
    ) -> list[RawJob]:
        jobs: list[RawJob] = []
        page = 0
        while page < MAX_PAGES:
            payload = await client.get_json(
                SEARCH_URL,
                params={
                    "tags": f"comment,story_{story_id}",
                    "hitsPerPage": PAGE_SIZE,
                    "page": page,
                },
            )
            hits = payload.get("hits", [])
            if not hits:
                break
            if page == 0:
                client.archive(f"hn-{story_id}-page0", str(payload))

            for hit in hits:
                # Replies are discussion, not job posts.
                if str(hit.get("parent_id")) != str(hit.get("story_id")):
                    continue
                job = self._parse_comment(hit, story_title)
                if job is not None:
                    jobs.append(job)

            page += 1
            if page >= int(payload.get("nbPages") or 0):
                break
        return jobs

    def _parse_comment(self, hit: dict[str, Any], story_title: str) -> RawJob | None:
        raw_html = hit.get("comment_text")
        if not raw_html:
            return None  # deleted or flagged

        text = html_to_text(raw_html)
        if len(text) < 40:
            return None  # too short to be a real posting

        header = self._header_line(raw_html)
        parsed = parse_hn_header(header)

        company = parsed["company"] or self._company_from_links(raw_html) or "Unknown"
        title = parsed["title"] or _first_role_like(text) or "Unspecified role"

        return RawJob(
            source=self.name,
            source_job_id=str(hit["objectID"]),
            title=title[:200],
            company=company[:120],
            url=ITEM_URL.format(hit["objectID"]),
            apply_url=self._first_link(raw_html),
            location=parsed["location"],
            description_html=raw_html,
            description_text=text,
            posted_at=_parse_date(hit.get("created_at")),
            employment_type=parsed["employment_type"],
            remote_hint=parsed["remote_hint"],
            raw={
                "author": hit.get("author"),
                "story_id": hit.get("story_id"),
                "story_title": story_title,
                "header": header,
                "salary_text": parsed["salary_text"],
            },
        )

    @staticmethod
    def _header_line(raw_html: str) -> str:
        """The first paragraph carries the pipe-delimited summary."""
        first_block = _PARA_SPLIT_RE.split(raw_html)[0]
        cleaned = normalize_whitespace(html.unescape(_TAG_RE.sub(" ", first_block)))
        first_line = cleaned.split("\n")[0]
        return first_line[:400]

    @staticmethod
    def _first_link(raw_html: str) -> str | None:
        match = _URL_RE.search(html.unescape(raw_html))
        return match.group(0) if match else None

    @staticmethod
    def _company_from_links(raw_html: str) -> str | None:
        """Derive a company name from the first non-generic link domain."""
        for match in _URL_RE.finditer(html.unescape(raw_html)):
            host = urlparse(match.group(0)).netloc.lower().removeprefix("www.")
            if not host or host in _GENERIC_DOMAINS:
                continue
            label = host.split(".")[0]
            if len(label) < 2:
                continue
            return label.replace("-", " ").title()
        return None


def parse_hn_header(header: str) -> dict[str, Any]:
    """Classify each ``|``-delimited segment of an HN hiring header.

    Pure function so the messiest logic in the project is directly unit tested.
    """
    result: dict[str, Any] = {
        "company": None,
        "title": None,
        "location": None,
        "employment_type": None,
        "remote_hint": None,
        "salary_text": None,
    }
    segments = [s.strip(" .-–—") for s in header.split("|")]
    segments = [s for s in segments if s]
    if not segments:
        return result

    unclaimed: list[str] = []
    for index, segment in enumerate(segments):
        lowered = segment.lower()

        salary_match = _SALARY_RE.search(segment)
        if salary_match and result["salary_text"] is None:
            result["salary_text"] = segment[:120]
            continue

        for keyword, value in EMPLOYMENT_WORDS.items():
            if keyword in lowered:
                result["employment_type"] = result["employment_type"] or value
                break

        if any(word in lowered for word in ("remote", "anywhere", "worldwide")):
            result["remote_hint"] = True
        elif any(word in lowered for word in ("onsite", "on-site", "in-office", "hybrid")):
            result["remote_hint"] = False if result["remote_hint"] is None else result["remote_hint"]

        is_location = any(word in lowered for word in LOCATION_WORDS)
        is_role = any(word in lowered for word in ROLE_WORDS)

        if is_location and not is_role and result["location"] is None:
            result["location"] = segment[:150]
        elif is_role and result["title"] is None:
            result["title"] = segment[:200]
        elif index == 0 and result["company"] is None and not is_location:
            result["company"] = segment[:120]
        else:
            unclaimed.append(segment)

    # A leading segment that was neither role nor location is the company name.
    if result["company"] is None and unclaimed:
        result["company"] = unclaimed[0][:120]
    if result["title"] is None and unclaimed:
        candidates = [s for s in unclaimed if s != result["company"]]
        if candidates:
            result["title"] = candidates[0][:200]
    return result


def _first_role_like(text: str) -> str | None:
    for line in text.split("\n"):
        stripped = line.strip()
        if 8 < len(stripped) < 120 and any(w in stripped.lower() for w in ROLE_WORDS):
            return stripped
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
