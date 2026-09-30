"""
Docker HEALTHCHECK: GET /api/v1/health/ from the local gunicorn.

Healthy if it answers without an error. Redirects are not followed and
count as healthy, so SECURE_SSL_REDIRECT can't send the check elsewhere.
"""

import http.client
import os
import sys


bind_host, _, port = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000").rpartition(":")

if bind_host in ("", "0.0.0.0", "[::]"):
    bind_host = "127.0.0.1"

# The Host header must pass ALLOWED_HOSTS.
host_header = os.environ.get("ALLOWED_HOSTS", "localhost").split(",")[0].strip()

if host_header in ("", "*"):
    host_header = "localhost"

host_header = host_header.lstrip(".")

try:
    connection = http.client.HTTPConnection(bind_host.strip("[]"), int(port), timeout=3)
    connection.request(
        "GET",
        "/api/v1/health/",
        headers={
            "Host": host_header,
            # Only trusted with TRUST_X_FORWARDED_PROTO, like Nginx's.
            "X-Forwarded-Proto": "https",
        },
    )
    status = connection.getresponse().status
except OSError:
    sys.exit(1)

sys.exit(0 if status < 400 else 1)
