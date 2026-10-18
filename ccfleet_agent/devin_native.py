"""Disabled-by-default single-step native Devin adapter for an isolated node worker.

One sanitized Messages body becomes explicitly labeled native-agent context.
This is not raw Messages/model-API parity. A genuine MCP invocation is returned
to the caller only after native/shim cleanup; tools never run on this node.
Continuations use a fresh process/session and include the caller's tool history.

The authenticated caller must supply a root-owned policy and revalidate the
owner/key/provider/rental generations through authorize() before each step.
No account identity is inferred from a token or login. Native Devin owns login.
run_step requires a dedicated single-threaded Linux worker with no existing
children: subreaper cleanup must not adopt another application's descendants.
The CLI prints a plan unless both --execute and an enabled policy are supplied.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import signal
import sqlite3
import stat
import sys
import threading
import time
import types
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

if __package__:
    from . import devin_acp as acp
    from . import devin_mcp as mcp
    from . import devin_tools as tools
    from . import egress
    from . import local_relay as relay
else:
    _package = types.ModuleType("ccfleet_native_pinned")
    _package.__path__ = []  # Every dependency below is compiled from its exact sibling source.
    sys.modules[_package.__name__] = _package

    def _sibling(name):
        spec = importlib.util.spec_from_file_location(
            _package.__name__ + "." + name, Path(__file__).resolve().with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        setattr(_package, name, module)
        exec(compile(Path(spec.origin).read_bytes(), spec.origin, "exec"), module.__dict__)
        return module

    _sibling("inference_policy")
    egress = _sibling("egress")
    acp, tools, mcp, relay = (_sibling(name) for name in (
        "devin_acp", "devin_tools", "devin_mcp", "local_relay"))

MAX_BODY = 2 * 1024 * 1024
MAX_PROMPT = 256 * 1024
MAX_TEXT = 1024 * 1024
MCP_PARTIAL_GRACE_S = 0.5
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
_DISABLED = frozenset({"read", "edit", "write", "grep", "glob", "exec", "webfetch", "web_search"})
_IMPORTS = frozenset({"agents_standard", "cursor", "windsurf", "claude", "copilot",
                      "opencode", "zed"})
_DENIED = frozenset({"read", "edit", "grep", "glob", "exec", "Read(/**)", "Write(/**)",
                     "Fetch(http://*/*)", "Fetch(https://*/*)"})
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_SAFE_ACP = frozenset({"busy", "closed", "invalid_config", "invalid_state", "launch_failed",
    "limit_exceeded", "model_requests_disabled", "process_exited", "protocol_error", "rpc_error",
    "timeout", "unsupported_method", "version_mismatch"})


class NativeError(ValueError):
    """Static error codes only; provider text and filesystem details never escape."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OwnershipBinding:
    owner_id: str
    key_id: str
    key_generation: int
    slot_id: str
    claim_id: str
    claim_generation: int
    provider_generation: int
    provider: str = "devin"

    def __post_init__(self):
        if (self.provider != "devin"
                or any(not isinstance(value, str) or not _ID.fullmatch(value)
                       for value in (self.owner_id, self.key_id, self.slot_id))
                or not isinstance(self.claim_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", self.claim_id)
                or any(type(value) is not int or not 1 <= value <= 2**31 - 1
                       for value in (self.key_generation, self.claim_generation,
                                     self.provider_generation))):
            raise NativeError("invalid_ownership_binding")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise NativeError("invalid_ownership_binding")
        return cls(**value)

    @property
    def digest(self):
        return hashlib.sha256(_encode(asdict(self))).hexdigest()


def _encode(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode()
        mcp._decode(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise NativeError("invalid_bounded_json") from None
    if len(raw) > MAX_BODY:
        raise NativeError("body_limit")
    return raw


def _cache(block):
    if "cache_control" not in block:
        return
    value = block["cache_control"]
    if (not isinstance(value, dict) or value.keys() - {"type", "ttl"}
            or value.get("type") != "ephemeral"
            or "ttl" in value and value["ttl"] not in ("5m", "1h")):
        raise NativeError("unsupported_cache_metadata")


def _effort(value, expected):
    if not isinstance(value, dict) or set(value) != {"effort"} or value["effort"] != expected:
        raise NativeError("unsupported_output_control")


def _text_blocks(value):
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        raise NativeError("unsupported_content")
    for block in value:
        if (not isinstance(block, dict) or block.keys() - {"type", "text", "cache_control"}
                or block.get("type") != "text" or not isinstance(block.get("text"), str)):
            raise NativeError("unsupported_content")
        _cache(block)


def _declared_tools(body):
    declarations = body.get("tools")
    if not isinstance(declarations, list) or not declarations:
        raise NativeError("unsupported_empty_tool_catalog")
    frozen = []
    for declaration in declarations:
        if (not isinstance(declaration, dict)
                or declaration.keys() - {"name", "description", "input_schema", "cache_control"}
                or not {"name", "description", "input_schema"} <= declaration.keys()):
            raise NativeError("unsupported_tool_declaration")
        _cache(declaration)
        frozen.append({key: declaration[key] for key in ("name", "description", "input_schema")})
    try:
        tools.ToolCatalog(frozen)
    except tools.ToolBridgeError:
        raise NativeError("invalid_tool_catalog") from None
    return frozen


def validate_native_request(messages_body, *, request_models, effort="max"):
    """native-agent-v1 preflight; baseline controls remain labeled data, not API promises.

    Structured identity uses the established privacy filter. Visible text,
    declarations, history and cache/control metadata are preserved exactly.
    Native model/effort remain root-policy controlled. Rich/server-side content
    and nonbaseline generation controls fail before any native process starts.
    """
    if (not isinstance(request_models, (list, tuple, set, frozenset)) or not request_models
            or len(request_models) > 256
            or any(not isinstance(model, str) or not _MODEL.fullmatch(model)
                   for model in request_models)):
        raise NativeError("invalid_request_model_policy")
    try:
        body = mcp._decode(relay.sanitize_body(_encode(messages_body)))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeError("invalid_messages_body") from None
    allowed = {"model", "messages", "tools", "system", "max_tokens", "stream", "thinking",
               "context_management", "output_config", "tool_choice"}
    if (not isinstance(body, dict) or body.keys() - allowed
            or not isinstance(body.get("model"), str) or body["model"] not in request_models
            or not isinstance(body.get("messages"), list) or not body["messages"]):
        raise NativeError("unsupported_native_request")
    if ("max_tokens" in body and (type(body["max_tokens"]) is not int
                                  or not 1 <= body["max_tokens"] <= 1000000)
            or "stream" in body and type(body["stream"]) is not bool
            or "tool_choice" in body and body["tool_choice"] != {"type": "auto"}):
        raise NativeError("unsupported_generation_control")
    if "thinking" in body and body["thinking"] not in (
            {"type": "adaptive"}, {"type": "adaptive", "display": "omitted"}):
        raise NativeError("unsupported_thinking_control")
    if "context_management" in body and body["context_management"] != {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}:
        raise NativeError("unsupported_context_control")
    if "output_config" in body:
        _effort(body["output_config"], effort)
    if "system" in body:
        _text_blocks(body["system"])
    _declared_tools(body)
    for message in body["messages"]:
        if (not isinstance(message, dict)
                or message.keys() - {"role", "content", "output_config", "cache_control"}
                or message.get("role") not in {"user", "assistant", "system"}
                or "content" not in message):
            raise NativeError("unsupported_message")
        _cache(message)
        if "output_config" in message:
            _effort(message["output_config"], effort)
        content = message["content"]
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise NativeError("unsupported_content")
        for block in content:
            if not isinstance(block, dict):
                raise NativeError("unsupported_content")
            kind = block.get("type")
            if kind == "text":
                _text_blocks([block])
            elif kind == "tool_use":
                if (message["role"] != "assistant"
                        or set(block) - {"type", "id", "name", "input", "cache_control"}
                        or not isinstance(block.get("id"), str) or not mcp.ID.fullmatch(block["id"])
                        or not isinstance(block.get("name"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", block["name"])
                        or not isinstance(block.get("input"), dict)):
                    raise NativeError("unsupported_tool_history")
                _cache(block)
            elif kind == "tool_result":
                if (message["role"] != "user"
                        or set(block) - {"type", "tool_use_id", "content", "is_error",
                                         "cache_control"}
                        or not isinstance(block.get("tool_use_id"), str)
                        or not mcp.ID.fullmatch(block["tool_use_id"])
                        or "content" not in block
                        or "is_error" in block and type(block["is_error"]) is not bool):
                    raise NativeError("unsupported_tool_result")
                _cache(block)
                _text_blocks(block["content"])
            else:
                raise NativeError("unsupported_content")
    return body


def _read(path, *, uid, maximum=MAX_BODY, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                or info.st_mode & (0o077 if private else 0o022)
                or not 0 < info.st_size <= maximum):
            raise NativeError("file_guard")
        result = bytearray()
        while len(result) <= maximum:
            part = os.read(fd, min(65536, maximum + 1 - len(result)))
            if not part:
                break
            result.extend(part)
        if not result or len(result) > maximum:
            raise NativeError("file_limit")
        return bytes(result)
    finally:
        os.close(fd)


def _plain(path):
    value = Path(path)
    if (not value.is_absolute() or ".." in value.parts
            or value.resolve(strict=True) != value):
        raise NativeError("path_guard")
    return value


def _private_directory(path):
    value = _plain(path)
    info = value.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise NativeError("private_directory_guard")
    return value


def _root_ancestry(path):
    # Linux O_PATH needs search permission, not directory-list permission:
    # root-owned 0711 policy parents are intentionally traverse-only to slots.
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for part in (None, *path.parent.parts[1:]):
            if part is not None:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
                raise NativeError("source_directory_guard")
    finally:
        os.close(fd)


def _pinned(record, *, expected=None, uid=0, private=False):
    if (not isinstance(record, dict) or set(record) != {"path", "sha256"}
            or not isinstance(record["path"], str) or not isinstance(record["sha256"], str)
            or not _DIGEST.fullmatch(record["sha256"])):
        raise NativeError("source_pin_guard")
    path = _plain(record["path"])
    if expected is not None and path != expected:
        raise NativeError("source_path_guard")
    if uid == 0:
        _root_ancestry(path)
    raw = _read(path, uid=uid, maximum=256 * 1024 * 1024, private=private)
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise NativeError("source_digest_guard")
    return path


@dataclass(frozen=True)
class NativeConfig:
    policy_path: Path
    policy_sha256: str
    _json: str

    @classmethod
    def load(cls, path, *, expected_sha256=None):
        path = _plain(path)
        _root_ancestry(path)
        raw = _read(path, uid=0, maximum=64 * 1024)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise NativeError("policy_digest_guard")
        value = mcp._decode(raw)
        config = cls(path, digest, _encode(value).decode())
        config.validate()
        return config

    @property
    def data(self):
        return json.loads(self._json)

    def status(self, binding):
        """Model-free eligibility; never assert that login or the live MCP peer is ready."""
        self.validate()
        matched = (isinstance(binding, OwnershipBinding)
                   and binding == OwnershipBinding.from_dict(self.data["binding"]))
        return {"contract": "native-agent-v1", "configured": matched,
                "enabled": matched and self.data["enabled"] is True,
                "mcp_ready": False, "model_requests": 0,
                "model_uid": self.data["model_uid"] if matched else None,
                "raw_Messages_semantic_parity": False, "raw_request_features": False}

    def validate(self):
        raw = _read(self.policy_path, uid=0, maximum=64 * 1024)
        if (hashlib.sha256(raw).hexdigest() != self.policy_sha256
                or _encode(mcp._decode(raw)).decode() != self._json):
            raise NativeError("policy_changed")
        value = self.data
        fields = {"version", "enabled", "execution_uid", "binding", "model_uid", "verified_models",
                  "model_state_values", "request_models", "thought_level", "state_dir", "home",
                  "files", "timeout_s"}
        if (not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int
                or value["version"] != 1 or type(value["enabled"]) is not bool
                or type(value["execution_uid"]) is not int or value["execution_uid"] <= 0
                or value["execution_uid"] != os.geteuid()
                or type(value["timeout_s"]) not in (int, float)
                or not 1 <= value["timeout_s"] <= 60):
            raise NativeError("invalid_native_policy")
        OwnershipBinding.from_dict(value["binding"])
        for field in ("verified_models", "model_state_values", "request_models"):
            options = value[field]
            if (not isinstance(options, list) or not 1 <= len(options) <= 256
                    or any(not isinstance(item, str) or not _MODEL.fullmatch(item)
                           for item in options)):
                raise NativeError("invalid_verified_model")
        if value["model_uid"] not in value["verified_models"]:
            raise NativeError("invalid_verified_model")
        if (value["thought_level"] is not None
                and (not isinstance(value["thought_level"], str)
                     or value["thought_level"] not in {"low", "medium", "high", "xhigh", "max"})):
            raise NativeError("invalid_model_state")
        home, state = _private_directory(value["home"]), _private_directory(value["state_dir"])
        expected = {"adapter": Path(__file__).resolve(), "acp": Path(acp.__file__).resolve(),
                    "mcp": Path(mcp.__file__).resolve(), "tools": Path(tools.__file__).resolve(),
                    "relay": Path(relay.__file__).resolve(),
                    "inference_policy": Path(relay.inference_policy.__file__).resolve(),
                    "egress": Path(egress.__file__).resolve(),
                    "package_init": Path(__file__).resolve().with_name("__init__.py"),
                    "native_config": home / ".config/devin/config.json",
                    "native_mcp_config": home / ".config/devin/mcp_config.json"}
        files = value["files"]
        if not isinstance(files, dict) or set(files) != {*expected, "executable", "python"}:
            raise NativeError("invalid_source_manifest")
        for name, record in files.items():
            private = name in {"native_config", "native_mcp_config"}
            _pinned(record, expected=expected.get(name),
                    uid=os.geteuid() if private else 0, private=private)
        for name in ("executable", "python"):
            if not os.access(files[name]["path"], os.X_OK):
                raise NativeError("executable_guard")
        settings = mcp._decode(_read(expected["native_config"], uid=os.geteuid(), private=True))
        disabled = settings.get("disabled_tools") if isinstance(settings, dict) else None
        imports = settings.get("read_config_from") if isinstance(settings, dict) else None
        permissions = settings.get("permissions") if isinstance(settings, dict) else None
        allow = permissions.get("allow") if isinstance(permissions, dict) else None
        deny = permissions.get("deny") if isinstance(permissions, dict) else None
        agent = settings.get("agent") if isinstance(settings, dict) else None
        command = shlex.join([files["python"]["path"], "-I", files["mcp"]["path"], "--hook"])
        hooks = {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command",
                                                           "command": command, "timeout": 5}]}]}
        if (not isinstance(settings, dict) or settings.get("auto_update") is not False
                or settings.get("subagents_enabled") is not False
                or not isinstance(disabled, list) or any(not isinstance(n, str) for n in disabled)
                or not _DISABLED <= set(disabled)
                or not isinstance(imports, dict)
                or any(imports.get(name) is not False for name in _IMPORTS)
                or not isinstance(allow, list) or any(
                    not isinstance(name, str)
                    or (name != "mcp__ccfleet__*"
                        and not re.fullmatch(r"mcp__ccfleet__[A-Za-z0-9_-]{1,64}", name))
                    for name in allow)
                or not isinstance(deny, list) or any(not isinstance(name, str) for name in deny)
                or not _DENIED <= set(deny)
                or not isinstance(agent, dict) or agent.get("model") != value["model_uid"]
                or settings.get("hooks") != hooks):
            raise NativeError("native_containment_guard")
        if mcp._decode(_read(expected["native_mcp_config"], uid=os.geteuid(), private=True)) != {
                "mcpServers": {}}:
            raise NativeError("native_mcp_config_guard")
        return home, state


def _authorized(config, binding, authorize, cancelled):
    if cancelled.is_set():
        raise NativeError("cancelled")
    try:
        config.validate()
    except NativeError:
        raise
    except Exception:
        raise NativeError("policy_guard_failed") from None
    if (config.data["enabled"] is not True
            or OwnershipBinding.from_dict(config.data["binding"]) != binding):
        raise NativeError("native_requests_disabled_or_binding_changed")
    try:
        permitted = authorize(binding)
    except Exception:
        permitted = False
    if permitted is not True:
        raise NativeError("authorization_revoked")


def _selected_model(session, notifications, config):
    value = config.data
    if session.get("models", {}).get("currentModelId") == value["model_uid"]:
        return True
    options = list(session.get("configOptions", []))
    for notice in notifications:
        update = notice.get("update", {})
        if update.get("sessionUpdate") == "config_option_update":
            options.extend(update.get("configOptions", []))
    return (any(isinstance(option, dict) and option.get("category") == "model"
                and option.get("currentValue") in value["model_state_values"] for option in options)
            and (value["thought_level"] is None or any(isinstance(option, dict)
                and option.get("category") == "thought_level"
                and option.get("currentValue") == value["thought_level"] for option in options)))


class _Controller(acp.ACPController):
    def _handle(self, value, deadline):
        previous = len(self._notifications)
        super()._handle(value, deadline)
        if value.get("method") != "session/update" or "id" in value:
            return
        params = value["params"]
        update = params["update"]
        if update.get("sessionUpdate") == "agent_thought_chunk":
            content = update.get("content")
            if (params.keys() - {"sessionId", "update", "_meta"}
                    or update.keys() - {"sessionUpdate", "content", "_meta"}
                    or not isinstance(content, dict) or set(content) != {"type", "text"}
                    or content.get("type") != "text" or not isinstance(content.get("text"), str)
                    or params["sessionId"] not in self._sessions
                    or len(self._notifications) != previous + 1
                    or self._notifications[-1] is not params):
                raise acp.ACPError("unsupported bound thought observation")
            self._notifications.pop()


def _identity(process, native_pid):
    if (not isinstance(process, dict) or set(process) != {"pid", "pgid", "startticks"}
            or type(process["pid"]) is not int or process["pid"] <= 1
            or type(process["pgid"]) is not int or process["pgid"] != native_pid
            or type(process["startticks"]) is not int or process["startticks"] <= 0):
        raise NativeError("mcp_process_guard")
    path = Path("/proc") / str(process["pid"])
    ticks = int((path / "stat").read_text().rsplit(")", 1)[1].split()[19])
    if (path.stat().st_uid != os.geteuid() or ticks != process["startticks"]
            or os.getpgid(process["pid"]) != native_pid):
        raise NativeError("mcp_process_changed")


def _lifecycle(run):
    return mcp.read_lifecycle(run)


def _wait_ready(run, native_pid, cancelled, deadline):
    while time.monotonic() < deadline:
        if cancelled.is_set():
            raise NativeError("cancelled")
        try:
            process = mcp._decode(_read(run / "process.json", uid=os.geteuid(), private=True))
            _identity(process, native_pid)
            summary = _lifecycle(run)
            if summary["ready"] is True:
                return process, summary
        except FileNotFoundError:
            pass
        time.sleep(0.025)
    raise NativeError("mcp_handshake_timeout")


def _isolated_subreaper():
    if (not sys.platform.startswith("linux")
            or threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
            or Path(f"/proc/self/task/{os.getpid()}/children").read_text().strip()):
        raise NativeError("dedicated_linux_worker_required")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise NativeError("subreaper_unavailable")


def _cleanup_group(native_pid, timeout_s=3):
    """Reap the dedicated worker's owned child tree, including escaped-group descendants.

    _isolated_subreaper must precede native launch. The worker had no children
    or other threads; authorize callbacks must not spawn processes. Only an
    unreaped direct child is ever signalled, so its PID cannot have been reused.
    """
    if native_pid is None:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        owned = [int(text) for text in Path(
            f"/proc/self/task/{os.getpid()}/children").read_text().split()]
        if not owned:
            return True
        for pid in owned[:128]:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited == 0:
                    os.kill(pid, signal.SIGKILL)
            except (ChildProcessError, ProcessLookupError):
                pass
        time.sleep(0.025)
    return False


def _final_text(notices, session_id, answer):
    if answer.get("stopReason") != "end_turn":
        raise NativeError("native_completion_not_end_turn")
    pieces, size = [], 0
    for notice in notices:
        if notice.get("sessionId") != session_id:
            raise NativeError("native_session_changed")
        update = notice.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if (not isinstance(content, dict) or set(content) != {"type", "text"}
                    or content.get("type") != "text" or not isinstance(content.get("text"), str)):
                raise NativeError("unsupported_native_content")
            size += len(content["text"].encode())
            if size > MAX_TEXT:
                raise NativeError("native_text_limit")
            pieces.append(content["text"])
        elif kind not in {"usage_update", "config_option_update", "current_mode_update",
                          "available_commands_update", "session_info_update", "user_message_chunk",
                          "plan"}:
            raise NativeError("unsupported_native_update")
    if not pieces:
        raise NativeError("empty_native_completion")
    return [{"type": "text", "text": "".join(pieces)}]


def _accounting(home, session_id, model):
    result = {"usage": None, "unique_finalized_provider_ids": None,
              "actual_provider_requests": None,
              "provider_count_scope": "final_DB_records_not_billing",
              "served_model_verified": False, "model_mismatch_detected": False}
    try:
        # Native Devin may create a 0644 DB inside its private 0700 HOME.
        # Relax only this readonly observation, never config/credential files.
        home = _private_directory(home)
        path = home / ".local/share/devin/cli/sessions.db"
        _plain(path)
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            return result
        deadline = time.monotonic() + 1
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            rows = db.execute("SELECT chat_message FROM message_nodes WHERE session_id=? LIMIT 513",
                              (session_id,)).fetchall()
        if len(rows) > 512:
            return result
        generations, incomplete = {}, False
        for (raw,) in rows:
            value = mcp._decode(raw.encode() if isinstance(raw, str) else raw)
            metadata = value.get("metadata", {})
            request_id = metadata.get("request_id")
            generation_model = metadata.get("generation_model")
            if value.get("role") == "assistant" and (request_id or generation_model):
                if (isinstance(generation_model, str) and generation_model
                        and generation_model != model):
                    result["model_mismatch_detected"] = True
                    return result
                if (not isinstance(request_id, str) or not request_id
                        or generation_model != model):
                    incomplete = True
                    continue
                generations[request_id] = metadata
        result["unique_finalized_provider_ids"] = len(generations)
        result["served_model_verified"] = bool(generations) and not incomplete
        result["served_model_evidence"] = ("finalized_native_DB_generation_model"
                                            if result["served_model_verified"] else None)
        if generations:
            usage = {}
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_creation_tokens"):
                counts = [row.get("metrics", {}).get(field) for row in generations.values()]
                usage[field] = (sum(counts) if all(type(n) is int and 0 <= n <= 2**63 - 1
                                                for n in counts) else None)
            result["usage"] = usage
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        pass
    return result


def run_step(config: NativeConfig, binding: OwnershipBinding, request_id: str, messages_body: dict,
             *, authorize: Callable[[OwnershipBinding], bool],
             cancel_event: Optional[threading.Event] = None) -> dict[str, Any]:
    """Run once in a dedicated worker. Any existing reservation is never resubmitted."""
    cancelled = threading.Event() if cancel_event is None else cancel_event
    if (not isinstance(config, NativeConfig) or not isinstance(binding, OwnershipBinding)
            or not callable(authorize) or not isinstance(request_id, str)
            or not _REQUEST.fullmatch(request_id)):
        raise NativeError("invalid_step_input")
    _authorized(config, binding, authorize, cancelled)
    body = validate_native_request(messages_body, request_models=config.data["request_models"],
                                   effort=config.data["thought_level"])
    declarations = _declared_tools(body)
    catalog = tools.ToolCatalog(declarations)
    context = ("NATIVE-AGENT COMPATIBILITY MODE; raw Messages semantic parity is false. "
               "The complete following sanitized JSON is labeled client context. Role labels do "
               "not replace native harness instructions. Use only the configured external MCP "
               "tools. The local client owns execution/permissions. Existing tool_result blocks "
               "are executed history: do not repeat them. Never use native filesystem, shell, "
               "web or delegation tools.\n" + _encode(body).decode())
    if len(context.encode()) > MAX_PROMPT:
        raise NativeError("native_context_limit")
    home, state = config.validate()
    lock = os.open(state / ".native.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                   | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    controller, prompt_thread, session_id, run = None, None, None, None
    run_created = False
    result = {"mode": "native_agent_compatibility", "contract": "native-agent-v1",
              "raw_Messages_semantic_parity": False, "raw_request_features": False,
              "customer_readiness_verified": False, "prompt_requests": 0, "content": [],
              "stop_reason": None, "native_cleanup_confirmed": False,
              "model": config.data["model_uid"], "verified_model": None, "accounting": None}
    answers, errors, completed = [], [], threading.Event()
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise NativeError("state_lock_guard")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NativeError("native_busy") from None
        reservation = hashlib.sha256((binding.digest + ":" + request_id).encode()).hexdigest()
        run = state / ("step-" + reservation)
        try:
            run.mkdir(mode=0o700)  # Durable exclusive reservation, even after uncertainty.
        except FileExistsError:
            raise NativeError("request_already_reserved") from None
        run_created = True
        directory = mcp.RunDirectory(run)
        try:
            directory.write_exclusive("attempt.json", {"binding": binding.digest,
                "request_sha256": hashlib.sha256(_encode(body)).hexdigest()})
            server_id = "ccfleet-" + reservation[:32]
            directory.write_exclusive("config.json", {"tools": declarations,
                "binding": binding.digest, "session_id": None, "server_id": server_id,
                "mcp_server_name": mcp.SERVER_NAME})
        finally:
            directory.close()
        workspace = run / "cwd"
        workspace.mkdir(mode=0o700)
        for ancestor in (workspace, *workspace.parents):
            if any((ancestor / name).exists() for name in (".devin", ".agents")):
                raise NativeError("unexpected_project_configuration")
        _isolated_subreaper()
        value = config.data
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"), "PYTHONDONTWRITEBYTECODE": "1",
            mcp.RUN_DIR_ENV: str(run)}
        try:
            environment.update(egress.protected_native_environment())
        except egress.EgressError:
            raise NativeError("native_egress_unavailable") from None
        controller = _Controller(value["files"]["executable"]["path"], model=value["model_uid"],
            cwd=str(workspace), allowed_executables=[value["files"]["executable"]["path"]],
            verified_models=set(value["verified_models"]), allow_model_requests=True,
            env=environment, timeout_s=5, max_notifications=128, max_prompt_bytes=MAX_PROMPT,
            ignored_notifications=("_cognition.ai/mcp/serversChanged", "_cognition.ai/turn_stats",
                                   "_cognition.ai/agent_stopped", "_cognition.ai/output"),
            ignored_notification_prefixes=("_cognition.ai/",))
        controller.initialize()
        session = controller._rpc("session/new", {"cwd": str(workspace), "mcpServers": [{
            "name": mcp.SERVER_NAME, "command": value["files"]["python"]["path"],
            "args": ["-I", value["files"]["mcp"]["path"], "--run-dir", str(run)], "env": []}]})
        session_id = session.get("sessionId")
        if not isinstance(session_id, str) or not mcp.ID.fullmatch(session_id):
            raise NativeError("native_session_guard")
        if not _selected_model(session, controller.drain_notifications(), config):
            raise NativeError("native_model_state_mismatch")
        directory = mcp.RunDirectory(run)
        try:
            bound = {"binding": binding.digest, "session_id": session_id,
                     "server_id": server_id, "schema_digest": catalog.schema_digest}
            directory.write_exclusive("session.json", bound)
        finally:
            directory.close()
        process, summary = _wait_ready(run, controller.pid, cancelled, time.monotonic() + 5)
        result["mcp_lifecycle"] = summary
        _authorized(config, binding, authorize, cancelled)
        controller.timeout_s = value["timeout_s"]

        def prompt():
            try:
                answers.append(controller.prompt(session_id, context))
            except BaseException as error:
                errors.append(error.code if isinstance(error, acp.ACPError)
                              and error.code in _SAFE_ACP else "native_prompt_failed")
            finally:
                completed.set()

        result["prompt_requests"] = 1
        prompt_thread = threading.Thread(target=prompt, daemon=True)
        prompt_thread.start()
        deadline = time.monotonic() + value["timeout_s"]
        call, partial_until = None, None
        while time.monotonic() < deadline:
            if cancelled.is_set() or authorize(binding) is not True:
                raise NativeError("authorization_revoked_or_cancelled")
            _identity(process, controller.pid)
            current = _lifecycle(run)
            if (current.get("phase") == "partial" and current.get("handshake_complete") is True
                    and current.get("protocol_version") == summary.get("protocol_version")):
                if partial_until is None:
                    partial_until = min(deadline, time.monotonic() + MCP_PARTIAL_GRACE_S)
                if time.monotonic() >= partial_until:
                    raise NativeError("mcp_lifecycle_partial_timeout")
                # Already accepted readiness survives only a short in-progress
                # append. Keep checking process, cancellation and authorization;
                # never deliver a tool/final answer until the receipt completes.
                cancelled.wait(0.025)
                continue
            if (current.get("protocol_version") != summary.get("protocol_version")
                    or not current["ready"]):
                raise NativeError("mcp_lifecycle_changed")
            partial_until = None
            if (run / "call.json").exists():
                call = mcp._decode(_read(run / "call.json", uid=os.geteuid(), private=True))
                break
            if completed.wait(0.025):
                latest = _lifecycle(run)
                if (latest.get("phase") == "partial"
                        and latest.get("handshake_complete") is True
                        and latest.get("protocol_version") == summary.get("protocol_version")):
                    partial_until = min(deadline, time.monotonic() + MCP_PARTIAL_GRACE_S)
                    continue
                if (not latest["ready"] or latest.get("protocol_version")
                        != summary.get("protocol_version")):
                    raise NativeError("mcp_lifecycle_changed")
                # The genuine call may be atomically published between the
                # first file check and prompt completion; inspect it once more.
                if (run / "call.json").exists():
                    call = mcp._decode(_read(run / "call.json", uid=os.geteuid(), private=True))
                break
        if partial_until is not None:
            raise NativeError("mcp_lifecycle_partial_timeout")
        if call is not None:
            if (not isinstance(call, dict) or set(call) != {
                    "version", "frontend_tool_use", "native_rpc_id", "context", "process"}
                    or type(call["version"]) is not int or call["version"] != 1
                    or call["context"] != bound or call["process"] != process):
                raise NativeError("mcp_call_binding_changed")
            offered = call["frontend_tool_use"]
            if (not isinstance(offered, dict) or set(offered) != {"type", "id", "name", "input"}
                    or offered["type"] != "tool_use" or not isinstance(offered["id"], str)
                    or not re.fullmatch(r"toolu_devin_mcp_[a-f0-9]{32}", offered["id"])):
                raise NativeError("mcp_tool_identity_guard")
            handoff = tools.ToolHandoff(catalog, binding=binding.digest,
                                        session_id=session_id, server_id=server_id)
            handoff.begin({"jsonrpc": "2.0", "id": call["native_rpc_id"], "method": "tools/call",
                "params": {"name": offered["name"], "arguments": offered["input"]}},
                binding=binding.digest, session_id=session_id, server_id=server_id,
                remaining_seconds=1)
            handoff.cancel(binding=binding.digest, session_id=session_id,
                           server_id=server_id, request_id=call["native_rpc_id"])
            result.update(content=[offered], stop_reason="tool_use")
        else:
            if not completed.is_set() or errors or len(answers) != 1:
                raise NativeError(errors[0] if errors else "native_step_incomplete")
            text = _final_text(controller.drain_notifications(), session_id, answers[0])
            result.update(content=text, stop_reason="end_turn")
    except Exception as error:
        result["error_code"] = (error.code if isinstance(error, NativeError)
                                else "native_step_failed")
    finally:
        close_ok = True
        if controller is not None:
            try:
                controller.close()
            except Exception:
                close_ok = False
        if prompt_thread is not None:
            prompt_thread.join(3)
        try:
            cleaned = _cleanup_group(controller.pid if controller is not None else None)
        except Exception:
            cleaned = False
        if prompt_thread is not None and prompt_thread.is_alive():
            prompt_thread.join(1)
        result["native_cleanup_confirmed"] = bool(cleaned and close_ok
            and (controller is None or controller._process is None
                 or controller._process.returncode is not None)
            and (prompt_thread is None or not prompt_thread.is_alive()))
        if session_id is not None:
            try:
                result["accounting"] = _accounting(home, session_id, config.data["model_uid"])
            except Exception:
                result["accounting"] = {"usage": None, "unique_finalized_provider_ids": None,
                    "actual_provider_requests": None, "served_model_verified": False,
                    "model_mismatch_detected": False}
            if result["accounting"]["model_mismatch_detected"]:
                result["error_code"] = "native_model_mismatch"
            if result["accounting"]["served_model_verified"]:
                result["verified_model"] = config.data["model_uid"]
        if not result["native_cleanup_confirmed"]:
            result["error_code"] = "native_cleanup_unconfirmed"
        os.close(lock)
    try:
        _authorized(config, binding, authorize, cancelled)
    except Exception:
        result["error_code"] = "authorization_revoked_or_cancelled"
    if result.get("error_code"):
        result["content"], result["stop_reason"] = [], None
    if run_created:
        try:
            directory = mcp.RunDirectory(run)
            try:
                directory.write_exclusive("result.json", result)
            finally:
                directory.close()
        except Exception:
            result["content"], result["stop_reason"] = [], None
            result["error_code"] = "result_persistence_failed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"execute": False, "model_requests": 0, "node_tools_executed": 0,
                          "mode": "native_agent_compatibility",
                          "raw_Messages_semantic_parity": False}))
        return 0
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: cancelled.set())
    try:
        if args.config is None:
            raise NativeError("root_policy_required")
        config = NativeConfig.load(args.config)
        envelope = mcp._decode(sys.stdin.buffer.read(MAX_BODY + 1))
        if (not isinstance(envelope, dict)
                or set(envelope) != {"binding", "request_id", "messages_body"}):
            raise NativeError("invalid_step_envelope")
        binding = OwnershipBinding.from_dict(envelope["binding"])
        def authorize(current):
            return (current == OwnershipBinding.from_dict(config.data["binding"])
                    and hashlib.sha256(_read(config.policy_path, uid=0, maximum=64 * 1024)
                                       ).hexdigest() == config.policy_sha256)

        result = run_step(config, binding, envelope["request_id"], envelope["messages_body"],
                          authorize=authorize, cancel_event=cancelled)
    except Exception as error:
        code = error.code if isinstance(error, NativeError) else "native_request_failed"
        result = {"error_code": code,
                  "content": [], "stop_reason": None}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error_code") else 0


if __name__ == "__main__":
    raise SystemExit(main())
