"""Himalayas scraper - remote roles with first-class salary data.

Himalayas publishes a cursor-paginated JSON API covering roughly 100,000 remote
postings. Two things make it valuable here:

* salary arrives as real numbers with a currency and a period, rather than
  buried in prose, which is rare and raises the corpus disclosure rate;
* it carries its own seniority label, giving an independent check on the
  classifier in ``pipeline/normalize.py``.

The API is deep, so the crawl is capped by ``JMI_HIMALAYAS_MAX_JOBS`` and walks
pages in order rather than trying to mirror the whole board.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

API_URL = "https://himalayas.app/jobs/api"
PAGE_SIZE = 100

EMPLOYMENT_TYPES = {
    "full time": "full_time", "fulltime": "full_time", "full-time": "full_time",
    "part time": "part_time", "part-time": "part_time",
    "contract": "contract", "freelance": "contract", "temporary": "contract",
    "internship": "internship", "intern": "internship",
}

PAY_PERIODS = {
    "annual": "year", "yearly": "year", "year": "year",
    "monthly": "month", "month": "month",
    "weekly": "week", "week": "week",
    "daily": "day", "day": "day",
    "hourly": "hour", "hour": "hour",
}


class HimalayasScraper(BaseScraper):
    name = "himalayas"
    display_name = "Himalayas"
    homepage = "https://himalayas.app"
    attribution = "Remote roles from Himalayas (https://himalayas.app)"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        jobs: list[RawJob] = []
        offset = 0

        while len(jobs) < self.settings.himalayas_max_jobs:
            payload = await client.get_json(
                API_URL, params={"limit": PAGE_SIZE, "offset": offset}
            )
            batch = payload.get("jobs") or []
            if not batch:
                break
            if offset == 0:
                self.log.info("board reports %s postings", payload.get("totalCount"))

            for entry in batch:
                job = self._parse(entry)
                if job is not None:
                    jobs.append(job)

            offset += PAGE_SIZE
            if offset >= int(payload.get("totalCount") or 0):
                break

        return jobs[: self.settings.himalayas_max_jobs]

    def _parse(self, entry: dict[str, Any]) -> RawJob | None:
        title = (entry.get("title") or "").strip()
        company = (entry.get("companyName") or "").strip()
        guid = entry.get("guid") or entry.get("applicationLink")
        if not title or not company or not guid:
            return None

        description_html = entry.get("description") or entry.get("excerpt") or ""
        location = ", ".join(_as_list(entry.get("locationRestrictions"))) or "Remote"

        return RawJob(
            source=self.name,
            source_job_id=str(guid).rsplit("/", 1)[-1][:200],
            title=title[:300],
            company=company[:200],
            url=str(guid),
            apply_url=entry.get("applicationLink"),
            location=location,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=_epoch(entry.get("pubDate")),
            tags=_as_list(entry.get("categories"))[:12],
            employment_type=EMPLOYMENT_TYPES.get(
                str(entry.get("employmentType") or "").strip().lower()
            ),
            remote_hint=True,  # the board is remote-only by definition
            salary_min=_number(entry.get("minSalary")),
            salary_max=_number(entry.get("maxSalary")),
            salary_currency=(entry.get("currency") or "USD").upper(),
            salary_period=PAY_PERIODS.get(
                str(entry.get("salaryPeriod") or "").strip().lower(), "year"
            ),
            raw={
                "company_slug": entry.get("companySlug"),
                "seniority_label": _as_list(entry.get("seniority")),
                "parent_categories": _as_list(entry.get("parentCategories")),
                "expiry_date": entry.get("expiryDate"),
            },
        )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
