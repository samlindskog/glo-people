# Build context is the repo root (needs shared/permissions.json).
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PERMISSIONS_FILE=/shared/permissions.json
WORKDIR /app
COPY api/pyproject.toml api/uv.lock ./

FROM base AS dev
RUN uv sync --frozen
COPY shared /shared
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS prod
RUN uv sync --frozen --no-dev
COPY shared /shared
COPY api/app ./app
COPY api/alembic ./alembic
COPY api/alembic.ini ./
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--timeout-graceful-shutdown", "6", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
