"""
End-to-end checks against a deployed Jivo Auth, through the public URL.

Run by smoke_test.sh with the project venv (PyJWT verifies tokens against the
published JWKS):

    SMOKE_BASE=https://auth.jivo.in SMOKE_EMAIL=... SMOKE_PASSWORD=... uv run python smoke.py

SMOKE_EMAIL/SMOKE_PASSWORD must belong to an active, verified superuser (the
throwaway one smoke_test.sh creates). Prints PASS/FAIL per check, never
tokens or passwords. Exit status 0 only if every check passed.

When routes or admin pages change, update the paths here.
"""

import http.cookiejar
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import jwt

BASE = os.environ.get("SMOKE_BASE", "https://auth.jivo.in").rstrip("/")
EMAIL = os.environ["SMOKE_EMAIL"]
PASSWORD = os.environ["SMOKE_PASSWORD"]

ADMIN_PAGES = [
    "/admin/docs/",
    "/admin/audit/auditevent/",
    "/admin/users/user/",
    "/admin/applications/application/",
    "/admin/adminpanel/staffrole/",
]

results = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def opener(follow_redirects=False):
    handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]

    if not follow_redirects:
        handlers.append(NoRedirect)

    built = urllib.request.build_opener(*handlers)
    built.cookie_jar = handlers[0].cookiejar
    return built


def request(op, method, url, body=None, headers=None, form=False):
    headers = dict(headers or {})
    data = None

    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    try:
        resp = op.open(req, timeout=20)
        return resp.status, dict(resp.headers), resp.read(), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(), url
    # DNS failure, refused connection, TLS error, timeout: status 0, so the
    # check fails and reports it instead of crashing the run.
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, {"X-Smoke-Error": str(reason)}, b"", url


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""))


def json_or_empty(body):
    try:
        return json.loads(body)
    except ValueError:
        return {}


def public_checks(api):
    if BASE.startswith("https://"):
        http_url = "http://" + BASE.removeprefix("https://")
        s, h, _, _ = request(api, "GET", http_url + "/api/v1/health/")
        check("HTTP redirects to HTTPS", s == 301 and h.get("Location") == BASE + "/api/v1/health/", f"{s} {h.get('Location')}")

    s, h, b, _ = request(api, "GET", BASE + "/api/v1/health/")
    check("health", s == 200 and json_or_empty(b).get("status") == "ok", h.get("X-Smoke-Error") or f"{s}")

    if BASE.startswith("https://"):
        check("HSTS header", h.get("Strict-Transport-Security", "").startswith("max-age="), h.get("Strict-Transport-Security"))

    check("security headers", h.get("X-Content-Type-Options") == "nosniff" and h.get("X-Frame-Options") == "DENY")

    s, _, b, _ = request(api, "GET", BASE + "/.well-known/jwks.json")
    keys = json_or_empty(b).get("keys", [])
    check("JWKS publishes one RS256 key", s == 200 and len(keys) == 1 and keys[0].get("alg") == "RS256",
          f"kid {keys[0]['kid'][:12]}..." if keys else str(s))

    for path in ("/api/docs/", "/api/schema/", "/forgot-password/"):
        s, _, _, _ = request(api, "GET", BASE + path)
        check(f"GET {path}", s == 200, str(s))

    s, _, _, _ = request(api, "POST", BASE + "/api/v1/auth/login/", {"email": "nobody@example.com", "password": "wrong-password"})
    check("login with bad credentials is refused", s == 401, str(s))
    s, _, _, _ = request(api, "GET", BASE + "/api/v1/users/me/")
    check("/users/me/ without a token is refused", s == 401, str(s))

    return keys


def token_cycle(api, keys):
    s, _, b, _ = request(api, "POST", BASE + "/api/v1/auth/login/", {"email": EMAIL, "password": PASSWORD, "device_name": "post-deploy smoke test"})
    check("login", s == 200, str(s))

    if s != 200:
        return

    pair = json_or_empty(b)
    access, refresh = pair["access"], pair["refresh"]

    try:
        claims = jwt.decode(
            access,
            jwt.PyJWK(keys[0]).key,
            algorithms=["RS256"],
            issuer=BASE,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
        header = jwt.get_unverified_header(access)
        check("access token verifies against the JWKS",
              header.get("kid") == keys[0]["kid"] and claims.get("token_type") == "access" and claims.get("email") == EMAIL,
              f"iss={claims['iss']} ttl={claims['exp'] - claims['iat']}s")
    except (jwt.PyJWTError, IndexError, KeyError) as exc:
        check("access token verifies against the JWKS", False, type(exc).__name__)

    auth = {"Authorization": "Bearer " + access}
    s, _, b, _ = request(api, "GET", BASE + "/api/v1/users/me/", headers=auth)
    check("/users/me/ with the token", s == 200 and json_or_empty(b).get("email") == EMAIL, str(s))
    s, _, _, _ = request(api, "POST", BASE + "/api/v1/auth/verify/", {"token": access})
    check("token verify endpoint", s == 200, str(s))

    s, _, b, _ = request(api, "GET", BASE + "/api/v1/auth/sessions/", headers=auth)
    sessions = json_or_empty(b) if s == 200 else []
    ip = sessions[0].get("ip_address") if sessions else None

    try:
        public_ip = ip is not None and ipaddress.ip_address(ip).is_global
    except ValueError:
        public_ip = False

    check("session records the client's public IP (proxy headers work)", public_ip, f"ip={ip}")

    s, _, b, _ = request(api, "POST", BASE + "/api/v1/auth/refresh/", {"refresh": refresh})
    new_refresh = json_or_empty(b).get("refresh")
    check("refresh rotates the token", s == 200 and new_refresh and new_refresh != refresh, str(s))

    if new_refresh:
        s, _, _, _ = request(api, "POST", BASE + "/api/v1/auth/logout/", {"refresh": new_refresh})
        check("logout", s in (200, 204, 205), str(s))
        s, _, _, _ = request(api, "POST", BASE + "/api/v1/auth/refresh/", {"refresh": new_refresh})
        check("refresh after logout is refused", s == 401, str(s))


def admin_checks():
    browser = opener(follow_redirects=True)
    request(browser, "GET", BASE + "/admin/login/")
    csrf = next((c.value for c in browser.cookie_jar if c.name == "csrftoken"), None)
    check("CSRF cookie is Secure", any(c.name == "csrftoken" and c.secure for c in browser.cookie_jar) or not BASE.startswith("https://"))

    s, _, body, url = request(
        browser, "POST", BASE + "/admin/login/?next=/admin/",
        {"csrfmiddlewaretoken": csrf or "", "username": EMAIL, "password": PASSWORD, "next": "/admin/"},
        headers={"Referer": BASE + "/admin/login/"}, form=True,
    )
    check("admin login reaches the dashboard", s == 200 and urllib.parse.urlparse(url).path == "/admin/", f"{s} {urllib.parse.urlparse(url).path}")

    for pattern in (rb'href="(/static/[^"]+\.css)"', rb'src="(/static/[^"]+\.js)"'):
        match = re.search(pattern, body)
        asset = match.group(1).decode() if match else None
        s, h, _, _ = request(browser, "GET", BASE + asset) if asset else (0, {}, b"", "")
        check(f"static asset served ({asset})", s == 200, str(s))

    for path in ADMIN_PAGES:
        s, _, _, _ = request(browser, "GET", BASE + path)
        check(f"admin page {path}", s == 200, str(s))


def main():
    api = opener()
    keys = public_checks(api)

    if keys:
        token_cycle(api, keys)

    admin_checks()
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
