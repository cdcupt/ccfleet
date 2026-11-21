"""Enable the signed-in Mac owner's Antigravity workspace service on an enrolled slot."""
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "io.ccfleet.private-owner-workspace"


def install():
    if sys.platform != "darwin" or os.getuid() == 0:
        raise ValueError("run in the enrolled Mac owner's login session")
    home = Path.home()
    agents = home / "Library/LaunchAgents"
    unit = agents / "com.ccfleet.private-owner-agent.plist"
    owner_agent = Path(plistlib.loads(unit.read_bytes())["ProgramArguments"][-1])
    root = owner_agent.parent
    if (owner_agent.name != "mac_owner_agent.py" or owner_agent.resolve() != owner_agent
            or not root.is_relative_to(home / ".local/lib/ccfleet-owner")
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022):
        raise ValueError("the enrolled owner runtime directory is unavailable")
    if ("X-CCFleet-Workspace" not in owner_agent.read_text()
            or "workspace_public_keys" not in (root / "mac_keys.py").read_text()):
        raise ValueError("update the enrolled Mac owner agent to the workspace-capable release")
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "-R",
                    '=anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"',
                    home / ".local/bin/agy"], check=True)
    source = Path(__file__).resolve().parents[1] / "ccfleet_agent"
    components = {root / name: (source / name).read_bytes() for name in (
        "antigravity_entry.py", "antigravity_slot.py", "workspace_io.py")}
    destination = agents / (LABEL + ".plist")
    components[destination] = plistlib.dumps({
        "Label": LABEL, "ProgramArguments": ["/usr/bin/python3", "-I", "-B",
                                              str(root / "antigravity_slot.py"), "serve"],
        "WorkingDirectory": str(home), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 15, "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
    })
    for path, data in components.items():
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError(f"existing component differs; review the installed version: {path}")
    for path, data in components.items():
        if path.exists():
            continue
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            tmp = Path(temp.name)
            try:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
                os.link(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    domain = "gui/" + str(os.getuid())
    state = subprocess.run(["/bin/launchctl", "print", domain + "/" + LABEL],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if state.returncode:
        subprocess.run(["/bin/launchctl", "bootstrap", domain, destination], check=True)
    print("Antigravity workspace service installed for this private Mac login.")


if __name__ == "__main__":
    try:
        install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
