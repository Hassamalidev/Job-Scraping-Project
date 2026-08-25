"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Query

from ..analytics.queries import JobFilters


def job_filters(
    search: str | None = Query(None, description="Match against title or company"),
    skill: list[str] = Query(default_factory=list, description="Skill slug; repeat for AND"),
    source: list[str] = Query(default_factory=list, description="Restrict to these sources"),
    seniority: str | None = Query(None),
    employment_type: str | None = Query(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    company: str | None = Query(None),
    remote_only: bool = Query(False),
    with_salary_only: bool = Query(False),
    posted_within_days: int | None = Query(None, ge=1, le=365),
    include_duplicates: bool = Query(False, description="Include cross-posted duplicates"),
) -> JobFilters:
    return JobFilters(
        search=search,
        skills=skill,
        sources=source,
        seniority=seniority,
        employment_type=employment_type,
        country=country,
        region=region,
        company=company,
        remote_only=remote_only,
        with_salary_only=with_salary_only,
        posted_within_days=posted_within_days,
        include_duplicates=include_duplicates,
    )
