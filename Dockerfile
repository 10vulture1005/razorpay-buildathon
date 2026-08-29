FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt psycopg2-binary==2.9.9

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app
COPY app/policy/policy_config.yaml /srv/app/app/policy/policy_config.yaml

# Non-root runtime user
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv/app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/readyz')" || exit 1

# Default: API. Worker overrides the command (see docker-compose.yml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
