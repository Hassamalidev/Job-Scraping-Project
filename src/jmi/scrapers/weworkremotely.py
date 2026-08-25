"""We Work Remotely scraper (RSS).

WWR exposes a public RSS feed. Two quirks drive the parsing here:

* ``<title>`` packs company and role into one string as ``Company: Role``.
* the feed carries non-standard sibling tags (``region``, ``category``,
  ``type``) that describe the posting better than the description does.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from xml.etree import ElementTree

from dateutil import parser as date_parser

from ..utils import html_to_text
from .base import BaseScraper, HttpClient, RawJob

FEED_URL = "https://weworkremotely.com/remote-jobs.rss"

EMPLOYMENT_TYPES = {
    "full-time": "full_time",
    "full time": "full_time",
    "contract": "contract",
    "part-time": "part_time",
    "part time": "part_time",
    "internship": "internship",
    "temporary": "contract",
}

_HEADQUARTERS_RE = re.compile(r"Headquarters:\s*([^\n<]{2,80})", re.IGNORECASE)


class WeWorkRemotelyScraper(BaseScraper):
    name = "weworkremotely"
    display_name = "We Work Remotely"
    homepage = "https://weworkremotely.com"
    attribution = "Job data from We Work Remotely (https://weworkremotely.com)"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        result = await client.get(FEED_URL)
        client.archive("wwr-feed", result.text, extension="xml")

        try:
            root = ElementTree.fromstring(result.text.encode("utf-8"))
        except ElementTree.ParseError as exc:
            self.log.error("RSS parse failed: %s", exc)
            return []

        jobs: list[RawJob] = []
        for item in root.iter("item"):
            job = self._parse_item(item)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_item(self, item: ElementTree.Element) -> RawJob | None:
        title_raw = _text(item, "title")
        link = _text(item, "link")
        if not title_raw or not link:
            return None

        company, _, role = title_raw.partition(":")
        company, role = company.strip(), role.strip()
        if not role:  # title had no separator - treat the whole string as the role
            company, role = "", title_raw.strip()

        description_html = html.unescape(_text(item, "description") or "")
        description_text = html_to_text(description_html)

        if not company:
            match = _HEADQUARTERS_RE.search(description_text)
            company = match.group(1).strip() if match else "Unknown"

        region = _text(item, "region") or ""
        country = _text(item, "country") or ""
        state = _text(item, "state") or ""
        location = ", ".join(p for p in (state, country, region) if p) or "Remote"

        type_raw = (_text(item, "type") or "").strip().lower()
        category = _text(item, "category") or ""

        return RawJob(
            source=self.name,
            source_job_id=_text(item, "guid") or link,
            title=role,
            company=company,
            url=link,
            location=location,
            description_html=description_html,
            description_text=description_text,
            posted_at=_parse_date(_text(item, "pubDate")),
            tags=[t for t in (category,) if t],
            employment_type=EMPLOYMENT_TYPES.get(type_raw),
            remote_hint=True,
            raw={
                "region": region,
                "country": country,
                "state": state,
                "category": category,
                "type": type_raw,
            },
        )


def _text(element: ElementTree.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
