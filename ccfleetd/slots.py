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
