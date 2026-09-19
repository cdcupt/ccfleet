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
