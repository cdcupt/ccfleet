"""Account-scoped Anthropic Messages on the controller using Fleet's OpenRouter key."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import socket
import sqlite3
import stat
import threading
import time
from decimal import Decimal

from . import gateway
from .config import ConfigError
from .openrouter_billing import BillingError, hold_cents

PAGE = "/account/openrouter"
HOST = "openrouter.ai"
SLUG = re.compile(r"[A-Za-z0-9~][A-Za-z0-9_.~-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
RECEIPT = re.compile(r"(?:gen-|msg_)[A-Za-z0-9_-]{1,180}\Z")
LIMIT = 16 * 1024 * 1024
FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "system",
    "tools",
    "tool_choice",
    "stream",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "thinking",
    "safeguards",
    "output_config",
    "context_management",
    "cache_control",
}


def parse_models(raw):
    if not raw:
        return {}
    try:
        models = json.loads(raw, parse_float=Decimal)
        if not isinstance(models, dict) or not 1 <= len(models) <= 64:
            raise ValueError
        for slug, rates in models.items():
            if (
                not SLUG.fullmatch(slug)
                or not isinstance(rates, list)
                or len(rates) != 2
                or any(
                    type(v) not in (int, Decimal)
                    or not 0 <= v <= 10000
                    or Decimal(v).as_tuple().exponent < -8
                    for v in rates
                )
            ):
                raise ValueError
        return {key: tuple(map(Decimal, rates)) for key, rates in models.items()}
    except (ValueError, ArithmeticError, TypeError):
        raise ConfigError(
            "OpenRouter models need IDs and two USD-per-million price ceilings."
        ) from None


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ValueError("OpenRouter needs a private service-owned key file.")
        key = source.read(513).removesuffix(b"\n")
        if not re.fullmatch(rb"sk-or-[a-zA-Z0-9_-]{12,500}", key):
            raise ValueError("Invalid OpenRouter key file.")
        return key.decode("ascii")


def prepare(value, models):
    from ccfleet_agent.api_model_relay import prepare_blocks

    for name in gateway.wire.IDENTITY_FIELDS:
        value.pop(name, None)
    model = value.get("model")
    if not isinstance(model, str) or model not in models:
        raise ValueError("Choose a model offered by this Fleet OpenRouter endpoint.")
    if (
        value.keys() - FIELDS
        or type(value.get("max_tokens")) is not int
        or not 1 <= value["max_tokens"] <= 1_000_000
        or type(value.get("stream", False)) is not bool
        or not isinstance(value.get("messages"), list)
        or not value["messages"]
    ):
        raise ValueError("Unsupported OpenRouter Messages request.")
    # Reuse Fleet's text / local tool contract. Exclude provider-hosted tools, remote
    # content and separately billed cache writes from this prepaid text route.
    prepare_blocks(value.get("system", ""))
    value.pop("cache_control", None)
    for msg in value["messages"]:
        if (
            not isinstance(msg, dict)
            or msg.keys() != {"role", "content"}
            or msg["role"] not in ("user", "assistant", "system")
        ):
            raise ValueError("Invalid message.")
        prepare_blocks(msg["content"], history=msg["role"] != "system")
    tools = value.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Invalid tool declarations.")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not isinstance(tool.get("input_schema"), dict)
            or tool.keys()
            - {
                "name",
                "description",
                "input_schema",
                "cache_control",
                "strict",
                "type",
                "defer_loading",
            }
            or tool.get("type", "custom") != "custom"
        ):
            raise ValueError("Use local Claude Code tools on this route.")
        tool.pop("cache_control", None)
        tool.pop("defer_loading", None)
    prompt, completion = models[model]
    value["provider"] = {
        "max_price": {
            "prompt": float(prompt),
            "completion": float(completion),
            "request": 0,
            "image": 0,
        }
    }
    return value


def credential(headers):
    auth, keys = headers.get_all("Authorization", []), headers.get_all("x-api-key", [])
    if len(auth) + len(keys) != 1:
        return ""
    if keys:
        return keys[0]
    scheme, _, token = auth[0].partition(" ")
    return token if scheme.lower() == "bearer" else ""


def sse(response):
    """Bound and decode complete native events, including multi-line SSE data."""
    data, size = [], 0
    while True:
        line = response.readline(LIMIT + 1)
        if not line:
            raise ValueError("Incomplete OpenRouter stream.")
        size += len(line)
        if size > LIMIT:
            raise ValueError("Oversized OpenRouter event.")
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
        if not line:
            if data:
                raw = b"\n".join(data)
                event = json.loads(raw, parse_float=Decimal)
                yield event, raw
                if event["type"] in {"message_stop", "error"}:
                    return
            data, size = [], 0


class Service:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.models = (
            parse_models(getattr(cfg, "openrouter_models", "")) if not cfg.broker_only else {}
        )
        self.key = read_key(cfg.openrouter_key_file) if self.models else None
        self.stop = threading.Event()

    def catalog(self, owner):
        recent = self.store.openrouter_recent_models(owner, self.models)
        ordered = dict.fromkeys([*recent, *self.models])
        rows = [{"id": model, "type": "model", "display_name": model + " · Fleet +5%",
                 "fleet_recent": model in recent} for model in ordered]
        return {"data": rows, "has_more": False, "first_id": rows[0]["id"],
                "last_id": rows[-1]["id"]}

    def connect(self):
        return http.client.HTTPSConnection(HOST, timeout=10)

    def reconcile(self):
        while not self.stop.is_set():
            for row in self.store.openrouter_pending(time.time()):
                if self.stop.is_set():
                    return
                try:
                    self.lookup(row["id"], row["generation_id"])
                except (ValueError, OSError, http.client.HTTPException, sqlite3.Error,
                        KeyError, TypeError):
                    continue  # Leave the receipt pending for the next read-only lookup.
            self.stop.wait(30)

    def lookup(self, request_id, generation):
        if not RECEIPT.fullmatch(generation):
            raise BillingError("Invalid upstream generation ID.")
        with contextlib.closing(self.connect()) as conn:
            conn.request(
                "GET",
                "/api/v1/generation?id=" + generation,
                headers={"Authorization": "Bearer " + self.key},
            )
            response = conn.getresponse()
            if response.status != 200:
                return False
            record = json.loads(response.read(65537), parse_float=Decimal)["data"]
        if (
            record["id"] != generation
            or record.get("session_id") != request_id
            or not (record.get("finish_reason") or record.get("cancelled") is True)
            or record.get("total_cost") is None
        ):
            return False
        self.store.openrouter_identify(request_id, generation, verified=True)
        self.store.openrouter_settle(request_id, generation, record["total_cost"])
        return True

    def forward(self, handler, owner, token, value, headers, body_size):
        store = self.store
        try:
            value = prepare(value, self.models)
            cents = hold_cents(body_size, value["max_tokens"], self.models[value["model"]])
            request_id = store.openrouter_reserve(owner, token, value["model"], cents)
        except BillingError as exc:
            gateway.model_error(handler, 402, str(exc))
            return
        except (ValueError, TypeError, KeyError):
            gateway.model_error(handler, 400, "Unsupported model or Messages options.")
            return
        value["session_id"] = request_id
        connection = None
        done = threading.Event()
        sock = response = watcher = None
        committed = False
        generation = ""
        deadline = time.monotonic() + self.cfg.gateway_request_timeout_s

        def authorized():
            return (
                not self.stop.is_set()
                and time.monotonic() < deadline
                and store.openrouter_principal(token) == owner
                and not store.wallet_spending_blocked(owner)
                and not gateway.GatewayRuntime._disconnected(handler)
            )

        def watch():
            while not done.wait(0.2):
                try:
                    if authorized():
                        continue
                except Exception:
                    pass
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                return

        def identify(candidate):
            nonlocal generation
            if not generation and isinstance(candidate, str) and RECEIPT.fullmatch(candidate):
                store.openrouter_identify(request_id, candidate)
                generation = candidate

        def settle(usage):
            cost = usage.get("cost") if isinstance(usage, dict) else None
            if generation and cost is not None:
                store.openrouter_settle(request_id, generation, cost)

        try:
            connection = self.connect()
            connection.connect()
            sock = connection.sock
            sock.settimeout(self.cfg.gateway_request_timeout_s)
            if not authorized():
                gateway.model_error(handler, 403, "OpenRouter access ended.")
                return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            store.openrouter_submit(request_id, owner, token)
            outbound = {
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
            }
            if "anthropic-beta" in headers:
                outbound["x-anthropic-beta"] = headers["anthropic-beta"]
            connection.request("POST", "/api/v1/messages", json.dumps(value).encode(), outbound)
            response = connection.getresponse()
            identify(response.getheader("X-Generation-Id"))
            if response.status != 200:
                if response.status in (400, 401, 402, 403, 404, 413, 422, 429) and not generation:
                    store.openrouter_release(request_id, rejected=True)
                status = response.status if response.status in (400, 404, 413, 429, 503) else 502
                gateway.model_error(handler, status, "OpenRouter could not serve this request.")
                return
            media = response.getheader("Content-Type", "").split(";")[0].lower().strip()
            if not value.get("stream"):
                if media != "application/json":
                    raise ValueError("Invalid upstream response type.")
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError("Oversized reply.")
                message = json.loads(raw, parse_float=Decimal)
                if message.get("type") != "message":
                    raise ValueError("Invalid provider message.")
                identify(message.get("id"))
                settle(message.get("usage"))
                if authorized():
                    handler._send(
                        200,
                        raw,
                        {
                            "Content-Type": media,
                            "Cache-Control": "no-store",
                            "x-should-retry": "false",
                        },
                    )
                return
            if media != "text/event-stream":
                raise ValueError("Invalid upstream stream type.")
            final_usage = None
            for event, raw in sse(response):
                kind = event["type"]
                if kind == "message_start":
                    identify(event["message"].get("id"))
                elif kind == "message_delta":
                    if event.get("usage", {}).get("cost") is not None:
                        final_usage = event["usage"]
                elif kind == "message_stop":
                    settle(final_usage)
                elif kind == "error":
                    raw = (b'{"type":"error","error":{"type":"api_error",'
                           b'"message":"OpenRouter stream failed."}}')
                elif kind not in {
                    "content_block_start",
                    "content_block_delta",
                    "content_block_stop",
                    "ping",
                }:
                    raise ValueError("Invalid provider event.")
                if not authorized():
                    return
                if not committed:
                    committed = True
                    handler.protocol_version = "HTTP/1.1"
                    handler.send_response(200)
                    for name, val in (
                        ("Content-Type", media),
                        ("Cache-Control", "no-store"),
                        ("Connection", "close"),
                        ("Transfer-Encoding", "chunked"),
                        ("x-should-retry", "false"),
                    ):
                        handler.send_header(name, val)
                    handler.end_headers()
                frame = (
                    b"event: "
                    + kind.encode("ascii")
                    + b"\n"
                    + b"".join(b"data: " + line + b"\n" for line in raw.splitlines())
                    + b"\n"
                )
                handler.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException, sqlite3.Error, ValueError, KeyError, TypeError):
            if not committed:
                gateway.model_error(handler, 502, "OpenRouter request interrupted.")
        finally:
            done.set()
            if watcher:
                watcher.join(timeout=1)
            if response:
                response.close()
            if connection:
                connection.close()
            store.openrouter_release(request_id)  # Only releases a never-submitted hold.
