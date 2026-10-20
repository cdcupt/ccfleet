"""Public masked-handle ranking of observed tokens and API-equivalent estimates.

Legacy token totals remain useful. Model labels/costs require new matched model
observations: never distribute historical tokens across an inferred model mix.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ccfleet_agent import model_usage
from ccfleet_agent.relay_metrics import DAY_SECONDS, MAX_COUNTER, WINDOW_DAYS

from . import names, performance

MAX_ENTRIES = 50
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens")
COUNTERS = ("usage_eligible_messages", "usage_samples", "model_eligible_messages",
            "model_samples", "priced_samples", "cache_read_samples", "cache_creation_samples")
PRICING = {"date": model_usage.PRICING_DATE, "url": model_usage.PRICING_URL,
           "models_url": model_usage.MODELS_URL, "basis": "standard_global_api_equivalent",
           "cache_write_ttl_assumption": "5m", "haiku_5_5_tier": "unpriced",
           "historical_model_backfill": False, "subscription_charges": False}


def _empty(start):
    return {"state": "no_data", "window_days": WINDOW_DAYS, "window_start_day": start,
            "eligible_slots": 0, "reporting_slots": 0, **dict.fromkeys(COUNTERS, 0),
            "unpriced_samples": 0, "tokens": None, "token_state": "unknown",
            "priced_tokens": None, "priced_token_state": "unknown",
            "unpriced_tokens": None, "unpriced_token_state": "unknown",
            "estimated_usd": None, "cost_state": "unknown", "entries": [],
            "omitted_entries": 0, "pricing": dict(PRICING)}


def _sum(values):
    value = sum(values)
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError("invalid public usage total")
    return value


def _masked_handle(value):
    if not names.valid_handle(value):
        return None
    return value[0] + "***" + value[-1] if len(value) >= 4 else "***"


def _member_code(holder, secret):
    """A purpose-separated public pseudonym, never an email or raw account ID."""
    if not isinstance(holder, str) or not isinstance(secret, str) or not secret:
        return None
    digest = hmac.new(secret.encode(), b"public-usage-member-v1\0" + holder.encode(),
                      hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 100_000_000:08d}"


def _bucket():
    return {**dict.fromkeys(COUNTERS, 0), **dict.fromkeys(TOKEN_FIELDS, 0), "models": {}}


def _add(target, report):
    usage = report["token_usage"]
    for key, field in (("usage_eligible_messages", "eligible"), ("usage_samples", "samples"),
                       ("cache_read_samples", "cache_read_samples"),
                       ("cache_creation_samples", "cache_creation_samples")):
        target[key] = _sum((target[key], usage[field]))
    for key in TOKEN_FIELDS:
        target[key] = _sum((target[key], usage[key]))
    labeled = report.get("model_usage")
    if labeled is None:
        return
    target["model_eligible_messages"] = _sum((target["model_eligible_messages"],
                                             labeled["eligible"]))
    target["model_samples"] = _sum((target["model_samples"], labeled["samples"]))
    for row in labeled["models"]:
        key = row["model"]
        if key not in model_usage.PUBLIC_MODELS:
            raise ValueError("invalid public model key")
        saved = target["models"].setdefault(key, dict.fromkeys(
            ("samples", "cache_read_samples", "cache_creation_samples", *TOKEN_FIELDS), 0))
        for field in saved:
            saved[field] = _sum((saved[field], row[field]))


def _tokens(data, samples):
    if not samples:
        return None, "unknown"
    complete = (data["cache_read_samples"] == samples
                and data["cache_creation_samples"] == samples)
    result = {"input_tokens": data["input_tokens"], "output_tokens": data["output_tokens"],
              "cache_read_input_tokens": (data["cache_read_input_tokens"]
                                          if data["cache_read_samples"] else None),
              "cache_creation_input_tokens": (data["cache_creation_input_tokens"]
                                              if data["cache_creation_samples"] else None),
              "total_tokens": _sum(data[field] for field in TOKEN_FIELDS)}
    return result, "complete" if complete else "partial"


def _model_row(key, data):
    tokens, token_state = _tokens(data, data["samples"])
    price = model_usage.PRICES[key]
    estimate = (math.fsum((data["input_tokens"] * price[0],
                          data["output_tokens"] * price[1],
                          data["cache_creation_input_tokens"] * price[2],
                          data["cache_read_input_tokens"] * price[4])) / 1_000_000
                if price is not None and data["samples"] else None)
    if estimate is not None and not math.isfinite(estimate):
        raise ValueError("invalid API-equivalent estimate")
    return {"model": key, "label": model_usage.LABELS[key], "samples": data["samples"],
            "cache_read_samples": data["cache_read_samples"],
            "cache_creation_samples": data["cache_creation_samples"],
            "tokens": tokens, "token_state": token_state, "estimated_usd": estimate,
            "cost_state": (token_state if estimate is not None else "unknown")}


def _token_partition(data, priced, token_state):
    """Partition observed categories using exactly the rows in the USD estimate.

    Cache reads/writes are separate provider categories, not part of uncached
    input_tokens. Missing categories contribute no invented tokens or cost.
    The remainder is an observed subtotal, not inferred usage of unpriced models.
    """
    known = [data["models"][row["model"]] for row in priced]
    covered = {key: _sum(row[key] for row in known)
               for key in (*TOKEN_FIELDS, "cache_read_samples", "cache_creation_samples")}
    samples = _sum(row["samples"] for row in known)
    priced_tokens, priced_state = _tokens(covered, samples)
    if not data["usage_samples"]:
        return priced_tokens, priced_state, None, "unknown"
    remainder = {key: _sum((data[key], -covered[key])) for key in TOKEN_FIELDS}
    unpriced_tokens = {"input_tokens": remainder["input_tokens"],
                       "output_tokens": remainder["output_tokens"],
                       "cache_read_input_tokens": (remainder["cache_read_input_tokens"]
                                                   if data["cache_read_samples"] else None),
                       "cache_creation_input_tokens": (remainder["cache_creation_input_tokens"]
                                                       if data["cache_creation_samples"] else None),
                       "total_tokens": _sum(remainder.values())}
    return priced_tokens, priced_state, unpriced_tokens, token_state


def _entry(data, handle=None):
    models = [_model_row(key, row) for key, row in sorted(data["models"].items())]
    priced = [row for row in models if row["estimated_usd"] is not None]
    priced_samples = _sum(row["samples"] for row in priced)
    values = {key: data[key] for key in COUNTERS}
    values["priced_samples"] = priced_samples
    values["unpriced_samples"] = data["usage_samples"] - priced_samples
    tokens, token_state = _tokens(data, data["usage_samples"])
    if data["usage_samples"] < data["usage_eligible_messages"] and token_state == "complete":
        token_state = "partial"
    priced_tokens, priced_state, unpriced_tokens, unpriced_state = _token_partition(
        data, priced, token_state)
    estimate = math.fsum(row["estimated_usd"] for row in priced) if priced else None
    complete = (priced_samples == data["usage_samples"] == data["model_samples"]
                and data["usage_samples"] == data["usage_eligible_messages"]
                and data["model_samples"] == data["model_eligible_messages"]
                and all(row["cost_state"] == "complete" for row in priced))
    return {"rank": None, "masked_handle": _masked_handle(handle), **values,
            "tokens": tokens, "token_state": token_state,
            "priced_tokens": priced_tokens, "priced_token_state": priced_state,
            "unpriced_tokens": unpriced_tokens, "unpriced_token_state": unpriced_state,
            "estimated_usd": estimate,
            "cost_state": ("complete" if complete else "partial") if estimate is not None
                          else "unknown", "models": models}


def public_summary(store: Any, cfg: Any, now: float) -> dict[str, Any]:
    """Telemetry failures cannot break the homepage or disclose stored raw values."""
    start = None
    try:
        instant = performance._instant(now)
        age = performance._instant(cfg.heartbeat_max_age_s)
        if instant is None or age is None:
            return _empty(None)
        first = max(0, int(instant // DAY_SECONDS) - WINDOW_DAYS + 1)
        start = datetime.fromtimestamp(first * DAY_SECONDS, timezone.utc).date().isoformat()
        result = _empty(start)
        nodes = {node["id"] for node in store.list_nodes()
                 if isinstance(node, Mapping) and isinstance(node.get("id"), str)
                 and type(node.get("enabled")) in (int, bool) and node["enabled"] == 1}
        latest = store.latest_heartbeats()
        try:
            accounts = {row["id"]: row.get("handle") for row in store.list_accounts()
                        if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
        except Exception:
            accounts = {}
        groups, all_data = {}, _bucket()
        for slot in performance._active_slots(store.list_slots(), nodes):
            result["eligible_slots"] += 1
            report = performance._current_report(slot, latest.get(slot["node_id"]), slot["node_id"],
                                                 instant, age)
            if report is None or "token_usage" not in report:
                continue
            result["reporting_slots"] += 1
            group = groups.setdefault(slot["held_by"], _bucket())
            _add(group, report)
            _add(all_data, report)
        totals = _entry(all_data)
        for key in (*COUNTERS, "unpriced_samples", "tokens", "token_state",
                    "priced_tokens", "priced_token_state", "unpriced_tokens",
                    "unpriced_token_state",
                    "estimated_usd", "cost_state"):
            result[key] = totals[key]
        entries = [{**_entry(value, accounts.get(holder)),
                    "member_code": _member_code(holder, getattr(cfg, "cookie_secret", None))}
                   for holder, value in groups.items()
                   if value["usage_eligible_messages"] or value["usage_samples"]]
        entries.sort(key=lambda row: (row["estimated_usd"] is None,
                                     -(row["estimated_usd"] or 0),
                                     -((row["tokens"] or {}).get("total_tokens") or 0),
                                     row["masked_handle"] or ""))
        previous, rank = None, None
        for index, entry in enumerate(entries, 1):
            cost = entry["estimated_usd"]
            if cost is not None:
                if previous != cost:
                    rank = index
                entry["rank"] = rank
                previous = cost
        result["entries"] = entries[:MAX_ENTRIES]
        result["omitted_entries"] = max(0, len(entries) - MAX_ENTRIES)
        if result["usage_samples"]:
            result["state"] = ("ready" if result["cost_state"] == "complete"
                               and result["eligible_slots"] == result["reporting_slots"]
                               else "partial")
        return result
    except Exception:
        return _empty(start)
