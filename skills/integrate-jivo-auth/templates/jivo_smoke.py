"""
Phase 5 live check: sign in at Jivo Auth, call the app, refresh, sign out.

Run it OUTSIDE the repository, against a deployed (or local) app, with a test
account the user controls. Credentials come from the environment and are
never printed, nor is any token:

    read -rs JIVO_SMOKE_PASSWORD && export JIVO_SMOKE_PASSWORD
    JIVO_SMOKE_EMAIL=tester@jivo.in APP_API=https://app.example/api APP_ME_PATH=/me/ \
    python jivo_smoke.py

Optional: JIVO_AUTH_URL (default https://auth.jivo.in), JIVO_AUTH_APP (the
slug; checked in the token's `apps`), EXPECT_PK (the user's primary key from
before the migration). Exit status 1 if any step fails. Each run creates one
Jivo Auth session and always ends it. Needs the `requests` package.
"""

import base64
import json
import os
import sys

import requests

AUTH = os.environ.get("JIVO_AUTH_URL", "https://auth.jivo.in").rstrip("/") + "/api/v1"
API = os.environ["APP_API"].rstrip("/")
ME = os.environ.get("APP_ME_PATH", "/me/")
SLUG = os.environ.get("JIVO_AUTH_APP")
EXPECT_PK = os.environ.get("EXPECT_PK")
failures = 0


def step(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def body(response):
    try:
        return response.json()
    except ValueError:
        return {}


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def me_pk(data):
    data = data.get("data", data) if isinstance(data, dict) else {}
    return data.get("id", data.get("pk"))


r = requests.post(f"{AUTH}/auth/login/", timeout=15, json={
    "email": os.environ["JIVO_SMOKE_EMAIL"], "password": os.environ["JIVO_SMOKE_PASSWORD"],
    "device_name": "smoke test"})
if not step("Jivo Auth sign-in", r.status_code == 200, f"HTTP {r.status_code}"):
    print("     ", {k: v for k, v in body(r).items() if k in ("code", "detail")})
    sys.exit(1)
pair = r.json()

try:
    c = claims(pair["access"])
    step("access token", c.get("token_type") == "access" and (not SLUG or SLUG in c.get("apps", [])),
         f"iss={c.get('iss')} apps={c.get('apps')} lifetime={c['exp'] - c['iat']}s")

    r = requests.get(f"{API}{ME}", headers={"Authorization": f"Bearer {pair['access']}"}, timeout=30)
    pk = me_pk(body(r)) if r.status_code == 200 else None
    step("app accepts the token", r.status_code == 200, f"HTTP {r.status_code}, local pk={pk}")
    if EXPECT_PK:
        step("same local row as before the migration", str(pk) == EXPECT_PK, f"expected pk={EXPECT_PK}")

    r = requests.post(f"{AUTH}/auth/refresh/", json={"refresh": pair["refresh"]}, timeout=15)
    if step("Jivo Auth refresh", r.status_code == 200, f"HTTP {r.status_code}"):
        new = r.json()
        step("refresh rotated both tokens", new["access"] != pair["access"] and new["refresh"] != pair["refresh"])
        pair = new
        r = requests.get(f"{API}{ME}", headers={"Authorization": f"Bearer {pair['access']}"}, timeout=30)
        step("app accepts the refreshed token", r.status_code == 200 and me_pk(body(r)) == pk,
             f"HTTP {r.status_code}")

    r = requests.get(f"{API}{ME}", headers={"Authorization": "Bearer not.a.token"}, timeout=30)
    step("app refuses a bad token", r.status_code == 401, f"HTTP {r.status_code}")
finally:
    r = requests.post(f"{AUTH}/auth/logout/", json={"refresh": pair["refresh"]}, timeout=15)
    step("Jivo Auth sign-out", r.status_code == 200, f"HTTP {r.status_code}")

r = requests.post(f"{AUTH}/auth/refresh/", json={"refresh": pair["refresh"]}, timeout=15)
step("refresh after sign-out is refused", r.status_code == 401, f"HTTP {r.status_code}")

sys.exit(1 if failures else 0)
