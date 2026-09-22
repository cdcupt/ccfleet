"""Signing in with Google: the authorization code flow, and nothing else.

Scope is `openid email` and stops there. We want to know that this is the same
person who signed in last time and what address to show the operator. We do not
want their contacts, their profile, or a refresh token, so we do not ask for
them — an unused permission is still a permission somebody granted us.

**Why there is no JWT verification here.** The obvious reading of "sign in with
Google" is: take the id_token, verify its RS256 signature against Google's
published keys, check the issuer, audience and expiry, read the subject out of
it. All of that is correct, and none of it is possible in the standard library,
which has no RSA verification. Rather than hand-roll signature checking or take
a dependency, this uses the token endpoint and then the userinfo endpoint: the
code is exchanged over TLS with our client secret, and the identity is read
back over TLS from Google with the access token that exchange returned. Nothing
is being trusted that did not come straight from Google on a connection we
opened. A hand-rolled verifier that got one check wrong would be worse than
both.

Google's access token is used for that one call and then dropped. It is never
stored, which is why there is no column for it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = "openid email"

#: How long a half-finished sign-in stays valid. Long enough to read a consent
#: screen, short enough that a state left in a closed tab is not usable later.
FLOW_TTL_S = 600

TIMEOUT_S = 15


class OAuthError(ValueError):
    """Sign-in could not be completed. The message is safe to log, not to show."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_state() -> str:
    """The value that ties a callback to the browser that started it.

    Without it, anyone can send somebody a callback URL and sign that person
    into an account of the sender's choosing. It is not decoration.
    """
    return secrets.token_urlsafe(32)


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """The S256 PKCE challenge.

    Not strictly required for a confidential client, but it costs one hash and
    it closes the window where a stolen authorization code can be redeemed by
    somebody who does not hold the verifier.
    """
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(*, client_id: str, redirect_uri: str, state: str,
                  verifier: str) -> str:
    if not client_id or not redirect_uri:
        raise OAuthError("Google sign-in is not configured")
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        # Ask for the account chooser rather than silently reusing whichever
        # Google account the browser happens to be signed into.
        "prompt": "select_account",
    })
    return f"{AUTH_ENDPOINT}?{query}"


def _post_form(url: str, fields: dict[str, str], opener: Callable) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    return _read_json(request, opener)


def _get_json(url: str, token: str, opener: Callable) -> dict[str, Any]:
    request = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return _read_json(request, opener)


def _read_json(request: urllib.request.Request, opener: Callable) -> dict[str, Any]:
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        # Google puts a reason in the body. Keep it: "invalid_grant" and
        # "redirect_uri_mismatch" are the two that actually happen, and both
        # are unguessable from a bare 400.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001 - the error matters more than the body
            pass
        raise OAuthError(f"Google returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"could not reach Google: {exc.reason}") from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("Google returned something that is not JSON") from exc
    if not isinstance(data, dict):
        raise OAuthError("Google returned JSON that is not an object")
    return data


def exchange_code(*, code: str, verifier: str, client_id: str,
                  client_secret: str, redirect_uri: str,
                  opener: Callable = urllib.request.urlopen) -> str:
    """Trade the authorization code for an access token. Returns the token."""
    if not code:
        raise OAuthError("no authorization code")
    data = _post_form(TOKEN_ENDPOINT, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, opener)
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise OAuthError("Google's reply carried no access token")
    return token


def fetch_identity(access_token: str,
                   opener: Callable = urllib.request.urlopen) -> dict[str, str]:
    """Who this is, according to Google. Returns {'sub': ..., 'email': ...}.

    An unverified address is refused. Google will hand back an address the
    person has not proved they own, and an account keyed on one lets somebody
    register as an address that is not theirs — which matters here because the
    operator grants allowances by address.
    """
    data = _get_json(USERINFO_ENDPOINT, access_token, opener)
    sub = data.get("sub")
    email = data.get("email")
    if not isinstance(sub, str) or not sub:
        raise OAuthError("Google's reply carried no subject id")
    if not isinstance(email, str) or "@" not in email:
        raise OAuthError("Google's reply carried no usable email address")
    if data.get("email_verified") is not True:
        raise OAuthError(
            "that Google account has not verified its email address")
    return {"sub": sub, "email": email.lower()}
