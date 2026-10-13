"""A newly registered public machine must not inherit a removed machine's uptime."""

from ccfleetd import status

NOW = 2_000_000_000.0


def test_reused_internal_id_starts_new_public_machine_history(store):
    store.add_node("reused-node", "ops", now=NOW - 600)
    old_number = store.get_node("reused-node")["public_status_number"]
    store.count_status("node:reused-node", status.RED, NOW - 60, grace=2)
    store.remove_node("reused-node")

    store.add_node("reused-node", "ops", now=NOW)
    new_number = store.get_node("reused-node")["public_status_number"]
    assert new_number != old_number
    history = status.history(store, NOW)
    assert history.per_machine_minutes.get("reused-node", (0, 0)) == (0, 0)
