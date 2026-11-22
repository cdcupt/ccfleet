"""Local, selected-project MCP tools for a directly operated Antigravity session."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
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


class Workspace:
    def __init__(self, path, approve, command=None, *, test_path=None, file_tools=None):
        if file_tools is None:
            from . import project_files as file_tools
        self.files = file_tools
        self.path = Path(path).resolve(strict=True)
        if self.path == Path(self.path.anchor) or self.path in (Path.home(), *Path.home().parents):
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

    def _list(self, path):
        sentinel = (self.files._path(path) + "/" if path != "." else "") + "entry"
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
        if not self.command or not self.approve("Run local tests", " ".join(self.command)):
            raise ValueError("local tests were not enabled or approved")
        self._check()
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
            command = (
                "/usr/bin/sandbox-exec",
                "-D",
                "PROJECT=" + str(self.path),
                "-D",
                "SCRATCH=" + scratch,
                "-p",
                TEST_SANDBOX,
                executable,
                *self.command[1:],
            )
            process = subprocess.Popen(
                command,
                cwd=self.path,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output = bytearray()
            deadline = time.monotonic() + 90
            try:
                while time.monotonic() < deadline:
                    if not select.select([process.stdout], [], [], 0.25)[0]:
                        continue
                    part = os.read(process.stdout.fileno(), 32768)
                    if not part:
                        break
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
        item = self.files._read(self.root_fd, self.files._path(path))
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
