import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .exceptions import AuthServiceError, AuthServiceUnavailable


USER_AGENT = f"jivo-auth-client/{__version__}"


def request_json(method, url, *, data=None, params=None, headers=None, timeout):
    """
    Send a JSON request and return the decoded response body. Raises
    AuthServiceError for error statuses and AuthServiceUnavailable when the
    service can't be reached.
    """

    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    request = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if data is not None else {}),
            **(headers or {}),
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()

    except HTTPError as exc:
        try:
            error_data = json.loads(exc.read() or b"{}")
        except ValueError:
            error_data = {}

        raise AuthServiceError(exc.code, error_data) from exc

    # URLError, timeouts and connection resets are all OSErrors.
    except OSError as exc:
        raise AuthServiceUnavailable(exc) from exc

    try:
        return json.loads(body or b"null")
    except ValueError as exc:
        raise AuthServiceUnavailable("response is not JSON") from exc
