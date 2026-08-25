"""API contract tests against a seeded database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmi.pipeline.ingest import store_raw_jobs

from .conftest import make_raw_job


@pytest.fixture
def client(session, settings) -> TestClient:
    """A client backed by a small, known corpus."""
    store_raw_jobs(
        session,
        "boardA",
        [
            make_raw_job(source="boardA", source_job_id="1", title="Senior Backend Engineer",
                         company="Acme Inc.", location="Berlin, Germany"),
            make_raw_job(source="boardA", source_job_id="2", title="Frontend Engineer",
                         company="Globex", location="Remote",
                         description_text="React and TypeScript work. Salary: $120,000 - $140,000."),
            make_raw_job(source="boardA", source_job_id="3", title="Marketing Intern",
                         company="Initech", location="Austin, TX",
                         description_text="Support campaigns. No salary listed."),
        ],
        settings,
    )
    # Cross-posted copy of posting 1 - must not be counted twice.
    store_raw_jobs(
        session, "boardB",
        [make_raw_job(source="boardB", source_job_id="9", title="Senior Backend Engineer",
                      company="Acme Inc.", location="Berlin, Germany")],
        settings,
    )
    session.commit()

    from jmi.api.main import app

    return TestClient(app)


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["total_jobs"] >= 3


def test_dashboard_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Job Market Intelligence" in response.text


def test_openapi_schema_is_valid(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/jobs" in schema["paths"]
    assert "/api/analytics/skills" in schema["paths"]


def test_job_listing_excludes_duplicates(client: TestClient) -> None:
    body = client.get("/api/jobs").json()
    assert body["total"] == 3  # four rows stored, one is a cross-post

    with_dupes = client.get("/api/jobs", params={"include_duplicates": True}).json()
    assert with_dupes["total"] == 4


def test_overview_reports_the_merged_duplicate(client: TestClient) -> None:
    body = client.get("/api/analytics/overview").json()
    assert body["total_jobs"] == 3
    assert body["duplicates_detected"] == 1


def test_skill_filter_uses_and_semantics(client: TestClient) -> None:
    react_only = client.get("/api/jobs", params=[("skill", "react")]).json()
    assert react_only["total"] == 1

    impossible = client.get("/api/jobs", params=[("skill", "react"), ("skill", "docker")]).json()
    assert impossible["total"] == 0


def test_remote_and_salary_filters(client: TestClient) -> None:
    remote = client.get("/api/jobs", params={"remote_only": True}).json()
    assert remote["total"] == 1
    assert remote["items"][0]["is_remote"] is True

    paid = client.get("/api/jobs", params={"with_salary_only": True}).json()
    assert all(j["salary_min_usd"] or j["salary_max_usd"] for j in paid["items"])


def test_search_matches_company_and_title(client: TestClient) -> None:
    assert client.get("/api/jobs", params={"search": "globex"}).json()["total"] == 1
    assert client.get("/api/jobs", params={"search": "intern"}).json()["total"] == 1


def test_pagination(client: TestClient) -> None:
    first = client.get("/api/jobs", params={"limit": 1, "offset": 0}).json()
    second = client.get("/api/jobs", params={"limit": 1, "offset": 1}).json()
    assert first["total"] == second["total"] == 3
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_job_detail_includes_skills_and_cross_posts(client: TestClient) -> None:
    listing = client.get("/api/jobs", params={"search": "Senior Backend"}).json()
    job_id = listing["items"][0]["id"]

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert {s["slug"] for s in detail["skills"]} >= {"python", "postgresql"}
    assert len(detail["also_posted_on"]) == 1
    assert detail["also_posted_on"][0]["source"] == "boardB"


def test_missing_job_returns_404(client: TestClient) -> None:
    assert client.get("/api/jobs/999999").status_code == 404


def test_skills_endpoint_ranks_by_demand(client: TestClient) -> None:
    rows = client.get("/api/analytics/skills", params={"limit": 10}).json()
    assert rows
    counts = [row["job_count"] for row in rows]
    assert counts == sorted(counts, reverse=True)


def test_trends_returns_one_series_per_skill(client: TestClient) -> None:
    body = client.get(
        "/api/analytics/trends", params=[("skill", "python"), ("skill", "react"), ("days", 90)]
    ).json()
    assert set(body["series"]) == {"python", "react"}
    assert len(body["labels"]) == len(body["series"]["python"])


def test_breakdown_rejects_unknown_dimension(client: TestClient) -> None:
    assert client.get("/api/analytics/breakdown/source").status_code == 200
    assert client.get("/api/analytics/breakdown/nonsense").status_code == 400


def test_sources_endpoint_reports_attribution(client: TestClient) -> None:
    rows = client.get("/api/sources").json()
    assert {row["name"] for row in rows} == {
        "remoteok", "weworkremotely", "hackernews", "greenhouse", "lever",
        "mustakbil", "himalayas", "arbeitnow",
    }
    assert all(row["attribution"] for row in rows)
