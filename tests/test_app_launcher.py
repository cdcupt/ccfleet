"""App launchers use synthetic native children and existing mocked Fleet authority."""

import contextlib
import hashlib
import json
import os
import pty
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_release as release
from tests.test_client_release import distribution, install
from tests.test_client_release import signer as signer

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SYNTHETIC_FLEET_APP_SECRET"
HARNESS = """
import json, os, runpy, sys
cli = runpy.run_path(sys.argv[1])
scope = cli['cmd_external_claude'].__globals__
route = json.loads(os.environ['TEST_FLEET_ROUTE'])
def resolve(config, wanted, transport, **options):
    with open(os.environ['TEST_ROUTE_CALL'], 'w') as record:
        json.dump({'slot': wanted, 'transport': transport, 'options': options}, record)
    return route
scope['load_config'] = lambda: {}
scope['load_gateway'] = lambda: route['gateway']
scope['local_route'] = resolve
def managed():
    if os.environ.get('TEST_MANAGED_ROUTE'):
        raise scope['CliError']('managed routing conflict')
scope['check_managed_route'] = managed
scope['native_proxy_exclusions'] = lambda *args: None
raise SystemExit(cli['main'](['__external-claude', '--launcher', 'app', '--', *sys.argv[2:]]))
"""


@pytest.fixture
def app(tmp_path, monkeypatch, capsys):
    directory = tmp_path.resolve()
    config = directory / "fleet"
    monkeypatch.setenv("CCFLEET_HOME", str(config))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory / "native-config"))
    native = directory / "real-claude"
    record = directory / "native-record.json"
    native.write_text("#!" + sys.executable + "\n" + """
import json, os, pathlib, signal, sys, time
if sys.argv[1:] == ['--version']:
    print('2.1.999 (Claude Code)')
    raise SystemExit(0)
if sys.argv[1:] == ['--help']:
    print('Native Claude help')
    raise SystemExit(0)
args = sys.argv[1:]
settings = pathlib.Path(args[args.index('--settings') + 1])
body = {'argv': args, 'cwd': os.getcwd(), 'pid': os.getpid(),
        'blocked_signals': list(signal.pthread_sigmask(signal.SIG_BLOCK, [])),
        'settings': json.loads(settings.read_text()),
        'settings_mode': settings.stat().st_mode & 0o777,
        'env': {key: os.environ.get(key) for key in (
            'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
            'ANTHROPIC_CUSTOM_HEADERS', 'ANTHROPIC_MODEL', 'CLAUDE_CODE_EFFORT_LEVEL')}}
if os.environ.get('TEST_TTY'):
    terminal = os.open('/dev/tty', os.O_RDWR)
    body['controlling_terminal'] = os.isatty(terminal)
    os.close(terminal)
    signal.signal(signal.SIGWINCH, lambda number, frame:
        pathlib.Path(os.environ['TEST_NATIVE_RECORD'] + '.resized').write_text(str(number)))
    signal.signal(signal.SIGINT, lambda number, frame:
        pathlib.Path(os.environ['TEST_NATIVE_RECORD'] + '.interrupted').write_text(str(number)))
if not os.environ.get('TEST_HOLD'):
    body['stdin'] = sys.stdin.read()
if os.environ.get('TEST_HOLD'):
    def stopped(number, frame):
        pathlib.Path(os.environ['TEST_NATIVE_RECORD'] + '.terminated').write_text(str(number))
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped if os.environ.get('TEST_GRACEFUL') else signal.SIG_IGN)
pathlib.Path(os.environ['TEST_NATIVE_RECORD']).write_text(json.dumps(body))
if os.environ.get('TEST_HOLD'):
    while True:
        time.sleep(.05)
print('{"type":"result","result":"synthetic"}')
raise SystemExit(int(os.environ.get('TEST_NATIVE_EXIT', '0')))
""")
    native.chmod(0o700)
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_launcher_create"].__globals__
    route = {"slot_id": "stable-slot", "provider": "anthropic", "gateway": {
        "base_url": "https://fleet.invalid", "secret": SECRET, "provider": "anthropic"}}
    monkeypatch.setitem(scope, "load_config", lambda: {})
    monkeypatch.setitem(scope, "local_route", lambda *args, **kwargs: route)
    assert cli["main"](["launcher", "create", "app", "--slot", "display-name",
                        "--claude", str(native)]) == 0
    wrapper = Path(capsys.readouterr().out.strip())
    route_call = directory / "route-call.json"
    env = {**os.environ, "TEST_FLEET_ROUTE": json.dumps(route),
           "TEST_NATIVE_RECORD": str(record), "TEST_ROUTE_CALL": str(route_call)}

    def invoke(arguments, *, input_text="", changes=None, cwd=None):
        return subprocess.run([sys.executable, "-I", "-c", HARNESS,
                               str(ROOT / "laptop/ccfleet"), *arguments],
                              input=input_text, text=True, capture_output=True,
                              env={**env, **(changes or {})}, cwd=cwd or directory, timeout=8)

    return SimpleNamespace(cli=cli, scope=scope, root=directory, config=config, native=native,
                           record=record, wrapper=wrapper, route=route, env=env, invoke=invoke,
                           route_call=route_call)


def observed(app):
    value = json.loads(app.record.read_text())
    args = value["argv"]
    position = args.index("--settings")
    path = Path(args[position + 1])
    assert not path.exists(), "runtime credentials must be cleaned up"
    assert value["settings_mode"] == 0o600
    return value, args[:position] + args[position + 2:]


def test_created_profile_is_private_secret_free_and_pins_stable_identity(app, capsys):
    profile = json.loads((app.wrapper.parent / "profile.json").read_text())
    assert profile == {"version": 1, "slot_id": "stable-slot", "server": "https://fleet.invalid",
                       "provider": "anthropic", "claude": str(app.native)}
    assert app.wrapper.stat().st_mode & 0o777 == 0o700
    assert app.wrapper.parent.stat().st_mode & 0o777 == 0o700
    assert (app.wrapper.parent / "profile.json").stat().st_mode & 0o777 == 0o600
    assert SECRET not in app.wrapper.read_text() + json.dumps(profile)
    assert app.cli["main"](["launcher", "create", "app", "--slot", "other",
                            "--claude", str(app.native)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert json.loads((app.wrapper.parent / "profile.json").read_text()) == profile


@pytest.mark.parametrize("flag,expected", [("--version", "2.1.999"), ("--help", "Native Claude help")])
def test_app_information_probes_are_offline_and_do_not_need_pairing_or_gateway(app, flag, expected):
    assert not (app.config / "config.json").exists()
    result = subprocess.run([str(app.wrapper), flag], text=True, capture_output=True,
                            env={**app.env, "CCFLEET_HOME": "/wrong/app/environment"}, timeout=5)
    assert result.returncode == 0 and expected in result.stdout and result.stderr == ""
    assert not app.route_call.exists() and not app.record.exists()
    assert not list(app.config.glob(".launcher-settings-*"))


def test_t3_title_invocation_preserves_schema_empty_tools_dontask_and_stdin(app):
    schema = '{"type":"object","properties":{"title":{"type":"string"}}}'
    app_settings = {"disableAllHooks": True, "alwaysThinkingEnabled": False}
    arguments = ["-p", "--output-format", "json", "--json-schema", schema,
                 "--model", "claude-sonnet-test", "--settings", json.dumps(app_settings),
                 "--tools", "", "--disable-slash-commands", "--no-session-persistence",
                 "--strict-mcp-config", "--permission-mode", "dontAsk"]
    result = app.invoke(arguments, input_text="A private synthetic title prompt\n")
    assert result.returncode == 0 and json.loads(result.stdout)["type"] == "result"
    value, forwarded = observed(app)
    assert forwarded == arguments[:7] + arguments[9:]
    assert value["stdin"] == "A private synthetic title prompt\n"
    assert all(value["settings"][key] == item for key, item in app_settings.items())
    assert "--effort" not in forwarded and "--dangerously-skip-permissions" not in forwarded
    assert SECRET not in result.stdout + result.stderr
    assert len(list(app.config.rglob("*"))) == 4, "no prompt, stdin or job logs are persisted"


def test_sdk_stream_input_hooks_mcp_cwd_and_unspecified_defaults_survive(app):
    settings = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo ok"}]}]},
                "permissions": {"allow": ["Read"], "deny": ["Bash"]}, "env": {"APP_MARKER": "yes"}}
    path = app.root / "app-settings.json"
    path.write_text(json.dumps(settings))
    path.chmod(0o644)
    arguments = ["--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
                 "--include-partial-messages", "--settings", str(path), "--mcp-config",
                 '{"mcpServers":{"app":{"command":"tool"}}}', "--permission-prompt-tool", "stdio"]
    stream = '{"type":"user","message":{"role":"user","content":"synthetic"}}\n' * 20
    project = app.root / "project with spaces"
    project.mkdir()
    result = app.invoke(arguments, input_text=stream, cwd=project,
                        changes={"ANTHROPIC_BASE_URL": "https://fleet.invalid",
                                 "ANTHROPIC_MODEL": "app-model", "CLAUDE_CODE_EFFORT_LEVEL": "low"})
    assert result.returncode == 0
    value, forwarded = observed(app)
    assert forwarded == arguments[:6] + arguments[8:]
    assert value["stdin"] == stream and value["cwd"] == str(project)
    assert value["settings"]["hooks"] == settings["hooks"]
    assert value["settings"]["permissions"] == settings["permissions"]
    assert value["settings"]["env"]["APP_MARKER"] == "yes"
    assert value["env"]["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert value["env"]["ANTHROPIC_MODEL"] == "app-model"
    assert value["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert value["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: stable-slot"
    assert not {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGWINCH} \
        .intersection(value["blocked_signals"])
    assert not {"--model", "--effort", "--permission-mode", "--dangerously-skip-permissions"} \
        .intersection(forwarded)
    assert json.loads(path.read_text()) == settings
    assert json.loads(app.route_call.read_text()) == {
        "slot": "stable-slot", "transport": "gateway", "options": {"canonical": True}}


def test_multiple_settings_merge_without_losing_benign_app_fields(app):
    first = {"env": {"ONE": "1"}, "permissions": {"deny": ["Bash"]}, "disableAllHooks": True}
    second = {"env": {"TWO": "2"}, "permissions": {"allow": ["Read"]}}
    assert app.invoke(["--settings=" + json.dumps(first), "--settings", json.dumps(second)]).returncode == 0
    value, arguments = observed(app)
    assert arguments == []
    assert value["settings"]["permissions"] == {"allow": ["Read"], "deny": ["Bash"]}
    assert value["settings"]["env"]["ONE"] == "1"
    assert value["settings"]["env"]["TWO"] == "2"
    assert value["settings"]["disableAllHooks"] is True


@pytest.mark.parametrize("settings", [
    {"env": {"ANTHROPIC_BASE_URL": "https://wrong.invalid"}},
    {"env": {"ANTHROPIC_AUTH_TOKEN": "NEVER_PRINT"}},
    {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}},
    {"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-CCFleet-Slot: foreign"}},
    {"apiKeyHelper": "printf NEVER_PRINT"}, {"forceLoginMethod": "claudeai"},
])
def test_app_routing_auth_overrides_fail_before_network_or_native_launch(app, settings):
    result = app.invoke(["--settings", json.dumps(settings)])
    assert result.returncode == 2 and result.stdout == ""
    assert "conflict" in result.stderr and "NEVER_PRINT" not in result.stderr
    assert not app.route_call.exists() and not app.record.exists()


@pytest.mark.parametrize("kind", ["symlink", "writable", "duplicate", "oversized"])
def test_unsafe_app_settings_are_rejected_without_contents(app, kind):
    path = app.root / "settings.json"
    path.write_text('{"disableAllHooks":true}')
    if kind == "symlink":
        alias = app.root / "alias.json"
        alias.symlink_to(path)
        path = alias
    elif kind == "writable":
        path.chmod(0o666)
    elif kind == "duplicate":
        path.write_text('{"env":{},"env":{"ANTHROPIC_AUTH_TOKEN":"NEVER_PRINT"}}')
    else:
        path.write_text('{"big":"' + "x" * (1024 * 1024) + '"}')
    result = app.invoke(["--settings", str(path)])
    assert result.returncode == 2 and "NEVER_PRINT" not in result.stderr
    assert not app.record.exists()


@pytest.mark.parametrize("arguments", [["auth", "status", "--json"], ["--cloud"],
    ["remote-control"], ["--environment", "remote"], ["--background"]])
def test_account_management_remote_and_detached_modes_cannot_leave_the_fixed_route(app, arguments):
    result = app.invoke(arguments)
    assert result.returncode == 2 and not app.route_call.exists() and not app.record.exists()


@pytest.mark.parametrize("arguments,bare", [(["--bare"], True),
    (["--system-prompt", "--bare"], False), (["--", "--bare"], False)])
def test_bare_auth_is_selected_only_by_a_real_native_option(app, arguments, bare):
    assert app.invoke(arguments).returncode == 0
    value, forwarded = observed(app)
    assert forwarded == arguments
    assert value["env"]["ANTHROPIC_API_KEY"] == (SECRET if bare else "")
    assert value["env"]["ANTHROPIC_AUTH_TOKEN"] == ("" if bare else SECRET)


@pytest.mark.parametrize("change", ["origin", "provider", "slot", "managed"])
def test_changed_route_and_managed_policy_fail_closed(app, change):
    route = json.loads(json.dumps(app.route))
    changes = {}
    if change == "origin":
        route["gateway"]["base_url"] = "https://other.invalid"
    elif change == "provider":
        route["gateway"]["provider"] = route["provider"] = "devin"
    elif change == "slot":
        route["slot_id"] = "foreign-slot"
    else:
        changes["TEST_MANAGED_ROUTE"] = "1"
    changes["TEST_FLEET_ROUTE"] = json.dumps(route)
    result = app.invoke([], changes=changes)
    assert result.returncode == 2 and not app.record.exists()
    if change != "slot":
        assert not app.route_call.exists()


def test_terminal_ctrl_c_during_readiness_cancels_before_native_launch(app, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setitem(app.scope, "load_gateway", lambda: app.route["gateway"])
    monkeypatch.setitem(app.scope, "check_managed_route", lambda: None)

    def interrupted_readiness(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGINT)
        pytest.fail("readiness continued after Ctrl-C")

    monkeypatch.setitem(app.scope, "local_route", interrupted_readiness)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs:
                        pytest.fail("native Claude started after cancelled readiness"))
    assert app.cli["main"](["__external-claude", "--launcher", "app", "--"]) == 130
    assert not app.record.exists() and not list(app.config.glob(".launcher-settings-*"))


@pytest.mark.parametrize("graceful", [False, True])
def test_cancellation_reaps_native_group_and_removes_private_settings(app, graceful):
    process = subprocess.Popen([sys.executable, "-I", "-c", HARNESS,
                                str(ROOT / "laptop/ccfleet")],
                               env={**app.env, "TEST_HOLD": "1",
                                    "TEST_GRACEFUL": "1" if graceful else ""}, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not app.record.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert app.record.exists()
        value = json.loads(app.record.read_text())
        settings = Path(value["argv"][value["argv"].index("--settings") + 1])
        assert settings.exists()
        process.terminate()
        out, err = process.communicate(timeout=3)
        assert process.returncode == 128 + signal.SIGTERM
        assert not settings.exists() and out == "" and SECRET not in err
        assert Path(str(app.record) + ".terminated").exists() is graceful
        with pytest.raises(ProcessLookupError):
            os.kill(value["pid"], 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_terminal_app_preserves_native_controlling_terminal_and_resize(app):
    command = [sys.executable, "-I", "-c", HARNESS, str(ROOT / "laptop/ccfleet")]
    env = {**app.env, "TEST_TTY": "1", "TEST_HOLD": "1", "TEST_GRACEFUL": "1"}
    process = None
    master = None
    if sys.__stdin__.isatty():
        process = subprocess.Popen(command, env=env, stdin=sys.__stdin__,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid = process.pid
    elif sys.platform == "darwin":
        pytest.skip("run this controlling-TTY check with pytest -s in an allocated terminal")
    else:
        pid, master = pty.fork()
        if pid == 0:
            os.execve(sys.executable, command, env)
    finished = False
    try:
        deadline = time.monotonic() + 5
        while not app.record.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert app.record.exists()
        if master is not None:
            os.write(master, b"\x03")
            interrupted = Path(str(app.record) + ".interrupted")
            while not interrupted.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert interrupted.exists()
            time.sleep(.7)
            assert os.waitpid(pid, os.WNOHANG)[0] == 0, "Ctrl-C cancels a turn, not the app"
        os.kill(pid, signal.SIGWINCH)
        resized = Path(str(app.record) + ".resized")
        while not resized.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert resized.exists(), "terminal resize must reach the native child"
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            found, exit_status = os.waitpid(pid, os.WNOHANG)
            if found:
                finished = True
                code = os.waitstatus_to_exitcode(exit_status)
                if process is not None:
                    process.returncode = code
                assert code == 128 + signal.SIGTERM
                break
            time.sleep(.02)
        assert finished
        value, _ = observed(app)
        assert value["controlling_terminal"] is True
    finally:
        if not finished:
            if app.record.exists():
                native = json.loads(app.record.read_text())["pid"]
                with contextlib.suppress(ProcessLookupError):
                    os.kill(native, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        if master is not None:
            os.close(master)
        if process is not None:
            process.communicate(timeout=1)


def test_original_native_symlink_updates_without_recursing_into_a_fleet_launcher(app, capsys):
    entrypoint = app.root / "claude"
    entrypoint.symlink_to(app.native)
    assert app.cli["main"](["launcher", "create", "updated", "--slot", "stable-slot",
                            "--claude", str(entrypoint)]) == 0
    wrapper = Path(capsys.readouterr().out.strip())
    newer = app.root / "native-new"
    newer.write_text("#!/bin/sh\nprintf '2.1.1000 (Claude Code)\\n'\n")
    newer.chmod(0o700)
    entrypoint.unlink()
    entrypoint.symlink_to(newer)
    result = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "2.1.1000" in result.stdout
    entrypoint.unlink()
    entrypoint.symlink_to(app.wrapper)
    result = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "unsafe" in result.stderr


def test_managed_fleet_launcher_follows_real_signed_activation_and_rollback(app, signer,
                                                                         monkeypatch, capsys):
    destination = app.root / "installed"
    old = distribution(signer, version="0.2.8", revision="1" * 40)
    new = distribution(signer, version="0.2.9", revision="2" * 40)
    for item, supported in ((old, False), (new, True)):
        code = item.contents["laptop/ccfleet"].replace(
            f"print({item.manifest['version']!r})".encode(),
            ("import sys\n" + ("print('active app launcher')\n" if supported else
             "print('unsupported command', file=sys.stderr)\nraise SystemExit(2)\n")).encode())
        item.contents["laptop/ccfleet"] = code
        entry = next(row for row in item.manifest["files"] if row["path"] == "laptop/ccfleet")
        entry.update(size=len(code), sha256=hashlib.sha256(code).hexdigest())
        raw = release.canonical(item.manifest)
        item.values.update({item.url: raw, item.url + ".sig": signer.sign(raw), entry["url"]: code})
    install(old, signer, destination)
    current = install(new, signer, destination)
    monkeypatch.setitem(app.scope, "__file__",
                        str(destination / release.ROOT_NAME / current["current"] / "ccfleet"))
    monkeypatch.setitem(app.scope, "client_release", lambda: release)
    monkeypatch.setitem(app.scope, "RELEASE_PUBLIC_KEY", signer.public)
    assert app.cli["main"](["launcher", "create", "managed", "--slot", "stable-slot",
                            "--claude", str(app.native)]) == 0
    wrapper = Path(capsys.readouterr().out.strip())
    assert release.ROOT_NAME not in wrapper.read_text()
    result = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "active app launcher"
    release.rollback(destination, signer.public)
    result = subprocess.run([str(wrapper), "--version"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "unsupported command" in result.stderr
