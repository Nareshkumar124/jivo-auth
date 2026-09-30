# Running auth.jivo.in

How to deploy the Jivo Auth service itself at `https://auth.jivo.in`.

Only the Django application runs in Docker. PostgreSQL and Nginx run on the
host machine:

```text
Internet ──HTTPS──► Nginx (TLS, auth.jivo.in)               ┐
                      │ HTTP                                │
                      ▼                                     │
                    127.0.0.1:8000  jivo_auth container     │  one host
                      │             gunicorn · Django       │  machine
                      ▼                                     │
                    127.0.0.1:5432  PostgreSQL (on the host)┘
```

The container uses **host networking**, so it reaches PostgreSQL at
`127.0.0.1:5432` exactly like a program on the host. PostgreSQL can keep
listening on localhost only, and gunicorn listens on `127.0.0.1:8000` for
Nginx. Neither is reachable from the network.

## First deployment

### 1. Prepare PostgreSQL on the host

Any PostgreSQL 14+ works. On Ubuntu:

```bash
sudo apt install postgresql
sudo -u postgres createuser --pwprompt jivo_auth     # choose a strong password
sudo -u postgres createdb --owner jivo_auth jivo_auth
```

The defaults are already right:

- PostgreSQL listens on `127.0.0.1`.
- It accepts password logins from `127.0.0.1`.

Don't open it to the network for this service.

### 2. Get the code

```bash
git clone https://github.com/Nareshkumar124/jivo-auth.git
cd jivo-auth
```

### 3. Create the signing key

This key signs every token. Anyone who has it can log in as any user, and
losing it logs everyone out. Generate it on the server, keep it out of Git
(`*.pem` is ignored), and back it up somewhere safe:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem
chmod 600 jwt_private.pem
```

`docker compose` mounts it into the container as a secret. The mounted file
keeps these permissions, so the container must run as a user who can read
it. Pick one:

- **Run as the key's owner** (simplest): put that user's IDs in `.env`:
  ```bash
  echo "APP_UID=$(id -u)" >> .env
  echo "APP_GID=$(id -g)" >> .env
  ```
- **Or keep the image's own user (10001)** and give it the key:
  `sudo chown 10001:10001 jwt_private.pem`.

### 4. Write `.env`

Start from `.env.example`. The production values that matter:

```bash
DEBUG=False
DJANGO_SECRET_KEY=<50+ random characters>
ALLOWED_HOSTS=auth.jivo.in
PUBLIC_URL=https://auth.jivo.in             # becomes the token issuer; must match clients' URL
CSRF_TRUSTED_ORIGINS=https://auth.jivo.in
TRUST_X_FORWARDED_PROTO=true
NUM_PROXIES=1
SECURE_HSTS_SECONDS=31536000

POSTGRES_DB=jivo_auth
POSTGRES_USER=jivo_auth
POSTGRES_PASSWORD=<the password from step 1>
POSTGRES_HOST=127.0.0.1                     # the host's PostgreSQL (host networking)
POSTGRES_PORT=5432

JWT_ALGORITHM=RS256

EMAIL_BACKEND=smtp
EMAIL_HOST=<smtp host>
EMAIL_PORT=587
EMAIL_HOST_USER=<user>
EMAIL_HOST_PASSWORD=<password>
DEFAULT_FROM_EMAIL=no-reply@jivo.in

# Browser apps that call the API directly, if any:
CORS_ALLOWED_ORIGINS=https://oms.jivo.in

# Registration is off by default in production. To allow self-sign-up
# for company addresses only:
# REGISTRATION_ENABLED=True
# REGISTRATION_EMAIL_DOMAINS=jivo.in
```

A random secret key:
`python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.

The service refuses to start if something essential is missing or unsafe:

- no `DJANGO_SECRET_KEY`
- a `PUBLIC_URL` that isn't https
- no signing key

### 5. Build and start the container

```bash
docker compose up -d --build
docker compose logs -f auth        # migrations run on every start, then gunicorn
docker compose ps                  # STATUS shows "healthy" after about 30 seconds
docker compose exec auth python manage.py check --deploy
docker compose exec auth python manage.py createsuperuser
```

`check --deploy` should report at most `security.W021`, about HSTS preload.
Preloading is optional and hard to undo.

What the image contains:

- **Base and user:** Python 3.13 slim, running as the unprivileged user
  `10001` unless `APP_UID` says otherwise.
- **Contents:** the production dependencies only, and the admin's static
  files, which WhiteNoise serves.
- **Secrets:** no `.env`, key or other secret is baked in. All of them come
  from `.env` and the mounted key at run time.
- **Health check:** `docker/healthcheck.py` calls `/api/v1/health/` every
  30 seconds.

Container settings, set in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `GUNICORN_BIND` | `127.0.0.1:8000` | Address gunicorn listens on. With host networking this is the host's address, so keep it on `127.0.0.1`. |
| `WEB_CONCURRENCY` | `3` | Number of gunicorn worker processes. |
| `RUN_MIGRATIONS` | `1` | Apply migrations on start. Set `0` to run them yourself: `docker compose run --rm --entrypoint python auth manage.py migrate` (the entrypoint ignores its arguments). |
| `APP_UID` / `APP_GID` | `10001` | User the container runs as ([step 3](#3-create-the-signing-key)). |

**Docker Desktop (macOS, Windows)** has no host networking by default.
There, remove `network_mode: host` and publish the port with
`ports: ["127.0.0.1:8000:8000"]`, and set these:

- `GUNICORN_BIND=0.0.0.0:8000`
- `POSTGRES_HOST=host.docker.internal`

PostgreSQL must then accept connections from Docker's network
(`listen_addresses` and `pg_hba.conf`).

### 6. Nginx

```nginx
server {
    listen 443 ssl;
    server_name auth.jivo.in;

    ssl_certificate     /etc/letsencrypt/live/auth.jivo.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/auth.jivo.in/privkey.pem;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name auth.jivo.in;
    return 301 https://$host$request_uri;
}
```

Why these headers matter:

- **`X-Forwarded-Proto`** tells Django the request was HTTPS
  (`TRUST_X_FORWARDED_PROTO`). Without it, admin and password-page forms
  fail the CSRF check. Nginx must *set* it, not pass through the client's
  value, which is what `proxy_set_header` does.
- **`X-Forwarded-For`** with `NUM_PROXIES=1` gives rate limiting and the
  session list the real client IP. Only the entry Nginx appends is trusted.

### 7. Check it

```bash
curl https://auth.jivo.in/api/v1/health/            # {"status": "ok", ...}
curl https://auth.jivo.in/.well-known/jwks.json     # one RSA key
```

Then open https://auth.jivo.in/admin/ and
[register the first application](register-an-application.md).

## Updating

On the server:

```bash
git pull
docker compose up -d --build       # runs new migrations
```

### Redeploying from a developer machine: `deploy.sh`

`deploy.sh` deploys the working tree of the machine it runs on, over SSH, so
the server needs no Git checkout. It needs `ssh` access to the server, and
`rsync`, `curl` and `python3` locally. Set the target once, in `deploy.env`
(gitignored):

```bash
cp deploy.env.example deploy.env   # set DEPLOY_HOST (user@server) and DEPLOY_DIR
```

```bash
./deploy.sh --dry-run     # list the files that would change on the server
./deploy.sh               # deploy (asks for confirmation; -y skips it)
./deploy.sh --rollback    # back to the image that ran before the last deploy
./deploy.sh --no-backup   # skip the pre-deploy database backup
```

A deploy stops at the first failure. These are its steps:

1. **Sync.** `rsync --delete` copies the tree into `DEPLOY_DIR`. It never sends
   `.env`, `*.pem`, `.git`, `.venv`, caches or `deploy.env`, and those files
   are never deleted on the server.
2. **Build.** It tags the running image `jivo-auth:previous`, then runs
   `docker compose build`.
3. **Pre-flight.** It runs `manage.py check --deploy` and `migrate --plan`
   with the new image while the old container keeps serving. If either fails,
   nothing is switched.
4. **Backup.** It backs up the database with `~/pg_backups/jivo_auth.sh` on
   the server, if that script exists. Use `--no-backup` to skip this step;
   without the script, the deploy stops.
5. **Switch.** It runs `docker compose up -d` and waits for the health check.
   An unhealthy container is rolled back to `previous`, and the bad image is
   tagged `jivo-auth:failed`.
6. **Verify.** It checks `DEPLOY_URL/api/v1/health/` from the local machine,
   and warns if the JWKS `kid` changed.

Each run is recorded in `DEPLOY_DIR/.deploy-history`. Rollbacks never
reverse migrations, so restore the pre-deploy backup if a migration has to
be undone.

## Operations notes

| Topic | Notes |
|---|---|
| **Backups** | Back up the host database, e.g. `pg_dump -Fc -h 127.0.0.1 -U jivo_auth jivo_auth > jivo_auth.dump` from cron, and `jwt_private.pem`. |
| **Rotating the signing key** | Replace `jwt_private.pem` and restart. Only one key is published at a time, so **every user is logged out** and applications fetch the new key on the next token. Do it only if the key may have leaked. |
| **Changing `PUBLIC_URL`** | It is the token issuer. Every application's `JIVO_AUTH["URL"]` must change at the same time. |
| **Scaling** | `WEB_CONCURRENCY` (default 3) sets the gunicorn workers. Rate-limit counters live in PostgreSQL, so they're shared between workers. |
| **Emails** | Sent while the request waits, so a slow SMTP server slows forgot-password and registration. Keep `EMAIL_TIMEOUT` low (default 10 seconds). |
| **Audit log retention** | Run `docker compose exec -T auth python manage.py prune_audit_events` daily from cron. It keeps `AUDIT_LOG_RETENTION_DAYS` (365) days. |
| **Logs** | Everything goes to stderr, including server errors and gunicorn's access log: `docker compose logs auth`. Docker rotates them at 5 × 10 MB (`logging:` in `docker-compose.yml`). |
| **Database maintenance** | PostgreSQL is outside Docker, so upgrade, tune and monitor it as usual. Django opens a new connection per request, so the container recovers by itself after a database restart. |
| **Token lifetimes** | `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` (15) is how long a revoked user can keep using an access token already issued. Keep it short. |
