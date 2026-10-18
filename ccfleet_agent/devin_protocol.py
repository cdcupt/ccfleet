"""Bounded, network-free Messages/Devin Chat Completions pilot adapter.

This keeps tools in the caller's Claude Code process; it never runs a Devin
agent. Model mappings must come from a separately verified account catalogue.
No login, billing, entitlement, HTTP, retry, or fallback policy lives here.

The deliberately small contract accepts text and native client tools only.
Thinking, images, server tools, cache directives, and structured outputs fail
closed. SSE input is assembled and validated before output is emitted: vendor
usage normally arrives last, so this pilot trades token latency for truthful
usage and avoids executing a partially validated tool call. Output preserves
each tool argument fragment, grouped by the upstream tool's index.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Union

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_SSE_CHUNK_BYTES = 1024 * 1024
MAX_SSE_EVENT_BYTES = 1024 * 1024
MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024
MAX_MESSAGES = 1024
MAX_TOOLS = 128
MAX_BLOCKS = 4096
MAX_OUTPUT_TOKENS = 131072
MAX_TOKEN_COUNT = 1_000_000_000
MAX_JSON_DEPTH = 64
MAX_JSON_INTEGER_DIGITS = 1024
MAX_JSON_INTEGER = 10 ** MAX_JSON_INTEGER_DIGITS - 1
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
JSONInput = Union[bytes, dict[str, Any]]


class DevinProtocolError(ValueError):
    """A sanitized error; callers must not retry/fallback after partial output."""

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedRequest:
    requested_model: str
    actual_model: str
    body: dict[str, Any]
    tool_names: frozenset[str]
    prior_tool_ids: frozenset[str]


def _fail(message: str, code: str = "invalid_request") -> None:
    raise DevinProtocolError(message, code)


def _keys(value: Any, allowed: set[str], required: set[str], where: str,
          code: str = "invalid_request") -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"invalid {where}", code)
    if value.keys() - allowed:
        _fail(f"unsupported fields in {where}", "unsupported_feature")
    if not required <= value.keys():
        _fail(f"invalid {where}", code)
    return value


def _string(value: Any, where: str, *, empty: bool = True,
            pattern: Optional[re.Pattern[str]] = None,
            code: str = "invalid_request") -> str:
    if not isinstance(value, str) or (not empty and not value):
        _fail(f"invalid {where}", code)
    if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in value):
        _fail(f"invalid control character in {where}", code)
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail(f"invalid Unicode in {where}", code)
    if pattern is not None and not pattern.fullmatch(value):
        _fail(f"invalid {where}", code)
    return value


def _integer(value: Any, low: int, high: int, where: str,
             code: str = "invalid_request") -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"invalid {where}", code)
    return value


def _number(value: Any, low: float, high: float, where: str) -> Union[int, float]:
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        _fail(f"invalid {where}")
    return value


def _validate_json(value: Any, depth: int = 0, code: str = "invalid_request") -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("JSON nesting exceeds the bound", "limit_exceeded")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        # Compare integers without converting a potentially huge Python int to
        # decimal. This also bounds dictionaries supplied without JSON parsing.
        if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
            _fail("JSON integer exceeds the bound", "limit_exceeded")
        return
    if isinstance(value, str):
        _string(value, "JSON string", code=code)
    elif type(value) is float:
        if not math.isfinite(value):
            _fail("nonfinite JSON number", code)
    elif isinstance(value, list):
        for child in value:
            _validate_json(child, depth + 1, code)
    elif isinstance(value, dict):
        for key, child in value.items():
            _string(key, "JSON key", code=code)
            _validate_json(child, depth + 1, code)
    else:
        _fail("invalid JSON value", code)


def _dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _parse_integer(text: str) -> int:
    # A size check after json.loads is too late on Python versions without a
    # decimal conversion guard: reject the lexeme before calling int().
    digits = len(text) - (1 if text.startswith("-") else 0)
    if digits > MAX_JSON_INTEGER_DIGITS:
        _fail("JSON integer exceeds the bound", "limit_exceeded")
    return int(text)


def _load(value: JSONInput, limit: int, code: str) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                _fail("duplicate JSON field", code)
            result[key] = item
        return result

    def nonfinite(_value):
        _fail("nonfinite JSON number", code)

    try:
        if isinstance(value, bytes):
            if len(value) > limit:
                _fail("JSON payload exceeds the bound", "limit_exceeded")
            value = json.loads(value.decode("utf-8"), object_pairs_hook=unique,
                               parse_constant=nonfinite, parse_int=_parse_integer)
        if not isinstance(value, dict):
            _fail("JSON payload must be an object", code)
        _validate_json(value, code=code)
        raw = _dump(value)
        if len(raw) > limit:
            _fail("JSON payload exceeds the bound", "limit_exceeded")
        # Independent data prevents later caller mutation of input history/schema.
        return json.loads(raw)
    except DevinProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise DevinProtocolError("invalid JSON payload", code) from exc


def _text_blocks(value: Any, where: str) -> str:
    if isinstance(value, str):
        return _string(value, where)
    if not isinstance(value, list) or len(value) > MAX_BLOCKS:
        _fail(f"invalid {where}")
    result = []
    for block in value:
        _keys(block, {"type", "text"}, {"type", "text"}, where)
        if block["type"] != "text":
            _fail(f"only text is supported in {where}", "unsupported_feature")
        result.append(_string(block["text"], where))
    return "".join(result)


def prepare_request(raw: JSONInput, *, model_map: Mapping[str, str]) -> PreparedRequest:
    """Validate the complete request before any upstream send; never choose a fallback."""
    source = _load(raw, MAX_REQUEST_BYTES, "invalid_request")
    _keys(source, {"model", "max_tokens", "messages", "system", "stream", "tools",
                   "tool_choice", "temperature", "top_p"},
          {"model", "max_tokens", "messages"}, "Messages request")
    requested = _string(source["model"], "model", pattern=_MODEL)
    if not isinstance(model_map, Mapping) or requested not in model_map:
        _fail("model has no verified Devin mapping", "unmapped_model")
    actual = _string(model_map[requested], "verified model UID", pattern=_MODEL)
    body: dict[str, Any] = {
        "model": actual,
        "max_tokens": _integer(source["max_tokens"], 1, MAX_OUTPUT_TOKENS, "max_tokens"),
    }
    stream = source.get("stream", False)
    if type(stream) is not bool:
        _fail("stream must be a boolean")
    body["stream"] = stream
    if stream:
        body["stream_options"] = {"include_usage": True}
    for field in ("temperature", "top_p"):
        if field in source:
            body[field] = _number(source[field], 0, 1, field)
    names: set[str] = set()
    if "tools" in source:
        tools = source["tools"]
        if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
            _fail("invalid tools")
        converted = []
        for tool in tools:
            _keys(tool, {"name", "description", "input_schema"},
                  {"name", "input_schema"}, "client tool")
            name = _string(tool["name"], "tool name", pattern=_NAME)
            if name in names:
                _fail("duplicate tool name")
            names.add(name)
            schema = tool["input_schema"]
            if not isinstance(schema, dict) or schema.get("type") != "object":
                _fail("tool input_schema must be an object schema")
            function = {"name": name, "parameters": schema}
            if "description" in tool:
                function["description"] = _string(tool["description"], "tool description")
            converted.append({"type": "function", "function": function})
        if converted:
            body["tools"] = converted
    if "tool_choice" in source:
        choice = _keys(source["tool_choice"], {"type", "name", "disable_parallel_tool_use"},
                       {"type"}, "tool_choice")
        kind = choice["type"]
        if kind not in ("auto", "any", "tool", "none"):
            _fail("unsupported tool_choice", "unsupported_feature")
        if kind != "none" and not names:
            _fail("tool_choice requires tools")
        if kind == "tool":
            name = _string(choice.get("name"), "selected tool name", pattern=_NAME)
            if name not in names:
                _fail("selected tool is not defined")
            body["tool_choice"] = {"type": "function", "function": {"name": name}}
        else:
            if "name" in choice:
                _fail("tool_choice name requires type tool")
            body["tool_choice"] = {"any": "required"}.get(kind, kind)
        if "disable_parallel_tool_use" in choice:
            if type(choice["disable_parallel_tool_use"]) is not bool:
                _fail("disable_parallel_tool_use must be a boolean")
            body["parallel_tool_calls"] = not choice["disable_parallel_tool_use"]
    messages = source["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        _fail("invalid messages")
    output: list[dict[str, Any]] = []
    if "system" in source:
        output.append({"role": "system", "content": _text_blocks(source["system"], "system")})
    seen: set[str] = set()
    pending: set[str] = set()
    for message in messages:
        _keys(message, {"role", "content"}, {"role", "content"}, "message")
        role = message["role"]
        if role not in ("user", "assistant"):
            _fail("unsupported message role", "unsupported_feature")
        content = message["content"]
        if isinstance(content, str):
            if pending:
                _fail("tool results must immediately follow their calls")
            output.append({"role": role, "content": _string(content, "message content")})
            continue
        if not isinstance(content, list) or len(content) > MAX_BLOCKS:
            _fail("invalid message content")
        text, calls = [], []
        for block in content:
            if not isinstance(block, dict):
                _fail("invalid content block")
            kind = block.get("type")
            if kind == "text":
                _keys(block, {"type", "text"}, {"type", "text"}, "text block")
                if pending or calls:
                    _fail("text interleaved with pending tool calls is unsupported",
                          "unsupported_feature")
                text.append(_string(block["text"], "text block"))
            elif kind == "tool_use" and role == "assistant":
                _keys(block, {"type", "id", "name", "input"},
                      {"type", "id", "name", "input"}, "tool_use")
                if pending:
                    _fail("previous tool calls have no results")
                tool_id = _string(block["id"], "tool ID", pattern=_ID)
                name = _string(block["name"], "tool name", pattern=_NAME)
                if tool_id in seen or name not in names or not isinstance(block["input"], dict):
                    _fail("invalid or duplicate tool_use")
                arguments = _dump(block["input"])
                if len(arguments) > MAX_TOOL_ARGUMENT_BYTES:
                    _fail("tool input exceeds the bound", "limit_exceeded")
                seen.add(tool_id)
                calls.append({"id": tool_id, "type": "function", "function": {
                    "name": name, "arguments": arguments.decode("utf-8")}})
            elif kind == "tool_result" and role == "user":
                _keys(block, {"type", "tool_use_id", "content", "is_error"},
                      {"type", "tool_use_id", "content"}, "tool_result")
                if text:
                    _fail("tool results must precede user text")
                tool_id = _string(block["tool_use_id"], "tool result ID", pattern=_ID)
                if tool_id not in pending:
                    _fail("tool result has no matching pending call")
                if "is_error" in block and type(block["is_error"]) is not bool:
                    _fail("is_error must be a boolean")
                if block.get("is_error", False):
                    _fail("error tool results are not supported by this pilot",
                          "unsupported_feature")
                output.append({"role": "tool", "tool_call_id": tool_id,
                               "content": _text_blocks(block["content"], "tool result")})
                pending.remove(tool_id)
            else:
                _fail("unsupported content block", "unsupported_feature")
        if role == "assistant":
            if pending:
                _fail("previous tool calls have no results")
            item: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
            if calls:
                item["tool_calls"] = calls
                pending = {call["id"] for call in calls}
            output.append(item)
        elif text or not content:
            if pending:
                _fail("missing tool results")
            output.append({"role": "user", "content": "".join(text)})
    if pending:
        _fail("missing tool results")
    body["messages"] = output
    if len(_dump(body)) > MAX_REQUEST_BYTES:
        _fail("converted request exceeds the bound", "limit_exceeded")
    return PreparedRequest(requested, actual, body, frozenset(names), frozenset(seen))


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        _fail("upstream usage counts are required", "invalid_upstream")
    prompt = _integer(value.get("prompt_tokens"), 0, MAX_TOKEN_COUNT,
                      "upstream prompt_tokens", "invalid_upstream")
    completion = _integer(value.get("completion_tokens"), 0, MAX_TOKEN_COUNT,
                          "upstream completion_tokens", "invalid_upstream")
    if "total_tokens" in value:
        total = _integer(value["total_tokens"], 0, MAX_TOKEN_COUNT,
                         "upstream total_tokens", "invalid_upstream")
        if total != prompt + completion:
            _fail("inconsistent upstream usage totals", "invalid_upstream")
    result = {"input_tokens": prompt, "output_tokens": completion}
    details = value.get("prompt_tokens_details")
    if details is not None:
        if not isinstance(details, dict):
            _fail("invalid upstream usage details", "invalid_upstream")
        if "cached_tokens" in details:
            cached = _integer(details["cached_tokens"], 0, prompt,
                              "upstream cached_tokens", "invalid_upstream")
            result["input_tokens"] = prompt - cached
            result["cache_read_input_tokens"] = cached
    return result


def _identity(value: dict[str, Any], request: PreparedRequest, object_type: str) -> str:
    if "error" in value:
        _fail("upstream returned an error", "upstream_error")
    message_id = _string(value.get("id"), "upstream message ID", pattern=_ID,
                         code="invalid_upstream")
    if value.get("model") != request.actual_model:
        _fail("upstream model differs from the verified model UID", "model_mismatch")
    if value.get("object") != object_type:
        _fail("invalid upstream response object type", "invalid_upstream")
    if "created" in value:
        _integer(value["created"], 0, 1_000_000_000_000, "upstream created", "invalid_upstream")
    return message_id


def _arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        _fail("upstream tool arguments must be a JSON string", "invalid_upstream")
    return _load(value.encode("utf-8"), MAX_TOOL_ARGUMENT_BYTES, "invalid_upstream")


def _stop(value: Any, has_tools: bool) -> str:
    if value == "tool_calls" and has_tools:
        return "tool_use"
    if value == "stop" and not has_tools:
        return "end_turn"
    if value == "length" and not has_tools:
        return "max_tokens"
    _fail("unsupported or inconsistent upstream finish_reason", "invalid_upstream")
    return ""  # unreachable


def _tool_policy(names: list[str], request: PreparedRequest) -> None:
    """Do not turn an upstream policy violation into executable caller-side tools."""
    choice = request.body.get("tool_choice", "auto")
    if choice == "none" and names:
        _fail("upstream called a tool when tool_choice was none", "invalid_upstream")
    if choice == "required" and not names:
        _fail("upstream omitted a required tool call", "invalid_upstream")
    if isinstance(choice, dict):
        selected = choice["function"]["name"]
        if not names or any(name != selected for name in names):
            _fail("upstream did not follow the selected tool choice", "invalid_upstream")
    if request.body.get("parallel_tool_calls") is False and len(names) > 1:
        _fail("upstream returned forbidden parallel tool calls", "invalid_upstream")


def _message(message_id: str, request: PreparedRequest, content: list[dict[str, Any]],
             stop: Optional[str], usage: dict[str, int]) -> dict[str, Any]:
    return {"id": message_id, "type": "message", "role": "assistant", "content": content,
            "model": request.actual_model, "stop_reason": stop, "stop_sequence": None,
            "usage": usage}


def translate_response(raw: JSONInput, request: PreparedRequest) -> dict[str, Any]:
    """Translate one completed response, requiring actual model identity and token counts."""
    source = _load(raw, MAX_RESPONSE_BYTES, "invalid_upstream")
    message_id = _identity(source, request, "chat.completion")
    _keys(source, {"id", "object", "created", "model", "choices", "usage",
                   "system_fingerprint", "service_tier"}, {"choices", "usage"},
          "Chat Completions response", "invalid_upstream")
    choices = source["choices"]
    if not isinstance(choices, list) or len(choices) != 1:
        _fail("exactly one upstream choice is required", "invalid_upstream")
    choice = _keys(choices[0], {"index", "message", "finish_reason", "logprobs"},
                   {"index", "message", "finish_reason"}, "upstream choice", "invalid_upstream")
    if type(choice["index"]) is not int or choice["index"] != 0 or choice.get("logprobs"):
        _fail("invalid upstream choice", "invalid_upstream")
    message = _keys(choice["message"], {"role", "content", "tool_calls", "refusal"},
                    {"role"}, "upstream assistant message", "invalid_upstream")
    if message["role"] != "assistant" or message.get("refusal") is not None:
        _fail("unsupported upstream assistant message", "invalid_upstream")
    blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if text is not None:
        text = _string(text, "upstream text", code="invalid_upstream")
        if text:
            blocks.append({"type": "text", "text": text})
    calls = message.get("tool_calls", [])
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > MAX_TOOLS:
        _fail("invalid upstream tool calls", "invalid_upstream")
    seen = set(request.prior_tool_ids)
    for call in calls:
        _keys(call, {"id", "type", "function"}, {"id", "type", "function"},
              "upstream tool call", "invalid_upstream")
        tool_id = _string(call["id"], "upstream tool ID", pattern=_ID, code="invalid_upstream")
        function = _keys(call["function"], {"name", "arguments"}, {"name", "arguments"},
                         "upstream function", "invalid_upstream")
        name = _string(function["name"], "upstream tool name", pattern=_NAME,
                       code="invalid_upstream")
        if call["type"] != "function" or tool_id in seen or name not in request.tool_names:
            _fail("invalid or duplicate upstream tool call", "invalid_upstream")
        seen.add(tool_id)
        blocks.append({"type": "tool_use", "id": tool_id, "name": name,
                       "input": _arguments(function["arguments"])})
    _tool_policy([block["name"] for block in blocks if block["type"] == "tool_use"], request)
    return _message(message_id, request, blocks, _stop(choice["finish_reason"], bool(calls)),
                    _usage(source["usage"]))


def _sse_data(chunks: Iterable[bytes]) -> Iterator[str]:
    """Parse SSE lines across arbitrary UTF-8/CRLF byte boundaries, with bounded buffers."""
    buffer = bytearray()
    data: list[str] = []
    event = ""
    total = event_size = scan_from = 0

    def line(raw: bytes) -> Optional[str]:
        nonlocal event, event_size, data
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise DevinProtocolError("invalid upstream SSE Unicode", "invalid_upstream") from exc
        event_size += len(raw) + 1
        if event_size > MAX_SSE_EVENT_BYTES:
            _fail("upstream SSE event exceeds the bound", "limit_exceeded")
        if not text:
            if event not in ("", "message", "error"):
                _fail("unsupported upstream SSE event", "unsupported_feature")
            result = "\n".join(data) if data else None
            if event == "error":
                _fail("upstream returned an SSE error", "upstream_error")
            data, event, event_size = [], "", 0
            return result
        if text.startswith(":"):
            return None
        field, separator, value = text.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field == "event":
            if event:
                _fail("duplicate upstream SSE event name", "invalid_upstream")
            event = value
        else:
            _fail("unsupported upstream SSE field", "unsupported_feature")
        return None

    for chunk in chunks:
        if not isinstance(chunk, bytes):
            _fail("SSE chunks must be bytes", "invalid_upstream")
        total += len(chunk)
        if len(chunk) > MAX_SSE_CHUNK_BYTES or total > MAX_STREAM_BYTES:
            _fail("upstream SSE exceeds the bound", "limit_exceeded")
        buffer.extend(chunk)
        while buffer:
            endings = [index for index in (buffer.find(b"\n", scan_from),
                                            buffer.find(b"\r", scan_from)) if index >= 0]
            if not endings:
                scan_from = len(buffer)
                break
            ending = min(endings)
            if buffer[ending] == 13 and ending + 1 == len(buffer):
                scan_from = ending
                break
            width = 2 if buffer[ending:ending + 2] == b"\r\n" else 1
            raw = bytes(buffer[:ending])
            del buffer[:ending + width]
            scan_from = 0
            result = line(raw)
            if result is not None:
                yield result
        if len(buffer) + event_size > MAX_SSE_EVENT_BYTES:
            _fail("upstream SSE event exceeds the bound", "limit_exceeded")
    # An EOF is not a substitute for the event's blank-line delimiter.
    if buffer == b"\r":
        result = line(b"")
        buffer.clear()
        if result is not None:
            yield result
    if buffer or data or event:
        _fail("incomplete upstream SSE event", "incomplete_stream")


def _event(kind: str, **fields: Any) -> bytes:
    return (b"event: " + kind.encode("ascii") + b"\ndata: "
            + _dump({"type": kind, **fields}) + b"\n\n")


def translate_sse(chunks: Iterable[bytes], request: PreparedRequest) -> Iterator[bytes]:
    """Assemble one complete upstream SSE response, then emit validated Messages SSE.

    Missing vendor usage, [DONE], malformed arguments, identity changes, and any
    interrupted iterator abort before the first output event. This is buffered
    validation, not live token streaming; callers must set a bounded HTTP timeout.
    """
    message_id: Optional[str] = None
    usage: Optional[dict[str, int]] = None
    finish: Optional[str] = None
    text: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    seen = set(request.prior_tool_ids)
    done = False
    for data in _sse_data(chunks):
        if done:
            _fail("data followed upstream [DONE]", "invalid_upstream")
        if data == "[DONE]":
            done = True
            continue
        source = _load(data.encode("utf-8"), MAX_SSE_EVENT_BYTES, "invalid_upstream")
        current_id = _identity(source, request, "chat.completion.chunk")
        _keys(source, {"id", "object", "created", "model", "choices", "usage",
                       "system_fingerprint", "service_tier"}, {"choices"},
              "Chat Completions chunk", "invalid_upstream")
        if message_id is None:
            message_id = current_id
        elif message_id != current_id:
            _fail("upstream stream changed message ID", "invalid_upstream")
        if source.get("usage") is not None:
            current_usage = _usage(source["usage"])
            if usage is not None and current_usage != usage:
                _fail("upstream stream repeated inconsistent usage", "invalid_upstream")
            usage = current_usage
        choices = source["choices"]
        if not isinstance(choices, list) or len(choices) > 1:
            _fail("invalid upstream streaming choices", "invalid_upstream")
        if not choices:
            if source.get("usage") is None:
                _fail("empty upstream chunk without usage", "invalid_upstream")
            continue
        if finish is not None:
            _fail("upstream content followed finish_reason", "invalid_upstream")
        choice = _keys(choices[0], {"index", "delta", "finish_reason", "logprobs"},
                       {"index", "delta"}, "upstream delta choice", "invalid_upstream")
        if type(choice["index"]) is not int or choice["index"] != 0 or choice.get("logprobs"):
            _fail("invalid upstream streaming choice", "invalid_upstream")
        delta = _keys(choice["delta"], {"role", "content", "tool_calls", "refusal"},
                      set(), "upstream delta", "invalid_upstream")
        if delta.get("role", "assistant") != "assistant" or delta.get("refusal") is not None:
            _fail("unsupported upstream streaming role or refusal", "invalid_upstream")
        if delta.get("content") is not None:
            fragment = _string(delta["content"], "upstream text delta", code="invalid_upstream")
            if fragment and tools:
                _fail("text after tool calls is unsupported", "unsupported_feature")
            if fragment:
                text.append(fragment)
        calls = delta.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > MAX_TOOLS:
                _fail("invalid upstream tool deltas", "invalid_upstream")
            in_chunk: set[int] = set()
            for call in calls:
                _keys(call, {"index", "id", "type", "function"}, {"index", "function"},
                      "upstream tool delta", "invalid_upstream")
                index = _integer(call["index"], 0, MAX_TOOLS - 1, "tool index", "invalid_upstream")
                if index in in_chunk:
                    _fail("duplicate upstream tool index in one chunk", "invalid_upstream")
                in_chunk.add(index)
                function = _keys(call["function"], {"name", "arguments"}, set(),
                                 "upstream function delta", "invalid_upstream")
                if index not in tools:
                    tool_id = _string(call.get("id"), "upstream tool ID", pattern=_ID,
                                      code="invalid_upstream")
                    name = _string(function.get("name"), "upstream tool name", pattern=_NAME,
                                   code="invalid_upstream")
                    if (call.get("type") != "function" or tool_id in seen
                            or name not in request.tool_names):
                        _fail("invalid or duplicate upstream streaming tool", "invalid_upstream")
                    seen.add(tool_id)
                    tools[index] = {"id": tool_id, "name": name, "fragments": [], "size": 0}
                elif any(field in call for field in ("id", "type")) or "name" in function:
                    _fail("upstream tool metadata was redefined", "invalid_upstream")
                if "arguments" in function:
                    fragment = _string(function["arguments"], "upstream argument delta",
                                       code="invalid_upstream")
                    tool = tools[index]
                    tool["size"] += len(fragment.encode("utf-8"))
                    if tool["size"] > MAX_TOOL_ARGUMENT_BYTES:
                        _fail("upstream tool arguments exceed the bound", "limit_exceeded")
                    tool["fragments"].append(fragment)
        if choice.get("finish_reason") is not None:
            finish = _stop(choice["finish_reason"], bool(tools))
    if not done or message_id is None or finish is None:
        _fail("upstream SSE ended before completion", "incomplete_stream")
    if usage is None:
        _fail("upstream usage counts are required", "invalid_upstream")
    if set(tools) != set(range(len(tools))):
        _fail("upstream tool indices are not contiguous", "invalid_upstream")
    for tool in tools.values():
        _arguments("".join(tool["fragments"]))
    _tool_policy([tool["name"] for tool in tools.values()], request)
    yield _event("message_start", message=_message(message_id, request, [], None, usage))
    index = 0
    if text:
        yield _event("content_block_start", index=index, content_block={"type": "text", "text": ""})
        for fragment in text:
            yield _event("content_block_delta", index=index,
                         delta={"type": "text_delta", "text": fragment})
        yield _event("content_block_stop", index=index)
        index += 1
    for tool_index in sorted(tools):
        tool = tools[tool_index]
        yield _event("content_block_start", index=index, content_block={
            "type": "tool_use", "id": tool["id"], "name": tool["name"], "input": {}})
        for fragment in tool["fragments"]:
            yield _event("content_block_delta", index=index,
                         delta={"type": "input_json_delta", "partial_json": fragment})
        yield _event("content_block_stop", index=index)
        index += 1
    yield _event("message_delta", delta={"stop_reason": finish, "stop_sequence": None}, usage=usage)
    yield _event("message_stop")
