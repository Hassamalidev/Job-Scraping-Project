"""Crawling primitives shared by every source scraper.

What lives here is the "be a good citizen" layer:

* per-domain token-bucket rate limiting (honouring ``Crawl-delay``)
* robots.txt evaluation before the first request to a host
* conditional GET (ETag / Last-Modified) so re-crawls are cheap for both sides
* retry with exponential backoff + jitter, respecting ``Retry-After``
* optional archival of raw payloads so parsing can be replayed offline

Source scrapers only implement :meth:`BaseScraper.fetch` and return
:class:`RawJob` objects; normalisation happens later in the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings, get_settings
from ..utils import sha1_hex

logger = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids the URL we were about to fetch."""


@dataclass(slots=True)
class RawJob:
    """A posting as the source described it, before any normalisation."""

    source: str
    source_job_id: str
    title: str
    company: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    posted_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    employment_type: str | None = None
    remote_hint: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    bytes_downloaded: int = 0

    def merge(self, other: FetchStats) -> None:
        self.requests += other.requests
        self.cache_hits += other.cache_hits
        self.retries += other.retries
        self.bytes_downloaded += other.bytes_downloaded


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


class DomainRateLimiter:
    """Serialises requests per host to at most ``rate`` per second."""

    def __init__(self, rate_per_second: float) -> None:
        self._default_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._intervals: dict[str, float] = {}
        self._next_allowed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_crawl_delay(self, host: str, delay: float | None) -> None:
        if delay and delay > 0:
            # Never crawl faster than the site asked us to.
            self._intervals[host] = max(delay, self._default_interval)
            logger.debug("host=%s honouring crawl-delay=%.2fs", host, delay)

    async def acquire(self, host: str) -> None:
        interval = self._intervals.get(host, self._default_interval)
        if interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait_for = self._next_allowed.get(host, 0.0) - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_allowed[host] = now + interval


class RobotsPolicy:
    """Fetches and caches robots.txt per host, fail-open on network errors."""

    def __init__(self, user_agent: str, enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.enabled = enabled
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _load(self, client: httpx.AsyncClient, host: str, scheme: str) -> RobotFileParser | None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self._parsers:
                return self._parsers[host]
            parser: RobotFileParser | None = None
            try:
                response = await client.get(f"{scheme}://{host}/robots.txt", timeout=10.0)
                if response.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                else:
                    logger.debug("host=%s robots.txt status=%s -> allow all", host, response.status_code)
            except httpx.HTTPError as exc:
                # Unreachable robots.txt is treated as "no restrictions", which is
                # the conventional reading, but we log it so it is never silent.
                logger.warning("host=%s robots.txt unreachable (%s) -> allow all", host, exc)
            self._parsers[host] = parser
            return parser

    async def check(
        self, client: httpx.AsyncClient, url: str, limiter: DomainRateLimiter | None = None
    ) -> None:
        if not self.enabled:
            return
        parts = urlparse(url)
        parser = await self._load(client, parts.netloc, parts.scheme or "https")
        if parser is None:
            return
        if limiter is not None:
            limiter.set_crawl_delay(parts.netloc, parser.crawl_delay(self.user_agent))
        if not parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")


class ConditionalCache:
    """On-disk ETag / Last-Modified store enabling cheap re-crawls."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.directory / f"{sha1_hex(url)}.json"

    def load(self, url: str) -> dict[str, Any] | None:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, url: str, response: httpx.Response, body: str) -> None:
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if not etag and not last_modified:
            return
        payload = {
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "body": body,
            "cached_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._path(url).write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk issues
            logger.debug("cache write failed for %s: %s", url, exc)

    @staticmethod
    def conditional_headers(entry: dict[str, Any] | None) -> dict[str, str]:
        if not entry:
            return {}
        headers = {}
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]
        return headers


class HttpClient:
    """Async HTTP client wrapping httpx with the politeness layer applied."""

    def __init__(self, source: str, settings: Settings | None = None) -> None:
        self.source = source
        self.settings = settings or get_settings()
        self.stats = FetchStats()
        self._limiter = DomainRateLimiter(self.settings.rate_limit_per_second)
        self._robots = RobotsPolicy(self.settings.user_agent, self.settings.respect_robots)
        self._cache = ConditionalCache(self.settings.cache_dir)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(self.settings.request_timeout),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")
        return self._client

    async def get(self, url: str, params: dict[str, Any] | None = None) -> FetchResult:
        """GET with robots check, rate limiting, conditional GET and retries."""
        await self._robots.check(self.client, url, self._limiter)
        host = urlparse(url).netloc

        cached = self._cache.load(url)
        headers = ConditionalCache.conditional_headers(cached)

        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            await self._limiter.acquire(host)
            try:
                response = await self.client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                self.stats.retries += 1
                await self._sleep_backoff(attempt)
                continue

            self.stats.requests += 1

            if response.status_code == 304 and cached:
                self.stats.cache_hits += 1
                logger.debug("304 not-modified %s", url)
                return FetchResult(url=url, status=304, text=cached["body"], from_cache=True)

            if response.status_code in RETRY_STATUSES:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} for {url}", request=response.request, response=response
                )
                if attempt >= self.settings.max_retries:
                    break
                self.stats.retries += 1
                await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            body = response.text
            self.stats.bytes_downloaded += len(response.content)
            self._cache.save(url, response, body)
            return FetchResult(url=url, status=response.status_code, text=body)

        raise RuntimeError(f"GET failed after {self.settings.max_retries + 1} attempts: {url}") from last_error

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        result = await self.get(url, params=params)
        return result.json()

    async def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 60.0))
                return
            except ValueError:
                pass  # HTTP-date form; fall through to exponential backoff
        delay = self.settings.backoff_base * (2**attempt)
        delay += random.uniform(0, delay * 0.3)  # jitter avoids thundering herds
        await asyncio.sleep(min(delay, 30.0))

    def archive(self, name: str, content: str, extension: str = "json") -> Path | None:
        """Persist a raw payload so parser changes can be replayed offline."""
        if not self.settings.archive_raw:
            return None
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        directory = self.settings.raw_dir / self.source / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.{extension}"
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk issues
            logger.debug("archive failed for %s: %s", path, exc)
            return None
        return path


class BaseScraper(ABC):
    """Implement :meth:`fetch` and register the subclass to add a source."""

    name: str = "base"
    display_name: str = "Base"
    homepage: str = ""
    attribution: str | None = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.log = logging.getLogger(f"jmi.scrapers.{self.name}")

    @abstractmethod
    async def fetch(self, client: HttpClient) -> list[RawJob]:
        """Return every posting currently visible on this source."""

    async def run(self) -> tuple[list[RawJob], FetchStats]:
        async with HttpClient(self.name, self.settings) as client:
            try:
                jobs = await self.fetch(client)
            except RobotsDisallowed as exc:
                self.log.warning("skipping source: %s", exc)
                return [], client.stats
            self.log.info(
                "fetched %d postings (%d requests, %d cache hits, %.1f KB)",
                len(jobs), client.stats.requests, client.stats.cache_hits,
                client.stats.bytes_downloaded / 1024,
            )
            return jobs, client.stats
