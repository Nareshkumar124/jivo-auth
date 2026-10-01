#!/usr/bin/env bash
#
# Post-deploy smoke test against production (smoke.py), with a throwaway
# superuser that's created on the server and always deleted afterwards.
# Then scans the last few minutes of container logs for tracebacks.
#
# The target comes from deploy.env (gitignored), exactly as for deploy.sh:
# DEPLOY_HOST, DEPLOY_DIR, DEPLOY_URL. Environment variables override it.
#
# The throwaway user's creation, logins and deletion stay in the audit log.
# That's expected; a security log isn't edited.

set -euo pipefail

root="$(git rev-parse --show-toplevel)"
here="$(dirname "$(readlink -f "$0")")"
cd "$root"

env_host="${DEPLOY_HOST-}" env_dir="${DEPLOY_DIR-}" env_url="${DEPLOY_URL-}"
if [ -f deploy.env ]; then
    # shellcheck source=/dev/null
    . ./deploy.env
fi
DEPLOY_HOST="${env_host:-${DEPLOY_HOST-}}"
DEPLOY_DIR="${env_dir:-${DEPLOY_DIR-}}"
DEPLOY_URL="${env_url:-${DEPLOY_URL:-https://auth.jivo.in}}"
[ -n "$DEPLOY_HOST" ] && [ -n "$DEPLOY_DIR" ] \
    || { echo "Set DEPLOY_HOST and DEPLOY_DIR in deploy.env (copy deploy.env.example)." >&2; exit 2; }

remote_dir="$(printf '%q' "$DEPLOY_DIR")"

# Django shell inside the running container; the code arrives on stdin.
django_shell() {
    ssh -o ConnectTimeout=15 "$DEPLOY_HOST" \
        "cd $remote_dir && docker compose exec -T auth python manage.py shell" 2>&1 \
        | grep -v "objects imported" || true
}

email="deploy-smoke-$(openssl rand -hex 4)@example.com"
# Letters and digits plus a fixed tail that satisfies the composition rules;
# nothing that needs quoting in Python or the shell.
password="$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | head -c 24)Aa1!"

# shellcheck disable=SC2329  # runs via the EXIT trap
cleanup() {
    printf "from users.models import User\nprint('deleted throwaway user:', User.objects.filter(email='%s').delete()[1])\n" "$email" \
        | django_shell
}

echo "==> Creating throwaway superuser $email"
printf "from users.models import User\nUser.objects.create_superuser(email='%s', password='%s')\nprint('created')\n" "$email" "$password" \
    | django_shell | grep -q created || { echo "Could not create the throwaway user." >&2; exit 1; }
trap cleanup EXIT

echo "==> Checking $DEPLOY_URL"
status=0
SMOKE_BASE="$DEPLOY_URL" SMOKE_EMAIL="$email" SMOKE_PASSWORD="$password" \
    uv run python "$here/smoke.py" || status=1

echo "==> Errors in the last 10 minutes of container logs"
errors="$(ssh -o ConnectTimeout=15 "$DEPLOY_HOST" \
    "cd $remote_dir && docker compose logs --since 10m auth 2>&1" | grep -iE "traceback|\\berror\\b|critical" || true)"
if [ -n "$errors" ]; then
    echo "$errors" | tail -n 20
    status=1
else
    echo "none"
fi

exit "$status"
