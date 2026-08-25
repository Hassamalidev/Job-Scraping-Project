"""Lever job board scraper.

The second of the two big applicant tracking systems, and the counterpart to
``greenhouse.py``. Between them they cover most first-party company careers
pages, which is the highest quality end of the corpus: no aggregator middleman,
no duplicated reposts, and the description is whatever the employer actually
wrote.

Lever's public API differs from Greenhouse in every detail that matters -
``text`` rather than ``title``, a nested ``categories`` object for location and
commitment, millisecond timestamps, and an explicit ``workplaceType`` - so it
needs its own parser rather than a shared one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

BOARD_URL = "https://api.lever.co/v0/postings/{slug}"

COMMITMENTS = {
    "full-time": "full_time", "full time": "full_time",
    "part-time": "part_time", "part time": "part_time",
    "contract": "contract", "temporary": "contract", "consultant": "contract",
    "intern": "internship", "internship": "internship",
}

PAY_PERIODS = {
    "per-year-salary": "year", "per-year": "year", "yearly": "year", "annual": "year",
    "per-month-salary": "month", "monthly": "month",
    "per-hour-wage": "hour", "hourly": "hour",
    "per-day": "day", "per-week": "week",
}


class LeverScraper(BaseScraper):
    name = "lever"
    display_name = "Lever boards"
    homepage = "https://www.lever.co"
    attribution = "Postings from company-owned Lever job boards"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        jobs: list[RawJob] = []
        for slug in self.settings.lever_boards:
            try:
                jobs.extend(await self._fetch_board(client, slug))
            except Exception as exc:  # one bad board must not fail the run
                self.log.warning("board %s failed: %s", slug, exc)
        return jobs

    async def _fetch_board(self, client: HttpClient, slug: str) -> list[RawJob]:
        result = await client.get(BOARD_URL.format(slug=slug), params={"mode": "json"})
        client.archive(f"lever-{slug}", result.text)
        payload = result.json()

        if not isinstance(payload, list):
            self.log.warning("board %s returned %s, not a list", slug, type(payload).__name__)
            return []

        limit = self.settings.lever_max_per_board
        if len(payload) > limit:
            self.log.info("board %s: capping %d postings at %d", slug, len(payload), limit)
            payload = payload[:limit]

        jobs = []
        for entry in payload:
            job = self._parse(entry, slug)
            if job is not None:
                jobs.append(job)
        self.log.info("board %s: %d postings", slug, len(jobs))
        return jobs

    def _parse(self, entry: dict[str, Any], slug: str) -> RawJob | None:
        title = (entry.get("text") or "").strip()
        job_id = entry.get("id")
        if not title or not job_id:
            return None

        categories = entry.get("categories") or {}
        location = (categories.get("location") or "").strip() or None
        commitment = str(categories.get("commitment") or "").strip().lower()
        workplace = str(entry.get("workplaceType") or "").strip().lower()

        description_html = entry.get("description") or entry.get("descriptionBody") or ""
        minimum, maximum, currency, period = _parse_salary(entry.get("salaryRange"))

        return RawJob(
            source=self.name,
            source_job_id=f"{slug}:{job_id}",
            title=title[:300],
            # Lever does not name the employer in the payload; the board is the employer.
            company=slug.replace("-", " ").title()[:200],
            url=entry.get("hostedUrl") or BOARD_URL.format(slug=slug),
            apply_url=entry.get("applyUrl"),
            location=location,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=_ms_epoch(entry.get("createdAt")),
            tags=[t for t in [categories.get("team"), categories.get("department")] if t],
            employment_type=COMMITMENTS.get(commitment),
            remote_hint=True if workplace == "remote" else (False if workplace else None),
            salary_min=minimum,
            salary_max=maximum,
            salary_currency=currency,
            salary_period=period,
            raw={
                "board": slug,
                "lever_id": job_id,
                "team": categories.get("team"),
                "all_locations": categories.get("allLocations"),
                "workplace_type": workplace,
                "country": entry.get("country"),
            },
        )


def _parse_salary(
    salary_range: Any,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Lever exposes salaryRange only when the employer filled it in."""
    if not isinstance(salary_range, dict):
        return None, None, None, None
    currency = (salary_range.get("currency") or "USD").upper()
    period = PAY_PERIODS.get(str(salary_range.get("interval") or "").lower(), "year")
    return (
        _number(salary_range.get("min")),
        _number(salary_range.get("max")),
        currency,
        period,
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ms_epoch(value: Any) -> datetime | None:
    """Lever timestamps are milliseconds since the epoch."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
