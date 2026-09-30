# Jivo Auth documentation

Jivo Auth is the central login service for Jivo applications. It runs at
**https://auth.jivo.in**, owns every user account, and issues the tokens that
all other Jivo applications trust.

| I want to… | Read |
|---|---|
| Protect a Django REST Framework API (React, Vue or mobile frontend) | [Integrating a DRF API](integrate-drf.md), or **Docs** in the admin panel (https://auth.jivo.in/admin/docs/) for the same guide with this server's live settings |
| Add Jivo login to a server-rendered Django site or the Django admin | [Integrating a Django application](integrate-django.md#b-server-rendered-django-site-templates-admin) |
| Register a new application and give users access to it | [Registering an application](register-an-application.md) |
| Run or configure auth.jivo.in itself | [Running auth.jivo.in](deploy-auth-service.md) |
| Look up an endpoint | [API reference (Swagger)](https://auth.jivo.in/api/docs/) |

## How it works

```text
                 ┌────────────────────────────────────┐
                 │        https://auth.jivo.in        │
                 │  users · passwords · sessions      │
                 │  signs tokens with a private key   │
                 └──────┬──────────────────────┬──────┘
          login/refresh │                      │ public key (JWKS),
          (a few times  │                      │ fetched once and cached
           an hour)     │                      │
                 ┌──────▼──────┐        ┌──────▼──────────────┐
                 │   Browser,  │ token  │   Your application  │
                 │ mobile app  ├───────►│   (e.g. oms.jivo.in)│
                 │ or your     │        │   verifies tokens   │
                 │ server      │        │   locally           │
                 └─────────────┘        └─────────────────────┘
```

1. The user logs in with their email and password at Jivo Auth, either
   directly from a browser or mobile app, or through your application's
   server.
2. Jivo Auth returns an **access token**, valid for 15 minutes, and a
   **refresh token**, valid for 30 days.
3. Every request to your application carries the access token. Your
   application checks it **locally** with Jivo Auth's public key. There's
   no call to Jivo Auth per request, so your application keeps working even
   if Jivo Auth is briefly down.
4. When the access token expires, the client exchanges the refresh token
   for a new pair.

## What a token tells your application

An access token is a signed JWT (RS256) with these claims:

| Claim | Meaning |
|---|---|
| `sub` | The user's ID, a UUID. Stable for the life of the account. |
| `email` | The user's email address, lowercased and verified. |
| `apps` | Slugs of the applications the user may use, e.g. `["oms", "ecom"]`. |
| `iss` | `https://auth.jivo.in` |
| `token_type` | `access`. Refresh tokens say `refresh` and must never be accepted as access tokens. |
| `exp`, `iat`, `jti` | Expiry, issue time and token ID. |

**Access is granted per application.** A valid token alone doesn't mean
the user may use your application: its slug must be in `apps`. The client
package checks this for you.

## Key facts

- **Public keys:** `https://auth.jivo.in/.well-known/jwks.json`. Each
  token's `kid` header names the key that signed it.
- **Changes reach your application within 15 minutes.** A token is
  checked without calling Jivo Auth, so an access token that's already
  issued stays valid until it expires. That covers deactivating a user,
  removing their access, and a password change.
- **Other stacks:** FastAPI, Node and others can verify the same tokens
  with any JWT library that supports JWKS. Pin the algorithm to `RS256`,
  and check `iss`, `exp`, `token_type == "access"` and your slug in `apps`.
