import json
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


def test_reconcile_is_absent_until_a_node_actually_reports_one():
    """An invented skeleton of Nones is truthy, so every node would look as though
    it had reconciled at least once."""
    assert "reconcile" not in validate_heartbeat({"node_id": "n"}, "n")
    assert "reconcile" not in validate_heartbeat(
        {"node_id": "n", "reconcile": {"upgrade": {}}}, "n")
    assert "reconcile" not in validate_heartbeat(
        {"node_id": "n", "reconcile": "not a mapping"}, "n")
    reported = validate_heartbeat(
        {"node_id": "n", "reconcile": {"upgrade": {"to": "2.1.99", "ok": True}}}, "n")
    assert reported["reconcile"]["upgrade"]["to"] == "2.1.99"
    assert reported["reconcile"]["upgrade"]["ok"] is True


def test_identity_fields_cannot_reach_the_store_through_credentials():
    """The agent does not send these, and the server would not keep them anyway."""
    from ccfleetd.heartbeat import validate_heartbeat
    out = validate_heartbeat({"node_id": "n", "credentials": {
        "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty",
        "email": "someone@example.com", "orgId": "a7ba7d79", "orgName": "Acme",
    }}, "n")
    creds = out["credentials"]
    assert creds["logged_in"] is True and creds["auth_method"] == "claude.ai"
    blob = json.dumps(out)
    for private in ("someone@example.com", "a7ba7d79", "Acme"):
        assert private not in blob


def test_a_real_sign_in_url_survives_the_whitelist_intact():
    """Measured at 496 characters against a live node.

    Capped at MAX_STR the URL arrived truncated to 200 — which is worse than
    absent, because it still looks like a URL and fails only when clicked.
    """
    from ccfleetd.heartbeat import MAX_URL
    url = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9"
           "-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform."
           "claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile"
           "+user%3Ainference+user%3Asessions%3Aclaude_code+user%3Amcp_servers+user%3A"
           "file_upload+user%3Aplugins&code_challenge=gz7bOnSAmGyBMHOKijGzBhiB5LoTTqcHon"
           "ZexLJsx3E&code_challenge_method=S256&state=hN1YnDdKxl6It0ZEI_hAP5HUqVDY5qZn"
           "XiHRSFT9-N8&login_hint=someone%40example.com")
    assert len(url) > 400, "fixture should be a realistic length"
    out = validate_heartbeat(
        {"node_id": "n", "reconcile": {"login": {"state": "url_ready", "url": url,
                                                 "detail": "d" * 500}}}, "n")
    login = out["reconcile"]["login"]
    assert login["url"] == url, "the URL must arrive whole"
    # The larger cap is for this one field, not a general loosening.
    assert len(login["detail"]) == 200
    # And it is still bounded: a node cannot post an unbounded string.
    huge = validate_heartbeat(
        {"node_id": "n", "reconcile": {"login": {"state": "x", "url": "h" * 9000}}}, "n")
    assert len(huge["reconcile"]["login"]["url"]) == MAX_URL


def test_usage_is_counts_only_and_every_part_of_it_is_bounded():
    """A node could otherwise post an unbounded series, or smuggle content."""
    out = validate_heartbeat({"node_id": "n", "usage": {
        "total_tokens": 40897, "sessions": 3, "window_days": 14,
        "models": ["claude-opus-5"],
        "by_day": [{"day": "2026-09-19", "tokens": 40897}],
        "transcript": "the user's private conversation",
    }}, "n")
    usage = out["usage"]
    assert usage["total_tokens"] == 40897 and usage["sessions"] == 3
    assert usage["by_day"] == [{"day": "2026-09-19", "tokens": 40897}]
    assert "private conversation" not in json.dumps(out)

    big = validate_heartbeat({"node_id": "n", "usage": {
        "by_day": [{"day": f"d{i}", "tokens": i} for i in range(500)],
        "models": [f"m{i}" for i in range(50)],
    }}, "n")["usage"]
    assert len(big["by_day"]) == 31 and len(big["models"]) == 8

    junk = validate_heartbeat({"node_id": "n", "usage": {
        "by_day": ["not a mapping", {"day": None, "tokens": 5}, {"day": "ok", "tokens": None}],
        "models": "not a list",
    }}, "n")["usage"]
    assert junk["by_day"] == [] and junk["models"] == []
