"""Password hashing for console accounts, standard library only.

The project has no runtime dependencies and this is not the place to acquire
one, so this uses PBKDF2-HMAC-SHA256 from hashlib rather than argon2 or bcrypt.
PBKDF2 is the weaker choice against a GPU attacker; it is chosen because it is
the strongest thing available without a dependency, and because the threat here
is narrow: these accounts read a private dashboard, the hashes live in a SQLite
file only root can read, and a node token is never derived from them.

Format is self-describing so the cost can be raised later without invalidating
existing hashes: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000          # OWASP's 2023 floor for PBKDF2-HMAC-SHA256
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Returns False for anything malformed rather than raising."""
    try:
        algorithm, raw_iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    if iterations < 1 or not salt or not expected:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def generate_password(length: int = 20) -> str:
    """A password for an operator to hand over, when they do not supply one."""
    return secrets.token_urlsafe(length)[:length]
