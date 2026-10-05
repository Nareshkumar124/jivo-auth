"""
"Type your password again" before a sensitive action, once passwords live in
Jivo Auth (decision D9). The app never sees the password.

The client signs in to Jivo Auth AGAIN with the user's email and password,
under a device name kept for this check, and sends that sign-in's access
token to the app. `confirm_fresh_sign_in()` checks that the token belongs to
the request's user and that this user has a session under that device name,
created by a password sign-in in the last FRESH_SIGN_IN_SECONDS. It then
ends that session, so it confirms only once.

The limit, stated plainly: access tokens carry no session ID, so the check
ties the confirmation to the user and the time window, not to the very
token sent. Someone holding the user's refresh token could pass it during
the few seconds between the user's own password sign-in and its use.
Revoking on first use keeps that to one confirmation, by whichever request
comes first.

Why not just check the token's `iat`: a refresh mints a brand-new access
token too, from the refresh token every signed-in browser stores. A recent
`iat` proves nothing about a password. A password sign-in is what creates a
Jivo Auth *session*, and the sessions API says when each one was created,
under which device name. That is the proof.

    # the view
    from .fresh_sign_in import CONFIRM_DEVICE_NAME, FreshSignInRefused, confirm_fresh_sign_in

    try:
        confirm_fresh_sign_in(request.user, request.data.get("access"))
    except FreshSignInRefused as exc:
        return Response({"detail": str(exc)}, status=exc.status)
    # ...issue the app's own short-lived "confirmed" token, as before

    // the client
    const { access, refresh } = await jivoLogin(email, password, "<App> confirmation");
    try { await api.post(".../confirm/", { access }); }
    catch (e) { await jivoLogout(refresh); throw e; }   // don't leave it open

Adapt CONFIRM_DEVICE_NAME (the client must send the same string) and
ID_FIELD. Keep the app's own failure counter around it if it had one: wrong
passwords are rate-limited by Jivo Auth before the app is ever called.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from jivo_auth.exceptions import AuthServiceError, AuthServiceUnavailable, InvalidToken
from jivo_auth import settings as conf
from jivo_auth.http import request_json
from jivo_auth.tokens import decode_access_token, has_app_access


# The device name the client signs in with for this check, and nothing else.
CONFIRM_DEVICE_NAME = "<App> confirmation"
# How recent that sign-in must be.
FRESH_SIGN_IN_SECONDS = 120
ID_FIELD = "auth_id"


class FreshSignInRefused(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def _jivo_api(method, path, access):
    """Call Jivo Auth as the user, with their own access token."""
    # The package's own settings: URL and TIMEOUT may come from JIVO_AUTH or
    # from JIVO_AUTH_<KEY> environment variables.
    return request_json(
        method,
        f"{conf.require_auth_url()}/api/v1{path}",
        headers={"Authorization": f"Bearer {access}"},
        timeout=conf.get("TIMEOUT"),
    )


def _fresh_session(user, access):
    """The session proving `user` just signed in for this check, or None."""
    try:
        claims = decode_access_token(access or "")
    except InvalidToken:
        return None

    local_id = getattr(user, ID_FIELD, None)

    if not has_app_access(claims) or not local_id or str(claims["sub"]) != str(local_id):
        return None

    try:
        sessions = _jivo_api("GET", "/auth/sessions/", access)
    except AuthServiceUnavailable as exc:
        raise FreshSignInRefused("Jivo Auth can't be reached. Try again shortly.", status=503) from exc
    except AuthServiceError:
        return None

    oldest = timezone.now() - timedelta(seconds=FRESH_SIGN_IN_SECONDS)

    for session in sessions or []:
        created = parse_datetime(session.get("created_at") or "")

        if (
            session.get("is_active")
            and session.get("device_name") == CONFIRM_DEVICE_NAME
            and created is not None
            and created >= oldest
        ):
            return session

    return None


def confirm_fresh_sign_in(user, access):
    """Return quietly if `access` proves a fresh password sign-in by `user`;
    raise FreshSignInRefused otherwise. Ends the Jivo Auth session it found,
    so one sign-in confirms once."""
    session = _fresh_session(user, access)

    if session is None:
        raise FreshSignInRefused("Confirm with your password again.")

    try:
        _jivo_api("POST", f"/auth/sessions/{session['id']}/revoke/", access)
    except AuthServiceUnavailable as exc:
        raise FreshSignInRefused("Jivo Auth can't be reached. Try again shortly.", status=503) from exc
    except AuthServiceError as exc:
        # Already revoked: the same sign-in used twice at once.
        raise FreshSignInRefused("Confirm with your password again.") from exc
