# Job Market Intelligence

A production-shaped scraping pipeline that turns eight public job sources into one
queryable dataset: normalised, deduplicated across boards, and enriched with a
skill taxonomy and salaries parsed into comparable annual USD.

It ships as a CLI, a documented REST API, and a dashboard.

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Sources](#sources)
- [Usage](#usage): [CLI](#cli) · [API](#api) · [Dashboard](#dashboard)
- [Sample output](#sample-output)
- [Engineering notes](#engineering-notes)
- [Data quality: known limits](#data-quality-known-limits)
- [Ethics and compliance](#ethics-and-compliance)
- [Development](#development): [Testing](#testing) · [Project layout](#project-layout) · [Adding a source](#adding-a-source)
- [Licence](#licence)

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

jmi init            # create the schema, seed the skill taxonomy
jmi scrape          # crawl every enabled source (~12 min, ~4,000 postings)
jmi stats           # headline metrics
jmi serve           # dashboard at http://127.0.0.1:8000
```

### Docker

With Docker (Postgres + API + a scheduler that re-crawls every 6 hours):

```bash
docker compose up --build
```

### Deploying

See [DEPLOY.md](DEPLOY.md) to put it online for free.

## Configuration

Every setting is optional: the defaults run locally against SQLite. To change
one, copy [.env.example](.env.example) to `.env`. All variables share the
`JMI_` prefix.

| Variable | Default | Purpose |
|---|---|---|
| `JMI_DATABASE_URL` | `sqlite:///data/jobs.db` | Storage. Point it at PostgreSQL with `postgresql+psycopg://…` (install the `postgres` extra) |
| `JMI_USER_AGENT` | project placeholder | Identifies the crawler. Set a real contact address before running |
| `JMI_RATE_LIMIT_PER_SECOND` | `0.75` | Per-domain request rate |
| `JMI_RESPECT_ROBOTS` | `true` | Evaluate `robots.txt` before crawling a host |
| `JMI_ENABLED_SOURCES` | all eight | Which scrapers `jmi scrape` runs |
| `JMI_GREENHOUSE_BOARDS`, `JMI_LEVER_BOARDS` | see `.env.example` | Company boards to crawl |
| `JMI_SCRAPE_INTERVAL_MINUTES` | `360` | Scheduler interval |
| `JMI_STALE_AFTER_DAYS` | `45` | Age at which unseen postings are retired |
| `JMI_ARCHIVE_RAW` | `true` | Keep raw payloads for offline replay |

Per-source caps (`JMI_HN_THREADS`, `JMI_MUSTAKBIL_MAX_JOBS`,
`JMI_HIMALAYAS_MAX_JOBS`, `JMI_ARBEITNOW_PAGES`, …) are listed in
[.env.example](.env.example).

## Architecture

```
              RemoteOK   WeWorkRemotely   HN "Who is hiring"   Greenhouse boards
   sources    Lever      Mustakbil (PK)   Himalayas            Arbeitnow (EU)
              JSON API · RSS · free text · sitemap+JSON-LD · cursor-paginated JSON
                                                       │
                    ┌──────────────────────────────────▼───────────────────────────────────┐
   crawl layer      │  robots.txt · per-domain rate limit · conditional GET (ETag/304)     │
                    │  retry + exponential backoff + jitter · raw payload archive          │
                    └──────────────────────────────────┬───────────────────────────────────┘
                                                       │  RawJob
                    ┌──────────────────────────────────▼───────────────────────────────────┐
   enrichment       │  normalise ─→ seniority · employment type · remote · country/region  │
                    │  salary    ─→ free text → annual USD (FX + period conversion)        │
                    │  skills    ─→ 132-term taxonomy, alias-aware, ambiguity-safe         │
                    │  dedupe    ─→ identity key, then SimHash + title similarity          │
                    └──────────────────────────────────┬───────────────────────────────────┘
                                                       │
                    ┌──────────────────────────────────▼───────────────────────────────────┐
   storage          │  SQLite (default) or PostgreSQL, same code, one env var            │
                    │  jobs · companies · skills · job_skills · scrape_runs                │
                    └──────────────────────────────────┬───────────────────────────────────┘
                                                       │
                    ┌──────────────────┬───────────────┴─────────┬──────────────────────────┐
   surfaces         │   FastAPI JSON   │   Dashboard (Chart.js)  │   CLI (typer + rich)     │
                    └──────────────────┴─────────────────────────┴──────────────────────────┘
```

## Sources

### Why these eight

Each one breaks in a different way, which is the point. A scraper that only
handles clean JSON hasn't been tested.

| Source | Format | What makes it interesting |
|---|---|---|
| **RemoteOK** | JSON API | The first array element is a legal notice, not a job. Uses `0` for "salary undisclosed". |
| **We Work Remotely** | RSS/XML | Packs company and role into one `Company: Role` string; carries non-standard sibling tags. |
| **HN "Who is hiring?"** | Free text | No schema at all. Human-written pipe-delimited headers with inconsistent field order, and replies mixed in with actual postings. |
| **Greenhouse boards** | JSON API | First-party, high quality, HTML-escaped bodies; one request per company board. |
| **Mustakbil** (Pakistan) | Sitemap + JSON-LD | No feed at all: the sitemap says what to crawl, and each page embeds a schema.org `JobPosting`. Salaries arrive as `PKR …/MONTH`. |
| **Lever** | JSON API | The other big ATS. Same job, completely different field names to Greenhouse: `text` not `title`, nested `categories`, millisecond timestamps. |
| **Himalayas** | Cursor JSON API | ~100k remote roles with real salary numbers, currency and period, which lifts the disclosure rate. |
| **Arbeitnow** (Europe) | Paginated JSON | Geographic ballast. Without European postings every salary aggregate skews to one market. |

### Deliberately left out

Two well-known boards were evaluated and rejected, which matters more than the
list of ones included:

- **rozee.pk** (Pakistan's largest board) sits behind a Cloudflare bot
  challenge. Getting past it would mean evading bot protection, so it is out.
- **Remotive** and **Jobicy** both serve their `robots.txt` from behind the same
  challenge, so their crawl policy cannot be read at all. A crawler that cannot
  verify permission should not assume it.

Every included source has a readable, permissive `robots.txt`, or in Mustakbil's
case a sitemap that explicitly invites crawling.

## Usage

### CLI

```bash
jmi init                        # schema + skill taxonomy
jmi scrape                      # crawl all enabled sources
jmi scrape -s hackernews        # crawl one source
jmi reprocess                   # re-enrich stored postings (no network)
jmi stats                       # corpus overview, source split, pay by seniority
jmi skills --limit 20           # ranked skill demand with average salary
jmi skills --category ml        # filter to one category
jmi runs                        # crawl history: requests, bytes, failures, timing
jmi export --format csv         # dump to CSV/JSON for pandas or Excel
jmi serve                       # API + dashboard
jmi schedule --every 360        # crawl on a timer
jmi db check                    # connect to the configured database and report what is there
jmi db copy --to <url>           # copy the corpus to another database (e.g. SQLite to Postgres)
jmi db reset --yes              # drop and recreate
```

### API

Interactive docs at `/docs` (OpenAPI is generated from the response models).

| Endpoint | Returns |
|---|---|
| `GET /api/jobs` | Paged search filtered by skill (AND), source, seniority, country, remote, salary disclosed, recency. Pass `have=` to score each posting against a profile and `sort=match` to rank by it |
| `GET /api/jobs/{id}` | One posting with extracted skills and its cross-posts on other boards |
| `GET /api/analytics/overview` | Headline counters |
| `GET /api/analytics/skills` | Ranked demand with the salary attached to each skill |
| `GET /api/analytics/skills/{slug}/cooccurrence` | What that skill is paired with |
| `GET /api/analytics/trends` | Demand per week, one series per skill |
| `GET /api/analytics/salary/seniority` | Pay bands |
| `GET /api/analytics/breakdown/{dimension}` | Counts by source, country, region, seniority, company |
| `GET /api/analytics/skill-gap` | Marginal analysis: which skill unlocks the most jobs |
| `GET /api/analytics/momentum` | Rising and falling skills across two windows |
| `GET /api/analytics/salary/distribution` | Percentiles and histogram |
| `GET /api/skills` | The full skill taxonomy |
| `GET /api/analytics/runs` | Crawl audit trail |
| `GET /api/health` | Liveness + row count |

Every filter applies to the analytics endpoints too, so
`/api/analytics/skills?remote_only=true&country=Germany` answers "what do remote
German employers ask for" without any new code.

### Dashboard

Served at `/` by `jmi serve`. Six sections behind a nav bar, not one endless page.

| Section | What it answers |
|---|---|
| **Overview** | Headline metrics, plus which skills are gaining and losing ground |
| **Jobs** | Search the corpus, apply on the source site, save what you like |
| **Skills** | Demand, pay and co-occurrence: what a skill travels with |
| **Compensation** | Percentile bands and the distribution shape |
| **Skill Gap** | What to learn next, given what you already know |
| **Pipeline** | Crawl telemetry: what ran, what it fetched, what it cost |

#### The part that is not just another dashboard

Most job dashboards describe the market. These three answer *"what does it mean
for me"*, which is the question a job seeker actually has.

**Skill gap analysis**: enter your stack and it runs a marginal analysis. For
every skill you lack, how many *additional* postings would you qualify for by
learning that one thing? Not raw popularity: the most common skill in the
corpus is useless advice if you already have it. Each recommendation carries the
salary of the jobs it unlocks and the sample size behind it.

```
Profile: python, sql            1,562 postings analysed
                                you qualify for 51 (3.3%) · 760 within two skills

  learn Salesforce     unlocks +73 jobs   avg $223,831
  learn Security       unlocks +33 jobs   avg $241,408
  learn System Design  unlocks +30 jobs   avg $311,578
```

**Match scoring**: every posting is scored against your stack (share of its
required skills you cover) and the job list can be ranked by it.

**Skill momentum**: what is rising and falling, measured on *posting dates*
rather than crawl time. Crawl time would make every skill appear to spike on the
day the crawler first ran. Raw counts ship next to the percentage, because
"+800%" from a base of one is not a trend.

Everything else is table stakes: shared filters that drive the charts *and* the
analytics endpoints, saved jobs, CSV export, debounced search, pagination,
keyboard shortcuts, and a light/dark theme that persists.

## Sample output

Real numbers from one crawl of all eight sources:

```
$ jmi stats
 Active postings (deduplicated)     3,385
 Distinct companies                 1,073
 Remote                       1,306 (39%)
 Salary disclosed                937 (28%)
 Average salary (midpoint)       $203,988
 Cross-post duplicates merged   233 (6.4%)

 Source           Postings          Source           Postings
 greenhouse          1,578          arbeitnow             416
 hackernews            454          himalayas             241
 lever                 450          remoteok               99
 weworkremotely         87          mustakbil              51

$ jmi skills --limit 6
  #  Skill           Category   Postings   Share   Avg salary
  1  Python          language        504   14.9%     $231,713
  2  AWS             cloud           314    9.3%     $227,149
  3  SQL             language        309    9.1%     $202,595
  4  Salesforce      tool            280    8.3%     $169,151
  5  System Design   practice        248    7.3%     $240,734
  6  TypeScript      language        248    7.3%     $219,637
```

Salaries skew high because Greenhouse boards are weighted toward large US tech
employers, which is a sampling bias rather than a market finding. This is exactly why sample
sizes ship with every aggregate.

## Engineering notes

The scraping is the easy part. These are the problems that took the real work,
each solved in one focused module:

### Cross-source deduplication ([`pipeline/dedupe.py`](src/jmi/pipeline/dedupe.py))

The same role appears on RemoteOK, We Work Remotely *and* the company's own
Greenhouse board. Counting it three times inflates every demand number. Two passes:

1. **Identity key**: `company + normalised title + location bucket`, hashed and
   indexed. Catches the common case with one indexed lookup.
2. **Near-duplicate**: SimHash of the body (Hamming distance ≤ 10) combined with
   Jaccard title overlap (≥ 0.80), scoped to the same company *and* the same
   location. That last constraint matters: without it, one company's identical
   job ad in Berlin and Bengaluru collapses into a single row.

Duplicates keep their own row and point at the canonical one via
`duplicate_of_id`, so "which boards carried this role" is still answerable and
nothing is destroyed.

### Salary parsing ([`pipeline/salary.py`](src/jmi/pipeline/salary.py))

Compensation is the most valuable field and the worst formatted. All of these
resolve to a comparable annual USD range:

```
"$170-240K + equity"    "€80,000 - €100,000 per year"    "$75/hr"
"$160-180k"             "up to $200K"                    "£45,000 - £55,000 DOE"
```

Precision matters more than recall, because one bad parse poisons an average.
Every one of these was found producing a nonsense salary in the live corpus and
is now a regression test:

| Text in a real posting | Naive result | Why it's rejected |
|---|---|---|
| `$2M pre-seed backed by founders of Ramp` | $2,000,000 | funding, not pay |
| `manage this $1M+/year BPO business unit` | $1,000,000 | business revenue |
| `$1 minimum payout threshold` | $1,000,000 | the **m** of "minimum" was read as the millions suffix |

**Two plausibility floors, because trust differs.** A number scraped out of prose
needs a high floor ($8,000/year) to stay precise. A number the source published
as a structured salary field is trustworthy and gets a low one ($300/year), because
holding it to a US-shaped floor would silently discard *every* salary from a
lower-income market. PKR 25,000/month is a real Pakistani wage worth about
$1,080 a year, and it belongs in the dataset.

### Ambiguous skill names ([`pipeline/skills.py`](src/jmi/pipeline/skills.py))

"Go", "R", "C" and "Rust" are programming languages *and* ordinary English.
A naive keyword match tags "Come **rust**-proof our **go**-to-market strategy"
as a systems role. Those terms require an explicit qualifier ("Golang", "Go
developer", "R programming"), while word boundaries are tuned so `C++`, `C#`,
`.NET` and `Node.js` match but `Java` never matches inside `JavaScript`.

Skill matching runs a **two-stage matcher**: a cheap literal substring probe
rejects the ~130 skills that cannot possibly match before any regex runs. On a
14 KB description that took extraction from **545 ms to 12 ms, a 44× speedup**,
with byte-identical output.

One more subtlety: scraping Figma's own board made *Figma* the most in-demand
skill in the dataset, because every Figma posting mentions Figma. Employer
self-mentions are now suppressed.

### Being a well-behaved crawler ([`scrapers/base.py`](src/jmi/scrapers/base.py))

- **robots.txt** is fetched and evaluated per host before the first request, and
  a `Crawl-delay` directive tightens the rate limiter automatically (RemoteOK
  asks for 1 s, and the crawler honours it without being told).
- **Per-domain token bucket** so concurrency across sources never means
  concurrency against one host.
- **Conditional GET**: ETag/`Last-Modified` are stored and replayed, so a
  re-crawl of unchanged boards transfers *zero bytes* and returns `304`.
- **Retry** with exponential backoff and jitter, honouring `Retry-After`.
- **Raw payload archive** so a parser change can be replayed offline against the
  exact bytes that caused a bug.

### Ingest that survives bad data ([`pipeline/ingest.py`](src/jmi/pipeline/ingest.py))

- Each posting is written inside a **SAVEPOINT**. One malformed row rolls back
  only itself instead of discarding the whole batch.
- Runs are **idempotent**: re-crawling immediately creates nothing and only
  updates `last_seen_at`.
- Postings that disappear are **retired, never deleted** (`is_active=False`), so
  historical trends stay correct.
- If a source returns **zero** postings, deactivation is skipped entirely, because an
  empty fetch almost always means the source broke, and wiping the dataset on a
  transient failure is far worse than carrying stale rows for one cycle.

### Fixing data quality without re-crawling

Because descriptions and raw payloads are stored, improving a parser is a local
replay rather than a re-crawl. `jmi reprocess` re-runs normalisation, salary
parsing, skill extraction and deduplication over every stored posting, with no
network requests at all.

That loop caught three false positives the first crawl made obvious, each found
by sampling the text that actually matched:

| Skill | Was ranked | What was really matching |
|---|---|---|
| **Security** | #1, 24% of postings | policy boilerplate, "our security and privacy standards" |
| **Excel** | #3, 15% | the *verb*, "candidates who **excel** at client acquisition" |
| **Apache Spark** | #4, 9.5% | "**Spark** Capital" (an investor) and "**spark** joy" |

All three now require qualifying context ("Application Security Engineer",
"Microsoft Excel", "PySpark"/"Spark SQL"). After `jmi reprocess`, Python takes
the top slot and the ranking reads like a real stack.

## Data quality: known limits

Stated plainly, because a dashboard that hides its error bars is worse than no
dashboard:

- **Salary coverage is partial.** Roughly a third of postings disclose pay, and
  US postings disclose far more often than others, so averages skew American.
  Sample sizes are returned with every salary aggregate, and bands with fewer
  than 3 disclosed salaries are suppressed rather than shown as fact.
- **FX rates are pinned**, not live (`FX_RATES_AS_OF` in `salary.py`), so results
  stay reproducible. A production deployment would refresh them on a schedule.
- **Seniority is unstated in about half of titles.** It is reported as
  `unspecified` rather than guessed.
- **Deduplication is deliberately conservative.** It prefers missing a duplicate
  over merging two genuinely different roles, so the true duplicate rate is
  slightly higher than reported.
- **Skill extraction is lexical, not semantic.** It reads what a posting *says*,
  which is not always what the job requires.

## Ethics and compliance

- `robots.txt` is respected on every host, including `Crawl-delay`.
- Requests are rate limited per domain and retry politely on `429`/`5xx`.
- The User-Agent identifies the project and carries a contact address, so set
  yours in `.env` before running.
- Only **public** postings are collected. No accounts, no logins, no paywalls, no
  personal data beyond what an employer published as part of an advert.
- Sources are credited in the dashboard footer and via `/api/sources`. RemoteOK's
  API terms ask consumers to link back, and every stored row keeps its canonical
  source URL so that attribution survives.

## Development

### Testing

```bash
pytest              # 152 tests
```

The suite covers the parts that actually break: salary edge cases (including the
three real-world false positives above), skill ambiguity traps, seniority
priority (`Senior Engineering Manager` is a *manager*), location parsing,
duplicate detection, savepoint isolation, idempotency, retirement semantics, and
the full API contract.

### Project layout

```
src/jmi/
├─ config.py             settings (env-driven, one prefix)
├─ models.py             SQLAlchemy schema
├─ db.py                 engine/session, SQLite ⇄ Postgres
├─ utils.py              slugify, SimHash, HTML→text
├─ scrapers/
│  ├─ base.py            robots, rate limiting, conditional GET, retry
│  ├─ remoteok.py  weworkremotely.py  hackernews.py  greenhouse.py
│  ├─ lever.py  mustakbil.py  himalayas.py  arbeitnow.py
│  └─ registry.py        add a source = one class + one line
├─ pipeline/
│  ├─ normalize.py       seniority · employment type · remote · location
│  ├─ salary.py          free text → annual USD
│  ├─ skills.py          132-term taxonomy + two-stage matcher
│  ├─ dedupe.py          identity key + SimHash near-duplicates
│  └─ ingest.py          orchestration, savepoints, retirement
├─ analytics/
│  ├─ queries.py         every aggregate, duplicate-aware
│  └─ advisor.py         match scoring · skill gap · momentum · percentiles
├─ api/                  FastAPI routes, schemas, dashboard
├─ cli.py                typer + rich
└─ scheduler.py          APScheduler, non-overlapping runs
```

### Adding a source

Implement `fetch()` and register the class. The rest of the pipeline
(normalisation, salary, skills, dedup, storage, API, dashboard) applies
automatically:

```python
class MyBoardScraper(BaseScraper):
    name = "myboard"
    display_name = "My Board"

    async def fetch(self, client: HttpClient) -> list[RawJob]:
        payload = await client.get_json("https://example.com/api/jobs")
        return [
            RawJob(
                source=self.name,
                source_job_id=str(entry["id"]),
                title=entry["title"],
                company=entry["company"],
                url=entry["url"],
                description_html=entry.get("body"),
            )
            for entry in payload["jobs"]
        ]
```

Then add it to `SCRAPER_CLASSES` in `scrapers/registry.py`.

## Licence

MIT. Scraped content belongs to its original publishers.
