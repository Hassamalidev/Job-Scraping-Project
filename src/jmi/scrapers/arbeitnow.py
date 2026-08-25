"""Arbeitnow scraper - European, largely German-speaking postings.

Adds geographic balance. Without it the corpus is US and remote-heavy, which
quietly skews every salary aggregate and demand ranking toward one market.

The feed is a plain paginated JSON board with a ``links.next`` cursor, so the
crawl follows the site's own pagination rather than guessing page numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

API_URL = "https://www.arbeitnow.com/api/job-board-api"

JOB_TYPES = {
    "full time": "full_time", "full-time": "full_time", "vollzeit": "full_time",
    "part time": "part_time", "part-time": "part_time", "teilzeit": "part_time",
    "contract": "contract", "freelance": "contract",
    "internship": "internship", "praktikum": "internship",
}


class ArbeitnowScraper(BaseScraper):
    name = "arbeitnow"
    display_name = "Arbeitnow (Europe)"
    homepage = "https://www.arbeitnow.com"
    attribution = "European listings from Arbeitnow (https://www.arbeitnow.com)"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        jobs: list[RawJob] = []
        url: str | None = API_URL

        for page in range(self.settings.arbeitnow_pages):
            if not url:
                break
            result = await client.get(url)
            if page == 0:
                client.archive("arbeitnow-page1", result.text)
            payload = result.json()

            entries = payload.get("data") or []
            if not entries:
                break
            for entry in entries:
                job = self._parse(entry)
                if job is not None:
                    jobs.append(job)

            url = (payload.get("links") or {}).get("next")

        self.log.info("collected %d postings", len(jobs))
        return jobs

    def _parse(self, entry: dict[str, Any]) -> RawJob | None:
        title = (entry.get("title") or "").strip()
        company = (entry.get("company_name") or "").strip()
        slug = entry.get("slug")
        if not title or not company or not slug:
            return None

        description_html = entry.get("description") or ""
        job_types = [str(t).strip().lower() for t in (entry.get("job_types") or [])]
        employment = next((JOB_TYPES[t] for t in job_types if t in JOB_TYPES), None)

        return RawJob(
            source=self.name,
            source_job_id=str(slug)[:200],
            title=title[:300],
            company=company[:200],
            url=entry.get("url") or f"https://www.arbeitnow.com/jobs/companies/{slug}",
            location=(entry.get("location") or "").strip() or None,
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=_epoch(entry.get("created_at")),
            tags=[str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()][:12],
            employment_type=employment,
            remote_hint=bool(entry.get("remote")),
            raw={"job_types": job_types, "slug": slug},
        )


def _epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
