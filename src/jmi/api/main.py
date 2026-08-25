"""FastAPI application: JSON API plus the served dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, init_db
from ..models import Job, ScrapeRun
from . import routes_analytics, routes_jobs
from .schemas import HealthResponse

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Aggregated job-market analytics built from four public sources: RemoteOK,
We Work Remotely, Hacker News "Who is hiring?" and company Greenhouse boards.

Postings are normalised into one schema, deduplicated across sources, and
enriched with a skill taxonomy plus salary parsed into annual USD.

**Counts exclude cross-posted duplicates** unless `include_duplicates=true`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("database ready at %s", get_settings().safe_database_url)
    yield


app = FastAPI(
    title="Job Market Intelligence API",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # read-only public data
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(routes_jobs.router)
app.include_router(routes_analytics.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
def health(session: Session = Depends(get_db)) -> HealthResponse:
    total = session.scalar(select(func.count(Job.id)).where(Job.is_active.is_(True))) or 0
    last_run = session.execute(
        select(ScrapeRun.started_at).order_by(ScrapeRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    settings = get_settings()
    return HealthResponse(
        status="ok",
        database="postgresql" if settings.database_url.startswith("postgres") else "sqlite",
        total_jobs=int(total),
        last_run_at=last_run.isoformat() if last_run else None,
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    index = TEMPLATES_DIR / "dashboard.html"
    if not index.exists():  # pragma: no cover - packaging guard
        return HTMLResponse("<h1>Dashboard template missing</h1>", status_code=500)
    return HTMLResponse(index.read_text(encoding="utf-8"))
