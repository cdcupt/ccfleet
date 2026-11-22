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


# Normal fresh installs may be from a source archive or a shallow checkout. Recover
# the exact public bytes expected by 0.2.15 without storing a second source tree.
FIRST_EDITOR_REVISION = "ee147f63c559f4f8deb9e5962501412168edf27e"


def legacy_editor_source(dist, source, root, filename, digest):
    for path in (root / filename, source / filename):
        data = dist.pinned_editor_file(path, digest)
        if data is not None:
            return data
    git_path = f"{FIRST_EDITOR_REVISION}:ccfleet_agent/{filename}"
    git = subprocess.run(
        ["/usr/bin/git", "-C", str(source.parent), "cat-file", "blob", git_path],
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True, timeout=12,
    )
    if git.returncode == 0 and hashlib.sha256(git.stdout).hexdigest() == digest:
        return git.stdout
    url = (f"https://raw.githubusercontent.com/cdcupt/ccfleet/{FIRST_EDITOR_REVISION}/"
           f"ccfleet_agent/{filename}")
    with tempfile.TemporaryDirectory() as stage:
        path = Path(stage) / filename
        dist._fetch(url, path, dist.EDITOR_LIMIT, 25)
        data = dist.pinned_editor_file(path, digest)
    if data is None:
        raise ValueError("previous signed client component checksum mismatch")
    return data


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
    sys.path.insert(0, str(source.parent))
    from ccfleet_agent import gemini_distribution as dist

    components = {root / name: (source / name).read_bytes() for name in (
        "antigravity_entry.py", "antigravity_slot.py", "gemini_preview.py",
        "gemini_distribution.py", "workspace_io.py", "gemini_editor_distribution.py",
        "gemini_editor_slot.py", "project_files.py")}
    for name, digest in dist.LEGACY_EDITOR_SOURCES.values():
        components[root / name] = legacy_editor_source(dist, source, root, name, digest)
        components[root / dist.editor_snapshot(name)] = (source / name).read_bytes()
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
                    "67ff27fc244194d0f7e752d828befc848b31f7f0e6f8576e7a6476baa8b58c5f",
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
    # Install the snapshots before the new distributor. Switch the forced entry last.
    activation_order = {"gemini_distribution.py": 1, "antigravity_entry.py": 2}
    ordered = sorted(components, key=lambda path: activation_order.get(path.name, 0))
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
    print(f"Antigravity workspace services installed. For personal Google sign-in, open {launcher}")


if __name__ == "__main__":
    try:
        install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
