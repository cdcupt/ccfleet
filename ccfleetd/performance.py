"""Public, sample-gated numeric relay and matched stream observations.

Read each slot's latest seven-UTC-day snapshot once, never add successive
heartbeats or infer unmeasured gateway requests. No request path, credential,
native transcript usage or individual identity is returned. The caller must
keep reporting_users internal and hide coverage counts when state is not ready.
Output throughput has its own sample gate and never uses relay request timing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any, Optional

from ccfleet_agent.relay_metrics import DAY_SECONDS, MAX_EPOCH, TIMINGS, WINDOW_DAYS

from . import slots
from .heartbeat import account_fp, relay_report

MIN_REPORTING_USERS = 1
MIN_REQUESTS = 20
MIN_TIMING_SAMPLES = 20
MIN_THROUGHPUT_SAMPLES = 20
MAX_THROUGHPUT_TOTAL = 2**53 - 1
FUTURE_SKEW_S = 60


def _instant(value: Any) -> Optional[float]:
    if (type(value) in (int, float) and 0 <= value <= MAX_EPOCH
            and math.isfinite(value)):
        return float(value)
    return None


def _stream_totals(raw: Any) -> Optional[tuple[int, int, float]]:
    """Validate one independently measured source, without retaining raw fields."""
    if not isinstance(raw, Mapping):
        return None
    samples, tokens, elapsed = (raw.get(key) for key in
                                ("samples", "output_tokens", "stream_ms"))
    if (type(samples) is not int or type(tokens) is not int
            or not 0 <= samples <= MAX_THROUGHPUT_TOTAL
            or not 0 <= tokens <= MAX_THROUGHPUT_TOTAL
            or type(elapsed) not in (int, float) or not math.isfinite(elapsed)
            or not 0 <= elapsed <= MAX_THROUGHPUT_TOTAL):
        return None
    if samples == 0:
        return (0, 0, 0.0) if tokens == elapsed == 0 else None
    if (not samples * 100 <= elapsed <= samples * 3_600_000
            or tokens > samples * 1_000_000_000):
        return None
    return samples, tokens, float(elapsed)


def _gateway_totals(store: Any, now: float, start: str):
    """A corrupt/unsupported gateway source does not suppress valid native data."""
    try:
        measure = getattr(store, "gateway_throughput_summary", None)
        if not callable(measure):
            return None
        raw = measure(now)
        if not isinstance(raw, Mapping) or raw.get("window_start_day") != start:
            return None
        totals = _stream_totals(raw)
        if totals is None:
            return None
        collected = raw.get("collection_start_day")
        if collected is not None:
            if (not isinstance(collected, str) or date.fromisoformat(collected).isoformat()
                    != collected or collected < start
                    or collected > datetime.fromtimestamp(now, timezone.utc)
                    .date().isoformat()):
                return None
        if totals[0] and collected is None:
            return None
        return totals, collected
    except Exception:
        return None


def _throughput_summary(store: Any, now: float, start: str,
                        reports=()) -> dict[str, Any]:
    """Same-definition native and gateway sums; node gateway samples are excluded.

    Native reports do not carry the first sample's date, so any positive native
    contribution makes the combined collection_start_day unknown.
    """
    result = {"state": "no_data", "samples": None, "tokens_per_second": None,
              "window_start_day": start, "collection_start_day": None}
    try:
        gateway = _gateway_totals(store, now, start)
        sources = [gateway[0]] if gateway is not None else []
        collected = gateway[1] if gateway is not None else None
        for report in reports:
            native = _stream_totals(report.get("stream_output"))
            if native is not None:
                sources.append(native)
                if native[0]:
                    collected = None
        samples = sum(source[0] for source in sources)
        tokens = sum(source[1] for source in sources)
        elapsed = math.fsum(source[2] for source in sources)
        if _stream_totals({"samples": samples, "output_tokens": tokens,
                           "stream_ms": elapsed}) is None:
            return result
        result["collection_start_day"] = collected
        if not samples:
            return result
        result["state"] = "limited_data"
        if samples < MIN_THROUGHPUT_SAMPLES:
            return result
        rate = tokens / (elapsed / 1000)
        if not math.isfinite(rate):
            return {**result, "state": "no_data", "collection_start_day": None}
        result.update(state="ready", samples=samples, tokens_per_second=rate)
    except Exception:
        # Telemetry must never make Status fail or turn corrupt observations into zero.
        return {**result, "state": "no_data", "samples": None, "tokens_per_second": None,
                "collection_start_day": None}
    return result


def _current_report(slot: Mapping[str, Any], heartbeat: Any, node_id: str,
                    now: float, max_age: float) -> Optional[dict[str, Any]]:
    """Only a fresh report about the current slot/account assignment counts."""
    if not isinstance(heartbeat, Mapping):
        return None
    heard = _instant(heartbeat.get("ts"))
    if heard is None or not now - max_age <= heard <= now + FUTURE_SKEW_S:
        return None
    payload = heartbeat.get("payload")
    if (not isinstance(payload, Mapping) or payload.get("node_id") != node_id
            or payload.get("mode") != slots.MACHINE_MODE):
        return None
    entries = payload.get("slots")
    if not isinstance(entries, list):
        return None
    # The heartbeat boundary already keeps the first entry for each unix user.
    # Defend against a malformed archived snapshot without choosing an identity.
    matches = [entry for entry in entries if isinstance(entry, Mapping)
               and entry.get("unix_user") == slot.get("unix_user")]
    if len(matches) != 1:
        return None
    entry = matches[0]
    credentials = entry.get("credentials")
    if (entry.get("present") is not True or not isinstance(credentials, Mapping)
            or credentials.get("present") is False
            or credentials.get("logged_in") is not True):
        return None
    bound = account_fp(credentials.get("bound_fp"))
    actual = account_fp(credentials.get("account_fp"))
    if bound is None or len(bound) != 16 or actual != bound:
        return None
    report = relay_report(entry.get("relay"), now)
    if report is None:
        return None
    claimed = _instant(slot.get("claimed_at"))
    switched = _instant(slot.get("account_switched_at"))
    provisioned = _instant(entry.get("provisioned_for"))
    if (claimed is None or provisioned is None or provisioned != claimed
            or (slot.get("account_switched_at") is not None and switched is None)):
        return None
    observed = report["observed_at"]
    if (not now - max_age <= observed <= now + FUTURE_SKEW_S
            or observed < max(claimed, switched or 0)
            or int(observed // DAY_SECONDS) != int(now // DAY_SECONDS)):
        return None
    return report


def _active_slots(rows, nodes):
    """Do not attribute an ambiguous duplicate machine snapshot to either holder."""
    unique, ambiguous = {}, set()
    for slot in rows:
        if (not isinstance(slot, Mapping) or slot.get("kind") != slots.MACHINE_SLOT
                or slot.get("state") != slots.ACTIVE or slot.get("node_id") not in nodes
                or not isinstance(slot.get("held_by"), str) or not slot["held_by"]
                or not isinstance(slot.get("unix_user"), str) or not slot["unix_user"]):
            continue
        key = slot["node_id"], slot["unix_user"]
        if key in unique:
            ambiguous.add(key)
        unique[key] = slot
    return [row for key, row in unique.items() if key not in ambiguous]


def summary(store: Any, cfg: Any, now: float) -> dict[str, Any]:
    """Allowlisted aggregate; traffic values need enough measured attempts.

    completion_rate is a ratio from 0 to 1 of completed relay transfers to
    observed relay attempts. Timing means are weighted by each stage's sample
    count, including failures when that stage was measured. Each positive timing
    stage separately needs twenty samples, even from a single holder. Zero
    timing samples have count=0 and mean=None when ready; gated stages have both
    values None, including positive stages with too few measurements.
    A single holder can contribute the entire public aggregate; identities and
    per-holder or per-slot breakdowns are never returned. Eligible slots are
    enabled, currently held active hosted slots, regardless
    of whether they support metrics. Reporting counts are internal until ready.
    """
    observed_now = _instant(now)
    max_age = _instant(cfg.heartbeat_max_age_s)
    if observed_now is None or max_age is None:
        raise ValueError("invalid performance observation clock")
    first_day = max(0, int(observed_now // DAY_SECONDS) - WINDOW_DAYS + 1)
    start = datetime.fromtimestamp(first_day * DAY_SECONDS, timezone.utc).date().isoformat()
    result: dict[str, Any] = {
        "state": "no_data", "window_days": WINDOW_DAYS, "window_start_day": start,
        "eligible_slots": 0, "reporting_slots": 0, "reporting_users": 0,
        "oldest_observed_at": None, "requests": None, "completed": None,
        "completion_rate": None,
        "latencies": {stage: {"count": None, "mean": None} for stage in TIMINGS},
        "throughput": None,
    }
    nodes = {node["id"] for node in store.list_nodes()
             if isinstance(node, Mapping) and isinstance(node.get("id"), str)
             and type(node.get("enabled")) in (bool, int) and node["enabled"] == 1}
    latest = store.latest_heartbeats()
    reports: list[dict[str, Any]] = []
    contributors: set[str] = set()
    stage_contributors: dict[str, set[str]] = {stage: set() for stage in TIMINGS}
    for slot in _active_slots(store.list_slots(), nodes):
        result["eligible_slots"] += 1
        report = _current_report(slot, latest.get(slot["node_id"]), slot["node_id"],
                                 observed_now, max_age)
        if report is None:
            continue
        reports.append(report)
        if report["requests"]:
            contributors.add(slot["held_by"])
        for stage in TIMINGS:
            if report["latencies"][stage]["count"]:
                stage_contributors[stage].add(slot["held_by"])
    result["reporting_slots"] = len(reports)
    result["reporting_users"] = len(contributors)
    result["throughput"] = _throughput_summary(store, observed_now, start, reports)
    requests = sum(report["requests"] for report in reports)
    if not requests:
        return result
    if len(contributors) < MIN_REPORTING_USERS or requests < MIN_REQUESTS:
        result["state"] = "limited_data"
        return result
    completed = sum(report["success"] for report in reports)
    result.update(state="ready", requests=requests, completed=completed,
                  completion_rate=completed / requests,
                  oldest_observed_at=min(report["observed_at"] for report in reports))
    for stage in TIMINGS:
        count = sum(report["latencies"][stage]["count"] for report in reports)
        if count and (len(stage_contributors[stage]) < MIN_REPORTING_USERS
                      or count < MIN_TIMING_SAMPLES):
            continue
        weighted = math.fsum(report["latencies"][stage]["mean"]
                             * report["latencies"][stage]["count"] for report in reports)
        result["latencies"][stage] = {"count": count, "mean": weighted / count if count else None}
    return result
