"""Local UX, read-only diagnostics and private preferences; no real network/model calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_experience as ux

SECRET = "PRIVATE_TOKEN_EMAIL_PATH_DO_NOT_PRINT"


class Failure(RuntimeError):
    def __init__(self, status=None):
        super().__init__(SECRET)
        self.status = status


@pytest.fixture
def environment(tmp_path):
    root = tmp_path.resolve()
    config = root / "private-config"
    config.mkdir(mode=0o700)
    project = root / "project"
    project.mkdir()
    key, pin = config / "key", config / "pin"
    key.write_text(SECRET)
    pin.write_text("public test pin")
    key.chmod(0o600)
    pin.chmod(0o644)
    device = {
        "device_id": SECRET,
        "device_token": SECRET,
        "slot_id": SECRET,
        "server": "https://fleet.invalid",
        "key": str(key),
        "known_hosts": str(pin),
        "slot_name": SECRET,
        "user": SECRET,
    }
    context = {
        "authenticated": True,
        "protocol": 2,
        "device": {"active": True},
        "slot": {
            "state": "active",
            "ready": True,
            "health": "ready",
            "reason": SECRET,
            "email": SECRET,
            "host": SECRET,
        },
    }
    events = []

    def load():
        events.append("load_config")
        return {"devices": {"device": device}, "active": "device", "private": SECRET}

    def choose(config, slot):
        events.append(("choose", slot))
        return device

    def infer(selected, timeout):
        assert selected is device and timeout == 10
        events.append("relay")

    def version(binary, timeout):
        assert binary == "/fake/claude" and timeout == 5
        events.append("version")
        return "2.1.284"

    callbacks = {
        "load_config": load,
        "choose_device": choose,
        "find_local_claude": lambda: "/fake/claude",
        "which": lambda _: "/fake/ssh",
        "run_version": version,
        "check_managed_route": lambda: events.append("settings"),
        "check_inference": infer,
        "device_context": lambda _: context,
        "launch_local": lambda values: events.append(("local", values)) or 17,
        "launch_remote": lambda values: events.append(("remote", values)) or 19,
    }
    return SimpleNamespace(
        root=root,
        config=config,
        project=project,
        key=key,
        pin=pin,
        device=device,
        context=context,
        callbacks=callbacks,
        events=events,
        preferences=ux.Preferences(config),
    )


def codes(report):
    return {entry["code"] for entry in report["checks"]}


def test_preferences_roundtrip_private_atomic_and_project_scoped(environment):
    env = environment
    assert env.preferences.get(env.project) == {}
    assert not (env.config / "preferences.json").exists()
    saved = {"model": "opus", "effort": "high", "mode": "plan"}
    assert env.preferences.set(env.project, saved) == saved
    target = env.config / "preferences.json"
    assert target.stat().st_mode & 0o777 == 0o600
    assert (env.config / "preferences.lock").stat().st_mode & 0o777 == 0o600
    assert str(env.project) not in target.read_text() and SECRET not in target.read_text()
    alias = env.root / "project-alias"
    alias.symlink_to(env.project, target_is_directory=True)
    assert env.preferences.get(alias) == saved
    other = env.root / "other-project"
    other.mkdir()
    assert env.preferences.get(other) == {}
    env.preferences.set(other, {"mode": "manual"})
    assert env.preferences.set(env.project, {"effort": None}) == {"model": "opus", "mode": "plan"}
    env.preferences.clear(env.project)
    assert env.preferences.get(env.project) == {}
    assert env.preferences.get(other) == {"mode": "manual"}
    assert not list(env.config.glob(".preferences-*"))


def test_preferences_read_does_not_create_missing_directory(environment):
    path = environment.root / "missing"
    assert ux.Preferences(path).get(environment.project) == {}
    assert not path.exists()
    ux.Preferences(path).set(environment.project, {"model": "sonnet"})
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"email": SECRET},
        {"mode": "unsafe"},
        {"model": "../x"},
        {"model": "a\nb"},
        {"model": "-flag"},
        {"model": True},
        {"effort": "extreme"},
        {"model": "x" * 65},
    ],
)
def test_invalid_preferences_fail_without_changes(environment, values):
    with pytest.raises(ux.ExperienceError):
        environment.preferences.set(environment.project, values)
    assert not (environment.config / "preferences.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public"])
def test_unsafe_preference_file_is_not_read_or_overwritten(environment, kind):
    target = environment.config / "preferences.json"
    protected = environment.root / "preserve"
    protected.write_text(SECRET)
    protected.chmod(0o600)
    if kind == "symlink":
        target.symlink_to(protected)
    elif kind == "hardlink":
        os.link(protected, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_text("{}")
        target.chmod(0o644)
    for action in (
        lambda: environment.preferences.get(environment.project),
        lambda: environment.preferences.set(environment.project, {"model": "opus"}),
    ):
        with pytest.raises(ux.ExperienceError):
            action()
    assert protected.read_text() == SECRET


@pytest.mark.parametrize("kind", ["symlink", "public"])
def test_unsafe_config_directory_is_never_used(environment, kind):
    if kind == "symlink":
        location = environment.root / "alias"
        location.symlink_to(environment.config, target_is_directory=True)
    else:
        location = environment.config
        location.chmod(0o755)
    with pytest.raises(ux.ExperienceError):
        ux.Preferences(location).set(environment.project, {"model": "opus"})
    assert not (environment.config / "preferences.json").exists()


def test_preferences_invalid_project_does_not_scan_or_create(environment):
    for project in (environment.root / "missing", environment.key):
        with pytest.raises(ux.ExperienceError, match="project directory is unavailable"):
            environment.preferences.get(project)
    assert not (environment.config / "preferences.json").exists()


def test_preference_failed_replace_preserves_prior_data(environment, monkeypatch):
    environment.preferences.set(environment.project, {"model": "opus"})
    before = (environment.config / "preferences.json").read_bytes()

    def fail(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(ux.os, "replace", fail)
    with pytest.raises(ux.ExperienceError) as error:
        environment.preferences.set(environment.project, {"model": "sonnet"})
    assert SECRET not in str(error.value)
    assert (environment.config / "preferences.json").read_bytes() == before
    assert not list(environment.config.glob(".preferences-*"))


@pytest.mark.parametrize(
    "data",
    [
        "{broken",
        '{"version":true,"projects":{}}',
        '{"version":1,"projects":{},"secret":"x"}',
        '{"version":1,"projects":{"bad":{}}}',
    ],
)
def test_corrupt_preferences_are_preserved(environment, data):
    target = environment.config / "preferences.json"
    target.write_text(data)
    target.chmod(0o600)
    with pytest.raises(ux.ExperienceError):
        environment.preferences.set(environment.project, {"mode": "plan"})
    assert target.read_text() == data


def test_doctor_returns_fixed_allowlisted_report_without_raw_context(environment):
    report = ux.doctor(environment.callbacks, privacy=True)
    assert report["ok"] is True
    assert {
        "ssh_ready",
        "claude_ready",
        "version_ready",
        "settings_ready",
        "pairing_ready",
        "key_ready",
        "pin_ready",
        "device_ready",
        "slot_report_ready",
        "relay_ready",
    } == codes(report)
    assert SECRET not in json.dumps(report) and str(environment.root) not in json.dumps(report)
    assert report["privacy"] == list(ux.PRIVACY)
    assert environment.events == ["version", "settings", "load_config", ("choose", ""), "relay"]
    assert not (environment.config / "preferences.json").exists()


def test_status_uses_no_native_process_settings_or_model_calls(environment):
    def forbidden(*args, **kwargs):
        pytest.fail("status must not launch Claude or read managed settings")

    for name in (
        "find_local_claude",
        "run_version",
        "check_managed_route",
        "launch_local",
        "launch_remote",
    ):
        environment.callbacks[name] = forbidden
    report = ux.status(environment.callbacks)
    assert report["kind"] == "status" and report["ok"] is True
    assert "version_ready" not in codes(report)


def compatibility_record(state="passed", reason=None):
    now = ux.time.time()
    return {"state": state, "checked_at": now - 10, "native_version": "2.1.295",
            "usage_observed_at": now - 10, "last_success_at": now - 10,
            "next_check_at": now + 300,
            "checks": dict.fromkeys(ux.COMPATIBILITY_CHECKS, True),
            **({"reason": reason} if reason else {})}


def test_client_compatibility_enums_match_node_contract_without_runtime_dependency():
    from ccfleet_agent import compatibility
    assert ux.COMPATIBILITY_STATES == compatibility.STATES
    assert ux.COMPATIBILITY_CHECKS == compatibility.CHECKS
    assert ux.COMPATIBILITY_REASONS == compatibility.REASONS


@pytest.mark.parametrize("kind", ["status", "doctor"])
@pytest.mark.parametrize("state,reason", [("passed", None), ("pending", "usage_pending"),
                                       ("failed", "native_interface_changed"),
                                       ("blocked", "sign_in_pending")])
def test_compatibility_warnings_are_visible_without_disabling_reported_model_readiness(
        environment, kind, state, reason):
    environment.context["slot"]["compatibility"] = compatibility_record(state, reason)
    report = getattr(ux, kind)(environment.callbacks)
    assert report["ok"] is True and "relay_ready" in codes(report)
    assert "compatibility_" + state in codes(report)
    assert report["compatibility"]["state"] == state
    assert report["compatibility"]["native_version"] == "2.1.295"
    assert environment.events.count("relay") == 1
    assert not any(isinstance(event, tuple) and event[0] in {"local", "remote"}
                   for event in environment.events)
    rendered = ux.render_report(report)
    if state == "failed":
        assert rendered.startswith("CC Fleet " + kind + ": operator review needed")
        assert "Contact your operator" in rendered
    elif state != "passed":
        assert rendered.startswith("CC Fleet " + kind + ": ready with warnings")
    else:
        assert "provider acceptance is unverified" in rendered


def test_public_compatibility_metadata_and_exports_drop_fingerprints_and_raw_messages(environment):
    raw = {**compatibility_record("failed", "relay_tls_policy"), "runtime_fp": SECRET,
           "account_fp": SECRET, "raw_output": SECRET, "path": SECRET, "token": SECRET,
           "message": SECRET}
    raw["checks"][SECRET] = True
    environment.context["slot"]["compatibility"] = raw
    report = ux.doctor(environment.callbacks, privacy=True)
    allowed = {"state", "reason", "native_version", "checked_at", "next_check_at",
               "last_success_at", "usage_observed_at", "checks"}
    assert set(report["compatibility"]) <= allowed
    assert set(report["compatibility"]["checks"]) == ux.COMPATIBILITY_CHECKS
    assert SECRET not in json.dumps(report) and SECRET not in ux.render_report(report)
    target = environment.root / "compatibility-support.json"
    ux.export_report(report, target)
    assert target.stat().st_mode & 0o777 == 0o600
    assert SECRET not in target.read_text()
    assert json.loads(target.read_text())["compatibility"] == report["compatibility"]


@pytest.mark.parametrize("kind", ["status", "doctor"])
@pytest.mark.parametrize("reason", ["native_auth_source", "native_extensions"])
def test_blocked_native_configuration_requires_operator_review_without_model_denial(
        environment, kind, reason):
    environment.context["slot"]["compatibility"] = compatibility_record(
        "blocked", reason)
    report = getattr(ux, kind)(environment.callbacks)
    assert report["ok"] is True and "relay_ready" in codes(report)
    assert "compatibility_operator_review" in codes(report)
    assert report["compatibility"]["state"] == "blocked"
    rendered = ux.render_report(report)
    assert rendered.startswith("CC Fleet " + kind + ": operator review needed")
    assert "review the hosted native settings" in rendered
    assert "Keep the saved pairing" in rendered and "Sign in again" not in rendered
    assert environment.events.count("relay") == 1


@pytest.mark.parametrize("field,bad", [
    ("state", SECRET), ("checked_at", SECRET), ("checked_at", True),
    ("checked_at", 0), ("checked_at", float("nan")), ("checked_at", 10 ** 400),
    ("native_version", SECRET), ("usage_observed_at", False),
    ("checks", {"native_version": True}),
    ("checks", {**dict.fromkeys(ux.COMPATIBILITY_CHECKS, True), "usage_parser": 1}),
])
def test_malformed_compatibility_is_unverified_without_echoing_data_or_blocking_access(
        environment, field, bad):
    raw = compatibility_record()
    raw[field] = bad
    environment.context["slot"]["compatibility"] = raw
    report = ux.status(environment.callbacks)
    assert report["ok"] and "relay_ready" in codes(report)
    assert "compatibility_unverified" in codes(report) and "compatibility" not in report
    assert SECRET not in json.dumps(report) and SECRET not in ux.render_report(report)


def test_future_compatibility_check_is_not_claimed_passed(environment):
    raw = compatibility_record()
    raw["checked_at"] = ux.time.time() + 1000
    environment.context["slot"]["compatibility"] = raw
    assert "compatibility_unverified" in codes(ux.status(environment.callbacks))


def test_diagnostic_export_rejects_conflicting_compatibility_state_without_private_error(environment):
    environment.context["slot"]["compatibility"] = compatibility_record("failed", "relay_unavailable")
    report = ux.status(environment.callbacks)
    report["compatibility"] = compatibility_record("passed")
    target = environment.root / "invalid-compatibility.json"
    with pytest.raises(ux.ExperienceError) as error:
        ux.export_report(report, target)
    assert SECRET not in str(error.value) and not target.exists()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "relay_renewal"),
        (403, "relay_disabled"),
        (409, "relay_transition"),
        (None, "relay_connection"),
        ({"private": SECRET}, "relay_connection"),
    ],
)
def test_distinct_relay_failures_never_export_private_exceptions(environment, status, code):
    def failure(*args, **kwargs):
        raise Failure(status)

    environment.callbacks["check_inference"] = failure
    report = ux.doctor(environment.callbacks)
    assert code in codes(report) and report["ok"] is False
    assert SECRET not in ux.render_report(report)


@pytest.mark.parametrize(
    "status,code,ok",
    [
        (401, "device_revoked", False),
        (403, "device_forbidden", False),
        (404, "device_unchecked", True),
        (None, "device_connection", False),
    ],
)
def test_device_context_is_separate_from_slot_credential_health(environment, status, code, ok):
    def failure(*args, **kwargs):
        raise Failure(status)

    environment.callbacks["device_context"] = failure
    report = ux.doctor(environment.callbacks)
    assert code in codes(report) and report["ok"] is ok
    assert "relay_ready" in codes(report)


@pytest.mark.parametrize(
    "health,code",
    [
        ("renewal_pending", "slot_report_pending"),
        ("degraded", "slot_report_degraded"),
        ("sign_in_required", "slot_report_pending"),
        ("switching", "slot_report_pending"),
    ],
)
def test_slot_report_is_observation_not_authoritative_relay_test(environment, health, code):
    environment.context["slot"].update(health=health, ready=False)
    report = ux.status(environment.callbacks)
    assert code in codes(report) and "relay_ready" in codes(report)


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"authenticated": "yes"},
        {
            "authenticated": True,
            "device": {"active": True},
            "protocol": True,
            "slot": {"state": "active", "ready": True, "health": "ready"},
        },
    ],
)
def test_invalid_device_context_is_never_called_ready(environment, context):
    environment.callbacks["device_context"] = lambda _: context
    report = ux.status(environment.callbacks)
    assert report["ok"] is False and "device_invalid" in codes(report)


def test_readiness_does_not_hide_revoked_pairing_or_bad_host_pin(environment):
    environment.pin.unlink()
    environment.pin.symlink_to(environment.key)
    report = ux.doctor(environment.callbacks)
    assert report["ok"] is False and {"pin_invalid", "relay_unchecked"} <= codes(report)
    assert "relay" not in environment.events


@pytest.mark.parametrize(
    "name,code",
    [
        ("find_local_claude", "claude_missing"),
        ("check_managed_route", "settings_conflict"),
        ("load_config", "pairing_invalid"),
        ("choose_device", "pairing_missing"),
        ("run_version", "version_unavailable"),
    ],
)
def test_local_failures_are_safely_categorized(environment, name, code):
    def failure(*args, **kwargs):
        raise Failure()

    environment.callbacks[name] = failure
    report = ux.doctor(environment.callbacks)
    assert code in codes(report) and SECRET not in json.dumps(report)


def test_version_timeout_and_untrusted_output_are_not_echoed(environment):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(SECRET, 5, output=SECRET)

    environment.callbacks["run_version"] = timeout
    report = ux.doctor(environment.callbacks)
    assert "version_timeout" in codes(report) and SECRET not in json.dumps(report)
    environment.callbacks["run_version"] = lambda *a, **kw: "2.1.284\n" + SECRET
    report = ux.doctor(environment.callbacks)
    assert "version_unavailable" in codes(report) and SECRET not in json.dumps(report)


def test_export_rebuilds_fixed_fields_is_private_and_never_overwrites(environment):
    report = ux.doctor(environment.callbacks, privacy=True)
    report["private"] = SECRET
    report["checks"][0].update(message=SECRET, advice=SECRET, extra=SECRET)
    report["privacy"] = [SECRET]
    destination = environment.root / "support.json"
    ux.export_report(report, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert SECRET not in destination.read_text()
    parsed = json.loads(destination.read_text())
    assert parsed == ux.safe_report(report) and parsed["privacy"] == list(ux.PRIVACY)
    with pytest.raises(ux.ExperienceError):
        ux.export_report(report, destination)
    alias = environment.root / "link.json"
    alias.symlink_to(destination)
    before = destination.read_bytes()
    with pytest.raises(ux.ExperienceError):
        ux.export_report(report, alias)
    assert destination.read_bytes() == before


@pytest.mark.parametrize(
    "checks",
    [
        [{"code": SECRET}],
        [{"code": {"secret": SECRET}}],
        [],
        [{"code": "version_ready", "version": SECRET}],
        [{"code": "ssh_ready"}, {"code": "ssh_missing"}],
    ],
)
def test_export_rejects_invalid_report_without_creating_file(environment, checks):
    destination = environment.root / "support.json"
    with pytest.raises(ux.ExperienceError):
        ux.export_report({"kind": "doctor", "checks": checks}, destination)
    assert not destination.exists()


@pytest.mark.parametrize("action", ["new", "continue", "resume"])
def test_launch_uses_native_callbacks_preserves_history_defaults_and_consent(environment, action):
    env = environment
    env.preferences.set(env.project, {"model": "sonnet", "mode": "plan", "effort": "high"})
    assert ux.launch(action, env.callbacks, project=env.project, preferences=env.preferences) == 17
    kind, values = env.events[-1]
    assert kind == "local" and "yes" not in values
    if action == "new":
        assert values["model"] == "sonnet" and values["mode"] == "plan"
    else:
        assert not {"model", "mode", "effort"}.intersection(values)
    assert values["resume"] == ("" if action == "resume" else None)
    assert values["continue_session"] is (action == "continue")


def test_launch_explicit_values_override_preferences_and_native_resume_skips_defaults(environment):
    env = environment
    env.preferences.set(env.project, {"model": "sonnet", "effort": "high"})
    ux.launch(
        "new",
        env.callbacks,
        project=env.project,
        preferences=env.preferences,
        options={"model": "opus", "effort": None, "name": "research", "yes": False},
    )
    assert env.events[-1][1]["model"] == "opus" and env.events[-1][1]["effort"] == "high"
    assert env.events[-1][1]["yes"] is False
    ux.launch(
        "new",
        env.callbacks,
        project=env.project,
        preferences=env.preferences,
        options={"claude_args": ["--resume=existing"]},
    )
    assert "effort" not in env.events[-1][1] and "model" not in env.events[-1][1]


def menu(environment, answers, **kwargs):
    output = []
    choices = iter(answers)
    result = ux.menu(
        environment.callbacks,
        project=environment.project,
        preferences=environment.preferences,
        input_fn=lambda _: next(choices),
        output_fn=output.append,
        is_tty=True,
        **kwargs,
    )
    return result, "\n".join(output)


@pytest.mark.parametrize("selection,action", [("1", "new"), ("2", "continue"), ("3", "resume")])
def test_guided_menu_dispatches_only_selected_native_action(environment, selection, action):
    result, _ = menu(environment, [selection])
    assert result == 17
    values = environment.events[-1][1]
    assert values["new_session"] is (action == "new")
    assert "yes" not in values


@pytest.mark.parametrize("selection", ["1", "2", "3"])
def test_menu_preserves_explicit_permission_model_and_consent_options(environment, selection):
    options = {"mode": "plan", "model": "sonnet", "effort": "low", "name": "review", "yes": False}
    result, _ = menu(environment, [selection], options=options)
    assert result == 17
    assert all(environment.events[-1][1][key] == value for key, value in options.items())


def test_remote_menu_forwards_modes_but_not_local_only_options(environment):
    result, _ = menu(environment, ["4", "y"], options={"mode": "plan", "model": "sonnet",
                                                       "effort": "low", "name": "local", "yes": True})
    assert result == 19
    assert environment.events[-1] == ("remote", {"slot": "", "mode": "plan",
                                                "model": "sonnet", "effort": "low"})


@pytest.mark.parametrize("answer,launched", [("y", True), ("n", False), ("", False)])
def test_remote_menu_requires_explicit_choice_and_confirmation(environment, answer, launched):
    result, output = menu(environment, ["4", answer])
    assert "does not share or synchronize" in output
    assert result == (19 if launched else 0)
    assert bool(environment.events) is launched


def test_preferences_menu_edits_only_explicit_local_field(environment):
    result, _ = menu(environment, ["5", "2", "high"])
    assert result == 0 and environment.preferences.get(environment.project) == {"effort": "high"}
    assert environment.events == []
    menu(environment, ["5", "4"])
    assert environment.preferences.get(environment.project) == {}


def test_doctor_menu_and_quit_never_launch(environment):
    result, output = menu(environment, ["6"])
    assert result == 0 and "Privacy boundaries" in output and SECRET not in output
    assert not any(
        isinstance(event, tuple) and event[0] in ("local", "remote") for event in environment.events
    )
    environment.events.clear()
    assert menu(environment, ["0"])[0] == 0 and environment.events == []


def test_noninteractive_menu_fails_without_reading_stdin_or_launching(environment):
    with pytest.raises(ux.ExperienceError, match="needs a terminal"):
        ux.menu(
            environment.callbacks,
            project=environment.project,
            is_tty=False,
            input_fn=lambda _: pytest.fail("must not read piped input"),
        )
    assert environment.events == []


@pytest.mark.parametrize(
    "contents,expected",
    [
        ("print('2.1.284 (Claude Code)')", "2.1.284"),
        ("print('2.1.284')", "2.1.284"),
        ("print('private output')", None),
        ("print('x' * 5000)", None),
    ],
)
def test_native_version_is_bounded_and_parsed_without_other_output(tmp_path, contents, expected):
    binary = tmp_path / "native"
    binary.write_text(f"#!{sys.executable}\n{contents}\n")
    binary.chmod(0o700)
    if expected:
        assert ux.native_version(str(binary)) == expected
    else:
        with pytest.raises(ux.ExperienceError):
            ux.native_version(str(binary))


def test_native_version_timeout_is_bounded(tmp_path):
    binary = tmp_path / "native"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
    binary.chmod(0o700)
    with pytest.raises(subprocess.TimeoutExpired):
        ux.native_version(str(binary), timeout=0.1)
