"""Analytics queries powering the API and dashboard.

Two rules hold across everything here:

* only **canonical** rows count (``duplicate_of_id IS NULL``), otherwise a role
  cross-posted to three boards would be counted three times;
* salary aggregates use the midpoint of a posting's range and only include
  postings that actually disclosed pay, with the sample size returned alongside
  so a "$400k average" from two postings is visibly untrustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, Select, and_, case, distinct, func, select
from sqlalchemy.orm import Session

from ..models import Job, JobSkill, ScrapeRun, Skill

# The midpoint of a disclosed range, tolerating one-sided ranges.
SALARY_MIDPOINT = case(
    (
        and_(Job.salary_min_usd.isnot(None), Job.salary_max_usd.isnot(None)),
        (Job.salary_min_usd + Job.salary_max_usd) / 2.0,
    ),
    (Job.salary_min_usd.isnot(None), Job.salary_min_usd),
    else_=Job.salary_max_usd,
)

MIN_SALARY_SAMPLE = 3  # below this, a salary average is noise


@dataclass(slots=True)
class JobFilters:
    """Filters shared by the job search endpoint and every aggregate."""

    search: str | None = None
    skills: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    seniority: str | None = None
    employment_type: str | None = None
    country: str | None = None
    region: str | None = None
    remote_only: bool = False
    with_salary_only: bool = False
    company: str | None = None
    posted_within_days: int | None = None
    include_duplicates: bool = False

    def apply(self, stmt: Select, session: Session | None = None) -> Select:
        if not self.include_duplicates:
            stmt = stmt.where(Job.duplicate_of_id.is_(None))
        stmt = stmt.where(Job.is_active.is_(True))

        if self.search:
            pattern = f"%{self.search.lower()}%"
            stmt = stmt.where(
                func.lower(Job.title).like(pattern) | func.lower(Job.company_name).like(pattern)
            )
        if self.sources:
            stmt = stmt.where(Job.source.in_(self.sources))
        if self.seniority:
            stmt = stmt.where(Job.seniority == self.seniority)
        if self.employment_type:
            stmt = stmt.where(Job.employment_type == self.employment_type)
        if self.country:
            stmt = stmt.where(Job.country == self.country)
        if self.region:
            stmt = stmt.where(Job.region == self.region)
        if self.remote_only:
            stmt = stmt.where(Job.is_remote.is_(True))
        if self.with_salary_only:
            stmt = stmt.where(
                Job.salary_min_usd.isnot(None) | Job.salary_max_usd.isnot(None)
            )
        if self.company:
            stmt = stmt.where(func.lower(Job.company_name).like(f"%{self.company.lower()}%"))
        if self.posted_within_days:
            cutoff = datetime.now(UTC) - timedelta(days=self.posted_within_days)
            stmt = stmt.where(
                (Job.posted_at >= cutoff) | (Job.posted_at.is_(None) & (Job.first_seen_at >= cutoff))
            )
        for slug in self.skills:
            # AND semantics: each requested skill must be present.
            stmt = stmt.where(
                Job.id.in_(
                    select(JobSkill.job_id).join(Skill).where(Skill.slug == slug)
                )
            )
        return stmt


# Smoothing prior for match ranking. Raw coverage puts a posting where the
# extractor found a single skill you happen to have at a perfect 100%, which
# floods the top of the list with the weakest evidence in the corpus. Dividing
# by (required + PRIOR) instead of required shrinks small-denominator matches
# toward zero, so "5 of 6" outranks "1 of 1" - which is what a person means by
# a good match.
MATCH_PRIOR = 2.0


def _match_ratio_subquery(have: list[str]):
    """Per-job match strength: matched skills, damped by a smoothing prior."""
    matched = func.count(distinct(case((Skill.slug.in_(have), Skill.slug))))
    required = func.count(distinct(Skill.slug))
    return (
        select(
            JobSkill.job_id.label("job_id"),
            (func.cast(matched, Float) / (required + MATCH_PRIOR)).label("ratio"),
        )
        .select_from(JobSkill)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .group_by(JobSkill.job_id)
        .subquery()
    )


def search_jobs(
    session: Session,
    filters: JobFilters,
    limit: int = 50,
    offset: int = 0,
    sort: str = "recent",
    have: list[str] | None = None,
) -> tuple[list[Job], int]:
    """Return one page of postings plus the total match count.

    ``sort="match"`` ranks by how much of each posting's stack ``have`` covers.
    """
    total = session.scalar(filters.apply(select(func.count(distinct(Job.id))))) or 0

    stmt = filters.apply(select(Job))
    if sort == "match" and have:
        ratios = _match_ratio_subquery([slug.lower() for slug in have])
        stmt = stmt.outerjoin(ratios, ratios.c.job_id == Job.id).order_by(
            ratios.c.ratio.desc().nullslast(),
            func.coalesce(Job.posted_at, Job.first_seen_at).desc(),
        )
    elif sort == "salary":
        stmt = stmt.order_by(SALARY_MIDPOINT.desc().nullslast(), Job.posted_at.desc().nullslast())
    elif sort == "company":
        stmt = stmt.order_by(Job.company_name.asc(), Job.posted_at.desc().nullslast())
    else:
        stmt = stmt.order_by(
            func.coalesce(Job.posted_at, Job.first_seen_at).desc(), Job.id.desc()
        )
    stmt = stmt.limit(limit).offset(offset)
    return list(session.execute(stmt).scalars()), int(total)


def overview(session: Session, filters: JobFilters | None = None) -> dict[str, Any]:
    """Headline counters for the dashboard."""
    filters = filters or JobFilters()
    base = filters.apply(select(func.count(distinct(Job.id))))

    total = session.scalar(base) or 0
    remote = session.scalar(base.where(Job.is_remote.is_(True))) or 0
    with_salary = session.scalar(
        base.where(Job.salary_min_usd.isnot(None) | Job.salary_max_usd.isnot(None))
    ) or 0
    companies = session.scalar(filters.apply(select(func.count(distinct(Job.company_id))))) or 0

    salary_stmt = filters.apply(
        select(func.avg(SALARY_MIDPOINT), func.count(SALARY_MIDPOINT))
    ).where(Job.salary_min_usd.isnot(None) | Job.salary_max_usd.isnot(None))
    avg_salary, salary_sample = session.execute(salary_stmt).one()

    duplicates = session.scalar(
        select(func.count(Job.id)).where(Job.duplicate_of_id.isnot(None), Job.is_active.is_(True))
    ) or 0
    all_active = session.scalar(
        select(func.count(Job.id)).where(Job.is_active.is_(True))
    ) or 0

    return {
        "total_jobs": int(total),
        "remote_jobs": int(remote),
        "remote_share": round(remote / total, 4) if total else 0.0,
        "jobs_with_salary": int(with_salary),
        "salary_disclosure_rate": round(with_salary / total, 4) if total else 0.0,
        "companies": int(companies),
        "avg_salary_usd": round(float(avg_salary), 0) if avg_salary else None,
        "salary_sample_size": int(salary_sample or 0),
        "duplicates_detected": int(duplicates),
        "duplicate_rate": round(duplicates / all_active, 4) if all_active else 0.0,
    }


def top_skills(
    session: Session, filters: JobFilters | None = None, limit: int = 25, category: str | None = None
) -> list[dict[str, Any]]:
    """Most demanded skills, with the salary premium attached to each."""
    filters = filters or JobFilters()
    stmt = (
        filters.apply(
            select(
                Skill.slug,
                Skill.name,
                Skill.category,
                func.count(distinct(Job.id)).label("job_count"),
                func.avg(SALARY_MIDPOINT).label("avg_salary"),
                func.count(SALARY_MIDPOINT).label("salary_sample"),
            )
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .join(Skill, Skill.id == JobSkill.skill_id)
        )
        .group_by(Skill.slug, Skill.name, Skill.category)
        .order_by(func.count(distinct(Job.id)).desc())
        .limit(limit)
    )
    if category:
        stmt = stmt.where(Skill.category == category)

    total = session.scalar(filters.apply(select(func.count(distinct(Job.id))))) or 0
    rows = []
    for slug, name, cat, count, avg_salary, sample in session.execute(stmt):
        rows.append(
            {
                "slug": slug,
                "name": name,
                "category": cat,
                "job_count": int(count),
                "share": round(count / total, 4) if total else 0.0,
                "avg_salary_usd": (
                    round(float(avg_salary), 0)
                    if avg_salary and (sample or 0) >= MIN_SALARY_SAMPLE
                    else None
                ),
                "salary_sample_size": int(sample or 0),
            }
        )
    return rows


def skill_trend(
    session: Session, slugs: list[str], days: int = 90, bucket_days: int = 7
) -> dict[str, Any]:
    """Postings per time bucket for each skill - the "demand over time" chart.

    Buckets are computed in Python rather than SQL so the query stays portable
    between SQLite and Postgres, which have different date-truncation syntax.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    posted = func.coalesce(Job.posted_at, Job.first_seen_at)

    stmt = (
        select(Skill.slug, posted)
        .select_from(Job)
        .join(JobSkill, JobSkill.job_id == Job.id)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .where(
            Job.duplicate_of_id.is_(None),
            Job.is_active.is_(True),
            posted >= cutoff,
        )
    )
    if slugs:
        stmt = stmt.where(Skill.slug.in_(slugs))

    now = datetime.now(UTC)
    bucket_count = max(1, days // bucket_days)
    labels = [
        (now - timedelta(days=days) + timedelta(days=bucket_days * (i + 1))).date().isoformat()
        for i in range(bucket_count)
    ]
    series: dict[str, list[int]] = {slug: [0] * bucket_count for slug in slugs}

    for slug, when in session.execute(stmt):
        if when is None or slug not in series:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        age_days = (now - when).days
        index = bucket_count - 1 - min(bucket_count - 1, max(0, age_days // bucket_days))
        series[slug][index] += 1

    return {"labels": labels, "series": series, "bucket_days": bucket_days, "days": days}


def salary_by_seniority(session: Session, filters: JobFilters | None = None) -> list[dict[str, Any]]:
    filters = filters or JobFilters()
    stmt = (
        filters.apply(
            select(
                Job.seniority,
                func.count(distinct(Job.id)),
                func.avg(SALARY_MIDPOINT),
                func.min(Job.salary_min_usd),
                func.max(Job.salary_max_usd),
                func.count(SALARY_MIDPOINT),
            )
        )
        .where(Job.salary_min_usd.isnot(None) | Job.salary_max_usd.isnot(None))
        .group_by(Job.seniority)
        .order_by(func.avg(SALARY_MIDPOINT).desc())
    )
    rows = []
    for seniority, count, avg_salary, low, high, sample in session.execute(stmt):
        if (sample or 0) < MIN_SALARY_SAMPLE:
            continue
        rows.append(
            {
                "seniority": seniority or "unspecified",
                "job_count": int(count),
                "avg_salary_usd": round(float(avg_salary), 0) if avg_salary else None,
                "min_salary_usd": round(float(low), 0) if low else None,
                "max_salary_usd": round(float(high), 0) if high else None,
                "salary_sample_size": int(sample or 0),
            }
        )
    return rows


def breakdown(session: Session, dimension: str, filters: JobFilters | None = None, limit: int = 15):
    """Counts grouped by one dimension (source, country, region, seniority...)."""
    columns = {
        "source": Job.source,
        "country": Job.country,
        "region": Job.region,
        "seniority": Job.seniority,
        "employment_type": Job.employment_type,
        "company": Job.company_name,
    }
    column = columns.get(dimension)
    if column is None:
        raise ValueError(f"unknown dimension {dimension!r}; try one of {sorted(columns)}")

    filters = filters or JobFilters()
    stmt = (
        filters.apply(select(column, func.count(distinct(Job.id))))
        .group_by(column)
        .order_by(func.count(distinct(Job.id)).desc())
        .limit(limit)
    )
    return [
        {"key": key or "unspecified", "job_count": int(count)}
        for key, count in session.execute(stmt)
    ]


def top_companies(
    session: Session, filters: JobFilters | None = None, limit: int = 15
) -> list[dict[str, Any]]:
    filters = filters or JobFilters()
    stmt = (
        filters.apply(
            select(
                Job.company_name,
                func.count(distinct(Job.id)),
                func.avg(SALARY_MIDPOINT),
                func.count(SALARY_MIDPOINT),
            )
        )
        .where(Job.company_name != "")
        .group_by(Job.company_name)
        .order_by(func.count(distinct(Job.id)).desc())
        .limit(limit)
    )
    return [
        {
            "company": name,
            "job_count": int(count),
            "avg_salary_usd": (
                round(float(avg_salary), 0)
                if avg_salary and (sample or 0) >= MIN_SALARY_SAMPLE
                else None
            ),
        }
        for name, count, avg_salary, sample in session.execute(stmt)
    ]


def skill_cooccurrence(
    session: Session, slug: str, limit: int = 12
) -> list[dict[str, Any]]:
    """Which skills are asked for alongside this one - the "stack" view."""
    target_jobs = (
        select(JobSkill.job_id)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .join(Job, Job.id == JobSkill.job_id)
        .where(Skill.slug == slug, Job.duplicate_of_id.is_(None), Job.is_active.is_(True))
        .scalar_subquery()
    )
    stmt = (
        select(Skill.slug, Skill.name, func.count(distinct(JobSkill.job_id)))
        .select_from(JobSkill)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .where(JobSkill.job_id.in_(target_jobs), Skill.slug != slug)
        .group_by(Skill.slug, Skill.name)
        .order_by(func.count(distinct(JobSkill.job_id)).desc())
        .limit(limit)
    )
    base_count = session.scalar(
        select(func.count(distinct(JobSkill.job_id))).where(JobSkill.job_id.in_(target_jobs))
    ) or 0
    return [
        {
            "slug": other_slug,
            "name": name,
            "job_count": int(count),
            "share": round(count / base_count, 4) if base_count else 0.0,
        }
        for other_slug, name, count in session.execute(stmt)
    ]


def recent_runs(session: Session, limit: int = 12) -> list[dict[str, Any]]:
    """Crawl history - the operational view of the pipeline."""
    stmt = select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
    return [
        {
            "id": run.id,
            "source": run.source,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "duration_seconds": round(run.duration_seconds, 1) if run.duration_seconds else None,
            "fetched": run.fetched,
            "created": run.created,
            "updated": run.updated,
            "duplicates": run.duplicates,
            "failed": run.failed,
            "http_requests": run.http_requests,
            "bytes_downloaded": run.bytes_downloaded,
            "error": run.error,
        }
        for run in session.execute(stmt).scalars()
    ]


def filter_options(session: Session) -> dict[str, list[str]]:
    """Distinct values available for each filter, for populating the UI."""

    def distinct_values(column) -> list[str]:
        stmt = (
            select(distinct(column))
            .where(column.isnot(None), Job.is_active.is_(True), Job.duplicate_of_id.is_(None))
            .order_by(column)
        )
        return [value for (value,) in session.execute(stmt) if value]

    return {
        "sources": distinct_values(Job.source),
        "seniority": distinct_values(Job.seniority),
        "employment_types": distinct_values(Job.employment_type),
        "countries": distinct_values(Job.country),
        "regions": distinct_values(Job.region),
        "skill_categories": [
            value for (value,) in session.execute(select(distinct(Skill.category)).order_by(Skill.category))
        ],
    }
