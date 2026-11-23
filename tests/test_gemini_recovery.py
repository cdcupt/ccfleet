"""A paired device can resume exactly its own bounded private native profile."""

import contextlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import gemini_editor_slot as slot
from ccfleet_agent import gemini_preview as wire
from ccfleet_agent import gemini_recovery as recovery

A, B = b"a" * 32, b"b" * 32


@pytest.fixture
def registry(tmp_path):
    return tmp_path / "recovery"


def begin(root, device=A):
    profile = recovery.Profile.create(root, device, lambda _: True, lambda *_: None)
    assert profile is not None
    profile.sid, profile.model = "test-provider-id", "gemini-fixture"
    return profile


def test_device_bound_single_use_attachment_and_clean_close(registry):
    first = begin(registry)
    token = first.token
    payload = first.sandbox / "native-history"
    payload.write_text("test history")
    for wrong_device, wrong_token in ((B, token), (A, "f" * 64), (A, "../outside"), (A, True)):
        with pytest.raises(ValueError, match="cannot be resumed"):
            recovery.Profile.resume(registry, wrong_device, wrong_token, lambda _: True)
    with pytest.raises(ValueError):
        recovery.Profile.resume(registry, A, token, lambda _: True)
    first.finish(True, lambda _: True)
    data = (first.folder / "detached").read_text()
    assert token not in str(first.folder) and token not in data
    with pytest.raises(ValueError):
        recovery.Profile.resume(registry, B, token, lambda _: True)
    with pytest.raises(ValueError):
        recovery.Profile.resume(registry, A, token, lambda _: False)
    second = recovery.Profile.resume(registry, A, token, lambda fp: fp == A)
    assert second.sid == "test-provider-id" and second.model == "gemini-fixture"
    assert second.token == token and second.sandbox == first.sandbox and payload.is_file()
    assert not (second.folder / "detached").exists()
    with pytest.raises(ValueError):
        recovery.Profile.resume(registry, A, token, lambda _: True)
    second.finish(False, lambda _: True)
    assert not first.folder.exists()


@pytest.mark.parametrize("reason", ["expired", "revoked", "crash", "clock_rollback"])
def test_reaper_removes_stale_sandboxes_without_following_symlinks(registry, tmp_path, reason):
    f = begin(registry)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (f.sandbox / "outside").symlink_to(outside)
    name = f.folder.name
    if reason == "crash":
        os.close(f.lock)  # Worker exit before any complete detachable state.
    else:
        f.finish(True, lambda _: True)
        if reason in {"expired", "clock_rollback"}:
            p = f.folder / "detached"
            state = json.loads(p.read_text())
            state["expires"] = time.time() - 1 if reason == "expired" else time.time() + 99999
            p.write_text(json.dumps(state))
    assert recovery.reap(name, lambda _: reason != "revoked", root=registry, interval=0.001) == 0
    assert not f.folder.exists() and outside.read_text() == "untouched"


def test_reaper_runs_through_active_ownership_and_wall_clock_expiry(registry, monkeypatch):
    monkeypatch.setattr(recovery, "TTL", 0.15)
    f = begin(registry)
    t = threading.Thread(
        target=recovery.reap,
        args=(f.folder.name, lambda _: True),
        kwargs={"root": registry, "interval": 0.01},
        daemon=True,
    )
    t.start()
    time.sleep(0.2)
    assert f.folder.exists()
    f.finish(True, lambda _: True)
    assert f.folder.exists()
    t.join(2)
    assert not t.is_alive() and not f.folder.exists()


def test_retention_size_and_number_bounds(registry, monkeypatch):
    monkeypatch.setattr(recovery, "MAX_PROFILES", 1)
    first = begin(registry)
    assert recovery.Profile.create(registry, B, lambda _: True, lambda *_: None) is None
    monkeypatch.setattr(recovery, "MAX_BYTES", 4)
    (first.sandbox / "file").write_bytes(b"large")
    first.finish(True, lambda _: True)
    assert not first.folder.exists()
    other = begin(registry, B)
    other.finish(True, lambda _: False)
    assert not other.folder.exists()


def test_long_native_conversation_remains_resumable(registry):
    profile = begin(registry)
    # The native helper, conversation database and transcripts grow independently.
    for name, mib in (("encoder", 12), ("conversation.db", 30), ("transcripts", 10)):
        with (profile.sandbox / name).open("wb") as f:
            f.truncate(mib * 1024 * 1024)
    profile.finish(True, lambda _: True)
    resumed = recovery.Profile.resume(registry, A, profile.token, lambda _: True)
    assert resumed.sid == "test-provider-id"
    assert (resumed.sandbox / "conversation.db").stat().st_size == 30 * 1024 * 1024
    resumed.finish(False, lambda _: True)


def test_resume_native_uses_known_id_and_requires_original_model(monkeypatch):
    a = object.__new__(slot.Agent)
    a.supports_resume = True
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return {
            "models": {
                "currentModelId": "gemini-b",
                "availableModels": [{"modelId": "gemini-a"}, {"modelId": "gemini-b"}],
            }
        }

    monkeypatch.setattr(a, "rpc", rpc)
    assert a.resume(Path("/test"), [], "old-sid", "gemini-a") == "gemini-a"
    assert a.sid == "old-sid"
    assert [name for name, _ in calls] == ["session/resume", "session/set_model"]
    assert calls[0][1] == {"sessionId": "old-sid", "cwd": "/test", "mcpServers": []}
    with pytest.raises(slot.EditorError, match="cannot be resumed"):
        a.resume(Path("/test"), [], "old-sid", "gemini-missing")


def test_worker_wire_recovers_only_after_loss_and_opt_in(registry, monkeypatch, tmp_path):
    got = []
    permitted = {A, B}
    monkeypatch.setattr(slot, "require_personal", lambda *_: None)

    def run(home, device, binary, path, lease):
        got.append((str(path), lease.sid))
        lease.sid, lease.model = "original-provider-session", "gemini-fixture"
        device.resume_token = lease.token
        device.send({"type": "ready", "resumeToken": lease.token, "resumed": bool(lease.state)})
        while True:
            msg = device.receive(2)
            if msg == {"type": "close"}:
                return
            if msg.get("type") == "ping":
                device.send({"type": "pong"})
            else:
                raise slot.EditorError("invalid")

    monkeypatch.setattr(slot, "run_editor", run)

    @contextlib.contextmanager
    def connect(fp, token=None):
        client, worker = socket.socketpair()
        with client, worker, client.makefile("rb") as inp, client.makefile("wb") as out:
            t = threading.Thread(
                target=slot.serve_device,
                args=(tmp_path, worker, fp, permitted.__contains__),
                kwargs={
                    "recovery_root": registry,
                    "spawn_reaper": lambda *_: None,
                    "runtime_fn": lambda _: "fixture",
                },
                daemon=True,
            )
            t.start()
            try:
                opening = (
                    {"type": "open", "version": 1}
                    if token is None
                    else {"type": "resume", "version": 1, "resumeToken": token}
                )
                wire.send(out, opening)
                yield wire.receive(inp), lambda x: wire.send(out, x), lambda: wire.receive(inp)
            finally:
                with contextlib.suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
                t.join(3)
                assert not t.is_alive()

    with connect(A) as (ready, send, receive):
        secret = ready["resumeToken"]
        send({"type": "enableResume"})
        send({"type": "ping"})
        assert receive() == {"type": "pong"}
    with connect(B, secret) as (answer, _, __):
        assert answer == {"type": "error", "message": recovery.NO_SESSION}
    with connect(A, secret) as (answer, send, _):
        assert answer == {"type": "ready", "resumeToken": secret, "resumed": True}
        send({"type": "close"})
    assert got[0][0] == got[1][0] and got[1][1] == "original-provider-session"
    with connect(A, secret) as (answer, _, __):
        assert answer["message"] == recovery.NO_SESSION
    with connect(A) as (ready, _, __):
        legacy = ready["resumeToken"]
    with connect(A, legacy) as (answer, _, __):
        assert answer["message"] == recovery.NO_SESSION


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox policy")
def test_runtime_can_traverse_lease_ancestors_but_cannot_read_the_registry():
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="gr-test-") as tmp:
        base = Path(tmp) / "r"
        lease = begin(base)
        try:
            home = Path(tmp) / "owner"
            home.mkdir()
            slot.session_profile(home, lease.sandbox)
            sentinel = lease.folder / "detached"
            sentinel.write_text("test-only")
            check = """
import os,sys
root,parent,marker,project = sys.argv[1:]
assert os.path.isdir(root) and os.path.isdir(parent)
for action in [lambda: os.listdir(root), lambda: os.listdir(parent), lambda: open(marker).read(),
               lambda: open(marker, 'w')]:
    try: action()
    except PermissionError: continue
    raise AssertionError('unexpected access to recovery registry')
with open(os.path.join(project,'allowed'), 'w') as value: value.write('own sandbox')
"""
            binary = Path("/usr/bin/python3")
            command = slot._sandbox(binary, home, lease.sandbox) + [
                "-I",
                "-B",
                "-c",
                check,
                str(base),
                str(lease.folder),
                str(sentinel),
                str(lease.sandbox),
            ]
            interpreter = Path(
                subprocess.check_output(
                    [
                        str(binary),
                        "-I",
                        "-c",
                        "import os,sys; print(os.path.realpath(sys.executable))",
                    ],
                    text=True,
                ).strip()
            )
            for i, arg in enumerate(command):
                if arg.startswith("PYTHON="):
                    command[i] = "PYTHON=" + str(interpreter)
                elif arg.startswith("PYTHON_APP="):
                    command[i] = "PYTHON_APP=" + str(
                        interpreter.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
                    )
            proc = subprocess.run(
                command,
                env={"PATH": "/usr/bin:/bin"},
                cwd=lease.sandbox,
                capture_output=True,
                timeout=15,
            )
            assert proc.returncode == 0, proc.stderr.decode(errors="replace")
            assert sentinel.read_text() == "test-only"
        finally:
            lease.finish(False, lambda _: True)


def test_cleanup_on_declined_native_resume_and_unsafe_registry(registry, tmp_path):
    prior = begin(registry)
    prior.finish(True, lambda _: True)
    loaded = recovery.Profile.resume(registry, A, prior.token, lambda _: True)
    # No alternative session identity or timer is manufactured on a provider rejection.
    loaded.finish(False, lambda _: True)
    assert not loaded.folder.exists()
    real = tmp_path / "outside-registry"
    real.mkdir(mode=0o700)
    link = tmp_path / "fake-registry"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        recovery.Profile.create(link, B, lambda _: True, lambda *_: None)
    assert not list(real.iterdir())


def test_registry_retention_forgets_revoked_and_expired_leases_on_restart(registry, monkeypatch):
    first = begin(registry)
    first.finish(True, lambda _: True)
    second = begin(registry, B)
    second.finish(True, lambda _: True)
    rearmed = []
    recovery.restart_reapers(registry, lambda fp: fp == B, lambda name, *_: rearmed.append(name))
    assert not first.folder.exists()
    assert rearmed == [second.folder.name] and second.folder.exists()


def test_native_resume_declined_without_provider_capability(monkeypatch):
    a = object.__new__(slot.Agent)
    a.supports_resume = False
    monkeypatch.setattr(a, "rpc", lambda *args: pytest.fail("must not call absent native method"))
    with pytest.raises(slot.EditorError, match="cannot be resumed"):
        a.resume(Path("/test"), [], "known-session", "gemini-fixture")


def test_second_network_drop_before_resume_confirmation_keeps_original_deadline(registry):
    first = begin(registry)
    first.finish(True, lambda _: True)
    marker = first.folder / "detached"
    before = json.loads(marker.read_text())
    interrupted = recovery.Profile.resume(registry, A, first.token, lambda _: True)
    interrupted.finish(True, lambda _: True, renew=False)
    after = json.loads(marker.read_text())
    assert after == before
    resumed = recovery.Profile.resume(registry, A, first.token, lambda _: True)
    resumed.finish(False, lambda _: True)


def test_spurious_registry_symlink_does_not_block_cleaning_other_profiles(registry, tmp_path):
    f = begin(registry)
    f.finish(True, lambda _: True)
    outside = tmp_path / "legitimate-file"
    outside.write_text("untouched")
    entry = registry / ("f" * 48)
    entry.symlink_to(outside)
    assert recovery.reap(f.folder.name, lambda _: False, root=registry) == 0
    assert outside.read_text() == "untouched" and not entry.exists() and not f.folder.exists()


def test_restarting_service_never_spawns_a_duplicate_watcher(registry):
    launches = []

    def fork(name, directory, guard):
        launches.append((name, (os.dup(directory), os.dup(guard))))

    f = recovery.Profile.create(registry, A, lambda _: True, fork)
    assert f is not None
    f.sid, f.model = "test-sid", "gemini-fixture"
    f.finish(True, lambda _: True)
    name, pair = launches[0]
    for _ in range(12):
        recovery.restart_reapers(registry, lambda _: True, fork)
    assert len(launches) == 1
    assert (f.folder / recovery.WATCHER).stat().st_mode & 0o777 == 0o600
    for fd in pair:
        os.close(fd)
    for _ in range(12):
        recovery.restart_reapers(registry, lambda _: True, fork)
    assert len(launches) == 2  # Original watcher died; restart schedules precisely one.
    done = []
    watcher = threading.Thread(
        target=lambda: done.append(
            recovery.reap(
                name, lambda _: True, root=registry, inherited=launches[1][1], interval=0.01
            )
        ),
        daemon=True,
    )
    watcher.start()
    active = recovery.Profile.resume(registry, A, f.token, lambda _: True)
    active.finish(False, lambda _: True)
    watcher.join(2)
    assert done == [0] and not watcher.is_alive()


@pytest.mark.parametrize("replace", ["rename", "mode", "guard"])
def test_watcher_exits_if_lease_directory_or_guard_identity_changes(registry, tmp_path, replace):
    inherited = []
    f = recovery.Profile.create(
        registry, A, lambda _: True, lambda _, d, g: inherited.append((os.dup(d), os.dup(g)))
    )
    assert f is not None
    folder = f.folder
    if replace == "rename":
        folder.rename(tmp_path / "original-lease")
        folder.mkdir(mode=0o700)
    elif replace == "mode":
        folder.chmod(0o750)
    else:
        (folder / recovery.WATCHER).unlink()
        (folder / recovery.WATCHER).touch(mode=0o600)
    assert (
        recovery.reap(
            folder.name, lambda _: False, root=registry, interval=0.01, inherited=inherited[0]
        )
        == 0
    )
    assert folder.is_dir()
    os.close(f.lock)
    if replace == "mode":
        spawned = []
        recovery.restart_reapers(registry, lambda _: True, lambda *x: spawned.append(x))
        assert not spawned


def test_nonprivate_guard_refuses_watcher_and_preserves_unrelated_files(registry, tmp_path):
    f = begin(registry)
    f.finish(True, lambda _: True)
    external = tmp_path / "other"
    external.write_text("personal")
    guard = f.folder / recovery.WATCHER
    guard.unlink()
    guard.symlink_to(external)
    spawned = []
    recovery.restart_reapers(registry, lambda _: True, lambda *x: spawned.append(x))
    assert not spawned and recovery.reap(f.folder.name, lambda _: True, root=registry) == 0
    assert external.read_text() == "personal"


@pytest.mark.parametrize("retain", [False, True])
def test_stale_worker_finish_never_modifies_replaced_lease(registry, tmp_path, retain):
    f = begin(registry)
    f.folder.rename(tmp_path / "stale-folder")
    f.folder.mkdir(mode=0o700)
    (f.folder / "untouched").write_text("replacement")
    f.finish(retain, lambda _: True)
    assert list(f.folder.iterdir()) == [f.folder / "untouched"]
    assert (f.folder / "untouched").read_text() == "replacement"
