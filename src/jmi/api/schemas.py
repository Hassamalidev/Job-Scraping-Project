"""Pydantic response models - the API's public contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    category: str
    matched_via: str | None = None
    confidence: float | None = None


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    company_name: str
    url: str
    apply_url: str | None = None
    location_raw: str | None = None
    country: str | None = None
    region: str | None = None
    is_remote: bool
    seniority: str | None = None
    employment_type: str | None = None
    salary_min_usd: float | None = None
    salary_max_usd: float | None = None
    salary_origin: str | None = None
    posted_at: datetime | None = None
    first_seen_at: datetime
    # Populated only when the caller supplies a skill profile.
    match_score: float | None = None
    matched_skills: int | None = None
    required_skills: int | None = None
    missing_skills: list[str] = Field(default_factory=list)


class JobDetail(JobSummary):
    description_text: str | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    duplicate_of_id: int | None = None
    skills: list[SkillRef] = Field(default_factory=list)
    also_posted_on: list[dict[str, Any]] = Field(default_factory=list)


class JobPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobSummary]


class Overview(BaseModel):
    total_jobs: int
    remote_jobs: int
    remote_share: float
    jobs_with_salary: int
    salary_disclosure_rate: float
    companies: int
    avg_salary_usd: float | None
    salary_sample_size: int
    duplicates_detected: int
    duplicate_rate: float


class SkillStat(BaseModel):
    slug: str
    name: str
    category: str
    job_count: int
    share: float
    avg_salary_usd: float | None = None
    salary_sample_size: int = 0


class TrendResponse(BaseModel):
    labels: list[str]
    series: dict[str, list[int]]
    bucket_days: int
    days: int


class SalaryBand(BaseModel):
    seniority: str
    job_count: int
    avg_salary_usd: float | None
    min_salary_usd: float | None
    max_salary_usd: float | None
    salary_sample_size: int


class BreakdownRow(BaseModel):
    key: str
    job_count: int


class CompanyStat(BaseModel):
    company: str
    job_count: int
    avg_salary_usd: float | None = None


class CooccurrenceRow(BaseModel):
    slug: str
    name: str
    job_count: int
    share: float


class RunInfo(BaseModel):
    id: int
    source: str
    status: str
    started_at: str | None
    duration_seconds: float | None
    fetched: int
    created: int
    updated: int
    duplicates: int
    failed: int
    http_requests: int
    bytes_downloaded: int
    error: str | None = None


class SourceInfo(BaseModel):
    name: str
    display_name: str
    homepage: str
    attribution: str | None = None
    job_count: int = 0


class GapRecommendation(BaseModel):
    slug: str
    name: str
    unlocks: int
    unlock_share: float
    avg_salary_usd: float | None = None
    salary_sample_size: int = 0
    total_demand: int = 0


class SkillGapResponse(BaseModel):
    analysed_jobs: int
    qualified_now: int
    qualified_share: float
    within_reach: int
    threshold: float
    profile_size: int
    recommendations: list[GapRecommendation]


class MomentumRow(BaseModel):
    slug: str
    name: str
    recent: int
    prior: int
    change: float


class MomentumResponse(BaseModel):
    window_days: int
    rising: list[MomentumRow]
    falling: list[MomentumRow]


class SalaryBucket(BaseModel):
    from_usd: int = Field(alias="from")
    to_usd: int = Field(alias="to")
    count: int

    model_config = ConfigDict(populate_by_name=True)


class SalaryDistribution(BaseModel):
    count: int
    percentiles: dict[str, float] = Field(default_factory=dict)
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    buckets: list[SalaryBucket] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    database: str
    total_jobs: int
    last_run_at: str | None = None
