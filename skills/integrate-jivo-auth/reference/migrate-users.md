# Phase 3: Migrate the existing users

The app's users are copied into Jivo Auth, and each local row gets its Jivo ID
in `auth_id`. Nothing else in the app changes now. One later change is
expected: Jivo Auth stores emails lowercased, and from the switch on, each
user's first token rewrites the local email to that form
(`Priya.Sharma@jivo.in` becomes `priya.sharma@jivo.in`). Rehearse the whole phase on
staging or a restored copy of production before running it on production.

```text
 app DB ──export_jivo_users──► users.json ──(Auth host) import_users <slug>──► mapping.json ──link_jivo_users──► app DB (auth_id)
         (active, no auth_id)   (has hashes)                                    (source_id → auth_id)
```

## Before you start

- Phase 2's code is deployed in this environment: the `auth_id` field and the three commands exist.
- The application exists in Jivo Auth, and `import_users` is available on the Auth host (prerequisites 5, 8 and 10).
- Decisions D1, D2, D3, D5 and D6 are recorded.
- **Back up the app's database:** `umask 077; pg_dump -Fc -f app-before-jivo-$(date +%F).dump <db>`. Confirm the file exists, has a sensible size, and lists the user table: `pg_restore -l <file> | grep "TABLE DATA"`. Backups hold every password hash, just like the export, so keep them mode 600 and off shared storage.
- **Record exact row counts of every table** (read-only), to show later that no business data changed. On a large production database, run it on the rehearsal copy or off-peak:
  ```bash
  psql <db> -XAt -c "SELECT format('SELECT %L, count(*) FROM %I.%I;', n.nspname||'.'||c.relname, n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1" > count.sql
  psql <db> -XAt -F'|' -f count.sql > rowcounts-before.txt
  ```
- **If phase 2's migration isn't applied in this environment yet**, apply it now, after the backup. ⛔ On a shared or production database, confirm first: it's a schema change other clients see. Run only that migration (`migrate <app> <migration>`), and check first with `showmigrations` that nothing else is pending. A dev server reloading the new model against a database without the column fails every user query until it's applied. `migrate` also runs the app's own `post_migrate` hooks (e.g. registries that `update_or_create` rows); report what they logged.

## 1. Export

```bash
python manage.py export_jivo_users /secure/tmp/users.json            # active users without auth_id
python manage.py export_jivo_users /secure/tmp/users.json --include-inactive   # decision D5
```

- The command refuses to write inside the repository or to overwrite a file, and creates it with mode 600. The file holds **password hashes**, so treat it like a password database.
- It prints users it didn't export (no email). Settle each one with the user (D6), for example by fixing the email in the app and exporting again.
- It also lists emails shared by several users (the import skips all of them), and how many inactive users it left out (D5). Check: exported + without email + inactive = users without `auth_id`.
- Accounts whose password must not become a Jivo credential (D1, e.g. still on a default committed by a seed command): `--without-password <pk,...>`. They are created with no password, and the dry run says "no usable password" for them, as it does for rows whose local hash was already unusable. The command refuses a primary key it wouldn't export, so a typo can't let a password through.
- To leave passwords behind (D1 = no), strip them before transferring:
  ```bash
  python -c "import json;d=json.load(open('/secure/tmp/users.json'));[u.pop('password',None) for u in d];json.dump(d,open('/secure/tmp/users-nopw.json','w'))"
  ```

## 2. Dry run on Jivo Auth

⛔ This runs a command on the Jivo Auth host. Confirm with the user, and let them transfer the file if that's how they work (`scp` to the host; never through chat or a repository). If the source is a test or staging database and the target is production Jivo Auth, restate what that means (prerequisites.md, "First": accounts are global) and the option chosen.

```bash
# from where the export is, streaming it to the Auth host: it's never written there
ssh <auth-host> "cd <auth-dir> && docker compose exec -T auth python manage.py import_users <slug> - --dry-run [--mark-verified]" \
  < users.json > mapping-dry.json

# or, on the Jivo Auth host itself, in the project directory
docker compose exec -T auth python manage.py import_users <slug> - --dry-run [--mark-verified] < users.json > mapping-dry.json
```

A dry run also confirms that `import_users` exists on the host and that the slug is accepted (prerequisites 5 and 8).

`import_users` input: a JSON list of `{source_id, email, first_name?, last_name?, password?, is_active?, employee_code?}`. It never accepts staff flags.

The output is `{"application", "dry_run", "summary": {"created", "linked", "skipped"}, "users": [{"source_id", "email", "auth_id", "status", "notes"}]}`. A dry run's output says `"dry_run": true`, and `link_jivo_users` refuses it, because its IDs don't exist. Show the user:

| Status | Meaning | Review |
|---|---|---|
| `created` | A new Jivo account is made and granted the slug | Notes say `password kept (pbkdf2_sha256)`, or `no usable password` / `password hash (<algorithm>) can't be verified here`, which means a reset or an admin-set password. Without `--mark-verified`: `email not verified: the user can't sign in until it is`. |
| `linked` | A Jivo account with that email already exists. It's granted the slug, and its password, names and status are **unchanged** | Check every one (D3): is it the same person? Notes flag an account that is deactivated or whose email isn't verified. |
| `skipped` | No valid email, the email is shared with another row in the file, or the row failed Jivo Auth's validation (the note says which field) | Settle per D6, then export again |

Before asking for the go-ahead, also show the user the addresses that aren't
anyone's real mailbox (placeholder domains, made-up internal ones). With
`--mark-verified` they become verified sign-in identities on the central
service.

A bad employee code doesn't skip the user: it's dropped with a note.

## 3. Import for real

⛔ Get the user's go-ahead on the dry-run results.

```bash
ssh <auth-host> "cd <auth-dir> && docker compose exec -T auth python manage.py import_users <slug> - [--mark-verified]" \
  < users.json > mapping.json
# or on the Auth host: docker compose exec -T auth python manage.py import_users <slug> - [--mark-verified] < users.json > mapping.json
```

- The run is atomic: all or nothing.
- It's idempotent: running again with the same file reports everyone as `linked` with the same `auth_id`.
- Every new account and grant appears in the Jivo Auth audit log, with the source "imported from an application's user list".

Bring `mapping.json` back to where the app's commands run. It holds emails and IDs, but no hashes.

Check what Jivo Auth now says, with the app's API key: `AuthClient().get_users()` should list the imported users, with the same IDs and emails as `mapping.json`.

## 4. Link in the app

```bash
python manage.py link_jivo_users /secure/tmp/mapping.json --dry-run
python manage.py link_jivo_users /secure/tmp/mapping.json          # ⛔ in production
```

For each `created`/`linked` row, it sets `auth_id` on the local user whose primary key is `source_id`. It refuses, and reports, when:

- the local user no longer exists,
- the local user already has a different `auth_id`,
- the local email changed since the export (the mapping might then point at the wrong person), or
- another local user already has that `auth_id`.

It writes nothing but `auth_id`, and it's idempotent.

## 5. Verify

```bash
python manage.py shell -c "
from django.contrib.auth import get_user_model as g; U = g()
print('linked', U.objects.filter(auth_id__isnull=False).count())
print('active, unlinked', list(U.objects.filter(is_active=True, auth_id__isnull=True).values_list('pk', 'email')))
"
```

- **The counts add up:** linked + skipped + not exported = users in scope.
- **Spot checks:** a few users in Jivo Auth admin → Users. They show the application under **Application access**.
- **Business data unchanged:** run `count.sql` again and `diff` the result against `rowcounts-before.txt`. On a quiet database expect only `django_migrations` to differ, by one row per applied migration. On a live or shared one, sessions, logs and business tables move with traffic: attribute each difference (the app's logs, the tables' timestamps) before calling it unrelated. `link_jivo_users` only touches `auth_id`, but check anyway and report it.
- **The user table itself:** compare every column except `auth_id` with the backup taken just before linking. Use the data lines of `pg_restore -a -t <user_table> -f - <dump>`, without the `SET`, `COPY …` and `\.` lines, against `COPY (SELECT <same columns in the same order> FROM <user_table>) TO STDOUT`, both sorted. On a live database, `last_login` may differ for users who signed in meanwhile.

## 6. Clean up the files

- Delete `users.json` everywhere it was copied (the app server, the Auth host, the transfer location), with `shred -u` where available. Tell the user where it was. Streamed over SSH stdin, it was only ever on the machine that exported it.
- Keep `mapping.json` with the migration records if the user wants an audit trail, or delete it too.

## 7. Right before the switch

Users added to the app after the export have no `auth_id`. Just before phase 4 deploys, ideally once the old sign-up is frozen, run steps 1–5 again. Only unlinked users are exported, and the import and link are idempotent.

With `LOCAL_USER_LINK_BY_EMAIL` (D4), anyone missed is linked at their first Jivo sign-in instead. Only one local row with that email and no `auth_id` is ever claimed, and never one that's already linked.
