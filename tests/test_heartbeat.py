import math

import pytest

from ccfleetd.heartbeat import HeartbeatError, validate_heartbeat


def test_rejects_non_object_and_wrong_node():
    with pytest.raises(HeartbeatError):
        validate_heartbeat([], "node-a")
    with pytest.raises(HeartbeatError):
        validate_heartbeat({"node_id": "node-b"}, "node-a")


def test_keeps_known_fields_and_drops_the_rest():
    out = validate_heartbeat({
        "node_id": "node-a", "ts": 12.5, "hostname": "h" * 500, "surprise": {"x": 1},
        "claude": {"version": "2.1.92", "extra": 1}, "credentials": {"present": True,
                                                                     "mtime": 5, "accessToken": "sk"},
        "disk": {"used_pct": float("nan")}, "egress": {"ip": "203.0.113.1"},
        "remote_control": {"state": "active"}, "tmux_sessions": True,
    }, "node-a")
    assert "surprise" not in out and "extra" not in out["claude"]
    assert "accessToken" not in out["credentials"]
    assert len(out["hostname"]) == 200
    assert out["disk"]["used_pct"] is None
    assert out["tmux_sessions"] is None
    assert out["agent_ts"] == 12.5 and out["egress"]["ip"] == "203.0.113.1"


def test_sections_tolerate_wrong_types():
    out = validate_heartbeat({"node_id": "node-a", "claude": "nope", "credentials": 3,
                              "disk": None, "load": [1, 2]}, "node-a")
    assert out["claude"] == {"version": None, "path": None}
    assert out["credentials"]["present"] is None
    assert out["load"] == {"1": None, "5": None, "15": None}
    assert math.isfinite(out["agent_ts"] or 0.0)


def test_the_new_credential_fields_survive_validation():
    """A laptop has no credentials file, so these two carry the whole login signal."""
    out = validate_heartbeat({"node_id": "node-a", "ts": 1.0, "credentials": {
        "present": True, "store": "keychain",
        "profile_fetched_at": 1_700_000_000_000, "plan": "default_claude_max_20x"}}, "node-a")
    assert out["credentials"]["profile_fetched_at"] == 1_700_000_000_000
    assert out["credentials"]["plan"] == "default_claude_max_20x"
