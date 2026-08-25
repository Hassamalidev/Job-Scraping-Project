"""Greenhouse job board scraper.

Greenhouse hosts the careers page for thousands of companies behind one public,
documented JSON API (``boards-api.greenhouse.io``). Pointing the crawler at a
list of board slugs gives high-quality, first-party postings - the useful
counterweight to the noisy aggregator feeds.

Each board is fetched independently so one bad slug cannot fail the whole run.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from dateutil import parser as date_parser

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseScraper(BaseScraper):
    name = "greenhouse"
    display_name = "Greenhouse boards"
    homepage = "https://www.greenhouse.io"
    attribution = "Postings from company-owned Greenhouse job boards"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        jobs: list[RawJob] = []
        for slug in self.settings.greenhouse_boards:
            try:
                jobs.extend(await self._fetch_board(client, slug))
            except Exception as exc:  # one broken board must not kill the run
                self.log.warning("board %s failed: %s", slug, exc)
        return jobs

    async def _fetch_board(self, client: HttpClient, slug: str) -> list[RawJob]:
        result = await client.get(BOARD_URL.format(slug=slug), params={"content": "true"})
        client.archive(f"greenhouse-{slug}", result.text)
        payload = result.json()

        entries = payload.get("jobs") or []
        limit = self.settings.greenhouse_max_per_board
        if len(entries) > limit:
            self.log.info("board %s: capping %d postings at %d", slug, len(entries), limit)
            entries = entries[:limit]

        jobs = []
        for entry in entries:
            job = self._parse(entry, slug)
            if job is not None:
                jobs.append(job)
        self.log.info("board %s: %d postings", slug, len(jobs))
        return jobs

    def _parse(self, entry: dict[str, Any], slug: str) -> RawJob | None:
        job_id = entry.get("id")
        title = (entry.get("title") or "").strip()
        if not job_id or not title:
            return None

        # Greenhouse double-encodes the description body.
        content_html = html.unescape(entry.get("content") or "")
        location = ((entry.get("location") or {}).get("name") or "").strip()
        departments = [
            d.get("name", "") for d in (entry.get("departments") or []) if d.get("name")
        ]
        offices = [o.get("name", "") for o in (entry.get("offices") or []) if o.get("name")]
        company = (entry.get("company_name") or slug).strip()

        return RawJob(
            source=self.name,
            source_job_id=f"{slug}:{job_id}",
            title=title,
            company=company,
            url=entry.get("absolute_url") or BOARD_URL.format(slug=slug),
            location=location or None,
            description_html=content_html,
            description_text=html_to_text(content_html),
            posted_at=_parse_date(entry.get("first_published") or entry.get("updated_at")),
            tags=departments,
            remote_hint=_looks_remote(location, offices),
            raw={
                "board": slug,
                "greenhouse_id": job_id,
                "departments": departments,
                "offices": offices,
                "updated_at": entry.get("updated_at"),
            },
        )


def _looks_remote(location: str, offices: list[str]) -> bool | None:
    haystack = " ".join([location, *offices]).lower()
    if not haystack.strip():
        return None
    return any(word in haystack for word in ("remote", "anywhere", "distributed"))


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
