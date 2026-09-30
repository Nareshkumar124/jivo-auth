import logging
import threading
import time

from jwt import PyJWKSet
from jwt.exceptions import PyJWTError

from .exceptions import AuthServiceError
from .http import request_json


logger = logging.getLogger(__name__)


class JWKSCache:
    """
    Jivo Auth's public signing keys by `kid`. Fetched on first use, and
    again when a token names a `kid` not seen yet (at most once a minute,
    or every 10 seconds while fetching fails). Keys already fetched stay in
    use when a fetch fails, so tokens keep verifying while Jivo Auth is down.
    """

    REFETCH_INTERVAL = 60
    RETRY_INTERVAL = 10

    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout

        self._keys = {}
        self._next_fetch = 0.0
        self._lock = threading.Lock()

    def get(self, kid):
        key = self._keys.get(kid)

        if key is not None:
            return key

        with self._lock:
            if kid not in self._keys and time.monotonic() >= self._next_fetch:
                self._fetch()

            return self._keys.get(kid)

    def _fetch(self):
        try:
            key_set = PyJWKSet.from_dict(
                request_json(
                    "GET",
                    self.url,
                    timeout=self.timeout,
                )
            )

        except (AuthServiceError, PyJWTError, AttributeError) as exc:
            logger.warning(
                "Could not fetch Jivo Auth signing keys from %s: %s",
                self.url,
                exc,
            )

            self._next_fetch = time.monotonic() + self.RETRY_INTERVAL
            return

        self._keys = {
            key.key_id: key
            for key in key_set.keys
            if key.key_id
        }

        self._next_fetch = time.monotonic() + self.REFETCH_INTERVAL


_caches = {}
_caches_lock = threading.Lock()


def get_jwks_cache(url, timeout):
    """One cache per URL for the whole process."""

    with _caches_lock:
        cache = _caches.get(url)

        if cache is None:
            cache = _caches[url] = JWKSCache(url, timeout)

        cache.timeout = timeout

        return cache


def clear_jwks_caches():
    with _caches_lock:
        _caches.clear()
