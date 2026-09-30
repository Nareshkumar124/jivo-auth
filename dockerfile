# syntax=docker/dockerfile:1

# The Django application only; PostgreSQL runs on the host (see
# docker-compose.yml and docs/deploy-auth-service.md).

# --- Dependencies ---------------------------------------------------------
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# packages/ holds a uv workspace member, which uv needs to read the lockfile.
COPY pyproject.toml uv.lock ./
COPY packages ./packages

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Runtime --------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    WEB_CONCURRENCY=3

# Runs as this unprivileged user unless `docker run --user` / compose's
# `user:` picks another (e.g. the owner of the mounted signing key).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY . .

# The admin's static files, served by WhiteNoise. Settings refuse to load
# without secrets, so throwaway ones stand in for this one command.
RUN DEBUG=True JWT_ALGORITHM=HS256 \
    JWT_SECRET_KEY=collectstatic-only-not-a-real-secret \
    python manage.py collectstatic --noinput --verbosity 0

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]

ENTRYPOINT ["docker/entrypoint.sh"]
