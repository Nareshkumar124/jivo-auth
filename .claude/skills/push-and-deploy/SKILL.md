---
name: push-and-deploy
description: Ship jivo_auth. Verify the working tree (migrations, tests, client package, OpenAPI schema), scan what would go public for secrets and server details, commit on main, push to GitHub, redeploy production (auth.jivo.in) with ./deploy.sh, then smoke-test the live service with a throwaway user. Use whenever the user asks in this repo to push to main and deploy, redeploy, ship, release, "push the code and redeploy the app", "put this live" or "update production", even if they only say "deploy" or "push to main". Not for changing the deployment itself (Nginx, SSL certificates, PostgreSQL, the server's .env); DEPLOYMENT.md covers those.
---

# Push to main and redeploy

Jivo Auth is the login service every Jivo app trusts, and its GitHub repo is
**public**. Shipping has two ways to go badly wrong, and this workflow guards
against both:

- **A bad deploy logs people out of every Jivo app.** So nothing ships until
  the tests pass. `deploy.sh` checks the new image before switching, and
  rolls back if the container isn't healthy. Afterwards you check the live
  service end to end.
- **A leak can't be undone.** Anything pushed is public at once, and it stays
  in clones and caches after a revert. Server details (SSH user, host,
  paths, internal IPs, known weak points) live only in the gitignored
  `DEPLOYMENT.md` and `deploy.env`. Never copy them into tracked files,
  commit messages, or this skill.

`deploy.sh` deploys the **working tree**, not a commit. So commit everything
first, and the deployed code is exactly what's on `main`.

The scripts live in `.claude/skills/push-and-deploy/scripts/`. Run them from
the repo root. A full run takes about 2 minutes: tests ~40 s, deploy ~20 s,
smoke test ~15 s.

## 1. Orient

```bash
git fetch origin && git status --short --branch
git diff --stat; git ls-files --others --exclude-standard
```

- **Check the branch.** You should be on `main`. If `origin/main` has commits
  you don't have, someone else pushed. Stop and ask; never force-push over
  them.
- **Read the changes.** Look at the diffs, not just the file names: you need
  them for the commit message, and to notice anything odd.
- **Watch for work that isn't finished.** Other sessions sometimes edit this
  tree in parallel. The default is to ship everything in the tree, which is
  what "push the code" means. But if something looks half-done (failing
  tests, `TODO: finish`, files still changing minute to minute), ask before
  committing it.
- **If the user only asked to push,** skip steps 5–6. If they only asked to
  redeploy and the tree is clean and pushed, go straight to step 5.

## 2. Pre-flight

```bash
.claude/skills/push-and-deploy/scripts/preflight.sh
```

This runs every check and prints a summary:

- migrations match the models;
- the service test suite, which needs the local PostgreSQL from `.env`;
- the client package tests;
- the OpenAPI schema check;
- `leak_scan.py`.

**Everything must pass before you push.** If tests fail, report the failures
and stop. A red build isn't shipped to production, even if the user seems in
a hurry: that's exactly when a broken login service hurts most.

**Leak-scan findings** each need a judgment:

- **Real secrets or server details:** remove them from the change and tell
  the user. They belong in `DEPLOYMENT.md` or `deploy.env`.
- **False positives,** such as an example token in API docs or a test
  fixture: say why each one is harmless, then continue.
- **A `FORBIDDEN FILE` finding** (`.env`, `*.pem`, `deploy.env`,
  `DEPLOYMENT.md`, dumps): never commit it. Unstage it and check
  `.gitignore`.

The scan reads the server patterns from `deploy.env`: the user and host from
`DEPLOY_HOST`, `DEPLOY_DIR`, and an optional `LEAK_SCAN_EXTRA` regex. So it
can catch details without the public repo naming them.

## 3. Review new migrations

`deploy.sh` backs up the database and prints the planned migrations. But a
rollback swaps only the image, **never the schema**. List new migration files
(`git status -- '*/migrations/*'`) and read them:

- **Additive changes are safe to ship:** `AddField` with a default or
  `null=True`, `CreateModel`, and indexes or constraints on data that already
  satisfies them.
- **Destructive or incompatible changes need the user's explicit go-ahead
  first:** `RemoveField`, `DeleteModel`, `RenameField`/`RenameModel`,
  narrowing `AlterField`, `RunPython` that deletes or rewrites data, and
  constraints that existing rows might violate. After these, the previous
  image can't run against the new schema. Tell the user what the migration
  does and that undoing it means restoring the pre-deploy backup.

## 4. Commit and push

```bash
git add -A
git diff --cached --name-only | grep -E '(^|/)(\.env|deploy\.env|DEPLOYMENT\.md)$|\.pem$' && echo "STOP: sensitive file staged"
git commit -F <message-file>
git push origin main
```

- **Commit directly on `main`.** That's this repo's workflow, and the user
  asked for it.
- **Message format:** an imperative subject of about 60 characters or less
  that says what changed for users of the service. Add a body with one bullet
  per area (app or module) saying what and why, based on the diffs you read.
  End with the co-author attribution line your environment specifies.
- **Keep server details out of the message.** It's public too.
- **If the push is rejected** as non-fast-forward, fetch and look. Don't
  force-push; ask the user.

## 5. Deploy

```bash
./deploy.sh --yes
```

**Check the `Deploying:` line.** It must say `<hash> on main` with **no**
"uncommitted change(s)". If it lists uncommitted changes, something wasn't
committed: stop and find out what.

**The script:**
- syncs the tree;
- builds, and runs `check --deploy` plus `migrate --plan` with the new image;
- backs up the database;
- switches containers and waits for the health check;
- checks the public URL and the JWKS `kid`.

**Warnings that are expected:** the security warnings W005, W008 and W021.
Nginx handles the HTTPS redirect, and HSTS subdomains and preload are off on
purpose.

**Failures:**
- **Build or pre-flight fails:** nothing was switched and production is
  untouched. Fix the cause (usually a missed test or setting), commit, and
  run it again.
- **The new container doesn't become healthy:** the script rolls back on its
  own and prints the logs. Report what the logs say; don't retry in a loop.
  Any migrations the new image applied stay applied.
- **`signing key changed` warning:** every user has been logged out.
  Something replaced `jwt_private.pem` on the server. Tell the user right
  away.

Don't use `--no-backup` when migrations are pending. The backup is the only
way back.

## 6. Smoke-test production

```bash
.claude/skills/push-and-deploy/scripts/smoke_test.sh
```

This creates a throwaway superuser on the server. It then checks through the
public URL:
- redirect, health and security headers;
- JWKS;
- the full login → `/users/me/` → verify → refresh → logout cycle, with the
  token verified against the published key;
- the session records the client's public IP (proxy headers work);
- admin login, static assets and the main admin pages;
- no tracebacks in the container logs from the last 10 minutes.

The user is deleted even if a check fails. Its audit-log entries stay: it's a
security log, so leave them.

**If a check fails**, production is running the new image. Look at what
failed:
- **Core auth failures** (login, token verification, refresh) mean every Jivo
  app is affected. Recommend `./deploy.sh --rollback` at once, and do it if
  the user agrees.
- **An admin page returns 404:** the page may have been renamed on purpose.
  Update `ADMIN_PAGES` in `scripts/smoke.py` rather than rolling back.

## 7. Report

Keep it short, and lead with the outcome:

```text
Pushed <old>..<new> to main and deployed it to https://auth.jivo.in.

- Checks: <N> service tests, <M> client tests, schema, leak scan: all passed
- Migrations: <applied names, or "none">. Backup taken before switching
- Deploy: healthy in <s> s, signing key unchanged
- Smoke test: <X>/<X> passed (throwaway user deleted)
- <anything the user should know: skipped files, false positives, follow-ups>
```

## Keeping this skill current

When routes, admin pages or the deploy flow change, update `scripts/smoke.py`
(`ADMIN_PAGES`, endpoint paths) and this file in the same commit.
`DEPLOYMENT.md` (private) is the full production runbook. Update it too when
server-side setup changes, and never commit it.
