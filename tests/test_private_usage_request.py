"""A private owner's quota refresh is narrow and never enables lifecycle actions."""
import pytest

from ccfleetd.store import StoreError
from tests.test_private_owner_nodes import NOW, enable
from tests.test_private_owner_nodes import private as private


def test_private_quota_refresh_requires_the_active_bound_admin(private):
    store, _ = private
    with pytest.raises(StoreError):
        store.request_quota_read("mac-private", NOW, held_by="operator")
    enable(store)
    for other in ("other-admin", "customer"):
        with pytest.raises(StoreError):
            store.request_quota_read("mac-private", NOW, held_by=other)
    assert store.request_quota_read("mac-private", NOW, held_by="operator") is True
    assert store.private_owner_quota("mac-private") == {"requested_at": NOW}
    assert store.request_quota_read("mac-private", NOW + 1, held_by="operator") is False
    store._conn.execute("UPDATE accounts SET role='user' WHERE id='operator'")
    store._conn.commit()
    with pytest.raises(StoreError):
        store.request_quota_read("mac-private", NOW + 60, held_by="operator")
    assert store.private_owner_quota("mac-private") is None


def test_generic_owner_node_does_not_acquire_remote_quota_commands(private):
    store, _ = private
    store.add_node("ordinary-owner", "nativeuser", now=NOW)
    store.hold_owner_node("ordinary-owner", "operator", now=NOW)
    with pytest.raises(StoreError):
        store.request_quota_read("ordinary-owner", NOW, held_by="operator")
    assert store.private_owner_quota("ordinary-owner") is None
