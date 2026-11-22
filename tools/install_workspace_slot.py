"""Enable the signed-in Mac owner's Antigravity workspace service on an enrolled slot."""
import hashlib
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "io.ccfleet.private-owner-workspace"
EDITOR_LABEL = "io.ccfleet.private-owner-gemini-editor"


def signin_app(home):
    apps = home / "Applications"
    apps.mkdir(mode=0o700, exist_ok=True)
    if (apps.is_symlink() or apps.stat().st_uid != os.getuid()
            or apps.stat().st_mode & 0o022):
        raise ValueError("unsafe owner Applications directory")
    name = "CCFleet Gemini Sign In.app"
    target = apps / name
    files = {"Contents/Info.plist": plistlib.dumps({
        "CFBundleExecutable": "signin", "CFBundleIdentifier": "io.ccfleet.gemini-owner-signin",
        "CFBundleName": "CCFleet Gemini Sign In", "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1", "LSUIElement": True,
    }), "Contents/MacOS/signin": Path(__file__).with_name("gemini_editor_signin.py").read_bytes()}
    if target.exists() or target.is_symlink():
        for rel, raw in files.items():
            path = target / rel
            if (path.resolve() != path or not path.is_file() or path.stat().st_uid != os.getuid()
                    or path.stat().st_mode & 0o022 or path.read_bytes() != raw
                    or rel.endswith("/signin") and not os.access(path, os.X_OK)):
                raise ValueError(f"review the existing owner sign-in launcher: {target}")
        return target
    with tempfile.TemporaryDirectory(dir=apps) as tmp:
        staged = Path(tmp) / name
        (staged / "Contents/MacOS").mkdir(parents=True, mode=0o700)
        for rel, raw in files.items():
            (staged / rel).write_bytes(raw)
            (staged / rel).chmod(0o700 if rel.endswith("/signin") else 0o600)
        os.rename(staged, target)
    return target


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
        "antigravity_entry.py", "antigravity_slot.py", "gemini_preview.py",
        "gemini_distribution.py", "workspace_io.py", "gemini_editor_distribution.py",
        "gemini_editor_slot.py", "gemini_editor_client.py", "project_files.py", "workspace_tools.py")}
    units = {LABEL: "antigravity_slot.py", EDITOR_LABEL: "gemini_editor_slot.py"}
    for label, service in units.items():
        components[agents / (label + ".plist")] = plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/usr/bin/python3", "-I", "-B", str(root / service), "serve"],
            "WorkingDirectory": str(home), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "LimitLoadToSessionType": "Aqua",
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
        })
    upgrades = {}
    for path, data in components.items():
        if path.is_symlink():
            raise ValueError(f"review the linked workspace component: {path}")
        if path.exists() and path.read_bytes() != data:
            previous = hashlib.sha256(path.read_bytes()).hexdigest()
            predecessors = {
                "antigravity_entry.py": {
                    "ae173ac4ff3092367de9cb8f71b781e301c6cf192ce6a66a580ae8a7205d3fac",
                    "fa617a5d93bf2074d92f13a94ee3c5fb07b938fe30ee24262d9a2725359192db",
                    "92d538c2b46ced80e3d4e15422d404da4ea073a4c523c53f3c560fe6040ac24b",
                },
                "gemini_distribution.py": {
                    "7e70e1cc368469fbd940e573b7028b463f9d271b1ebd9cb2b5b427ce53785cab",
                },
                "workspace_tools.py": {
                    "7536b0e948c2716c33bf88dfa2a6eb519ecbbde3466884c300dcddb8b9d8cd97",
                },
                "gemini_preview.py": {
                    "429d6efdb57e1c265f6ee8debd69d166d4f27fd7735b55f7e40b48868c9e70b9",
                },
            }
            if previous in predecessors.get(path.name, ()):
                upgrades[path] = previous
            else:
                raise ValueError(f"review the existing workspace component: {path}")
    # The new forced entry imports the preview helper; install its dependency first.
    ordered = sorted(components, key=lambda path: path.name == "antigravity_entry.py")
    for path in ordered:
        data = components[path]
        if path.exists() and path not in upgrades:
            continue
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            tmp = Path(temp.name)
            try:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
                if path in upgrades:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != upgrades[path]:
                        raise ValueError("workspace component changed during installation")
                    os.replace(tmp, path)
                else:
                    os.link(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    launcher = signin_app(home)
    domain = "gui/" + str(os.getuid())
    for label in units:
        state = subprocess.run(["/bin/launchctl", "print", domain + "/" + label],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if state.returncode:
            subprocess.run(["/bin/launchctl", "bootstrap", domain, agents / (label + ".plist")],
                           check=True)
    print("Antigravity workspace services installed. For personal Google sign-in, open " + str(launcher))


if __name__ == "__main__":
    try:
        install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
