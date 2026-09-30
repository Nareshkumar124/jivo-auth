"""
The request being handled, so events recorded deep inside models and
services still know the client IP and the acting administrator.
"""

from contextvars import ContextVar


_current_request = ContextVar("audit_current_request", default=None)


def current_request():
    return _current_request.get()


class AuditRequestMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)

        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)
