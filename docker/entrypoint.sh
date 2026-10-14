#!/bin/sh
# Container start: apply migrations (unless RUN_MIGRATIONS=0), then serve.
set -eu

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    python manage.py migrate --noinput
fi

# gunicorn reads the number of workers from WEB_CONCURRENCY itself. Each
# worker serves GUNICORN_THREADS requests at once (gthread), so a dashboard
# waiting on SAP or the factory database never holds up a sign-in; with
# threads the timeout only catches a worker that stopped answering. Its
# control socket (gunicornc) would go in $HOME, the read-only /app, and
# Docker manages the process anyway, so it's off.
exec gunicorn config.wsgi:application \
    --bind "${GUNICORN_BIND:-0.0.0.0:8000}" \
    --worker-class gthread \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-30}" \
    --no-control-socket \
    --access-logfile -
