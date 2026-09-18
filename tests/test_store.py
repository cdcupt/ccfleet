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
