"""Opt-in stdio management, synthetic status only; no model or network requests."""

from __future__ import annotations

import copy
import io
import json

import pytest

from ccfleet_agent import client_experience as ux
from ccfleet_agent import relay_metrics

NOW = 1_790_000_000.0
SECRET = "PRIVATE_EMAIL_TOKEN_HOST_IP_PATH_MODEL_SESSION"


def observation():
    return {
        "authenticated": True, "protocol": 2, "device": {"active": True, "id": SECRET},
        "identity": SECRET,
        "slot": {"state": "active", "ready": True, "health": "ready",
                 "reason": "credentials_current", "readiness_source": "reported",
                 "observed_at": NOW, "account_generation": SECRET, "claude_version": SECRET,
                 "name": SECRET, "email": SECRET, "host": SECRET, "path": SECRET,
                 "quota": {"checked_at": NOW - 10, "private": SECRET,
                           "session": {"used_pct": 12.5, "resets_at": NOW + 500,
                                       "resets": SECRET},
                           "week": {"used_pct": 40}}},
    }


def metrics():
    return {
        "version": 1, "observed_at": NOW, "window_days": 7, "last_success_at": NOW - 1,
        "requests": 3, "success": 2, "auth_errors": 0, "permission_errors": 0,
        "rate_limits": 1, "upstream_errors": 0, "connection_errors": 0,
        "cancelled": 0, "input_errors": 0,
        "latencies": {key: {"count": 2, "mean": 50.5, "max": 100}
                      for key in ux.MCP_TIMINGS},
        "token_usage": {"eligible": 2, "samples": 1, "input_tokens": 11, "output_tokens": 20,
                        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0,
                        "cache_read_samples": 1, "cache_creation_samples": 0},
    }


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ux.time, "time", lambda: NOW)
    monkeypatch.setattr(ux.time, "monotonic", lambda: 100.0)


def request(method, params=None, identifier=1):
    result = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        result["params"] = params
    return result


def initialize(version="2025-11-25"):
    return request("initialize", {"protocolVersion": version,
                                 "capabilities": {"roots": {"private": SECRET}},
                                 "clientInfo": {"name": SECRET, "version": SECRET,
                                                "websiteUrl": SECRET}})


def ready():
    return [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def call(name="ccfleet_health", arguments=None, identifier=2):
    return request("tools/call", {"name": name, "arguments": {} if arguments is None else arguments},
                   identifier)


def run(messages, callback=None, *, raw=False, text=False):
    events = []

    def read():
        events.append("GET status")
        return callback() if callback else observation()

    wire = messages if raw else b"".join((json.dumps(item) + "\n").encode() for item in messages)
    source = io.StringIO(wire.decode()) if text else io.BytesIO(wire)
    destination = io.StringIO() if text else io.BytesIO()
    code = ux.serve_mcp(read, input_stream=source, output_stream=destination)
    output = destination.getvalue()
    if isinstance(output, bytes):
        output = output.decode()
    return code, [json.loads(line) for line in output.splitlines()], events, output


def test_discovery_is_exact_read_only_and_never_uses_status_or_caller_metadata(capsys):
    code, messages, events, output = run([*ready(), request("tools/list"), request("ping")])
    assert code == 0 and events == [] and SECRET not in output
    initialized, listed, pong = (item["result"] for item in messages)
    assert initialized["protocolVersion"] == "2025-11-25"
    assert initialized["capabilities"] == {"tools": {"listChanged": False}}
    assert initialized["serverInfo"] == {"name": "ccfleet-management", "version": "1"}
    assert "calling AI" in initialized["instructions"]
    assert {tool["name"] for tool in listed["tools"]} == {
        "ccfleet_health", "ccfleet_quota", "ccfleet_relay_usage"}
    for tool in listed["tools"]:
        assert tool["inputSchema"] == {"type": "object", "properties": {},
                                        "additionalProperties": False}
        assert tool["annotations"] == {"readOnlyHint": True, "destructiveHint": False,
                                        "idempotentHint": True, "openWorldHint": False}
    assert pong == {} and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("version,chosen", [
    ("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
    ("2024-11-05", "2025-11-25"), ("2099-01-01", "2025-11-25"),
])
def test_protocol_negotiates_a_known_version_without_reflecting_client(version, chosen):
    _, result, events, output = run([initialize(version)])
    assert result[0]["result"]["protocolVersion"] == chosen
    assert events == [] and SECRET not in output


@pytest.mark.parametrize("text", [False, True])
def test_three_tools_return_only_projected_content_and_each_reads_status_once(text):
    status = observation()
    status["slot"]["relay"] = metrics()
    code, messages, events, output = run([*ready(), *(call(name) for name in ux._MCP_TOOLS)],
                                         lambda: status, text=text)
    assert code == 0 and len(events) == 3 and SECRET not in output
    for item in messages[1:]:
        result = item["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert "provider_acceptance_verified" in output and '"billing":false' in output


def test_health_is_reported_not_provider_verified_and_stale_readiness_is_false():
    source = observation()
    current = ux.management_health(source, now=NOW)
    assert current["reported_ready"] and not current["provider_acceptance_verified"]
    source["slot"]["observed_at"] = NOW - ux.MCP_FRESH_SECONDS - 1
    stale = ux.management_health(source, now=NOW)
    assert not stale["reported_ready"] and stale["freshness"] == "stale"
    del source["slot"]["observed_at"]
    unknown = ux.management_health(source, now=NOW)
    assert not unknown["reported_ready"] and unknown["age_seconds"] is None


def compatibility(state="passed", reason=None):
    return {"state": state, "native_version": "2.1.295", "checked_at": NOW - 10,
            "usage_observed_at": NOW - 10, "last_success_at": NOW - 10,
            "next_check_at": NOW + 300,
            "checks": dict.fromkeys(ux.COMPATIBILITY_CHECKS, True),
            **({"reason": reason} if reason else {})}


@pytest.mark.parametrize("state,reason", [("passed", None), ("pending", "usage_pending"),
                                       ("failed", "relay_protocol_mismatch"),
                                       ("blocked", "account_transition"),
                                       ("blocked", "native_auth_source"),
                                       ("blocked", "native_extensions")])
def test_management_health_exports_fixed_compatibility_without_provider_acceptance_claim(state, reason):
    source = observation()
    source["slot"]["compatibility"] = {**compatibility(state, reason), "runtime_fp": SECRET,
                                     "account_fp": SECRET, "message": SECRET, "token": SECRET}
    result = ux.management_health(source, now=NOW)
    assert result["reported_ready"] is True and result["provider_acceptance_verified"] is False
    assert result["compatibility"] == compatibility(state, reason)
    assert SECRET not in json.dumps(result)
    # Compatibility is separate from cached quota/relay availability.
    assert ux.management_quota(source, now=NOW)["available"] is True
    assert ux.management_relay_usage(source, now=NOW)["available"] is False


def test_management_tool_compatibility_metadata_does_not_reveal_private_fields():
    source = observation()
    source["slot"]["compatibility"] = {**compatibility("failed", "relay_tls_policy"),
                                     "account_fp": SECRET, "runtime_fp": SECRET,
                                     "raw_output": SECRET}
    code, messages, events, output = run([*ready(), call()], lambda: source)
    result = messages[-1]["result"]["structuredContent"]
    assert code == 0 and events == ["GET status"] and SECRET not in output
    assert result["compatibility"]["state"] == "failed"
    assert result["reported_ready"] and not result["provider_acceptance_verified"]


@pytest.mark.parametrize("changes", [
    {"state": SECRET}, {"reason": SECRET, "native_version": SECRET},
    {"checked_at": True}, {"checked_at": 0}, {"checked_at": 10 ** 400},
    {"checked_at": NOW + 61}, {"last_success_at": NOW + 61},
    {"usage_observed_at": None}, {"checks": {"native_version": True}},
])
def test_malformed_compatibility_fails_closed_without_private_exception(changes):
    source = observation()
    source["slot"]["compatibility"] = {**compatibility(), **changes}
    for projection in (ux.management_health, ux.management_quota, ux.management_relay_usage):
        with pytest.raises(ux.ExperienceError) as error:
            projection(source, now=NOW)
        assert SECRET not in str(error.value)


def test_cached_compatibility_pass_does_not_promote_stale_model_readiness():
    source = observation()
    source["slot"]["compatibility"] = compatibility()
    source["slot"]["observed_at"] = NOW - ux.MCP_FRESH_SECONDS - 1
    result = ux.management_health(source, now=NOW)
    assert result["compatibility"]["state"] == "passed"
    assert result["reported_ready"] is False and result["provider_acceptance_verified"] is False


@pytest.mark.parametrize("health,reasons", list(ux.MCP_REASONS.items()))
def test_all_known_health_reasons_project_fixed_codes(health, reasons):
    for reason in reasons:
        status = observation()
        status["slot"].update(health=health, reason=reason, ready=health == "ready")
        result = ux.management_health(status)
        assert result["health"] == health and result["reason"] == reason


@pytest.mark.parametrize("change", [
    lambda s: s.update(authenticated=False),
    lambda s: s.update(protocol=True),
    lambda s: s.update(device={"active": 1}),
    lambda s: s.update(slot=[]),
    lambda s: s["slot"].update(reason=SECRET),
    lambda s: s["slot"].update(health=SECRET),
    lambda s: s["slot"].update(health=[]),
    lambda s: s["slot"].update(state="free"),
    lambda s: s["slot"].update(ready=1),
    lambda s: s["slot"].update(ready=False),
    lambda s: s["slot"].update(readiness_source=SECRET),
    lambda s: s["slot"].update(observed_at=SECRET),
    lambda s: s["slot"].update(observed_at=NOW + 61),
])
def test_malformed_or_unauthorized_status_fails_closed_for_every_projection(change):
    value = observation()
    change(value)
    for projection in (ux.management_health, ux.management_quota, ux.management_relay_usage):
        with pytest.raises(ux.ExperienceError):
            projection(value)


@pytest.mark.parametrize("timestamp", [True, "123", float("nan"), float("inf"), -1, NOW + 61])
def test_invalid_observation_never_becomes_ready(timestamp):
    status = observation()
    status["slot"]["observed_at"] = timestamp
    with pytest.raises(ux.ExperienceError):
        ux.management_health(status)


def test_quota_stamps_and_resets_are_numeric_cached_and_unavailable_stays_unknown():
    source = observation()
    result = ux.management_quota(source)
    assert result["available"] and result["age_seconds"] == 10
    assert result["windows"]["session"] == {
        "used_pct": 12.5, "remaining_pct": 87.5, "resets_at": NOW + 500}
    assert result["windows"]["week"]["resets_at"] is None
    source["slot"]["quota"]["checked_at"] = NOW - 601
    assert ux.management_quota(source)["freshness"] == "stale"
    source["slot"]["quota"].pop("checked_at")
    result = ux.management_quota(source)
    assert not result["available"] and result["windows"] == {} and result["observed_at"] is None
    source["slot"].pop("quota")
    assert ux.management_quota(source) == result


@pytest.mark.parametrize("field,value", [
    ("used_pct", True), ("used_pct", -1), ("used_pct", 101), ("used_pct", "20"),
    ("used_pct", float("nan")), ("resets_at", SECRET), ("resets_at", float("inf")),
])
def test_quota_rejects_invalid_numbers_and_private_reset_strings(field, value):
    source = observation()
    source["slot"]["quota"]["session"][field] = value
    with pytest.raises(ux.ExperienceError):
        ux.management_quota(source)


def test_relay_is_cached_seven_calendar_days_and_usage_coverage_is_not_billing():
    source = observation()
    raw = metrics()
    assert relay_metrics.validate_report(raw) == raw
    source["slot"]["relay"] = raw
    saved = copy.deepcopy(source)
    result = ux.management_relay_usage(source)
    assert result["available"] and result["window_days"] == 7
    assert result["window_kind"] == "utc_calendar_days" and result["billing"] is False
    assert result["counts"]["requests"] == 3 and result["counts"]["rate_limits"] == 1
    assert result["latencies"]["total_ms"] == {"samples": 2, "mean_ms": 50.5, "max_ms": 100}
    assert result["token_usage"]["coverage_pct"] == 50
    assert result["token_usage"]["input_tokens"] == 11
    assert result["token_usage"]["cache_creation_input_tokens"] is None
    assert source == saved and SECRET not in json.dumps(result)


def test_legacy_and_zero_sample_relay_observations_are_not_fabricated_zero_performance():
    source = observation()
    raw = metrics()
    source["slot"]["relay"] = raw
    raw.pop("token_usage")
    raw["latencies"]["connect_ms"] = {"count": 0, "mean": 0, "max": 0}
    result = ux.management_relay_usage(source)
    assert result["token_usage"] is None
    assert result["latencies"]["connect_ms"] == {"samples": 0, "mean_ms": None, "max_ms": None}
    raw["token_usage"] = {key: 0 for key in metrics()["token_usage"]}
    raw["token_usage"]["eligible"] = 2
    usage = ux.management_relay_usage(source)["token_usage"]
    assert usage["coverage_pct"] == 0 and usage["input_tokens"] is None
    raw["token_usage"]["eligible"] = 0
    assert ux.management_relay_usage(source)["token_usage"]["coverage_pct"] is None


@pytest.mark.parametrize("change", [
    lambda r: r.update(email=SECRET), lambda r: r.update(version=True),
    lambda r: r.update(window_days=1), lambda r: r.update(requests=True),
    lambda r: r.update(requests=4), lambda r: r.update(success=-1),
    lambda r: r.update(observed_at=NOW + 61), lambda r: r.update(last_success_at=NOW + 1),
    lambda r: r["latencies"]["total_ms"].update(mean=float("nan")),
    lambda r: r["latencies"]["total_ms"].update(mean=101),
    lambda r: r["latencies"]["total_ms"].update(count=4),
    lambda r: r["latencies"]["total_ms"].update(count=0),
    lambda r: r["token_usage"].update(eligible=3),
    lambda r: r["token_usage"].update(samples=3),
    lambda r: r["token_usage"].update(input_tokens=True),
    lambda r: r["token_usage"].update(input_tokens=10**9 + 1),
    lambda r: r["token_usage"].update(cache_creation_input_tokens=1),
    lambda r: r["token_usage"].update(cache_read_samples=2),
    lambda r: r["token_usage"].update(model=SECRET),
])
def test_relay_aggregate_guards_reject_malformed_and_identity_content(change):
    source = observation()
    source["slot"]["relay"] = metrics()
    change(source["slot"]["relay"])
    with pytest.raises(ux.ExperienceError):
        ux.management_relay_usage(source)


def test_expired_and_missing_relay_is_unknown_not_zero():
    source = observation()
    missing = ux.management_relay_usage(source)
    assert missing["available"] is False and "counts" not in missing
    source["slot"]["relay"] = metrics()
    source["slot"]["relay"]["observed_at"] = NOW - 7 * 86400 - 1
    assert ux.management_relay_usage(source) == missing


@pytest.mark.parametrize("status,expected", [
    (401, "pairing_unavailable"), (403, "access_denied"), (404, "service_unavailable"),
    (409, "assignment_changed"), (500, "status_unavailable"), (None, "status_unavailable"),
])
def test_callback_failures_never_echo_bodies_paths_tokens_or_exception_text(status, expected):
    def fail():
        error = RuntimeError(SECRET)
        error.status = status
        raise error

    _, result, events, output = run([*ready(), call()], fail)
    assert len(events) == 1 and SECRET not in output
    assert result[-1]["result"]["isError"] is True
    assert result[-1]["result"]["structuredContent"]["error"] == expected


def test_revocation_is_rechecked_every_call_and_previous_success_is_never_replayed():
    events = []

    def once():
        events.append(1)
        if len(events) > 1:
            error = RuntimeError(SECRET)
            error.status = 401
            raise error
        return observation()

    _, result, _, output = run([*ready(), call(), call(identifier=3)], once)
    assert result[1]["result"]["isError"] is False
    assert result[2]["result"]["isError"] is True and SECRET not in output


def test_lifecycle_blocks_preinitialization_reinitialization_and_request_notification_confusion():
    _, result, events, _ = run([call(), initialize(), call(),
                               {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "ccfleet_health"}},
                               {"jsonrpc": "2.0", "method": "notifications/initialized"},
                               initialize(), call()])
    assert [item["error"]["code"] for item in result if "error" in item] == [-32002, -32002, -32602]
    assert len(events) == 1


@pytest.mark.parametrize("invalid", [
    request("tools/list", {"cursor": SECRET}), request("tools/list", {"cursor": ""}),
    request("tools/call", {"name": SECRET}), call(arguments={"slot": SECRET}),
    call(arguments={"url": "https://example.invalid/"}), call(arguments=[]),
    request("tools/call", {"name": "ccfleet_health", "arguments": None}),
    request("tools/call", {"name": "ccfleet_health", "task": {}}),
    request("tools/call", {"name": "ccfleet_health", "_meta": SECRET}),
    request("tools/call", []), request("ping", {"extra": SECRET}),
])
def test_unknown_cursors_tools_arguments_and_targets_never_reach_callback(invalid):
    _, result, events, output = run([*ready(), invalid])
    assert result[-1]["error"]["code"] == -32602 and events == [] and SECRET not in output


def test_unadvertised_capabilities_are_not_available_and_no_meta_reaches_callback():
    _, result, events, output = run([*ready(), request("resources/list"),
                                    request("prompts/list"), request("roots/list"),
                                    request("tools/call", {"name": "ccfleet_health",
                                            "_meta": {"progressToken": SECRET}})])
    assert [item["error"]["code"] for item in result if "error" in item] == [-32601] * 3
    assert len(events) == 1 and SECRET not in output


def test_initialized_notification_discards_metadata_without_disclosing_or_requesting_roots():
    _, result, events, output = run([
        initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized",
                       "params": {"_meta": {"trace": SECRET}}}, request("tools/list"), call(),
    ])
    assert "tools" in result[1]["result"] and len(events) == 1 and SECRET not in output


def test_malformed_status_becomes_fixed_tool_error_without_private_strings():
    _, result, _, output = run([*ready(), call()], lambda: {"raw": SECRET})
    assert result[-1]["result"]["isError"] is True
    assert result[-1]["result"]["structuredContent"]["error"] == "status_unavailable"
    assert SECRET not in output


def test_rate_limit_expires_without_background_refresh(monkeypatch):
    monkeypatch.setattr(ux, "MCP_MAX_CALLS", 1)
    ticks = iter([100, 101, 160])
    monkeypatch.setattr(ux.time, "monotonic", lambda: next(ticks))
    _, result, events, _ = run([*ready(), call(), call(), call()])
    assert len(events) == 2
    assert [item["result"]["isError"] for item in result[1:]] == [False, True, False]


@pytest.mark.parametrize("identifier", [None, True, [], {}, 1.5, 2**53, "x" * 129, "line\nbreak"])
def test_invalid_or_unbounded_ids_are_rejected_without_echo(identifier):
    _, result, events, _ = run([request("ping", identifier=identifier)])
    assert result == [{"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32600, "message": "Invalid request"}}]
    assert events == []


@pytest.mark.parametrize("wire", [
    b"{}\n", b"[]\n", b"null\n", b"{broken}\n", b"\xff\n", b"\n",
    b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/call"}\n',
    b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n',
    b'{\r"jsonrpc":"2.0","id":1,"method":"ping"}\n',
    b'{"jsonrpc":"2.0","id":1,"method":"ping","extra":"private"}\n',
    (b"[" * 3000) + b"0" + (b"]" * 3000) + b"\n",
])
def test_malformed_frames_fail_closed_without_status_reads_or_raw_echo(wire):
    code, result, events, output = run(wire, raw=True)
    assert code == 0 and len(result) == 1 and "error" in result[0]
    assert events == [] and "private" not in output


@pytest.mark.parametrize("wire", [b"x" * (ux.MCP_MAX_LINE + 1), b'{"jsonrpc":"2.0"}'])
def test_oversized_and_unterminated_frames_close_without_draining_or_network(wire):
    code, result, events, _ = run(wire, raw=True)
    assert code == 2 and result[0]["error"]["code"] == -32600 and not events


def test_binary_crlf_frames_and_empty_stream_are_supported():
    wire = (json.dumps(request("ping")) + "\r\n").encode()
    assert run(wire, raw=True)[1] == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert run(b"", raw=True)[:3] == (0, [], [])


def test_rate_limit_blocks_callback_without_sleeping_or_caching_success(monkeypatch):
    monkeypatch.setattr(ux, "MCP_MAX_CALLS", 3)
    _, result, events, _ = run([*ready(), *(call(identifier=i) for i in range(2, 6))])
    assert len(events) == 3
    assert result[-1]["result"]["structuredContent"]["error"] == "rate_limited"


def test_broken_output_is_quiet_and_exits_without_status_call():
    class Broken(io.BytesIO):
        def write(self, _data):
            raise BrokenPipeError(SECRET)

    source = io.BytesIO((json.dumps(request("ping")) + "\n").encode())
    assert ux.serve_mcp(lambda: pytest.fail("no status read"), input_stream=source,
                        output_stream=Broken()) == 1
