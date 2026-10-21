"""Public masked-identity ranking using a common API reference for observed tokens.

Legacy token totals remain useful. Model labels/costs require new matched model
observations: a common reference conversion does not infer historical models.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
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
MODEL_COVERAGE_FIELDS = ("identified_model_samples", "unknown_model_samples",
                         "unrecorded_model_samples")
PRICING = {"date": model_usage.PRICING_DATE, "url": model_usage.PRICING_URL,
           "models_url": model_usage.MODELS_URL, "basis": "standard_global_api_equivalent",
           "cache_write_ttl_assumption": "5m", "haiku_5_5_tier": "unpriced",
           "historical_model_backfill": False, "subscription_charges": False}
REFERENCE_MODEL = "claude-opus-5-5"
REFERENCE_RATES = {"input_tokens": 4.0, "output_tokens": 20.0,
                   "cache_creation_input_tokens": 5.0, "cache_read_input_tokens": 0.20}
REFERENCE_PRICING = {"model": REFERENCE_MODEL, "date": "2026-10-20",
                     "url": model_usage.PRICING_URL, "models_url": model_usage.MODELS_URL,
                     "rates_usd_per_million": REFERENCE_RATES,
                     "cache_write_ttl_assumption": "5m", "basis": "common_reference_conversion"}


def _empty(start):
    return {"state": "no_data", "window_days": WINDOW_DAYS, "window_start_day": start,
            "eligible_slots": 0, "reporting_slots": 0, **dict.fromkeys(COUNTERS, 0),
            **dict.fromkeys(MODEL_COVERAGE_FIELDS, 0), "model_state": "unknown",
            "unpriced_samples": 0, "tokens": None, "token_state": "unknown",
            "priced_tokens": None, "priced_token_state": "unknown",
            "unpriced_tokens": None, "unpriced_token_state": "unknown",
            "reference_tokens": None, "reference_estimated_usd": None,
            "reference_cost_state": "unknown", "reference_model": REFERENCE_MODEL,
            "estimated_usd": None, "cost_state": "unknown", "entries": [],
            "omitted_entries": 0, "pricing": dict(PRICING),
            "reference_pricing": {**REFERENCE_PRICING,
                                  "rates_usd_per_million": dict(REFERENCE_RATES)}}


def _sum(values):
    value = sum(values)
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError("invalid public usage total")
    return value


def _masked_handle(value):
    if not names.valid_handle(value):
        return None
    return value[0] + "***" + value[-1] if len(value) >= 4 else "***"


def _masked_email(value):
    """Only a bounded, unquoted ASCII address can yield a public local-part fragment.

    Domains and complete local parts never leave this boundary. Unsupported,
    ambiguous or short addresses fall back to the existing nickname/member code.
    """
    if type(value) is not str or not 6 <= len(value) <= 254 or not value.isascii():
        return None
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if (not 4 <= len(local) <= 64
            or re.fullmatch(r"[A-Za-z0-9._%+-]+", local) is None
            or local.startswith(".") or local.endswith(".") or ".." in local):
        return None
    parts = domain.split(".")
    if (len(parts) < 2 or re.fullmatch(r"[A-Za-z]{2,63}", parts[-1]) is None
            or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                   is None for part in parts)):
        return None
    edge = 2 if len(local) >= 6 else 1
    return local[:edge] + "•••" + local[-edge:]


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
    identified = _sum(row["samples"] for row in models if row["model"] != "other")
    unknown = _sum((data["model_samples"], -identified))
    unrecorded = _sum((data["usage_samples"], -data["model_samples"]))
    model_state = ("unknown" if not identified else "complete"
                   if identified == data["usage_samples"] == data["usage_eligible_messages"]
                   else "partial")
    priced = [row for row in models if row["estimated_usd"] is not None]
    priced_samples = _sum(row["samples"] for row in priced)
    values = {key: data[key] for key in COUNTERS}
    values["priced_samples"] = priced_samples
    values["unpriced_samples"] = data["usage_samples"] - priced_samples
    tokens, token_state = _tokens(data, data["usage_samples"])
    if data["usage_samples"] < data["usage_eligible_messages"] and token_state == "complete":
        token_state = "partial"
    reference = (math.fsum(tokens[field] * rate for field, rate in REFERENCE_RATES.items()
                           if tokens[field] is not None) / 1_000_000
                 if tokens is not None else None)
    if reference is not None and not math.isfinite(reference):
        raise ValueError("invalid common API reference estimate")
    priced_tokens, priced_state, unpriced_tokens, unpriced_state = _token_partition(
        data, priced, token_state)
    estimate = math.fsum(row["estimated_usd"] for row in priced) if priced else None
    complete = (priced_samples == data["usage_samples"] == data["model_samples"]
                and data["usage_samples"] == data["usage_eligible_messages"]
                and data["model_samples"] == data["model_eligible_messages"]
                and all(row["cost_state"] == "complete" for row in priced))
    return {"rank": None, "masked_handle": _masked_handle(handle), **values,
            "identified_model_samples": identified, "unknown_model_samples": unknown,
            "unrecorded_model_samples": unrecorded, "model_state": model_state,
            "tokens": tokens, "token_state": token_state,
            "priced_tokens": priced_tokens, "priced_token_state": priced_state,
            "unpriced_tokens": unpriced_tokens, "unpriced_token_state": unpriced_state,
            "reference_tokens": dict(tokens) if tokens is not None else None,
            "reference_estimated_usd": reference, "reference_cost_state": token_state,
            "reference_model": REFERENCE_MODEL,
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
            accounts = {row["id"]: {"masked_handle": _masked_handle(row.get("handle")),
                                     "masked_email": _masked_email(row.get("email"))}
                        for row in store.list_accounts()
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
        for key in (*COUNTERS, *MODEL_COVERAGE_FIELDS, "model_state",
                    "unpriced_samples", "tokens", "token_state",
                    "priced_tokens", "priced_token_state", "unpriced_tokens",
                    "unpriced_token_state",
                    "reference_tokens", "reference_estimated_usd", "reference_cost_state",
                    "reference_model",
                    "estimated_usd", "cost_state"):
            result[key] = totals[key]
        entries = [{**_entry(value), **accounts.get(holder, {"masked_email": None}),
                    "member_code": _member_code(holder, getattr(cfg, "cookie_secret", None))}
                   for holder, value in groups.items()
                   if value["usage_eligible_messages"] or value["usage_samples"]]
        entries.sort(key=lambda row: (row["reference_estimated_usd"] is None,
                                     -(row["reference_estimated_usd"] or 0),
                                     -((row["tokens"] or {}).get("total_tokens") or 0),
                                     row["masked_handle"] or ""))
        previous, rank = None, None
        for index, entry in enumerate(entries, 1):
            cost = entry["reference_estimated_usd"]
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
