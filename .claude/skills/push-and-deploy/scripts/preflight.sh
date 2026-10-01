#!/usr/bin/env bash
#
# Pre-push checks for jivo_auth. Every check runs even if an earlier one
# fails, so one run shows everything that needs fixing.
#
#   1. migrations match the models      (makemigrations --check)
#   2. service test suite               (needs the local PostgreSQL from .env)
#   3. jivo-auth-client package tests   (SQLite, no PostgreSQL)
#   4. OpenAPI schema                   (spectacular --validate --fail-on-warn)
#   5. leak scan of what the push would publish (public repository)
#
# Exit status: 0 if everything passed, 1 otherwise.

set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
here="$(dirname "$(readlink -f "$0")")"

results=()
failed=0

check() {
    local name="$1"; shift
    printf '\n==> %s\n' "$name"
    local start=$SECONDS

    if "$@"; then
        results+=("PASS  $name ($((SECONDS - start))s)")
    else
        results+=("FAIL  $name ($((SECONDS - start))s)")
        failed=1
    fi
}

check "migrations match the models" \
    uv run python manage.py makemigrations --check --dry-run
check "service tests" \
    uv run python manage.py test
check "client package tests" \
    uv run pytest -q packages/jivo-auth-client
check "OpenAPI schema" \
    uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null
check "leak scan (public repository)" \
    python3 "$here/leak_scan.py"

printf '\n==> Summary\n'
printf '  %s\n' "${results[@]}"

exit "$failed"
