#!/usr/bin/env bash
#
# Redeploy Jivo Auth to production (https://auth.jivo.in) from this machine.
#
#   ./deploy.sh              sync this working tree, rebuild, back up the DB, switch
#   ./deploy.sh --dry-run    only show which files would change on the server
#   ./deploy.sh --rollback   switch back to the image that ran before the last deploy
#
# Options: -y/--yes (no confirmation), --no-backup, -h/--help.
# Target: DEPLOY_HOST, DEPLOY_DIR (and DEPLOY_URL) from deploy.env (gitignored;
# copy deploy.env.example) or the environment, which takes precedence.
#
# What a deploy does, in order (docs/deploy-auth-service.md has the details):
#   1. rsync the working tree to the server. .env, *.pem and other secrets are
#      never sent, and never deleted on the server.
#   2. Tag the running image jivo-auth:previous, then `docker compose build`.
#   3. Pre-flight with the new image before switching: `check --deploy` and
#      `migrate --plan`. Failure leaves the running container untouched.
#   4. Back up the database (~/pg_backups/jivo_auth.sh): migrations run on start.
#   5. `docker compose up -d`, then wait for the health check. If the new
#      container doesn't become healthy, switch back to the previous image.
#   6. Check https://auth.jivo.in from here, and that the signing key (JWKS
#      kid) didn't change.

set -euo pipefail

# Never sent to the server. Excluded files are also never deleted there, so
# .env, jwt_private.pem and .deploy-history survive `--delete`.
RSYNC_EXCLUDES=(
    --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='*.pem'
    --exclude='__pycache__/' --exclude='*.py[cod]' --exclude='.pytest_cache/'
    --exclude='staticfiles/' --exclude='.claude/' --exclude='.idea/'
    --exclude='.vscode/' --exclude='.coverage' --exclude='htmlcov/'
    --exclude='.DS_Store' --exclude='.deploy-history' --exclude='deploy.env'
)

mode=deploy
assume_yes=0
backup=1

usage() { sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) mode=dry-run ;;
        --rollback) mode=rollback ;;
        -y|--yes) assume_yes=1 ;;
        --no-backup) backup=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then
    bold=$'\e[1m' red=$'\e[31m' green=$'\e[32m' yellow=$'\e[33m' reset=$'\e[0m'
else
    bold='' red='' green='' yellow='' reset=''
fi

step() { printf '\n%s==> %s%s\n' "$bold" "$*" "$reset"; }
warn() { printf '%sWARNING: %s%s\n' "$yellow" "$*" "$reset" >&2; }
die()  { printf '%sERROR: %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }

confirm() {
    [ "$assume_yes" = 1 ] && return 0
    [ -t 0 ] || die "Not a terminal; pass --yes to confirm."
    local answer
    read -r -p "$1 [y/N] " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Aborted."
}

for tool in ssh rsync curl python3; do
    command -v "$tool" >/dev/null || die "$tool is not installed."
done

# Run from the repository root, wherever the script is called from. Checked
# because rsync --delete mirrors this directory onto the server.
cd "$(dirname "$(readlink -f "$0")")"
[ -f manage.py ] && [ -f docker-compose.yml ] && [ -f dockerfile ] \
    || die "Run this from the jivo_auth repository (manage.py, docker-compose.yml, dockerfile)."

# Where to deploy. Kept out of Git in deploy.env; environment variables of the
# same names win over it.
env_host="${DEPLOY_HOST-}" env_dir="${DEPLOY_DIR-}" env_url="${DEPLOY_URL-}"
if [ -f deploy.env ]; then
    # shellcheck source=/dev/null
    . ./deploy.env
fi
DEPLOY_HOST="${env_host:-${DEPLOY_HOST-}}"
DEPLOY_DIR="${env_dir:-${DEPLOY_DIR-}}"
DEPLOY_URL="${env_url:-${DEPLOY_URL:-https://auth.jivo.in}}"
[ -n "$DEPLOY_HOST" ] && [ -n "$DEPLOY_DIR" ] \
    || die "Set DEPLOY_HOST and DEPLOY_DIR in deploy.env (copy deploy.env.example)."

# One SSH connection for the whole run.
ssh_dir="$(mktemp -d)"
SSH_OPTS=(-o ConnectTimeout=15 -o ServerAliveInterval=30
          -o ControlMaster=auto -o "ControlPath=$ssh_dir/%C" -o ControlPersist=120)
cleanup() {
    ssh "${SSH_OPTS[@]}" -O exit "$DEPLOY_HOST" >/dev/null 2>&1 || true
    rm -rf "$ssh_dir"
}
trap cleanup EXIT

# Arguments run in the remote shell; callers quote them with printf %q.
# shellcheck disable=SC2029
remote() { ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "$@"; }

jwks_kid() {
    curl -fsS --max-time 15 "$DEPLOY_URL/.well-known/jwks.json" 2>/dev/null \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["keys"][0]["kid"])' 2>/dev/null \
        || echo unavailable
}

check_public() {
    step "Checking $DEPLOY_URL from this machine"
    local body
    body="$(curl -fsS --max-time 15 "$DEPLOY_URL/api/v1/health/")" \
        || die "$DEPLOY_URL/api/v1/health/ did not answer 200 (the container itself is healthy; check Nginx)."
    echo "health: $body"

    local kid_after
    kid_after="$(jwks_kid)"
    if [ "$kid_before" != unavailable ] && [ "$kid_after" != "$kid_before" ]; then
        warn "The signing key changed ($kid_before -> $kid_after): every user must log in again."
    else
        echo "signing key unchanged (kid ${kid_after:0:12}...)"
    fi
}

# --- Shared remote helpers (prepended to every remote script) --------------

read -r -d '' REMOTE_LIB <<'EOF' || true
set -euo pipefail
cd "$DEPLOY_DIR"

# `docker compose run/exec` read stdin, which is this script: keep them off it.
compose() { docker compose "$@" </dev/null; }

wait_healthy() {
    local status
    for _ in $(seq 1 60); do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' jivo_auth 2>/dev/null || echo missing)"
        case "$status" in
            healthy) echo "container healthy"; return 0 ;;
            unhealthy) break ;;
        esac
        sleep 2
    done
    echo "container not healthy (last status: $status)"
    return 1
}

running_is_latest() {
    [ "$(docker inspect -f '{{.Image}}' jivo_auth 2>/dev/null)" = "$(docker image inspect -f '{{.Id}}' jivo-auth:latest)" ]
}

record() {
    printf '%s  %-9s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$1" "$2" >> .deploy-history
}
EOF

# --- Remote: deploy ----------------------------------------------------------

read -r -d '' REMOTE_DEPLOY <<'EOF' || true
[ -f .env ] || { echo "ABORT: $DEPLOY_DIR/.env is missing"; exit 1; }
[ -e jwt_private.pem ] || { echo "ABORT: $DEPLOY_DIR/jwt_private.pem is missing"; exit 1; }
chmod 750 .

restore_latest() {
    if docker image inspect jivo-auth:previous >/dev/null 2>&1; then
        docker tag jivo-auth:previous jivo-auth:latest
        echo "jivo-auth:latest points at the previous image again; the running container was not touched."
    fi
}

echo "--- build"
docker image inspect jivo-auth:latest >/dev/null 2>&1 && docker tag jivo-auth:latest jivo-auth:previous
if ! compose build --quiet; then
    echo "BUILD FAILED"; restore_latest; exit 1
fi

echo "--- pre-flight with the new image (nothing switched yet)"
# Override the entrypoint: it would migrate and start gunicorn instead.
if ! compose run --rm --no-deps -T --entrypoint python auth manage.py check --deploy --fail-level ERROR; then
    echo "PRE-FLIGHT FAILED: manage.py check"; restore_latest; exit 1
fi
if ! compose run --rm --no-deps -T --entrypoint python auth manage.py migrate --plan; then
    echo "PRE-FLIGHT FAILED: migrate --plan"; restore_latest; exit 1
fi

if [ "$BACKUP" = 1 ]; then
    echo "--- database backup"
    if [ -x "$HOME/pg_backups/jivo_auth.sh" ]; then
        "$HOME/pg_backups/jivo_auth.sh" </dev/null || { echo "BACKUP FAILED (use --no-backup to skip)"; restore_latest; exit 1; }
    else
        echo "BACKUP SCRIPT MISSING: $HOME/pg_backups/jivo_auth.sh (use --no-backup to skip)"; restore_latest; exit 1
    fi
fi

echo "--- switch (migrations run on start)"
compose up -d --no-build
if wait_healthy && running_is_latest; then
    compose logs --tail=12 auth
    # Old images of this project only; other projects' images are untouched.
    docker image prune -f --filter "label=com.docker.compose.project=jivo_auth" >/dev/null || true
    record deploy "$REVISION"
    echo "DEPLOYED"
    exit 0
fi

echo "--- NEW CONTAINER FAILED; recent logs:"
compose logs --tail=60 auth || true
if docker image inspect jivo-auth:previous >/dev/null 2>&1; then
    echo "--- rolling back to the previous image"
    docker tag jivo-auth:latest jivo-auth:failed
    docker tag jivo-auth:previous jivo-auth:latest
    compose up -d --no-build
    if wait_healthy; then
        record failed "$REVISION (rolled back; failed image tagged jivo-auth:failed)"
        echo "ROLLED BACK. The previous image is serving again."
        echo "Any migrations the new image applied were NOT reversed; the pre-deploy backup is in ~/backups/jivo_auth/."
    else
        record failed "$REVISION (rollback also unhealthy)"
        echo "ROLLBACK ALSO UNHEALTHY. Investigate now: docker compose logs auth"
    fi
fi
exit 1
EOF

# --- Remote: rollback ----------------------------------------------------------

read -r -d '' REMOTE_ROLLBACK <<'EOF' || true
docker image inspect jivo-auth:previous >/dev/null 2>&1 || { echo "ABORT: there is no jivo-auth:previous image"; exit 1; }
if [ "$(docker image inspect -f '{{.Id}}' jivo-auth:previous)" = "$(docker image inspect -f '{{.Id}}' jivo-auth:latest)" ]; then
    echo "ABORT: jivo-auth:previous is the image already running; nothing to roll back to."; exit 1
fi
docker tag jivo-auth:latest jivo-auth:rolled-back
docker tag jivo-auth:previous jivo-auth:latest
compose up -d --no-build
if wait_healthy; then
    record rollback "to $(docker image inspect -f '{{.Id}}' jivo-auth:latest | cut -c8-19) (replaced image tagged jivo-auth:rolled-back)"
    echo "ROLLED BACK. The replaced image is tagged jivo-auth:rolled-back."
    echo "Migrations applied by the replaced image were NOT reversed."
    exit 0
fi
compose logs --tail=60 auth || true
echo "ROLLBACK UNHEALTHY. Investigate now: docker compose logs auth"
exit 1
EOF

run_remote() {  # run_remote <script> [VAR=value ...]
    local body="$1" vars assignment; shift
    vars="DEPLOY_DIR=$(printf '%q' "$DEPLOY_DIR")"
    for assignment in "$@"; do
        vars+=" ${assignment%%=*}=$(printf '%q' "${assignment#*=}")"
    done
    printf '%s\n%s\n%s\n' "$vars" "$REMOTE_LIB" "$body" | remote 'bash -s'
}

# --- Main --------------------------------------------------------------------

step "Target: $DEPLOY_HOST:$DEPLOY_DIR ($DEPLOY_URL)"
remote true || die "Can't reach $DEPLOY_HOST over SSH."

if [ "$mode" = rollback ]; then
    kid_before="$(jwks_kid)"
    remote "cd $(printf '%q' "$DEPLOY_DIR") && tail -n 5 .deploy-history 2>/dev/null" || true
    confirm "Roll production back to the previous image?"
    step "Rolling back"
    run_remote "$REMOTE_ROLLBACK" || die "Rollback failed."
    check_public
    printf '\n%sRolled back.%s\n' "$green" "$reset"
    exit 0
fi

revision="working tree (not a git checkout)"
if git rev-parse --git-dir >/dev/null 2>&1; then
    dirty="$(git status --porcelain | wc -l | tr -d ' ')"
    revision="$(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
    [ "$dirty" != 0 ] && revision+=" + $dirty uncommitted change(s)"
fi
revision+=" by $(git config user.name 2>/dev/null || whoami)@$(hostname -s)"
echo "Deploying: $revision"
[ "${dirty:-0}" != 0 ] && warn "Deploying uncommitted changes. The server gets exactly this working tree."

RSYNC=(rsync -az --delete --chmod=go-w "${RSYNC_EXCLUDES[@]}" -e "ssh ${SSH_OPTS[*]}")

step "Files that will change on the server"
itemized="$("${RSYNC[@]}" --dry-run --itemize-changes ./ "$DEPLOY_HOST:$DEPLOY_DIR/")" \
    || die "rsync dry run failed."
changes="$(printf '%s\n' "$itemized" | grep -E '^(\*deleting|[<>c]f|[<>c]L)' || true)"
if [ -n "$changes" ]; then
    echo "$changes" | head -n 60
    count="$(echo "$changes" | wc -l | tr -d ' ')"
    [ "$count" -gt 60 ] && echo "... and $((count - 60)) more"
else
    echo "(none: the server already has this code; the image is rebuilt and the container refreshed if needed)"
fi

[ "$mode" = dry-run ] && { echo; echo "Dry run: nothing was changed."; exit 0; }

confirm "Deploy to production ($DEPLOY_URL)?"

kid_before="$(jwks_kid)"

step "Syncing code"
"${RSYNC[@]}" ./ "$DEPLOY_HOST:$DEPLOY_DIR/"

step "Building and switching on the server"
if ! run_remote "$REMOTE_DEPLOY" "BACKUP=$backup" "REVISION=$revision"; then
    die "Deploy failed (see above). Production is on the previous image unless it says otherwise."
fi

check_public
printf '\n%sDeployed %s%s\n' "$green" "$revision" "$reset"
