"""RemoteOK scraper.

RemoteOK publishes a single JSON feed of currently listed roles. The first
element of that array is a legal/ToS notice rather than a job, which is the kind
of source quirk that silently corrupts a dataset if you do not handle it.

Their API terms ask consumers to credit RemoteOK and link back, so every row we
store keeps its canonical URL and the dashboard renders an attribution line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dateutil import parser as date_parser

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

API_URL = "https://remoteok.com/api"


class RemoteOkScraper(BaseScraper):
    name = "remoteok"
    display_name = "RemoteOK"
    homepage = "https://remoteok.com"
    attribution = "Job data from RemoteOK (https://remoteok.com)"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        result = await client.get(API_URL)
        client.archive("remoteok-feed", result.text)
        payload = result.json()

        if not isinstance(payload, list):
            self.log.error("unexpected payload type %s", type(payload).__name__)
            return []

        jobs: list[RawJob] = []
        for entry in payload:
            if not isinstance(entry, dict) or "legal" in entry:
                continue  # index 0 is the ToS notice, not a posting
            job = self._parse(entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, entry: dict[str, Any]) -> RawJob | None:
        job_id = str(entry.get("id") or entry.get("slug") or "").strip()
        title = (entry.get("position") or "").strip()
        company = (entry.get("company") or "").strip()
        if not job_id or not title or not company:
            return None

        description_html = entry.get("description") or ""
        tags = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]

        return RawJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company=company,
            url=entry.get("url") or f"https://remoteok.com/remote-jobs/{entry.get('slug', job_id)}",
            apply_url=entry.get("apply_url"),
            location=(entry.get("location") or "Remote").strip() or "Remote",
            description_html=description_html,
            description_text=html_to_text(description_html),
            posted_at=self._parse_date(entry),
            tags=tags,
            salary_min=_positive_float(entry.get("salary_min")),
            salary_max=_positive_float(entry.get("salary_max")),
            salary_currency="USD",
            salary_period="year",
            remote_hint=True,
            raw={k: v for k, v in entry.items() if k != "description"},
        )

    @staticmethod
    def _parse_date(entry: dict[str, Any]) -> datetime | None:
        raw_date = entry.get("date")
        if raw_date:
            try:
                return date_parser.isoparse(str(raw_date))
            except (ValueError, TypeError):
                pass
        epoch = entry.get("epoch")
        if epoch:
            try:
                return datetime.fromtimestamp(int(epoch), tz=UTC)
            except (ValueError, TypeError, OSError):
                pass
        return None


def _positive_float(value: Any) -> float | None:
    """RemoteOK uses 0 to mean "not disclosed" rather than omitting the key."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
