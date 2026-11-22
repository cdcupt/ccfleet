"""Private Mac editor installation leaves pre-existing workspace jobs running."""

import hashlib
import os
import plistlib
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import gemini_distribution as dist

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("editor_running", [False, True])
@pytest.mark.parametrize("old_worker", [False, True])
def test_install_preserves_existing_job_and_finder_launches_only_owner_login(
    tmp_path, monkeypatch, editor_running, old_worker
):
    home = tmp_path / "owner"
    runtime = home / ".local/lib/ccfleet-owner" / ("c" * 40) / "ccfleet_agent"
    agents = home / "Library/LaunchAgents"
    runtime.mkdir(parents=True, mode=0o700)
    agents.mkdir(parents=True)
    (runtime / "mac_owner_agent.py").write_text("# X-CCFleet-Workspace")
    (runtime / "mac_keys.py").write_text("# workspace_public_keys")
    (agents / "com.ccfleet.private-owner-agent.plist").write_bytes(
        plistlib.dumps({"ProgramArguments": [str(runtime / "mac_owner_agent.py")]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "darwin")
    # Exercise the owner-only checks even under root-based Linux CI.
    if os.getuid() == 0:
        pytest.skip("installer requires a login user")
    old = {}
    history = []
    for version, revision, historical_pins in dist.EDITOR_HISTORY:
        pins = {}
        for op in historical_pins:
            name = dist.EDITOR_FILES[op]
            raw = f"public {version} {name}".encode()
            snapshot = dist.editor_snapshot(name, version)
            path = runtime / snapshot
            path.write_bytes(raw)
            old[snapshot] = (raw, path.stat().st_ino, path.stat().st_mtime_ns)
            pins[op] = hashlib.sha256(raw).hexdigest()
        history.append((version, revision, pins))
    monkeypatch.setattr(dist, "EDITOR_HISTORY", history)
    calls = []
    loaded = {"io.ccfleet.private-owner-workspace"}
    if editor_running:
        loaded.add("io.ccfleet.private-owner-gemini-editor")
    def command(argv, **kw):
        calls.append(list(argv))
        if argv[0] == "/bin/launchctl":
            if argv[1] == "print":
                return SimpleNamespace(returncode=0 if argv[2].rsplit("/", 1)[1] in loaded else 113)
            assert argv[1] == "bootstrap"
            loaded.add(plistlib.loads(argv[3].read_bytes())["Label"])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", command)
    app = runpy.run_path(str(ROOT / "tools/install_workspace_slot.py"))
    prior = b"# predecessor for an in-flight request\n"
    old_dist = runtime / "gemini_distribution.py"
    old_dist.write_bytes(prior)
    monkeypatch.setitem(app["PREDECESSORS"], old_dist.name, {hashlib.sha256(prior).hexdigest()})
    worker = runtime / "gemini_editor_slot.py"
    original_worker = ((ROOT / "ccfleet_agent" / worker.name).read_bytes() if not old_worker
                       else b"# previously supported worker version\n")
    worker.write_bytes(original_worker)
    if old_worker:
        monkeypatch.setitem(app["PREDECESSORS"], worker.name,
                            {hashlib.sha256(original_worker).hexdigest()})
    worker_inode = worker.stat().st_ino
    with old_dist.open("rb") as active_request, worker.open("rb") as active_worker:
        app["install"]()
        assert active_request.read() == prior
        assert active_worker.read() == original_worker
    if not old_worker:
        assert worker.stat().st_ino == worker_inode
    assert old_dist.read_bytes() == (ROOT / "ccfleet_agent" / old_dist.name).read_bytes()
    for snapshot, (raw, inode, mtime) in old.items():
        path = runtime / snapshot
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == (raw, inode, mtime)
    for name in dist.EDITOR_FILES.values():
        assert (runtime / dist.editor_snapshot(name)).read_bytes() == (ROOT / "ccfleet_agent" / name).read_bytes()
    for name in ("gemini_editor_distribution.py", "gemini_editor_slot.py", "project_files.py"):
        assert (runtime / name).read_bytes() == (ROOT / "ccfleet_agent" / name).read_bytes()
    # The new services are independent and the original is neither unloaded nor restarted.
    unit = agents / "io.ccfleet.private-owner-gemini-editor.plist"
    assert [c[3] for c in calls if c[0:2] == ["/bin/launchctl", "bootstrap"]] == ([] if editor_running else [unit])
    mtime = (runtime / "antigravity_entry.py").stat().st_mtime_ns
    calls.clear()
    app["install"]()
    assert not [c for c in calls if c[:2] == ["/bin/launchctl", "bootstrap"]]
    assert mtime == (runtime / "antigravity_entry.py").stat().st_mtime_ns
    bundle = home / "Applications/CCFleet Gemini Sign In.app"
    conf = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    launcher = bundle / "Contents/MacOS" / conf["CFBundleExecutable"]
    assert os.access(launcher, os.X_OK)
    calls.clear()
    runpy.run_path(str(launcher), run_name="__main__")
    assert calls == [["/usr/bin/python3", "-I", "-B", str(runtime / "gemini_editor_slot.py"), "login"]]
    # An unrelated installed component is preserved.
    altered = runtime / "gemini_editor_slot.py"
    altered.write_text("# user change\n")
    with pytest.raises(ValueError, match="review the existing"):
        app["install"]()
    assert altered.read_bytes() == b"# user change\n"

def test_fresh_editor_install_recovers_only_pinned_public_sources(tmp_path, monkeypatch):
    app = runpy.run_path(str(ROOT / "tools/install_workspace_slot.py"))
    source = tmp_path / "src"
    target = tmp_path / "runtime"
    source.mkdir()
    target.mkdir()
    name = "workspace_tools.py"
    raw = b"old workspace fixture"
    wanted = hashlib.sha256(raw).hexdigest()
    (source / name).write_bytes(b"updated workspace fixture")
    fetch = app["historical_editor_source"]
    version, revision, _ = dist.EDITOR_HISTORY[-1]
    destination = target / dist.editor_snapshot(name, version)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=raw))
    monkeypatch.setattr(dist, "_fetch", lambda *_: pytest.fail("unexpected network request"))
    assert fetch(dist, source, destination, name, revision, wanted) == raw
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=b""))
    def archive(url, path, limit, timeout):
        assert url == ("https://raw.githubusercontent.com/cdcupt/ccfleet/"
                       + revision + "/ccfleet_agent/workspace_tools.py")
        path.write_bytes(raw)
    monkeypatch.setattr(dist, "_fetch", archive)
    assert fetch(dist, source, destination, name, revision, wanted) == raw
    with pytest.raises(ValueError, match="checksum"):
        fetch(dist, source, destination, name, revision, "0" * 64)
