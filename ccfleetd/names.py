"""The names people see: a slot's name is its holder's to choose.

One machine is one slot (Erik, 2026-09-23), and claude.ai/code shows a machine
by its hostname, so a slot's name is also the name its machine answers to
there, and Anthropic receives it. So it is never made from anything of the
holder's (Erik, 2026-09-24): a claim gets a neutral "slot-4821", and the holder
renames it on their page. Everything made here is a hostname label —
lowercase letters, digits and inner hyphens, at most 63 characters.

A slot's id never changes: sign-ins, requests and pages are keyed on it. The
name is separate, set when somebody claims the slot and cleared when the wipe
that frees it completes, and the id stands in for it whenever there is none.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Collection, Mapping
from typing import Any

#: At most this long, so "<handle>-<n>" is a comfortable hostname.
MAX_HANDLE = 20
#: What a slot is called until its holder names it: "slot-" and random digits.
NEUTRAL_PREFIX = "slot-"
NEUTRAL_DIGITS = 4
#: Draws before a neutral name takes one more digit: four are crowded by then.
NEUTRAL_TRIES = 20
#: A holder's own name for their slot, at most this long.
MAX_NICKNAME = 30

# fullmatch, never match-with-$: `$` also matches before a trailing newline,
# and a name with a newline in it is a hostname nobody can type.
_HANDLE_RE = re.compile(rf"[a-z0-9](?:[a-z0-9-]{{0,{MAX_HANDLE - 2}}}[a-z0-9])?")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NICKNAME_RE = re.compile(rf"[a-z0-9][a-z0-9-]{{0,{MAX_NICKNAME - 2}}}[a-z0-9]")
# The operator's own words for machines and the neutral names: a holder's
# nickname never takes their shape, so no name reads as a pool machine's or
# waits to become a neutral one that another slot is later given.
_RESERVED_RE = re.compile(r"(?:pool|slot)-[0-9]+")


def valid_handle(handle: Any) -> bool:
    """A handle an operator may set, at the person's request: 1-20 lowercase
    letters, digits and inner hyphens, so "<handle>-<n>" is a hostname."""
    return isinstance(handle, str) and _HANDLE_RE.fullmatch(handle) is not None


def valid_hostname(name: Any) -> bool:
    """One hostname label, as `hostnamectl` and /etc/hosts both take it."""
    return isinstance(name, str) and _HOSTNAME_RE.fullmatch(name) is not None


def valid_nickname(name: Any) -> bool:
    """A name a holder may give their slot: 2-30 lowercase letters, digits and
    inner hyphens, and not "pool-<n>" or "slot-<n>"."""
    return (isinstance(name, str) and _NICKNAME_RE.fullmatch(name) is not None
            and _RESERVED_RE.fullmatch(name) is None)


def neutral_name(taken: Collection[str],
                 pick: Callable[[int], int] = secrets.randbelow) -> str:
    """"slot-4821": random digits nobody answers to yet, one more digit
    whenever the draws keep landing on names already taken."""
    digits = NEUTRAL_DIGITS
    while True:
        for _ in range(NEUTRAL_TRIES):
            name = f"{NEUTRAL_PREFIX}{pick(10 ** digits):0{digits}d}"
            if name not in taken:
                return name
        digits += 1


def next_name(handle: str, taken: Collection[str]) -> str:
    """"<handle>-<n>" for the smallest n nobody already answers to."""
    k = 1
    while f"{handle}-{k}" in taken:
        k += 1
    return f"{handle}-{k}"


def display(slot: Mapping[str, Any]) -> str:
    """What a slot is called on every page: its holder's name, else its id."""
    return slot.get("name") or slot["id"]
