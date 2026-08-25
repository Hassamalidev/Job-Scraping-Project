# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

# Run as a non-root user.
RUN useradd --create-home --uid 1000 jmi \
    && mkdir -p /app/data \
    && chown -R jmi:jmi /app
USER jmi

ENV JMI_DATA_DIR=/app/data \
    JMI_DATABASE_URL=sqlite:////app/data/jobs.db

EXPOSE 8000
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=4).status==200 else 1)"

# Shell form on purpose: every free host injects its own $PORT (Hugging Face
# expects 7860, Render and Cloud Run assign one at runtime). A hardcoded port
# is the commonest reason a container boots fine and still returns 502.
CMD uvicorn jmi.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
