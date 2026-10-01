# Phase 4: Frontend and mobile changes

The frontend talks to Jivo Auth for everything about the account, and to
the app's API for everything else. If the frontend is in another repository,
write these changes up as a handoff for its owners, and ship both sides
together.

**Base URLs.** Auth is `https://auth.jivo.in/api/v1`, and the API is the app's own URL.

## Screen by screen

| Screen / flow | Before | After |
|---|---|---|
| **Sign in** | The app's login endpoint | `POST {AUTH}/auth/login/` with `{"email", "password", "device_name"}` returns `{"access", "refresh"}`. Errors: 401 with `code: email_not_verified` (show "verify your email"), 401 otherwise (wrong email or password), 429 (wait `Retry-After` seconds). |
| **Every API call** | The app's token | `Authorization: Bearer <access>`. On 401: refresh once, then retry. On 403: "you don't have access to this application", and refreshing won't help. |
| **Refresh** | The app's refresh endpoint | `POST {AUTH}/auth/refresh/` with `{"refresh"}` returns a **new pair**. Store both. The old refresh token keeps working for 30 seconds (requests in flight), and after that reusing it ends the session. On failure: sign in again. |
| **Sign out** | The app's logout | `POST {AUTH}/auth/logout/` with `{"refresh"}` (no access token needed), then delete the stored tokens |
| **Sign up** | The app's registration | **Remove it.** Accounts are created by administrators in Jivo Auth, and self-registration is off. Show "ask your administrator for access". |
| **Forgot password** | The app's reset flow | Link to `https://auth.jivo.in/forgot-password/`, which is hosted by Jivo Auth, emails included. It only works when Jivo Auth has SMTP configured (prerequisite 14). |
| **Change password** | The app's endpoint | `POST {AUTH}/users/change-password/` with Bearer and `{"current_password", "new_password", "new_password_confirm"}`. It **ends every session**, including this one, so sign in again afterwards. |
| **Profile (name)** | The app's user endpoint | `PATCH {AUTH}/users/me/` with Bearer and `{"first_name", "last_name"}`. Email can't be changed by the user. The app's own profile fields still go to the app's API. |
| **Who am I** | The app's "me" endpoint | Keep the app's "me" endpoint for app data (roles and so on). `GET {AUTH}/users/me/` gives the Jivo profile. |
| **User management** (admins) | "Create user" with a password | "Add a Jivo user to this app" (see [switch.md](switch.md#creating-users)): pick an existing Jivo user, then set app roles. No password fields anywhere. |

## A complete browser client

Adapt it rather than writing one from scratch. It's the tested client from
the Jivo Auth docs:

- it refreshes once for any number of requests that fail together;
- it shares tokens between tabs through `localStorage`;
- it signs the user out when the session has ended.

```js
const AUTH = "https://auth.jivo.in/api/v1";
const API = "https://<the app's API>";
const STORAGE_KEY = "jivoTokens";

const loadTokens = () => JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
function saveTokens(tokens) {
  if (tokens) localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens));
  else localStorage.removeItem(STORAGE_KEY);
}
const postJSON = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

export async function login(email, password) {
  const response = await postJSON(`${AUTH}/auth/login/`, { email, password, device_name: "<slug> web" });
  const data = await response.json();
  if (response.ok) return saveTokens(data);
  if (data.code === "email_not_verified") throw new Error("verify-email");
  if (response.status === 429) throw new Error("too-many-attempts");
  throw new Error("wrong-credentials");
}

let refreshing = null;
function refresh(rejectedAccess) {
  const tokens = loadTokens();
  if (!tokens) return Promise.reject(new Error("logged-out"));
  if (tokens.access !== rejectedAccess) return Promise.resolve();   // another tab refreshed
  refreshing ??= postJSON(`${AUTH}/auth/refresh/`, { refresh: tokens.refresh })
    .then(async (response) => {
      if (!response.ok) { saveTokens(null); throw new Error("logged-out"); }
      saveTokens(await response.json());
    })
    .finally(() => { refreshing = null; });
  return refreshing;
}

export async function api(path, options = {}) {
  const send = (access) => fetch(API + path, {
    ...options, headers: { ...options.headers, Authorization: `Bearer ${access}` },
  });
  const tokens = loadTokens();
  if (!tokens) throw new Error("logged-out");
  let response = await send(tokens.access);
  if (response.status === 401) {
    await refresh(tokens.access);
    response = await send(loadTokens().access);
  }
  return response;   // 403: no access to this application, or the app's own rules
}

export async function logout() {
  const tokens = loadTokens();
  if (tokens) await postJSON(`${AUTH}/auth/logout/`, { refresh: tokens.refresh });
  saveTokens(null);
}
```

## Also check

- **CORS at Jivo Auth.** The frontend's origin must be in Jivo Auth's `CORS_ALLOWED_ORIGINS` (prerequisite 12), or every sign-in fails with "blocked by CORS policy".
- **CORS at the app.** If the frontend and API are on different origins, the app needs `django-cors-headers` for the frontend. Tokens go in the `Authorization` header, so `CORS_ALLOW_CREDENTIALS` stays off.
- **Token storage.** `localStorage` survives reloads, but any injected script can read it, so keep a strict Content-Security-Policy. Mobile apps use the Keychain or Keystore.
- **Access changes take up to 15 minutes.** An access token that's already issued stays valid until it expires. The next refresh picks up lost access or deactivation.
- **Remove the old token handling:** stored tokens from the old system, their refresh logic, and old sign-in URLs in environment config.
