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

- Phase 2 is deployed in this environment: the `auth_id` column and the three commands exist.
- The application exists in Jivo Auth, and `import_users` is available on the Auth host (prerequisites 5, 8 and 10).
- Decisions D1, D2, D3, D5 and D6 are recorded.
- **Back up the app's database:** `umask 077; pg_dump -Fc -f app-before-jivo-$(date +%F).dump <db>`. Confirm the file exists and has a sensible size. Backups hold every password hash, just like the export, so keep them mode 600 and off shared storage.

## 1. Export

```bash
python manage.py export_jivo_users /secure/tmp/users.json            # active users without auth_id
python manage.py export_jivo_users /secure/tmp/users.json --include-inactive   # decision D5
```

- The command refuses to write inside the repository or to overwrite a file, and creates it with mode 600. The file holds **password hashes**, so treat it like a password database.
- It prints users it didn't export (no email). Settle each one with the user (D6), for example by fixing the email in the app and exporting again.
- It also lists emails shared by several users (the import skips all of them), and how many inactive users it left out (D5). Check: exported + without email + inactive = users without `auth_id`.
- To leave passwords behind (D1 = no), strip them before transferring:
  ```bash
  python -c "import json;d=json.load(open('/secure/tmp/users.json'));[u.pop('password',None) for u in d];json.dump(d,open('/secure/tmp/users-nopw.json','w'))"
  ```

## 2. Dry run on Jivo Auth

⛔ This runs a command on the Jivo Auth host. Confirm with the user, and let them transfer the file if that's how they work (`scp` to the host; never through chat or a repository).

```bash
# on the Jivo Auth host, in the project directory
docker compose exec -T auth python manage.py import_users <slug> - --dry-run [--mark-verified] < users.json > mapping-dry.json
```

`import_users` input: a JSON list of `{source_id, email, first_name?, last_name?, password?, is_active?, employee_code?}`. It never accepts staff flags.

The output is `{"summary": {"created", "linked", "skipped"}, "users": [{"source_id", "email", "auth_id", "status", "notes"}]}`. Show the user:

| Status | Meaning | Review |
|---|---|---|
| `created` | A new Jivo account is made and granted the slug | Notes say `password kept (…)`, or that a reset is needed. Without `--mark-verified`: "can't sign in until verified". |
| `linked` | A Jivo account with that email already exists. It's granted the slug, and its password, names and status are **unchanged** | Check every one (D3): is it the same person? Notes flag deactivated or unverified accounts. |
| `skipped` | No valid email, or the email is shared with another row in the file | Settle per D6, then export again |

A bad employee code doesn't skip the user: it's dropped with a note.

## 3. Import for real

⛔ Get the user's go-ahead on the dry-run results.

```bash
docker compose exec -T auth python manage.py import_users <slug> - [--mark-verified] < users.json > mapping.json
```

- The run is atomic: all or nothing.
- It's idempotent: running again with the same file reports everyone as `linked` with the same `auth_id`.
- Every new account and grant appears in the Jivo Auth audit log, with the source "imported from an application's user list".

Bring `mapping.json` back to where the app's commands run. It holds emails and IDs, but no hashes.

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
- **Business data unchanged:** row counts of the main business tables and FK columns match before and after. `link_jivo_users` only touches `auth_id`, but check anyway and report it.

## 6. Clean up the files

- Delete `users.json` everywhere it was copied (the app server, the Auth host, the transfer location). Tell the user where it was.
- Keep `mapping.json` with the migration records if the user wants an audit trail, or delete it too.

## 7. Right before the switch

Users added to the app after the export have no `auth_id`. Just before phase 4 deploys, ideally once the old sign-up is frozen, run steps 1–5 again. Only unlinked users are exported, and the import and link are idempotent.

With `LOCAL_USER_LINK_BY_EMAIL` (D4), anyone missed is linked at their first Jivo sign-in instead. Only one local row with that email and no `auth_id` is ever claimed, and never one that's already linked.
