#!/usr/bin/env python3
"""Read-only Claude Code wait diagnostic. Python 3; standard library only.

Reads recent conversation JSONL files locally, but emits ONLY selected metadata:
timestamps, tool names, numeric usage, model IDs, and process names/statistics.
Never emits prompts, responses, tool arguments/results, process arguments,
credentials, settings, environment variables, full paths, or raw exceptions.
Makes no network requests and does not change files, processes, or configuration.

Run: python3 claude-wait-diagnostic.py
Optional: --session SESSION_ID --minutes 120 --max-files 12 --tail-mib 10
Redirect stdout yourself if you want to save its JSON report.
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
SAFE_LABEL = re.compile(r"[A-Za-z0-9_.:/-]{1,120}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def label(value):
    if (
        not isinstance(value, str)
        or not SAFE_LABEL.fullmatch(value)
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:/", value)
    ):
        return "unknown"
    return value


def number(value):
    return (
        value
        if type(value) in (int, float) and 0 <= value <= 2**53 and math.isfinite(value)
        else None
    )


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def iso(value):
    return (
        dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat() if value is not None else None
    )


def age(now, value):
    return round(max(0, now - value), 3) if value is not None else None


def discover(root, session, cutoff, max_files):
    candidates, scanned, errors = [], 0, 0
    cap = 100000

    def entries(path):
        nonlocal scanned, errors
        try:
            with os.scandir(path) as items:
                for entry in items:
                    if scanned >= cap:
                        break
                    scanned += 1
                    if not entry.is_symlink():
                        yield entry
        except OSError:
            errors += 1

    def add(entry, project, session_id, kind):
        nonlocal errors
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= cutoff:
                candidates.append((info.st_mtime, entry.path, project, session_id, kind))
        except OSError:
            errors += 1

    # Only projects/<project>/*.jsonl and <session>/subagents/*.jsonl.
    for project in entries(root):
        if not project.is_dir(follow_symlinks=False):
            continue
        project_id = hashlib.sha256(project.name.encode()).hexdigest()[:12]
        for entry in entries(project.path):
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                stem = entry.name[:-6]
                if session is None or stem == session:
                    add(
                        entry, project_id, stem, "subagent" if stem.startswith("agent-") else "main"
                    )
            elif entry.is_dir(follow_symlinks=False) and (session is None or entry.name == session):
                subdir = Path(entry.path) / "subagents"
                if subdir.is_symlink() or not subdir.is_dir():
                    continue
                for sub in entries(subdir):
                    if sub.name.startswith("agent-") and sub.name.endswith(".jsonl"):
                        add(sub, project_id, entry.name, "subagent")
    candidates.sort(key=lambda row: row[0], reverse=True)
    return candidates[:max_files], {
        "eligible_recent_files": len(candidates),
        "selected_files": min(len(candidates), max_files),
        "scan_entries": scanned,
        "scan_limit_reached": scanned >= cap,
        "directory_read_errors": errors,
        "files_omitted_by_limit": max(0, len(candidates) - max_files),
    }


def analyze_file(candidate, limit, now, cutoff):
    modified, path, project, session, kind = candidate
    report = {
        "kind": kind,
        "project_id_hash": project,
        "session_id": label(session),
        "file_id": label(Path(path).stem),
        "file_modified_age_seconds": age(now, modified),
    }
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return {**report, "error": "not_regular_file"}
            start = max(0, info.st_size - limit)
            previous = b"\n"
            if start:
                stream.seek(start - 1)
                previous = stream.read(1)
            stream.seek(start)
            data = stream.read(min(limit, info.st_size))
        partial_first = start > 0 and previous != b"\n"
        if partial_first:
            data = data.partition(b"\n")[2]
    except OSError as exc:
        return {**report, "error": type(exc).__name__}

    times, recent, uses, results, responses = [], [], {}, {}, {}
    counts, tool_counts, models = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    malformed = missing_time = unkeyed_usage = 0
    last_assistant = last_result = None
    for index, raw in enumerate(data.splitlines()):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError, UnicodeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        stamp = timestamp(event.get("timestamp"))
        if stamp is not None:
            times.append(stamp)
        else:
            missing_time += 1
        typ = event.get("type")
        typ = (
            typ
            if isinstance(typ, str)
            and typ in {"assistant", "user", "system", "progress", "summary"}
            else "other"
        )
        counts[typ] += 1
        if event.get("isApiErrorMessage") is True:
            counts["api_error_messages"] += 1
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        if typ == "assistant":
            last_assistant = max(last_assistant or 0, stamp or 0) or None
            usage = message.get("usage")
            if isinstance(usage, dict) and event.get("isUnmetered") is not True:
                key = message.get("id")
                if not isinstance(key, str) or not key or len(key) > 256:
                    key = "unkeyed-event-" + str(index)
                    unkeyed_usage += 1
                response = responses.setdefault(
                    key, {"timestamp": stamp, "model": label(message.get("model")), "usage": {}}
                )
                if stamp is not None:
                    response["timestamp"] = max(response["timestamp"] or 0, stamp)
                for field in USAGE_FIELDS:
                    value = number(usage.get(field))
                    if value is not None:
                        response["usage"][field] = max(response["usage"].get(field, 0), value)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                key = block.get("id") if block_type == "tool_use" else block.get("tool_use_id")
                if not isinstance(key, str) or not key or len(key) > 256:
                    continue
                if typ == "assistant" and block_type == "tool_use" and key not in uses:
                    tool = label(block.get("name"))
                    uses[key] = {"tool": tool, "timestamp": stamp}
                    tool_counts[tool] += 1
                    if stamp is not None and stamp >= cutoff:
                        recent.append({"at": iso(stamp), "kind": "tool_use", "tool": tool})
                elif typ == "user" and block_type == "tool_result" and key not in results:
                    results[key] = {"timestamp": stamp, "is_error": block.get("is_error") is True}
                    last_result = max(last_result or 0, stamp or 0) or None
        if stamp is not None and stamp >= cutoff and typ in {"assistant", "system"}:
            item = {"at": iso(stamp), "kind": typ}
            if typ == "assistant":
                item["model"] = label(message.get("model"))
            else:
                subtype = event.get("subtype")
                if isinstance(subtype, str) and subtype in {
                    "turn_duration",
                    "api_error",
                    "compact_boundary",
                    "stop_hook_summary",
                    "local_command",
                }:
                    item["subtype"] = subtype
                for key in (
                    "durationMs",
                    "duration_ms",
                    "statusCode",
                    "status_code",
                    "retryAttempt",
                    "retryInMs",
                ):
                    value = number(event.get(key))
                    if value is not None:
                        item[key] = value
            recent.append(item)

    pending, completed = [], []
    for key, use in uses.items():
        result = results.get(key)
        if result is None:
            pending.append(
                {
                    "tool": use["tool"],
                    "started_at": iso(use["timestamp"]),
                    "age_seconds": age(now, use["timestamp"]),
                }
            )
        else:
            begin, end = use["timestamp"], result["timestamp"]
            completed.append(
                {
                    "tool": use["tool"],
                    "started_at": iso(begin),
                    "result_at": iso(end),
                    "observed_duration_seconds": round(end - begin, 3)
                    if begin is not None and end is not None and end >= begin
                    else None,
                    "is_error": result["is_error"],
                }
            )
            if end is not None and end >= cutoff:
                recent.append(
                    {
                        "at": iso(end),
                        "kind": "tool_result",
                        "tool": use["tool"],
                        "is_error": result["is_error"],
                    }
                )
    ordered_times = sorted(set(t for t in times if t >= cutoff))
    gaps = [
        {"from": iso(a), "to": iso(b), "seconds": round(b - a, 3)}
        for a, b in zip(ordered_times, ordered_times[1:])
        if b - a >= 5
    ]
    totals = {
        field: sum(r["usage"].get(field, 0) for r in responses.values()) for field in USAGE_FIELDS
    }
    coverage = {
        field: sum(field in r["usage"] for r in responses.values()) for field in USAGE_FIELDS
    }
    for response in responses.values():
        models[response["model"]] += 1
    latest = max(responses.values(), key=lambda r: r["timestamp"] or 0, default=None)
    latest_usage = (
        None
        if latest is None
        else {"at": iso(latest["timestamp"]), "model": latest["model"], **latest["usage"]}
    )
    report.update(
        {
            "file_size_bytes": info.st_size,
            "tail_bytes_parsed": len(data),
            "tail_truncated": start > 0,
            "partial_first_line_discarded": partial_first,
            "malformed_or_incomplete_lines_skipped": malformed,
            "events_missing_timestamp": missing_time,
            "first_observed_event_at": iso(min(times)) if times else None,
            "last_observed_event_at": iso(max(times)) if times else None,
            "observed_event_span_seconds": round(max(times) - min(times), 3) if times else None,
            "last_event_age_seconds": age(now, max(times)) if times else None,
            "last_assistant_age_seconds": age(now, last_assistant),
            "last_tool_result_age_seconds": age(now, last_result),
            "tail_event_counts": dict(counts),
            "tail_unique_tool_use_count": len(uses),
            "tail_tool_name_counts": dict(tool_counts),
            "tail_tool_result_count": len(results),
            "tail_tool_result_errors": sum(r["is_error"] for r in results.values()),
            "tail_tool_results_without_matching_start": sum(key not in uses for key in results),
            "pending_tool_candidates": sorted(
                pending, key=lambda r: r["age_seconds"] or 0, reverse=True
            )[:12],
            "pending_tool_candidates_total": len(pending),
            "longest_observed_tool_pairs": sorted(
                completed, key=lambda r: r["observed_duration_seconds"] or 0, reverse=True
            )[:10],
            "recent_completed_tool_pairs": sorted(
                completed, key=lambda r: r["result_at"] or "", reverse=True
            )[:10],
            "recent_metadata_events": sorted(recent, key=lambda r: r["at"])[-10:],
            "largest_event_gaps_in_recent_window": sorted(
                gaps, key=lambda r: r["seconds"], reverse=True
            )[:5],
            "tail_usage_unique_response_count": len(responses),
            "tail_usage_unkeyed_events": unkeyed_usage,
            "tail_usage_totals_deduplicated": totals,
            "tail_usage_field_sample_counts": coverage,
            "tail_model_response_counts": dict(models),
            "latest_response_usage": latest_usage,
        }
    )
    return report


def process_snapshot():
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,pcpu=,pmem=,stat=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            text=True,
        )
        if result.returncode:
            return {"error": "ps_failed"}
    except (OSError, subprocess.SubprocessError):
        return {"error": "ps_unavailable"}
    rows, roots = {}, set()
    for line in result.stdout.splitlines()[:20000]:
        parts = line.split(None, 6)
        if len(parts) != 7:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            cpu, memory = float(parts[3]), float(parts[4])
        except ValueError:
            continue
        name = (
            os.path.basename(parts[6]).removesuffix(" (deleted)")
            if sys.version_info >= (3, 9)
            else os.path.basename(parts[6]).replace(" (deleted)", "")
        )
        rows[pid] = {
            "pid": pid,
            "ppid": ppid,
            "elapsed": label(parts[2]),
            "cpu_percent": number(cpu),
            "memory_percent": number(memory),
            "state": re.sub(r"[^A-Za-z+<NslL]", "", parts[5])[:16],
            "process_name": label(name),
        }
        if (
            name.lower() in {"claude", "claude-code", "claude.exe"}
            or "/claude/versions/" in parts[6]
        ):
            roots.add(pid)
    selected = set(roots)
    while True:
        added = {pid for pid, row in rows.items() if row["ppid"] in selected} - selected
        if not added:
            break
        selected.update(added)
    selected.discard(os.getpid())
    selected = {pid for pid in selected if rows[pid]["ppid"] != os.getpid()}
    return {
        "claude_root_pids": sorted(roots),
        "processes": [rows[pid] for pid in sorted(selected)[:160]],
        "processes_omitted_by_limit": max(0, len(selected) - 160),
        "note": (
            "Process names only; Node-based Claude launches may not be identified. "
            "No process-to-session mapping is inferred."
        ),
    }


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_arguments")


def main():
    parser = Parser(add_help=False)
    parser.add_argument("--session")
    parser.add_argument("--minutes", type=int, default=120)
    parser.add_argument("--max-files", type=int, default=12)
    parser.add_argument("--tail-mib", type=int, default=10)
    parser.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    parser.add_argument("--no-processes", action="store_true")
    parser.add_argument("--help", action="store_true")
    try:
        args = parser.parse_args()
        if args.help:
            print(
                json.dumps(
                    {
                        "usage": (
                            "python3 claude_wait_diagnostic.py [--session SESSION_ID] "
                            "[--minutes 120] [--max-files 12] [--tail-mib 10] "
                            "[--no-processes]"
                        ),
                        "privacy": __doc__,
                    },
                    indent=2,
                )
            )
            return 0
        if not (
            1 <= args.minutes <= 1440 and 1 <= args.max_files <= 24 and 1 <= args.tail_mib <= 20
        ):
            raise ValueError("argument_out_of_range")
        if args.session is not None and not SESSION_ID.fullmatch(args.session):
            raise ValueError("invalid_session_id")
        now = time.time()
        cutoff = now - args.minutes * 60
        root = Path(args.projects_dir).expanduser()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("projects_directory_missing_or_symlink")
        candidates, scan = discover(root, args.session, cutoff, args.max_files)
        report = {
            "diagnostic_version": VERSION,
            "observed_at_utc": iso(now),
            "privacy": (
                "Local metadata extraction only. Conversation files are read locally; "
                "text, arguments, results, secrets and full paths are not emitted. "
                "No network requests or changes."
            ),
            "window_minutes": args.minutes,
            "per_file_tail_limit_bytes": args.tail_mib * 1024 * 1024,
            "scan": scan,
            "files": [
                analyze_file(c, args.tail_mib * 1024 * 1024, now, cutoff) for c in candidates
            ],
            "process_snapshot": {"skipped": True} if args.no_processes else process_snapshot(),
            "limitations": [
                (
                    "Files are selected by modification time. Tail totals are partial when "
                    "truncated; shared response IDs use maximum reported usage per field."
                ),
                (
                    "Observed event span is not current tool runtime or uninterrupted agent "
                    "runtime. Logs can include resumed work and idle time."
                ),
                (
                    "Pending tools mean no matching result was observed, not proof the tool "
                    "is still running. Tool-pair duration includes approval and scheduling delays."
                ),
                (
                    "Event gaps can include model/network wait, retries, tool execution or logging "
                    "delays. This report cannot separate upload from provider wait."
                ),
                (
                    "Token counts are provider-reported input/output/cache metadata, not network "
                    "bytes. Missing usage fields have separate coverage counts."
                ),
                (
                    "Only standard ~/.claude/projects paths are searched. Custom CLAUDE_CONFIG_DIR "
                    "installations need --projects-dir; file and scan limits may omit agents."
                ),
            ],
        }
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        code = (
            str(exc)
            if type(exc) is ValueError
            and str(exc)
            in {
                "invalid_arguments",
                "argument_out_of_range",
                "invalid_session_id",
                "projects_directory_missing_or_symlink",
            }
            else type(exc).__name__
        )
        print(json.dumps({"diagnostic_version": VERSION, "error": code}))
        return 2
    except Exception as exc:
        print(
            json.dumps({"diagnostic_version": VERSION, "error": "unexpected_" + type(exc).__name__})
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
