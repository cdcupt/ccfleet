"""Pure collection of one owned ACP text response; no inference or wire writer.

Input notifications are the params-shaped objects from ACPController's
drain_notifications(), not raw JSON-RPC envelopes. Configuration is supplied by
the caller after separate verification; it does not prove the served model.
Context occupancy and cost notices never become input/output token accounting.
This collector cannot prevent actions inside a native agent; it rejects observed
tool activity rather than describing it as a successful text-only response.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_NOTIFICATIONS = 1024
MAX_FRAGMENTS = 1024
MAX_FRAGMENT_BYTES = 64 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_METADATA_STRING_BYTES = 4 * 1024 * 1024
MAX_JSON_NODES = 50000
MAX_JSON_DEPTH = 48
STOP_REASONS = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"})
IGNORED_UPDATES = frozenset({"usage_update", "config_option_update", "current_mode_update",
                             "available_commands_update", "session_info_update",
                             "agent_thought_chunk", "user_message_chunk", "plan"})
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


class ResultError(ValueError):
    def __init__(self, message: str, code: str = "invalid_result"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NativeTextResult:
    text: str
    stop_reason: str
    configured_model: str
    served_model: Optional[str] = field(default=None, init=False)
    usage: Optional[dict[str, int]] = field(default=None, init=False)


def _utf8(value: Any, label: str) -> int:
    if not isinstance(value, str):
        raise ResultError(f"invalid {label}")
    if len(value) > MAX_METADATA_STRING_BYTES:
        raise ResultError("result string exceeds bound", "limit_exceeded")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ResultError(f"invalid Unicode in {label}") from exc
    if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in value):
        raise ResultError(f"invalid control character in {label}")
    return size


def _structure(value: Any, budget: list[int], depth: int = 0) -> None:
    """Bound even ignored metadata without serializing or retaining its content."""
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise ResultError("result JSON structure exceeds bound", "limit_exceeded")
    if value is None or type(value) is bool:
        return
    if isinstance(value, str):
        budget[1] += _utf8(value, "result string")
        if budget[1] > MAX_METADATA_STRING_BYTES:
            raise ResultError("result metadata strings exceed bound", "limit_exceeded")
    elif type(value) is int:
        if value.bit_length() > 3402:
            raise ResultError("result integer exceeds bound", "limit_exceeded")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ResultError("nonfinite result number")
    elif isinstance(value, list):
        for item in value:
            _structure(item, budget, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultError("invalid result JSON key")
            _structure(key, budget, depth + 1)
            _structure(item, budget, depth + 1)
    else:
        raise ResultError("invalid result JSON value")


def validate_result(notifications: Iterable[dict[str, Any]], prompt_result: dict[str, Any], *,
                    session_id: str, configured_model: str) -> NativeTextResult:
    """Return validated text and completion state, never tools or invented usage.

    Ignored update kinds have explicit names and remain bounded and owned by the
    same session. Unknown kinds fail closed. No metadata is copied to the result.
    A refusal, truncation, cancellation or empty reply remains exactly that;
    recognition of its stop reason does not label it successful.
    """
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        raise ResultError("invalid owned session ID", "invalid_config")
    if not isinstance(configured_model, str) or not _MODEL.fullmatch(configured_model):
        raise ResultError("invalid configured model", "invalid_config")
    if (not isinstance(notifications, Iterable)
            or isinstance(notifications, (str, bytes, dict))):
        raise ResultError("notifications must be an iterable of ACP parameters")
    budget = [0, 0]
    _structure(prompt_result, budget)
    if not isinstance(prompt_result, dict):
        raise ResultError("invalid ACP prompt result")
    stop = prompt_result.get("stopReason")
    if not isinstance(stop, str) or stop not in STOP_REASONS:
        raise ResultError("missing or unsupported ACP stop reason")
    pieces: list[str] = []
    text_bytes = 0
    for count, notification in enumerate(notifications, 1):
        if count > MAX_NOTIFICATIONS:
            raise ResultError("notification count exceeds bound", "limit_exceeded")
        _structure(notification, budget)
        if (not isinstance(notification, dict)
                or set(notification) - {"sessionId", "update", "_meta"}
                or notification.get("sessionId") != session_id):
            raise ResultError("notification is not owned by this session", "session_mismatch")
        update = notification.get("update")
        if not isinstance(update, dict):
            raise ResultError("invalid ACP session update")
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str):
            raise ResultError("invalid ACP update kind")
        if kind in {"tool_call", "tool_call_update"}:
            raise ResultError("tool activity is unsupported in text-only results", "tool_activity")
        if kind in IGNORED_UPDATES:
            continue
        if kind != "agent_message_chunk":
            raise ResultError("unsupported ACP update kind", "unsupported_update")
        if set(update) - {"sessionUpdate", "content", "_meta"}:
            raise ResultError("unsupported fields in ACP text update", "unsupported_content")
        content = update.get("content")
        if (not isinstance(content, dict) or set(content) != {"type", "text"}
                or content.get("type") != "text"):
            raise ResultError("only plain ACP agent text is supported", "unsupported_content")
        text = content["text"]
        size = _utf8(text, "agent text")
        if size > MAX_FRAGMENT_BYTES:
            raise ResultError("text fragment exceeds bound", "limit_exceeded")
        if len(pieces) >= MAX_FRAGMENTS:
            raise ResultError("text fragment count exceeds bound", "limit_exceeded")
        text_bytes += size
        if text_bytes > MAX_TEXT_BYTES:
            raise ResultError("agent text exceeds bound", "limit_exceeded")
        pieces.append(text)
    return NativeTextResult("".join(pieces), stop, configured_model)
