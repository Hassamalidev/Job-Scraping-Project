"""Scraper registry - adding a source means adding one class and one line here."""

from __future__ import annotations

from ..config import Settings, get_settings
from .arbeitnow import ArbeitnowScraper
from .base import BaseScraper, FetchStats, HttpClient, RawJob, RobotsDisallowed
from .greenhouse import GreenhouseScraper
from .hackernews import HackerNewsScraper
from .himalayas import HimalayasScraper
from .lever import LeverScraper
from .mustakbil import MustakbilScraper
from .remoteok import RemoteOkScraper
from .weworkremotely import WeWorkRemotelyScraper

SCRAPER_CLASSES: tuple[type[BaseScraper], ...] = (
    RemoteOkScraper,
    WeWorkRemotelyScraper,
    HackerNewsScraper,
    GreenhouseScraper,
    LeverScraper,
    MustakbilScraper,
    HimalayasScraper,
    ArbeitnowScraper,
)

SCRAPERS: dict[str, type[BaseScraper]] = {cls.name: cls for cls in SCRAPER_CLASSES}


def available_sources() -> list[str]:
    return list(SCRAPERS)


def get_scraper(name: str, settings: Settings | None = None) -> BaseScraper:
    try:
        cls = SCRAPERS[name]
    except KeyError:
        raise KeyError(
            f"unknown source {name!r}; available: {', '.join(available_sources())}"
        ) from None
    return cls(settings or get_settings())


def enabled_scrapers(settings: Settings | None = None) -> list[BaseScraper]:
    settings = settings or get_settings()
    return [get_scraper(name, settings) for name in settings.enabled_sources if name in SCRAPERS]


__all__ = [
    "BaseScraper",
    "FetchStats",
    "HttpClient",
    "RawJob",
    "RobotsDisallowed",
    "SCRAPERS",
    "available_sources",
    "enabled_scrapers",
    "get_scraper",
]
