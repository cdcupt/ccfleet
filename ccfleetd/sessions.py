"""Browser sessions: a signed cookie, and the rules for reading one back.

The console has never had a session. It uses HTTP basic auth, which means the
browser holds the credentials for as long as it is open and re-sends them on
every request. That was acceptable for an operator-only tool and is not
acceptable for a product, so everything user-facing goes through here instead.

Two decisions worth stating, because both are places where the obvious version
is weaker than it looks:

**The cookie is signed, and the signature covers the whole value.** Without a
signature the cookie is just a string the browser sends, and the server is
trusting whatever arrives. Signing is what makes a forged session id fail at
the door rather than in a database lookup that might match something.

**What is stored is a hash of the session id, not the id.** The cookie carries
the id; the database holds its SHA-256. Anyone who reads the database — a
backup, a stray copy, a SQL injection somewhere else — gets hashes they cannot
present to us, rather than a list of live sessions they can sign in with. The
node token in this project is stored the same way, for the same reason.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

#: Name of the cookie. Prefixed because a product and an operator console may
#: end up on sibling hostnames, and two cookies called "session" is a support
#: ticket nobody enjoys.
COOKIE_NAME = "ccfleet_session"

#: How long a session lasts without being renewed.
DEFAULT_TTL_S = 14 * 24 * 3600

#: Sessions shorter than this are almost always a misconfiguration — a zero or
#: a value in the wrong unit — and a session that expires immediately looks
#: exactly like sign-in being broken.
MIN_TTL_S = 300

ID_BYTES = 32


class SessionError(ValueError):
    """A cookie that cannot be trusted."""


def new_session_id() -> str:
    return secrets.token_urlsafe(ID_BYTES)


def hash_session_id(session_id: str) -> str:
    """What the database stores. The cookie holds the id itself."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def sign(session_id: str, secret: str) -> str:
    """The cookie value: the id, a dot, and a signature over the id."""
    if not secret:
        raise SessionError("no cookie secret configured; refusing to sign")
    mac = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{session_id}.{mac}"


def unsign(value: str, secret: str) -> str:
    """Recover the session id from a cookie, or raise.

    Compared in constant time. A comparison that returns early leaks how much
    of the signature was right, and a few thousand requests turn that into the
    whole signature.
    """
    if not secret:
        raise SessionError("no cookie secret configured; refusing to verify")
    if not value or value.count(".") != 1:
        raise SessionError("malformed session cookie")
    session_id, mac = value.split(".", 1)
    if not session_id:
        raise SessionError("malformed session cookie")
    expected = hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise SessionError("session cookie signature does not match")
    return session_id


def cookie_header(value: str, *, ttl_s: int, secure: bool = True) -> str:
    """A Set-Cookie for a session that has just begun.

    HttpOnly so script cannot read it, SameSite=Lax so it does not ride along
    on a cross-site POST, Secure unless we are plainly on http for local
    development — a Secure cookie over http is silently dropped, which reads
    as sign-in doing nothing at all.
    """
    parts = [f"{COOKIE_NAME}={value}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={int(ttl_s)}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clearing_header(*, secure: bool = True) -> str:
    """A Set-Cookie that ends the session in the browser.

    The attributes have to match the ones it was set with or the browser keeps
    the original and signing out appears to do nothing.
    """
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def read_cookie(header: str | None) -> str | None:
    """Pull our cookie out of a Cookie header, or None.

    Browsers send every cookie for the host in one header, so this has to find
    ours among others rather than assume it is alone.
    """
    if not header:
        return None
    for crumb in header.split(";"):
        name, _, value = crumb.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None


def is_expired(expires_at: float, *, now: float | None = None) -> bool:
    return (time.time() if now is None else now) >= expires_at
