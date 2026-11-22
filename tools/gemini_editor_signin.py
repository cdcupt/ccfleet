#!/usr/bin/python3 -I
"""Finder entry point for the private slot owner's Google personal sign-in."""

import os
import plistlib
import subprocess
from pathlib import Path


def login():
    home = Path.home()
    plist = home / "Library/LaunchAgents/io.ccfleet.private-owner-gemini-editor.plist"
    program = plistlib.loads(plist.read_bytes()).get("ProgramArguments", [])
    if (len(program) != 5 or program[:3] != ["/usr/bin/python3", "-I", "-B"]
            or program[4] != "serve"):
        raise ValueError("unrecognized private editor configuration")
    server = Path(program[3])
    if (server.name != "gemini_editor_slot.py" or server.resolve() != server
            or not server.is_relative_to(home / ".local/lib/ccfleet-owner")
            or server.stat().st_uid != os.getuid() or server.stat().st_mode & 0o022):
        raise ValueError("unrecognized private editor executable")
    subprocess.run([*program[:4], "login"], check=True, cwd=home,
                   env={"HOME": str(home), "PATH": os.defpath, "LANG": "en_US.UTF-8"})


if __name__ == "__main__":
    try:
        login()
    except (OSError, ValueError, subprocess.SubprocessError):
        subprocess.run(["/usr/bin/osascript", "-e",
                        'display alert "Gemini sign-in could not finish" message '
                        '"Open this app on your enrolled Mac slot and complete Google sign-in."'])
        raise SystemExit(1)
