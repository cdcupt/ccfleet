"""Provider selection and readiness metadata; no credentials or native execution.

Devin needs a separately verified stable provider-account subject and an explicit
bridge contract. Native-agent compatibility does not assert raw Messages role or
feature parity. A token hash, catalog, PONG or lone Read is not readiness evidence.
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
NATIVE_AGENT_CONTRACT = "native-agent-v1"
MAX_BINDING_AGE_S = 300
REQUIRED_CAPABILITIES = frozenset({
    "messages", "system", "history", "local_tools", "streaming", "request_features",
    "truthful_usage", "revocation",
})
NATIVE_AGENT_CAPABILITIES = frozenset({
    # Labeled context is not raw role authority. Client tools must remain
    # caller-local, SSE/cancellation must be honest, and unknown usage stays unknown.
    "labeled_visible_context", "text", "client_tools", "streaming", "cancellation",
    "unknown_usage", "revocation",
})
CONTRACT_CAPABILITIES = {
    BRIDGE_CONTRACT: REQUIRED_CAPABILITIES,
    NATIVE_AGENT_CONTRACT: NATIVE_AGENT_CAPABILITIES,
}
RUNTIME_FIELDS = frozenset({"bridge_contract", "native_version", "adapter_revision",
                            "model_map_digest"})
REPORT_FIELDS = frozenset({
    "account_fp", "identity_verified", "bridge_contract", "capabilities", "native_version",
    "adapter_revision", "model_map_digest", "observed_at",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CLAIM = re.compile(r"[0-9a-f]{32}\Z")
_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}\Z")
_PLACEHOLDER_NAME = "DeferredToolPlaceholder"
_PLACEHOLDER_DESCRIPTION = (
    "Reserved placeholder that keeps deferred tool loading active; never call this tool."
)


def native_client_request(body: Any) -> Any:
    """Remove one known inert CLI annotation before the pinned native contract.

    The reserved empty placeholder remains declared and cannot execute a real
    action. Real tools, history and all generation controls remain unchanged;
    unknown declarations still fail the existing native validator.
    """
    if not isinstance(body, dict) or not isinstance(body.get("tools"), list):
        return body
    changed = False
    declarations = []
    for tool in body["tools"]:
        if (isinstance(tool, dict) and tool.get("name") == _PLACEHOLDER_NAME
                and tool.get("description") == _PLACEHOLDER_DESCRIPTION
                and tool.get("input_schema") == {"type": "object", "properties": {}}
                and tool.get("defer_loading") is True
                and not tool.keys() - {"name", "description", "input_schema",
                                       "cache_control", "defer_loading"}):
            tool = {name: value for name, value in tool.items() if name != "defer_loading"}
            changed = True
        declarations.append(tool)
    return {**body, "tools": declarations} if changed else body


def normalize_provider(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in PROVIDERS else None


def _instant(value: Any) -> float:
    if (type(value) not in (int, float) or not 0 <= value <= 10**11
            or not math.isfinite(value)):
        raise ValueError("invalid provider observation time")
    return float(value)


def clean_runtime(value: Any) -> dict[str, str]:
    """Validate an operator-selected runtime; this never enables or discovers one."""
    if (not isinstance(value, Mapping) or set(value) != RUNTIME_FIELDS
            or not isinstance(value.get("bridge_contract"), str)
            or value.get("bridge_contract") not in CONTRACT_CAPABILITIES
            or not isinstance(value.get("native_version"), str)
            or not _VERSION.fullmatch(value["native_version"])
            or any(not isinstance(value.get(key), str) or not _DIGEST.fullmatch(value[key])
                   for key in ("adapter_revision", "model_map_digest"))):
        raise ValueError("invalid expected provider runtime")
    return dict(value)


def runtime_expectations(cfg: Any) -> Optional[dict[str, str]]:
    """Format-only native-agent profile; flags and verified availability remain separate."""
    value = {"bridge_contract": NATIVE_AGENT_CONTRACT,
             "native_version": getattr(cfg, "gateway_devin_native_version", ""),
             "adapter_revision": getattr(cfg, "gateway_devin_adapter_revision", ""),
             "model_map_digest": getattr(cfg, "gateway_devin_model_map_digest", "")}
    try:
        return clean_runtime(value)
    except (ValueError, TypeError):
        return None


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
    allowed = CONTRACT_CAPABILITIES.get(contract, REQUIRED_CAPABILITIES)
    if (not isinstance(capabilities, Mapping) or set(capabilities) - allowed
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


def binding_ready(binding: Any, slot: Any, account_id: str, provider: str, *, now: float,
                  provider_runtime: Optional[Mapping[str, Any]] = None) -> bool:
    """Default to strict Messages; native compatibility needs an exact expected profile."""
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
        runtime = clean_runtime(provider_runtime) if provider_runtime is not None else None
        contract = runtime["bridge_contract"] if runtime is not None else BRIDGE_CONTRACT
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    return bool(report["identity_verified"] and report["account_fp"]
                and report["bridge_contract"] == contract
                and (runtime is None or all(report[key] == value for key, value in runtime.items()))
                and 0 <= age <= MAX_BINDING_AGE_S
                and all(report["capabilities"].get(key) is True
                        for key in CONTRACT_CAPABILITIES[contract]))


def available_providers(cfg: Any, binding: Any, slot: Any, account_id: str, *, now: float
                        ) -> tuple[str, ...]:
    """Customer choices require the flag, approved runtime and fresh own-account evidence."""
    runtime = runtime_expectations(cfg)
    if (getattr(cfg, "gateway_ready", False) is True
            and getattr(cfg, "gateway_devin_enabled", False) is True
            and runtime is not None
            and binding_ready(binding, slot, account_id, DEVIN, now=now,
                              provider_runtime=runtime)):
        return PROVIDERS
    return (ANTHROPIC,)
