# Carbon platform — single-service image (API + built frontend on one origin).
# Stage 1 builds the Vite frontend; stage 2 runs the FastAPI app under gunicorn and serves the
# built frontend from the same origin, so one container is a complete, browsable deployment.

# --- Stage 1: build the frontend -----------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /fe
# Install deps against the lockfile first so this layer caches across source-only changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /fe/dist

# --- Stage 2: runtime ----------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Faster, quieter, no .pyc clutter; unbuffered logs for container log collectors.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the API needs at runtime.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY scripts ./scripts
# The built frontend from stage 1 — main.py serves it at / when this directory exists.
COPY --from=frontend /fe/dist ./frontend/dist

# Non-root user — never run the app as root. `chown /app` so appuser can CREATE files in the
# working dir: with the default (no-Postgres) config the app opens sqlite ./carbon_mvp.db here,
# and COPY runs as root, so without this appuser hits "unable to open database file" on boot.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrate + seed (idempotent: scripts.init_db upgrades always, seeds only when empty) ONCE
# before gunicorn forks workers — so there is no per-worker migration race — then serve on the
# platform-provided $PORT (Railway/Render set it; defaults to 8000 locally). For a
# horizontally-scaled deploy (many container instances) move `init_db` to a release step
# instead, so instances don't race the migration.
#
# NO --access-logfile, deliberately. The application emits its own structured request log
# (app/observability.py: method, route template, status, duration, tenant, correlation id),
# which carries strictly more than an access line. gunicorn's access log, in contrast, is
# written by uvicorn's worker as "path?query_string" verbatim — and a number of endpoints
# still take narrative, person-identifying free text as QUERY parameters, so enabling it
# would publish that text to stdout, including for requests that fail authorisation.
#
# Belt and braces: app/observability.py also disables the uvicorn/gunicorn access loggers
# at configure time (LOG_ACCESS_LOG=1 opts back in), because this flag is not the only way
# they get switched on — a bare `uvicorn app.main:app` enables access logging by default,
# and some platforms supply their own start command. The flag is dropped here anyway so
# the image does not ask for a logger the application then has to switch off.
#
# The error log stays: it carries worker/boot failures that happen outside the application.
# Tune the application log with LOG_LEVEL (default INFO), LOG_FORMAT (json|text, default
# json), LOG_LEVEL_THIRD_PARTY (default WARNING) and LOG_ACCESS_LOG (default off).
CMD ["sh", "-c", "python -m scripts.init_db && exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:${PORT:-8000} --error-logfile -"]
