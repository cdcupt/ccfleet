import threading

from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import Notifier
from tests.conftest import heartbeat

NOW = 2_000_000.0


class Recorder(Notifier):
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


def make_monitor(store, cfg):
    notifier = Recorder()
    clock = {"now": NOW}
    monitor = Monitor(store, cfg, notifier, clock=lambda: clock["now"])
    return monitor, notifier, clock


def test_alert_opens_then_closes(store, cfg):
    store.add_node("node-a", "erik", now=NOW - 86400)
    monitor, notifier, clock = make_monitor(store, cfg)
    events = monitor.check_all()
    assert [e["event"] for e in events] == ["opened"]
    assert events[0]["alert"]["rule"] == "no_heartbeat"
    assert "CRITICAL" in notifier.messages[0] and "node-a (erik)" in notifier.messages[0]
    hb = heartbeat(NOW)
    store.insert_heartbeat("node-a", NOW, hb["payload"])
    events = monitor.check_all()
    assert [e["event"] for e in events] == ["closed"]
    assert "resolved" in notifier.messages[1]
    assert store.open_alerts() == []
    assert monitor.check_all() == []


def test_level_change_reopens_alert(store, cfg):
    store.add_node("node-a", "erik", now=NOW - 86400)
    monitor, notifier, _ = make_monitor(store, cfg)
    store.insert_heartbeat("node-a", NOW, heartbeat(NOW, disk={"used_pct": 90.0})["payload"])
    assert [e["alert"]["level"] for e in monitor.check_all()] == ["warn"]
    store.insert_heartbeat("node-a", NOW + 1, heartbeat(NOW, disk={"used_pct": 99.0})["payload"])
    events = monitor.check_all()
    assert [(e["event"], e["alert"]["level"]) for e in events] == [("opened", "critical")]
    open_alerts = store.open_alerts("node-a")
    assert len(open_alerts) == 1 and open_alerts[0]["level"] == "critical"
    assert len(notifier.messages) == 2


def test_disabled_nodes_are_skipped_and_prune_runs(store, cfg):
    store.add_node("node-a", "erik", now=NOW - 86400)
    store.set_enabled("node-a", False)
    monitor, notifier, clock = make_monitor(store, cfg)
    store.insert_heartbeat("node-a", NOW - 40 * 86400, {})
    assert monitor.check_all() == []
    assert notifier.messages == []
    assert store.recent_heartbeats("node-a") == []  # pruned by retention


def test_public_url_is_appended(store):
    cfg = Config.from_env({"CCFLEET_PUBLIC_URL": "https://fleet.example"})
    store.add_node("node-a", "erik", now=NOW - 86400)
    monitor, notifier, _ = make_monitor(store, cfg)
    monitor.check_all()
    assert notifier.messages[0].endswith("https://fleet.example/")


def test_run_forever_survives_check_errors(store, cfg, monkeypatch):
    monitor, _, _ = make_monitor(store, cfg)
    calls = {"n": 0}

    def boom(now=None):
        calls["n"] += 1
        raise RuntimeError("db gone")

    monkeypatch.setattr(monitor, "check_all", boom)
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout: stop.set())
    monitor.run_forever(stop)
    assert calls["n"] == 1


def test_concurrent_checks_never_duplicate_alerts(store, cfg):
    """Periodic checks racing heartbeat handlers must leave at most one open alert per rule."""
    store.add_node("node-a", "erik", now=NOW - 86400)
    monitor, notifier, clock = make_monitor(store, cfg)
    errors = []

    def periodic():
        for _ in range(40):
            try:
                monitor.check_all()
            except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
                errors.append(exc)

    def poster():
        node = store.get_node("node-a")
        for i in range(40):
            hb = heartbeat(NOW, disk={"used_pct": 99.0 if i % 2 else 10.0})
            try:
                monitor.record_heartbeat(node, hb["payload"], NOW)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=periodic), threading.Thread(target=poster),
               threading.Thread(target=periodic)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    open_alerts = store.open_alerts("node-a")
    rules_open = [a["rule"] for a in open_alerts]
    assert len(rules_open) == len(set(rules_open))
    assert "no_heartbeat" not in rules_open


def test_a_claim_the_machine_never_finished_is_given_up(store, cfg):
    """The machine went quiet mid-claim. Without this the slot, and the
    claimant's allowance with it, would sit in `claiming` for good."""
    from ccfleetd import slots
    store.add_node("m1", "op", now=NOW - 86400)
    store.set_machine_capacity("m1", 2)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=2, now=NOW)
    for sid, user in (("s1", "slot01"), ("s2", "slot02")):
        store.add_slot(sid, "m1", user, now=NOW)
        store.apply_slot_report("m1", [{"unix_user": user, "present": False}], now=NOW)
    store.claim_slot("a1", now=NOW - slots.CLAIM_TIMEOUT_S - 1)
    store.claim_slot("a1", now=NOW - 60)

    monitor, _, _ = make_monitor(store, cfg)
    monitor.check_all()
    assert store.get_slot("s1")["state"] == slots.RELEASING
    assert store.get_slot("s1")["held_by"] == "a1", "freed before the wipe"
    assert store.get_slot("s2")["state"] == slots.CLAIMING, "gave up on a claim still in time"


def test_a_shared_machines_slot_trouble_reaches_the_operator(store, cfg):
    store.add_node("m1", "op", now=NOW - 86400)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    monitor, notifier, _ = make_monitor(store, cfg)
    payload = heartbeat(NOW)["payload"]
    payload.update(mode="machine", slots=[{"unix_user": "slot01", "present": True}])
    for key in ("claude", "credentials", "remote_control"):
        payload.pop(key)
    events = monitor.record_heartbeat(store.get_node("m1"), payload, NOW)
    assert [(e["event"], e["alert"]["rule"]) for e in events] == [
        ("opened", "slot_occupied:slot01")]
    assert "slot_occupied:slot01" in notifier.messages[0]


def test_an_owner_slot_is_never_judged_as_a_machines_slot(store, cfg):
    """Somebody's own node counted as their slot has no machine agent to vouch
    for its Linux user; a machine-shaped report about that user is no alarm."""
    store.add_node("erik-1", "erik", now=NOW - 86400)
    store.add_account("e1", "sub-e", "cdcupt@gmail.com", slot_quota=1, now=NOW)
    store.hold_owner_node("erik-1", "e1", now=NOW)
    monitor, _, _ = make_monitor(store, cfg)
    payload = heartbeat(NOW)["payload"]
    payload.update(node_id="erik-1", mode="machine",
                   slots=[{"unix_user": "erik", "present": False}])
    for key in ("claude", "credentials", "remote_control"):
        payload.pop(key)
    events = monitor.record_heartbeat(store.get_node("erik-1"), payload, NOW)
    assert not [e for e in events if str(e["alert"]["rule"]).startswith("slot_")]


# -- the server's own silence is not the node's (see status.machine_state) ---------------------

def test_a_restart_raises_no_alarm_about_silence_that_was_the_servers(store, cfg):
    store.add_node("node-a", "erik", now=NOW - 86400)
    store.insert_heartbeat("node-a", NOW - 3600, heartbeat(NOW - 3600)["payload"])
    store.set_listening_since(NOW - 30)
    monitor, notifier, _ = make_monitor(store, cfg)
    assert monitor.check_all() == [] and notifier.messages == []


def test_an_alarm_raised_before_a_restart_holds_until_the_node_reports(store, cfg):
    """Counting its silence from the restart would say it resolved, then
    raise it again a quarter of an hour later."""
    store.add_node("node-a", "erik", now=NOW - 86400)
    store.insert_heartbeat("node-a", NOW - 3600, heartbeat(NOW - 3600)["payload"])
    monitor, notifier, clock = make_monitor(store, cfg)
    assert [e["event"] for e in monitor.check_all()] == ["opened"]
    store.set_listening_since(NOW + 1200)            # the server was down, and is back
    clock["now"] = NOW + 1230
    assert monitor.check_all() == [] and len(notifier.messages) == 1
    store.insert_heartbeat("node-a", NOW + 1240, heartbeat(NOW + 1240)["payload"])
    clock["now"] = NOW + 1250
    assert [e["event"] for e in monitor.check_all()] == ["closed"]


def test_a_claim_is_not_given_up_for_the_servers_own_silence(store, cfg):
    """No report could say it was set up while the server was down."""
    from ccfleetd import slots
    store.add_node("m1", "op", now=NOW - 86400)
    store.set_machine_capacity("m1", 1)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    store.claim_slot("a1", now=NOW - slots.CLAIM_TIMEOUT_S - 600)
    store.set_listening_since(NOW - 300)
    monitor, _, clock = make_monitor(store, cfg)
    monitor.check_all()
    assert store.get_slot("s1")["state"] == slots.CLAIMING
    clock["now"] = NOW - 300 + slots.CLAIM_TIMEOUT_S + 1
    monitor.check_all()
    assert store.get_slot("s1")["state"] == slots.RELEASING


def test_an_update_is_not_failed_for_the_servers_own_silence(store, cfg):
    from ccfleetd.monitor import LOGIN_MAX_AGE_S
    store.add_node("m1", "op", now=NOW - 86400)
    store.set_machine_capacity("m1", 1)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    claim = store.claim_slot("a1", now=NOW - 7200)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": claim["claimed_at"]}], now=NOW - 7000)
    store.request_claude_update("s1", NOW - LOGIN_MAX_AGE_S - 600, held_by="a1")
    store.set_listening_since(NOW - 60)
    monitor, _, clock = make_monitor(store, cfg)
    monitor.check_all()
    assert store.get_claude_update("s1")["state"] == "pending"
    clock["now"] = NOW - 60 + LOGIN_MAX_AGE_S + 1
    monitor.check_all()
    assert store.get_claude_update("s1")["state"] == "failed"


def test_an_account_seen_on_two_nodes_is_not_called_resolved_by_a_restart(store, cfg):
    """After a long outage every node's last report is old; the alert about
    one account in two places must not close for that."""
    for node in ("n1", "n2"):
        store.add_node(node, "erik", now=NOW - 86400)
        beat = heartbeat(NOW - 3600, credentials={"present": True, "logged_in": True,
                                                  "account_fp": "fp1", "store": "file",
                                                  "mtime": NOW - 3600,
                                                  "expires_at": (NOW + 7200) * 1000})
        store.insert_heartbeat(node, NOW - 3600, {**beat["payload"], "node_id": node})
    monitor, _, clock = make_monitor(store, cfg)
    clock["now"] = NOW - 3600 + 30
    opened = {e["alert"]["rule"] for e in monitor.check_all() if e["event"] == "opened"}
    assert any(rule.startswith("account_") for rule in opened), opened
    store.set_listening_since(NOW - 30)              # down an hour, and back
    clock["now"] = NOW
    closed = [e["alert"]["rule"] for e in monitor.check_all() if e["event"] == "closed"]
    assert not any(rule.startswith("account_") for rule in closed), closed
