import pytest

from ccfleetd.store import Store, StoreError, hash_token


def test_add_node_returns_token_and_hides_hash(store):
    token = store.add_node("node-a", "erik", "us-west", "2.1.92", True, now=100.0)
    assert len(token) == 64 and int(token, 16) >= 0
    node = store.get_node("node-a")
    assert node["owner"] == "erik" and node["rc_expected"] is True and "token_hash" not in node
    assert store.list_nodes()[0]["id"] == "node-a"


@pytest.mark.parametrize("bad_id", ["A", "bad id", "-x", "x" * 41, ""])
def test_invalid_node_id_rejected(store, bad_id):
    with pytest.raises(StoreError):
        store.add_node(bad_id, "erik")


def test_duplicate_and_empty_owner_rejected(store):
    store.add_node("node-a", "erik")
    with pytest.raises(StoreError):
        store.add_node("node-a", "erik")
    with pytest.raises(StoreError):
        store.add_node("node-b", "   ")


def test_token_lookup_rotation_and_disable(store):
    token = store.add_node("node-a", "erik")
    assert store.node_for_token(token)["id"] == "node-a"
    assert store.node_for_token("0" * 64) is None
    assert store.node_for_token("") is None
    new_token = store.rotate_token("node-a")
    assert store.node_for_token(token) is None
    assert store.node_for_token(new_token)["id"] == "node-a"
    store.set_enabled("node-a", False)
    assert store.node_for_token(new_token) is None
    assert store.get_node("node-a")["enabled"] is False


def test_update_unknown_node_raises(store):
    with pytest.raises(StoreError):
        store.set_pinned_version("ghost", "1.0.0")
    with pytest.raises(StoreError):
        store.remove_node("ghost")


def test_heartbeats_recent_latest_and_prune(store):
    store.add_node("node-a", "erik")
    store.add_node("node-b", "sam")
    for ts in (10.0, 20.0, 30.0):
        store.insert_heartbeat("node-a", ts, {"seq": ts})
    store.insert_heartbeat("node-b", 5.0, {"seq": 5})
    recent = store.recent_heartbeats("node-a", limit=2)
    assert [h["payload"]["seq"] for h in recent] == [30.0, 20.0]
    latest = store.latest_heartbeats()
    assert latest["node-a"]["ts"] == 30.0 and latest["node-b"]["ts"] == 5.0
    assert store.prune_heartbeats(15.0) == 2
    assert store.recent_heartbeats("node-a", limit=5)[-1]["ts"] == 20.0


def test_remove_node_cascades(store):
    store.add_node("node-a", "erik")
    store.insert_heartbeat("node-a", 1.0, {})
    store.open_alert("node-a", "disk_high", "warn", "disk 90% used", 1.0)
    store.remove_node("node-a")
    assert store.get_node("node-a") is None
    assert store.recent_heartbeats("node-a") == []
    assert store.open_alerts() == []


def test_alert_lifecycle(store):
    store.add_node("node-a", "erik")
    alert_id = store.open_alert("node-a", "disk_high", "warn", "disk 90% used", 1.0)
    assert [a["rule"] for a in store.open_alerts("node-a")] == ["disk_high"]
    store.close_alert(alert_id, 2.0)
    assert store.open_alerts() == []
    assert store.recent_alerts()[0]["closed_at"] == 2.0


def test_hash_token_is_sha256_hex():
    assert len(hash_token("abc")) == 64


def test_file_backed_store_creates_parent(tmp_path):
    s = Store(str(tmp_path / "nested" / "fleet.db"))
    s.add_node("node-a", "erik")
    s.close()
    assert (tmp_path / "nested" / "fleet.db").exists()


@pytest.mark.parametrize("bad_owner", [
    "alice; rm -rf /",      # the injection the review found
    "alice owner",
    "Alice",
    "-alice",
    "a" * 33,
    "",
    "   ",
    "root$(id)",
    "_svc",   # adduser refuses a leading underscore, so the console must too
])
def test_owner_must_be_a_usable_unix_name(store, bad_owner):
    """The owner reaches a command an operator pastes as root, so it is validated."""
    with pytest.raises(StoreError):
        store.add_node("node-a", bad_owner)
    assert store.list_nodes() == []


@pytest.mark.parametrize("ok_owner", ["alice", "svc", "bob-2", "a", "a" * 32])
def test_reasonable_owner_names_are_accepted(store, ok_owner):
    store.add_node("node-a", ok_owner)
    assert store.get_node("node-a")["owner"] == ok_owner


def test_a_late_report_cannot_kill_the_attempt_that_replaced_it(store):
    """The node posts on its own schedule, so a stale report overtaking a new
    request is ordinary, not exotic."""
    store.add_node("node-a", "erik")
    store.request_login("node-a", "a@b.com", 100.0)
    first = store.get_login("node-a")["requested_at"]

    # The owner gives up and starts again.
    store.request_login("node-a", "a@b.com", 200.0)
    assert store.get_login("node-a")["requested_at"] == 200.0

    # The abandoned attempt finally reports. It must not delete the live one.
    store.record_login_progress("node-a", "done", "", "", 201.0, requested_at=first)
    assert store.get_login("node-a") is not None
    assert store.get_login("node-a")["requested_at"] == 200.0

    # Nor overwrite its state with its own.
    store.record_login_progress("node-a", "url_ready", "https://claude.ai/old", "",
                                201.0, requested_at=first)
    assert store.get_login("node-a")["url"] == ""

    # The live attempt is still perfectly able to finish.
    store.record_login_progress("node-a", "done", "", "", 202.0, requested_at=200.0)
    assert store.get_login("node-a") is None


def test_the_code_is_cleared_the_moment_the_node_says_it_used_it(store):
    store.add_node("node-a", "erik")
    store.request_login("node-a", "a@b.com", 100.0)
    store.record_login_progress("node-a", "url_ready", "https://claude.ai/x", "", 101.0)
    store.submit_login_code("node-a", "the-code", 102.0)
    assert store.get_login("node-a")["code"] == "the-code"
    store.record_login_progress("node-a", "code_sent", "", "", 103.0)
    row = store.get_login("node-a")
    assert row["state"] == "code_sent"
    assert row["code"] == "", "a consumed code must not sit in the database"


def test_progress_for_a_node_with_no_attempt_is_simply_ignored(store):
    store.add_node("node-a", "erik")
    store.record_login_progress("node-a", "url_ready", "https://claude.ai/x", "", 1.0)
    assert store.get_login("node-a") is None


def test_the_login_path_is_safe_under_concurrent_use(store):
    """The store shares one connection across threads under a lock.

    The login methods first used `with self._conn:` instead, which is a different
    transaction mechanism; two threads in it at once raised "cannot start a
    transaction within a transaction". Local tests passed and CI caught it.
    """
    import threading
    import time
    store.add_node("node-a", "erik")
    errors = []

    def hammer(i):
        try:
            for _ in range(30):
                store.request_login("node-a", f"a{i}@b.com", time.time())
                store.get_login("node-a")
                store.record_login_progress("node-a", "url_ready",
                                            "https://claude.ai/x", "", time.time())
                store.expire_logins(0)
                store.clear_login("node-a")
        except Exception as exc:                      # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# -- device tokens ----------------------------------------------------------------

def _node(store, node_id="node-a"):
    store.add_node(node_id, "erik")
    return node_id


def test_a_token_request_is_a_sign_in_with_a_different_errand(store):
    """One table drives both flows; the kind is what tells them apart."""
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    assert store.get_login(n)["kind"] == "token"
    # The default stays the sign-in, so every existing caller is unchanged.
    store.request_login(n, "e@x.com", 200.0)
    assert store.get_login(n)["kind"] == "login"
    with pytest.raises(StoreError):
        store.request_login(n, "", 300.0, kind="relay")


def test_a_minted_token_is_held_until_somebody_takes_it(store):
    """'done' deletes the row, which would throw away the thing just minted."""
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "code_sent", "", "", 110.0)
    store.record_login_progress(n, "ready", "", "", 120.0, secret="sk-ant-oat01-" + "a" * 40)
    row = store.get_login(n)
    assert row["state"] == "ready" and row["secret"].startswith("sk-ant-oat01-")
    assert row["code"] == "", "the verification code has served its purpose"


def test_a_token_is_shown_exactly_once(store):
    """The whole contract. A refresh or a second tab must get nothing."""
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "ready", "", "", 120.0, secret="sk-ant-oat01-secret")
    assert store.take_secret(n) == "sk-ant-oat01-secret"
    assert store.take_secret(n) == "", "second read gets nothing"
    assert store.get_login(n) is None, "and the row is gone, not merely blanked"


def test_taking_a_secret_that_is_not_ready_yields_nothing(store):
    n = _node(store)
    assert store.take_secret(n) == "", "no attempt at all"
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "url_ready", "https://claude.com/x", "", 110.0)
    assert store.take_secret(n) == "", "mid-flight is not ready"
    assert store.get_login(n) is not None, "and taking must not destroy it"


def test_a_ready_with_no_token_is_a_failure_not_an_empty_box(store):
    """The node reporting success while losing the one thing that mattered."""
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "ready", "", "", 120.0, secret="   ")
    row = store.get_login(n)
    assert row["state"] == "failed" and row["secret"] == ""
    assert "no token" in row["detail"]


def test_a_stored_token_is_bounded(store):
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "ready", "", "", 120.0, secret="s" * 5000)
    assert len(store.get_login(n)["secret"]) == store.MAX_SECRET


def test_an_unfinished_token_expires_like_any_other_attempt(store):
    """A credential must not sit in the database because nobody came back."""
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    store.record_login_progress(n, "ready", "", "", 120.0, secret="sk-ant-oat01-x")
    assert store.expire_logins(130.0) == 1
    assert store.get_login(n) is None


def test_a_database_written_before_these_columns_still_opens(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to a table that already exists."""
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE logins (node_id TEXT PRIMARY KEY, requested_at REAL NOT NULL,
            email TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'requested',
            url TEXT NOT NULL DEFAULT '', code TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
        INSERT INTO logins (node_id, requested_at, updated_at) VALUES ('old-node', 1.0, 1.0);
    """)
    con.commit()
    con.close()

    s = Store(path)
    try:
        row = s.get_login("old-node")
        assert row["kind"] == "login", "an old row reads as the flow it was"
        assert row["secret"] == ""
        s.add_node("old-node", "erik")
        s.request_login("old-node", "", 2.0, kind="token")
        assert s.get_login("old-node")["kind"] == "token"
    finally:
        s.close()


def test_a_minted_token_never_reaches_the_heartbeat_archive(store):
    """The bug this test exists for: `take_secret` deleted the copy in `logins`
    while a second copy sat in `heartbeats` for the whole retention window,
    which made "shown once" false by thirty days."""
    n = _node(store)
    secret = "sk-ant-oat01-" + "Z" * 50
    store.request_login(n, "", 1.0, kind="token")
    payload = {"hostname": "h",
               "reconcile": {"login": {"state": "ready", "secret": secret,
                                       "requested_at": 1.0}}}
    store.record_login_progress(n, "ready", "", "", 2.0, 1.0, secret=secret)
    store.insert_heartbeat(n, 2.0, payload)

    import json
    archived = json.dumps(store.recent_heartbeats(n, 5)) if hasattr(
        store, "recent_heartbeats") else json.dumps(
        [dict(r) for r in store._conn.execute("SELECT payload FROM heartbeats")])
    assert secret not in archived, "a credential must not outlive its one showing"
    # Everything else about the beat survives: this is a redaction, not a drop.
    assert "ready" in archived and "hostname" in archived
    # And the caller's own dict is untouched — it is still needed in memory.
    assert secret in json.dumps(payload)
    # The one legitimate copy still works, once.
    assert store.take_secret(n) == secret
    assert store.take_secret(n) == ""


def test_redaction_leaves_an_ordinary_heartbeat_alone(store):
    n = _node(store)
    for payload in ({"hostname": "h"},
                    {"reconcile": {}},
                    {"reconcile": {"login": {"state": "url_ready"}}},
                    {"reconcile": {"upgrade": {"to": "2.1.278"}}}):
        store.insert_heartbeat(n, 1.0, payload)
    import json
    rows = [dict(r) for r in store._conn.execute("SELECT payload FROM heartbeats")]
    blob = json.dumps(rows)
    assert "url_ready" in blob and "2.1.278" in blob and "hostname" in blob


def test_a_late_report_cannot_land_on_the_attempt_that_replaced_it(store):
    """The check and the write have to be one decision.

    Checking `requested_at` outside the lock and then writing by node id alone
    left a window: a console request landing between them replaces the row, and
    the late report writes into an attempt it knows nothing about.
    """
    n = _node(store)
    store.request_login(n, "", 100.0, kind="token")
    stale_at = store.get_login(n)["requested_at"]

    # Somebody presses the button again; the first attempt is now history.
    store.request_login(n, "", 200.0, kind="token")

    # The old attempt finishes and reports its token, late.
    store.record_login_progress(n, "ready", "", "", 210.0, stale_at,
                                secret="sk-ant-oat01-stale")
    row = store.get_login(n)
    assert row["requested_at"] == 200.0, "the live attempt is untouched"
    assert row["state"] == "requested" and row["secret"] == ""
    assert store.take_secret(n) == "", "a stale token must not be collectable"

    # And a late terminal state cannot delete the live attempt either.
    store.record_login_progress(n, "done", "", "", 211.0, stale_at)
    assert store.get_login(n) is not None, "the live attempt survives"
