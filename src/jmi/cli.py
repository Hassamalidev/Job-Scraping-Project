"""Command line interface.

    jmi init                 create tables and seed the skill taxonomy
    jmi scrape               crawl every enabled source
    jmi scrape -s remoteok   crawl one source
    jmi reprocess            re-enrich stored postings (no network)
    jmi stats                headline metrics for the stored corpus
    jmi skills --limit 20    ranked skill demand
    jmi runs                 crawl history
    jmi export --format csv  dump the corpus
    jmi serve                run the API + dashboard
    jmi schedule             run crawls on a timer
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .analytics.queries import (
    JobFilters,
    breakdown,
    overview,
    recent_runs,
    salary_by_seniority,
    top_skills,
)
from .config import get_settings
from .db import get_engine, init_db, reset_db, session_scope
from .pipeline.ingest import run_all, run_source, seed_skills
from .scrapers.registry import SCRAPERS, available_sources

# Windows consoles default to cp1252 and job titles are full of non-ASCII.
if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - platform glue
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Job Market Intelligence - scrape, enrich and analyse job postings.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, get_settings().log_level.upper(), 20),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value else "-"


@app.command()
def init() -> None:
    """Create the database schema and seed the skill taxonomy."""
    init_db()
    with session_scope() as session:
        added = seed_skills(session)
    console.print(f"[green]Schema ready.[/green] Seeded {added} new skills.")
    console.print(f"Database: [cyan]{get_settings().database_url}[/cyan]")


@app.command()
def scrape(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help=f"One of: {', '.join(available_sources())}"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Crawl sources, enrich the postings and store them."""
    _setup_logging(verbose)
    init_db()
    settings = get_settings()

    unknown = [name for name in (source or []) if name not in SCRAPERS]
    if unknown:
        console.print(f"[red]Unknown source(s):[/red] {', '.join(unknown)}")
        raise typer.Exit(code=1)

    if source:
        reports = [asyncio.run(run_source(name, settings)) for name in source]
    else:
        reports = asyncio.run(run_all(settings=settings))

    table = Table(title="Crawl results", header_style="bold cyan")
    for column in ("Source", "Fetched", "New", "Updated", "Dupes", "Retired", "Failed", "Time"):
        table.add_column(column, justify="right" if column != "Source" else "left")
    for report in reports:
        table.add_row(
            report.source, str(report.fetched), f"[green]{report.created}[/green]",
            str(report.updated), str(report.duplicates), str(report.deactivated),
            f"[red]{report.failed}[/red]" if report.failed else "0",
            f"{report.duration_seconds:.1f}s",
        )
    console.print(table)

    for report in reports:
        if report.error:
            console.print(f"[red]{report.source} failed:[/red] {report.error}")


@app.command()
def reprocess(
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Limit to these sources")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Re-run enrichment on stored postings: no network, no re-crawl.

    Use after changing the skill taxonomy, salary parser or dedup rules.
    """
    from .pipeline.ingest import reprocess_stored

    _setup_logging(verbose)
    settings = get_settings()
    with console.status("Reprocessing stored postings…"), session_scope() as session:
        report = reprocess_stored(session, settings, sources=source)

    table = Table(title="Reprocess results", header_style="bold cyan", show_header=False)
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Postings re-enriched", f"{report.updated:,}")
    table.add_row("Duplicates detected", f"{report.duplicates:,}")
    table.add_row("Skill links written", f"{report.skills_linked:,}")
    table.add_row("Failed", f"{report.failed:,}")
    console.print(table)
    for error in report.errors[:5]:
        console.print(f"[red]{error}[/red]")


@app.command()
def stats() -> None:
    """Headline metrics for the stored corpus."""
    with session_scope() as session:
        data = overview(session)
        if not data["total_jobs"]:
            console.print("[yellow]No postings yet. Run 'jmi scrape' first.[/yellow]")
            raise typer.Exit()

        table = Table(title="Corpus overview", header_style="bold cyan", show_header=False)
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")
        table.add_row("Active postings (deduplicated)", f"{data['total_jobs']:,}")
        table.add_row("Distinct companies", f"{data['companies']:,}")
        table.add_row("Remote", f"{data['remote_jobs']:,} ({data['remote_share']:.0%})")
        table.add_row(
            "Salary disclosed",
            f"{data['jobs_with_salary']:,} ({data['salary_disclosure_rate']:.0%})",
        )
        table.add_row("Average salary (midpoint)", _money(data["avg_salary_usd"]))
        table.add_row(
            "Cross-post duplicates merged",
            f"{data['duplicates_detected']:,} ({data['duplicate_rate']:.1%})",
        )
        console.print(table)

        source_table = Table(title="By source", header_style="bold cyan")
        source_table.add_column("Source")
        source_table.add_column("Postings", justify="right")
        for row in breakdown(session, "source"):
            source_table.add_row(row["key"], f"{row['job_count']:,}")
        console.print(source_table)

        bands = salary_by_seniority(session)
        if bands:
            band_table = Table(title="Salary by seniority", header_style="bold cyan")
            for column in ("Seniority", "Postings", "Average", "Low", "High"):
                band_table.add_column(column, justify="right" if column != "Seniority" else "left")
            for band in bands:
                band_table.add_row(
                    band["seniority"], f"{band['job_count']:,}",
                    _money(band["avg_salary_usd"]), _money(band["min_salary_usd"]),
                    _money(band["max_salary_usd"]),
                )
            console.print(band_table)


@app.command()
def skills(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    remote_only: Annotated[bool, typer.Option("--remote-only")] = False,
) -> None:
    """Ranked skill demand, with the salary attached to each skill."""
    filters = JobFilters(remote_only=remote_only)
    with session_scope() as session:
        rows = top_skills(session, filters, limit=limit, category=category)
    if not rows:
        console.print("[yellow]No skill data yet. Run 'jmi scrape' first.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Top {len(rows)} skills", header_style="bold cyan")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Skill")
    table.add_column("Category", style="dim")
    table.add_column("Postings", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("Avg salary", justify="right")
    for index, row in enumerate(rows, start=1):
        table.add_row(
            str(index), row["name"], row["category"], f"{row['job_count']:,}",
            f"{row['share']:.1%}", _money(row["avg_salary_usd"]),
        )
    console.print(table)


@app.command()
def runs(limit: Annotated[int, typer.Option("--limit", "-n")] = 10) -> None:
    """Crawl history - the operational view."""
    with session_scope() as session:
        rows = recent_runs(session, limit=limit)
    if not rows:
        console.print("[yellow]No crawls recorded yet.[/yellow]")
        raise typer.Exit()

    table = Table(title="Recent crawls", header_style="bold cyan")
    for column in ("Started", "Source", "Status", "Fetched", "New", "Reqs", "KB", "Time"):
        table.add_column(column, justify="right" if column not in ("Started", "Source") else "left")
    for row in rows:
        status = f"[green]{row['status']}[/green]" if row["status"] == "ok" else f"[red]{row['status']}[/red]"
        table.add_row(
            (row["started_at"] or "")[:19].replace("T", " "), row["source"], status,
            str(row["fetched"]), str(row["created"]), str(row["http_requests"]),
            f"{row['bytes_downloaded'] / 1024:.0f}",
            f"{row['duration_seconds']:.1f}s" if row["duration_seconds"] else "-",
        )
    console.print(table)


@app.command()
def export(
    output: Annotated[Path, typer.Option("--out", "-o")] = Path("export/jobs.csv"),
    fmt: Annotated[str, typer.Option("--format", "-f", help="csv or json")] = "csv",
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10_000,
    include_duplicates: Annotated[bool, typer.Option("--include-duplicates")] = False,
) -> None:
    """Dump the corpus for analysis in pandas, Excel or a notebook."""
    from .analytics.queries import search_jobs

    filters = JobFilters(include_duplicates=include_duplicates)
    with session_scope() as session:
        jobs, total = search_jobs(session, filters, limit=limit, offset=0)
        rows = [
            {
                "id": job.id, "source": job.source, "title": job.title,
                "company": job.company_name, "location": job.location_raw,
                "country": job.country, "region": job.region, "is_remote": job.is_remote,
                "seniority": job.seniority, "employment_type": job.employment_type,
                "salary_min_usd": job.salary_min_usd, "salary_max_usd": job.salary_max_usd,
                "salary_origin": job.salary_origin,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "url": job.url,
                "skills": ";".join(sorted(link.skill.slug for link in job.skill_links)),
            }
            for job in jobs
        ]

    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    elif fmt == "csv":
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["id"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        console.print(f"[red]Unknown format {fmt!r}; use csv or json.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Wrote {len(rows):,} of {total:,} postings to[/green] {output}")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option("--reload")] = False,
) -> None:
    """Run the API and dashboard."""
    import uvicorn

    init_db()
    console.print(f"[green]Dashboard:[/green] http://{host}:{port}/")
    console.print(f"[green]API docs: [/green] http://{host}:{port}/docs")
    uvicorn.run("jmi.api.main:app", host=host, port=port, reload=reload)


@app.command()
def schedule(
    interval_minutes: Annotated[int | None, typer.Option("--every")] = None,
    run_now: Annotated[bool, typer.Option("--run-now/--no-run-now")] = True,
) -> None:
    """Crawl on a repeating schedule until interrupted."""
    from .scheduler import run_scheduler

    _setup_logging(False)
    minutes = interval_minutes or get_settings().scrape_interval_minutes
    console.print(f"[green]Scheduler started[/green] - crawling every {minutes} minutes. Ctrl+C to stop.")
    run_scheduler(minutes, run_immediately=run_now)


db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("check")
def db_check() -> None:
    """Connect to the configured database and report what is there.

    Run this first when pointing the app at a new Postgres. It separates
    "cannot reach the database" from "the app is broken", which is otherwise
    guesswork from a container log.
    """
    from sqlalchemy import func, inspect, select, text

    from .models import Job, ScrapeRun

    settings = get_settings()
    console.print(f"Target: [cyan]{settings.safe_database_url}[/cyan]")

    try:
        engine = get_engine()
        with engine.connect() as conn:
            backend = conn.dialect.name
            if backend == "postgresql":
                version = conn.execute(text("SHOW server_version")).scalar_one()
            else:
                version = conn.execute(text("select sqlite_version()")).scalar_one()
            tables = sorted(inspect(conn).get_table_names())
    except Exception as exc:
        console.print(f"[red]Connection failed:[/red] {type(exc).__name__}: {exc}")
        console.print(
            "Common causes: wrong password, a missing '?sslmode=require', or a "
            "'postgresql://' scheme where SQLAlchemy needs 'postgresql+psycopg://'."
        )
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Connected.[/green] {backend} {version}")

    if not tables:
        console.print("[yellow]No tables yet. Run 'jmi init'.[/yellow]")
        raise typer.Exit()

    console.print(f"Tables: {', '.join(tables)}")
    with session_scope() as session:
        jobs = session.scalar(select(func.count(Job.id))) or 0
        runs = session.scalar(select(func.count(ScrapeRun.id))) or 0
        last = session.execute(
            select(ScrapeRun.source, ScrapeRun.started_at, ScrapeRun.status)
            .order_by(ScrapeRun.started_at.desc()).limit(1)
        ).first()
    console.print(f"Postings stored: [bold]{jobs:,}[/bold]  ·  crawl runs recorded: {runs:,}")
    if last:
        console.print(f"Last crawl: {last[0]} at {str(last[1])[:19]} ({last[2]})")
    if jobs == 0:
        console.print("[yellow]Schema is ready but empty. Run 'jmi scrape'.[/yellow]")


@db_app.command("reset")
def db_reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")] = False,
) -> None:
    """Drop every table and recreate the schema. Destroys all scraped data."""
    settings = get_settings()
    if not yes:
        typer.confirm(
            f"This deletes all data in {settings.database_url}. Continue?", abort=True
        )
    reset_db()
    with session_scope() as session:
        seed_skills(session)
    console.print("[green]Database reset.[/green]")


def main() -> None:  # pragma: no cover - console script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
