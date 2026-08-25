"""Job search and detail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.advisor import match_scores
from ..analytics.queries import JobFilters, filter_options, search_jobs
from ..db import get_db
from ..models import Job, JobSkill, Skill
from ..scrapers.registry import SCRAPERS
from .deps import job_filters
from .schemas import JobDetail, JobPage, JobSummary, SkillRef, SourceInfo

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs", response_model=JobPage, summary="Search postings")
def list_jobs(
    filters: JobFilters = Depends(job_filters),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("recent", pattern="^(recent|salary|company|match)$"),
    have: list[str] = Query(
        default_factory=list,
        description="Skill slugs you already have; adds a match score to each posting",
    ),
    session: Session = Depends(get_db),
) -> JobPage:
    jobs, total = search_jobs(
        session, filters, limit=limit, offset=offset, sort=sort, have=have
    )
    items = [JobSummary.model_validate(job) for job in jobs]

    if have:
        scores = match_scores(session, [job.id for job in jobs], have)
        for item in items:
            score = scores.get(item.id)
            if score:
                item.match_score = score["score"]
                item.matched_skills = score["matched"]
                item.required_skills = score["required"]
                item.missing_skills = score["missing"]

    return JobPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="One posting with skills")
def get_job(job_id: int, session: Session = Depends(get_db)) -> JobDetail:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    skills = session.execute(
        select(Skill, JobSkill.matched_via, JobSkill.confidence)
        .join(JobSkill, JobSkill.skill_id == Skill.id)
        .where(JobSkill.job_id == job_id)
        .order_by(JobSkill.confidence.desc(), Skill.name)
    ).all()

    detail = JobDetail.model_validate(job)
    detail.skills = [
        SkillRef(
            slug=skill.slug, name=skill.name, category=skill.category,
            matched_via=matched_via, confidence=confidence,
        )
        for skill, matched_via, confidence in skills
    ]

    # Cross-posted siblings: rows that resolved to this canonical posting.
    canonical_id = job.duplicate_of_id or job.id
    siblings = session.execute(
        select(Job.id, Job.source, Job.url).where(
            (Job.duplicate_of_id == canonical_id) | (Job.id == canonical_id), Job.id != job_id
        )
    ).all()
    detail.also_posted_on = [
        {"job_id": sid, "source": source, "url": url} for sid, source, url in siblings
    ]
    return detail


@router.get("/filters", summary="Available filter values")
def get_filters(session: Session = Depends(get_db)) -> dict:
    return filter_options(session)


@router.get("/skills", response_model=list[SkillRef], summary="The full skill taxonomy")
def get_skill_taxonomy(session: Session = Depends(get_db)) -> list[SkillRef]:
    """Every skill the extractor knows about - used to build the profile editor."""
    rows = session.execute(
        select(Skill.slug, Skill.name, Skill.category).order_by(Skill.category, Skill.name)
    ).all()
    return [SkillRef(slug=slug, name=name, category=category) for slug, name, category in rows]


@router.get("/sources", response_model=list[SourceInfo], summary="Configured sources")
def get_sources(session: Session = Depends(get_db)) -> list[SourceInfo]:
    from sqlalchemy import func

    counts = dict(
        session.execute(
            select(Job.source, func.count(Job.id))
            .where(Job.is_active.is_(True))
            .group_by(Job.source)
        ).all()
    )
    return [
        SourceInfo(
            name=cls.name,
            display_name=cls.display_name,
            homepage=cls.homepage,
            attribution=cls.attribution,
            job_count=int(counts.get(cls.name, 0)),
        )
        for cls in SCRAPERS.values()
    ]
