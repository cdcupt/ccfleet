#!/usr/bin/env python3
"""Sample connectivity on the affected computer using the installed CCFleet client."""

import argparse
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FLEET_HEALTH = "https://ccfleet.io/healthz"
MILESTONES = ("dns_done_ms", "tcp_connected_ms", "tls_ready_ms", "first_byte_ms", "total_ms")
CURL_FORMAT = "\n%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} " \
              "%{time_starttransfer} %{time_total}"
CURL_ERRORS = {5: "proxy_dns_failed", 6: "dns_failed", 7: "connect_failed", 28: "timeout",
               35: "tls_failed", 60: "certificate_failed", 77: "certificate_store_failed",
               56: "receive_failed", 52: "empty_reply", 63: "unexpected_response"}
ROUTE_CODES = frozenset("""
gateway_ready gateway_unavailable pairing_ready pairing_missing pairing_invalid
key_ready key_invalid pin_ready pin_invalid device_ready device_revoked device_forbidden
device_invalid device_unchecked device_connection slot_report_ready slot_report_degraded
slot_report_pending relay_ready relay_unchecked relay_connection relay_transition relay_disabled
relay_renewal compatibility_passed compatibility_pending compatibility_failed compatibility_blocked
compatibility_operator_review compatibility_unverified
""".split())
OUTCOMES = ("success", "connection_errors", "rate_limits", "upstream_errors", "auth_errors",
            "permission_errors", "input_errors", "cancelled")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_probe(command, timeout, data=b""):
    # Keep terminal input, stderr and diagnostics from subprocesses out of the report.
    # A timed-out status command can have a broker or SSH child.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True) as proc:
        try:
            output, _ = proc.communicate(data, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        return proc.returncode, output


def public_probe():
    command = ["curl", "-q", "--silent", "--proto", "=https", "--connect-timeout", "4",
               "--max-time", "8", "--max-filesize", "4096", "--write-out", CURL_FORMAT,
               FLEET_HEALTH]
    try:
        rc, output = run_probe(command, 10)
    except subprocess.TimeoutExpired:
        return {"state": "timeout"}
    except OSError:
        return {"state": "curl_unavailable"}
    body, _, trailer = output.rpartition(b"\n")
    state = CURL_ERRORS.get(rc, "transfer_failed") if rc else "unexpected_response"
    result = {"state": state}
    try:
        status, *times = trailer.decode("ascii").split()
        if not re.fullmatch(r"\d{3}", status) or len(times) != len(MILESTONES):
            return result
        for name, raw in zip(MILESTONES, times):
            seconds = float(raw)
            if math.isfinite(seconds) and 0 <= seconds <= 30:
                result[name] = round(seconds * 1000, 1) if seconds else None
        if status != "000":
            result["http_status"] = int(status)
        if rc == 0 and status != "200":
            result["state"] = "http_error"
        elif rc == 0 and len(body) <= 4096:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("ok") is True:
                result["state"] = "ready"
    except (ValueError, RecursionError):
        pass
    return result


def route_probe(client, slot):
    if not client:
        return {"state": "cli_missing"}
    args = [client, "status", "--json"] + (["--slot", slot] if slot else [])
    try:
        rc, raw = run_probe(args, 45)
        if rc not in (0, 1) or len(raw) > 64 * 1024:
            return {"state": "status_unavailable"}
        report = json.loads(raw)
        if (not isinstance(report, dict) or report.get("kind") != "status"
                or report.get("schema") != 1 or not isinstance(report.get("checks"), list)
                or type(report.get("ok")) is not bool
                or report.get("transport") not in (None, "gateway")):
            return {"state": "unsupported_status"}
        checks = []
        for item in report["checks"]:
            code = item.get("code") if isinstance(item, dict) else None
            if not isinstance(code, str) or code not in ROUTE_CODES:
                return {"state": "unsupported_status"}
            checks.append(code)
        gateway = report.get("transport") == "gateway"
        transport = "gateway" if gateway else "paired_relay"
        if "pairing_missing" in checks or "pairing_invalid" in checks:
            transport = "unresolved"
        ready = "gateway_ready" if gateway else "relay_ready"
        if not report["ok"] or rc != 0:
            state = "failed"
        elif ready not in checks:
            state = "unverified"
        else:
            state = "ready"
        result = {"state": state, "transport": transport, "checks": checks}
        facts = report.get("gateway")
        if gateway and isinstance(facts, dict):
            selected, eligible = facts.get("selected_count"), facts.get("eligible_count")
            if (type(selected) is int and type(eligible) is int and 0 <= eligible <= selected <= 100
                    and facts.get("routing") in ("sticky", "priority")):
                result["gateway"] = {"routing": facts["routing"], "selected": selected,
                                     "eligible": eligible}
        return result
    except subprocess.TimeoutExpired:
        return {"state": "status_timeout"}
    except (OSError, ValueError, RecursionError):
        return {"state": "status_unavailable"}


def slot_history(client, slot):
    """Use the installed client's read-only management interface for this explicit pairing."""
    missing = {"available": False}
    if not client:
        return missing
    messages = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
         "capabilities": {}, "clientInfo": {"name": "ccfleet-diagnose", "version": "1"}}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/call", "params": {"name": "ccfleet_relay_usage"}},
    ]
    data = b"".join(json.dumps({"jsonrpc": "2.0", **m}).encode() + b"\n" for m in messages)
    try:
        rc, raw = run_probe([client, "mcp", "serve", "--slot", slot], 20, data)
        if rc != 0 or len(raw) > 64 * 1024:
            return missing
        for line in raw.splitlines():
            reply = json.loads(line)
            if reply["id"] != 2:
                continue
            result = reply["result"]
            view = result["structuredContent"]
            if (result["isError"] is not False or view["available"] is not True
                    or view["source"] != "cached_relay_aggregates"
                    or view["window_days"] != 7 or view["window_kind"] != "utc_calendar_days"
                    or view["freshness"] not in ("fresh", "stale")):
                return missing
            counts = {k: view["counts"][k] for k in ("requests", *OUTCOMES)}
            age = view["age_seconds"]
            if (any(type(v) is not int or not 0 <= v < 2**53 for v in counts.values())
                    or counts["requests"] != sum(counts[k] for k in OUTCOMES)
                    or type(age) is not int or age < 0):
                return missing
            timing = view["latencies"]["first_byte_ms"]
            n = timing["samples"]
            if type(n) is not int or not 0 <= n <= counts["requests"]:
                return missing
            clean = {"samples": n}
            if n:
                for k in ("mean_ms", "max_ms"):
                    value = timing[k]
                    if type(value) not in (int, float) or not 0 <= value <= 3600000:
                        return missing
                    clean[k] = round(value, 1)
            return {"available": True, "window": "seven_utc_calendar_days",
                    "freshness": view["freshness"], "age_seconds": age,
                    "counts": counts, "first_byte": clean}
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError, RecursionError):
        pass
    return missing


def timed(probe, *args):
    at = utc_now()
    start = time.monotonic()
    result = probe(*args)
    return {"at_utc": at, "elapsed_ms": round((time.monotonic() - start) * 1000), **result}


def write_report(report, destination):
    # Publish only a complete, private file. A previous report is never replaced.
    with tempfile.NamedTemporaryFile(dir=destination.parent, mode="w", encoding="utf-8") as tmp:
        json.dump(report, tmp, indent=2, allow_nan=False)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        os.link(tmp.name, destination)


def bounded_int(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"choose {minimum} to {maximum}")
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", help="select a Claude slot by its paired name or ID")
    parser.add_argument("--samples", type=bounded_int(1, 30), default=5)
    parser.add_argument("--interval", type=bounded_int(1, 60), default=5,
                        help="seconds between completed samples (default 5)")
    parser.add_argument("--symptom",
                        choices=("slow-start", "stream-stalled", "disconnect", "other"))
    parser.add_argument("--output", type=Path,
                        help="new private JSON file (default: current folder)")
    args = parser.parse_args(argv)
    destination = args.output or Path(datetime.now(timezone.utc).strftime(
        "ccfleet-connection-%Y%m%dT%H%M%S%fZ.json"))
    if destination.exists() or destination.is_symlink():
        parser.error("choose a new filename because the report file already exists")
    client = shutil.which("ccfleet")
    report = {"schema": 1, "scope": "ccfleet_claude_connection", "started_utc": utc_now(),
              "public_endpoint": FLEET_HEALTH, "model_requests": 0, "samples": [],
              "requested_samples": args.samples, "interval_seconds": args.interval,
              "selection": "explicit_slot" if args.slot else "client_default"}
    if args.slot and re.fullmatch(r"slot-[0-9]{4}", args.slot):
        report["slot"] = args.slot
    if args.symptom:
        report["symptom"] = args.symptom
    interrupted = False
    print("Checking Fleet HTTPS and your configured Claude route. Press Ctrl-C to save early.",
          flush=True)
    try:
        for i in range(args.samples):
            row = {}
            report["samples"].append(row)
            row["public_https"] = timed(public_probe)
            row["configured_route"] = timed(route_probe, client, args.slot)
            print(f"{i+1}/{args.samples}  {row['public_https']['at_utc']}  "
                  f"Fleet HTTPS {row['public_https']['state']}  "
                  f"Claude route {row['configured_route']['state']} "
                  f"({row['configured_route']['elapsed_ms']} ms)", flush=True)
            if i + 1 < args.samples:
                time.sleep(args.interval)
        if args.slot:
            routes = [r["configured_route"] for r in report["samples"]]
            # Gateway routing resolves live names ahead of old saved pairings. MCP
            # addresses a local pairing. Only join their evidence on the bridge route.
            if all(r.get("transport") == "paired_relay" and "pairing_ready" in r["checks"]
                   for r in routes):
                report["named_pairing_history"] = slot_history(client, args.slot)
            else:
                report["named_pairing_history"] = {"available": False,
                                                   "reason": "pairing_not_verified_for_route"}
    except KeyboardInterrupt:
        interrupted = True
    report["interrupted"] = interrupted
    report["finished_utc"] = utc_now()
    summary = {}
    for key, label in (("public_https", "Fleet HTTPS"), ("configured_route", "Claude route")):
        observations = [row[key] for row in report["samples"] if key in row]
        states = Counter(o["state"] for o in observations)
        times = [o["elapsed_ms"] for o in observations if o["state"] == "ready"]
        summary[key] = {"states": dict(states), "completed": len(observations),
                        "ready_median_ms": statistics.median(times) if times else None,
                        "ready_max_ms": max(times) if times else None}
        print(f"{label} ready in {states['ready']}/{len(observations)} completed checks.")
    public = summary["public_https"]
    route = summary["configured_route"]
    if not public["completed"] or not route["completed"]:
        print("The sample window ended before both checks completed.")
    elif public["states"].get("ready", 0) < public["completed"]:
        print("At least one public HTTPS check did not complete successfully. "
              "The report includes its last measured connection stage.")
    elif route["states"].get("ready", 0) < route["completed"]:
        print("Fleet HTTPS responded throughout. The configured route has a failed or "
              "incomplete check. Support can inspect the pairing, gateway or relay codes.")
    else:
        print("Both checks passed during this window. For a slow reply or stopped stream, "
              "support can match these UTC times to slot request logs.")
    report["summary"] = summary
    try:
        write_report(report, destination)
    except OSError:
        print("Could not save the report. Choose a new filename in a writable folder.",
              file=sys.stderr)
        return 2
    print(f"Saved {destination}. Share this file with support. "
          "Model replies and streaming were not tested.")
    if interrupted:
        return 130
    return int(any(s["states"].get("ready", 0) != args.samples for s in summary.values()))


if __name__ == "__main__":
    raise SystemExit(main())
