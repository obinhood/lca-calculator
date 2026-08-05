# Carbon platform API — production image.
# Runs the FastAPI app under gunicorn with uvicorn workers. The frontend (frontend/) is a
# static Vite build and is served separately (CDN / static host), so it is NOT in this image.
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

# Non-root user — never run the app as root.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# DB migrations are a RELEASE step, run once per deploy (not per container / worker), to avoid
# many workers racing `alembic upgrade` on boot:
#     python -m alembic upgrade head
# Then the container serves the app. Workers overridable via WEB_CONCURRENCY.
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-4} -b 0.0.0.0:8000 --access-logfile - --error-logfile -"]
