"""Advisory analytics - the layer that turns a corpus into personal guidance.

The rest of ``analytics`` answers "what does the market look like". This module
answers "what does the market mean *for me*", which is the part a job seeker
actually wants:

* :func:`match_scores` - how well one profile covers each posting's stack.
* :func:`skill_gap` - the marginal analysis: which single skill would qualify
  you for the most additional jobs, and what those jobs pay.
* :func:`skill_momentum` - which skills are being asked for more than they were,
  measured on posting dates rather than on when we happened to crawl.
* :func:`salary_distribution` - percentile bands, because an average alone hides
  the shape of the market.

Everything here is deliberately explainable. A recommendation a user cannot
interrogate is worthless, so each one ships with the counts it was derived from.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Job, JobSkill, Skill
from .queries import SALARY_MIDPOINT, JobFilters

# A posting is "within reach" once a profile covers this share of its stack.
DEFAULT_MATCH_THRESHOLD = 0.7

# Below this, a skill's trend is noise rather than a signal.
MIN_MOMENTUM_SAMPLE = 5


@dataclass(slots=True)
class JobSkillSet:
    job_id: int
    required: frozenset[str]
    salary: float | None

    def matched(self, have: set[str]) -> int:
        return len(self.required & have)

    def coverage(self, have: set[str]) -> float:
        if not self.required:
            return 0.0
        return self.matched(have) / len(self.required)


def _job_skill_sets(session: Session, filters: JobFilters) -> list[JobSkillSet]:
    """Required-skill set and salary midpoint for every matching posting."""
    stmt = filters.apply(
        select(Job.id, Skill.slug, SALARY_MIDPOINT.label("salary"))
        .select_from(Job)
        .join(JobSkill, JobSkill.job_id == Job.id)
        .join(Skill, Skill.id == JobSkill.skill_id)
    )

    required: dict[int, set[str]] = defaultdict(set)
    salaries: dict[int, float | None] = {}
    for job_id, slug, salary in session.execute(stmt):
        required[job_id].add(slug)
        salaries[job_id] = float(salary) if salary is not None else None

    return [
        JobSkillSet(job_id=job_id, required=frozenset(slugs), salary=salaries.get(job_id))
        for job_id, slugs in required.items()
    ]


def match_scores(
    session: Session, job_ids: list[int], have: list[str]
) -> dict[int, dict[str, Any]]:
    """Per-job coverage for a profile: matched, required, and what is missing."""
    if not job_ids:
        return {}

    have_set = {slug.lower() for slug in have}
    stmt = (
        select(JobSkill.job_id, Skill.slug, Skill.name)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .where(JobSkill.job_id.in_(job_ids))
    )

    per_job: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for job_id, slug, name in session.execute(stmt):
        per_job[job_id].append((slug, name))

    result: dict[int, dict[str, Any]] = {}
    for job_id in job_ids:
        skills = per_job.get(job_id, [])
        if not skills:
            result[job_id] = {"score": None, "matched": 0, "required": 0, "missing": []}
            continue
        matched = [name for slug, name in skills if slug in have_set]
        missing = [name for slug, name in skills if slug not in have_set]
        result[job_id] = {
            "score": round(len(matched) / len(skills), 3),
            "matched": len(matched),
            "required": len(skills),
            "missing": sorted(missing)[:8],
        }
    return result


def skill_gap(
    session: Session,
    have: list[str],
    filters: JobFilters | None = None,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    limit: int = 12,
) -> dict[str, Any]:
    """Which single skill would unlock the most additional postings.

    For every posting we already know the required stack. A profile *qualifies*
    once it covers ``threshold`` of that stack. For each skill the profile is
    missing, we ask: if this one skill were added, how many postings would cross
    the line that do not today? That marginal count - not raw popularity - is
    what makes a recommendation actionable. The most common skill in the corpus
    is useless advice if you already have it, or if the jobs needing it are out
    of reach for five other reasons.
    """
    filters = filters or JobFilters()
    have_set = {slug.lower().strip() for slug in have if slug.strip()}
    jobs = _job_skill_sets(session, filters)

    names = dict(session.execute(select(Skill.slug, Skill.name)).all())

    qualified_now = 0
    within_reach = 0
    unlocks: dict[str, int] = defaultdict(int)
    unlock_salaries: dict[str, list[float]] = defaultdict(list)
    demand: dict[str, int] = defaultdict(int)

    for job in jobs:
        coverage = job.coverage(have_set)
        already = coverage >= threshold
        qualified_now += already
        missing = job.required - have_set
        if not already and len(missing) <= 2:
            within_reach += 1

        for slug in job.required:
            demand[slug] += 1

        if already:
            continue

        # Would adding this one skill push the posting over the threshold?
        total = len(job.required)
        matched = job.matched(have_set)
        for slug in missing:
            if (matched + 1) / total >= threshold:
                unlocks[slug] += 1
                if job.salary is not None:
                    unlock_salaries[slug].append(job.salary)

    recommendations = []
    for slug, count in sorted(unlocks.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        salaries = unlock_salaries[slug]
        recommendations.append(
            {
                "slug": slug,
                "name": names.get(slug, slug),
                "unlocks": count,
                "unlock_share": round(count / len(jobs), 4) if jobs else 0.0,
                "avg_salary_usd": round(sum(salaries) / len(salaries), 0) if salaries else None,
                "salary_sample_size": len(salaries),
                "total_demand": demand.get(slug, 0),
            }
        )

    return {
        "analysed_jobs": len(jobs),
        "qualified_now": qualified_now,
        "qualified_share": round(qualified_now / len(jobs), 4) if jobs else 0.0,
        "within_reach": within_reach,
        "threshold": threshold,
        "profile_size": len(have_set),
        "recommendations": recommendations,
    }


def skill_momentum(
    session: Session,
    window_days: int = 30,
    min_sample: int = MIN_MOMENTUM_SAMPLE,
    limit: int = 8,
) -> dict[str, Any]:
    """Skills asked for more (or less) than in the preceding window.

    Measured on ``posted_at`` rather than crawl time - otherwise every skill
    would appear to spike on the day the crawler first ran.
    """
    now = datetime.now(UTC)
    recent_start = now - timedelta(days=window_days)
    prior_start = now - timedelta(days=window_days * 2)
    posted = func.coalesce(Job.posted_at, Job.first_seen_at)

    stmt = (
        select(Skill.slug, Skill.name, posted)
        .select_from(Job)
        .join(JobSkill, JobSkill.job_id == Job.id)
        .join(Skill, Skill.id == JobSkill.skill_id)
        .where(
            Job.duplicate_of_id.is_(None),
            Job.is_active.is_(True),
            posted >= prior_start,
        )
    )

    recent: dict[str, int] = defaultdict(int)
    prior: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}

    for slug, name, when in session.execute(stmt):
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        names[slug] = name
        if when >= recent_start:
            recent[slug] += 1
        else:
            prior[slug] += 1

    rows = []
    for slug in set(recent) | set(prior):
        recent_count, prior_count = recent.get(slug, 0), prior.get(slug, 0)
        if recent_count + prior_count < min_sample:
            continue
        # Guard the divide: a skill going 0 -> 12 is real, but its growth is not
        # a meaningful percentage, so it is anchored at one.
        change = (recent_count - prior_count) / max(prior_count, 1)
        rows.append(
            {
                "slug": slug,
                "name": names.get(slug, slug),
                "recent": recent_count,
                "prior": prior_count,
                "change": round(change, 4),
            }
        )

    rows.sort(key=lambda r: r["change"], reverse=True)
    return {
        "window_days": window_days,
        "rising": rows[:limit],
        "falling": [r for r in reversed(rows) if r["change"] < 0][:limit],
    }


def salary_distribution(
    session: Session, filters: JobFilters | None = None, buckets: int = 10
) -> dict[str, Any]:
    """Percentile bands and a histogram - the shape an average hides."""
    filters = filters or JobFilters()
    stmt = filters.apply(select(SALARY_MIDPOINT)).where(
        Job.salary_min_usd.isnot(None) | Job.salary_max_usd.isnot(None)
    )
    values = sorted(float(v) for (v,) in session.execute(stmt) if v is not None)

    if not values:
        return {
            "count": 0, "percentiles": {}, "buckets": [],
            "mean": None, "min": None, "max": None,
        }

    def percentile(p: float) -> float:
        if len(values) == 1:
            return values[0]
        index = min(len(values) - 1, max(0, int(round(p * (len(values) - 1)))))
        return values[index]

    percentiles = {
        "p10": round(percentile(0.10)),
        "p25": round(percentile(0.25)),
        "p50": round(percentile(0.50)),
        "p75": round(percentile(0.75)),
        "p90": round(percentile(0.90)),
    }

    # Clip the histogram to p5..p95 so a handful of outliers cannot flatten it.
    low, high = percentile(0.05), percentile(0.95)
    if high <= low:
        high = low + 1
    width = (high - low) / buckets
    counts = [0] * buckets
    for value in values:
        index = int((min(max(value, low), high) - low) / width)
        counts[min(index, buckets - 1)] += 1

    histogram = [
        {
            "from": round(low + i * width),
            "to": round(low + (i + 1) * width),
            "count": counts[i],
        }
        for i in range(buckets)
    ]

    return {
        "count": len(values),
        "percentiles": percentiles,
        "mean": round(sum(values) / len(values)),
        "min": round(values[0]),
        "max": round(values[-1]),
        "buckets": histogram,
    }
