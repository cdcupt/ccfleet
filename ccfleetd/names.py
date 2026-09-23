"""The names people see: a slot is named after whoever holds it.

One machine is one slot (Erik, 2026-09-23), and claude.ai/code shows a machine
by its hostname, so a slot's name is also the name its machine answers to
there. Everything made here is therefore a hostname label — lowercase letters,
digits and inner hyphens, at most 63 characters — and the holder's handle is
kept short enough that "<handle>-<n>" always fits.

A slot's id never changes: sign-ins, requests and pages are keyed on it. The
name is separate, set when somebody claims the slot and cleared when the wipe
that frees it completes, and the id stands in for it whenever there is none.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

#: At most this long, so "<handle>-<n>" is a comfortable hostname.
MAX_HANDLE = 20
#: For an address with nothing usable before the @.
FALLBACK_HANDLE = "user"

# fullmatch, never match-with-$: `$` also matches before a trailing newline,
# and a name with a newline in it is a hostname nobody can type.
_HANDLE_RE = re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{MAX_HANDLE - 2}}}[a-z0-9])?")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NOT_KEPT = re.compile(r"[^a-z0-9]+")


def handle_from_email(email: str) -> str:
    """The part of an address before the @, made hostname-safe.

    Lowercased; every run of anything but a-z and 0-9 becomes one hyphen,
    letters outside ASCII included; cut to MAX_HANDLE; no hyphen at either
    end. "user" when nothing is left.
    """
    local = (email or "").split("@", 1)[0].lower()
    kept = _NOT_KEPT.sub("-", local).strip("-")
    return kept[:MAX_HANDLE].strip("-") or FALLBACK_HANDLE


def valid_handle(handle: Any) -> bool:
    """A handle an operator may choose: what handle_from_email could make."""
    return isinstance(handle, str) and _HANDLE_RE.fullmatch(handle) is not None


def valid_hostname(name: Any) -> bool:
    """One hostname label, as `hostnamectl` and /etc/hosts both take it."""
    return isinstance(name, str) and _HOSTNAME_RE.fullmatch(name) is not None


def next_name(handle: str, taken: Collection[str]) -> str:
    """"<handle>-<n>" for the smallest n nobody already answers to."""
    k = 1
    while f"{handle}-{k}" in taken:
        k += 1
    return f"{handle}-{k}"


def display(slot: Mapping[str, Any]) -> str:
    """What a slot is called on every page: its holder's name, else its id."""
    return slot.get("name") or slot["id"]
