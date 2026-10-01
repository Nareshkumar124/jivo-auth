# Registering an application

These are the steps a Jivo Auth administrator takes when a new application
starts using Jivo login, and when users need access to it. You'll need a
staff account on https://auth.jivo.in/admin/ with the **Application
manager** role, or a superuser.

## Staff roles

| Role | Can |
|---|---|
| **Support** | Activate or deactivate users, mark them verified, send reset and verification emails, end sessions; read applications and the audit log. |
| **Application manager** | Register applications, rotate API keys, grant or remove access; read users and the audit log. |
| **Auditor** | Read everything, including the audit log; change nothing. |
| **Superuser** | Everything, including staff accounts, roles and other superusers. |

Only superusers can make someone staff, change roles, or edit a superuser.

The examples use an application with the slug `oms`.

## 1. Create the application and its API key

In the admin: **Applications** → **Add application**. Enter the name and
the slug, save, then press **Create API key** at the top of the page.

Or on the server, in the project directory:

```bash
docker compose exec auth python manage.py app_api_key oms --create "Jivo OMS"
```

Either way you get the application's **API key** (starting with `jivo_`).
It's shown only once. Only its hash is stored, so if it's lost, create a
new one.

Send the application's developers:

- the slug: `oms`
- the API key, through a password manager or secret store (never chat or
  email)

**Slugs** are lowercase letters, digits, `-` and `_`. The slug is what
appears in users' tokens, so choose it once and don't change it.

The API key lets the application's **server**:

- log users in on their behalf, with each user's own IP used for rate
  limiting
- look up users who have access to the application

It never goes to a browser or mobile app.

## 2. Give users access

A user can only use the application if it's in their `apps` claim.

- **One user:** Admin → **Users** → open the user → tick `Jivo OMS` under
  **Application access** → Save.
- **Many users:** Admin → **Users** → select them → action **Grant access
  to an application…** → pick `Jivo OMS` → **Grant access**. Or open the
  application and add users under **Users with access**.

New accounts have access to nothing until you grant it.

The change reaches the user's token on their next refresh, **within 15
minutes**. To make it take effect at once, open the user and press **Log
out everywhere**, so they sign in again.

## 3. If the application's frontend calls Jivo Auth from the browser

If the application is a single-page app or website that calls
`https://auth.jivo.in/api/...` directly from JavaScript, its origin must be
allowed for CORS. Add it to `CORS_ALLOWED_ORIGINS` in the server's `.env`
(comma-separated), then restart:

```bash
# .env
CORS_ALLOWED_ORIGINS=https://oms.jivo.in,https://ecom.jivo.in

docker compose up -d
```

This isn't needed when the application logs users in from its own server,
as server-rendered Django sites do.

## Everyday tasks

| Task | How |
|---|---|
| Remove a user's access | Untick the application under the user's **Application access**, or select users → **Remove access to an application…**. Takes effect within 15 minutes, or at once with **Log out everywhere**. |
| Block a user everywhere | Select them → **Deactivate selected users** (this also ends their sessions), or untick **Active** on the user. Refreshes fail at once; access tokens already issued expire within 15 minutes. |
| Log a user out of all devices | Open the user → **Log out everywhere**, or select several → **Log out selected users everywhere**. |
| See or end a user's sessions | The user's page lists their sessions. Or Admin → **Sessions**: filter by status or last use, search by email or IP, then **Revoke selected sessions**. |
| Reset a user's password | Open the user → **Send password reset** (they choose a new one), or **Reset password** under the password field to set one yourself. Setting it logs them out everywhere. |
| Set a user's employee code | Open the user → **Profile** → **Employee code**. It's stored uppercase, must be unique, and applications can read it (it's how they match people to HR records). Search the user list by it. |
| Create a user who can't self-register | **Users** → **Add**. Leave **Email verified** ticked if you know the address is right. |
| Rotate an application's API key | Open the application → **Rotate API key** (or `docker compose exec auth python manage.py app_api_key oms`). The old key stops working **immediately**, so update the application at the same time. |
| See who did what | Admin → **Audit log**: sign-ins, failed sign-ins, password changes, access changes and key rotations. Filter by event, severity or time; export to CSV. Each user's page shows their recent activity too. |
| Give a colleague admin access | Superusers only: open their user → **Staff and permissions** → tick **Staff** and add a **staff role** (Support, Application manager or Auditor). |
| Retire an application | Untick **Active** on the application. It disappears from new tokens and its API key stops working. |
| Bring in an application's existing users | On the server: `docker compose exec -T auth python manage.py import_users oms - --dry-run < users.json`, then again without `--dry-run`, saving the output. It creates the missing accounts (keeping password hashes it can verify), links emails that already have an account without changing them, grants `oms`, and prints each user's Jivo ID for the application to store. Add `--mark-verified` only if the addresses are known to belong to their users. See [Moving an existing application](integrate-django.md#moving-an-existing-application-onto-jivo-auth). |
