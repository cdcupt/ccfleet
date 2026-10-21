"""Private traffic DTOs: machine NIC bytes and independently measured API usage.

No authorization is implied by these helpers. Routes must pass only the owner's
slots, or all slots after checking the operator role. Network usage is a machine
observation, never per-user attribution on a shared machine. Nothing is public.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from collections.abc import Mapping
from typing import Any

from ccfleet_agent import network_traffic as network
from ccfleet_agent.relay_metrics import WINDOW_DAYS

from . import performance, slots

INTERVAL_S = 15
STALE_AFTER_S = 60
HOST_SNAPSHOT = "/run/ccfleet-network/network.json"
MAX_OBSERVATIONS = 512
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens")


def _api_empty():
    return {"state": "unavailable", "window_days": WINDOW_DAYS, "requests": None,
            "success": None, "errors": None, "usage_eligible_messages": None,
            "usage_samples": None, "cache_read_samples": None, "cache_creation_samples": None,
            "tokens": None, "token_state": "unknown"}


def _api(report):
    if report is None:
        return _api_empty()
    result = {**_api_empty(), "state": "ready", "requests": report["requests"],
              "success": report["success"], "errors": report["requests"] - report["success"]}
    usage = report.get("token_usage")
    if usage is not None:
        result.update(usage_eligible_messages=usage["eligible"], usage_samples=usage["samples"],
                      cache_read_samples=usage["cache_read_samples"],
                      cache_creation_samples=usage["cache_creation_samples"])
        if usage["samples"]:
            tokens = {field: usage[field] for field in TOKEN_FIELDS}
            tokens["cache_read_input_tokens"] = (usage["cache_read_input_tokens"]
                                                  if usage["cache_read_samples"] else None)
            tokens["cache_creation_input_tokens"] = (usage["cache_creation_input_tokens"]
                                                      if usage["cache_creation_samples"] else None)
            tokens["total_tokens"] = sum(usage[field] for field in TOKEN_FIELDS)
            result["tokens"] = tokens
            result["token_state"] = ("complete" if usage["samples"] == usage["eligible"]
                                     == usage["cache_read_samples"]
                                     == usage["cache_creation_samples"]
                                     else "partial")
    return result


def slot_summary(store, cfg, slot: Mapping[str, Any], now: float, *,
                 include_unheld: bool = False) -> dict[str, Any]:
    """A retained measured window begins after this assignment; no prior holder bytes."""
    result = {"network": network.empty("machine_nic"), "network_attribution": "machine",
              "api": _api_empty(), "api_scope": "bound_slot_relay"}
    try:
        node_id = slot["node_id"]
        same_machine = [row for row in store.list_slots() if row.get("node_id") == node_id
                        and row.get("state") in slots.HELD]
        if len(same_machine) > 1:
            result["network_attribution"] = "shared_machine"
        # Owner nodes are their owner's machine; claim timestamps belong only to rented slots.
        claimed = (0 if slot.get("kind") == slots.OWNER_SLOT else slot.get("claimed_at"))
        if include_unheld and slot.get("state") == slots.FREE and not slot.get("held_by"):
            claimed = 0
        if not network.number(claimed):
            return result
        rows = list(reversed(store.recent_heartbeats(node_id, limit=MAX_OBSERVATIONS)))
        observations = []
        for row in rows:
            payload = row.get("payload")
            value = payload.get("network") if isinstance(payload, Mapping) else None
            # Server arrival and agent observation must both be fresh enough relative
            # to each other. Do not turn a replayed agent counter into a fresh reading.
            valid = network.validate_observation(value)
            if (valid is not None and network.number(row.get("ts"))
                    and abs(row["ts"] - valid["observed_at"]) <= cfg.heartbeat_max_age_s):
                observations.append(valid)
            else:
                observations.append(None)
        result["network"] = network.summarize(observations, now,
            max_gap_s=cfg.heartbeat_max_age_s, since=claimed)
        if slot.get("state") in (slots.CLAIMED, slots.ACTIVE):
            # Arrival order is canonical even if the server wall clock moved backwards.
            report = performance._current_report(slot, store.latest_heartbeats().get(node_id),
                                                  node_id, now, cfg.heartbeat_max_age_s)
            result["api"] = _api(report)
    except Exception:  # telemetry failure must never break a customer's page
        pass
    return result


def controller_summary(store, cfg, now: float, *, network_report=None) -> dict[str, Any]:
    """Operator-only host/service network and distinct bound-relay API aggregates."""
    source = network_report if isinstance(network_report, Mapping) else {}
    host = network.validate_summary(source.get("host"), "machine_nic")
    service = network.validate_summary(source.get("service"), "service_namespace")
    result = {"host": host or network.empty("machine_nic"),
              "service": service or network.empty("service_namespace"),
              "api": _api_empty(), "api_scope": "fleet_bound_relays",
              "eligible_slots": 0, "reporting_slots": 0}
    try:
        nodes = {row["id"] for row in store.list_nodes() if row.get("enabled")}
        latest = store.latest_heartbeats()
        candidates = performance._active_slots(store.list_slots(), nodes)
        totals = {key: 0 for key in ("requests", "success", "errors", "usage_eligible_messages",
                                     "usage_samples", "cache_read_samples",
                                     "cache_creation_samples")}
        tokens = dict.fromkeys((*TOKEN_FIELDS, "total_tokens"), 0)
        token_reports, usage_reports, complete = 0, 0, True
        known_token_fields = set()
        for slot in candidates:
            result["eligible_slots"] += 1
            report = performance._current_report(slot, latest.get(slot["node_id"]), slot["node_id"],
                                                  now, cfg.heartbeat_max_age_s)
            row = _api(report)
            if row["state"] != "ready":
                complete = False
                continue
            result["reporting_slots"] += 1
            for key in totals:
                totals[key] += row[key] or 0
            if row["usage_samples"] is not None:
                usage_reports += 1
            if row["tokens"] is not None:
                token_reports += 1
                for key in tokens:
                    tokens[key] += row["tokens"][key] or 0
                    if row["tokens"][key] is not None:
                        known_token_fields.add(key)
            complete = complete and row["token_state"] == "complete"
        if any(not network.counter(value) for value in (*totals.values(), *tokens.values())):
            return result
        if result["reporting_slots"]:
            for key in tokens:
                if key not in known_token_fields:
                    tokens[key] = None
            if not usage_reports:
                totals["usage_eligible_messages"] = totals["usage_samples"] = None
                totals["cache_read_samples"] = totals["cache_creation_samples"] = None
            result["api"] = {**_api_empty(), **totals, "state": "ready",
                             "tokens": tokens if token_reports else None,
                             "token_state": ("complete" if complete else "partial")
                                            if token_reports else "unknown"}
    except Exception:
        pass
    return result


class Observer:
    """Primary-only cached sampler. HTTP rendering never opens proc or host files."""

    def __init__(self, *, collector=network.collect, reader=network._read, clock=time.time):
        self._collector, self._reader, self._clock = collector, reader, clock
        self._lock = threading.Lock()
        self._history = []
        self._cached = {"host": network.empty("machine_nic"),
                        "service": network.empty("service_namespace")}

    def sample_once(self):
        now = self._clock()
        observation = self._collector(now=now, scope="service_namespace")
        self._history = [*self._history[-511:], observation]
        report = {"host": network.empty("machine_nic"),
                  "service": network.summarize(self._history, now, scope="service_namespace",
                                                max_gap_s=STALE_AFTER_S)}
        try:
            raw = json.loads(self._reader(HOST_SNAPSHOT, 262144))
            host = network.validate_snapshot(raw, now, max_age_s=STALE_AFTER_S)
            if host is not None:
                report["host"] = host
        except (OSError, ValueError, TypeError, UnicodeError):
            pass
        with self._lock:
            self._cached = report

    def snapshot(self, now):
        with self._lock:
            result = copy.deepcopy(self._cached)
        for key, scope in (("host", "machine_nic"), ("service", "service_namespace")):
            observed = result[key]["observed_at"]
            if observed is not None and (not network.number(now)
                    or not -network.FUTURE_SKEW_S <= now - observed <= STALE_AFTER_S):
                result[key] = network.empty(scope, "stale", observed)
        return result

    def run(self, stop):
        while not stop.is_set():
            try:
                self.sample_once()
            except Exception:
                with self._lock:
                    self._cached = {"host": network.empty("machine_nic"),
                                    "service": network.empty("service_namespace")}
            stop.wait(INTERVAL_S)
