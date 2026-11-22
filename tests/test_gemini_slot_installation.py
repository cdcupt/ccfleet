"""Private Mac editor installation leaves pre-existing workspace jobs running."""

import os
import plistlib
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_install_preserves_existing_job_and_finder_launches_only_owner_login(tmp_path, monkeypatch):
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
    calls = []
    loaded = {"io.ccfleet.private-owner-workspace"}
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
    app["install"]()
    for name in ("gemini_editor_distribution.py", "gemini_editor_slot.py", "gemini_editor_client.py",
                 "workspace_tools.py", "project_files.py"):
        assert (runtime / name).read_bytes() == (ROOT / "ccfleet_agent" / name).read_bytes()
    # The new services are independent and the original is neither unloaded nor restarted.
    unit = agents / "io.ccfleet.private-owner-gemini-editor.plist"
    assert [c[3] for c in calls if c[0:2] == ["/bin/launchctl", "bootstrap"]] == [unit]
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
