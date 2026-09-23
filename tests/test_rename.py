"""Renaming a node, and renaming a slot, on the server.

The operator names machines by role (erik-N for their own, pool-N for the ones
offered to customers) and slots by letter (pool-1-a), and fleets already
running under older names have to move onto that scheme with people's slots
held and in use. So a rename is judged by what it must not disturb:

- every row that names the old id names the new one, and nothing still names
  the old one — a half-renamed node is two nodes, one of them a ghost;
- a slot keeps its holder, its state, its claim, its sign-in, and any row
  left in the unused account_intents table;
- the machine hears exactly what it heard before: it knows its slots by their
  Linux user and each claim by its time, never by the slot's id;
- the box keeps its token, and is refused until it uses the new id;
- a name something else already has is refused, and a failure half way leaves
  nothing renamed.
"""

from __future__ import annotations

import json
import time

import pytest

from ccfleetd import slots as slotstates
from ccfleetd.desired import desired_state
from ccfleetd.store import Store, StoreError, slot_login_key

NOW = 1_700_000_000.0


@pytest.fixture
def st():
    s = Store(":memory:")
    yield s
    s.close()


def rows_naming(st, value):
    """Every column, in every table, holding exactly `value` — and how many rows.

    Exact match on purpose: a free-text field (an alert's message, a kept
    heartbeat) may go on mentioning an old name, the way a log does. A column
    that *is* a name may not.
    """
    hits = []
    with st._lock:
        tables = [r[0] for r in st._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            for col in [r[1] for r in st._conn.execute(f"PRAGMA table_info({table})")]:
                n = st._conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (value,)).fetchone()[0]
                if n:
                    hits.append((table, col, n))
    return hits


def put_intent(st, slot_id):
    """A row in account_intents, the table left unused when slots went back to
    one Claude account each. A live database may still hold one, so renaming a
    slot has to carry it along like any other row that names the slot."""
    with st._lock:
        st._conn.execute(
            "INSERT INTO account_intents (slot_id, action, account, requested_at, updated_at) "
            "VALUES (?, 'use', '2', ?, ?)", (slot_id, NOW, NOW))
        st._conn.commit()


def intent_row(st, slot_id):
    with st._lock:
        row = st._conn.execute("SELECT * FROM account_intents WHERE slot_id = ?",
                               (slot_id,)).fetchone()
    return dict(row) if row else None


def make_active(st, slot_id, account_id):
    """Claimed by `account_id`, provisioned, signed in: a slot somebody is using."""
    slot = st.claim_slot(account_id, now=NOW)
    assert slot["id"] == slot_id
    user = slot["unix_user"]
    st.apply_slot_report(slot["node_id"], [
        {"unix_user": user, "present": True, "provisioned_for": slot["claimed_at"]}],
        now=NOW + 1)
    st.apply_slot_report(slot["node_id"], [
        {"unix_user": user, "present": True, "credentials": {"logged_in": True}}],
        now=NOW + 2)
    assert st.get_slot(slot_id)["state"] == slotstates.ACTIVE
    return st.get_slot(slot_id)


def fleet(st):
    """A shared machine with one slot in use and one free, and everything that
    can name either: heartbeats, an alert, the machine's own sign-in row, the
    slot's sign-in, and a leftover account_intents row. Plus a bystander node
    whose rows must not move."""
    token = st.add_node("old-m", "erik", region="us-west-residential", now=NOW)
    st.set_machine_capacity("old-m", 2)
    st.add_account("a1", "sub-a1", "a1@example.com", slot_quota=2, now=NOW)
    st.reserve_machine("old-m", "a1")
    for sid, user in (("old-m-01", "slot01"), ("old-m-02", "slot02")):
        st.add_slot(sid, "old-m", user, now=NOW)
    st.apply_slot_report("old-m", [{"unix_user": "slot01", "present": False},
                                   {"unix_user": "slot02", "present": False}], now=NOW)
    make_active(st, "old-m-01", "a1")
    st.request_login("old-m", "", NOW)
    st.request_slot_login("old-m-01", "", NOW, kind="login", held_by="a1")
    put_intent(st, "old-m-01")
    for ts in (NOW, NOW + 60):
        st.insert_heartbeat("old-m", ts, {"node_id": "old-m", "mode": "machine"})
    st.open_alert("old-m", "slot_missing:slot02", "critical", "slot02 gone on old-m", NOW)

    st.add_node("bystander", "erik", now=NOW)
    st.insert_heartbeat("bystander", NOW, {"node_id": "bystander"})
    st.open_alert("bystander", "disk_high", "warn", "disk", NOW)
    return token


def machine_hears(st, node_id):
    """The desired block a heartbeat from `node_id` is answered with, built the
    way the API builds it."""
    node = st.get_node(node_id)
    slots = st.list_slots(node_id=node_id)
    return desired_state(node, st.get_login(node_id), slots,
                         {s["id"]: st.get_login(slot_login_key(s["id"])) for s in slots})


# -- renaming a node ---------------------------------------------------------------

def test_renaming_a_node_moves_every_row_that_names_it_and_leaves_none_behind(st):
    fleet(st)
    named = rows_naming(st, "old-m")
    assert {(t, c) for t, c, _ in named} == {
        ("nodes", "id"), ("slots", "node_id"), ("heartbeats", "node_id"),
        ("alerts", "node_id"), ("logins", "node_id")}, "the fixture must exercise every column"
    bystander = rows_naming(st, "bystander")

    st.rename_node("old-m", "erik-2")

    assert rows_naming(st, "old-m") == []
    assert rows_naming(st, "erik-2") == named
    assert rows_naming(st, "bystander") == bystander


def test_a_renamed_node_keeps_its_token_and_everything_about_itself(st):
    token = fleet(st)
    before = st.get_node("old-m")
    st.rename_node("old-m", "erik-2")
    assert st.get_node("old-m") is None
    assert st.get_node("erik-2") == {**before, "id": "erik-2"}
    assert st.get_node("erik-2")["reserved_for"] == "a1"     # still kept for its account
    assert st.node_for_token(token)["id"] == "erik-2"
    assert [h["ts"] for h in st.recent_heartbeats("erik-2", limit=5)] == [NOW + 60, NOW]
    assert [a["rule"] for a in st.open_alerts("erik-2")] == ["slot_missing:slot02"]
    assert st.get_login("erik-2")["state"] == "requested"


def test_renaming_a_node_is_invisible_to_the_machine(st):
    fleet(st)
    heard = machine_hears(st, "old-m")
    st.rename_node("old-m", "erik-2")
    assert machine_hears(st, "erik-2") == heard


@pytest.mark.parametrize("new", ["", "x", "Erik-2", "erik_2", "erik 2", "-erik", "slot:x",
                                 "a" * 41])
def test_a_node_name_the_node_id_rule_refuses_is_refused(st, new):
    fleet(st)
    before = rows_naming(st, "old-m")
    with pytest.raises(StoreError):
        st.rename_node("old-m", new)
    assert rows_naming(st, "old-m") == before


def test_a_node_name_already_taken_is_refused(st):
    fleet(st)
    before, bystander = rows_naming(st, "old-m"), rows_naming(st, "bystander")
    with pytest.raises(StoreError, match="already"):
        st.rename_node("old-m", "bystander")
    assert rows_naming(st, "old-m") == before
    assert rows_naming(st, "bystander") == bystander


def test_renaming_a_node_that_does_not_exist_is_refused(st):
    fleet(st)
    with pytest.raises(StoreError, match="unknown node"):
        st.rename_node("ghost", "erik-9")
    assert rows_naming(st, "erik-9") == []


def test_a_failure_half_way_through_a_node_rename_leaves_nothing_renamed(st):
    """The last table refuses the change. Everything written before it must be
    undone, or the node exists twice: half its rows under each name."""
    fleet(st)
    before = rows_naming(st, "old-m")
    with st._lock:
        st._conn.execute("CREATE TRIGGER refuse BEFORE UPDATE ON logins "
                         "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        st._conn.commit()
    with pytest.raises(Exception, match="refused"):
        st.rename_node("old-m", "erik-2")
    assert rows_naming(st, "old-m") == before
    assert rows_naming(st, "erik-2") == []


def test_what_a_removed_node_left_behind_does_not_attach_to_the_node_renamed_onto_its_name(st):
    """Removing a node keeps no sign-in of its own worth attaching to anybody
    else — a minted device token can sit in that row. Its history and alerts are
    gone with it. Whatever still names the old id is dropped, not adopted."""
    fleet(st)
    st.add_node("gone", "erik", now=NOW)
    st.request_login("gone", "", NOW, kind="token")
    st.record_login_progress("gone", "ready", "", "", NOW, requested_at=NOW,
                             secret="sk-ant-oat01-somebody-elses")
    st.remove_node("gone")
    assert st.read_secret("gone", NOW) != "", "the leftover this test is about"
    # And rows a hand-edited database might hold for a node that is not there.
    st.insert_heartbeat("gone", NOW - 5, {"node_id": "gone"})
    st.open_alert("gone", "no_heartbeat", "critical", "gone", NOW)
    moved = rows_naming(st, "old-m")

    st.rename_node("old-m", "gone")

    assert rows_naming(st, "gone") == moved
    assert st.get_login("gone")["kind"] == "login"      # old-m's own, not the token
    assert st.read_secret("gone", NOW) == ""
    assert [h["ts"] for h in st.recent_heartbeats("gone", limit=5)] == [NOW + 60, NOW]
    assert [a["rule"] for a in st.open_alerts("gone")] == ["slot_missing:slot02"]


def test_a_slot_still_naming_a_missing_node_stops_a_rename_onto_that_name(st):
    """Slots are somebody's. One left pointing at a node that no longer exists
    is not garbage to sweep up, and must not be adopted by another machine —
    that hands a person's slot to hardware that is not theirs."""
    fleet(st)
    with st._lock:
        st._conn.execute("INSERT INTO slots (id, node_id, unix_user, state) "
                         "VALUES ('stray-01', 'gone', 'slot01', 'free')")
        st._conn.commit()
    before = rows_naming(st, "old-m")
    with pytest.raises(StoreError, match="stray-01"):
        st.rename_node("old-m", "gone")
    assert rows_naming(st, "old-m") == before
    assert st.get_slot("stray-01")["node_id"] == "gone"


# -- renaming a slot ---------------------------------------------------------------

def test_renaming_a_slot_moves_every_row_that_names_it(st):
    fleet(st)
    named = rows_naming(st, "old-m-01")
    login_key = rows_naming(st, slot_login_key("old-m-01"))
    assert {(t, c) for t, c, _ in named} == {("slots", "id"), ("account_intents", "slot_id")}
    assert login_key == [("logins", "node_id", 1)]
    other = rows_naming(st, "old-m-02")

    st.rename_slot("old-m-01", "erik-2-a")

    assert rows_naming(st, "old-m-01") == []
    assert rows_naming(st, slot_login_key("old-m-01")) == []
    assert rows_naming(st, "erik-2-a") == named
    assert rows_naming(st, slot_login_key("erik-2-a")) == login_key
    assert rows_naming(st, "old-m-02") == other


def test_a_renamed_slot_in_use_keeps_its_holder_its_claim_its_sign_in_and_its_request(st):
    fleet(st)
    slot = st.get_slot("old-m-01")
    login = st.get_login(slot_login_key("old-m-01"))
    intent = intent_row(st, "old-m-01")
    assert slot["state"] == slotstates.ACTIVE and slot["held_by"] == "a1"

    st.rename_slot("old-m-01", "erik-2-a")

    assert st.get_slot("old-m-01") is None
    assert st.get_slot("erik-2-a") == {**slot, "id": "erik-2-a"}
    assert st.get_login(slot_login_key("erik-2-a")) == {
        **login, "node_id": slot_login_key("erik-2-a")}
    assert intent_row(st, "erik-2-a") == {**intent, "slot_id": "erik-2-a"}
    assert [s["id"] for s in st.list_slots(held_by="a1")] == ["erik-2-a"]


def test_renaming_slots_is_invisible_to_the_machine(st):
    """The machine knows a slot by its Linux user, and a claim by its time.
    After the node and both slots are renamed it hears word for word what it
    heard before — so it has nothing to provision, wipe or sign in again."""
    fleet(st)
    st.claim_slot("a1", now=NOW + 10)                 # the free one, mid-claim
    heard = machine_hears(st, "old-m")
    assert [s["unix_user"] for s in heard["slots"]] == ["slot01", "slot02"]
    assert heard["slots"][1]["claimed_at"] == NOW + 10

    st.rename_node("old-m", "erik-2")
    st.rename_slot("old-m-01", "erik-2-a")
    st.rename_slot("old-m-02", "erik-2-b")

    assert machine_hears(st, "erik-2") == heard


@pytest.mark.parametrize("new", ["", "x", "Erik-2-a", "erik_2_a", "erik 2 a", "-a",
                                 "slot:erik-2-a", "a" * 65])
def test_a_slot_name_the_slot_id_rule_refuses_is_refused(st, new):
    fleet(st)
    before = rows_naming(st, "old-m-01")
    with pytest.raises(StoreError):
        st.rename_slot("old-m-01", new)
    assert rows_naming(st, "old-m-01") == before


def test_a_slot_name_already_taken_is_refused(st):
    fleet(st)
    before = rows_naming(st, "old-m-01")
    with pytest.raises(StoreError, match="already"):
        st.rename_slot("old-m-01", "old-m-02")
    assert rows_naming(st, "old-m-01") == before
    assert st.get_slot("old-m-02")["state"] == slotstates.FREE


def test_renaming_a_slot_that_does_not_exist_is_refused(st):
    fleet(st)
    with pytest.raises(StoreError, match="no slot"):
        st.rename_slot("ghost-01", "ghost-a")
    assert rows_naming(st, "ghost-a") == []


def test_a_failure_half_way_through_a_slot_rename_leaves_nothing_renamed(st):
    fleet(st)
    before = rows_naming(st, "old-m-01")
    with st._lock:
        st._conn.execute("CREATE TRIGGER refuse BEFORE UPDATE ON logins "
                         "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        st._conn.commit()
    with pytest.raises(Exception, match="refused"):
        st.rename_slot("old-m-01", "erik-2-a")
    assert rows_naming(st, "old-m-01") == before
    assert rows_naming(st, "erik-2-a") == []


def test_a_sign_in_or_request_left_under_the_new_name_is_dropped_not_adopted(st):
    """Releasing clears a slot's sign-in and requests, so none should survive
    the slot. Should one anyway, it belongs to nobody the renamed slot's holder
    is — and a sign-in row can hold a minted token."""
    fleet(st)
    with st._lock:
        st._conn.execute(
            "INSERT INTO logins (node_id, requested_at, state, updated_at, kind, secret) "
            "VALUES (?, ?, 'ready', ?, 'token', 'sk-ant-oat01-somebody-elses')",
            (slot_login_key("old-m-02"), NOW, NOW))
        st._conn.execute(
            "INSERT INTO account_intents (slot_id, action, account, requested_at, updated_at) "
            "VALUES ('old-m-02', 'forget', '1', ?, ?)", (NOW, NOW))
        st._conn.execute("DELETE FROM slots WHERE id = 'old-m-02'")
        st._conn.commit()
    mine = st.get_login(slot_login_key("old-m-01"))
    my_intent = intent_row(st, "old-m-01")

    st.rename_slot("old-m-01", "old-m-02")

    assert st.get_login(slot_login_key("old-m-02")) == {
        **mine, "node_id": slot_login_key("old-m-02")}
    assert intent_row(st, "old-m-02") == {**my_intent, "slot_id": "old-m-02"}
    assert rows_naming(st, "sk-ant-oat01-somebody-elses") == []


# -- what may name a node or a slot ----------------------------------------------------

# Columns named like an id that hold neither a node's id nor a slot's. Listed so
# that a table added later with a node_id or slot_id column fails the test below
# until the renames are taught about it.
NEITHER = {("accounts", "id"), ("payments", "id"), ("payments", "account_id"),
           ("sessions", "account_id"), ("heartbeats", "id"), ("alerts", "id")}


def test_every_id_column_in_the_schema_is_known_to_the_renames(st):
    covered = set(Store.NODE_ID_COLUMNS) | set(Store.SLOT_ID_COLUMNS) | NEITHER
    with st._lock:
        tables = [r[0] for r in st._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")]
        cols = {(t, r[1]) for t in tables
                for r in st._conn.execute(f"PRAGMA table_info({t})")}
    id_like = {(t, c) for t, c in cols if c == "id" or c.endswith("_id")}
    assert id_like - covered == set(), "teach rename_node / rename_slot about these"
    assert (set(Store.NODE_ID_COLUMNS) | set(Store.SLOT_ID_COLUMNS)) <= cols


# -- over the wire ---------------------------------------------------------------------

def test_the_box_is_heard_under_its_new_name_with_its_old_token(cfg):
    import http.client
    import threading

    from ccfleetd.api import Context, build_server
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier
    from tests.conftest import heartbeat

    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        token = store.add_node("att3", "erik", now=NOW)
        store.rename_node("att3", "erik-1")

        def beat(node_id):
            payload = {**heartbeat(time.time())["payload"], "node_id": node_id}
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("POST", "/api/heartbeat", body=json.dumps(payload),
                         headers={"Authorization": f"Bearer {token}"})
            status = conn.getresponse().status
            conn.close()
            return status

        assert beat("att3") == 400          # the box still says its old name
        assert store.recent_heartbeats("att3") == []
        assert beat("erik-1") == 200        # same token, new name
        assert len(store.recent_heartbeats("erik-1")) == 1
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()
