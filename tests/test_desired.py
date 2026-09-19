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
