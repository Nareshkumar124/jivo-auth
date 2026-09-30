class InvalidToken(Exception):
    """An access token failed verification. The message is safe to show."""


class AuthServiceError(Exception):
    """Jivo Auth answered a request with an error status."""

    def __init__(self, status, data=None):
        self.status = status
        self.data = data or {}

        super().__init__(
            f"Jivo Auth returned {status}: {self.data}"
        )


class AuthServiceUnavailable(AuthServiceError):
    """Jivo Auth could not be reached or sent an unreadable response."""

    def __init__(self, reason):
        self.status = None
        self.data = {}

        Exception.__init__(
            self,
            f"Jivo Auth is unavailable: {reason}",
        )
