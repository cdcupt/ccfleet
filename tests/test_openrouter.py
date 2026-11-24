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


def api(c, token, body=None, path=None, *, headers=None):
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
            **(headers or {}),
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


@pytest.mark.parametrize("nested", [False, True])
def test_image_history_survives_switching_input_modes(router, provider, nested):
    c = router
    grant(c)
    token = c.store.openrouter_key("alice")
    rows = json.loads(api(c, token)[2])["data"]
    assert [r["input_modalities"] for r in rows] == [["text", "image"], ["text"]]
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
           "/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
    content = [{"type": "text", "text": "Screenshot"}, image]
    message = [{"type": "tool_result", "tool_use_id": "t1", "content": content}] if nested else content
    turns = [{"role": "user", "content": message}]
    if nested:
        turns.insert(0, {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "id": "t1", "input": {}}
        ]})
    calls, replies = provider
    for i, headers in enumerate(({}, {"X-CCFleet-Input": "images"}, {})):
        replies.append((200, "application/json", json.dumps({
            "id": f"gen-image-{i}", "type": "message", "usage": {"cost": 0}
        }).encode()))
        assert api(c, token, payload(MODELS[0], messages=turns), headers=headers)[0] == 200
        body = calls[-1][2]
        blocks = body["messages"][-1]["content"]
        if nested:
            assert blocks[0]["tool_use_id"] == "t1"
            blocks = blocks[0]["content"]
        assert blocks[0] == content[0]
        if headers:
            assert blocks[1] == image
            assert body["provider"]["max_price"]["image"] == 0
            assert "X-CCFleet-Input" not in calls[-1][1]
        else:
            assert png not in repr(body)
            assert blocks[1]["type"] == "text" and "omitted image" in blocks[1]["text"]


def test_image_mode_validates_model_and_inline_source_before_billing(router, provider):
    c = router
    grant(c)
    token = c.store.openrouter_key("alice")
    headers = {"X-CCFleet-Input": "images"}
    status, _, msg = api(c, token, payload(MODELS[1]), headers=headers)
    assert status == 400 and b"does not accept image" in msg
    for src in ({"type": "url", "url": "https://example.org/picture.png"},
                {"type": "base64", "media_type": "image/png", "data": "invalid!"},
                {"type": "base64", "media_type": "image/png", "data": "bm90IGEgcG5n"}):
        body = payload(MODELS[0], messages=[{"role": "user", "content": [
            {"type": "image", "source": src}
        ]}])
        assert api(c, token, body, headers=headers)[0] == 400
    assert not provider[0] and not c.store.openrouter_receipts("alice")


def test_image_wallet_hold_uses_visual_context_only_for_forwarded_images(router, provider):
    c = router
    grant(c, "0.50")
    token = c.store.openrouter_key("alice")
    body = payload(MODELS[0], messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/gif",
                                     "data": "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="}}
    ]}])
    assert api(c, token, body, headers={"X-CCFleet-Input": "images"})[0] == 402
    assert not provider[0] and not c.store.openrouter_receipts("alice")
    provider[1].append((200, "application/json", b'{"id":"gen-text","type":"message","usage":{"cost":0}}'))
    assert api(c, token, body)[0] == 200
    assert c.store.wallet_balance("alice") == 50


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
    assert result.status == 303
    assert result.getheader("Cache-Control") == "no-store"
    assert result.getheader("Location") == "/account/openrouter"
    assert "ccf_or_" not in (result.getheader("Location") or "")
    landed = user.call("GET", result.getheader("Location"), headers=c.public)
    assert landed.status == 200 and landed.getheader("Cache-Control") == "no-store"
    [key] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", landed.body)
    status, _, catalog = api(c, key)
    assert status == 200 and [r["id"] for r in json.loads(catalog)["data"]] == MODELS
    assert key not in user.call("GET", "/account/openrouter", headers=c.public).body
    new = c.store.openrouter_key("alice")
    assert api(c, key)[0] == 401
    assert c.store.openrouter_principal(new) == "alice"
    assert not c.store.resolve_gateway_key(new, now=time.time())
    revoked = user.call(
        "POST",
        "/account/openrouter/revoke",
        form={"csrf": usersite.csrf_for(user.session, c.cfg.cookie_secret)},
        headers=c.public,
    )
    assert revoked.status == 303 and revoked.getheader("Location") == "/account/openrouter"
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
    safeguard = {"type": "dangerous_tool_use", "classifier_context": {"permission_mode": "auto"}}
    body = payload(MODELS[0], tools=[tool], safeguards=[safeguard], metadata={"user_id": "private"})
    body["messages"].append({"role": "system", "content": [{"type": "text", "text": "Use local tools."}]})
    assert json.loads(api(c, key, body)[2])["content"] == content
    assert calls[0][0] == "/api/v1/messages"
    assert calls[0][1]["Authorization"] == "Bearer sk-or-synthetic-test-only"
    assert key not in repr(calls) and "PRIVATE-CLIENT" not in repr(calls)
    assert "metadata" not in calls[0][2] and calls[0][2]["model"] == MODELS[0]
    assert calls[0][2]["safeguards"] == [safeguard]
    assert calls[0][2]["messages"] == body["messages"]
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
    c.store.openrouter_submit(rid, "alice", token)
    c.store.openrouter_identify(rid, "gen-wallet")
    for _ in range(2):
        c.store.openrouter_settle(rid, "gen-wallet", Decimal("1.00"))
    with pytest.raises(BillingError, match="Conflicting"):
        c.store.openrouter_settle(rid, "gen-wallet", Decimal("0.50"))
    receipt = c.store.openrouter_receipts("alice")[0]
    assert receipt["debit_cents"] == 105 and Decimal(receipt["fee_usd"]) == Decimal(".05")
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

def test_rotated_key_cannot_submit_and_ambiguous_hold_needs_review(router):
    c = router
    grant(c)
    token = c.store.openrouter_key("alice")
    stale = c.store.openrouter_reserve("alice", token, MODELS[0], 5)
    new = c.store.openrouter_key("alice")
    with pytest.raises(BillingError):
        c.store.openrouter_submit(stale, "alice", token)
    c.store.openrouter_release(stale)
    rid = c.store.openrouter_reserve("alice", new, MODELS[0], 5)
    c.store.openrouter_submit(rid, "alice", new)
    c.store.openrouter_release(rid)
    assert c.store.wallet_balance("alice") == 2995
    with pytest.raises(BillingError):
        c.store.openrouter_void_unbilled(rid, "Synthetic provider evidence", 0)
    c.store.openrouter_void_unbilled(rid, "Synthetic provider evidence", time.time() + 1)
    assert c.store.wallet_balance("alice") == 3000

def test_zero_priced_model_has_zero_hold_and_zero_fee(router, provider):
    calls, replies = provider
    router.service.models[MODELS[0]] = (Decimal(0), Decimal(0))
    token = router.store.openrouter_key("alice")
    replies.append((200, "application/json", b'{"type":"message","id":"gen-free","usage":{"cost":0}}'))
    assert api(router, token, payload(MODELS[0]))[0] == 200
    receipt = router.store.openrouter_receipts("alice")[0]
    assert receipt["state"] == "settled" and receipt["hold_cents"] == 0
    assert Decimal(receipt["fee_usd"]) == 0 and receipt["debit_cents"] == 0
    assert calls[0][2]["provider"]["max_price"]["completion"] == 0


def test_recent_catalog_is_personal_deduplicated_and_limited_to_ten(router):
    c = router
    offered = [f"example/model-{n}" for n in range(13)]
    c.service.models = dict.fromkeys(offered, (Decimal(0), Decimal(0)))
    alice = c.store.openrouter_key("alice")
    bob = c.store.openrouter_key("bob")

    def submit(model):
        rid = c.store.openrouter_reserve("alice", alice, model, 0)
        c.store.openrouter_submit(rid, "alice", alice)
        c.store.openrouter_identify(rid, "gen-" + rid)
        return rid

    def settle(rid):
        c.store.openrouter_settle(rid, "gen-" + rid, Decimal(0))

    pending = submit(offered[0])
    for model in offered[1:]:
        settle(submit(model))
    settle(pending)  # Reconciliation can finish out of order.
    settle(submit(offered[4]))
    removed = offered[-1]
    del c.service.models[removed]
    submit(offered[0])  # An unresolved request does not move this model to the front.
    c.store.openrouter_release(c.store.openrouter_reserve("alice", alice, offered[0], 0))
    new_key = c.store.openrouter_key("alice")

    status, headers, raw = api(c, new_key)
    catalog = json.loads(raw)
    rows = catalog["data"]
    recent = [offered[n] for n in (4, 11, 10, 9, 8, 7, 6, 5, 3, 2)]
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert catalog["first_id"] == recent[0] and catalog["last_id"] == offered[1]
    assert [r["id"] for r in rows if r["fleet_recent"]] == recent
    assert [r["id"] for r in rows] == [*recent, offered[0], offered[1]]
    assert api(c, alice)[0] == 401
    fresh = json.loads(api(c, bob)[2])["data"]
    assert [r["id"] for r in fresh] == offered[:-1]
    assert not any(r["fleet_recent"] for r in fresh)


def test_only_configured_admin_has_no_fleet_charge(router, provider):
    calls, replies = provider
    store = router.store
    router.service.cfg = replace(router.service.cfg, openrouter_admin_account_id="admin")
    admin_key = store.openrouter_key("admin")
    regular_key = store.openrouter_key("alice")
    assert json.loads(api(router, admin_key)[2])["fleet_billing"] == "admin_covered"
    assert json.loads(api(router, regular_key)[2])["fleet_billing"] == "usage_plus_fee"
    assert api(router, regular_key, payload(MODELS[0]))[0] == 402
    replies.append((200, "application/json", b'{"type":"message","id":"gen-admin","usage":{"cost":1.0}}'))
    assert api(router, admin_key, payload(MODELS[0]))[0] == 200
    assert len(calls) == 1
    [receipt] = store.openrouter_receipts("admin")
    assert receipt["state"] == "settled" and Decimal(receipt["cost_usd"]) == 1
    assert receipt["wallet_exempt"] and receipt["hold_cents"] == receipt["debit_cents"] == 0
    assert Decimal(receipt["fee_usd"]) == 0 and store.wallet_balance("admin") == 0
    page = router.browser("admin").call("GET", "/account/openrouter", headers=router.public)
    assert "Fleet covers this admin account" in page.body and "$0.00" in page.body
    store.set_account_role("alice", "admin")
    assert api(router, regular_key, payload(MODELS[0]))[0] == 402
    rid = store.openrouter_reserve("admin", admin_key, MODELS[0], 25, admin_account_id="admin")
    store.set_account_role("admin", "user")
    with pytest.raises(BillingError):
        store.openrouter_submit(rid, "admin", admin_key)
    store.openrouter_release(rid)
    assert api(router, admin_key, payload(MODELS[0]))[0] == 402


@pytest.mark.parametrize(
    ("prefix", "expected_location"),
    [("", "/account/openrouter"), ("/en", "/en/account/openrouter"), ("/zh", "/zh/account/openrouter")],
)
def test_openrouter_create_prg_dedup_refresh_and_intentional_rotation(
    router, prefix, expected_location
):
    c = router
    user = c.browser("alice")
    other_session = c.browser("alice")

    # 1. Load initial page and extract form (csrf + single-use request_id)
    initial = user.call("GET", prefix + "/account/openrouter", headers=c.public)
    assert initial.status == 200 and initial.getheader("Cache-Control") == "no-store"
    action = (prefix if prefix == "/zh" else "") + "/account/openrouter/create"
    form = form_from(initial.body, action)
    assert "csrf" in form and "request_id" in form and len(form["request_id"]) >= 16

    # Missing request_id is refused without rotating
    bad = user.call(
        "POST", prefix + "/account/openrouter/create", form={"csrf": form["csrf"]}, headers=c.public
    )
    assert bad.status == 400

    # 2. First deliberate submission -> 303 PRG redirect to /account/openrouter (no key in URL)
    first_post = user.call("POST", prefix + "/account/openrouter/create", form=form, headers=c.public)
    assert first_post.status == 303
    assert first_post.getheader("Location") == expected_location
    assert first_post.getheader("Cache-Control") == "no-store"
    assert first_post.getheader("Referrer-Policy") == "no-referrer"
    assert "ccf_or_" not in first_post.body and "ccf_or_" not in (first_post.getheader("Location") or "")

    # 3. Duplicate submission of the exact same form -> 303 redirect, NO rotation, reveal preserved
    dup_post = user.call("POST", prefix + "/account/openrouter/create", form=form, headers=c.public)
    assert dup_post.status == 303
    assert dup_post.getheader("Location") == expected_location
    assert dup_post.getheader("Cache-Control") == "no-store"

    # A different session for the same user cannot steal the one-time reveal
    other_view = other_session.call("GET", expected_location, headers=c.public)
    assert other_view.status == 200 and not re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", other_view.body)

    # 4. Issuing session follows redirect -> sees key ONCE
    landed = user.call("GET", expected_location, headers=c.public)
    assert landed.status == 200 and landed.getheader("Cache-Control") == "no-store"
    [key1] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", landed.body)
    assert api(c, key1)[0] == 200

    # Raw key must never be stored in SQLite
    dump = "\n".join(c.store._conn.iterdump())
    assert key1 not in dump

    # 5. Refreshing the landed page hides key1 while leaving key1 valid
    refreshed = user.call("GET", expected_location, headers=c.public)
    assert refreshed.status == 200 and refreshed.getheader("Cache-Control") == "no-store"
    assert key1 not in refreshed.body
    assert api(c, key1)[0] == 200

    # 6. Replaying the original form after refresh or hitting GET /create leaves key1 valid
    replay_after = user.call(
        "POST", prefix + "/account/openrouter/create", form=form, headers=c.public
    )
    assert replay_after.status == 303 and replay_after.getheader("Location") == expected_location
    after_replay = user.call("GET", expected_location, headers=c.public)
    assert key1 not in after_replay.body and not re.findall(
        r"ccf_or_[A-Za-z0-9_-]{43}", after_replay.body
    )
    get_create = user.call("GET", prefix + "/account/openrouter/create", headers=c.public)
    assert get_create.status == 303 and get_create.getheader("Location") == expected_location
    assert api(c, key1)[0] == 200

    # 7. Fresh intentional rotation form rotates the key: key1 -> 401, key2 -> 200
    rotate_form = form_from(refreshed.body, action)
    assert rotate_form["request_id"] != form["request_id"]
    rotated_post = user.call(
        "POST", prefix + "/account/openrouter/create", form=rotate_form, headers=c.public
    )
    assert rotated_post.status == 303 and rotated_post.getheader("Location") == expected_location
    rotated_page = user.call("GET", expected_location, headers=c.public)
    [key2] = re.findall(r"ccf_or_[A-Za-z0-9_-]{43}", rotated_page.body)
    assert key2 != key1
    assert api(c, key1)[0] == 401
    assert api(c, key2)[0] == 200
    assert key2 not in user.call("GET", expected_location, headers=c.public).body
    assert api(c, key2)[0] == 200
