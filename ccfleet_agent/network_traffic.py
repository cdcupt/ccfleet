"""Bounded Linux byte counters, with no interface or host identity in reports.

Machine mode counts device-backed NICs (including a VM's virtual hardware),
excluding loopback, bridges, veth and tunnels so forwarding is not double-counted.
Service mode counts non-loopback interfaces in this process's network namespace.
Neither is provider-billable transfer or API request/response payload size.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping
from typing import Any, Optional

MAX_COUNTER = 2**53 - 1
MAX_EPOCH = 253402300799
MAX_FILE = 65536
MAX_GAP_S = 900
FUTURE_SKEW_S = 5
PROC_NETWORK = "/proc/net/dev"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
HOST_DIRECTORY = "/var/lib/ccfleet-network"
HOST_FILE = HOST_DIRECTORY + "/network.json"
SCOPES = ("machine_nic", "service_namespace")
OBSERVATION_KEYS = {"version", "scope", "observed_at", "counter_epoch",
                    "received_bytes", "sent_bytes"}
SUMMARY_KEYS = {"state", "scope", "observed_at", "measurement_started_at", "measurement_through_at",
                "coverage_s", "received_bytes", "sent_bytes", "receive_bytes_per_s",
                "send_bytes_per_s", "gaps", "resets"}


def number(value: Any, maximum: float = MAX_EPOCH) -> bool:
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _read(path: str, limit: int = MAX_FILE) -> str:
    with open(path, "rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversized network metric")
    return raw.decode("ascii")


def validate_observation(raw: Any) -> Optional[dict[str, Any]]:
    if (not isinstance(raw, Mapping) or set(raw) != OBSERVATION_KEYS
            or type(raw["version"]) is not int or raw["version"] != 1
            or raw["scope"] not in SCOPES or not number(raw["observed_at"])
            or not isinstance(raw["counter_epoch"], str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["counter_epoch"]) is None
            or not counter(raw["received_bytes"]) or not counter(raw["sent_bytes"])):
        return None
    return dict(raw)


def collect(*, now: Optional[float] = None, scope: str = "machine_nic",
            reader=_read, device_exists=os.path.exists) -> Optional[dict[str, Any]]:
    """Fixed proc inputs; bounded sysfs lookups only for names parsed from proc.

    Unsupported platforms or NIC views are unknown, never a synthetic zero.
    An opaque epoch changes on reboot, interface replacement or interface-set
    change. Counter decreases still invalidate a delta even with the same epoch.
    """
    try:
        observed = time.time() if now is None else now
        if scope not in SCOPES or not number(observed):
            return None
        text = reader(PROC_NETWORK)
        boot = reader(BOOT_ID, 64).strip()
        if (not isinstance(text, str) or len(text) > MAX_FILE or not text.isascii()
                or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is None):
            return None
        interfaces, received, sent = [], 0, 0
        for line in text.splitlines()[2:]:
            if not line.strip():
                continue
            if ":" not in line:
                return None
            name, values = line.split(":", 1)
            name, fields = name.strip(), values.split()
            if (re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name) is None or len(fields) != 16
                    or any(re.fullmatch(r"[0-9]{1,16}", field) is None for field in fields)):
                return None
            if name == "lo" or (scope == "machine_nic"
                                and not device_exists("/sys/class/net/" + name + "/device")):
                continue
            index = reader("/sys/class/net/" + name + "/ifindex", 32).strip()
            if re.fullmatch(r"[1-9][0-9]{0,9}", index) is None:
                return None
            identity = name + ":" + index
            if identity in interfaces or len(interfaces) >= 64:
                return None
            interfaces.append(identity)
            received += int(fields[0])
            sent += int(fields[8])
        if not interfaces or not counter(received) or not counter(sent):
            return None
        epoch = hashlib.sha256((boot + "\0" + scope + "\0" + "\0".join(
            sorted(interfaces))).encode("ascii")).hexdigest()
        return {"version": 1, "scope": scope, "observed_at": observed, "counter_epoch": epoch,
                "received_bytes": received, "sent_bytes": sent}
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, KeyError):
        return None


def empty(scope: str, state: str = "unavailable", observed_at=None) -> dict[str, Any]:
    return {"state": state, "scope": scope, "observed_at": observed_at,
            "measurement_started_at": None, "measurement_through_at": None,
            "coverage_s": 0, "received_bytes": None, "sent_bytes": None,
            "receive_bytes_per_s": None, "send_bytes_per_s": None, "gaps": 0, "resets": 0}


def summarize(observations, now: float, *, scope: str = "machine_nic",
              max_gap_s: float = MAX_GAP_S, since: float = 0) -> dict[str, Any]:
    """Only measured adjacent deltas count; reset/gap intervals contribute nothing.

    A first sample establishes a baseline. Retention and claim boundaries limit
    coverage, so this is never labelled lifetime, monthly or full-window traffic.
    Missing samples must be represented by None and break the baseline.
    """
    result = empty(scope)
    if not number(now) or not number(since) or not number(max_gap_s) or not max_gap_s:
        return result
    previous = latest = None
    received = sent = 0
    for raw in observations:
        current = validate_observation(raw)
        if current is None or current["scope"] != scope:
            if previous is not None:
                result["gaps"] += 1
            previous = None
            continue
        at = current["observed_at"]
        if at < since or at > now + FUTURE_SKEW_S:
            previous = None
            continue
        if latest is not None and at <= latest["observed_at"]:
            result["gaps"] += 1
            previous = None
            continue
        latest = current
        if result["measurement_started_at"] is None:
            result["measurement_started_at"] = at
        result["measurement_through_at"] = at
        result["receive_bytes_per_s"] = result["send_bytes_per_s"] = None
        if previous is not None:
            elapsed = at - previous["observed_at"]
            rx = current["received_bytes"] - previous["received_bytes"]
            tx = current["sent_bytes"] - previous["sent_bytes"]
            if (current["counter_epoch"] != previous["counter_epoch"] or rx < 0 or tx < 0):
                result["resets"] += 1
            elif not 0 < elapsed <= max_gap_s:
                result["gaps"] += 1
            elif (counter(received + rx) and counter(sent + tx)
                  and number(rx / elapsed, MAX_COUNTER) and number(tx / elapsed, MAX_COUNTER)):
                received += rx
                sent += tx
                result["coverage_s"] += elapsed
                result["receive_bytes_per_s"] = rx / elapsed
                result["send_bytes_per_s"] = tx / elapsed
            else:
                return empty(scope)
        previous = current
    if latest is None:
        return result
    observed = latest["observed_at"]
    if now - observed > max_gap_s:
        return empty(scope, "stale", observed)
    result["observed_at"] = observed
    result["state"] = "ready" if result["coverage_s"] else "warming"
    if result["coverage_s"]:
        result["received_bytes"], result["sent_bytes"] = received, sent
    return result



def validate_summary(raw: Any, scope: str) -> Optional[dict[str, Any]]:
    """Copy only a fixed metric contract; even cached input cannot add metadata."""
    if (not isinstance(raw, Mapping) or set(raw) != SUMMARY_KEYS or raw["scope"] != scope
            or raw["state"] not in ("unavailable", "warming", "ready", "stale")
            or any(raw[key] is not None and not number(raw[key]) for key in (
                "observed_at", "measurement_started_at", "measurement_through_at"))
            or not number(raw["coverage_s"])
            or any(not counter(raw[key]) for key in ("gaps", "resets"))
            or any(raw[key] is not None and not counter(raw[key])
                   for key in ("received_bytes", "sent_bytes"))
            or any(raw[key] is not None and not number(raw[key], MAX_COUNTER)
                   for key in ("receive_bytes_per_s", "send_bytes_per_s"))):
        return None
    if raw["state"] in ("unavailable", "stale", "warming") and any(
            raw[key] is not None for key in ("received_bytes", "sent_bytes",
                                            "receive_bytes_per_s", "send_bytes_per_s")):
        return None
    return dict(raw)


def validate_snapshot(raw: Any, now: float, *, max_age_s: float = 60) -> Optional[dict[str, Any]]:
    """The host collector stores a bounded, numeric-only observation history."""
    if (not isinstance(raw, dict) or set(raw) != {"version", "observations"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or not isinstance(raw["observations"], list)
            or not 1 <= len(raw["observations"]) <= 512):
        return None
    observations = [validate_observation(item) for item in raw["observations"]]
    if any(item is None or item["scope"] != "machine_nic" for item in observations):
        return None
    return summarize(observations, now, max_gap_s=max_age_s)


def write_host_snapshot() -> None:
    """Root-only oneshot. No commands, sockets, credentials or provider requests.

    systemd creates the dedicated directory with root:reader-group ownership and 0750.
    The primary mounts only that directory read-only. Atomic replacement avoids
    readers observing partial JSON; the parent directory may not be writable by
    the service UID/group. A failed collector leaves the old file to go stale.
    """
    reader_gid = os.getegid()
    if os.geteuid() != 0 or type(reader_gid) is not int or not 1 <= reader_gid < 2**31:
        raise PermissionError("host collector requires root and a non-root reader group")
    directory = os.lstat(HOST_DIRECTORY)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0
            or directory.st_gid != reader_gid or stat.S_IMODE(directory.st_mode) != 0o750):
        raise PermissionError("unsafe network metric directory")
    current = collect()
    if current is None:
        raise ValueError("host network counters unavailable")
    history = []
    try:
        info = os.lstat(HOST_FILE)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != reader_gid
                or stat.S_IMODE(info.st_mode) != 0o640 or info.st_nlink != 1):
            raise PermissionError("unsafe network metric file")
        try:
            raw = json.loads(_read(HOST_FILE, 262144))
        except (ValueError, UnicodeError):
            raw = None
        if validate_snapshot(raw, current["observed_at"]) is not None:
            history = raw["observations"]
    except FileNotFoundError:
        pass
    history = [*history[-511:], current]
    body = json.dumps({"version": 1, "observations": history}, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(prefix=".network-", dir=HOST_DIRECTORY)
    try:
        os.fchown(descriptor, 0, reader_gid)
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, HOST_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect numeric-only host network transfer")
    parser.add_argument("--write-host-snapshot", action="store_true", required=True)
    parser.parse_args()
    write_host_snapshot()


if __name__ == "__main__":  # pragma: no cover
    main()
