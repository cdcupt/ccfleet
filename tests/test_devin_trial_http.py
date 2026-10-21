"""Real HTTP/account cookies/CSRF + actual trial ledger with fake worker only."""

from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.parse
from dataclasses import replace
from http.client import HTTPConnection

import pytest

from ccfleetd import sessions, usersite
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


def test_trial_get_signin_link_no_purchase_and_own_csrf_key_copy_once(http):
    t = http
    status, headers, raw = call(t.server, "GET", "/en/account/devin-trial")
    assert status == 200 and b"cct_" not in raw
    href = re.search(r'href="([^"]*auth/google/start[^"]*)"', raw.decode())[1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(href)).query)
    assert query["next"] == ["/en/account/devin-trial"]
    key = token(t)
    status, _, raw = call(t.server, "GET", "/en/account/devin-trial", headers={"Cookie": t.cookie})
    assert status == 200 and key.encode() not in raw and b"No slot purchase" in raw
    assert b"1,000,000" in raw and b"100,000" in raw
    assert not t.store.list_slots(held_by=t.alice["id"])
    assert t.store.list_gateway_keys(t.alice["id"]) == []
    assert t.runtime.keys.resolve(key)["account_id"] == t.alice["id"]


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
    assert http.runtime.allowance(http.alice)["remaining_requests"] == 20


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
    assert words[0] == "env" and words[-5:] == [
        "claude",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "max",
    ]
    pairs = dict(part.split("=", 1) for part in words[1:-5])
    assert pairs["ANTHROPIC_BASE_URL"] == "https://fleet.invalid/trial"
    assert pairs["ANTHROPIC_AUTH_TOKEN"].startswith("cct_") and pairs["ANTHROPIC_API_KEY"] == ""
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
