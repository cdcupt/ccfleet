from __future__ import annotations

import pytest

from ccfleetd.config import Config
from ccfleetd.store import Store


@pytest.fixture
def cfg() -> Config:
    return Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:"})


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
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
