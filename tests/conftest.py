from __future__ import annotations

import re
import urllib.parse

import pytest

from ccfleetd.config import Config
from ccfleetd.store import Store


def refresh_of(page: str):
    """A page's own refresh: after how many seconds, and to where — "" meaning
    the address the page was opened at, note and fragment and all. None when
    the page stays put."""
    found = re.search(r'<meta http-equiv="refresh" content="(\d+)(?:;url=([^"]*))?">', page)
    return (int(found.group(1)), found.group(2) or "") if found else None


def next_load(at: str, page: str):
    """What a browser showing `page` at address `at` requests when the refresh
    fires, or None when it requests nothing: no refresh, or one that resolves
    to the same address with a fragment. That is a fragment navigation, which
    only scrolls; it is what a refresh naming no address does on /admin#card."""
    refresh = refresh_of(page)
    if refresh is None:
        return None
    target = urllib.parse.urljoin(at, refresh[1])
    bare = urllib.parse.urldefrag(target)[0]
    if "#" in target and bare == urllib.parse.urldefrag(at)[0]:
        return None
    return bare


@pytest.fixture
def cfg() -> Config:
    return Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:"})


@pytest.fixture
def store() -> Store:
    s = Store(":memory:", max_slots_per_machine=8)
    yield s
    s.close()


def heartbeat(ts: float, **overrides):
    """A plausible validated payload with per-section overrides."""
    payload = {
        "node_id": "node-a", "agent_ts": ts, "agent_version": "0.1.0", "hostname": "vps-a",
        "uptime_s": 1000.0,
        "claude": {"version": "2.1.92", "path": "/home/a/.local/bin/claude"},
        "credentials": {"present": True, "store": "file", "mtime": ts - 600,
                        "expires_at": (ts + 7200) * 1000, "subscription_type": "max"},
        "disk": {"used_pct": 40.0, "free_gb": 20.0}, "mem": {"used_pct": 30.0},
        "load": {"1": 0.1, "5": 0.1, "15": 0.1},
        "egress": {"ip": "203.0.113.10", "source": "api.ipify.org"},
        "remote_control": {"state": "active"}, "tmux_sessions": 1,
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return {"ts": ts, "payload": payload}
