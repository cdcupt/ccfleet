"""Read-only paid-claim checks for new access, independent of provider identity."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

from . import slots, wallet

EXPIRED = "This slot's paid period has ended. Renew it on Your slots."
UNAVAILABLE = "This slot's payment status is unavailable. Check Your slots."
SUBSCRIPTION_PAUSED = "This monthly rental is paused. Check Monthly payments on Your slots."

def paid_through(store: Any, slot: Mapping[str, Any]) -> Optional[float]:
    """None means a grandfathered current claim, never a failed financial read."""
    if slot.get("kind") == slots.OWNER_SLOT:
        return None
    owner, identifier, claim = slot.get("held_by"), slot.get("id"), slot.get("gateway_claim_id")
    if (not isinstance(owner, str) or not owner or not isinstance(identifier, str)
            or not identifier or not isinstance(claim, str) or not claim):
        raise ValueError("current held slot required")
    end = store.paid_slot_through(owner, identifier, claim_id=claim)
    if end is not None and (type(end) not in (int, float) or not math.isfinite(end) or end < 0):
        raise ValueError("paid claim end is unavailable")
    return end


def allowed(store: Any, slot: Mapping[str, Any], *, now: float) -> bool:
    try:
        now = wallet.instant(now)
        if slot.get("kind") != slots.OWNER_SLOT:
            recurring = store.slot_subscription_access(slot.get("held_by"), slot.get("id"),
                claim_id=slot.get("gateway_claim_id"), now=now)
            if recurring is not None:
                return recurring["allowed"] is True
        end = paid_through(store, slot)
        return end is None or now < end
    except Exception:
        return False


def target_allowed(store: Any, account_id: str, target: Mapping[str, Any], *, now: float) -> bool:
    return not target_pause_reason(store, account_id, target, now=now)


def target_pause_reason(store: Any, account_id: str, target: Mapping[str, Any],
                        *, now: float) -> str:
    try:
        now = wallet.instant(now)
        slot = store.get_slot(target["slot_id"])
        if (slot is None or slot.get("held_by") != account_id
                or "claim_generation" in target
                and slot.get("gateway_generation") != target["claim_generation"]
                or "claim_id" in target and slot.get("gateway_claim_id") != target["claim_id"]):
            return UNAVAILABLE
        recurring = store.slot_subscription_access(account_id, slot["id"],
            claim_id=slot["gateway_claim_id"], now=now)
        if recurring is not None:
            return "" if recurring["allowed"] is True else SUBSCRIPTION_PAUSED
        end = paid_through(store, slot)
        return EXPIRED if end is not None and now >= end else ""
    except Exception:
        return UNAVAILABLE
