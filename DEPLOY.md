# Deploying free on Neon + Render

Three pieces, three homes. They have different requirements and no free tier
gives you all three in one box:

| Piece | Where | Why there |
|---|---|---|
| **Database** (~45 MB) | **Neon** free Postgres | survives restarts, ~0.5 GB free |
| **Web service** (API + dashboard) | **Render** free web service | stateless reader, no card needed |
| **Crawler** (runs on a timer) | **GitHub Actions** | Render's workers and cron jobs are paid |

The web service never crawls. That keeps it inside the free memory budget, makes
restarts instant, and is the right split regardless of hosting.

---

## 1. Database: Neon

1. Sign up at [neon.tech](https://neon.tech) and create a project.
2. Copy the connection string from the dashboard. It looks like:

```
postgresql://user:pass@ep-xxx-yyy.eu-central-1.aws.neon.tech/neondb?sslmode=require
```

3. **Change the scheme** so SQLAlchemy uses psycopg 3. This is the single most
   common mistake:

```
postgresql+psycopg://user:pass@ep-xxx-yyy.eu-central-1.aws.neon.tech/neondb?sslmode=require
```

Keep `?sslmode=require`. Neon rejects unencrypted connections.

### Verify before going further

```bash
export JMI_DATABASE_URL="postgresql+psycopg://...neon.tech/neondb?sslmode=require"
jmi db check
```

A working connection prints the Postgres version and the table list. A broken
one tells you which part failed, with the password masked so it is safe to paste
into a search. Do this now: finding a bad connection string here takes one
command, finding it after deploying means reading container logs.

### Load the data

Deploying creates the tables but leaves them **empty**, which is the single most
common surprise: the site comes up perfectly and shows zeros everywhere.

If you already have a local corpus, copy it rather than re-crawling:

```bash
jmi db copy --to "postgresql+psycopg://...neon.tech/neondb?sslmode=require"
```

That moves every table and preserves row ids, so cross-post duplicate links and
skill associations survive intact. It also realigns Postgres identity sequences
afterwards, without which the next crawl would collide with existing primary
keys.

Starting from nothing instead:

```bash
export JMI_DATABASE_URL="postgresql+psycopg://...neon.tech/neondb?sslmode=require"
jmi init      # create the tables
jmi scrape    # ~12 minutes across all eight sources
```

Either way, confirm before moving on:

```bash
jmi db check  # should report the row count, not zero
```

Run this from your own machine. There is no reason to make the first load happen
in CI.

---

## 2. Web service: Render

The repo has a [`render.yaml`](render.yaml) blueprint, so you do not have to
click through settings.

1. Push the repo to GitHub.
2. On [render.com](https://render.com): **New > Blueprint**, select the repo.
3. Render reads `render.yaml` and asks for **one** value:
   - `JMI_DATABASE_URL` - the Neon string from step 1
4. Deploy. First build takes a few minutes.

If you would rather not use the blueprint: **New > Web Service**, runtime
**Docker**, plan **Free**, health check path `/api/health`, and add that single
environment variable by hand.

### Why only one variable

`JMI_USER_AGENT` is not needed here. It is read in exactly one place, when
building an HTTP client to crawl with, and the web service never crawls. It
belongs on the GitHub Actions workflow, which is where it already is.

Everything else has a working default. Verified by booting the app with nothing
but `JMI_DATABASE_URL` in the environment and hitting every route.

**One footgun:** if you forget the variable entirely, the container falls back to
its built-in SQLite default and starts perfectly, serving an empty dashboard.
Check `/api/health` after deploying. It reports which backend is live:

```json
{"status":"ok","database":"postgresql","total_jobs":3385}
```

If it says `"database":"sqlite"`, the variable did not reach the service.

**If it says `postgresql` but `total_jobs` is 0**, the connection is fine and the
database is simply empty. Load it with `jmi db copy` or `jmi scrape` as above.
The dashboard renders zeros rather than an error because an empty corpus is a
valid state, not a failure.

### What to expect on the free plan

Free web services **spin down after about 15 minutes idle**. The next request
takes 30 to 60 seconds while the container wakes. Neon's compute also suspends
when idle and takes a second or two to resume, so a cold visit can be slow.

If the link is going on a CV, keep it warm with a free uptime monitor
(UptimeRobot, cron-job.org) hitting `/api/health` every 10 minutes. The free
plan allows 750 instance-hours a month and a month is about 730 hours, so one
always-on service fits, but only one.

---

## 3. Crawler: GitHub Actions

Render's cron jobs and background workers are paid, so the schedule lives in
[`.github/workflows/crawl.yml`](.github/workflows/crawl.yml). It is already
written.

1. Repo **Settings > Secrets and variables > Actions**.
2. New repository secret: `DATABASE_URL` = the same Neon string.
3. Optionally add a `CONTACT_EMAIL` variable so the crawler identifies you.

It runs daily at 04:17 UTC and has a manual **Run workflow** button. Public
repos get unlimited Actions minutes.

The workflow drops `JMI_RATE_LIMIT_PER_SECOND` to 0.5 because datacentre IPs get
rate limited harder than home connections.

### Or skip CI entirely

Refreshing the data is one command from your laptop:

```bash
JMI_DATABASE_URL="postgresql+psycopg://..." jmi scrape
```

Perfectly reasonable for a portfolio project. The Actions workflow just means
the numbers stay current without you remembering.

---

## Order of operations

```
Neon project           ->  jmi db check   (connection works)
jmi init && jmi scrape ->  jmi db check   (data is there)
Render blueprint       ->  open /api/health
GitHub secret          ->  Actions > Run workflow
```

Each step is verifiable on its own. If the site 502s after the last step, the
database was already proven good, so the problem is the service.

---

## Things that will bite you

**The scheme.** `postgresql://` uses psycopg 2, which is not installed.
SQLAlchemy needs `postgresql+psycopg://`. `jmi db check` names this explicitly
when it fails.

**64-bit integers.** Already handled, but worth knowing why it mattered: the
SimHash column had to become `BIGINT`, because Postgres `INTEGER` is 4 bytes
while SQLite sizes integers dynamically. It is the classic bug that only appears
after switching databases, and there is a test pinning it.

**Never commit the connection string.** It contains the password. `render.yaml`
uses `sync: false` precisely so the value is entered in the dashboard instead of
living in git. `.env` is already gitignored.

**Do not ship `data/`.** It holds a 42 MB database plus 180 MB of replay archive
and HTTP cache that production never reads. `.dockerignore` already excludes it.

**Do not run `jmi scrape` on the web service.** Crawling and enrichment is the
memory-hungry part, and a free instance is 512 MB.

**Free tiers change.** These limits were accurate when written. Check the plan
pages before promising anyone uptime.

## Backup

The database is the only thing not reproducible from a fresh crawl, and even it
mostly is:

```bash
pg_dump "$JMI_DATABASE_URL" > backup.sql
```
