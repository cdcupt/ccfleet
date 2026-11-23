"""Local, selected-project MCP tools for a directly operated Antigravity session."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import pwd
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

LIMIT = 192 * 1024
COMMANDS = {
    "pytest": ("python3", "-m", "pytest"),
    "npm": ("npm", "test"),
    "go": ("go", "test", "./..."),
    "cargo": ("cargo", "test"),
}


# Test programs may be project-controlled. Give them a temporary home and allow
# writes only to this project and scratch space. Network access is disabled.
TEST_SANDBOX = """
(version 1)
(deny default)
(import "system.sb")
(deny network*)
(allow process-exec process-fork sysctl-read file-read-metadata)
(allow file-read* (subpath (param "PROJECT")) (subpath (param "SCRATCH"))
  (subpath "/usr") (subpath "/System") (subpath "/Library/Developer")
  (subpath "/Applications/Xcode.app") (subpath "/opt/homebrew") (subpath "/bin")
  (subpath "/dev") (subpath "/private/var/db/dyld") (literal "/private/etc/localtime"))
(allow file-write* (subpath (param "PROJECT")) (subpath (param "SCRATCH"))
  (literal "/dev/null"))
(deny file-read-data file-write* (regex #"/[.]git(/|$)"))
"""



COMMAND_LIMIT = 16 * 1024
COMMAND_OUTPUT_LIMIT = 32 * 1024
COMMAND_TIMEOUT = 300
BUNDLE_LIMIT = 64 * 1024 * 1024
# Only trusted system or package-manager tools, without user shell startup files.
APPLE_DEV_TOOLS = ("/Library/Developer/CommandLineTools/usr/bin",
                   "/Applications/Xcode.app/Contents/Developer/usr/bin")
DEV_PATH = ":".join(("/opt/homebrew/bin", "/usr/local/bin", *APPLE_DEV_TOOLS,
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"))

# sandbox-exec applies the policy before any repository code or shell expression runs.
# File writes are limited to the project and per-command scratch. Protect Git control
# files that can execute or redirect later Git commands outside this session.
DEV_SANDBOX = """
(version 1)
(deny default)
(import "system.sb")
(allow process-exec process-fork sysctl-read file-read-metadata)
(allow file-read* (subpath (param "PROJECT")) (subpath (param "SCRATCH"))
  (subpath "/usr") (subpath "/System") (subpath "/Library/Developer")
  (subpath "/Applications/Xcode.app") (subpath "/opt/homebrew") (subpath "/bin")
  (subpath "/sbin") (subpath "/private/var/db/dyld")
  (literal "/private/etc/localtime"))
(allow file-write* (subpath (param "PROJECT")) (subpath (param "SCRATCH"))
  (literal "/dev/null"))
(deny network*)
(deny file-read* file-write* (subpath "/dev"))
(allow file-read* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random")
  (literal "/dev/urandom") (subpath "/dev/fd"))
(allow file-write* (literal "/dev/null") (subpath "/dev/fd"))
(deny file-read-data file-write* (subpath "/usr/local/etc") (subpath "/usr/local/var")
  (subpath "/opt/homebrew/etc") (subpath "/opt/homebrew/var"))
; Keep descendants in the invocation's process group even if a tool daemonizes.
; posix_spawn can set process-group attributes inside the kernel. ENOSYS makes
; runtimes such as libuv fall back to fork/exec, where the group policy applies.
(deny syscall-unix (syscall-number SYS_setpgid SYS_setsid))
(deny syscall-unix (syscall-number SYS_posix_spawn) (with errno ENOSYS))
"""

# Case-insensitive volumes can resolve a differently cased spelling to the same file.
# Seatbelt regexes are POSIX EREs, so expand fixed ASCII names before compiling.
def _git_controls():
    def folded(word):
        return "".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in word)

    meta = "/[.]" + folded("git")
    subtrees = "|".join(map(folded, ("hooks", "info", "modules", "worktrees",
                                    "remotes", "branches", "objects/info")))
    controls = "|".join(map(folded, ("config", "gitdir", "commondir")))
    rules = (meta + "$", f"{meta}/({subtrees})(/|$)", f"{meta}/({controls})([.]|$)")
    return "(deny file-write* " + " ".join(f'(regex #"{rule}")' for rule in rules) + ")"


# chdir("/dev/fd/N") does not work on macOS. Use fchdir in an isolated helper,
# before exec'ing Seatbelt, without using Python preexec_fn in the threaded ACP host.
SANDBOX_ENTRY = """import os, resource, sys
fd = int(sys.argv[1])
os.fchdir(fd)
os.close(fd)
if not os.path.samestat(os.stat("."), os.stat(sys.argv[2])):
    sys.exit(125)
if sys.argv[3]:
    size = int(sys.argv[3])
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
os.execv(sys.argv[4], sys.argv[4:])
"""

if __package__:
    from .project_files import ProjectAccess  # noqa: F401


def _developer_git():
    # Skip the /usr/bin/git xcrun shim: it may prompt to install Xcode or try to
    # create a host-side cache outside the development sandbox.
    return next((str(Path(d) / "git") for d in APPLE_DEV_TOOLS
                 if os.access(Path(d) / "git", os.X_OK)), None)


def _git_identity():
    # Read two data values from global config. No project config, includes or
    # credential helpers are involved. Keep a deterministic identity without a login.
    values = {"user.name": "CCFleet Gemini", "user.email": "ccfleet-gemini@localhost"}
    git = _developer_git()
    if git is None:
        return values
    try:
        data = subprocess.run(
            [git, "config", "--global", "--no-includes", "--null", "--get-regexp",
             r"^user\.(name|email)$"],
            cwd="/", env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return values
    for record in data.stdout.decode(errors="replace").split("\0"):
        key, _, value = record.partition("\n")
        if key in values and 0 < len(value) <= 256 and value.isprintable():
            values[key] = value
    return values


def _command_environment(scratch):
    env = {"PATH": DEV_PATH, "HOME": str(scratch), "TMPDIR": str(scratch), "CI": "1",
           "TERM": "dumb", "LANG": "en_US.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "OPENSSL_CONF": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "3",
           "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
           "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false",
           "GIT_CONFIG_KEY_2": "gc.auto", "GIT_CONFIG_VALUE_2": "0"}
    # Apple's Python framework launcher requires posix_spawn even for --version.
    # Use the same framework interpreter directly when the installed launcher is
    # a wrapper. Other interpreters and project virtualenvs retain normal lookup.
    python = shutil.which("python3", path=DEV_PATH)
    if python:
        framework = Path(python).resolve().parent.parent
        real = framework / "Resources/Python.app/Contents/MacOS/Python"
        if real.is_file() and os.access(real, os.X_OK):
            shims = scratch / "bin"
            shims.mkdir(mode=0o700)
            (shims / "python3").symlink_to(real)
            env["PATH"] = str(shims) + ":" + DEV_PATH
    # A neutral scratch global config supplies the user's ordinary commit identity;
    # a repo-local identity can still override it.
    identity = _git_identity()
    lines = ["[user]"]
    for key, value in identity.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    {key.split(".")[1]} = "{escaped}"')
    (scratch / ".gitconfig").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


def _display_output(raw, truncated):
    text = raw.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if truncated or len(encoded) > COMMAND_OUTPUT_LIMIT:
        marker = "[Earlier command output truncated]\n"
        text = marker + encoded[-(COMMAND_OUTPUT_LIMIT - len(marker)):].decode(errors="ignore")
    return text


def _sandbox_command(access, grant, argv, scratch, env, cancelled, file_limit=""):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("local development commands require the macOS sandbox")
    if access.snapshot() != grant or not grant or (cancelled and cancelled()):
        raise ValueError("local development access is unavailable")
    options = ["-D", "PROJECT=" + str(access.project), "-D", "SCRATCH=" + str(scratch)]
    profile = DEV_SANDBOX + "\n" + _git_controls()
    denied = []
    # Even an unusual owner-selected project cannot expose the client's control directory.
    for i, path in enumerate(PrivateFolders(access.fleet_home).locations):
        key = f"PRIVATE_{i}"
        options.extend(("-D", key + "=" + str(path)))
        denied.append(f'(subpath (param "{key}"))')
    profile += "\n(deny file-read-data file-write* " + " ".join(denied) + ")"
    with access.project_fd() as fd:
        if access._project_id(fd) != grant[2] or access.snapshot() != grant:
            raise ValueError("the trusted project changed")
        command = ["/usr/bin/python3", "-I", "-S", "-B", "-c", SANDBOX_ENTRY, str(fd),
                   str(access.project), str(file_limit), "/usr/bin/sandbox-exec",
                   *options, "-p", profile, *argv]
        proc = subprocess.Popen(command, cwd="/", env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, pass_fds=(fd,),
                                start_new_session=True)
    output = bytearray()
    truncated = False
    deadline = time.monotonic() + COMMAND_TIMEOUT
    stream = True
    try:
        while stream or proc.poll() is None:
            if (cancelled and cancelled()) or access.snapshot() != grant:
                raise ValueError("local command interrupted; changes may already have been made")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("local command timed out; changes may already have been made")
            wait = min(0.2, remaining)
            if not stream:
                time.sleep(wait)
                continue
            if select.select([proc.stdout], [], [], wait)[0]:
                part = os.read(proc.stdout.fileno(), 32768)
                stream = bool(part)
                output.extend(part)
                if len(output) > COMMAND_OUTPUT_LIMIT:
                    truncated = True
                    del output[:-COMMAND_OUTPUT_LIMIT]
        return {"exit": proc.wait(), "output": _display_output(output, truncated)}
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()


def run_local_command(access, command, *, cancelled=None):
    """Run a user-authorized development command in the selected checkout's OS sandbox."""
    if (not isinstance(command, str) or not command.strip() or "\0" in command
            or len(command.encode("utf-8")) > COMMAND_LIMIT):
        raise ValueError("invalid or oversized local command")
    grant = access.snapshot()
    if not grant:
        raise ValueError("enable local development access for this project")
    with tempfile.TemporaryDirectory(prefix="ccfleet-project-command-") as temp:
        scratch = Path(temp).resolve()
        return _sandbox_command(access, grant, ["/bin/sh", "-c", command], scratch,
                                _command_environment(scratch), cancelled)


@contextlib.contextmanager
def export_branch_bundle(access, *, cancelled=None):
    """Export only the current named branch from inside the scoped shell sandbox."""
    grant = access.snapshot()
    if not grant or not grant[1]:
        raise ValueError("the project has no pinned publishing remote")
    with (tempfile.TemporaryDirectory(prefix="ccfleet-export-") as producer,
          tempfile.TemporaryDirectory(prefix="ccfleet-publish-") as consumer):
        source = Path(producer).resolve()
        target = Path(consumer).resolve() / "branch.bundle"
        env = _command_environment(source)
        executable = _developer_git() or shutil.which("git", path=DEV_PATH)
        if not executable:
            raise ValueError("Git is unavailable for publishing")
        git = [executable, "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never"]
        ref = _sandbox_command(access, grant, [*git, "symbolic-ref", "--quiet", "HEAD"],
                               source, env, cancelled)
        branch = ref["output"].rstrip("\n").removeprefix("refs/heads/")
        if (ref["exit"] or ref["output"] != f"refs/heads/{branch}\n"
                or not 1 <= len(branch) <= 200 or not branch.isascii()
                or not branch[0].isalnum()
                or any(not (c.isalnum() or c in "._/-") for c in branch)):
            raise ValueError("select a named branch before publishing")
        result = _sandbox_command(access, grant,
                                  [*git, "bundle", "create", str(source / "out.bundle"),
                                   "refs/heads/" + branch],
                                  source, env, cancelled, BUNDLE_LIMIT)
        if result["exit"] or access.snapshot() != grant or (cancelled and cancelled()):
            raise ValueError("unable to export the current branch within the publishing limits")
        fd = os.open(source / "out.bundle", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                    or not 0 < st.st_size <= BUNDLE_LIMIT):
                raise ValueError("invalid project bundle")
            # A different scratch root ensures the producer cannot alter the accepted bundle.
            with target.open("xb") as dest:
                remaining = st.st_size
                while remaining:
                    data = incoming.read(min(remaining, 65536))
                    if not data:
                        raise ValueError("incomplete project bundle")
                    remaining -= dest.write(data)
                after = os.fstat(fd)
                if (incoming.read(1) or after.st_size != st.st_size
                        or after.st_ctime_ns != st.st_ctime_ns):
                    raise ValueError("project bundle changed during export")
        if access.snapshot() != grant or (cancelled and cancelled()):
            raise ValueError("local publishing authorization changed")
        yield branch, target


def tool(name, description, fields, required):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {k: {"type": "string", "description": v} for k, v in fields.items()},
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool(
        "list_project",
        "List one folder in the selected client project.",
        {"path": "Project-relative directory. Use . for its root."},
        [],
    ),
    tool(
        "make_project_directory",
        "Create one directory after local approval.",
        {"path": "Relative directory inside an existing parent"},
        ["path"],
    ),
    tool(
        "read_project",
        "Read a UTF-8 file and its current SHA-256.",
        {"path": "Relative file"},
        ["path"],
    ),
    tool(
        "save_project",
        "Create or replace a UTF-8 client file after local approval.",
        {
            "path": "Relative file",
            "text": "Complete new contents",
            "expected": "SHA-256 from read_project, or new for a new file",
        },
        ["path", "text", "expected"],
    ),
    tool(
        "test_project",
        "Ask the client to run its locally selected test command. Takes no command input.",
        {},
        [],
    ),
]


PRIVATE_COMPONENTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".config", ".claude", ".codex", ".agents", ".gemini",
    ".gnupg", ".kube", ".password-store", ".ccfleet",
})


def _fold_path(path):
    # macOS user volumes commonly compare names case- and normalization-insensitively.
    return Path(unicodedata.normalize("NFD", str(path)).casefold())


def _identity(path):
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


class PrivateFolders:
    """Apply the editor's protected locations to targets, independent of its starting folder."""

    def __init__(self, fleet_home):
        homes = {Path.home().resolve(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
        names = PRIVATE_COMPONENTS | {"Library", ".local/share", ".local/lib/ccfleet-owner"}
        locations = {personal / name for personal in homes for name in names}
        locations.update((Path(fleet_home).absolute(), Path("/Library/Keychains")))
        for p in tuple(locations):
            # A broken optional credential-directory link must not prevent editor startup.
            with contextlib.suppress(OSError, RuntimeError):
                locations.add(p.resolve())
        self.locations = tuple(sorted(locations))
        self.names = {_fold_path(p) for p in self.locations}
        # File identities also cover macOS firmlinks and alternate names for mounted volumes.
        self.identities = {v for p in self.locations if (v := _identity(p)) is not None}
        parents = {parent for p in self.locations for parent in p.parents}
        self.broad_dirs = {v for p in parents if (v := _identity(p)) is not None}

    def blocked(self, path):
        folded = _fold_path(path)
        if PRIVATE_COMPONENTS.intersection(folded.parts) or self.names.intersection(
                (folded, *folded.parents)):
            return True
        return any(_identity(p) in self.identities for p in (path, *path.parents))

    def blocked_entry(self, path, info):
        # The containing directory has already passed the complete ancestry check.
        folded = _fold_path(path)
        return (folded.name in PRIVATE_COMPONENTS or folded in self.names
                or (info.st_dev, info.st_ino) in self.identities)

    def test_allowed(self, path):
        return (not os.path.ismount(path) and _identity(path) not in self.broad_dirs
                and not self.blocked(path))


class Workspace:
    def __init__(self, path, approve, command=None, *, test_path=None, file_tools=None,
                 private_folders=None, cancelled=None):
        if file_tools is None:
            from . import project_files as file_tools
        self.files = file_tools
        self.path = Path(path).resolve(strict=True)
        self.private_folders = private_folders
        self.cancelled = cancelled
        self.test_relative = "."
        if private_folders is None and self.path in (Path.home(), *Path.home().parents):
            raise ValueError("choose a project folder below a home or volume root")
        self.root_fd = self.files._open_root(self.path)
        self.approve = approve
        self.command = COMMANDS[command] if command else None
        self.test_path = test_path or os.environ.get("PATH", os.defpath)

    def close(self):
        os.close(self.root_fd)

    def _check(self):
        if not os.path.samestat(os.fstat(self.root_fd), self.path.stat()):
            raise ValueError("the selected folder moved; reopen it")

    def _guard(self, relative):
        if self.private_folders and self.private_folders.blocked(self.path / relative):
            raise ValueError("this personal data or CCFleet configuration path is protected")

    def test_directory(self, relative=None):
        requested = self.test_relative if relative is None else relative
        if requested != ".":
            self.files._path(requested)
        directory = self.path / requested
        if self.private_folders and not self.private_folders.test_allowed(directory):
            return None
        # Opening the full directory again also rejects a target replaced by a symlink.
        fd = self.files._open_root(directory)
        os.close(fd)
        return directory

    def _list(self, path):
        sentinel = (self.files._path(path) + "/" if path != "." else "") + "entry"
        self._guard(path)
        with self.files._parent(self.root_fd, sentinel) as fd:
            if fd is None:
                raise ValueError("directory unavailable")
            entries = []
            with os.scandir(fd) as directory:
                for entry in directory:
                    if len(entries) >= 1500:
                        raise ValueError("directory too large; open a subfolder")
                    try:
                        self.files._path(entry.name)
                        info = entry.stat(follow_symlinks=False)
                    except (OSError, ValueError):
                        continue
                    if self.private_folders and self.private_folders.blocked_entry(
                            self.path / path / entry.name, info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        entries.append(entry.name + "/")
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        entries.append(entry.name)
            return sorted(entries)

    def _save(self, path, text, expected):
        encoded = text.encode()
        if len(encoded) > LIMIT or not self.approve("Save file", path):
            raise ValueError("save declined or file too large")
        self._check()
        with self.files._parent(self.root_fd, path, create=False) as parent:
            if parent is None:
                raise ValueError("choose an existing parent directory")
            name = path.rsplit("/", 1)[-1]
            existing = self.files._read(self.root_fd, path)
            if expected != (existing["sha256"] if existing else "new"):
                raise ValueError("file changed; read it again")
            # Stage beside the destination. Refuse to overwrite an unobserved new file.
            temp = ".ccfleet-write-" + os.urandom(16).hex()
            fd = os.open(temp, os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(encoded)
                    if existing:
                        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
                        os.fchmod(f.fileno(), stat.S_IMODE(mode) & 0o777)
                    f.flush()
                    os.fsync(f.fileno())
                if existing:
                    if self.files._read(self.root_fd, path) != existing:
                        raise ValueError("file changed; read it again")
                    os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=parent)
        return {"saved": path, "sha256": hashlib.sha256(encoded).hexdigest()}

    def _test(self):
        directory = self.test_directory()
        if directory is None:
            raise ValueError("select the test project's subfolder with /test")
        identity = directory.stat()
        details = " ".join(self.command or ())
        if self.test_relative != ".":
            details += " in " + self.test_relative
        if not self.command or not self.approve("Run local tests", details):
            raise ValueError("local tests were not enabled or approved")
        self._check()
        if self.test_directory() != directory or not os.path.samestat(identity, directory.stat()):
            raise ValueError("the local test directory changed; select it again")
        executable = shutil.which(self.command[0], path=self.test_path)
        if not executable:
            raise ValueError("the selected test runner is not installed")
        with tempfile.TemporaryDirectory(prefix="ccfleet-test-") as tmp:
            scratch = str(Path(tmp).resolve())
            env = {
                "PATH": self.test_path,
                "HOME": scratch,
                "TMPDIR": scratch,
                "LANG": "en_US.UTF-8",
                "CI": "1",
                "TERM": "dumb",
            }
            params = ["-D", "PROJECT=" + str(directory), "-D", "SCRATCH=" + scratch]
            profile = TEST_SANDBOX
            if self.private_folders:
                # Runtime/toolchain read permissions must not grant access to credentials.
                denied = []
                for i, path in enumerate(self.private_folders.locations):
                    key = f"PRIVATE_{i}"
                    params.extend(("-D", key + "=" + str(path)))
                    denied.append(f'(subpath (param "{key}"))')
                profile += "\n(deny file-read-data file-write* " + " ".join(denied) + ")"
            command = ["/usr/bin/sandbox-exec", *params, "-p", profile,
                       executable, *self.command[1:]]
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output = bytearray()
            deadline = time.monotonic() + 90
            stream_open = True
            try:
                while time.monotonic() < deadline:
                    if self.cancelled and self.cancelled():
                        raise ValueError("local test cancelled")
                    if not stream_open:
                        if process.poll() is not None:
                            break
                        time.sleep(0.1)
                        continue
                    if not select.select([process.stdout], [], [], 0.25)[0]:
                        continue
                    part = os.read(process.stdout.fileno(), 32768)
                    if not part:
                        stream_open = False
                        continue
                    output.extend(part)
                    del output[:-16384]
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
                return {"exit": code, "output": output.decode(errors="replace")}
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                process.stdout.close()

    def call(self, name, args):
        self._check()
        schema = next((t["inputSchema"] for t in TOOLS if t["name"] == name), None)
        if (
            not schema
            or not isinstance(args, dict)
            or args.keys() - schema["properties"].keys()
            or set(schema["required"]) - args.keys()
            or any(type(v) is not str for v in args.values())
        ):
            raise ValueError("invalid workspace tool arguments")
        if name == "test_project":
            return self._test()
        if name == "list_project":
            return self._list(args.get("path", "."))
        path = self.files._path(args["path"])
        self._guard(path)
        if name == "make_project_directory":
            if not self.approve("Create directory", path):
                raise ValueError("creation declined")
            self._check()
            with self.files._parent(self.root_fd, path) as parent:
                if parent is None:
                    raise ValueError("choose an existing parent directory")
                os.mkdir(path.rsplit("/", 1)[-1], 0o700, dir_fd=parent)
            return {"created": path}
        if name == "save_project":
            return self._save(path, args["text"], args["expected"])
        item = self.read_optional(path)
        if item is None:
            raise ValueError("file unavailable")
        return item

    def read_optional(self, path):
        """A missing file can be created. Unsafe or unreadable files must never look missing."""
        self._check()
        path = self.files._path(path)
        self._guard(path)
        item = self.files._read(self.root_fd, path)
        if item is None:
            return None
        if len(item["data"]) > LIMIT * 4 // 3:
            raise ValueError("file too large for an editor operation")
        return {"text": base64.b64decode(item["data"]).decode("utf-8"), "sha256": item["sha256"]}

    def rpc(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise ValueError("invalid MCP request")
        if "id" not in msg:
            return None
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        method, params = msg.get("method"), msg.get("params", {})
        if method == "initialize":
            versions = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {
                "protocolVersion": requested if requested in versions else versions[0],
                "serverInfo": {"name": "ccfleet-local-project", "version": "1.0"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "ping":
            result = {}
        elif method == "tools/call" and isinstance(params, dict):
            try:
                data = self.call(params.get("name"), params.get("arguments", {}))
                result = {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
                }
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                # OS errors may include a private absolute path.
                message = str(exc) if isinstance(exc, ValueError) else "local operation unavailable"
                result = {"isError": True, "content": [{"type": "text", "text": message}]}
        else:
            return {**reply, "error": {"code": -32601, "message": "Unsupported method"}}
        return {**reply, "result": result}
