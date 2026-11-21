"""Build the Mac workspace app from repository-owned scripts and the Fleet client."""
from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def build(output: Path, identity: str = "-") -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "laptop/workspace"
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp) / "CCFleet Workspace.app"
        subprocess.run(["/usr/bin/osacompile", "-o", app, source / "launcher.applescript"],
                       check=True)
        resources = app / "Contents/Resources"
        for name in ("start.zsh", "client.py"):
            shutil.copyfile(source / name, resources / name)
        shutil.copyfile(root / "laptop/ccfleet", resources / "ccfleet")
        pkg = resources / "ccfleet_agent"
        pkg.mkdir()
        for name in ("__init__", "project_files", "workspace_io", "workspace_tools"):
            shutil.copyfile(root / "ccfleet_agent" / (name + ".py"), pkg / (name + ".py"))
        config = app / "Contents/Info.plist"
        values = plistlib.loads(config.read_bytes())
        values.update(CFBundleIdentifier="io.ccfleet.workspace", CFBundleShortVersionString="1.0.1",
                      CFBundleVersion="2", LSMinimumSystemVersion="12.0")
        config.write_bytes(plistlib.dumps(values))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", identity, app], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", app], check=True)
        subprocess.run(["/usr/bin/ditto", "-ck", "--norsrc", "--keepParent", app, output],
                       check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--identity", default="-")
    args = parser.parse_args()
    build(args.output.resolve(), args.identity)
