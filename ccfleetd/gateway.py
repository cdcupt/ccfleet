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

from . import gateway_providers, throughput
from .gateway_transport import GatewayTransportError, transport_attempt

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
    if getattr(handler, "_gateway_native_no_retry", False):
        handler.send_header("x-should-retry", "false")
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
        self.transport_factory = transport_factory or transport_attempt
        self.lock = threading.RLock()
        self.active = 0
        self.by_key: dict[str, int] = {}
        self.body_bytes = 0
        self.affinity = OrderedDict()
        self.cooldowns = OrderedDict()
        self.health = OrderedDict()
        self.salt = os.urandom(32)
        self.stopping = threading.Event()
        self.throughput = (throughput.Recorder(store)
                           if not getattr(cfg, "broker_only", False) else None)

    def close(self) -> bool:
        self.stopping.set()
        return self.throughput.close() if self.throughput is not None else True

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
        provider = key.get("provider", gateway_providers.ANTHROPIC)
        prefix = "devin:" if provider == gateway_providers.DEVIN else ""
        payload = f"{prefix}{key['id']}:{key['generation']}:{session}".encode()
        return hmac.new(self.salt, payload, hashlib.sha256).hexdigest()

    def _provider_options(self, key):
        # Keep the legacy Store/fake transport contract unchanged. Only native
        # targets require the explicitly configured and verified runtime tuple.
        if key.get("provider", gateway_providers.ANTHROPIC) != gateway_providers.DEVIN:
            return {}
        return {"allow_devin": getattr(self.cfg, "gateway_devin_enabled", False) is True,
                "provider_runtime": gateway_providers.runtime_expectations(self.cfg)}

    def _store_targets(self, key):
        return self.store.gateway_targets(key, **self._provider_options(key))

    @staticmethod
    def _route_identity(key, target):
        """A native snapshot cannot inherit health, affinity or cooldown after rebinding."""
        legacy = (target["slot_id"], target["claim_generation"])
        if target.get("provider", gateway_providers.ANTHROPIC) != gateway_providers.DEVIN:
            return legacy
        return (*legacy, gateway_providers.DEVIN, key["id"], key["generation"],
                target["claim_id"], target["provider_binding_generation"],
                target["provider_account_fp"], target["bridge_contract"],
                target["native_version"], target["adapter_revision"],
                target["model_map_digest"])

    def _health_identity(self, key, target):
        return (*self._route_identity(key, target), target["access_host"], target["access_port"],
                target["ssh_host_key"])

    def _cooldown_identity(self, key, target, model_key):
        if target.get("provider", gateway_providers.ANTHROPIC) == gateway_providers.DEVIN:
            return (*self._route_identity(key, target), model_key)
        return key["id"], target["slot_id"], model_key

    def _transport_target(self, key, target):
        if target.get("provider", gateway_providers.ANTHROPIC) != gateway_providers.DEVIN:
            return target
        # Neither browser headers nor Messages JSON can supply these fields.
        # Each attempt has a fresh correlation ID, shared by status and request.
        binding = {"owner_id": key["account_id"], "key_id": key["id"],
            "key_generation": key["generation"], "slot_id": target["slot_id"],
            "claim_id": target["claim_id"], "claim_generation": target["claim_generation"],
            "provider_generation": target["provider_binding_generation"], "provider": "devin"}
        runtime = {name: target[name] for name in ("bridge_contract", "native_version",
            "adapter_revision", "model_map_digest", "provider_account_fp")}
        return {**target, "_native_context": {"binding": binding, "runtime": runtime,
                                               "request_id": os.urandom(16).hex()}}

    def status(self, key, pinned: str = "") -> dict[str, Any]:
        """Bounded, read-only slot readiness; this never sends a model POST."""
        targets = self._store_targets(key)
        if pinned:
            targets = [target for target in targets if target["slot_id"] == pinned]
        ready = {}
        deadline = self.clock() + 8

        def probe(target):
            identity = self._health_identity(key, target)
            with self.lock:
                cached = self.health.get(identity)
                if cached and cached[1] > self.clock():
                    return target["slot_id"], cached[0] and self._authorized(key, target)
            if self.clock() >= deadline or not self._authorized(key, target):
                return target["slot_id"], False
            attempt = timer = None
            try:
                attempt = self.transport_factory(self._transport_target(key, target), self.cfg)
                timer = threading.Timer(max(0.01, deadline-self.clock()), attempt.close)
                timer.daemon = True
                timer.start()
                available = bool(attempt.check()) and self._authorized(key, target)
            except Exception:
                available = False
            finally:
                if timer is not None:
                    timer.cancel()
                if attempt is not None:
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
        targets = self._store_targets(key)
        if pinned:
            targets = [t for t in targets if t["slot_id"] == pinned]
        now = self.clock()
        with self.lock:
            hot, cold = [], []
            for target in targets:
                cooldown = self.cooldowns.get(self._cooldown_identity(key, target, model_key), 0)
                (cold if cooldown > now else hot).append(target)
            targets = hot + cold
            if key["mode"] == "sticky" and session_key and not pinned:
                remembered = self.affinity.get(session_key)
                if remembered and remembered[-1] > now:
                    for target in targets:
                        if self._route_identity(key, target) == remembered[:-1]:
                            if self.cooldowns.get(
                                    self._cooldown_identity(key, target, model_key), 0) <= now:
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
            active = self.store.gateway_request_is_active(
                key["id"], key["generation"], target["slot_id"], target["claim_generation"],
                **self._provider_options(key))
            if not active:
                return False
            provider = key.get("provider", gateway_providers.ANTHROPIC)
            if target.get("provider", gateway_providers.ANTHROPIC) != provider:
                return False
            if provider != gateway_providers.DEVIN:
                return True
            return any(self._health_identity(key, current) == self._health_identity(key, target)
                       for current in self._store_targets(key))
        except Exception:
            return False

    def _failure(self, key, target, model_key) -> None:
        with self.lock:
            self._cache_put(self.cooldowns, self._cooldown_identity(key, target, model_key),
                            self.clock() + 30)

    def _success(self, session_key: str, target, key=None) -> None:
        if session_key:
            with self.lock:
                self._cache_put(self.affinity, session_key,
                                (*self._route_identity(key, target),
                                 self.clock() + AFFINITY_TTL))

    @staticmethod
    def _disconnected(handler) -> bool:
        try:
            ready, _, _ = select.select([handler.connection], [], [], 0)
            return bool(ready and handler.connection.recv(1, socket.MSG_PEEK) == b"")
        except (OSError, ValueError):
            return True

    def _watch(self, handler, key, target, attempt, done, deadline, cancelled=None) -> None:
        while not done.wait(0.2):
            try:
                allowed = (self.clock() < deadline and self._authorized(key, target)
                           and not self._disconnected(handler))
            except Exception:
                allowed = False
            if not allowed:
                if cancelled is not None:
                    cancelled.set()
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
        handler._gateway_native_no_retry = key.get("provider") == gateway_providers.DEVIN
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
        if (key.get("provider", gateway_providers.ANTHROPIC) == gateway_providers.DEVIN
                and handler.path != "/v1/messages"):
            model_error(handler, 501, "Token counting is not available for the native agent.")
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
        handler._gateway_native_no_retry = key.get("provider") == gateway_providers.DEVIN
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
            try:
                attempt = self.transport_factory(self._transport_target(key, target), self.cfg)
            except (GatewayTransportError, ValueError, KeyError, TypeError):
                model_error(handler, 503, "The selected slot transport is unavailable.")
                return
            done = threading.Event()
            cancelled = threading.Event()
            watcher = threading.Thread(target=self._watch,
                args=(handler, key, target, attempt, done, deadline, cancelled), daemon=True)
            watcher.start()
            committed = False
            observer = None
            try:
                attempt.start(headers, body, path=handler.path,
                              before_model_send=lambda target=target: (self._authorized(key, target)
                                  and not self._disconnected(handler) and self.clock() < deadline))
                if not self._authorized(key, target) or self._disconnected(handler):
                    model_error(handler, 403, "Endpoint authorization changed.")
                    return
                reply = attempt.metadata()
                status = reply["status"]
                eligible = (reply["retryable"] and status in RETRY_STATUSES
                            and target.get("provider") != gateway_providers.DEVIN)
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
                    self._success(session, target, key)
                if (self.throughput is not None and handler.path.split("?", 1)[0] == "/v1/messages"
                        and 200 <= status < 300
                        and reply["headers"].get("content-type") == "text/event-stream"):
                    with contextlib.suppress(Exception):
                        observer = throughput.StreamObserver("text/event-stream", clock=self.clock)
                handler.protocol_version = "HTTP/1.1"
                handler.send_response(status)
                for name, value in reply["headers"].items():
                    handler.send_header(name, value)
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Transfer-Encoding", "chunked")
                handler.send_header("Connection", "close")
                if handler._gateway_native_no_retry:
                    # Applies to success too: a later truncated stream must not
                    # invite an SDK replay of a native turn already dispatched.
                    handler.send_header("x-should-retry", "false")
                # Headers commit the response, even before the first body byte.
                committed = True
                handler.end_headers()
                parts = [bytes(error_body)] if error_body is not None else attempt.chunks()
                for part in parts:
                    if not self._authorized(key, target) or self.clock() >= deadline:
                        raise GatewayTransportError()
                    handler.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    handler.wfile.flush()
                    if observer is not None:
                        try:
                            observer.feed(part)
                        except Exception:
                            with contextlib.suppress(Exception):
                                observer.discard()
                            observer = None
                if error_body is None:
                    attempt.finish(status)
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
                if (observer is not None and not cancelled.is_set()
                        and not self.stopping.is_set()):
                    with contextlib.suppress(Exception):
                        sample = observer.finish()
                        if sample is not None and self.clock() < deadline:
                            self.throughput.submit(sample, now=time.time())
                return
            except GatewayTransportError as exc:
                if committed:
                    return
                if (exc.retryable and position + 1 < len(targets) and not pinned
                        and not (target.get("provider") == gateway_providers.DEVIN
                                 and getattr(attempt, "model_started", False))):
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
                if observer is not None:
                    with contextlib.suppress(Exception):
                        observer.discard()
                attempt.close()
                watcher.join(timeout=1)
        model_error(handler, 503, "No selected slot is currently ready.")
