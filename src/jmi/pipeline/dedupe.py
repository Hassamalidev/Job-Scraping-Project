"""Cross-source duplicate detection.

The same role routinely appears on RemoteOK, We Work Remotely and the company's
own Greenhouse board. Counting it three times inflates every demand number, so
duplicates are detected in two passes:

1. **Exact key** - ``company + normalised title + location bucket``. Catches the
   common case cheaply, straight off an index.
2. **Near duplicate** - for postings that survive pass 1, compare title token
   overlap and the SimHash of the body. Handles "Senior Backend Engineer" vs
   "Senior Backend Engineer (Remote)" where the text is 95% identical.

Duplicates keep their own row and point at the canonical one, so "which boards
carried this role" stays answerable.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job
from ..utils import hamming_distance

# Tuned against the live corpus: tight enough to avoid merging genuinely
# different roles at the same company, loose enough to catch reposts.
TITLE_SIMILARITY_THRESHOLD = 0.80
SIMHASH_MAX_DISTANCE = 10
NEAR_DUP_CANDIDATE_LIMIT = 400


@dataclass(slots=True)
class DuplicateVerdict:
    canonical_id: int | None
    reason: str
    score: float = 0.0

    @property
    def is_duplicate(self) -> bool:
        return self.canonical_id is not None


def location_bucket(is_remote: bool, country: str | None) -> str:
    """Coarse location identity - keeps same-title roles in different cities apart."""
    if is_remote:
        return "remote"
    return (country or "unknown").lower()


def title_similarity(left: str, right: str) -> float:
    """Jaccard overlap of title tokens."""
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def find_duplicate(
    session: Session,
    *,
    job_id: int | None,
    content_hash: str,
    company_id: int | None,
    title_normalized: str,
    simhash: int,
    is_remote: bool = False,
    country: str | None = None,
) -> DuplicateVerdict:
    """Return the canonical job this posting duplicates, if any."""

    exact_stmt = (
        select(Job)
        .where(
            Job.content_hash == content_hash,
            Job.duplicate_of_id.is_(None),
            Job.is_active.is_(True),
        )
        .order_by(Job.first_seen_at.asc())
        .limit(1)
    )
    if job_id is not None:
        exact_stmt = exact_stmt.where(Job.id != job_id)

    match = session.execute(exact_stmt).scalar_one_or_none()
    if match is not None:
        return DuplicateVerdict(canonical_id=match.id, reason="exact_key", score=1.0)

    if company_id is None or not simhash:
        return DuplicateVerdict(canonical_id=None, reason="unique")

    # Only the three columns the comparison needs - pulling whole ORM rows here
    # would drag every candidate's description text through the session.
    # Location is part of identity: the same company advertising the same title
    # in Berlin and in Bengaluru is two jobs, however similar the text is.
    near_stmt = (
        select(Job.id, Job.simhash, Job.title_normalized)
        .where(
            Job.company_id == company_id,
            Job.duplicate_of_id.is_(None),
            Job.is_active.is_(True),
            Job.simhash != 0,
            Job.is_remote.is_(is_remote),
            Job.country.is_(country) if country is None else Job.country == country,
        )
        .limit(NEAR_DUP_CANDIDATE_LIMIT)
    )
    if job_id is not None:
        near_stmt = near_stmt.where(Job.id != job_id)

    best: tuple[float, int] | None = None
    for candidate_id, candidate_simhash, candidate_title in session.execute(near_stmt):
        distance = hamming_distance(simhash, candidate_simhash)
        if distance > SIMHASH_MAX_DISTANCE:
            continue
        similarity = title_similarity(title_normalized, candidate_title)
        if similarity < TITLE_SIMILARITY_THRESHOLD:
            continue
        score = similarity * (1 - distance / (SIMHASH_MAX_DISTANCE + 1))
        if best is None or score > best[0]:
            best = (score, candidate_id)

    if best is not None:
        return DuplicateVerdict(canonical_id=best[1], reason="near_duplicate", score=round(best[0], 3))
    return DuplicateVerdict(canonical_id=None, reason="unique")
