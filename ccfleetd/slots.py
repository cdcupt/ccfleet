"""The slot lifecycle: which states exist and which moves between them are legal.

A slot is one Linux user on one machine — the unit a person holds. It moves
through five states, and the whole point of keeping them here rather than
inline in the store is that one invariant has to be checkable on its own:

    the only way into `free` is out of `releasing`

`free` means the Linux user does not exist. Every other state means it might,
and a slot whose user still exists holds somebody's credential and somebody's
files. Handing that to the next person is the one failure in this system with
no recovery, so `free` is not a state anything may simply declare — it is what
a completed wipe leaves behind.

That is why `claiming` fails sideways into `releasing` rather than back into
`free`: provisioning that died halfway may well have created the account before
it died, and a wipe over a slot that has nothing on it costs nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

FREE = "free"
CLAIMING = "claiming"
CLAIMED = "claimed"
ACTIVE = "active"
RELEASING = "releasing"

STATES: tuple[str, ...] = (FREE, CLAIMING, CLAIMED, ACTIVE, RELEASING)

#: What each state means, and who moves it on. Kept beside the transitions so
#: the two cannot drift; the console renders these.
MEANING: dict[str, str] = {
    FREE: "the Linux user does not exist yet",
    CLAIMING: "provisioning is running on the machine",
    CLAIMED: "Linux user exists, Claude Code installed, nobody signed in",
    ACTIVE: "signed into their own Claude account and working",
    RELEASING: "being wiped",
}

MOVED_ON_BY: dict[str, str] = {
    FREE: "a user claiming it",
    CLAIMING: "the system, or a timeout",
    CLAIMED: "the user, by signing in",
    ACTIVE: "the user, or the operator",
    RELEASING: "the system",
}

#: Every legal move. Note what is absent: nothing reaches FREE except RELEASING,
#: and ACTIVE has exactly one exit.
ALLOWED: dict[str, tuple[str, ...]] = {
    FREE: (CLAIMING,),
    # Not back to FREE. Provisioning may have got as far as creating the
    # account before it failed, and only a wipe can say the slot is empty.
    CLAIMING: (CLAIMED, RELEASING),
    CLAIMED: (ACTIVE, RELEASING),
    ACTIVE: (RELEASING,),
    RELEASING: (FREE,),
}

#: States in which a person holds the slot, so it counts against their quota.
#: FREE does not; RELEASING still does, because the wipe has not finished and
#: handing the slot out again now would hand out their files with it.
HELD: frozenset[str] = frozenset({CLAIMING, CLAIMED, ACTIVE, RELEASING})

#: The states from which a release may be started. A free slot has nothing to
#: wipe and a releasing slot is already being wiped.
RELEASABLE: frozenset[str] = frozenset({CLAIMING, CLAIMED, ACTIVE})

#: What a shared machine's heartbeat says it is. Its agent runs as root and
#: reports its slots rather than an owner login, which it does not have.
MACHINE_MODE = "machine"

#: The two kinds of slot. A machine slot is a Linux user the machine agent
#: makes and wipes, claimed through this lifecycle. An owner slot is a
#: person's own node counted as a slot they hold (Erik, 2026-09-23): a record
#: and nothing more. Nothing on that node is ever provisioned, handed out or
#: wiped because of it, so it never enters the moves above.
MACHINE_SLOT = "machine"
OWNER_SLOT = "owner"
SLOT_KINDS: tuple[str, ...] = (MACHINE_SLOT, OWNER_SLOT)

#: How long provisioning may take before the claim is given up. Creating the
#: account is seconds; installing Claude Code downloads a release, which is a
#: few minutes on a slow link. Past this the machine is down or stuck, and the
#: person is better told no than left watching "setting up" for an evening.
CLAIM_TIMEOUT_S = 30 * 60

def _same_claim(reported: Any, claimed_at: Optional[float]) -> bool:
    """Is this report about this claim? Compared exactly: the timestamp goes
    down as JSON, is kept by the machine as JSON and comes back as JSON, and a
    float survives that unchanged. `True` is not a timestamp, though Python
    would happily compare it as 1."""
    if claimed_at is None or isinstance(reported, bool):
        return False
    if not isinstance(reported, (int, float)):
        return False
    return float(reported) == float(claimed_at)


def next_state(state: str, claimed_at: Optional[float],
               report: Mapping[str, Any]) -> Optional[str]:
    """Where a machine's report about a slot moves it, or None to leave it.

    Only the moves the system owns are decided here. Claiming a free slot and
    starting a release are acts by a person, and never follow from a report.

    A report is evidence about one moment on the machine, so each move asks
    for the evidence that actually proves it:

    - provisioning finished only counts for *this* claim. A slot released and
      claimed again must not be marked ready by news about the claim before.
    - releasing ends only on the machine saying the Linux user does not exist.
      Nothing else — not a timeout, not an absent report — proves the wipe.
    - signing in is the holder's act, and the machine reporting a working login
      is how we learn it happened.
    """
    present = report.get("present")
    if state == CLAIMING:
        # Failure first: a machine that provisioned and then reports the same
        # claim as failed has told us the second, later thing.
        if _same_claim(report.get("provision_failed_for"), claimed_at):
            return RELEASING
        if present is True and _same_claim(report.get("provisioned_for"), claimed_at):
            return CLAIMED
        return None
    if state == CLAIMED:
        credentials = report.get("credentials")
        logged_in = (credentials.get("logged_in")
                     if isinstance(credentials, Mapping) else None)
        if present is True and logged_in is True:
            return ACTIVE
        return None
    if state == RELEASING and present is False:
        return FREE
    return None


class TransitionError(ValueError):
    """A move the lifecycle does not allow."""


def is_state(state: str) -> bool:
    return state in STATES


def can_move(frm: str, to: str) -> bool:
    """Is `frm` -> `to` a legal move? Unknown states are never legal."""
    return to in ALLOWED.get(frm, ())


def check_move(frm: str, to: str) -> None:
    """Raise unless `frm` -> `to` is legal.

    The message names the alternative rather than only the refusal, because the
    caller that gets this wrong is nearly always trying to shortcut a wipe.
    """
    if not is_state(frm):
        raise TransitionError(f"{frm!r} is not a slot state")
    if not is_state(to):
        raise TransitionError(f"{to!r} is not a slot state")
    if can_move(frm, to):
        return
    if to == FREE:
        raise TransitionError(
            f"a slot cannot go from {frm} straight to free: free means the "
            f"Linux user is gone, and only a release proves that. Move it to "
            f"{RELEASING} and let the wipe finish."
        )
    raise TransitionError(
        f"a slot cannot go from {frm} to {to}; from {frm} it may only go to "
        + " or ".join(ALLOWED[frm])
    )
