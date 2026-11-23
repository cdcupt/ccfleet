"""The DMIT route needs a Fleet wallet, never a subscription or execution node."""

import http.client
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ccfleetd import openrouter, usersite
from ccfleetd.config import Config, ConfigError
from ccfleetd.openrouter_billing import BillingError
from tests.test_wallet_http import form_from, grant
from tests.test_wallet_http import web as web  # noqa: F401

MODELS = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-pro"]
RATES = json.dumps(dict.fromkeys(MODELS, [1, 2]))


@pytest.fixture
def router(web, tmp_path):
    path = tmp_path / "router.key"
    path.write_text("sk-or-synthetic-test-only")
    path.chmod(0o600)
    cfg = replace(web.cfg, openrouter_key_file=str(path), openrouter_models=RATES)
    web.ctx.cfg = cfg
    service = openrouter.Service(cfg, web.store)
    web.ctx.gateway.openrouter = service
    web.service = service
    return web


@pytest.fixture
def provider(router):
    calls = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, dict(self.headers), body))
            status, media, raw = replies.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "data": {
                            "id": "gen-interrupted",
                            "session_id": calls[-1][2]["session_id"],
                            "total_cost": 0.032,
                            "finish_reason": "stop",
                        }
                    }
                ).encode()
            )

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=srv.serve_forever, daemon=True)
    worker.start()
    router.service.connect = lambda: http.client.HTTPConnection(*srv.server_address, timeout=3)
    yield calls, replies
    srv.shutdown()
    srv.server_close()
    worker.join()


def api(c, token, body=None, path=None):
    conn = http.client.HTTPConnection(*c.server.server_address, timeout=5)
    path = path or ("/openrouter/v1/models" if body is None else "/openrouter/v1/messages")
    conn.request(
        "GET" if body is None else "POST",
        path,
        body=json.dumps(body) if body is not None else None,
        headers={
            **c.public,
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "PRIVATE-CLIENT",
        },
    )
    r = conn.getresponse()
    try:
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def payload(model, **opts):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Call a local tool."}],
        "max_tokens": 4096,
        **opts,
    }


def test_slotless_account_create_catalog_rotate_revoke(router):
    c = router
    assert c.store.list_slots(held_by="alice") == []
    user = c.browser()
    result = user.call("GET", "/account/openrouter", headers=c.public)
    assert result.status == 200 and "5%" in result.body
    form = form_from(result.body, "/account/openrouter/create")
    assert (
        user.call(
            "POST", "/account/openrouter/create", form={"csrf": "bad"}, headers=c.public
        ).status
        == 403
    )
    result = user.call("POST", "/account/openrouter/create", form=form, headers=c.public)
    assert result.getheader("Cache-Control") == "no-store"
    [key] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", result.body)
    status, _, catalog = api(c, key)
    assert status == 200 and [r["id"] for r in json.loads(catalog)["data"]] == MODELS
    assert key not in user.call("GET", "/account/openrouter", headers=c.public).body
    new = c.store.openrouter_key("alice")
    assert api(c, key)[0] == 401
    assert c.store.openrouter_principal(new) == "alice"
    assert not c.store.resolve_gateway_key(new, now=time.time())
    user.call(
        "POST",
        "/account/openrouter/revoke",
        form={"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)},
        headers=c.public,
    )
    assert api(c, new)[0] == 401


def test_direct_native_messages_model_switch_and_precise_fee(router, provider):
    c = router
    grant(c, "10")
    key = c.store.openrouter_key("alice")
    calls, replies = provider
    tool = {"name": "Read", "input_schema": {"type": "object"}}
    content = [{"type": "tool_use", "name": "Read", "id": "t1", "input": {}}]
    replies.append(
        (
            200,
            "application/json",
            json.dumps(
                {"type": "message", "id": "gen-first", "content": content, "usage": {"cost": 0.008}}
            ).encode(),
        )
    )
    body = payload(MODELS[0], tools=[tool], metadata={"user_id": "private"})
    assert json.loads(api(c, key, body)[2])["content"] == content
    assert calls[0][0] == "/api/v1/messages"
    assert calls[0][1]["Authorization"] == "Bearer sk-or-synthetic-test-only"
    assert key not in repr(calls) and "PRIVATE-CLIENT" not in repr(calls)
    assert "metadata" not in calls[0][2] and calls[0][2]["model"] == MODELS[0]
    body["messages"] += [
        {"role": "assistant", "content": content},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "fixture"}],
        },
    ]
    events = [
        {"type": "message_start", "message": {"id": "gen-second"}},
        {"type": "message_delta", "usage": {"output_tokens": 3, "cost": 0.012}},
        {"type": "message_stop"},
    ]
    stream = b": keepalive\r\n\r\n" + b"".join(
        b"data: " + json.dumps(e).encode() + b"\r\n\r\n" for e in events
    )
    replies.append((200, "text/event-stream", stream))
    status, headers, raw = api(c, key, {**body, "model": MODELS[1], "stream": True})
    assert status == 200 and headers["x-should-retry"] == "false" and b"event: message_stop" in raw
    assert calls[1][2]["messages"] == body["messages"] and calls[1][2]["model"] == MODELS[1]
    assert c.store.wallet_balance("alice") == 998
    assert [r["state"] for r in c.store.openrouter_receipts("alice")] == ["settled", "settled"]


def test_wallet_hold_settle_replay_and_concurrent_admission(router):
    c = router
    grant(c, "1.05")
    token = c.store.openrouter_key("alice")

    def reserve(_):
        try:
            return c.store.openrouter_reserve("alice", token, MODELS[0], 105)
        except BillingError:
            return None

    with ThreadPoolExecutor(2) as ex:
        [rid] = filter(None, ex.map(reserve, range(2)))
    assert c.store.wallet_balance("alice") == 0
    c.store.openrouter_submit(rid)
    c.store.openrouter_identify(rid, "gen-wallet")
    for _ in range(2):
        c.store.openrouter_settle(rid, "gen-wallet", Decimal("1.00"))
    with pytest.raises(BillingError, match="Conflicting"):
        c.store.openrouter_settle(rid, "gen-wallet", Decimal("0.50"))
    assert c.store.openrouter_receipts("alice")[0]["debit_cents"] == 105
    assert c.store.wallet_balance("alice") == 0


def test_bad_input_rejected_quota_and_missing_receipt(router, provider):
    c = router
    calls, replies = provider
    key = c.store.openrouter_key("alice")
    body = payload(MODELS[0])
    assert api(c, key, body)[0] == 402
    grant(c)
    for bad in (
        {"model": "unlisted/id"},
        {"models": ["other/id"]},
        {"provider": {"only": []}},
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    ):
        assert api(c, key, {**body, **bad})[0] == 400
    assert api(c, key, body, "/openrouter/v1/messages/count_tokens")[0] == 501
    assert not calls
    replies.append((429, "application/json", b'{"error":"private credentials"}'))
    status, headers, raw = api(c, key, body)
    assert status == 429 and headers["x-should-retry"] == "false" and b"private" not in raw
    assert c.store.wallet_balance("alice") == 3000
    body["max_tokens"] = 100000
    replies.append(
        (200, "application/json", b'{"type":"message","id":"gen-interrupted","usage":{}}')
    )
    assert api(c, key, body)[0] == 200
    row = c.store.openrouter_receipts("alice")[0]
    assert row["state"] == "submitted" and row["hold_cents"] > 3
    assert c.service.lookup(row["id"], row["generation_id"])
    assert c.store.wallet_balance("alice") == 2997
    assert len(calls) == 2


def test_config_fails_closed_and_secret_permissions(tmp_path):
    assert openrouter.parse_models("") == {}
    for bad in ('{"https://evil.test":[1,2]}', '{"deepseek/model":[-1,2]}', "[]"):
        with pytest.raises(ConfigError):
            openrouter.parse_models(bad)
    with pytest.raises(ConfigError):
        Config.from_env({"CCFLEET_OPENROUTER_MODELS": RATES})
    path = tmp_path / "credential"
    path.write_text("sk-or-synthetic-no-access")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        openrouter.read_key(path)
