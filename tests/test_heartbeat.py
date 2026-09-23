import json
import math

import pytest

from ccfleetd.heartbeat import HeartbeatError, validate_heartbeat


def test_rejects_non_object_and_wrong_node():
    with pytest.raises(HeartbeatError):
        validate_heartbeat([], "node-a")
    with pytest.raises(HeartbeatError):
        validate_heartbeat({"node_id": "node-b"}, "node-a")


def test_keeps_known_fields_and_drops_the_rest():
    out = validate_heartbeat({
        "node_id": "node-a", "ts": 12.5, "hostname": "h" * 500, "surprise": {"x": 1},
        "claude": {"version": "2.1.92", "extra": 1}, "credentials": {"present": True,
                                                                     "mtime": 5, "accessToken": "sk"},
        "disk": {"used_pct": float("nan")}, "egress": {"ip": "203.0.113.1"},
        "remote_control": {"state": "active"}, "tmux_sessions": True,
    }, "node-a")
    assert "surprise" not in out and "extra" not in out["claude"]
    assert "accessToken" not in out["credentials"]
    assert len(out["hostname"]) == 200
    assert out["disk"]["used_pct"] is None
    assert out["tmux_sessions"] is None
    assert out["agent_ts"] == 12.5 and out["egress"]["ip"] == "203.0.113.1"


def test_sections_tolerate_wrong_types():
    out = validate_heartbeat({"node_id": "node-a", "claude": "nope", "credentials": 3,
                              "disk": None, "load": [1, 2]}, "node-a")
    assert out["claude"] == {"version": None, "path": None}
    assert out["credentials"]["present"] is None
    assert out["load"] == {"1": None, "5": None, "15": None}
    assert math.isfinite(out["agent_ts"] or 0.0)


def test_the_new_credential_fields_survive_validation():
    """A laptop has no credentials file, so these two carry the whole login signal."""
    out = validate_heartbeat({"node_id": "node-a", "ts": 1.0, "credentials": {
        "present": True, "store": "keychain",
        "profile_fetched_at": 1_700_000_000_000, "plan": "default_claude_max_20x"}}, "node-a")
    assert out["credentials"]["profile_fetched_at"] == 1_700_000_000_000
    assert out["credentials"]["plan"] == "default_claude_max_20x"


def test_reconcile_is_absent_until_a_node_actually_reports_one():
    """An invented skeleton of Nones is truthy, so every node would look as though
    it had reconciled at least once."""
    assert "reconcile" not in validate_heartbeat({"node_id": "n"}, "n")
    assert "reconcile" not in validate_heartbeat(
        {"node_id": "n", "reconcile": {"upgrade": {}}}, "n")
    assert "reconcile" not in validate_heartbeat(
        {"node_id": "n", "reconcile": "not a mapping"}, "n")
    reported = validate_heartbeat(
        {"node_id": "n", "reconcile": {"upgrade": {"to": "2.1.99", "ok": True}}}, "n")
    assert reported["reconcile"]["upgrade"]["to"] == "2.1.99"
    assert reported["reconcile"]["upgrade"]["ok"] is True


def test_identity_fields_cannot_reach_the_store_through_credentials():
    """The agent does not send these, and the server would not keep them anyway."""
    from ccfleetd.heartbeat import validate_heartbeat
    out = validate_heartbeat({"node_id": "n", "credentials": {
        "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty",
        "email": "someone@example.com", "orgId": "a7ba7d79", "orgName": "Acme",
    }}, "n")
    creds = out["credentials"]
    assert creds["logged_in"] is True and creds["auth_method"] == "claude.ai"
    blob = json.dumps(out)
    for private in ("someone@example.com", "a7ba7d79", "Acme"):
        assert private not in blob


def test_a_real_sign_in_url_survives_the_whitelist_intact():
    """Measured at 496 characters against a live node.

    Capped at MAX_STR the URL arrived truncated to 200 — which is worse than
    absent, because it still looks like a URL and fails only when clicked.
    """
    from ccfleetd.heartbeat import MAX_URL
    url = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9"
           "-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform."
           "claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile"
           "+user%3Ainference+user%3Asessions%3Aclaude_code+user%3Amcp_servers+user%3A"
           "file_upload+user%3Aplugins&code_challenge=gz7bOnSAmGyBMHOKijGzBhiB5LoTTqcHon"
           "ZexLJsx3E&code_challenge_method=S256&state=hN1YnDdKxl6It0ZEI_hAP5HUqVDY5qZn"
           "XiHRSFT9-N8&login_hint=someone%40example.com")
    assert len(url) > 400, "fixture should be a realistic length"
    out = validate_heartbeat(
        {"node_id": "n", "reconcile": {"login": {"state": "url_ready", "url": url,
                                                 "detail": "d" * 500}}}, "n")
    login = out["reconcile"]["login"]
    assert login["url"] == url, "the URL must arrive whole"
    # The larger cap is for this one field, not a general loosening.
    assert len(login["detail"]) == 200
    # And it is still bounded: a node cannot post an unbounded string.
    huge = validate_heartbeat(
        {"node_id": "n", "reconcile": {"login": {"state": "x", "url": "h" * 9000}}}, "n")
    assert len(huge["reconcile"]["login"]["url"]) == MAX_URL


def test_usage_is_counts_only_and_every_part_of_it_is_bounded():
    """A node could otherwise post an unbounded series, or smuggle content."""
    out = validate_heartbeat({"node_id": "n", "usage": {
        "total_tokens": 40897, "sessions": 3, "window_days": 14,
        "models": ["claude-opus-5"],
        "by_day": [{"day": "2026-09-19", "tokens": 40897}],
        "transcript": "the user's private conversation",
    }}, "n")
    usage = out["usage"]
    assert usage["total_tokens"] == 40897 and usage["sessions"] == 3
    assert usage["by_day"] == [{"day": "2026-09-19", "tokens": 40897}]
    assert "private conversation" not in json.dumps(out)

    big = validate_heartbeat({"node_id": "n", "usage": {
        "by_day": [{"day": f"d{i}", "tokens": i} for i in range(500)],
        "models": [f"m{i}" for i in range(50)],
    }}, "n")["usage"]
    assert len(big["by_day"]) == 31 and len(big["models"]) == 8

    junk = validate_heartbeat({"node_id": "n", "usage": {
        "by_day": ["not a mapping", {"day": None, "tokens": 5}, {"day": "ok", "tokens": None}],
        "models": "not a list",
    }}, "n")["usage"]
    assert junk["by_day"] == [] and junk["models"] == []


def test_an_absurd_integer_cannot_take_the_dashboard_down():
    """Python ints are arbitrary precision, so an authenticated node can post a
    400-digit one. Anything doing float arithmetic on it later — the charts —
    raises OverflowError and the page stops rendering for everyone."""
    from ccfleetd.render import _human_tokens, _sparkline
    out = validate_heartbeat({"node_id": "n",
                              "disk": {"used_pct": 10 ** 500},
                              "usage": {"total_tokens": 10 ** 400, "sessions": 3,
                                        "by_day": [{"day": "a", "tokens": 10 ** 400},
                                                   {"day": "b", "tokens": 50}]}}, "n")
    assert out["disk"]["used_pct"] is None
    assert out["usage"]["total_tokens"] is None
    assert out["usage"]["by_day"] == [{"day": "b", "tokens": 50}]
    # And what survived still renders, which is the whole point: the absurd
    # value is dropped rather than carried into float arithmetic downstream.
    _human_tokens(out["usage"]["total_tokens"])
    assert _sparkline(out["usage"]["by_day"]), "the one surviving day still renders"
    # Ordinary measurements are untouched.
    fine = validate_heartbeat({"node_id": "n", "usage": {"total_tokens": 40897},
                               "disk": {"used_pct": 12.7}}, "n")
    assert fine["usage"]["total_tokens"] == 40897 and fine["disk"]["used_pct"] == 12.7


# -- a shared machine's slots ---------------------------------------------------

def _machine(slots, **extra):
    return validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": slots,
                               **extra}, "m1")


def test_an_ordinary_node_keeps_exactly_the_shape_it_had():
    """Every stored heartbeat of every node in the fleet predates slots. A
    skeleton `slots: []` on them would read as a machine with none."""
    out = validate_heartbeat({"node_id": "n", "slots": [{"unix_user": "slot01"}]}, "n")
    assert "slots" not in out and "mode" not in out
    out = validate_heartbeat({"node_id": "n", "mode": "shared", "slots": []}, "n")
    assert "slots" not in out and "mode" not in out, "an unknown mode was honoured"


def test_a_machine_reports_each_slot_by_its_user():
    out = _machine([{"unix_user": "slot01", "present": True, "provisioned_for": 12.5,
                     "claude": {"version": "2.1.278", "path": "/home/slot01/x"},
                     "credentials": {"logged_in": True, "subscription_type": "max",
                                     "email": "someone@example.com",
                                     "accessToken": "sk-ant-oat01-secret"},
                     "remote_control": {"state": "active"},
                     "quota": {"session": {"used_pct": 12, "resets": "5pm"}},
                     "usage": {"total_tokens": 900},
                     "home": "/home/slot01", "surprise": 1}])
    assert out["mode"] == "machine"
    [slot] = out["slots"]
    assert slot["unix_user"] == "slot01"
    assert slot["present"] is True and slot["provisioned_for"] == 12.5
    assert slot["claude"] == {"version": "2.1.278"}
    assert slot["credentials"]["logged_in"] is True
    assert slot["credentials"]["subscription_type"] == "max"
    assert slot["quota"]["session"]["used_pct"] == 12
    assert slot["usage"]["total_tokens"] == 900
    # Nothing that names the account, nothing that is a credential, nothing
    # the schema did not ask for.
    flat = json.dumps(out)
    assert "someone@example.com" not in flat
    assert "sk-ant-oat01" not in flat
    assert "surprise" not in slot and "home" not in slot and "path" not in slot["claude"]


@pytest.mark.parametrize("user", ["", "Slot01", "1slot", "root user", "x" * 33,
                                  "../etc", None, 7])
def test_a_slot_with_no_usable_name_is_dropped(user):
    """The name is what the server matches on. Guessing at a bad one would
    move whichever slot it happened to resemble."""
    assert _machine([{"unix_user": user, "present": False}])["slots"] == []


def test_a_name_reported_twice_keeps_its_first_word():
    out = _machine([{"unix_user": "slot01", "present": True},
                    {"unix_user": "slot01", "present": False}])
    assert out["slots"] == [out["slots"][0]]
    assert out["slots"][0]["present"] is True


def test_a_machine_cannot_post_an_unbounded_list():
    from ccfleetd.heartbeat import MAX_SLOT_REPORTS
    many = [{"unix_user": f"slot{n:03d}", "present": False}
            for n in range(MAX_SLOT_REPORTS + 10)]
    assert len(_machine(many)["slots"]) == MAX_SLOT_REPORTS


@pytest.mark.parametrize("slots", [None, "slot01", {"unix_user": "slot01"},
                                   [None, 3, "slot01"]])
def test_slots_of_the_wrong_shape_are_an_empty_report(slots):
    assert _machine(slots)["slots"] == []


def test_slot_fields_of_the_wrong_type_are_nulled_not_trusted():
    [slot] = _machine([{"unix_user": "slot01", "present": "yes",
                        "provisioned_for": True, "provision_failed_for": "12",
                        "wipe_error": 5, "credentials": "logged in"}])["slots"]
    assert slot["present"] is None
    assert slot["provisioned_for"] is None, "True would match a claim made at 1.0"
    assert slot["provision_failed_for"] is None
    assert slot["wipe_error"] is None
    assert slot["credentials"]["logged_in"] is None


def test_an_error_a_machine_reports_is_bounded_like_every_other_string():
    [slot] = _machine([{"unix_user": "slot01", "wipe_error": "x" * 5000,
                        "provision_error": "y" * 5000}])["slots"]
    assert len(slot["wipe_error"]) == 200 and len(slot["provision_error"]) == 200


# -- the hourly week -------------------------------------------------------------

def _usage(section):
    return validate_heartbeat({"node_id": "n", "usage": section}, "n")["usage"]


def test_an_hourly_week_is_kept_whole():
    usage = _usage({"total_tokens": 42, "window_hours": 168,
                    "by_hour": {"start": 1_700_000_000.0, "tokens": [0] * 167 + [42]}})
    assert usage["window_hours"] == 168
    assert usage["by_hour"]["start"] == 1_700_000_000.0
    assert len(usage["by_hour"]["tokens"]) == 168 and usage["by_hour"]["tokens"][-1] == 42


def test_a_count_that_is_not_a_sane_number_counts_as_nothing():
    tokens = _usage({"by_hour": {"start": 1.0, "tokens": [5, -3, "x", None, True,
                                                          10 ** 400, float("nan")]}}
                    )["by_hour"]["tokens"]
    assert tokens == [5, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("hourly", [
    {"start": 1.0, "tokens": [1] * (31 * 24 + 1)},   # longer than any window we draw
    {"start": 1.0, "tokens": []},
    {"start": None, "tokens": [1, 2]},                # no idea which hour is which
    {"start": "1", "tokens": [1, 2]},
    {"start": 1.0, "tokens": "12"},
    [1, 2, 3],
])
def test_a_series_that_cannot_be_placed_is_dropped_whole(hourly):
    """Cut short or unanchored, every bar would stand in the wrong hour."""
    assert "by_hour" not in _usage({"by_hour": hourly})



# -- upgrades a slot reports, and a reboot the OS asks for ----------------------------

def test_a_slots_upgrade_is_kept_bounded():
    out = _machine([{"unix_user": "slot01", "upgrade": {
        "from": "2.1.278", "to": "2.1.300", "ok": True, "ts": 5.0, "error": None,
        "restart": "waiting", "surprise": 1}}])
    assert out["slots"][0]["upgrade"] == {"from": "2.1.278", "to": "2.1.300", "ok": True,
                                          "ts": 5.0, "error": None, "restart": "waiting"}
    long = _machine([{"unix_user": "slot01", "upgrade": {"ok": False, "error": "x" * 5000}}])
    assert len(long["slots"][0]["upgrade"]["error"]) <= 200


@pytest.mark.parametrize("restart", ["restarted", "", 1, True, None, ["done"]])
def test_a_restart_the_agent_did_not_name_properly_is_nothing(restart):
    out = _machine([{"unix_user": "slot01", "upgrade": {"to": "2.1.300", "ok": True,
                                                        "restart": restart}}])
    assert out["slots"][0]["upgrade"]["restart"] is None


def test_a_restart_still_owed_is_reported_without_its_record():
    """The record is dropped once the pin is met; the restart it caused may not
    have happened yet, and is still news."""
    out = _machine([{"unix_user": "slot01", "upgrade": {"restart": "waiting"}}])
    assert out["slots"][0]["upgrade"] == {"restart": "waiting"}


@pytest.mark.parametrize("upgrade", [None, {}, {"surprise": 1}, "done", []])
def test_a_slot_with_no_upgrade_to_speak_of_says_nothing(upgrade):
    entry = {"unix_user": "slot01"}
    if upgrade is not None:
        entry["upgrade"] = upgrade
    assert "upgrade" not in _machine([entry])["slots"][0]


@pytest.mark.parametrize("sent,kept", [(True, True), (False, False)])
def test_a_reboot_the_os_asked_for_is_kept(sent, kept):
    out = validate_heartbeat({"node_id": "n", "reboot_required": sent}, "n")
    assert out["reboot_required"] is kept
    assert _machine([], reboot_required=sent)["reboot_required"] is kept


@pytest.mark.parametrize("sent", ["yes", "true", 1, 0, None, {}, [True]])
def test_anything_but_a_plain_yes_or_no_about_rebooting_is_dropped(sent):
    out = validate_heartbeat({"node_id": "n", "reboot_required": sent}, "n")
    assert "reboot_required" not in out


def test_the_owners_upgrade_record_keeps_its_shape():
    """Refactored onto the slots' reader: an owner node's record must not gain
    the slot-only restart field."""
    out = validate_heartbeat({"node_id": "n", "reconcile": {"upgrade": {
        "from": "2.1.90", "to": "2.1.99", "ok": True, "ts": 1.0, "error": None,
        "restart": "done"}}}, "n")
    assert out["reconcile"]["upgrade"] == {"from": "2.1.90", "to": "2.1.99", "ok": True,
                                           "error": None, "ts": 1.0}
