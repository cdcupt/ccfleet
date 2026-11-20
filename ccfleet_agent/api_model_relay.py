"""Slot-local, API-billed Messages adapter for an explicitly enrolled GPT or Gemini key."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import json
import os
import pwd
import re
import select
import signal
import stat
import sys
import tempfile
import threading
from pathlib import Path


def sibling(name):
    # This script is installed beside root-owned modules and launched with -I.
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wire = sibling("local_relay")
egress = sibling("egress")
contract = sibling("api_model_contract")
ALIASES = contract.ALIASES
FIELDS = {"messages", "max_tokens", "model", "system", "tools", "tool_choice", "stop_sequences",
          "stream", "temperature", "top_k", "top_p", "thinking", "output_config",
          "context_management", "metadata"}
KEEP_THINKING = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}


def credential_path(home, provider):
    if provider not in ALIASES:
        raise ValueError("invalid provider")
    return home / (".ccfleet-" + provider + "-api.json")


def receipt(profile):
    return {k: v for k, v in profile.items() if k != "api_key"} | {
        "credential_fp": contract.digest(profile["api_key"])}


def enroll(home, reservation, secret):
    contract.validate(reservation, credential=False)
    if not re.fullmatch(r"[!-~]{16,2048}", secret):
        raise ValueError("invalid provider credential")
    profile = {**reservation, "api_key": secret}
    path = credential_path(home, profile["provider"])
    fd, name = tempfile.mkstemp(dir=home, prefix=".ccfleet-api-enroll-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(profile, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return receipt(profile)


def load_profile(home, expected):
    path = credential_path(home, expected["provider"])
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("invalid credential permissions")
    profile = wire.read_object(path, limit=8192)
    if set(profile) != {*contract.RESERVATION, "api_key"}:
        raise ValueError("invalid profile")
    if receipt(profile) != expected:
        raise ValueError("slot enrollment has changed")
    return profile


def check_blocks(blocks, *, history=False):
    if isinstance(blocks, str):
        return
    if not isinstance(blocks, list):
        raise ValueError("invalid message content")
    for b in blocks:
        if not isinstance(b, dict):
            raise ValueError("invalid block")
        kind = b.get("type")
        if kind == "text" and isinstance(b.get("text"), str):
            continue
        if history and kind == "tool_use" and isinstance(b.get("input"), dict):
            if isinstance(b.get("name"), str) and isinstance(b.get("id"), str):
                continue
        if history and kind == "tool_result" and isinstance(b.get("tool_use_id"), str):
            check_blocks(b.get("content", ""))
            continue
        if history and kind in ("thinking", "redacted_thinking"):
            continue
        raise ValueError("unsupported content in API model request")


def model_request(raw, profile):
    value = wire.decode_json(wire.sanitize_body(raw))
    if (set(value) - FIELDS or value.get("model") != ALIASES[profile["provider"]]
            or type(value.get("max_tokens")) is not int or value["max_tokens"] <= 0
            or type(value.get("stream", False)) is not bool
            or not isinstance(value.get("messages"), list) or not value["messages"]):
        raise ValueError("unsupported API model request")
    check_blocks(value.get("system", ""))
    for message in value["messages"]:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in ("user", "assistant")):
            raise ValueError("unsupported message")
        check_blocks(message["content"], history=True)
        if isinstance(message["content"], list):
            # Preserve the Responses adapter's encrypted reasoning for stateless tool
            # continuations. Foreign reasoning blocks don't belong in the next provider's call.
            message["content"] = [
                b for b in message["content"] if b["type"] not in ("thinking", "redacted_thinking")
                or (profile["provider"] == "openai" and isinstance(
                    signature := b.get("signature", b.get("data")), str)
                    and signature.startswith("litellm_encrypted_reasoning:"))
            ]
    tools = value.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("invalid tool declarations")
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("input_schema"), dict)
                or not isinstance(tool.get("name"), str)
                or tool.keys() - {"name", "input_schema", "description", "cache_control",
                                  "defer_loading", "strict", "type"}
                or tool.get("type", "custom") != "custom"):
            raise ValueError("use client-executed tools on this route")
        # The API adapters receive the actual schemas for all local tools.
        tool.pop("defer_loading", None)
    if value.pop("context_management", KEEP_THINKING) != KEEP_THINKING:
        raise ValueError("unsupported context edits")
    options = value.get("output_config", {})
    if not isinstance(options, dict) or options.keys() - {"effort", "format"}:
        raise ValueError("unsupported output settings")
    if profile["provider"] == "openai":
        value["store"] = False
    value["model"] = profile["provider"] + "/" + profile["model"]
    return value


def write_data(out, raw):
    for pos in range(0, len(raw), wire.CHUNK_SIZE):
        wire.write_chunk(out, raw[pos:pos + wire.CHUNK_SIZE])


async def respond(out, value, secret, create):
    streaming = value.get("stream", False)
    reply = await create(**value, api_key=secret, num_retries=0, max_retries=0,
                         timeout=wire.REQUEST_TIMEOUT)
    if not streaming:
        body = reply.model_dump() if hasattr(reply, "model_dump") else reply
        raw = json.dumps(body, ensure_ascii=False).encode()
        wire.write_metadata(out, 200, {"content-type": "application/json"})
        write_data(out, raw)
    else:
        iterator = reply.__aiter__()
        # Upstream failures before the first event can still return an HTTP status.
        first = await iterator.__anext__()
        wire.write_metadata(out, 200, {"content-type": "text/event-stream"})
        try:
            chunk = first
            while True:
                # The pinned adapter emits one complete SSE event per chunk.
                # Provider diagnostics can contain request details or credentials.
                if not isinstance(chunk, bytes) or not chunk.startswith(b"event: "):
                    raise ValueError("invalid adapter event")
                event = chunk.split(b"\n", 1)[0]
                if event == b"event: error":
                    write_data(out, b'event: error\ndata: {"type":"error","error":'
                                    b'{"type":"api_error","message":"Stream failed."}}\n\n')
                    break
                write_data(out, chunk)
                if event == b"event: message_stop":
                    break
                chunk = await iterator.__anext__()

        except Exception as error:
            # A committed stream cannot carry a second response frame.
            raise BrokenPipeError("provider stream interrupted") from error
    wire.write_chunk(out, b"")


def serve(inp, out, home, create, *, after_body=lambda: None):
    out = wire._GatewayOutput(out)
    try:
        status = wire.read_metadata(inp)
        if set(status) != {"version", "operation", "route"} or status["operation"] != "status":
            raise ValueError("invalid readiness")
        profile = load_profile(home, status["route"])
        wire.require_enabled()
        wire.write_metadata(out, 200, {"content-type": "application/json"})
        wire.write_chunk(out, json.dumps({"ready": True, "protocol": 2,
                                         "route": status["route"]}).encode())
        wire.write_chunk(out, b"")
        path, _, size = wire.validate_request(wire.read_metadata(inp))
        if path.split("?", 1)[0] != "/v1/messages":
            raise wire.RelayError(501, "token counting is unavailable")
        value = model_request(wire.read_exact(inp, size), profile)
        after_body()
        wire.require_enabled()
        profile = load_profile(home, status["route"])
        asyncio.run(respond(out, value, profile["api_key"], create))
        return 0
    except (EOFError, BrokenPipeError):
        return 2
    except Exception as exc:
        status = getattr(exc, "status_code", getattr(exc, "status", None))
        if type(status) is not int or not 400 <= status <= 599:
            status = 400 if isinstance(exc, (ValueError, KeyError, TypeError)) else 502
        wire.send_error(out, status, "The assigned API model route could not serve this request.")
        return 2


def main():
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.argv[1:] == ["serve"]:
        # No slot-home dotenv files, user-configurable upstreams, or provider keys from SSH.
        os.chdir("/")
        os.environ.clear()
        os.environ.update(PATH=os.defpath, HOME="/nonexistent", LITELLM_LOCAL_MODEL_COST_MAP="True",
                          LITELLM_MODE="PRODUCTION", LITELLM_LOG="ERROR",
                          **egress.protected_native_environment())
        output = sys.stdout.buffer
        sys.stdout = sys.stderr  # Keep SDK diagnostics outside the framed protocol.
        import litellm

        litellm.suppress_debug_info = True
        litellm.num_retries = 0
        signal.alarm(wire.INPUT_TIMEOUT)

        def after_body():
            signal.alarm(wire.REQUEST_TIMEOUT)

            def disconnected():
                # The gateway keeps stdin open for this request's lifetime.
                select.select([sys.stdin], [], [])
                os._exit(2)
            threading.Thread(target=disconnected, daemon=True).start()

        return serve(sys.stdin.buffer, output, home, litellm.anthropic.messages.acreate,
                     after_body=after_body)
    parser = argparse.ArgumentParser(description="Enroll an API credential on its assigned slot")
    parser.add_argument("action", choices=["enroll"])
    parser.add_argument("reservation", type=argparse.FileType("r"))
    args = parser.parse_args()
    if os.getuid() == 0 or not sys.stdin.isatty():
        parser.error("run as the slot user in an interactive terminal")
    with args.reservation as stream:
        reservation = json.loads(stream.read(4097))
    contract.validate(reservation, credential=False)
    info = enroll(home, reservation, getpass.getpass("Provider API key: "))
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
