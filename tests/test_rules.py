from ccfleetd import rules
from tests.conftest import heartbeat

NOW = 1_000_000.0
NODE = {"id": "node-a", "owner": "erik", "region": "", "pinned_version": "",
        "rc_expected": False, "enabled": True, "created_at": NOW - 86400}


def rule_names(findings):
    return sorted(f.rule for f in findings)


def test_healthy_node_has_no_findings(cfg):
    assert rules.evaluate(NODE, heartbeat(NOW - 60), None, NOW, cfg) == ()


def test_no_heartbeat_when_old_or_absent(cfg):
    assert rule_names(rules.evaluate(NODE, None, None, NOW, cfg)) == ["no_heartbeat"]
    fresh_node = {**NODE, "created_at": NOW - 10}
    assert rules.evaluate(fresh_node, None, None, NOW, cfg) == ()
    stale = rules.evaluate(NODE, heartbeat(NOW - 1200), None, NOW, cfg)
    assert stale[0].rule == "no_heartbeat" and stale[0].level == "critical"
    assert "min" in stale[0].message


def test_claude_missing_and_version_mismatch(cfg):
    missing = rules.evaluate(NODE, heartbeat(NOW, claude={"version": None}), None, NOW, cfg)
    assert rule_names(missing) == ["claude_missing"]
    pinned = {**NODE, "pinned_version": "2.1.90"}
    mismatch = rules.evaluate(pinned, heartbeat(NOW), None, NOW, cfg)
    assert rule_names(mismatch) == ["version_mismatch"]
    assert "2.1.92" in mismatch[0].message and "2.1.90" in mismatch[0].message


def test_credential_rules(cfg):
    missing = rules.evaluate(NODE, heartbeat(NOW, credentials={"present": False}), None, NOW, cfg)
    assert rule_names(missing) == ["credentials_missing"]
    unknown = rules.evaluate(NODE, heartbeat(NOW, credentials={"present": None, "mtime": None,
                                                              "expires_at": None}), None, NOW, cfg)
    assert unknown == ()
    stale = rules.evaluate(NODE, heartbeat(NOW, credentials={"mtime": NOW - 3 * 86400}),
                           None, NOW, cfg)
    assert rule_names(stale) == ["token_stale"] and "d" in stale[0].message
    expired = rules.evaluate(NODE, heartbeat(NOW, credentials={"expires_at": (NOW - 7200) * 1000}),
                             None, NOW, cfg)
    assert rule_names(expired) == ["token_expired"]
    within_grace = rules.evaluate(NODE, heartbeat(NOW, credentials={"expires_at": (NOW - 60) * 1000}),
                                  None, NOW, cfg)
    assert within_grace == ()


def test_disk_thresholds(cfg):
    warn = rules.evaluate(NODE, heartbeat(NOW, disk={"used_pct": 90.0}), None, NOW, cfg)
    assert (warn[0].rule, warn[0].level) == ("disk_high", "warn")
    crit = rules.evaluate(NODE, heartbeat(NOW, disk={"used_pct": 97.0}), None, NOW, cfg)
    assert crit[0].level == "critical"
    assert rules.evaluate(NODE, heartbeat(NOW, disk={"used_pct": None}), None, NOW, cfg) == ()


def test_egress_change_needs_two_heartbeats(cfg):
    previous = heartbeat(NOW - 300)
    same = rules.evaluate(NODE, heartbeat(NOW), previous, NOW, cfg)
    assert same == ()
    changed = rules.evaluate(NODE, heartbeat(NOW, egress={"ip": "198.51.100.7"}), previous, NOW, cfg)
    assert rule_names(changed) == ["egress_changed"]
    assert "203.0.113.10" in changed[0].message and "198.51.100.7" in changed[0].message
    no_ip = rules.evaluate(NODE, heartbeat(NOW, egress={"ip": None}), previous, NOW, cfg)
    assert no_ip == ()


def test_remote_control_expected(cfg):
    node = {**NODE, "rc_expected": True}
    down = rules.evaluate(node, heartbeat(NOW, remote_control={"state": "inactive"}), None, NOW, cfg)
    assert rule_names(down) == ["remote_control_down"] and "inactive" in down[0].message
    assert rules.evaluate(node, heartbeat(NOW), None, NOW, cfg) == ()
    assert rules.evaluate(NODE, heartbeat(NOW, remote_control={"state": "failed"}), None, NOW, cfg) == ()


def test_worst_level():
    assert rules.worst_level(()) == "ok"
    assert rules.worst_level((rules.Finding("a", "warn", ""),)) == "warn"
    assert rules.worst_level((rules.Finding("a", "warn", ""),
                              rules.Finding("b", "critical", ""))) == "critical"


def test_multiple_findings_are_all_reported(cfg):
    hb = heartbeat(NOW, claude={"version": None}, disk={"used_pct": 99.0},
                   credentials={"present": False})
    assert rule_names(rules.evaluate(NODE, hb, None, NOW, cfg)) == [
        "claude_missing", "credentials_missing", "disk_high"]


def test_one_missed_claude_probe_is_not_a_broken_node(cfg):
    """The binary is a symlink the installer replaces; a probe can land mid-swap."""
    working = heartbeat(NOW - 300)
    missed = heartbeat(NOW, claude={"version": None, "path": None})
    assert "claude_missing" not in rule_names(rules.evaluate(NODE, missed, working, NOW, cfg)), \
        "a single miss straight after a good sample is a transient, not a broken node"


def test_two_missed_probes_in_a_row_do_alert(cfg):
    missed = heartbeat(NOW, claude={"version": None, "path": None})
    earlier = heartbeat(NOW - 300, claude={"version": None, "path": None})
    assert "claude_missing" in rule_names(rules.evaluate(NODE, missed, earlier, NOW, cfg)), \
        "a node that really lost claude must still alert, one interval later"


def test_a_node_that_never_had_claude_alerts_immediately(cfg):
    """With no earlier heartbeat there is nothing to call this a blip."""
    missed = heartbeat(NOW, claude={"version": None, "path": None})
    assert "claude_missing" in rule_names(rules.evaluate(NODE, missed, None, NOW, cfg))


def test_a_laptop_login_goes_stale_without_a_file_to_stat(cfg):
    """No mtime on macOS, so the profile fetch time carries the staleness signal."""
    old = (NOW - 200_000) * 1000
    hb = heartbeat(NOW, credentials={"present": True, "store": "keychain",
                                     "mtime": None, "expires_at": None,
                                     "profile_fetched_at": old})
    names = rule_names(rules.evaluate(NODE, hb, None, NOW, cfg))
    assert "token_stale" in names
    assert "credentials_missing" not in names, "a Keychain login is present, not missing"


def test_a_fresh_laptop_login_raises_nothing(cfg):
    hb = heartbeat(NOW, credentials={"present": True, "store": "keychain",
                                     "mtime": None, "expires_at": None,
                                     "profile_fetched_at": (NOW - 60) * 1000})
    assert rule_names(rules.evaluate(NODE, hb, None, NOW, cfg)) == []
