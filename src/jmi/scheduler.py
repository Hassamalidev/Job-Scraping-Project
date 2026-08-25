"""Periodic crawling with APScheduler.

Two settings keep repeated runs safe:

``max_instances=1``
    a long crawl can outlast its interval; without this the scheduler would
    start a second overlapping crawl and both would fight over the same rows.
``coalesce=True``
    if runs were missed (laptop asleep, container restarted), catch up with one
    run rather than a burst of queued ones.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_settings
from .db import init_db
from .pipeline.ingest import run_all

logger = logging.getLogger(__name__)


def crawl_once() -> None:
    """One full pass over every enabled source."""
    settings = get_settings()
    reports = asyncio.run(run_all(settings=settings))
    created = sum(report.created for report in reports)
    fetched = sum(report.fetched for report in reports)
    failed = sum(report.failed for report in reports)
    logger.info("crawl complete: fetched=%d new=%d failed=%d", fetched, created, failed)


def run_scheduler(interval_minutes: int, run_immediately: bool = True) -> None:
    init_db()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        crawl_once,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="crawl",
        name="Crawl all enabled sources",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    if run_immediately:
        crawl_once()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover - manual stop
        logger.info("scheduler stopped")
