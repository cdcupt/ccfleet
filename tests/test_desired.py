"""The desired-state block the server hands each node, and how a node reads it."""

from ccfleetd.desired import desired_state


def _node(**over):
    node = {"id": "node-a", "owner": "erik", "pinned_version": "", "rc_expected": 0}
    node.update(over)
    return node


def test_no_pin_means_leave_the_version_alone():
    # "" is not "install nothing" by accident — it is the explicit unmanaged case,
    # and the agent must be able to tell it apart from a pin it cannot read.
    assert desired_state(_node())["claude_version"] == ""


def test_exact_versions_and_channels_pass_through():
    assert desired_state(_node(pinned_version="2.1.278"))["claude_version"] == "2.1.278"
    assert desired_state(_node(pinned_version="stable"))["claude_version"] == "stable"
    assert desired_state(_node(pinned_version="latest"))["claude_version"] == "latest"
    assert desired_state(_node(pinned_version=" 2.1.278 "))["claude_version"] == "2.1.278"


def test_a_pin_that_could_not_work_is_dropped_rather_than_forwarded():
    """This string becomes an argument to `claude install` on the node.

    The node re-checks it too, but a pin that cannot work should not travel.
    """
    for junk in ("--force", "; rm -rf /", "$(whoami)", "../../etc", "nightly",
                 "v2.1.278", "x" * 41, None, 42, True):
        assert desired_state(_node(pinned_version=junk))["claude_version"] == ""


def test_remote_control_is_a_bool_whatever_sqlite_stored():
    assert desired_state(_node(rc_expected=1))["remote_control"] is True
    assert desired_state(_node(rc_expected=0))["remote_control"] is False
    assert desired_state(_node(rc_expected=True))["remote_control"] is True


def test_is_channel_is_the_one_place_that_decides():
    from ccfleetd.desired import is_channel
    assert is_channel("stable") and is_channel("latest") and is_channel(" stable ")
    for not_channel in ("2.1.92", "", "STABLE", None, 3, True):
        assert not is_channel(not_channel)


def test_a_channel_pin_never_reports_drift(cfg):
    """`claude_version` is a number and the pin is a word, so a literal compare
    would alert forever on a node doing exactly what it was told."""
    from ccfleetd import rules
    from tests.conftest import heartbeat
    now = 1_000_000.0
    hb = heartbeat(now - 60)
    hb["payload"]["claude"] = {"version": "2.1.99", "path": "/home/erik/.local/bin/claude"}
    node = {"id": "node-a", "owner": "erik", "region": "", "pinned_version": "stable",
            "rc_expected": False, "enabled": True, "created_at": now - 86400}
    found = [f.rule for f in rules.evaluate(node, hb, None, now, cfg)]
    assert "version_mismatch" not in found

    # An exact pin still drifts, or the feature would be pointless.
    exact = {**node, "pinned_version": "2.1.90"}
    assert "version_mismatch" in [f.rule for f in rules.evaluate(exact, hb, None, now, cfg)]


def test_only_a_real_sign_in_url_is_ever_linkable():
    """The node supplies this and an operator clicks it. Escaping makes it safe as
    text; it does nothing about the scheme, so the scheme is checked."""
    from ccfleetd.desired import is_login_url
    # The first is the shape a live sign-in actually produced. It was on
    # claude.com, which the original host list did not include, so the real URL
    # would have been refused as unsafe.
    for good in ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a"
                 "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback",
                 "https://claude.ai/oauth/authorize?code=1",
                 "https://www.claude.ai/x", "https://console.anthropic.com/y"):
        assert is_login_url(good), good
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)",
                "data:text/html,<script>alert(1)</script>",
                "http://claude.ai/x",                    # not https
                "https://evil.com/x", "https://claude.ai.evil.com/x",
                "https://claude.com.evil.com/x",
                "https://user:pw@claude.ai/x",           # credentials hidden in it
                "//claude.ai/x", "", "   ", None, 123, "https://claude.ai/" + "x" * 2000):
        assert not is_login_url(bad), bad


def test_the_kind_handed_to_a_node_is_one_it_knows():
    """This word picks which command the agent runs, so it is not passed through.

    A row can carry a strange one: an older database, a future version writing
    a kind this build has never heard of, or a corrupted value. Coercing beats
    forwarding something the node has to guess about.
    """
    from ccfleetd.desired import desired_state
    node = {"pinned_version": "", "rc_expected": False}

    def kind_for(k):
        d = desired_state(node, {"state": "requested", "requested_at": 1.0, "kind": k})
        return d["login"]["kind"]

    assert kind_for("token") == "token"
    assert kind_for("login") == "login"
    for strange in ("relay", "", None, 7, "TOKEN"):
        assert kind_for(strange) == "login", f"{strange!r} must not reach the node"


def test_a_waiting_token_stops_asking_the_node_for_anything():
    """'ready' means a person has to collect it. Re-sending the block would
    restart the flow and mint a second credential nobody asked for."""
    from ccfleetd.desired import desired_state
    node = {"pinned_version": "", "rc_expected": False}
    ready = {"state": "ready", "requested_at": 1.0, "kind": "token",
             "secret": "sk-ant-oat01-x"}
    d = desired_state(node, ready)
    assert d["login"] is None
    assert d["poll_s"] == 300, "and the node goes back to its idle rhythm"


def test_an_ordinary_node_hears_nothing_about_slots():
    assert "slots" not in desired_state(_node())
    assert "slots" not in desired_state(_node(), slots=[])


def test_a_shared_machine_is_told_what_each_slot_should_be():
    """The state is the whole instruction — claiming means set it up, releasing
    means wipe it — and a claim carries its timestamp, which is how the machine
    says which claim it finished."""
    rows = [
        {"id": "s1", "unix_user": "slot01", "state": "claiming", "claimed_at": 12.5,
         "held_by": "u-secret-account-id"},
        {"id": "s2", "unix_user": "slot02", "state": "releasing", "claimed_at": 3.0},
        {"id": "s3", "unix_user": "slot03", "state": "free", "claimed_at": None},
    ]
    assert desired_state(_node(), slots=rows)["slots"] == [
        {"unix_user": "slot01", "state": "claiming", "claimed_at": 12.5},
        {"unix_user": "slot02", "state": "releasing"},
        {"unix_user": "slot03", "state": "free"},
    ]
