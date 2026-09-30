FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# packages/ holds a uv workspace member, which uv needs to read the lockfile.
COPY pyproject.toml uv.lock ./
COPY packages ./packages

RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 8000

# WEB_CONCURRENCY sets the number of gunicorn worker processes.
ENV WEB_CONCURRENCY=3

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --access-logfile -"]
