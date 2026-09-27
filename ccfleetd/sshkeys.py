"""Validation and privacy-preserving display of slot SSH public keys.

Only public keys cross the fleet server. Comments are deliberately discarded:
OpenSSH commonly puts an email address there, and the key blob already contains
everything the server and machine need.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct

SUPPORTED_KEY_TYPES = frozenset({
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "ssh-rsa",
})
MAX_PUBLIC_KEY_CHARS = 16 * 1024


class PublicKeyError(ValueError):
    """The supplied text is not one supported OpenSSH public key."""


def normalize(value: str) -> str:
    """Return ``type base64`` for one valid OpenSSH public key."""
    if not isinstance(value, str):
        raise PublicKeyError("paste one SSH public key")
    line = value.strip()
    if not line or len(line) > MAX_PUBLIC_KEY_CHARS or "\n" in line or "\r" in line:
        raise PublicKeyError("paste one SSH public key on one line")
    parts = line.split()
    if len(parts) < 2 or parts[0] not in SUPPORTED_KEY_TYPES:
        raise PublicKeyError("use an Ed25519, ECDSA, security-key, or RSA public key")
    key_type, encoded = parts[:2]
    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise PublicKeyError("the SSH public key is not valid base64") from exc
    if len(blob) < 8:
        raise PublicKeyError("the SSH public key is incomplete")
    size = struct.unpack(">I", blob[:4])[0]
    if size <= 0 or size > len(blob) - 4:
        raise PublicKeyError("the SSH public key is malformed")
    try:
        embedded = blob[4:4 + size].decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublicKeyError("the SSH public key type is malformed") from exc
    if embedded != key_type:
        raise PublicKeyError("the SSH public key type does not match its contents")
    # Canonical base64 prevents two spellings of one key. The comment is left
    # out because it often contains an address or a local machine name.
    return f"{key_type} {base64.b64encode(blob).decode('ascii')}"


def fingerprint(normalized: str) -> str:
    """OpenSSH-style SHA256 fingerprint for an already-normalized key."""
    try:
        blob = base64.b64decode(normalized.split()[1].encode("ascii"), validate=True)
    except (IndexError, UnicodeEncodeError, binascii.Error) as exc:  # defensive
        raise PublicKeyError("the stored SSH public key is malformed") from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"
