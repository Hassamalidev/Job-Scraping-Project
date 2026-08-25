"""The ingest orchestrator: scrape -> normalise -> enrich -> dedupe -> store.

Every run is recorded as a :class:`ScrapeRun` so the crawl has an audit trail:
how many postings were fetched, created, updated, judged duplicate, or failed.
Runs are idempotent - re-running immediately updates ``last_seen_at`` and
creates nothing, which is exactly what a scheduled crawl should do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import Company, Job, JobSkill, ScrapeRun, Skill, utcnow
from ..scrapers.registry import RawJob, enabled_scrapers, get_scraper
from ..utils import company_key, sha1_hex, signed_64, simhash, truncate
from .dedupe import find_duplicate, location_bucket
from .normalize import (
    classify_employment_type,
    classify_seniority,
    detect_remote,
    normalize_title,
    parse_location,
)
from .salary import from_structured, parse_salary_text
from .skills import TAXONOMY, extract_skills

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    source: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    deactivated: int = 0
    skills_linked: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_row(self) -> tuple[str, ...]:
        return (
            self.source, str(self.fetched), str(self.created), str(self.updated),
            str(self.duplicates), str(self.deactivated), str(self.failed),
            f"{self.duration_seconds:.1f}s",
        )


def seed_skills(session: Session) -> int:
    """Sync the skills table with the taxonomy.

    Inserts new entries and updates renamed or recategorised ones, so editing the
    taxonomy does not leave stale display names behind in the database.
    """
    existing = {skill.slug: skill for skill in session.execute(select(Skill)).scalars()}
    added = 0
    for definition in TAXONOMY:
        current = existing.get(definition.slug)
        if current is None:
            session.add(
                Skill(slug=definition.slug, name=definition.name, category=definition.category)
            )
            added += 1
        elif (current.name, current.category) != (definition.name, definition.category):
            current.name = definition.name
            current.category = definition.category
    session.flush()
    return added


def _get_or_create_company(session: Session, name: str, cache: dict[str, Company]) -> Company | None:
    if not name or name.lower() in {"unknown", "n/a", "-"}:
        return None
    slug = company_key(name)
    if slug in cache:
        return cache[slug]

    company = session.execute(select(Company).where(Company.slug == slug)).scalar_one_or_none()
    if company is None:
        company = Company(slug=slug, name=name.strip()[:200])
        session.add(company)
        session.flush()
    cache[slug] = company
    return company


def _apply_raw_to_job(job: Job, raw: RawJob) -> None:
    """Normalise and enrich a raw posting onto a Job row."""
    description_text = raw.description_text or ""

    job.title = raw.title.strip()[:300]
    job.title_normalized = normalize_title(raw.title)
    job.url = raw.url[:600]
    job.apply_url = truncate(raw.apply_url, 600)
    job.location_raw = truncate(raw.location, 300)
    job.description_html = raw.description_html
    job.description_text = description_text
    job.posted_at = raw.posted_at

    job.is_remote = detect_remote(raw.location, description_text, raw.remote_hint)
    country, region = parse_location(raw.location)
    job.country = country
    job.region = region if region else ("Worldwide" if job.is_remote else None)
    job.seniority = classify_seniority(raw.title, description_text)
    job.employment_type = classify_employment_type(
        raw.title, description_text, raw.employment_type
    )

    salary = from_structured(
        raw.salary_min, raw.salary_max, raw.salary_currency, raw.salary_period
    ) or parse_salary_text(raw.raw.get("salary_text") or description_text)
    if salary is not None:
        job.salary_min_usd = salary.min_usd
        job.salary_max_usd = salary.max_usd
        job.salary_currency = salary.currency
        job.salary_period = salary.period
        job.salary_origin = salary.origin
    else:
        job.salary_min_usd = job.salary_max_usd = None
        job.salary_currency = job.salary_period = job.salary_origin = None

    job.simhash = signed_64(simhash(f"{raw.title} {description_text[:4000]}"))
    job.content_hash = sha1_hex(
        company_key(raw.company),
        job.title_normalized,
        location_bucket(job.is_remote, country),
    )
    job.raw = raw.raw


def _sync_skills(session: Session, job: Job, raw: RawJob, skill_ids: dict[str, int]) -> int:
    matches = extract_skills(raw.description_text, raw.title, raw.tags, company=raw.company)
    session.query(JobSkill).filter(JobSkill.job_id == job.id).delete(synchronize_session=False)
    linked = 0
    for match in matches:
        skill_id = skill_ids.get(match.slug)
        if skill_id is None:
            continue
        session.add(
            JobSkill(
                job_id=job.id,
                skill_id=skill_id,
                matched_via=match.matched_via,
                confidence=match.confidence,
            )
        )
        linked += 1
    return linked


def store_raw_jobs(
    session: Session, source: str, raw_jobs: list[RawJob], settings: Settings
) -> IngestReport:
    """Persist a batch of scraped postings. Safe to call repeatedly."""
    report = IngestReport(source=source, fetched=len(raw_jobs))
    seed_skills(session)
    skill_ids = {slug: sid for slug, sid in session.execute(select(Skill.slug, Skill.id))}
    company_cache: dict[str, Company] = {}
    run_started = utcnow()

    seen_source_ids: set[str] = set()

    for raw in raw_jobs:
        if raw.source_job_id in seen_source_ids:
            continue  # the feed itself listed it twice
        seen_source_ids.add(raw.source_job_id)

        try:
            # A SAVEPOINT per posting: a bad row rolls back only itself instead
            # of discarding everything already staged in this batch.
            with session.begin_nested():
                job = session.execute(
                    select(Job).where(Job.source == source, Job.source_job_id == raw.source_job_id)
                ).scalar_one_or_none()

                # Resolve the company *before* a new Job is attached to the
                # session: its SELECT would otherwise autoflush a half-built row.
                company = _get_or_create_company(session, raw.company, company_cache)

                is_new = job is None
                if is_new:
                    job = Job(source=source, source_job_id=raw.source_job_id[:200], content_hash="")

                previous_hash = job.content_hash
                job.company_id = company.id if company else None
                job.company_name = (company.name if company else raw.company or "Unknown")[:200]

                _apply_raw_to_job(job, raw)
                if is_new:
                    session.add(job)  # fully populated, safe to attach
                job.last_seen_at = run_started
                job.is_active = True
                session.flush()

                content_changed = previous_hash != job.content_hash
                linked = 0
                is_duplicate = False
                if is_new or content_changed:
                    linked = _sync_skills(session, job, raw, skill_ids)

                    verdict = find_duplicate(
                        session,
                        job_id=job.id,
                        content_hash=job.content_hash,
                        company_id=job.company_id,
                        title_normalized=job.title_normalized,
                        simhash=job.simhash,
                        is_remote=job.is_remote,
                        country=job.country,
                    )
                    job.duplicate_of_id = verdict.canonical_id
                    is_duplicate = verdict.is_duplicate

                session.flush()
        except Exception as exc:  # one bad posting must not abort the batch
            report.failed += 1
            message = f"{raw.source_job_id}: {type(exc).__name__}: {exc}"
            if len(report.errors) < 10:
                report.errors.append(message)
            logger.warning("failed to store %s", message)
            company_cache.clear()  # cached objects may have been expunged
        else:
            report.skills_linked += linked
            report.duplicates += int(is_duplicate)
            report.created += int(is_new)
            report.updated += int(not is_new)

    report.deactivated = _deactivate_stale(session, source, run_started, len(raw_jobs), settings)
    return report


def _deactivate_stale(
    session: Session, source: str, run_started: datetime, fetched: int, settings: Settings
) -> int:
    """Mark postings this source stopped listing as inactive.

    Skipped when a run returned nothing - an empty fetch usually means the source
    broke, and wiping the dataset on a transient failure would be far worse than
    carrying stale rows for one cycle.
    """
    if fetched == 0:
        logger.warning("source=%s returned 0 postings; skipping deactivation", source)
        return 0

    cutoff = run_started - timedelta(days=settings.stale_after_days)
    result = session.execute(
        update(Job)
        .where(
            Job.source == source,
            Job.is_active.is_(True),
            Job.last_seen_at < run_started,
            Job.first_seen_at < cutoff,
        )
        .values(is_active=False)
        # SQLite returns naive datetimes, so letting the ORM re-evaluate this
        # criteria in Python raises "can't compare offset-naive and offset-aware".
        # The DB is the only thing that needs to apply it.
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    return int(result.rowcount or 0)


def _tags_from_raw(job: Job) -> list[str]:
    """Recover the source's own tags from the archived payload."""
    raw = job.raw or {}
    for key in ("tags", "departments"):
        value = raw.get(key)
        if isinstance(value, list):
            return [str(v) for v in value if v]
    category = raw.get("category")
    return [str(category)] if category else []


def _raw_job_from_row(job: Job) -> RawJob:
    """Rebuild a RawJob from a stored row so enrichment can be replayed."""
    return RawJob(
        source=job.source,
        source_job_id=job.source_job_id,
        title=job.title,
        company=job.company_name,
        url=job.url,
        apply_url=job.apply_url,
        location=job.location_raw,
        description_html=job.description_html,
        description_text=job.description_text,
        posted_at=job.posted_at,
        tags=_tags_from_raw(job),
        raw=job.raw or {},
    )


def reprocess_stored(
    session: Session, settings: Settings, sources: list[str] | None = None
) -> IngestReport:
    """Re-run enrichment over stored postings without hitting the network.

    This is what makes a parser fix cheap: descriptions and raw payloads are
    already on disk, so improving salary parsing or the skill taxonomy is a local
    replay rather than a re-crawl of every source.
    """
    report = IngestReport(source="reprocess")
    seed_skills(session)
    skill_ids = dict(session.execute(select(Skill.slug, Skill.id)).all())

    # Clear duplicate links so the pass recomputes them from scratch; processing
    # oldest-first keeps the earliest sighting canonical.
    clear_stmt = update(Job).values(duplicate_of_id=None)
    if sources:
        clear_stmt = clear_stmt.where(Job.source.in_(sources))
    session.execute(clear_stmt.execution_options(synchronize_session=False))
    session.expire_all()

    stmt = select(Job).where(Job.is_active.is_(True)).order_by(Job.first_seen_at, Job.id)
    if sources:
        stmt = stmt.where(Job.source.in_(sources))

    for job in session.execute(stmt).scalars():
        report.fetched += 1
        try:
            with session.begin_nested():
                raw = _raw_job_from_row(job)
                _apply_raw_to_job(job, raw)
                session.flush()
                linked = _sync_skills(session, job, raw, skill_ids)
                verdict = find_duplicate(
                    session,
                    job_id=job.id,
                    content_hash=job.content_hash,
                    company_id=job.company_id,
                    title_normalized=job.title_normalized,
                    simhash=job.simhash,
                    is_remote=job.is_remote,
                    country=job.country,
                )
                job.duplicate_of_id = verdict.canonical_id
                is_duplicate = verdict.is_duplicate
                session.flush()
        except Exception as exc:
            report.failed += 1
            if len(report.errors) < 10:
                report.errors.append(f"{job.id}: {type(exc).__name__}: {exc}")
            logger.warning("reprocess failed for job %s: %s", job.id, exc)
        else:
            report.updated += 1
            report.skills_linked += linked
            report.duplicates += int(is_duplicate)

    logger.info(
        "reprocessed %d postings: %d duplicates, %d skill links, %d failed",
        report.updated, report.duplicates, report.skills_linked, report.failed,
    )
    return report


async def run_source(name: str, settings: Settings | None = None) -> IngestReport:
    """Scrape one source end to end and persist the result."""
    settings = settings or get_settings()
    scraper = get_scraper(name, settings)
    started = datetime.now(UTC)

    with session_scope() as session:
        run = ScrapeRun(source=name, started_at=started, status="running")
        session.add(run)
        session.flush()
        run_id = run.id

    try:
        raw_jobs, stats = await scraper.run()
    except Exception as exc:
        logger.exception("source=%s crawl failed", name)
        with session_scope() as session:
            session.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(status="failed", finished_at=utcnow(), error=str(exc)[:2000])
            )
        return IngestReport(source=name, error=str(exc))

    with session_scope() as session:
        report = store_raw_jobs(session, name, raw_jobs, settings)
        report.duration_seconds = (datetime.now(UTC) - started).total_seconds()
        session.execute(
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(
                status="ok" if not report.error else "failed",
                finished_at=utcnow(),
                fetched=report.fetched,
                created=report.created,
                updated=report.updated,
                duplicates=report.duplicates,
                failed=report.failed,
                http_requests=stats.requests,
                bytes_downloaded=stats.bytes_downloaded,
                error="; ".join(report.errors)[:2000] or None,
            )
        )
    logger.info(
        "source=%s fetched=%d created=%d updated=%d dupes=%d failed=%d in %.1fs",
        name, report.fetched, report.created, report.updated,
        report.duplicates, report.failed, report.duration_seconds,
    )
    return report


async def run_all(
    sources: list[str] | None = None, settings: Settings | None = None
) -> list[IngestReport]:
    """Crawl every enabled source concurrently.

    Concurrency is across sources only - each source stays rate limited to its
    own domain, so this adds throughput without hammering anyone.
    """
    settings = settings or get_settings()
    names = sources or [s.name for s in enabled_scrapers(settings)]
    results = await asyncio.gather(
        *(run_source(name, settings) for name in names), return_exceptions=True
    )

    reports: list[IngestReport] = []
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            logger.error("source=%s raised %s", name, result)
            reports.append(IngestReport(source=name, error=str(result)))
        else:
            reports.append(result)
    return reports
