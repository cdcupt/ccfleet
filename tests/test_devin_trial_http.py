"""Real HTTP/account cookies/CSRF + actual trial ledger with fake worker only."""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
import urllib.parse
from dataclasses import replace
from http.client import HTTPConnection

import pytest

from ccfleetd import devin_trial, sessions, usersite
from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from tests.test_devin_trial_runtime import FakeWorker, body, receipt
from tests.test_devin_trial_runtime import trial as trial


def call(server, method, path, raw=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.request(method, path, raw, headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.fixture
def http(trial):
    t = trial
    context = Context(t.store, t.cfg, Monitor(t.store, t.cfg, LogNotifier()), trial=t.runtime)
    server = build_server(context, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    t.server = server
    t.session = t.store.create_session(t.alice["id"], now=time.time(), ttl_s=86400)
    t.cookie = sessions.COOKIE_NAME + "=" + sessions.sign(t.session, t.cfg.cookie_secret)
    t.csrf = usersite.csrf_for(t.session, t.cfg.cookie_secret)
    yield t
    server.shutdown()
    server.server_close()
    thread.join(3)
    context.gateway.close()


def form(t, suffix="create", fields=None, language="en", headers=None):
    raw = urllib.parse.urlencode(fields or {"csrf": t.csrf}).encode()
    return call(
        t.server,
        "POST",
        "/" + language + "/account/devin-trial/" + suffix,
        raw,
        {
            "Cookie": t.cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
    )


def token(t):
    status, headers, raw = form(t)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    found = re.findall(rb"cct_[0-9a-f]{64}", raw)
    assert len(set(found)) == 1
    return found[0].decode()


def test_runtime_limit_has_clear_reset_message_without_worker_contact_or_spending(http):
    key = token(http)
    principal = devin_trial.VerifiedAccount(http.alice["id"], True)
    with http.runtime._ledger() as book:
        book.allowance(principal)
        day = int(http.now[0] // 86400)
        book.connection.execute(
            "UPDATE devin_trial_account_days SET runtime_seconds=? WHERE account_id=? AND utc_day=?",
            (devin_trial.DAILY_RUNTIME_S - 80, principal.account_id, day),
        )
        book.connection.execute(
            "UPDATE devin_trial_global_days SET runtime_seconds=? WHERE utc_day=?",
            (devin_trial.GLOBAL_DAILY_RUNTIME_S - 80, day),
        )
        book.connection.commit()
        before = list(book.connection.iterdump())
    http.runtime.transport = lambda *a, **k: pytest.fail("exhausted quota contacted worker")
    status, headers, raw = call(http.server, "POST", "/trial/v1/messages", body(), {
        "x-api-key": key, "Content-Type": "application/json",
    })
    assert status == 429 and headers["x-should-retry"] == "false"
    error = json.loads(raw)["error"]["message"]
    assert "340-second reservation" in error and "00:00 UTC" in error
    assert "Remaining tokens do not override" in error
    assert "runtime_reservation_limit" not in error
    with http.runtime._ledger() as book:
        assert list(book.connection.iterdump()) == before


def test_nested_anthropic_web_search_stays_rejected_before_runtime_or_worker(http):
    key = token(http)
    with http.runtime._ledger() as book:
        before = list(book.connection.iterdump())
    http.runtime.transport = lambda *a, **k: pytest.fail("server tool request contacted worker")
    raw = body(tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}])
    status, headers, response = call(http.server, "POST", "/trial/v1/messages", raw, {
        "x-api-key": key, "Content-Type": "application/json",
    })
    assert status == 400 and headers["x-should-retry"] == "false"
    assert b"Deferred, server-side or other extended tool declarations" in response
    with http.runtime._ledger() as book:
        assert list(book.connection.iterdump()) == before


@pytest.mark.parametrize("block", [
    {"type": "server_tool_use", "id": "srvtoolu_old", "name": "web_search",
     "input": {"query": "synthetic query"}},
    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_old", "content": []},
])
def test_resumed_server_web_history_rejects_without_worker_or_allowance_change(http, block):
    key = token(http)
    with http.runtime._ledger() as book:
        before = list(book.connection.iterdump())
    http.runtime.transport = lambda *a, **k: pytest.fail("unsupported history contacted worker")
    raw = body(messages=[{"role": "user", "content": "synthetic question"},
                         {"role": "assistant", "content": [block]},
                         {"role": "user", "content": "hello"}])
    status, headers, _ = call(http.server, "POST", "/trial/v1/messages", raw, {
        "x-api-key": key, "Content-Type": "application/json",
    })
    assert status == 400 and headers["x-should-retry"] == "false"
    with http.runtime._ledger() as book:
        assert list(book.connection.iterdump()) == before


def test_plain_declared_mcp_network_tool_is_accepted(http):
    key = token(http)
    raw = body(tools=[{"name": "mcp__network__Search", "description": "Local search tool",
                      "input_schema": {"type": "object", "properties": {
                          "query": {"type": "string"}}, "required": ["query"]}}])
    status, _, response = call(http.server, "POST", "/trial/v1/messages", raw, {
        "x-api-key": key, "Content-Type": "application/json",
    })
    assert status == 200 and json.loads(response)["stop_reason"] == "end_turn"
    allowance = http.runtime.allowance(http.alice)
    assert not allowance["paused"] and allowance["remaining_requests"] == devin_trial.DAILY_REQUESTS - 1


def test_trial_get_signin_link_no_purchase_and_own_key_persists_after_refresh(http, caplog):
    t = http
    status, headers, raw = call(t.server, "GET", "/en/account/devin-trial")
    assert status == 200 and b"cct_" not in raw
    href = re.search(r'href="([^"]*auth/google/start[^"]*)"', raw.decode())[1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(href)).query)
    assert query["next"] == ["/en/account/devin-trial"]
    key = token(t)
    with t.runtime.keys._db() as access:
        before = list(access.iterdump())
    allowance = t.runtime.allowance(t.alice)
    t.runtime.transport = lambda *a, **k: pytest.fail("page retrieval contacted the worker")
    for language in ("en", "zh", "en"):
        status, headers, raw = call(t.server, "GET", f"/{language}/account/devin-trial",
                                    headers={"Cookie": t.cookie})
        assert status == 200 and set(re.findall(rb"cct_[0-9a-f]{64}", raw)) == {key.encode()}
        assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
        assert b"Trial key created" not in raw and "试用密钥已创建" not in raw.decode()
    assert b"1,000,000" in raw and b"10,000,000" in raw
    with t.runtime.keys._db() as access:
        assert list(access.iterdump()) == before
    assert t.runtime.allowance(t.alice) == allowance
    assert key not in caplog.text
    assert not t.store.list_slots(held_by=t.alice["id"])
    assert t.store.list_gateway_keys(t.alice["id"]) == []
    assert t.runtime.keys.resolve(key)["account_id"] == t.alice["id"]


def test_trial_command_get_never_reveals_another_accounts_key(http):
    alice_key = token(http)
    bob_key = http.runtime.create_key(http.bob)
    session = http.store.create_session(http.bob["id"], now=time.time(), ttl_s=86400)
    bob_cookie = sessions.COOKIE_NAME + "=" + sessions.sign(session, http.cfg.cookie_secret)
    status, _, raw = call(http.server, "GET",
                          "/account/devin-trial?account_id=" + http.alice["id"],
                          headers={"Cookie": bob_cookie})
    assert status == 200 and bob_key.encode() in raw and alice_key.encode() not in raw
    for headers in ({}, {"Authorization": "Bearer " + alice_key},
                    {"Authorization": "Basic YWRtaW46YWRtaW4="},
                    {"Cookie": sessions.COOKIE_NAME + "=forged.signature"}):
        status, _, raw = call(http.server, "GET", "/account/devin-trial", headers=headers)
        assert status == 200 and b"cct_" not in raw


def test_trial_legacy_key_get_does_not_rotate_or_reconstruct_it(http):
    legacy = "cct_" + "a" * 64
    with http.runtime.keys._db() as access:
        access.execute("INSERT INTO trial_access VALUES(?,?,?,?,?,0)", (
            "b" * 32, http.alice["id"], hashlib.sha256(legacy.encode()).hexdigest(),
            http.now[0], http.now[0] + 30 * 86400))
        before = list(access.iterdump())
    status, _, raw = call(http.server, "GET", "/account/devin-trial", headers={"Cookie": http.cookie})
    assert status == 200 and b"cct_" not in raw
    assert http.runtime.keys.resolve(legacy)["account_id"] == http.alice["id"]
    with http.runtime.keys._db() as access:
        assert list(access.iterdump()) == before


@pytest.mark.parametrize(
    "fields",
    [{"csrf": "wrong"}, {"csrf": "", "account_id": "bob"}, {"csrf": "bogus", "provider": "devin"}],
)
def test_trial_create_foreign_csrf_and_user_selected_identity_rejected(http, fields):
    assert form(http, fields=fields)[0] == 403
    with http.runtime.keys._db() as db:
        assert db.execute("SELECT COUNT(*) FROM trial_access").fetchone()[0] == 0


def test_trial_browser_request_requires_session_not_admin_token(http):
    raw = urllib.parse.urlencode({"csrf": http.csrf})
    assert (
        call(
            http.server,
            "POST",
            "/account/devin-trial/create",
            raw,
            {"Authorization": "Bearer admin", "Content-Type": "application/x-www-form-urlencoded"},
        )[0]
        == 401
    )


@pytest.mark.parametrize("raw", [b"csrf=x&csrf=y", b"csrf=\xff", b"csrf=%FF"])
def test_trial_forms_strict_duplicates_utf8_do_not_create_key(http, raw):
    assert (
        call(
            http.server,
            "POST",
            "/account/devin-trial/create",
            raw,
            {"Cookie": http.cookie, "Content-Type": "application/x-www-form-urlencoded"},
        )[0]
        == 400
    )


def test_model_http_streaming_success_has_all_usage_and_never_owner_context(http, caplog):
    t = http
    key = token(t)
    seen = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            assert authorized()
            self.model_started = True
            seen.append((self.target, json.loads(raw)))
            return receipt()

    t.runtime.transport = Worker
    status, headers, raw = call(
        t.server,
        "POST",
        "/trial/v1/messages?beta=true",
        body(stream=True, metadata={"user_id": "PRIVATE_USER"}),
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    assert status == 200 and headers["Content-Type"] == "text/event-stream"
    assert headers["x-should-retry"] == "false" and b"message_stop" in raw
    assert b"PRIVATE ANSWER" not in raw  # Fake receipt is the sole buffered response.
    assert "_native_context" not in seen[0][0] and "metadata" not in seen[0][1]
    assert t.runtime.allowance(t.alice)["reported_tokens"] == 19
    assert key not in caplog.text and "PRIVATE_USER" not in caplog.text


@pytest.mark.parametrize("path", ["/trial/v1/messages", "/trial/v1/messages?beta=true"])
def test_trial_request_over2mib_uses_context_recovery_before_rate_or_worker(http, path):
    key = token(http)
    before = http.runtime.allowance(http.alice)
    http.runtime.transport = lambda *a, **k: pytest.fail("oversized request reached worker")
    status, headers, raw = call(http.server, "POST", path,
                               b" " * (devin_trial.MAX_REQUEST_BYTES + 1), {
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    error = json.loads(raw)["error"]
    assert status == 400 and error["type"] == "invalid_request_error"
    assert error["code"] == "trial_context_too_large"
    assert error["message"].startswith("prompt is too long:")
    assert "2 MiB of request JSON including tools and history" in error["message"]
    assert "No model request was sent" in error["message"]
    assert headers["x-should-retry"] == "false"
    assert key not in raw.decode() and http.runtime.allowance(http.alice) == before
    with http.runtime.keys._db() as access:
        assert access.execute("SELECT COUNT(*) FROM trial_api_rate").fetchone()[0] == 0
    with http.runtime._ledger() as ledger:
        assert ledger.connection.execute("SELECT COUNT(*) FROM devin_trial_sessions").fetchone()[0] == 0


def test_trial_request_at2mib_reaches_exactly_one_worker(http):
    key = token(http)
    requests = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            requests.append(raw)
            return super().perform(raw, authorized)

    http.runtime.transport = Worker
    raw = body()
    padded = raw + b" " * (devin_trial.MAX_REQUEST_BYTES - len(raw))
    assert len(padded) == 2 * 1024 * 1024
    status, headers, _ = call(http.server, "POST", "/trial/v1/messages", padded, {
        "x-api-key": key, "Content-Type": "application/json",
    })
    assert status == 200 and headers["x-should-retry"] == "false"
    assert len(requests) == 1
    assert json.loads(requests[0])["messages"] == json.loads(raw)["messages"]
    assert http.runtime.allowance(http.alice)["remaining_requests"] == devin_trial.DAILY_REQUESTS - 1
    with http.runtime._ledger() as ledger:
        assert ledger.connection.execute("SELECT COUNT(*) FROM devin_trial_sessions").fetchone()[0] == 1


def test_empty_trial_request_is_a_framing_error_without_context_overflow(http):
    key = token(http)
    before = http.runtime.allowance(http.alice)
    http.runtime.transport = lambda *a, **k: pytest.fail("empty request reached worker")
    status, headers, raw = call(http.server, "POST", "/trial/v1/messages", b"", {
        "x-api-key": key, "Content-Type": "application/json",
    })
    error = json.loads(raw)["error"]
    assert status == 400 and error["type"] == "invalid_request_error"
    assert error["message"] == "invalid_trial_request_framing"
    assert "prompt is too long" not in raw.decode() and "code" not in error
    assert headers["x-should-retry"] == "false"
    assert http.runtime.allowance(http.alice) == before


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.invalid"},
        {"Cookie": "session=browser"},
        {"Content-Encoding": "gzip"},
        {"Content-Type": "text/plain"},
        {"Authorization": "Bearer OWNER_GATEWAY_KEY"},
        {"Authorization": "Bearer x", "x-api-key": "y"},
    ],
)
def test_trial_model_rejects_foreign_auth_or_browser_framing_before_worker(http, headers):
    key = token(http)
    values = {"Authorization": "Bearer " + key, "Content-Type": "application/json", **headers}
    assert call(http.server, "POST", "/trial/v1/messages", body(), values)[0] in {400, 401}
    assert http.runtime.allowance(http.alice)["remaining_requests"] == devin_trial.DAILY_REQUESTS


def test_trial_unsupported_history_has_static_actionable_code_before_admission(http, caplog):
    key = token(http)
    before = http.runtime.allowance(http.alice)
    http.runtime.transport = lambda *a, **k: pytest.fail("trial transport before rejection")
    raw = body(messages=[{"role": "user", "content": [
        {"type": "thinking", "thinking": "PRIVATE_HISTORY", "signature": "PRIVATE_SIGNATURE"}
    ]}])
    with caplog.at_level("INFO"):
        status, headers, response = call(http.server, "POST", "/trial/v1/messages", raw, {
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    error = json.loads(response)["error"]
    assert status == 400 and error["code"] == "unsupported_content"
    assert b"PRIVATE" not in response and b"reasoning shapes" in response
    assert headers["x-should-retry"] == "false"
    assert http.runtime.allowance(http.alice) == before
    assert [r.getMessage() for r in caplog.records if r.name == "ccfleetd.trial_routes"] == [
        "Devin request rejected before execution: code=unsupported_content"]
    assert "PRIVATE" not in caplog.text and key not in caplog.text


def test_trial_token_cannot_use_paid_slot_gateway_and_owner_key_cannot_use_trial(http):
    key = token(http)
    assert (
        call(
            http.server,
            "POST",
            "/v1/messages",
            body(),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )[0]
        >= 400
    )
    assert (
        call(
            http.server,
            "POST",
            "/trial/v1/messages/count_tokens",
            body(),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )[0]
        == 501
    )


def test_trial_revocation_is_csrf_owner_scoped_and_stops_local_key(http):
    key = token(http)
    other = http.runtime.create_key(http.bob)
    assert form(http, suffix="revoke")[0] == 200
    assert http.runtime.keys.resolve(key) is None and http.runtime.keys.resolve(other)
    status, _, raw = call(http.server, "GET", "/account/devin-trial", headers={"Cookie": http.cookie})
    assert status == 200 and b"cct_" not in raw
    assert (
        call(
            http.server,
            "POST",
            "/trial/v1/messages",
            body(),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )[0]
        == 401
    )


def test_replaced_trial_key_401_explains_fleet_recovery_without_claude_login(http, caplog):
    old = token(http)
    current = token(http)
    allowance = http.runtime.allowance(http.alice)
    http.runtime.transport = lambda *a, **k: pytest.fail("replaced key reached the worker")
    status, headers, raw = call(http.server, "POST", "/trial/v1/messages", body(), {
        "Authorization": "Bearer " + old, "Content-Type": "application/json"})
    error = json.loads(raw)["error"]
    assert status == 401 and error["type"] == "authentication_error"
    assert "Fleet trial key" in error["message"] and "expired or was replaced" in error["message"]
    assert "Open your Fleet trial page" in error["message"]
    assert "Claude /login does not restore" in error["message"]
    assert headers["x-should-retry"] == "false"
    assert old.encode() not in raw and current.encode() not in raw
    assert old not in caplog.text and current not in caplog.text
    assert http.runtime.allowance(http.alice) == allowance
    assert http.runtime.launch_key(http.alice) == current


@pytest.mark.parametrize("language,other", [("en", "zh"), ("zh", "en")])
@pytest.mark.parametrize("action", ["create", "revoke"])
def test_trial_result_language_switch_stays_on_trial_without_replaying_action(http, language, other, action):
    status, _, raw = form(http, suffix=action, language=language)
    assert status == 200
    link = re.search(r'<a[^>]*href="([^"]+)"[^>]*data-language-link="' + other + '"', raw.decode())
    assert link and html.unescape(link[1]) == f"/{other}/account/devin-trial"
    issued = set(re.findall(rb"cct_[0-9a-f]{64}", raw))
    status, headers, switched = call(http.server, "GET", link[1], headers={"Cookie": http.cookie})
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert set(re.findall(rb"cct_[0-9a-f]{64}", switched)) == issued
    assert b"Trial key created" not in switched and "试用密钥已创建" not in switched.decode()
    assert all(http.runtime.keys.resolve(key.decode()) is not None for key in issued)


def test_paused_trial_key_creation_is_explicit_and_does_not_resume_model_work(http):
    with http.runtime._ledger() as ledger:
        ledger.pause()
    status, _, raw = form(http)
    assert status == 200 and b"cct_" in raw
    assert b"key" in raw.lower() and b"paused" in raw.lower()
    assert http.runtime.allowance(http.alice)["paused"] is True
    key = next(iter(set(re.findall(rb"cct_[0-9a-f]{64}", raw)))).decode()
    status, _, raw = call(http.server, "POST", "/trial/v1/messages", body(),
                          {"x-api-key": key, "Content-Type": "application/json"})
    assert status == 429 and b"paused" in raw


@pytest.mark.parametrize(
    "language,visible,other",
    [
        ("en", "Try Devin in Claude Code", "在 Claude Code 中试用 Devin"),
        ("zh", "在 Claude Code 中试用 Devin", "Try Devin in Claude Code"),
    ],
)
def test_trial_language_privacy_and_entry_exact_selected_language(http, language, visible, other):
    status, _, raw = call(
        http.server, "GET", f"/{language}/account/devin-trial", headers={"Cookie": http.cookie}
    )
    assert status == 200 and visible in raw.decode() and other not in raw.decode()
    assert 'href="/' + language + '/account/devin-trial/create"' not in raw.decode()
    prefix = "/zh" if language == "zh" else ""
    assert f'action="{prefix}/account/devin-trial/create"' in raw.decode()
    _, _, raw = call(http.server, "GET", f"/{language}/privacy")
    assert (
        "Fleet-managed provider" if language == "en" else "Fleet 管理的服务账号"
    ) in raw.decode()
    status, _, raw = call(
        http.server, "GET", f"/{language}/account", headers={"Cookie": http.cookie}
    )
    assert status == 200 and f'href="{prefix}/account/devin-trial"' in raw.decode()


def test_trial_api_hidden_on_broker_and_operator_host(http):
    cfg = replace(http.cfg, broker_only=True, admin_host="admin.invalid")
    ctx = Context(http.store, cfg, None, trial=http.runtime)
    server = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert call(server, "GET", "/account/devin-trial")[0] == 404
        assert (
            call(
                server, "POST", "/trial/v1/messages", body(), {"Content-Type": "application/json"}
            )[0]
            == 404
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        ctx.gateway.close()


def test_busy_is_presend_retry_after_with_no_automatic_replay(http):
    entered, release = threading.Event(), threading.Event()
    seen = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            self.model_started = True
            seen.append(raw)
            entered.set()
            assert release.wait(4)
            return receipt()

    http.runtime.transport = Worker
    alice, bob = token(http), http.runtime.create_key(http.bob)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            call(
                http.server,
                "POST",
                "/trial/v1/messages",
                body(),
                {"x-api-key": alice, "Content-Type": "application/json"},
            )
        )
    )
    thread.start()
    assert entered.wait(3)
    status, headers, raw = call(
        http.server,
        "POST",
        "/trial/v1/messages",
        body(),
        {"x-api-key": bob, "Content-Type": "application/json"},
    )
    assert status == 429 and headers["Retry-After"] == "3"
    assert headers["x-should-retry"] == "false" and b"worker is busy" in raw
    assert len(seen) == 1
    release.set()
    thread.join(5)
    assert results[0][0] == 200


def test_enabled_trial_explanation_on_homepage_and_disabled_absence(http):
    status, _, raw = call(http.server, "GET", "/en/")
    assert status == 200 and b'href="/account/devin-trial"' in raw
    assert b"Fleet-managed provider" in raw
    from ccfleetd import trial_routes

    assert trial_routes.entry(replace(http.cfg, devin_trial_enabled=False)) == ""
    assert "Fleet-managed" not in usersite.privacy_page(
        replace(http.cfg, devin_trial_enabled=False)
    )


def test_opus_model_env_names_are_fixed_and_command_contains_no_owner_binding(http):
    import html as html_module
    import shlex

    status, _, raw = form(http)
    assert status == 200
    command = html_module.unescape(re.search(r"<pre><code>(.*?)</code></pre>", raw.decode())[1])
    words = shlex.split(command)
    executable = words.index("claude")
    assert words[0] == "env" and words[executable:] == [
        "claude",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "max",
                "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        "WebSearch,WebFetch",
    ]
    pairs = dict(part.split("=", 1) for part in words[1:executable])
    assert pairs["ANTHROPIC_BASE_URL"] == "https://fleet.invalid/trial"
    assert pairs["ANTHROPIC_AUTH_TOKEN"].startswith("cct_") and pairs["ANTHROPIC_API_KEY"] == ""
    assert pairs["ENABLE_TOOL_SEARCH"] == "false"
    assert pairs["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert pairs["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"
    assert pairs["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    for name in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert pairs[name] == "claude-opus-5-5"
    assert all(
        part not in command
        for part in ("slot_id", "claim_id", "provider_account_fp", "gateway-key")
    )
