"""Background input errors must be rejected before cancelling legacy work."""
from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest

from ccfleet_agent import local_jobs
from tests.test_native_local_cli import client as client
from tests.test_platform_cli import (
    PROMPT,
    invoke,
    preferences,
)
from tests.test_platform_cli import (
    job_capture as job_capture,
)
from tests.test_platform_cli import (
    platform as platform,
)


@pytest.fixture
def preflight(platform, job_capture, monkeypatch):
    started, helper = job_capture
    order = []
    monkeypatch.setitem(platform.scope, "check_inference", lambda *a, **k: order.append("ready"))
    monkeypatch.setitem(platform.scope, "retire_live_grants",
                        lambda device, *, yes: order.append(("retire", yes)))
    original = helper.start

    def start(*args, **kwargs):
        order.append("start")
        return original(*args, **kwargs)

    helper.start = start
    history = platform.home / ".claude/history-kept"
    history.parent.mkdir(exist_ok=True)
    history.write_text("synthetic history stays on this computer")
    return SimpleNamespace(platform=platform, started=started, helper=helper, order=order,
                           history=history)


def command(preflight, arguments, *, prompt=PROMPT, local=False):
    platform = preflight.platform
    before = platform.cli["config_path"]().read_bytes()
    history = preflight.history.read_bytes()
    prefix = ["local", "--background", "--print", prompt] if local else [
        "jobs", "start", "--prompt", prompt]
    result = invoke(platform, *prefix, "--project", str(platform.home), *arguments)
    assert platform.cli["config_path"]().read_bytes() == before
    assert preflight.history.read_bytes() == history
    assert not platform.calls  # No original Claude process was started by this preflight.
    assert not (platform.cli["home"]() / ".inference-settings-preflight").exists()
    return result


def rejected(preflight, arguments, **options):
    assert command(preflight, arguments, **options) == 2
    assert preflight.order == []
    assert preflight.started == []
    assert not (preflight.platform.cli["home"]() / "jobs").exists()


@pytest.mark.parametrize("local", [False, True], ids=["jobs-start", "local-background"])
@pytest.mark.parametrize("flags", [
    ["--model", "invalid/model"],
    ["--name", "invalid\nname"],
    ["--name", "x" * 257],
    ["--resume", "invalid\nname"],
    ["--resume", ""],
    ["--fork-session"],
    ["--legacy-history"],
    ["--", "--settings", "private.json"],
    ["--", "--client-data-url=https://other.invalid"],
    ["--", "--bg"],
    ["--", "--resume"],
    ["--", "-r"],
    ["--", "--resume="],
    ["--", "--", "second prompt"],
])
def test_bad_background_inputs_never_check_readiness_retire_or_start(preflight, flags, local):
    rejected(preflight, ["--yes", *flags], local=local)


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_bad_project_cannot_end_an_old_session(preflight, local, kind):
    project = preflight.platform.home / "invalid-project"
    if kind == "file":
        project.write_text("not a directory")
    rejected(preflight, ["--project", str(project), "--yes"], local=local)


@pytest.mark.parametrize("flags", [
    ["--timeout", "0"], ["--timeout", "86401"],
    ["--max-jobs", "0"], ["--max-jobs", "17"],
    ["--resume", "work", "--continue"],
])
def test_resource_bounds_and_conflicting_history_selection_are_preflight_errors(preflight, flags):
    rejected(preflight, ["--yes", *flags])


@pytest.mark.parametrize("prompt", ["", " ", "bad\0prompt", "x" * (local_jobs.MAX_SPEC_BYTES + 1),
                                    "😀" * 24000])
def test_prompt_and_encoded_spec_bounds_are_checked_before_migration(preflight, prompt, capsys):
    rejected(preflight, ["--yes"], prompt=prompt)
    output = capsys.readouterr()
    assert "bad\0prompt" not in output.out + output.err


def test_bad_stdin_prompt_does_not_reach_readiness_or_cleanup(preflight, monkeypatch):
    monkeypatch.setattr(preflight.platform.cli["sys"], "stdin", io.StringIO("bad\0prompt"))
    rejected(preflight, ["--yes"], prompt="-")


@pytest.mark.parametrize("kind", ["preferences", "native-settings"])
def test_invalid_local_settings_are_detected_without_retiring_old_work(preflight, kind):
    platform = preflight.platform
    target = (platform.cli["home"]() / "preferences.json" if kind == "preferences"
              else platform.home / ".claude/settings.json")
    target.write_text("{invalid")
    target.chmod(0o600)
    rejected(preflight, ["--yes"])
    assert target.read_text() == "{invalid"


@pytest.mark.parametrize("field", ["device_id", "slot_id"])
def test_invalid_saved_job_identity_is_rejected_by_the_canonical_spec_guard(preflight, field):
    platform = preflight.platform
    config = platform.cli["load_config"]()
    config["devices"]["device"][field] = "invalid\nidentity"
    platform.cli["save_config"](config)
    rejected(preflight, ["--yes"])


def test_native_argument_list_limit_is_checked_before_retirement(preflight):
    rejected(preflight, ["--yes", "--", *(["--verbose"] * 129)])


def test_valid_launch_keeps_permission_preferences_and_explicit_consent(preflight):
    platform = preflight.platform
    preferences(platform).set(platform.home, {"model": "sonnet", "effort": "high", "mode": "plan"})
    assert command(preflight, ["--yes", "--model", "opus", "--timeout", "45", "--max-jobs", "2"]) == 0
    assert preflight.order == ["ready", ("retire", True), "start"]
    _, spec, runner, limits = preflight.started[0]
    assert spec["options"]["mode"] == "plan" and spec["options"]["effort"] == "high"
    assert spec["options"]["model"] == "opus"
    assert limits == {"timeout_s": 45.0, "max_jobs": 2}
    assert PROMPT not in " ".join(runner)


@pytest.mark.parametrize("flags", [["--continue"], ["--resume", "work"],
                                    ["--", "--resume=work"], ["--", "--resume", "work"]])
def test_resumed_background_jobs_do_not_read_new_session_preferences(preflight, flags, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("resume must not load new-session defaults")

    monkeypatch.setattr(preflight.platform.scope["client_experience"](), "Preferences", forbidden)
    assert command(preflight, flags) == 0
    assert preflight.order == ["ready", ("retire", False), "start"]
    assert not {"mode", "model", "effort"}.intersection(preflight.started[0][1]["options"])


def test_existing_legacy_profile_is_selected_without_reading_history(preflight):
    platform = preflight.platform
    identifier = hashlib.sha256(platform.device["slot_id"].encode()).hexdigest()[:16]
    profile = platform.cli["home"]() / "local" / identifier
    profile.mkdir(parents=True)
    native_history = profile / "history-sentinel"
    native_history.write_text("existing preview history")
    assert command(preflight, ["--legacy-history", "--resume", "work"]) == 0
    assert native_history.read_text() == "existing preview history"


def test_readiness_failure_after_valid_preflight_still_preserves_legacy_work(preflight, monkeypatch):
    def unavailable(*args, **kwargs):
        preflight.order.append("ready")
        raise preflight.platform.cli["CliError"]("slot unavailable")

    monkeypatch.setitem(preflight.platform.scope, "check_inference", unavailable)
    assert command(preflight, ["--yes"]) == 2
    assert preflight.order == ["ready"] and not preflight.started


def test_declined_migration_is_not_turned_into_implicit_background_consent(preflight, monkeypatch):
    def declined(device, *, yes):
        preflight.order.append(("retire", yes))
        raise preflight.platform.cli["CliError"]("migration cancelled")

    monkeypatch.setitem(preflight.platform.scope, "retire_live_grants", declined)
    assert command(preflight, []) == 2
    assert preflight.order == ["ready", ("retire", False)] and not preflight.started
