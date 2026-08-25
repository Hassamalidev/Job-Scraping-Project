"""SQLAlchemy models for the job market warehouse.

Design notes
------------
* ``Job`` rows are never deleted. A posting that disappears from its source is
  marked ``is_active=False`` so historical trend queries stay correct.
* Cross-source duplicates keep their own row but point at the winner through
  ``duplicate_of_id``. That preserves "which boards carried this role" while
  analytics filters down to canonical rows only.
* ``JobSkill`` records *how* a skill was matched so extraction quality can be
  audited later instead of being an opaque blob.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300), default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Company {self.name!r}>"


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(40), index=True)

    job_links: Mapped[list[JobSkill]] = relationship(back_populates="skill")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Skill {self.slug!r}>"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_job_source_id"),
        Index("ix_jobs_posted_active", "posted_at", "is_active"),
        Index("ix_jobs_dedupe", "content_hash", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # provenance
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_job_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(600))
    apply_url: Mapped[str | None] = mapped_column(String(600), default=None)

    # identity / dedupe
    content_hash: Mapped[str] = mapped_column(String(40), index=True)
    # BigInteger, not Integer: a 64-bit SimHash overflows Postgres' 4-byte
    # INTEGER. SQLite stores it happily either way, so this only ever
    # surfaces the first time you point the app at Postgres.
    simhash: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id"), default=None, index=True
    )

    # core fields
    title: Mapped[str] = mapped_column(String(300))
    title_normalized: Mapped[str] = mapped_column(String(300), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), default=None)
    company_name: Mapped[str] = mapped_column(String(200), default="")

    # enrichment
    seniority: Mapped[str | None] = mapped_column(String(20), default=None, index=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), default=None, index=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    location_raw: Mapped[str | None] = mapped_column(String(300), default=None)
    country: Mapped[str | None] = mapped_column(String(80), default=None, index=True)
    region: Mapped[str | None] = mapped_column(String(80), default=None)

    salary_min_usd: Mapped[float | None] = mapped_column(Float, default=None)
    salary_max_usd: Mapped[float | None] = mapped_column(Float, default=None)
    salary_currency: Mapped[str | None] = mapped_column(String(8), default=None)
    salary_period: Mapped[str | None] = mapped_column(String(12), default=None)
    salary_origin: Mapped[str | None] = mapped_column(String(16), default=None)

    description_html: Mapped[str | None] = mapped_column(Text, default=None)
    description_text: Mapped[str | None] = mapped_column(Text, default=None)

    # lifecycle
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    raw: Mapped[dict | None] = mapped_column(JSON, default=None)

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    skill_links: Mapped[list[JobSkill]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    duplicate_of: Mapped[Job | None] = relationship(remote_side=[id])

    @property
    def is_canonical(self) -> bool:
        return self.duplicate_of_id is None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Job {self.source}:{self.source_job_id} {self.title!r}>"


class JobSkill(Base):
    __tablename__ = "job_skills"
    __table_args__ = (UniqueConstraint("job_id", "skill_id", name="uq_job_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    matched_via: Mapped[str] = mapped_column(String(20), default="description")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    job: Mapped[Job] = relationship(back_populates="skill_links")
    skill: Mapped[Skill] = relationship(back_populates="job_links")


class ScrapeRun(Base):
    """One execution of one scraper - the audit trail for every crawl."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(String(16), default="running")

    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)

    http_requests: Mapped[int] = mapped_column(Integer, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class DailySkillStat(Base):
    """Pre-aggregated daily rollup so dashboard charts stay fast as data grows."""

    __tablename__ = "daily_skill_stats"
    __table_args__ = (UniqueConstraint("day", "skill_id", name="uq_daily_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_salary_usd: Mapped[float | None] = mapped_column(Float, default=None)
    salary_sample_size: Mapped[int] = mapped_column(Integer, default=0)

    skill: Mapped[Skill] = relationship()
