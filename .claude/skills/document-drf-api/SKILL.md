---
name: document-drf-api
description: Document Django REST Framework APIs in OpenAPI/Swagger with drf-spectacular. Every endpoint gets correct auth/security (a lock only on protected endpoints, the right scheme for API-key endpoints), tags, summary, description, request/response schemas, every real error response, and examples taken from real responses. Uses a shared helpers module plus a per-app schema.py, with regression tests and strict schema validation. Use this whenever the user wants to document, improve, audit or fix API docs, Swagger UI, the OpenAPI schema or `@extend_schema` in a Django/DRF project. That includes documenting a newly added endpoint, view, ViewSet or app "like the rest of the API", fixing public endpoints that show a lock or wrong request/response bodies, and clearing drf-spectacular warnings or a failing APIDocsTests, even if the user doesn't say "drf-spectacular".
---

# Document a DRF API with drf-spectacular

The goal is API docs a client developer can trust. Each endpoint says what
it does, whether it needs authentication (and which kind), what to send, and
every response it can really return, with realistic examples. The docs must
be consistent across the whole API and checked by tests, so they don't drift.

Two things matter more than polish:

- **Accuracy over completeness.** The docs must describe what the code
  actually does. Read the code path for each endpoint and take examples from
  real responses. A confident but wrong example (a login response documented
  as the login request, a public endpoint with a lock) is worse than no
  example, because clients build against it.
- **Documentation only.** Don't change business logic or behaviour to make
  the docs nicer. These changes are fine:
  - `extend_schema`/`extend_schema_view`/`extend_schema_serializer` decorators
  - doc-only serializers
  - serializer `help_text`
  - `SPECTACULAR_SETTINGS`
  - `OpenApiAuthenticationExtension`s
  - moving existing doc-only code into the new modules

  If documenting reveals a behaviour bug (a 500 on bad input, a filter that
  silently ignores bad values, a 403 where 401 is expected), document what
  the API does today and list the bug in your summary. Fixing it quietly
  inside a docs change surprises the user and can break clients; in testing
  this was the most common way a docs task went wrong.

`references/patterns.md` has the gotchas table, which explains most broken
drf-spectacular setups, and copy-ready templates for every file below.
`scripts/audit_openapi.py` checks a generated schema for the problems this
skill fixes.

## Step 0: Pick the mode

Look for an existing documentation convention:

```bash
grep -rln "extend_schema" --include=*.py . | grep -v -e venv -e site-packages
ls */schema.py */openapi.py 2>/dev/null
grep -rn "PUBLIC_OPERATIONS\|APIDocsTests" --include=*.py . | grep -v venv
```

Also read CLAUDE.md/README for a documented API-docs convention.

- **Incremental mode.** The project already has a convention like the one
  below: a shared helpers module, per-app `schema.py`, and docs tests.
  Follow it exactly for the new or changed endpoints:
  - Read only the gotchas table (patterns.md §1) and the sections you need,
    e.g. §5 for a ViewSet or §10 for a new authentication class. The
    project's own `schema.py` files are the better template.
  - Reuse the helpers, wording and example style.
  - Add the new decorators to the right app's `schema.py`.
  - Add a new tag to `SPECTACULAR_SETTINGS["TAGS"]` if needed, and new shared
    responses (e.g. a 403) to the helpers module.
  - Add public endpoints to `PUBLIC_OPERATIONS`, and endpoints on another
    scheme to that scheme's set.

  Don't restyle documentation that already exists. Still do Step 1 for the
  new endpoints and all of Step 8.
- **Full mode.** No convention yet, or the user asked for the whole API.
  Read patterns.md, then do every step.

Work out how the project runs Python (`uv run`, `poetry run`, an activated
venv, `docker compose exec web`) and use that for every command below.

## Step 1: Analyze before writing

1. **Baseline schema and audit**:
   ```bash
   python manage.py spectacular --format openapi-json --file /tmp/openapi-before.json
   python <skill-dir>/scripts/audit_openapi.py /tmp/openapi-before.json
   ```
   The audit prints every operation with its tag, security, summary, request
   and response components, then lists problems. Keep the output so you can
   report what was wrong.
2. **Inventory each endpoint** from `urls.py` (including routers) and its
   view. For each one, record:
   - **HTTP methods**, respecting `http_method_names` and ReadOnly ViewSets.
   - **Effective authentication and permission classes.** View attributes
     override `REST_FRAMEWORK` defaults; `authentication_classes = []` means
     none; SimpleJWT token views use `()`. This decides public versus
     protected, which scheme, and whether 403 is possible. A custom
     authentication class needs an `OpenApiAuthenticationExtension`
     (patterns.md §10), or the endpoint looks public.
   - **Throttle scope** and its rate.
   - **Request and response bodies**, including hand-built `Response({...})`
     dicts that no serializer describes.
   - **Every status code the code path can return**, with its body. Look at
     `validate*()` methods, `get_object()`/404s, permission denials,
     explicit `Response(status=...)` calls and exception handlers.
   - **Parameters:** path parameters, query parameters the view reads itself,
     filter backends and pagination.
3. **Check `SPECTACULAR_SETTINGS`** against the gotchas table. The usual
   culprits are:
   - a global `SECURITY`, which is appended to every operation;
   - `COMPONENTS`, which is silently ignored (the real setting is
     `APPEND_COMPONENTS`);
   - a missing `COMPONENT_SPLIT_REQUEST`.

## Step 2: Capture real responses

Use the test client against a **throwaway test database** (template in
patterns.md §9), never the development database. Exercise the success path
and each error branch found in Step 1, and print status plus JSON body.
These become your examples and error descriptions: copy their structure and
messages verbatim, and swap real tokens, IDs and secrets for obvious
placeholders.

This step is what keeps examples honest. For example, DRF's duplicate-email
message is `"user with this email already exists."` (lowercase), and
SimpleJWT errors carry a `code`. Guessing gets details like these wrong.

## Step 3: Global settings (full mode)

Update `SPECTACULAR_SETTINGS` following patterns.md §2:

- Remove `SECURITY` and any bogus `COMPONENTS`.
- Add `APPEND_COMPONENTS` with described security schemes. Use the same
  names drf-spectacular generates, which you can see in the baseline schema.
  Also add the `ValidationError` schema here.
- Set `COMPONENT_SPLIT_REQUEST: True` and
  `SWAGGER_UI_SETTINGS.persistAuthorization`.
- Declare `TAGS` with descriptions for every tag you use.
- Write a `DESCRIPTION` overview covering every way to authenticate (how to
  get the token or key and how to send it), the error format and the rate
  limits.

## Step 4: Shared helpers (full mode)

Create a helpers module next to settings (e.g. `config/openapi.py`, from
patterns.md §3). Include:

- the `AUTH_REQUIRED`/`AUTH_NONE` description lines, plus one per other
  scheme if the API has several;
- `MessageSerializer` and `ErrorSerializer`, with only the fields the API
  really returns;
- `error_response`, `validation_error_response` and `throttled_response`;
- shared 401/403/404 responses;
- placeholder tokens and IDs;
- a `rate_limit()` helper that reads throttle rates from settings, so
  descriptions never drift.

Move any existing doc-only serializers (e.g. an ad-hoc `MessageSerializer`
in some app) here and update their imports.

## Step 5: Per-app `schema.py`

For every operation, write one `extend_schema` decorator in the app's
`schema.py` (patterns.md §4–5), and apply it to the view (§6). Each
operation gets:

- **tags**: one tag, matching `SPECTACULAR_SETTINGS["TAGS"]`.
- **summary**: 2–5 words, verb first ("Log in", "List sessions").
- **description**, in the same shape everywhere:
  1. what the endpoint does;
  2. anything a client can't guess: side effects, read-only fields,
     single-use tokens, identical responses to prevent enumeration,
     DEBUG-only fields, and quirks found in Step 1 (e.g. "unknown filter
     values return an empty page");
  3. rate limit, if one applies;
  4. the `**Authentication:**` line.
- **auth**: `auth=[]` on every public operation. Protected operations get
  their requirement from the authentication classes, so leave it off.
- **request**: derived from the serializer when that's correct; otherwise set
  it explicitly (`request=None` for an empty POST or a body-less `@action`).
- **responses**: the success body plus every error from Step 1:
  - 400 validation;
  - 401 on protected endpoints, and on public ones that reject credentials or
    tokens;
  - 403 only when a permission can deny an authenticated user;
  - 404 for lookups;
  - 429 for strict scoped throttles.

  Use the shared helpers so error bodies look the same everywhere.
- **examples**: request examples via `OpenApiExample(..., request_only=True)`,
  response examples on `OpenApiResponse(examples=[...])`, both from Step 2.
  For list endpoints, give a single item; drf-spectacular wraps it in a list.
- **parameters**: path parameters that need a description, and query
  parameters the view reads itself (with `enum` when the values are fixed).

If the helpers module turns up a name collision, or an auto component name
that leaks internals, fix it with `extend_schema_serializer(component_name=...)`.

## Step 6: Field descriptions

Add `help_text` to serializer fields where the meaning isn't obvious from the
name: constraints, case-insensitivity, what an ID is used for, and when a
nullable field is null. Put it on the serializer (declared field or
`Meta.extra_kwargs`), never on model fields, which would create a migration.
Don't describe every field mechanically; `first_name` needs nothing.

## Step 7: Regression tests (full mode; update in incremental mode)

Add `APIDocsTests` (patterns.md §8) where the project keeps cross-cutting
tests. It asserts that:

- schema generation emits no warnings;
- every operation has tags, a summary and a description;
- security matches `PUBLIC_OPERATIONS` (and one set per extra scheme), and
  protected operations document 401;
- every example validates against its schema.

## Step 8: Verify

Run all of these and fix documentation problems until they pass:

1. `python manage.py check`
2. `python manage.py spectacular --validate --fail-on-warn --file /dev/null`
3. The audit, on a freshly generated schema, with the public operations and
   any per-scheme operations:
   ```bash
   python <skill-dir>/scripts/audit_openapi.py /tmp/openapi-after.json \
       --public "POST /api/v1/auth/login/" ... \
       --scheme appKey "GET /api/v1/apps/users/"
   ```
   The target is 0 errors. "No examples" warnings should be gone too,
   unless an operation truly has no body.
4. The project's full test suite: behaviour tests prove you didn't change
   behaviour, and the docs tests prove the docs are right.
5. The docs page (`/api/docs/` or the project's URL) returns 200 through the
   test client or a running server.

If a check fails because of API behaviour rather than docs, don't bend the
docs or the test to hide it. Document the real behaviour and report it.

## Step 9: Summarize

Keep it short:

- **What was wrong.** Use the audit and baseline findings, in plain words,
  e.g. "public endpoints showed a lock because `SECURITY` was set globally".
- **What changed.** Settings, the helpers module, each `schema.py`,
  serializer `help_text`, and tests.
- **Verification results.** Check, validate, audit and test counts.
- **Behaviour issues noticed but not changed**, if any.
