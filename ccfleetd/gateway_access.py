"""Credentials and selection validation for the public Messages gateway.

Gateway credentials are independent of operator, node and laptop device
tokens. Only a hash is persisted; the plaintext is returned when it is minted.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

KEY_PREFIX = "ccf_api_"
MAX_KEYS_PER_ACCOUNT = 20
MAX_NAME = 60
MAX_SLOTS_PER_KEY = 100
MODES = ("sticky", "priority")
SLOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
TOKEN_RE = re.compile(r"^ccf_api_[A-Za-z0-9_-]{43}$")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def valid_token(token: object) -> bool:
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


def normalize_selection(slot_ids: Any, mode: Any) -> Optional[list[str]]:
    """Keep caller order, refuse duplicates and accept only bounded slot IDs."""
    if (mode not in MODES or not isinstance(slot_ids, list)
            or not 1 <= len(slot_ids) <= MAX_SLOTS_PER_KEY):
        return None
    if any(not isinstance(value, str) or SLOT_ID_RE.fullmatch(value) is None
           for value in slot_ids):
        return None
    if len(set(slot_ids)) != len(slot_ids):
        return None
    return list(slot_ids)


def normalize_name(name: Any) -> Optional[str]:
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        return None
    return " ".join(name.split())
