"""Dormant provider selection metadata; no dispatch, login or credential handling.

Devin needs a separately verified stable provider-account subject and the full
Messages bridge contract. A token hash, native login, catalog or PONG is not that
evidence. No current customer UI or native transport registers this provider.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Optional

ANTHROPIC = "anthropic"
DEVIN = "devin"
PROVIDERS = (ANTHROPIC, DEVIN)
BRIDGE_CONTRACT = "messages-v1"
MAX_BINDING_AGE_S = 300
REQUIRED_CAPABILITIES = frozenset({
    "messages", "system", "history", "local_tools", "streaming", "request_features",
    "truthful_usage", "revocation",
})
REPORT_FIELDS = frozenset({
    "account_fp", "identity_verified", "bridge_contract", "capabilities", "native_version",
    "adapter_revision", "model_map_digest", "observed_at",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CLAIM = re.compile(r"[0-9a-f]{32}\Z")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}\Z")


def normalize_provider(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in PROVIDERS else None


def _instant(value: Any) -> float:
    if (type(value) not in (int, float) or not 0 <= value <= 10**11
            or not math.isfinite(value)):
        raise ValueError("invalid provider observation time")
    return float(value)


def clean_report(value: Any) -> dict[str, Any]:
    """Allowlist non-secret evidence; the authenticated caller verifies its source.

    account_fp is the fingerprint of a stable native provider-account subject,
    verified separately by the caller. This module never derives it from tokens.
    Partial capability evidence may be recorded, but cannot become ready.
    """
    if not isinstance(value, Mapping) or set(value) != REPORT_FIELDS:
        raise ValueError("invalid provider binding report fields")
    account = value["account_fp"]
    if account is not None and (not isinstance(account, str) or not _DIGEST.fullmatch(account)):
        raise ValueError("invalid provider account fingerprint")
    if type(value["identity_verified"]) is not bool or value["identity_verified"] and not account:
        raise ValueError("verified native account identity is required")
    contract = value["bridge_contract"]
    if (not isinstance(contract, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", contract)):
        raise ValueError("invalid provider bridge contract")
    capabilities = value["capabilities"]
    if (not isinstance(capabilities, Mapping) or set(capabilities) - REQUIRED_CAPABILITIES
            or any(type(item) is not bool for item in capabilities.values())):
        raise ValueError("invalid provider capability evidence")
    version = value["native_version"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("invalid native provider version")
    for field in ("adapter_revision", "model_map_digest"):
        if not isinstance(value[field], str) or not _DIGEST.fullmatch(value[field]):
            raise ValueError("invalid provider revision")
    return {**{key: value[key] for key in REPORT_FIELDS - {"capabilities", "observed_at"}},
            "capabilities": dict(capabilities), "observed_at": _instant(value["observed_at"])}


def binding_ready(binding: Any, slot: Any, account_id: str, provider: str, *, now: float) -> bool:
    """Require exact current owner/provider/rental and fresh full-contract evidence."""
    if not isinstance(binding, Mapping) or not isinstance(slot, Mapping):
        return False
    if (normalize_provider(provider) is None or binding.get("provider") != provider
            or not isinstance(account_id, str) or not account_id
            or slot.get("held_by") != account_id or binding.get("owner_account_id") != account_id
            or slot.get("kind") != "machine" or slot.get("state") not in {"claimed", "active"}
            or type(slot.get("present")) not in (bool, int) or slot["present"] != 1
            or binding.get("slot_id") != slot.get("id")
            or not isinstance(slot.get("gateway_claim_id"), str)
            or not _CLAIM.fullmatch(slot["gateway_claim_id"])
            or binding.get("claim_id") != slot["gateway_claim_id"]
            or "revoked_at" not in binding or binding["revoked_at"] is not None
            or type(binding.get("generation")) is not int or binding["generation"] < 1):
        return False
    try:
        report = clean_report({key: binding[key] for key in REPORT_FIELDS})
        age = _instant(now) - report["observed_at"]
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    return bool(report["identity_verified"] and report["account_fp"]
                and report["bridge_contract"] == BRIDGE_CONTRACT
                and 0 <= age <= MAX_BINDING_AGE_S
                and all(report["capabilities"].get(key) is True for key in REQUIRED_CAPABILITIES))


def available_providers(cfg: Any, binding: Any, slot: Any, account_id: str, *, now: float
                        ) -> tuple[str, ...]:
    """Future UI helper: flag alone, partial evidence and another owner never suffice."""
    if (getattr(cfg, "gateway_ready", False) is True
            and getattr(cfg, "gateway_devin_enabled", False) is True
            and binding_ready(binding, slot, account_id, DEVIN, now=now)):
        return PROVIDERS
    return (ANTHROPIC,)
