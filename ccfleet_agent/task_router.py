"""Opt-in operator task delegation; no HTTP server or inference API routing.

This module deliberately sits outside the immutable signed laptop release.
Native agents run as the operator, with the operator's existing credentials.
Workspace selection is an allowlist, not an OS isolation boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

if __package__ in {None, ""}:
    # Detached workers start inside the selected project. Resolve imports only
    # from this operator-installed package, never from that project or PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccfleet_agent import local_jobs

MAX_CONFIG_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900
AUTH_TIMEOUT_S = 15
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
BACKENDS = frozenset({"claude", "devin"})
ENVIRONMENT = frozenset({
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
})


class RouterError(ValueError):
    pass


def _unique(pairs):
    answer = {}
    for key, value in pairs:
        if key in answer:
            raise RouterError("duplicate configuration field")
        answer[key] = value
    return answer


def _text(value: Any, label: str, maximum: int = 256) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RouterError("invalid " + label)
    return value


def _absolute(value: Any, label: str, *, canonical: bool = True) -> Path:
    value = _text(value, label, 4096)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise RouterError(label + " must be an absolute path without symlinks")
    if canonical:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise RouterError("invalid " + label) from exc
        if resolved != path:
            raise RouterError(label + " must be an absolute path without symlinks")
    return path


def _read_regular(path: Path, maximum: int, *, config: bool = False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise RouterError("input must be a bounded regular file")
        if config and (info.st_uid != os.getuid() or info.st_nlink != 1
                       or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}):
            raise RouterError("configuration must be operator-owned mode 0600 or 0644")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise RouterError("input is too large")
        return data
    finally:
        os.close(fd)


def load_config(path: Path, *, check_resources: bool = True) -> dict[str, Any]:
    """Read strict configuration without reading any provider credentials."""
    path = _absolute(str(path), "configuration path")
    raw = _read_regular(path, MAX_CONFIG_BYTES, config=True)
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RouterError("invalid router configuration") from exc
    required = {"version", "state_dir", "workspaces", "backends"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"enabled", "timeout_s"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value.get("enabled", False)) is not bool):
        raise RouterError("invalid router configuration fields")
    timeout = value.get("timeout_s", DEFAULT_TIMEOUT_S)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_S:
        raise RouterError("timeout_s must be an integer from 1 to 900")
    root = _absolute(value["state_dir"], "state directory")
    workspaces = value["workspaces"]
    if not isinstance(workspaces, dict) or not 1 <= len(workspaces) <= 32:
        raise RouterError("configure between 1 and 32 workspaces")
    for name, workspace in workspaces.items():
        if (not IDENTIFIER.fullmatch(name) or not isinstance(workspace, dict)
                or set(workspace) != {"path", "trusted"}
                or type(workspace["trusted"]) is not bool):
            raise RouterError("invalid workspace configuration")
        project = _absolute(workspace["path"], "workspace path", canonical=check_resources)
        if check_resources and not project.is_dir():
            raise RouterError("workspace directory is unavailable")
        if (root == project or root.is_relative_to(project) or path.is_relative_to(project)
                or project.is_relative_to(root)):
            raise RouterError("router controls and workspaces must be separate")
    backends = value["backends"]
    if (not isinstance(backends, dict) or not backends
            or set(backends) - BACKENDS):
        raise RouterError("configure only claude and devin backends")
    for backend in backends.values():
        if (not isinstance(backend, dict) or set(backend) != {
                "binary", "model", "auth_verified"}
                or type(backend["auth_verified"]) is not bool):
            raise RouterError("invalid backend configuration")
        binary = _absolute(backend["binary"], "backend binary", canonical=check_resources)
        if check_resources and (not binary.is_file() or not os.access(binary, os.X_OK)):
            raise RouterError("backend binary is unavailable")
        model = _text(backend["model"], "selected model")
        if model.startswith("-"):
            raise RouterError("selected model cannot begin with an option prefix")
    return {**value, "enabled": value.get("enabled", False), "timeout_s": timeout,
            "binding": hashlib.sha256(raw).hexdigest()}


def child_environment() -> dict[str, str]:
    """Keep native homes/tools, omit inherited provider keys, models and routing."""
    return {key: value for key, value in os.environ.items() if key in ENVIRONMENT}


def _devin_credentials_available(environment: dict[str, str]) -> bool:
    home = environment.get("HOME")
    if not home:
        return False
    directory = Path(environment.get("XDG_DATA_HOME") or Path(home) / ".local" / "share")
    if not directory.is_absolute():
        return False
    try:
        fd = os.open(directory / "devin" / "credentials.toml",
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                    and 0 < info.st_size <= MAX_CONFIG_BYTES)
        finally:
            os.close(fd)
    except OSError:
        return False


def authorized(config_path: Path, root: Path, binding: str,
               spec: dict[str, Any]) -> bool:
    try:
        config = load_config(config_path)
        route = config["backends"].get(spec["slot_id"])
        return bool(config["enabled"] and config["binding"] == binding
                    and config["state_dir"] == str(root)
                    and spec["device_id"] == binding
                    and route and route["auth_verified"]
                    and spec["options"] == {"model": route["model"]}
                    and any(workspace["path"] == spec["project"] and workspace["trusted"]
                            for workspace in config["workspaces"].values()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _runner(config_path: Path, operation: str, root: Path, binding: str) -> list[str]:
    return [sys.executable, "-I", str(Path(__file__).resolve()), "--config", str(config_path),
            operation, "--state-dir", str(root), "--binding", binding]


def start(config_path: Path, config: dict[str, Any], backend: str,
          workspace: str, prompt_file: Path) -> dict[str, Any]:
    if not config["enabled"]:
        raise RouterError("task router is disabled; operator verification is required")
    route = config["backends"].get(backend)
    project = config["workspaces"].get(workspace)
    if route is None or project is None:
        raise RouterError("unknown backend or workspace")
    if not route["auth_verified"] or not project["trusted"]:
        raise RouterError("operator authentication and workspace trust verification are required")
    try:
        prompt = _read_regular(prompt_file, MAX_PROMPT_BYTES).decode("utf-8")
    except UnicodeError as exc:
        raise RouterError("prompt must be UTF-8") from exc
    if not prompt.strip() or "\0" in prompt:
        raise RouterError("prompt must be nonempty and contain no NUL")
    root = Path(config["state_dir"])
    spec = {"device_id": config["binding"], "slot_id": backend,
            "project": project["path"], "prompt": prompt,
            "options": {"model": route["model"]}}
    result = local_jobs.start(root, spec, _runner(config_path, "_supervise", root,
                                               config["binding"]),
                              timeout_s=config["timeout_s"], max_jobs=1)
    return {**result, "backend": backend, "selected_model": route["model"]}


def _artifact(root: Path, job_id: str, name: str, data: bytes) -> Path:
    # Reuse the supervisor's checked private-directory operations. These are
    # internal trusted files, never caller-selected artifact paths.
    with local_jobs._job(root, job_id) as directory:
        fd = local_jobs._open_file(directory, name, os.O_WRONLY | os.O_EXCL, create=True)
        try:
            local_jobs._write_all(fd, data)
        finally:
            os.close(fd)
    return root / job_id / name


def execute(config_path: Path, root: Path, binding: str, job_id: str) -> int:
    with local_jobs.parent_guard():
        spec = local_jobs.read_spec(root, job_id)
        if not authorized(config_path, root, binding, spec):
            return 2
        config = load_config(config_path)
        route = config["backends"][spec["slot_id"]]
        environment = child_environment()
        # A successful `devin auth status` is insufficient: release 3000.11.92
        # also returns zero while logged out. The operator attestation above
        # and private native-file metadata are additional fail-closed gates;
        # neither proves entitlement, token validity or billing behavior.
        if spec["slot_id"] == "devin" and not _devin_credentials_available(environment):
            print("Native Devin credentials are unavailable; operator sign-in is required.",
                  file=sys.stderr)
            return 2
        arguments = [route["binary"]]
        if spec["slot_id"] == "devin":
            generated = _artifact(root, job_id, "devin-config.json", json.dumps({
                "auto_update": False, "subagents_enabled": False,
                "agent": {"model": route["model"]}}, separators=(",", ":")).encode())
            arguments.extend(["--config", str(generated)])
        try:
            authentication = subprocess.run([*arguments, "auth", "status"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=spec["project"], env=environment, timeout=AUTH_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            print("Native authentication check timed out.", file=sys.stderr)
            return 2
        if authentication.returncode != 0:
            print("Native authentication check failed.", file=sys.stderr)
            return authentication.returncode
        # Recheck after the bounded auth subprocess and before model execution.
        if not authorized(config_path, root, binding, spec):
            return 2
        arguments.extend(["--model", route["model"], "--print"])
        if spec["slot_id"] == "devin":
            prompt_path = _artifact(root, job_id, "prompt.txt", spec["prompt"].encode())
            arguments.extend(["--respect-workspace-trust", "true", "--prompt-file",
                              str(prompt_path), "--export", str(root / job_id / "trace.atif")])
            # The native export may contain full prompts/tool results. The
            # directory and the process umask keep newly created files private.
            os.umask(0o077)
            return subprocess.run(arguments, stdin=subprocess.DEVNULL, cwd=spec["project"],
                                  env=environment, check=False).returncode
        return subprocess.run(arguments, input=spec["prompt"].encode(), cwd=spec["project"],
                              env=environment, check=False).returncode


def supervise(config_path: Path, root: Path, binding: str, job_id: str) -> int:
    return local_jobs.run(root, job_id,
        command=lambda spec: [*_runner(config_path, "_execute", root, binding), job_id],
        check_authorization=lambda spec: authorized(config_path, root, binding, spec))


def _summary(root: Path, value: dict[str, Any]) -> dict[str, Any]:
    spec = local_jobs.read_spec(root, value["job_id"])
    return {**value, "backend": spec["slot_id"], "selected_model": spec["options"].get("model")}


def list_tasks(config: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(config["state_dir"])
    return [_summary(root, item) for item in local_jobs.list_jobs(root)]


def status(config: dict[str, Any], job_id: str) -> dict[str, Any]:
    root = Path(config["state_dir"])
    return _summary(root, local_jobs.status(root, job_id))


def stop(config: dict[str, Any], job_id: str) -> dict[str, Any]:
    root = Path(config["state_dir"])
    return _summary(root, local_jobs.stop(root, job_id))


def logs(config: dict[str, Any], job_id: str, *, stream: str = "stdout",
         max_bytes: int = 65536) -> dict[str, Any]:
    data = local_jobs.logs(Path(config["state_dir"]), job_id, stream=stream, max_bytes=max_bytes)
    return {"job_id": job_id, "stream": stream,
            "text": data.decode("utf-8", errors="replace"), "bytes": len(data)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, required=True)
    commands = result.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("start", help="submit one explicit operator task")
    submit.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--prompt-file", type=Path, required=True)
    commands.add_parser("list", help="list private task metadata")
    for name in ("status", "logs", "stop", "_supervise", "_execute"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
        if name == "logs":
            command.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
            command.add_argument("--bytes", type=int, default=65536)
        if name.startswith("_"):
            command.add_argument("--state-dir", type=Path, required=True)
            command.add_argument("--binding", required=True)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config_path = _absolute(str(arguments.config), "configuration path")
        if arguments.command in {"_supervise", "_execute"}:
            if not re.fullmatch(r"[0-9a-f]{64}", arguments.binding):
                raise RouterError("invalid operator configuration binding")
            root = _absolute(str(arguments.state_dir), "state directory")
            operation = supervise if arguments.command == "_supervise" else execute
            return operation(config_path, root, arguments.binding, arguments.job_id)
        config = load_config(config_path, check_resources=arguments.command == "start")
        if arguments.command == "start":
            value = start(config_path, config, arguments.backend, arguments.workspace,
                          arguments.prompt_file)
        elif arguments.command == "list":
            value = list_tasks(config)
        elif arguments.command == "status":
            value = status(config, arguments.job_id)
        elif arguments.command == "stop":
            value = stop(config, arguments.job_id)
        else:
            value = logs(config, arguments.job_id, stream=arguments.stream,
                         max_bytes=arguments.bytes)
        print(json.dumps(value, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Do not echo paths, model output, credentials or arbitrary exception
        # strings into an outer agent's automatic error context.
        print(json.dumps({"error": "task_router_failed",
                          "message": "Task operation failed; check private operator configuration "
                                     "and task status. Do not automatically resubmit."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
