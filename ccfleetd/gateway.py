"""Authenticated, owner-scoped Messages API with safe multi-slot routing."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import select
import socket
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any

from ccfleet_agent import inference_client as wire
from ccfleet_agent import local_relay

from .gateway_transport import GatewayTransportError, SSHAttempt

PATHS = wire.PATHS
RETRY_STATUSES = frozenset({401, 403, 409, 429, 502, 503, 504, 529})
MAX_CACHE = 4096
AFFINITY_TTL = 24 * 3600


def check_json_complexity(raw: bytes) -> None:
    """Bound object expansion before the ordinary JSON decoder allocates it."""
    quoted = escaped = False
    depth = nodes = 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            nodes += 1
            if depth > 128:
                raise ValueError("JSON is too deeply nested")
        elif byte in (125, 93):
            depth -= 1
        elif byte in (44, 58):
            nodes += 1
        if nodes > 200000:
            raise ValueError("JSON contains too many structured values")


def model_error(handler, status: int, message: str) -> None:
    handler.close_connection = True
    category = {400: "invalid_request_error", 401: "authentication_error",
                403: "permission_error", 413: "request_too_large",
                429: "rate_limit_error"}.get(status, "api_error")
    raw = json.dumps({"type": "error", "error": {
        "type": category, "message": message}}, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.send_header("Content-Length", str(len(raw)))
    if status == 503:
        handler.send_header("Retry-After", "3")
    handler.end_headers()
    with contextlib.suppress(OSError):
        handler.wfile.write(raw)


def authenticate(headers, store, *, now: float, touch: bool = True):
    auth, keys = headers.get_all("Authorization", []), headers.get_all("x-api-key", [])
    if len(auth) + len(keys) != 1:
        return None
    if auth:
        scheme, separator, token = auth[0].partition(" ")
        if not separator or scheme.lower() != "bearer" or token != token.strip():
            return None
    else:
        token = keys[0]
    return store.resolve_gateway_key(token, now=now, touch=touch)


def session_id(headers, body: dict[str, Any]) -> str:
    values = headers.get_all("X-CCFleet-Session", [])
    if len(values) > 1:
        raise ValueError("ambiguous session")
    value = values[0] if values else None
    if value is None:
        meta = body.get("metadata")
        if isinstance(meta, dict):
            value = meta.get("session_id")
            if value is None and isinstance(meta.get("user_id"), str):
                user = meta["user_id"]
                match = re.search(r"(?:^|_)session_([A-Za-z0-9-]{8,128})$", user)
                if match:
                    value = match[1]
                elif len(user) <= 4096 and user.startswith("{"):
                    try:
                        nested = wire._json(user.encode())
                        value = nested.get("session_id") if isinstance(nested, dict) else None
                    except (ValueError, UnicodeError, RecursionError):
                        pass
    if value is None:
        return ""
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
        raise ValueError("invalid session")
    return value


class GatewayRuntime:
    def __init__(self, cfg, store, *, transport_factory=None, clock=time.monotonic):
        self.cfg, self.store, self.clock = cfg, store, clock
        self.transport_factory = transport_factory or SSHAttempt
        self.lock = threading.RLock()
        self.active = 0
        self.by_key: dict[str, int] = {}
        self.body_bytes = 0
        self.affinity = OrderedDict()
        self.cooldowns = OrderedDict()
        self.health = OrderedDict()
        self.salt = os.urandom(32)
        self.stopping = threading.Event()

    @property
    def enabled(self) -> bool:
        return self.cfg.gateway_ready

    def _reserve(self, key_id: str, size: int) -> bool:
        with self.lock:
            if (self.active >= self.cfg.gateway_max_active
                    or self.by_key.get(key_id, 0) >= self.cfg.gateway_max_per_key
                    or self.body_bytes + size > self.cfg.gateway_body_budget_bytes):
                return False
            self.active += 1
            self.by_key[key_id] = self.by_key.get(key_id, 0) + 1
            self.body_bytes += size
            return True

    def _release(self, key_id: str, size: int) -> None:
        with self.lock:
            self.active -= 1
            self.body_bytes -= size
            self.by_key[key_id] -= 1
            if not self.by_key[key_id]:
                del self.by_key[key_id]

    def _session_key(self, key, session: str) -> str:
        if not session:
            return ""
        payload = f"{key['id']}:{key['generation']}:{session}".encode()
        return hmac.new(self.salt, payload, hashlib.sha256).hexdigest()

    def status(self, key, pinned: str = "") -> dict[str, Any]:
        """Bounded, read-only slot readiness; this never sends a model POST."""
        targets = self.store.gateway_targets(key)
        if pinned:
            targets = [target for target in targets if target["slot_id"] == pinned]
        ready = {}
        deadline = self.clock() + 8

        def probe(target):
            identity = (target["slot_id"], target["claim_generation"],
                        target["access_host"], target["access_port"], target["ssh_host_key"])
            with self.lock:
                cached = self.health.get(identity)
                if cached and cached[1] > self.clock():
                    return target["slot_id"], cached[0] and self._authorized(key, target)
            if self.clock() >= deadline or not self._authorized(key, target):
                return target["slot_id"], False
            attempt = self.transport_factory(target, self.cfg)
            timer = threading.Timer(max(0.01, deadline-self.clock()), attempt.close)
            timer.daemon = True
            timer.start()
            try:
                available = bool(attempt.check()) and self._authorized(key, target)
            except Exception:
                available = False
            finally:
                timer.cancel()
                attempt.close()
            with self.lock:
                self._cache_put(self.health, identity,
                                (available, self.clock() + (10 if available else 5)))
            return target["slot_id"], available

        if self._reserve(key["id"], 0):
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    ready = dict(executor.map(probe, targets))
            finally:
                self._release(key["id"], 0)
        held = {slot["id"]: slot for slot in self.store.list_slots(held_by=key["account_id"])}
        ids = [pinned] if pinned else key["slot_ids"]
        rows = [{"id": identifier, "name": held[identifier].get("name") or identifier,
                 "state": held[identifier]["state"], "ready": ready.get(identifier, False)}
                for identifier in ids if identifier in held]
        return {"ready": any(row["ready"] for row in rows), "mode": key["mode"],
                "base_url": self.cfg.public_url, "slots": rows,
                "key": {name: key[name] for name in
                        ("id", "name", "slot_ids", "mode", "generation")}}

    def _cache_put(self, cache, key, value) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > MAX_CACHE:
            cache.popitem(last=False)

    def _targets(self, key, session_key: str, pinned: str, model_key: str):
        targets = self.store.gateway_targets(key)
        if pinned:
            targets = [t for t in targets if t["slot_id"] == pinned]
        now = self.clock()
        with self.lock:
            hot, cold = [], []
            for target in targets:
                cooldown = self.cooldowns.get((key["id"], target["slot_id"], model_key), 0)
                (cold if cooldown > now else hot).append(target)
            targets = hot + cold
            if key["mode"] == "sticky" and session_key and not pinned:
                remembered = self.affinity.get(session_key)
                if remembered and remembered[2] > now:
                    for target in targets:
                        if (target["slot_id"], target["claim_generation"]) == remembered[:2]:
                            if self.cooldowns.get(
                                    (key["id"], target["slot_id"], model_key), 0) <= now:
                                targets.remove(target)
                                targets.insert(0, target)
                            break
                elif remembered:
                    self.affinity.pop(session_key, None)
            return targets

    def _authorized(self, key, target) -> bool:
        if self.stopping.is_set():
            return False
        try:
            return self.store.gateway_request_is_active(
                key["id"], key["generation"], target["slot_id"], target["claim_generation"])
        except Exception:
            return False

    def _failure(self, key, target, model_key) -> None:
        with self.lock:
            self._cache_put(self.cooldowns, (key["id"], target["slot_id"], model_key),
                            self.clock() + 30)

    def _success(self, session_key: str, target) -> None:
        if session_key:
            with self.lock:
                self._cache_put(self.affinity, session_key,
                                (target["slot_id"], target["claim_generation"],
                                 self.clock() + AFFINITY_TTL))

    @staticmethod
    def _disconnected(handler) -> bool:
        try:
            ready, _, _ = select.select([handler.connection], [], [], 0)
            return bool(ready and handler.connection.recv(1, socket.MSG_PEEK) == b"")
        except (OSError, ValueError):
            return True

    def _watch(self, handler, key, target, attempt, done, deadline) -> None:
        while not done.wait(0.2):
            try:
                allowed = (self.clock() < deadline and self._authorized(key, target)
                           and not self._disconnected(handler))
            except Exception:
                allowed = False
            if not allowed:
                attempt.close()
                return

    def handle(self, handler) -> None:
        handler.close_connection = True
        if not self.enabled:
            model_error(handler, 503, "The model gateway is not enabled.")
            return
        key = authenticate(handler.headers, self.store, now=time.time())
        if key is None:
            model_error(handler, 401, "A valid endpoint API key is required.")
            return
        names = [name.lower() for name in handler.headers]
        lengths = handler.headers.get_all("Content-Length", [])
        pins = handler.headers.get_all("X-CCFleet-Slot", [])
        if (len(names) != len(set(names)) or handler.headers.get_all("Transfer-Encoding")
                or handler.headers.get_all("Origin") or handler.headers.get_all("Expect")
                or handler.headers.get_all("Content-Encoding") or handler.path not in PATHS
                or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or len(lengths[0]) > 10 or str(int(lengths[0])) != lengths[0]
                or len(pins) > 1
                or any(not wire._header_value(value) for value in handler.headers.values())):
            model_error(handler, 400, "The model request framing is invalid.")
            return
        size = int(lengths[0])
        if not 0 < size <= self.cfg.gateway_max_body_bytes:
            model_error(handler, 413, "The model request exceeds the gateway transport limit.")
            return
        pinned = pins[0] if pins else ""
        if pinned and pinned not in key["slot_ids"]:
            model_error(handler, 403, "That slot is not selected for this endpoint.")
            return
        if not self._reserve(key["id"], size):
            model_error(handler, 503, "The gateway is busy. Send a new request later.")
            return
        try:
            handler.connection.settimeout(30)
            def expire_body():
                with contextlib.suppress(OSError):
                    handler.connection.shutdown(socket.SHUT_RDWR)
            body_timer = threading.Timer(30, expire_body)
            body_timer.daemon = True
            body_timer.start()
            try:
                raw = wire.read_exact(handler.rfile, size)
                check_json_complexity(raw)
                value = wire._json(raw)
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                if "metadata" in value and not isinstance(value["metadata"], (dict, type(None))):
                    raise ValueError("metadata")
                session = self._session_key(key, session_id(handler.headers, value))
                model = value.get("model", "")
                if not isinstance(model, str) or len(model) > 256:
                    raise ValueError("model")
                model_key = hashlib.sha256(model.encode()).hexdigest()
                for field in wire.IDENTITY_FIELDS:
                    value.pop(field, None)
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).encode()
                if len(body) > self.cfg.gateway_max_body_bytes:
                    raise ValueError("expanded body")
                headers = wire.sanitize_request_headers({
                    name.lower(): item for name, item in handler.headers.items()})
            except (ValueError, UnicodeError, RecursionError):
                model_error(handler, 400, "The model request must be valid JSON.")
                return
            except (OSError, EOFError):
                model_error(handler, 408, "The model request body did not arrive.")
                return
            finally:
                body_timer.cancel()
            handler.connection.settimeout(15)
            self._forward(handler, key, session, pinned, model_key, headers, body)
        finally:
            self._release(key["id"], size)

    def _forward(self, handler, key, session, pinned, model_key, headers, body) -> None:
        targets = self._targets(key, session, pinned, model_key)
        if not targets:
            model_error(handler, 503, "No selected slot is currently ready.")
            return
        deadline = self.clock() + self.cfg.gateway_request_timeout_s
        for position, target in enumerate(targets):
            if self._disconnected(handler):
                return
            if self.clock() >= deadline:
                model_error(handler, 504, "The gateway request deadline expired.")
                return
            if not self._authorized(key, target):
                # An endpoint configuration change is not permission to use the
                # next slot from a stale snapshot.
                refreshed = next((item for item in self.store.list_gateway_keys(key["account_id"])
                                  if item["id"] == key["id"]), None)
                if refreshed is None or refreshed["generation"] != key["generation"]:
                    model_error(handler, 403, "Endpoint authorization changed.")
                    return
                continue
            attempt = self.transport_factory(target, self.cfg)
            done = threading.Event()
            watcher = threading.Thread(target=self._watch,
                args=(handler, key, target, attempt, done, deadline), daemon=True)
            watcher.start()
            committed = False
            try:
                attempt.start(headers, body, path=handler.path,
                              before_model_send=lambda target=target: (self._authorized(key, target)
                                  and not self._disconnected(handler) and self.clock() < deadline))
                if not self._authorized(key, target) or self._disconnected(handler):
                    model_error(handler, 403, "Endpoint authorization changed.")
                    return
                reply = attempt.metadata()
                status = reply["status"]
                eligible = reply["retryable"] and status in RETRY_STATUSES
                if eligible and position + 1 < len(targets) and not pinned:
                    self._failure(key, target, model_key)
                    continue
                error_body = None
                if status >= 400:
                    # Even a trusted slot is not an unlimited public error source.
                    error_body = bytearray()
                    for part in attempt.chunks():
                        error_body.extend(part)
                        if len(error_body) > 65536:
                            raise GatewayTransportError()
                    attempt.finish(status)
                    try:
                        supplied_error = wire._json(bytes(error_body))
                        error = supplied_error.get("error", {})
                        overflow = (status == 400 and isinstance(error, dict)
                                    and error.get("type") == "invalid_request_error"
                                    and isinstance(error.get("message"), str)
                                    and error["message"].startswith("prompt is too long"))
                    except (ValueError, TypeError, AttributeError):
                        overflow = False
                    safe = BytesIO()
                    local_relay.send_upstream_error(
                        safe, status, reply["headers"], overflow=overflow)
                    safe.seek(0)
                    reply = wire.metadata(safe)
                    status = reply["status"]
                    error_body = wire.chunk(safe)
                if status < 400:
                    self._success(session, target)
                handler.protocol_version = "HTTP/1.1"
                handler.send_response(status)
                for name, value in reply["headers"].items():
                    handler.send_header(name, value)
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Transfer-Encoding", "chunked")
                handler.send_header("Connection", "close")
                # Headers commit the response, even before the first body byte.
                committed = True
                handler.end_headers()
                parts = [bytes(error_body)] if error_body is not None else attempt.chunks()
                for part in parts:
                    if not self._authorized(key, target) or self.clock() >= deadline:
                        raise GatewayTransportError()
                    handler.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    handler.wfile.flush()
                if error_body is None:
                    attempt.finish(status)
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
                return
            except GatewayTransportError as exc:
                if committed:
                    return
                if exc.retryable and position + 1 < len(targets) and not pinned:
                    self._failure(key, target, model_key)
                    continue
                model_error(handler, exc.status,
                            "The slot transport failed. This request was not replayed.")
                return
            except (OSError, ValueError, KeyError, TypeError):
                if not committed:
                    model_error(handler, 502,
                                "The slot response was invalid. This request was not replayed.")
                return
            finally:
                done.set()
                attempt.close()
                watcher.join(timeout=1)
        model_error(handler, 503, "No selected slot is currently ready.")
