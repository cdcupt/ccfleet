"""Content-free request errors before dispatch to an exactly reviewed native profile.

This is advisory admission, never provider readiness or execution authority. The
node's installed validator, policy and authorization checks remain mandatory.
Unknown runtime profiles retain their existing node-side behavior.
"""

from __future__ import annotations

from ccfleet_agent import devin_native as native

from .gateway_providers import NATIVE_AGENT_CONTRACT
from .native_visible_context import visible_history

# Reviewed against the installed adapter's exact validator/context format and
# both ordinary root policies on 2026-10-20. The map digest alone does not bind
# thought_level; max is evidence for this complete tuple, not a global default.
_REVIEWED_RUNTIME = {
    "bridge_contract": NATIVE_AGENT_CONTRACT,
    "native_version": "3000.11.92",
    "adapter_revision": "c5da172d278bd099c19aaec473fb17c1bd5117ba6ba34454ae82cf9a7027244e",
    "model_map_digest": "2770367d5b1a206af81433e026cd38af2338e6116b193632e6933c4061c01291",
}
_REQUEST_MODELS = ("claude-opus-5-5", "opus")
_ERRORS = {
    "unsupported_model": "This Devin route supports Opus 5.5. Start Claude Code with "
        "--model opus --effort max; another model or a resumed session may require Claude.",
    "unsupported_native_request": "This Devin route does not support these request controls. "
        "Use a supported text-and-local-tool request or select Claude.",
    "unsupported_output_control": "This Devin route requires max effort and does not support "
        "structured output. Start Claude Code with --model opus --effort max. "
        "A resumed session may retain its previous effort.",
    "unsupported_generation_control": "This Devin route supports automatic tool selection, "
        "a boolean stream setting and an integer max_tokens value from 1 to 1,000,000 only.",
    "unsupported_thinking_control": "This Devin route supports adaptive thinking only. "
        "Budgeted or disabled thinking requires a different request or Claude.",
    "unsupported_context_control": "This Devin route does not support the requested context "
        "management edits. Use the supported clear-thinking/keep-all setting or select Claude.",
    "unsupported_content": "This Devin route supports text and local tool history only. "
        "Images, documents, tool references and unsupported reasoning shapes are unsupported. "
        "Start a new supported conversation or select Claude; history was not removed.",
    "unsupported_message": "This Devin route does not support this message shape. "
        "Start a new supported conversation or select Claude; history was not removed.",
    "unsupported_tool_history": "This Devin route cannot use the supplied tool-call history. "
        "Start a new supported conversation or select Claude; history was not removed.",
    "unsupported_tool_result": "This Devin route cannot use the supplied tool-result history. "
        "Start a new supported conversation or select Claude; history was not removed.",
    "unsupported_empty_tool_catalog": "This Devin route requires at least one declared local "
        "tool. Enable a supported local tool or select Claude.",
    "unsupported_tool_declaration": "This Devin route supports plain declared local tools. "
        "Deferred, server-side or other extended tool declarations are unsupported. "
        "Disable unsupported tools or select Claude; tools were not removed.",
    "invalid_tool_catalog": "This Devin route requires 1–512 local tools with unique names "
        "of at most 64 supported characters and object input schemas. "
        "Reduce or repair the tool catalog, or select Claude.",
    "tool_catalog_count_limit": "This Devin route supports at most 512 local tools. "
        "Disable some MCP servers or tools and start a new conversation, or select Claude.",
    "tool_catalog_name_invalid": "A local tool name is invalid. Names must contain 1–64 "
        "letters, digits, underscores or hyphens. Repair or disable the tool, or select Claude.",
    "tool_catalog_name_duplicate": "Two local tools have the same name. Rename or disable "
        "one of them and start a new conversation, or select Claude.",
    "tool_catalog_schema_invalid": "A local tool has an unsupported input schema. "
        "Its input_schema must declare type object. Repair or disable the tool, or select Claude.",
    "tool_catalog_description_invalid": "A local tool description must be text of at most "
        "64 KiB. Repair or disable the tool, or select Claude.",
    "tool_catalog_complexity_limit": "The local tool catalog exceeds the JSON size or "
        "complexity limit. Disable some MCP servers or simplify tool schemas, or select Claude.",
    "unsupported_cache_metadata": "This Devin route supports ephemeral cache metadata "
        "with a 5m or 1h TTL only.",
    "invalid_messages_body": "This Devin route requires valid bounded Messages JSON. "
        "Reduce the request or select Claude.",
    # Keep the established native-client overflow signal. HTTP 413 describes a
    # transport limit and is not the existing Claude context-recovery contract.
    "native_context_limit": "prompt is too long: this Devin route accepts at most 256 KiB "
        "of formatted context including tools and history. Compact the conversation, "
        "reduce tools, or start a new session; context was not removed.",
}

_CATALOG_ERRORS = {
    "invalid_catalog_count": "tool_catalog_count_limit",
    "invalid_catalog_name": "tool_catalog_name_invalid",
    "duplicate_catalog_name": "tool_catalog_name_duplicate",
    "invalid_catalog_schema": "tool_catalog_schema_invalid",
    "invalid_catalog_description": "tool_catalog_description_invalid",
    "limit_exceeded": "tool_catalog_complexity_limit",
}


def _catalog_error(body):
    """Explain a native catalog rejection with static codes, never request values.

    Older pinned adapters deliberately mask the bridge's precise reason. Reuse
    the same pure catalog validation after that rejection; this does not change
    native admission or require changing the adapter's reviewed identity.
    """
    try:
        declarations = [{key: tool[key] for key in ("name", "description", "input_schema")}
                        for tool in body["tools"]]
        native.tools.ToolCatalog(declarations)
    except native.tools.ToolBridgeError as error:
        return _CATALOG_ERRORS.get(error.code, "invalid_tool_catalog")
    except (KeyError, TypeError):
        pass
    return "invalid_tool_catalog"


class RequestRejected(ValueError):
    """Only authored codes/messages, never exception or request text."""

    def __init__(self, code):
        self.code = (code if isinstance(code, str) and code in _ERRORS
                     else "unsupported_native_request")
        self.message = _ERRORS[self.code]
        self.status = 400
        super().__init__(self.message)


def validate(body, *, request_models, effort):
    """Pure preflight shared with the independently fixed trial contract."""
    try:
        body = visible_history(body)
        clean = native.validate_native_request(body, request_models=request_models, effort=effort)
        native.native_context(clean)
        return clean
    except native.NativeError as error:
        code = error.code
        if code == "invalid_tool_catalog":
            code = _catalog_error(body)
        if code in {"body_limit", "invalid_bounded_json"}:
            code = "invalid_messages_body"
        if (code == "unsupported_native_request" and isinstance(body, dict)
                and body.get("model") not in request_models):
            code = "unsupported_model"
        raise RequestRejected(code) from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # Some malformed JSON field shapes fail a validator membership/type
        # operation. Never turn its exception text into a public diagnostic.
        raise RequestRejected("invalid_messages_body") from None


def gateway_request(raw, target):
    """Check only the authorized target's exact reviewed profile, before SSH."""
    if (target.get("provider") != "devin"
            or any(target.get(name) != value for name, value in _REVIEWED_RUNTIME.items())):
        return raw
    try:
        body = native.relay.decode_json(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RequestRejected("invalid_messages_body") from None
    clean = validate(body, request_models=_REQUEST_MODELS, effort="max")
    return native._encode(clean)
