"""Analytics endpoints - the actual product of the pipeline."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..analytics.advisor import (
    DEFAULT_MATCH_THRESHOLD,
    salary_distribution,
    skill_gap,
    skill_momentum,
)
from ..analytics.queries import (
    JobFilters,
    breakdown,
    overview,
    recent_runs,
    salary_by_seniority,
    skill_cooccurrence,
    skill_trend,
    top_companies,
    top_skills,
)
from ..db import get_db
from .deps import job_filters
from .schemas import (
    BreakdownRow,
    CompanyStat,
    CooccurrenceRow,
    MomentumResponse,
    Overview,
    RunInfo,
    SalaryBand,
    SalaryDistribution,
    SkillGapResponse,
    SkillStat,
    TrendResponse,
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/overview", response_model=Overview, summary="Headline metrics")
def get_overview(
    filters: JobFilters = Depends(job_filters), session: Session = Depends(get_db)
) -> Overview:
    return Overview(**overview(session, filters))


@router.get("/skills", response_model=list[SkillStat], summary="Most demanded skills")
def get_skills(
    filters: JobFilters = Depends(job_filters),
    limit: int = Query(25, ge=1, le=100),
    category: str | None = Query(None),
    session: Session = Depends(get_db),
) -> list[SkillStat]:
    return [SkillStat(**row) for row in top_skills(session, filters, limit=limit, category=category)]


@router.get("/skills/{slug}/cooccurrence", response_model=list[CooccurrenceRow],
            summary="Skills requested alongside this one")
def get_cooccurrence(
    slug: str, limit: int = Query(12, ge=1, le=50), session: Session = Depends(get_db)
) -> list[CooccurrenceRow]:
    return [CooccurrenceRow(**row) for row in skill_cooccurrence(session, slug, limit=limit)]


@router.get("/trends", response_model=TrendResponse, summary="Skill demand over time")
def get_trends(
    skill: list[str] = Query(default_factory=lambda: ["python", "typescript", "react"]),
    days: int = Query(90, ge=14, le=365),
    bucket_days: int = Query(7, ge=1, le=30),
    session: Session = Depends(get_db),
) -> TrendResponse:
    return TrendResponse(**skill_trend(session, skill, days=days, bucket_days=bucket_days))


@router.get("/salary/seniority", response_model=list[SalaryBand], summary="Pay by seniority")
def get_salary_bands(
    filters: JobFilters = Depends(job_filters), session: Session = Depends(get_db)
) -> list[SalaryBand]:
    return [SalaryBand(**row) for row in salary_by_seniority(session, filters)]


@router.get("/breakdown/{dimension}", response_model=list[BreakdownRow],
            summary="Counts grouped by a dimension")
def get_breakdown(
    dimension: str,
    filters: JobFilters = Depends(job_filters),
    limit: int = Query(15, ge=1, le=100),
    session: Session = Depends(get_db),
) -> list[BreakdownRow]:
    from fastapi import HTTPException

    try:
        rows = breakdown(session, dimension, filters, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [BreakdownRow(**row) for row in rows]


@router.get("/companies", response_model=list[CompanyStat], summary="Most active employers")
def get_companies(
    filters: JobFilters = Depends(job_filters),
    limit: int = Query(15, ge=1, le=100),
    session: Session = Depends(get_db),
) -> list[CompanyStat]:
    return [CompanyStat(**row) for row in top_companies(session, filters, limit=limit)]


@router.get("/skill-gap", response_model=SkillGapResponse,
            summary="Which skill would unlock the most jobs")
def get_skill_gap(
    have: list[str] = Query(default_factory=list, description="Skill slugs you already have"),
    threshold: float = Query(DEFAULT_MATCH_THRESHOLD, ge=0.1, le=1.0),
    limit: int = Query(12, ge=1, le=40),
    filters: JobFilters = Depends(job_filters),
    session: Session = Depends(get_db),
) -> SkillGapResponse:
    """Marginal analysis: for each skill you lack, how many extra postings you
    would qualify for by learning it, and what those postings pay."""
    return SkillGapResponse(**skill_gap(session, have, filters, threshold=threshold, limit=limit))


@router.get("/momentum", response_model=MomentumResponse, summary="Rising and falling skills")
def get_momentum(
    window_days: int = Query(30, ge=7, le=120),
    limit: int = Query(8, ge=1, le=30),
    session: Session = Depends(get_db),
) -> MomentumResponse:
    return MomentumResponse(**skill_momentum(session, window_days=window_days, limit=limit))


@router.get("/salary/distribution", response_model=SalaryDistribution,
            summary="Salary percentiles and histogram")
def get_salary_distribution(
    filters: JobFilters = Depends(job_filters),
    buckets: int = Query(10, ge=4, le=30),
    session: Session = Depends(get_db),
) -> SalaryDistribution:
    return SalaryDistribution(**salary_distribution(session, filters, buckets=buckets))


@router.get("/runs", response_model=list[RunInfo], summary="Crawl history")
def get_runs(
    limit: int = Query(12, ge=1, le=100), session: Session = Depends(get_db)
) -> list[RunInfo]:
    return [RunInfo(**row) for row in recent_runs(session, limit=limit)]
