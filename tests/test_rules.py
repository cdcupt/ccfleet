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


# -- quota ------------------------------------------------------------------------

def _quota_payload(session=None, week=None, checked_at=1000.0):
    quota = {"checked_at": checked_at}
    if session is not None:
        quota["session"] = session
    if week is not None:
        quota["week"] = week
    return {"quota": quota}


def _quota_cfg(**env):
    from ccfleetd.config import Config
    base = {"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:"}
    return Config.from_env({**base, **env})


def _quota_rules(payload, now=1000.0, **env):
    from ccfleetd.rules import _quota_findings
    return {f.rule: f for f in _quota_findings(payload, now, _quota_cfg(**env))}


def test_quota_warns_before_a_window_runs_out():
    """The console has shown these since the windows landed; this is the half
    that reaches you without anyone looking at the page."""
    found = _quota_rules(_quota_payload(session={"used_pct": 80, "resets": "7:50pm (UTC)"}))
    assert set(found) == {"quota_high_session"}
    assert found["quota_high_session"].level == "warn"
    assert "5-hour window 80% used" in found["quota_high_session"].message
    assert "resets 7:50pm (UTC)" in found["quota_high_session"].message, "say when it clears"

    found = _quota_rules(_quota_payload(week={"used_pct": 93}))
    assert found["quota_high_week"].level == "critical"
    assert "weekly window 93% used" in found["quota_high_week"].message


def test_quota_is_quiet_while_there_is_room():
    assert _quota_rules(_quota_payload(session={"used_pct": 4}, week={"used_pct": 15})) == {}
    # Both thresholds are inclusive: exactly at it counts, one below does not.
    assert _quota_rules(_quota_payload(week={"used_pct": 75}))["quota_high_week"].level == "warn"
    assert _quota_rules(_quota_payload(week={"used_pct": 74})) == {}
    at_crit = _quota_rules(_quota_payload(week={"used_pct": 90}))["quota_high_week"]
    assert at_crit.level == "critical", "90% is critical, not one short of it"
    assert _quota_rules(_quota_payload(week={"used_pct": 89}))["quota_high_week"].level == "warn"


def test_the_two_windows_alert_separately():
    """One rule for both would flap as whichever is worse changes, and would
    hide a full week behind a fresh session."""
    found = _quota_rules(_quota_payload(session={"used_pct": 5}, week={"used_pct": 95}))
    assert set(found) == {"quota_high_week"}, "a quiet session must not mask a full week"
    found = _quota_rules(_quota_payload(session={"used_pct": 99}, week={"used_pct": 99}))
    assert found["quota_high_session"].level == "critical"
    assert found["quota_high_week"].level == "critical"


def test_a_stale_reading_is_not_evidence_about_now():
    """The agent refreshes every 30 min. Two missed refreshes means the read is
    broken, not that the quota is; alerting would name the wrong problem."""
    fresh = _quota_payload(week={"used_pct": 95}, checked_at=1000.0)
    assert "quota_high_week" in _quota_rules(fresh, now=1000.0 + 3600)
    assert _quota_rules(fresh, now=1000.0 + 3 * 3600) == {}
    # No timestamp at all is not a reason to go quiet; older payloads lack one.
    assert "quota_high_week" in _quota_rules({"quota": {"week": {"used_pct": 95}}}, now=9e9)


def test_a_node_cannot_break_the_rule_with_nonsense():
    for junk in (None, "lots", [1], {"used_pct": "high"}, {}):
        assert _quota_rules(_quota_payload(week=junk)) == {}
    assert _quota_rules({"quota": "not a mapping"}) == {}
    assert _quota_rules({}) == {}
    # True is an int in Python, so it passes an isinstance check for one. It
    # cannot reach 75, which hides the missing guard at the default thresholds;
    # lower the bar and a bool would alert as "window 1% used" without it.
    assert _quota_rules(_quota_payload(week={"used_pct": True}),
                        CCFLEET_QUOTA_WARN_PCT="1", CCFLEET_QUOTA_CRIT_PCT="2") == {}


def test_quota_thresholds_are_configurable():
    assert _quota_rules(_quota_payload(week={"used_pct": 60})) == {}
    loose = _quota_rules(_quota_payload(week={"used_pct": 60}),
                         CCFLEET_QUOTA_WARN_PCT="50", CCFLEET_QUOTA_CRIT_PCT="95")
    assert loose["quota_high_week"].level == "warn"


# -- a shared machine -----------------------------------------------------------

def machine_beat(ts, slots=(), **extra):
    """What a shared machine's agent sends: machine facts and its slots, and no
    owner login, because the machine has none."""
    beat = heartbeat(ts, **extra)
    for key in ("claude", "credentials", "remote_control", "quota"):
        beat["payload"].pop(key, None)
    beat["payload"]["mode"] = "machine"
    beat["payload"]["slots"] = list(slots)
    return beat


def slot_row(user, state):
    return {"id": f"s-{user}", "unix_user": user, "state": state}


def test_a_shared_machine_is_not_paged_about_a_login_it_does_not_have(cfg):
    """Judged as an ordinary node, a machine agent's report is "claude missing"
    and "not signed in", critical, forever — about an owner that does not
    exist. Every login on it belongs to a slot."""
    rc_node = {**NODE, "rc_expected": True, "pinned_version": "2.1.90"}
    assert rules.evaluate(rc_node, machine_beat(NOW), None, NOW, cfg) == ()


def test_a_shared_machine_is_still_watched_as_a_machine(cfg):
    full = rules.evaluate(NODE, machine_beat(NOW, disk={"used_pct": 99.0}), None, NOW, cfg)
    assert rule_names(full) == ["disk_high"]
    stale = rules.evaluate(NODE, machine_beat(NOW - 3600), None, NOW, cfg)
    assert rule_names(stale) == ["no_heartbeat"]
    moved = rules.evaluate(NODE, machine_beat(NOW, egress={"ip": "203.0.113.99"}),
                           machine_beat(NOW - 60), NOW, cfg)
    assert rule_names(moved) == ["egress_changed"]


def test_healthy_slots_say_nothing(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot01", "present": True},
                              {"unix_user": "slot02", "present": False}])
    rows = [slot_row("slot01", "active"), slot_row("slot02", "free")]
    assert rules.evaluate(NODE, beat, None, NOW, cfg, rows) == ()


def test_a_wipe_that_failed_is_critical_and_names_its_slot(cfg):
    """The one failure with no recovery is handing out a slot that still holds
    somebody's work, so a wipe that did not happen is the operator's to know."""
    beat = machine_beat(NOW, [{"unix_user": "slot02", "present": True,
                               "wipe_error": "processes still running"}])
    [finding] = rules.evaluate(NODE, beat, None, NOW, cfg, [slot_row("slot02", "releasing")])
    assert finding.rule == "slot_wipe_failed:slot02"
    assert finding.level == "critical"
    assert "processes still running" in finding.message


def test_a_free_slot_whose_user_exists_is_critical(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot03", "present": True}])
    [finding] = rules.evaluate(NODE, beat, None, NOW, cfg, [slot_row("slot03", "free")])
    assert finding.rule == "slot_occupied:slot03" and finding.level == "critical"
    assert "slot-remove.sh --slot slot03" in finding.message, "says what to do about it"


def test_a_held_slot_whose_user_vanished_is_critical(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot01", "present": False}])
    for state in ("claimed", "active"):
        [finding] = rules.evaluate(NODE, beat, None, NOW, cfg, [slot_row("slot01", state)])
        assert finding.rule == "slot_missing:slot01" and finding.level == "critical"
    # A machine that could not tell has not said the user is gone.
    unsure = machine_beat(NOW, [{"unix_user": "slot01", "present": None}])
    assert rules.evaluate(NODE, unsure, None, NOW, cfg, [slot_row("slot01", "active")]) == ()


def test_provisioning_that_failed_warns_while_the_slot_is_cleared(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot01", "present": True,
                               "provision_error": "installer unreachable"}])
    for state in ("claiming", "releasing"):
        found = rules.evaluate(NODE, beat, None, NOW, cfg, [slot_row("slot01", state)])
        assert [(f.rule, f.level) for f in found] == [("slot_provision_failed:slot01", "warn")]
        assert "installer unreachable" in found[0].message
    # Once the slot is free again the claim is over, and so is the alert.
    assert rules.evaluate(NODE, machine_beat(NOW, [{"unix_user": "slot01", "present": False,
                                                    "provision_error": "old news"}]),
                          None, NOW, cfg, [slot_row("slot01", "free")]) == ()


def test_two_slots_in_trouble_are_two_alerts(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot01", "present": True, "wipe_error": "a"},
                              {"unix_user": "slot02", "present": True, "wipe_error": "b"}])
    rows = [slot_row("slot01", "releasing"), slot_row("slot02", "releasing")]
    assert rule_names(rules.evaluate(NODE, beat, None, NOW, cfg, rows)) == [
        "slot_wipe_failed:slot01", "slot_wipe_failed:slot02"]


def test_a_slot_the_machine_did_not_mention_raises_nothing(cfg):
    """A slot declared since the machine last looked has simply not been
    reported yet; that is not a finding."""
    assert rules.evaluate(NODE, machine_beat(NOW), None, NOW, cfg,
                          [slot_row("slot01", "free")]) == ()


def test_a_wipe_error_on_a_slot_not_being_wiped_is_ignored(cfg):
    beat = machine_beat(NOW, [{"unix_user": "slot01", "present": True, "wipe_error": "x"}])
    assert rules.evaluate(NODE, beat, None, NOW, cfg, [slot_row("slot01", "active")]) == ()
