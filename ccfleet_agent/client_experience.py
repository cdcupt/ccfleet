"""Local CC Fleet UX helpers; no provider credentials, history parsing or uploads.

The executable supplies its existing transport and launch functions as callbacks.
Diagnostic callbacks must be bounded and read-only. Launch callbacks retain the
executable's own consent, pairing, routing and legacy-cleanup protections.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Optional

MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")
MAX_PREFERENCES_BYTES = 128 * 1024
MAX_PROJECTS = 512


class ExperienceError(ValueError):
    """A safe, actionable local UX error; messages contain no private values."""


def _project_key(project: Path) -> str:
    try:
        resolved = Path(project).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise OSError("not a directory")
        return hashlib.sha256(os.fsencode(resolved)).hexdigest()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ExperienceError("project directory is unavailable") from exc


def _values(values: Any, *, changes: bool = False) -> dict[str, Any]:
    if not isinstance(values, dict) or set(values) - {"model", "effort", "mode"}:
        raise ExperienceError("preferences contain unsupported fields")
    for key, value in values.items():
        if changes and value is None:
            continue
        if not isinstance(value, str) or not (
            (key == "model" and MODEL.fullmatch(value))
            or (key == "effort" and value in EFFORTS)
            or (key == "mode" and value in MODES)
        ):
            raise ExperienceError("invalid model, effort or permission preference")
    return dict(values)


def _private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise ExperienceError("preferences must be private files owned by this user")


class Preferences:
    """Separate private preferences indexed by a hash of the canonical directory.

    No project path, conversation, file content or account/device data is saved.
    Reads do not create files. Writes are locked, atomic and restricted to the
    owning user. Explicit native arguments should override these defaults; do
    not apply saved defaults when resuming a native conversation.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser()
        if not self.directory.is_absolute():
            raise ExperienceError("preferences directory must be absolute")

    def _directory(self, *, create: bool = False) -> Optional[int]:
        try:
            if create:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ExperienceError("private preferences directory is unavailable") from exc
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ExperienceError("preferences directory must be private and user-owned")
        return fd

    @staticmethod
    def _load(directory: int) -> dict[str, Any]:
        try:
            fd = os.open(
                "preferences.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return {"version": 1, "projects": {}}
        try:
            _private_file(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_PREFERENCES_BYTES + 1)
            if len(raw) > MAX_PREFERENCES_BYTES:
                raise ExperienceError("preferences exceed the local storage limit")
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "projects"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or not isinstance(data["projects"], dict)
                or len(data["projects"]) > MAX_PROJECTS
            ):
                raise ExperienceError("preferences have an unsupported format")
            for key, values in data["projects"].items():
                if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise ExperienceError("preferences have an invalid project identifier")
                _values(values)
            return data
        finally:
            os.close(fd)

    def get(self, project: Path) -> dict[str, str]:
        key = _project_key(project)
        directory = self._directory()
        if directory is None:
            return {}
        try:
            return dict(self._load(directory)["projects"].get(key, {}))
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, ExperienceError):
                raise
            raise ExperienceError("preferences could not be read safely") from exc
        finally:
            os.close(directory)

    def _write(self, project: Path, changes: Optional[dict[str, Any]]) -> dict[str, str]:
        key = _project_key(project)
        checked = _values(changes, changes=True) if changes is not None else None
        directory = self._directory(create=True)
        if directory is None:
            raise ExperienceError("private preferences directory is unavailable")
        lock = None
        temporary = ".preferences-" + secrets.token_hex(16)
        try:
            try:
                lock = os.open("preferences.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL
                               | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            except FileExistsError:
                lock = os.open("preferences.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=directory)
            _private_file(lock)
            # A crashed writer releases this lock automatically. A short bounded
            # wait prevents an unrelated stale process from hanging the launcher.
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ExperienceError(
                            "preferences are busy; retry after the other change"
                        ) from exc
                    time.sleep(0.025)
            data = self._load(directory)
            result = dict(data["projects"].get(key, {}))
            if checked is None:
                result = {}
            else:
                for name, value in checked.items():
                    if value is None:
                        result.pop(name, None)
                    else:
                        result[name] = value
            if result:
                data["projects"][key] = result
            else:
                data["projects"].pop(key, None)
            if len(data["projects"]) > MAX_PROJECTS:
                raise ExperienceError("too many saved projects; clear an unused preference")
            raw = (json.dumps(data, sort_keys=True) + "\n").encode()
            if len(raw) > MAX_PREFERENCES_BYTES:
                raise ExperienceError("preferences exceed the local storage limit")
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "preferences.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            return result
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, ExperienceError):
                raise
            raise ExperienceError("preferences could not be saved safely") from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def set(self, project: Path, values: dict[str, Any]) -> dict[str, str]:
        return self._write(project, values)

    def clear(self, project: Path) -> None:
        self._write(project, None)


# All diagnostic strings are fixed. Neither exception text nor callback output
# is copied into a report. Even a saved device name may contain personal data.
CHECKS = {
    "ssh_ready": ("ssh", "pass", "OpenSSH is available.", "No action needed."),
    "ssh_missing": ("ssh", "fail", "OpenSSH is missing.", "Install OpenSSH, then rerun setup."),
    "claude_ready": ("claude", "pass", "Original local Claude is available.", "No action needed."),
    "claude_missing": (
        "claude",
        "fail",
        "Original local Claude is unavailable.",
        "Rerun the setup installer.",
    ),
    "version_ready": ("version", "pass", "Native Claude version was read.", "No action needed."),
    "version_timeout": (
        "version",
        "warn",
        "Native version check timed out.",
        "Check the local Claude installation; no model request was made.",
    ),
    "version_unavailable": (
        "version",
        "warn",
        "Native version could not be verified.",
        "Check the local Claude installation; no model request was made.",
    ),
    "pairing_ready": (
        "pairing",
        "pass",
        "A saved device pairing was selected.",
        "No action needed.",
    ),
    "pairing_missing": (
        "pairing",
        "fail",
        "No matching paired device is available.",
        "Run setup or select an already paired slot.",
    ),
    "pairing_invalid": (
        "pairing",
        "fail",
        "Saved pairing could not be read safely.",
        "Keep the configuration; check Connected devices before pairing again.",
    ),
    "key_ready": (
        "key",
        "pass",
        "The private SSH key is present and protected.",
        "No action needed.",
    ),
    "key_invalid": (
        "key",
        "fail",
        "The saved private SSH key is missing or unsafe.",
        "Keep the configuration; check Connected devices before pairing again.",
    ),
    "pin_ready": (
        "pin",
        "pass",
        "The saved host-pin file is present and protected.",
        "The relay check verifies the pinned SSH connection.",
    ),
    "pin_invalid": (
        "pin",
        "fail",
        "The saved SSH host-pin file is missing or unsafe.",
        "Do not disable host checking; contact your operator.",
    ),
    "settings_ready": (
        "settings",
        "pass",
        "No managed routing conflict was detected.",
        "No action needed.",
    ),
    "settings_conflict": (
        "settings",
        "fail",
        "Managed settings conflict with the selected-slot route.",
        "Ask the settings administrator to resolve the routing policy.",
    ),
    "settings_unchecked": (
        "settings",
        "warn",
        "Managed routing checks are unavailable.",
        "Update CC Fleet before relying on this check.",
    ),
    "device_ready": (
        "device",
        "pass",
        "The server recognizes this device and assignment.",
        "No action needed.",
    ),
    "device_revoked": (
        "device",
        "fail",
        "The server did not authorize this device.",
        "Check Connected devices on your slot page; pair again only if revoked.",
    ),
    "device_forbidden": (
        "device",
        "fail",
        "The server denied this device's assignment.",
        "Check your slot page or contact your operator.",
    ),
    "device_connection": (
        "device",
        "fail",
        "The server device check could not connect.",
        "Check network access to CC Fleet, then retry; keep the saved pairing.",
    ),
    "device_invalid": (
        "device",
        "fail",
        "The server returned an unsupported device report.",
        "Update CC Fleet or contact your operator.",
    ),
    "device_unchecked": (
        "device",
        "warn",
        "A separate server device check is unavailable.",
        "The relay check still tests the selected device's connection.",
    ),
    "slot_report_ready": (
        "slot",
        "pass",
        "The server reports a held, ready slot.",
        "The direct relay check verifies current reachability.",
    ),
    "slot_report_pending": (
        "slot",
        "warn",
        "The server reports pending slot maintenance or sign-in.",
        "Check the slot page; the direct relay check is the current readiness test.",
    ),
    "slot_report_degraded": (
        "slot",
        "warn",
        "The server reports degraded slot health.",
        "Check the slot page or contact your operator; keep the saved pairing.",
    ),
    "relay_ready": (
        "relay",
        "pass",
        "The assigned slot's inference relay is ready.",
        "No model request was made.",
    ),
    "relay_renewal": (
        "relay",
        "fail",
        "The assigned slot needs Claude sign-in renewal.",
        "Open your slot page; use Sign in again if automatic renewal does not recover.",
    ),
    "relay_disabled": (
        "relay",
        "fail",
        "Inference is not enabled for this slot.",
        "Ask your operator to check the slot's inference policy.",
    ),
    "relay_transition": (
        "relay",
        "fail",
        "The slot's account transition is pending.",
        "Wait for the slot page to confirm completion, then retry.",
    ),
    "relay_connection": (
        "relay",
        "fail",
        "The pinned slot connection could not be completed.",
        "Check network access and Connected devices; do not remove the host pin.",
    ),
    "relay_unchecked": (
        "relay",
        "warn",
        "Relay readiness was not checked.",
        "Repair the preceding pairing or key problem, then rerun doctor.",
    ),
}
PRIVACY = (
    "This check does not scan project files, read conversation history or make a model request.",
    "Supported model requests use the assigned slot; selected identity headers "
    "and structured metadata are removed.",
    "Native prompts and tool results can still contain paths, OS details and private content.",
    "Hooks, plugins, MCP servers and tools may connect directly from this computer.",
    "BWH sees connection metadata, not the inner SSH stream; "
    "the slot administrator can inspect relayed content.",
    "A readiness check is not proof of an actual inference route or zero metadata disclosure.",
)


def _check(code: str, version: str = "") -> dict[str, str]:
    identifier, state, message, advice = CHECKS[code]
    item = {"id": identifier, "state": state, "code": code, "message": message, "advice": advice}
    if code == "version_ready" and VERSION.fullmatch(version):
        item["version"] = version
    return item


def native_version(binary: str, timeout: float = 5) -> str:
    """Bounded native --version; no stdout/stderr or environment is returned."""
    env = {
        "PATH": os.defpath,
        "HOME": str(Path.home()),
        "TERM": "dumb",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    # The small supervisor keeps its process-group identity alive after the
    # native program exits. Cleanup always precedes reaping that group leader.
    supervisor = """import os,subprocess,sys
try:
    child=subprocess.Popen([sys.argv[1],"--version"],stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
    data=child.stdout.read(4097)
    code=child.wait()
    sys.stdout.buffer.write(str(code).encode()+b"\\n"+data)
    sys.stdout.buffer.flush()
finally:
    os.close(1)
    sys.stdin.buffer.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", supervisor, binary],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    output = bytearray()
    deadline = time.monotonic() + max(0.1, min(timeout, 10))
    try:
        assert process.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("native version check", timeout)
            if not select.select([process.stdout], [], [], remaining)[0]:
                raise subprocess.TimeoutExpired("native version check", timeout)
            block = os.read(process.stdout.fileno(), 4114 - len(output))
            if not block:
                break
            output.extend(block)
            if len(output) > 4113:
                raise ExperienceError("native version output is invalid")
        code, separator, version = bytes(output).partition(b"\n")
        if separator != b"\n" or code != b"0" or len(version) > 4096:
            raise ExperienceError("native version check failed")
        match = re.fullmatch(rb"\s*(\d{1,4}\.\d{1,4}\.\d{1,6})(?: \(Claude Code\))?\s*", version)
        if match is None:
            raise ExperienceError("native version output is invalid")
        return match[1].decode("ascii")
    finally:
        # The version process can exit while a child retains stdout. Clean the
        # owned process group even if its original leader has already exited.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None:
            process.stdin.close()


def _safe_saved_file(value: Any, *, private: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        info = Path(value).lstat()
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & (0o077 if private else 0o022)
            and info.st_size > 0
        )
    except (OSError, ValueError):
        return False


def _http_status(exc: Exception) -> Optional[int]:
    value = getattr(exc, "status", None)
    return value if type(value) is int else None


def _device_context_checks(context: Any) -> list[dict[str, str]]:
    if (
        not isinstance(context, dict)
        or context.get("authenticated") is not True
        or type(context.get("protocol")) is not int
        or context["protocol"] != 2
        or not isinstance(context.get("device"), dict)
        or context["device"].get("active") is not True
        or not isinstance(context.get("slot"), dict)
    ):
        return [_check("device_invalid")]
    slot = context["slot"]
    if (
        slot.get("state") not in ("claimed", "active", "claiming", "free", "releasing")
        or type(slot.get("ready")) is not bool
        or slot.get("health")
        not in ("ready", "renewal_pending", "degraded", "sign_in_required", "switching")
    ):
        return [_check("device_invalid")]
    if slot["ready"] and slot["health"] == "ready" and slot["state"] in ("claimed", "active"):
        code = "slot_report_ready"
    elif slot["health"] == "degraded":
        code = "slot_report_degraded"
    else:
        code = "slot_report_pending"
    return [_check("device_ready"), _check(code)]


def _report(
    callbacks: Mapping[str, Callable[..., Any]], *, slot: str, privacy: bool, kind: str
) -> dict[str, Any]:
    checks = []
    if kind == "doctor":
        which = callbacks.get("which", shutil.which)
        try:
            available = bool(which("ssh")) and bool(which("ssh-keygen"))
        except Exception:
            available = False
        checks.append(_check("ssh_ready" if available else "ssh_missing"))
        try:
            binary = callbacks["find_local_claude"]()
            if not isinstance(binary, str) or not binary:
                raise ValueError("missing")
        except Exception:
            checks.append(_check("claude_missing"))
        else:
            checks.append(_check("claude_ready"))
            try:
                version = callbacks.get("run_version", native_version)(binary, timeout=5)
                if not isinstance(version, str) or not VERSION.fullmatch(version):
                    raise ValueError("invalid")
                checks.append(_check("version_ready", version))
            except subprocess.TimeoutExpired:
                checks.append(_check("version_timeout"))
            except Exception:
                checks.append(_check("version_unavailable"))
        if "check_managed_route" in callbacks:
            try:
                callbacks["check_managed_route"]()
                checks.append(_check("settings_ready"))
            except Exception:
                checks.append(_check("settings_conflict"))
        else:
            checks.append(_check("settings_unchecked"))
    device = None
    try:
        config = callbacks["load_config"]()
        if not isinstance(config, dict) or not isinstance(config.get("devices"), dict):
            raise ValueError("invalid")
    except Exception:
        checks.append(_check("pairing_invalid"))
    else:
        try:
            device = callbacks["choose_device"](config, slot)
            required = ("device_id", "device_token", "slot_id", "server", "key", "known_hosts")
            if not isinstance(device, dict) or any(
                not isinstance(device.get(k), str) or not device[k] for k in required
            ):
                device = None
                checks.append(_check("pairing_invalid"))
            else:
                checks.append(_check("pairing_ready"))
        except Exception:
            device = None
            checks.append(_check("pairing_missing"))
    if device is not None:
        key_ok = _safe_saved_file(device.get("key"), private=True)
        pin_ok = _safe_saved_file(device.get("known_hosts"), private=False)
        checks.extend(
            (
                _check("key_ready" if key_ok else "key_invalid"),
                _check("pin_ready" if pin_ok else "pin_invalid"),
            )
        )
        if "device_context" in callbacks:
            try:
                context = callbacks["device_context"](device)
                checks.extend(_device_context_checks(context))
            except Exception as exc:
                code = {
                    401: "device_revoked",
                    403: "device_forbidden",
                    404: "device_unchecked",
                }.get(_http_status(exc), "device_connection")
                checks.append(_check(code))
        else:
            checks.append(_check("device_unchecked"))
        if key_ok and pin_ok:
            try:
                callbacks["check_inference"](device, timeout=10)
                checks.append(_check("relay_ready"))
            except Exception as exc:
                code = {401: "relay_renewal", 403: "relay_disabled", 409: "relay_transition"}.get(
                    _http_status(exc), "relay_connection"
                )
                checks.append(_check(code))
        else:
            checks.append(_check("relay_unchecked"))
    else:
        checks.append(_check("relay_unchecked"))
    return {
        "schema": 1,
        "kind": kind,
        "ok": not any(c["state"] == "fail" for c in checks),
        "checks": checks,
        "privacy": list(PRIVACY) if privacy else [],
    }


def doctor(
    callbacks: Mapping[str, Callable[..., Any]], *, slot: str = "", privacy: bool = False
) -> dict[str, Any]:
    return _report(callbacks, slot=slot, privacy=privacy, kind="doctor")


def status(callbacks: Mapping[str, Callable[..., Any]], *, slot: str = "") -> dict[str, Any]:
    """Read-only device and relay readiness; no native process/version probe."""
    return _report(callbacks, slot=slot, privacy=False, kind="status")


def safe_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild strictly from codes, never serialize arbitrary report fields."""
    if (
        report.get("kind") not in ("doctor", "status")
        or not isinstance(report.get("checks"), list)
        or not 1 <= len(report["checks"]) <= 16
    ):
        raise ExperienceError("unsupported diagnostic report")
    checks = []
    seen = set()
    for item in report["checks"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("code"), str)
            or item["code"] not in CHECKS
        ):
            raise ExperienceError("unsupported diagnostic check")
        code = item["code"]
        if CHECKS[code][0] in seen:
            raise ExperienceError("duplicate diagnostic check")
        seen.add(CHECKS[code][0])
        version = item.get("version", "")
        if code == "version_ready" and (
            not isinstance(version, str) or not VERSION.fullmatch(version)
        ):
            raise ExperienceError("unsupported native version in diagnostic report")
        checks.append(_check(code, version if isinstance(version, str) else ""))
    return {
        "schema": 1,
        "kind": report["kind"],
        "ok": not any(c["state"] == "fail" for c in checks),
        "checks": checks,
        "privacy": list(PRIVACY) if report.get("privacy") else [],
    }


def render_report(report: Mapping[str, Any]) -> str:
    clean = safe_report(report)
    lines = ["CC Fleet " + clean["kind"] + (": ready" if clean["ok"] else ": action needed")]
    for item in clean["checks"]:
        line = f"[{item['state']}] {item['code']}: {item['message']}"
        if "version" in item:
            line += " " + item["version"]
        lines.append(line)
        if item["state"] != "pass":
            lines.append("  " + item["advice"])
    if clean["privacy"]:
        lines.extend(["", "Privacy boundaries:", *clean["privacy"]])
    return "\n".join(lines)


def export_report(report: Mapping[str, Any], destination: Path) -> None:
    """Explicit local export only, never overwrite or upload a file."""
    clean = safe_report(report)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(clean, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ExperienceError(
            "support export could not be created; choose a new private file"
        ) from exc


def launch(
    action: str,
    callbacks: Mapping[str, Callable[..., Any]],
    *,
    project: Path,
    slot: str = "",
    preferences: Optional[Preferences] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> int:
    """Build launch options, then delegate to the executable's protected flow."""
    if action not in ("new", "continue", "resume", "remote"):
        raise ExperienceError("choose a local new, continue, resume or explicit remote session")
    supplied = dict(options or {})
    if action == "remote":
        return callbacks["launch_remote"]({**supplied, "slot": slot})
    _project_key(project)  # Existence/type only; no project files are opened.
    values: dict[str, Any] = {
        "project": str(project),
        "slot": slot,
        "new_session": action == "new",
        "continue_session": action == "continue",
        "resume": "" if action == "resume" else None,
    }
    extras = supplied.get("claude_args") or []
    resuming = (
        action != "new"
        or supplied.get("resume") is not None
        or supplied.get("continue_session") is True
        or any(
            item in ("--resume", "-r", "--continue", "-c")
            or (isinstance(item, str) and item.startswith("--resume="))
            for item in extras
        )
    )
    if not resuming and preferences is not None:
        values.update(preferences.get(project))
    values.update({k: v for k, v in supplied.items() if v is not None})
    values.update(project=str(project), slot=slot)
    # Never imply --yes: the existing local-launch callback owns cleanup consent.
    return callbacks["launch_local"](values)


def menu(
    callbacks: Mapping[str, Callable[..., Any]],
    *,
    project: Path,
    slot: str = "",
    preferences: Optional[Preferences] = None,
    options: Optional[Mapping[str, Any]] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
    is_tty: Optional[bool] = None,
) -> int:
    """One explicit action, never silently change legacy command defaults."""
    if not (sys.stdin.isatty() if is_tty is None else is_tty):
        raise ExperienceError("the menu needs a terminal; use ccfleet local or ccfleet doctor")
    output_fn(
        "CC Fleet: local Claude uses this computer's files and your assigned slot's model relay.\n"
        "1. New local conversation\n2. Continue this folder's last conversation\n"
        "3. Native local resume picker\n4. Remote terminal (slot files)\n"
        "5. This project's new-session preferences\n6. Diagnose connection\n0. Quit"
    )
    try:
        choice = input_fn("Choose [1-6, 0]: ").strip()
        if choice == "0":
            return 0
        if choice in ("1", "2", "3"):
            return launch(
                {"1": "new", "2": "continue", "3": "resume"}[choice],
                callbacks,
                project=project,
                slot=slot,
                preferences=preferences,
                options=options,
            )
        if choice == "4":
            output_fn(
                "Remote Claude uses slot files; it does not share or synchronize this folder."
            )
            if input_fn("Open the remote terminal? [y/N]: ").strip().lower() != "y":
                return 0
            # Local conversation names and cleanup consent are not remote entry
            # arguments. Explicit model/effort/permission choices still apply.
            remote_options = {key: value for key, value in (options or {}).items()
                              if key in ("model", "effort", "mode") and value is not None}
            return launch("remote", callbacks, project=project, slot=slot, options=remote_options)
        if choice == "5":
            if preferences is None:
                raise ExperienceError("project preferences are unavailable")
            current = preferences.get(project)
            output_fn(
                "Saved new-session preferences: "
                + json.dumps(current, sort_keys=True)
                + "\n1. Model\n2. Effort\n3. Permission mode\n4. Clear saved preferences\n0. Back"
            )
            setting = input_fn("Choose a preference [1-4, 0]: ").strip()
            if setting == "0":
                return 0
            if setting == "4":
                preferences.clear(project)
            elif setting in ("1", "2", "3"):
                name = {"1": "model", "2": "effort", "3": "mode"}[setting]
                if name != "model":
                    output_fn("Choices: " + ", ".join(EFFORTS if name == "effort" else MODES))
                value = input_fn("New value (blank clears this preference): ").strip()
                preferences.set(project, {name: value or None})
            else:
                raise ExperienceError("invalid preference selection; nothing changed")
            output_fn(
                "Saved locally for new conversations; no saved preference is applied on resume."
            )
            return 0
        if choice == "6":
            report = doctor(callbacks, slot=slot, privacy=True)
            output_fn(render_report(report))
            return 0 if report["ok"] else 1
        raise ExperienceError("invalid menu selection; no session was started")
    except EOFError:
        return 0


# Management MCP deliberately lives in this already-pinned helper. Adding a new
# helper would make older signed updaters reject the otherwise valid release.
# This is a small stdio-only subset of MCP 2025-06-18 / 2025-11-25: no HTTP server,
# filesystem roots, resources, prompts, sampling, logging or server-initiated RPC.
MCP_PROTOCOLS = ("2025-06-18", "2025-11-25")
MCP_MAX_LINE = 64 * 1024
MCP_MAX_CALLS = 20  # Per process, in a sliding minute; no background polling.
MCP_MAX_COUNTER = 2**53 - 1
MCP_MAX_EPOCH = 253402300799
MCP_MAX_LATENCY = 3_600_000
MCP_FRESH_SECONDS = 300
MCP_QUOTA_FRESH_SECONDS = 600
MCP_OUTCOMES = (
    "success", "auth_errors", "permission_errors", "rate_limits", "upstream_errors",
    "connection_errors", "cancelled", "input_errors",
)
MCP_TIMINGS = ("connect_ms", "first_byte_ms", "total_ms")
MCP_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens")
MCP_REASONS = {
    "ready": {"credentials_current"},
    "renewal_pending": {"native_renewal_pending", "renewal_due"},
    "degraded": {"observation_stale", "account_unbound", "expiry_unknown"},
    "sign_in_required": {"not_signed_in", "credential_expired"},
    "switching": {"sign_in_pending", "account_transition", "account_mismatch"},
}


def _management_number(value: Any, upper: float = MCP_MAX_EPOCH) -> bool:
    return type(value) in (int, float) and 0 <= value <= upper and math.isfinite(value)


def _management_counter(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MCP_MAX_COUNTER


def _management_slot(status: Any, now: float) -> dict[str, Any]:
    if (not isinstance(status, dict) or status.get("authenticated") is not True
            or type(status.get("protocol")) is not int or status["protocol"] != 2
            or not isinstance(status.get("device"), dict)
            or status["device"].get("active") is not True
            or not isinstance(status.get("slot"), dict)):
        raise ExperienceError("invalid management status")
    slot = status["slot"]
    health, reason, state = slot.get("health"), slot.get("reason"), slot.get("state")
    if (not isinstance(health, str) or health not in MCP_REASONS
            or not isinstance(reason, str) or reason not in MCP_REASONS[health]
            or state not in ("free", "claiming", "claimed", "active", "releasing", "unknown")
            or slot.get("readiness_source") != "reported" or type(slot.get("ready")) is not bool
            or slot["ready"] != (health == "ready")
            or (slot["ready"] and state not in ("claimed", "active"))):
        raise ExperienceError("invalid management health")
    # A malformed known status field invalidates every projection, not just
    # health. Missing/stale observations remain legitimate unknown/stale data.
    _management_observation(slot.get("observed_at"), now, MCP_FRESH_SECONDS)
    return slot


def _management_observation(value: Any, now: float, fresh_seconds: int) -> dict[str, Any]:
    if value is None:
        return {"observed_at": None, "age_seconds": None, "freshness": "unknown"}
    if not _management_number(value) or value > now + 60:
        raise ExperienceError("invalid management observation time")
    age = max(0, now - value)
    return {"observed_at": float(value), "age_seconds": int(age),
            "freshness": "fresh" if age <= fresh_seconds else "stale"}


def _management_now(now: Optional[float]) -> float:
    value = time.time() if now is None else now
    if not _management_number(value):
        raise ExperienceError("invalid management clock")
    return value


def management_health(status: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """Project fixed health codes only; a heartbeat is not a provider acceptance test."""
    current = _management_now(now)
    slot = _management_slot(status, current)
    observation = _management_observation(slot.get("observed_at"), current,
                                          MCP_FRESH_SECONDS)
    return {"source": "cached_slot_report", "slot_state": slot["state"],
            "health": slot["health"], "reason": slot["reason"], **observation,
            "reported_ready": slot["ready"] and observation["freshness"] == "fresh",
            "provider_acceptance_verified": False}


def management_quota(status: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """Cached native subscription percentages, never a refresh or inferred billing value."""
    current = _management_now(now)
    slot = _management_slot(status, current)
    raw = slot.get("quota")
    result: dict[str, Any] = {"source": "cached_native_quota", "available": False,
                             **_management_observation(None, current, MCP_QUOTA_FRESH_SECONDS),
                             "windows": {}}
    if raw is None:
        return result
    if not isinstance(raw, dict):
        raise ExperienceError("invalid management quota")
    # Older servers omit the quota observation. Their percentages cannot be
    # attributed to a fresh/current-account observation, so disclose no numbers.
    if raw.get("checked_at") is None:
        return result
    observation = _management_observation(raw["checked_at"], current, MCP_QUOTA_FRESH_SECONDS)
    windows = {}
    for name in ("session", "week"):
        if name not in raw:
            continue
        window = raw[name]
        if not isinstance(window, dict) or not _management_number(window.get("used_pct"), 100):
            raise ExperienceError("invalid management quota window")
        reset = window.get("resets_at")
        if reset is not None and not _management_number(reset):
            raise ExperienceError("invalid management quota reset")
        used = window["used_pct"]
        windows[name] = {"used_pct": used, "remaining_pct": 100 - used, "resets_at": reset}
    return {**result, **observation, "available": bool(windows), "windows": windows}


def _management_usage(raw: Any, successes: int) -> dict[str, Any]:
    keys = {"eligible", "samples", "cache_read_samples", "cache_creation_samples",
            *MCP_TOKEN_FIELDS}
    if (not isinstance(raw, dict) or set(raw) != keys
            or any(not _management_counter(value) for value in raw.values())
            or not raw["samples"] <= raw["eligible"] <= successes
            or not raw["cache_read_samples"] <= raw["samples"]
            or not raw["cache_creation_samples"] <= raw["samples"]):
        raise ExperienceError("invalid management token coverage")
    for key, count in (("input_tokens", "samples"), ("output_tokens", "samples"),
                       ("cache_read_input_tokens", "cache_read_samples"),
                       ("cache_creation_input_tokens", "cache_creation_samples")):
        if raw[key] > raw[count] * 10**9:
            raise ExperienceError("invalid management token total")
    return {"eligible": raw["eligible"], "samples": raw["samples"],
            "coverage_pct": 100 * raw["samples"] / raw["eligible"] if raw["eligible"] else None,
            "input_tokens": raw["input_tokens"] if raw["samples"] else None,
            "output_tokens": raw["output_tokens"] if raw["samples"] else None,
            "cache_read_samples": raw["cache_read_samples"],
            "cache_creation_samples": raw["cache_creation_samples"],
            "cache_read_input_tokens": (raw["cache_read_input_tokens"]
                                        if raw["cache_read_samples"] else None),
            "cache_creation_input_tokens": (raw["cache_creation_input_tokens"]
                                            if raw["cache_creation_samples"] else None)}


def management_relay_usage(status: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """Revalidate the fixed seven-UTC-day report without importing another client helper.

    Zero timing/usage samples become null observations, not zero performance or
    free inference. No account binding, model, transcript, or other identifier is
    part of this view; its fixed fields mirror the server's numeric report v1.
    """
    current = _management_now(now)
    slot = _management_slot(status, current)
    result: dict[str, Any] = {
        "source": "cached_relay_aggregates", "available": False,
        "window_days": 7, "window_kind": "utc_calendar_days", "billing": False,
        **_management_observation(None, current, MCP_FRESH_SECONDS),
    }
    raw = slot.get("relay")
    if raw is None:
        return result
    keys = {"version", "observed_at", "window_days", "last_success_at", "latencies",
            "requests", *MCP_OUTCOMES}
    if (not isinstance(raw, dict) or set(raw) - {"token_usage"} != keys
            or type(raw["version"]) is not int or raw["version"] != 1
            or type(raw["window_days"]) is not int or raw["window_days"] != 7
            or not _management_number(raw["observed_at"])
            or any(not _management_counter(raw[key]) for key in ("requests", *MCP_OUTCOMES))
            or sum(raw[key] for key in MCP_OUTCOMES) != raw["requests"]):
        raise ExperienceError("invalid management relay report")
    observation = _management_observation(raw["observed_at"], current, MCP_FRESH_SECONDS)
    if raw["observed_at"] < current - 7 * 86400:
        return result
    last = raw["last_success_at"]
    first_day = max(0, int(raw["observed_at"] // 86400) - 6) * 86400
    if ((raw["success"] == 0 and last is not None)
            or (raw["success"] > 0 and (not _management_number(last)
                or not first_day <= last <= raw["observed_at"]))):
        raise ExperienceError("invalid management relay success time")
    if not isinstance(raw["latencies"], dict) or set(raw["latencies"]) != set(MCP_TIMINGS):
        raise ExperienceError("invalid management relay timings")
    latencies = {}
    for key in MCP_TIMINGS:
        sample = raw["latencies"][key]
        if (not isinstance(sample, dict) or set(sample) != {"count", "mean", "max"}
                or not _management_counter(sample["count"]) or sample["count"] > raw["requests"]
                or not _management_number(sample["mean"], MCP_MAX_LATENCY)
                or not _management_number(sample["max"], MCP_MAX_LATENCY)
                or sample["mean"] > sample["max"]
                or (sample["count"] == 0 and (sample["mean"] != 0 or sample["max"] != 0))):
            raise ExperienceError("invalid management relay timing sample")
        count = sample["count"]
        latencies[key] = {"samples": count, "mean_ms": sample["mean"] if count else None,
                          "max_ms": sample["max"] if count else None}
    usage = _management_usage(raw["token_usage"], raw["success"]) if "token_usage" in raw else None
    return {**result, **observation, "available": True,
            "counts": {key: raw[key] for key in ("requests", *MCP_OUTCOMES)},
            "last_success_at": last, "latency_unit": "milliseconds", "latencies": latencies,
            "token_usage": usage}


_MCP_TOOLS = {
    "ccfleet_health": (management_health,
        "Read cached health of the one paired slot. Reported readiness is not a live "
        "connection or provider acceptance test. Returns fixed health codes and observation age."),
    "ccfleet_quota": (management_quota,
        "Read cached native subscription quota percentages and reset timestamps for the one "
        "paired slot. Missing observations are unknown, not zero. Does not refresh quota."),
    "ccfleet_relay_usage": (management_relay_usage,
        "Read cached numeric relay counts, latency samples and optional token coverage for "
        "seven UTC calendar days, not just today. Not billing or subscription quota. "
        "Missing samples are unknown; completed transfer does not prove task success."),
}


def _mcp_tools() -> list[dict[str, Any]]:
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}}
            for name, (_function, description) in _MCP_TOOLS.items()]


def _mcp_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _mcp_constant(_value):
    raise ValueError("nonfinite JSON")


def _mcp_id(value: Any) -> bool:
    # Request IDs are echoed only in the required JSON-RPC envelope, never sent
    # to the status callback or included in tool results, stored, or logged.
    return ((type(value) is int and -MCP_MAX_COUNTER <= value <= MCP_MAX_COUNTER)
            or (isinstance(value, str) and 1 <= len(value) <= 128
                and all(32 <= ord(char) <= 126 for char in value)))


def _mcp_error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _mcp_result(identifier: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": value}


def _mcp_tool_result(value: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {"isError": error, "structuredContent": value,
            "content": [{"type": "text", "text": json.dumps(value, sort_keys=True,
                                                                 allow_nan=False)}]}


def _mcp_failure(exc: Exception) -> dict[str, Any]:
    code = {401: "pairing_unavailable", 403: "access_denied", 404: "service_unavailable",
            409: "assignment_changed"}.get(_http_status(exc), "status_unavailable")
    return _mcp_tool_result({"available": False, "error": code,
                            "message": "The paired-slot observation is unavailable. "
                            "Check CC Fleet in a separate terminal; no changes were made."},
                           error=True)


def serve_mcp(read_status: Callable[[], Any], *, input_stream=None, output_stream=None) -> int:
    """Opt-in, bounded NDJSON management server over stdio; never logs input/output.

    ``read_status`` owns bounded GET-only transport and binding/revocation checks
    for one preselected existing pairing. It must not scan projects, make model
    requests, invoke native clients, follow redirects, or print anything. The
    callback is invoked once per authorized tools/call and receives no caller
    arguments. Discovery/initialization/ping never invoke it. Tools intentionally
    disclose their fixed numeric/code results to the calling MCP client and AI.

    Streams default to binary stdio. Text IO is supported for embedded tests.
    Oversized or unterminated frames close the session; no unbounded draining.
    """
    incoming = sys.stdin.buffer if input_stream is None else input_stream
    outgoing = sys.stdout.buffer if output_stream is None else output_stream
    phase = "new"
    calls: deque[float] = deque()

    def emit(value):
        line = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        outgoing.write(line if isinstance(outgoing, io.TextIOBase) else line.encode("utf-8"))
        outgoing.flush()

    try:
        while True:
            line = incoming.readline(MCP_MAX_LINE + 1)
            if not line:
                return 0
            try:
                raw = line.encode("utf-8") if isinstance(line, str) else line
            except UnicodeError:
                emit(_mcp_error(None, -32700, "Invalid JSON message"))
                continue
            if len(raw) > MCP_MAX_LINE or not raw.endswith(b"\n"):
                emit(_mcp_error(None, -32600, "Message exceeds framing limits"))
                return 2
            try:
                text = raw[:-1].removesuffix(b"\r").decode("utf-8")
                if "\r" in text:
                    raise ValueError("embedded line break")
                message = json.loads(text, object_pairs_hook=_mcp_unique,
                                     parse_constant=_mcp_constant)
            except (ValueError, UnicodeError, RecursionError, OverflowError):
                emit(_mcp_error(None, -32700, "Invalid JSON message"))
                continue
            if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                    or set(message) - {"jsonrpc", "id", "method", "params"}
                    or not isinstance(message.get("method"), str)
                    or not 1 <= len(message["method"]) <= 128
                    or ("id" in message and not _mcp_id(message["id"]))):
                emit(_mcp_error(None, -32600, "Invalid request"))
                continue
            method, identifier = message["method"], message.get("id")
            params = message.get("params", {})
            if "id" not in message:
                if method == "notifications/initialized" and phase == "initializing" \
                        and isinstance(params, dict) and not set(params) - {"_meta"} \
                        and isinstance(params.get("_meta", {}), dict):
                    phase = "ready"
                # Notifications never invoke a tool and never get a response.
                continue
            if not isinstance(params, dict):
                emit(_mcp_error(identifier, -32602, "Invalid parameters"))
                continue
            # _meta is bounded by framing, discarded, and never reflected or sent
            # to the status callback (including progress tokens or trace context).
            if "_meta" in params and not isinstance(params["_meta"], dict):
                emit(_mcp_error(identifier, -32602, "Invalid parameters"))
                continue
            params = {key: value for key, value in params.items() if key != "_meta"}
            if method == "ping":
                emit(_mcp_error(identifier, -32602, "Invalid parameters") if params else
                     _mcp_result(identifier, {}))
                continue
            if method == "initialize":
                client = params.get("clientInfo")
                if (phase != "new" or set(params) != {
                        "protocolVersion", "capabilities", "clientInfo"}
                        or not isinstance(params["protocolVersion"], str)
                        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", params["protocolVersion"]) is None
                        or not isinstance(params["capabilities"], dict)
                        or not isinstance(client, dict)
                        or any(not isinstance(client.get(key), str)
                               or not 1 <= len(client[key]) <= 256 for key in ("name", "version"))):
                    emit(_mcp_error(identifier, -32602, "Invalid initialization"))
                    continue
                protocol = params["protocolVersion"]
                phase = "initializing"
                emit(_mcp_result(identifier, {
                    "protocolVersion": protocol if protocol in MCP_PROTOCOLS else MCP_PROTOCOLS[-1],
                    "capabilities": {"tools": {"listChanged": False}},
                    # Fixed implementation metadata required by MCP; never the
                    # native Claude, OS, account, slot or clientInfo version.
                    "serverInfo": {"name": "ccfleet-management", "version": "1"},
                    "instructions": "Opt-in read-only access to one paired slot's cached "
                    "health and numeric usage. Tool results are visible to the calling AI. "
                    "No models are invoked and no quota is refreshed. Observations are not "
                    "billing, current-day usage, or a guarantee of provider acceptance.",
                }))
                continue
            if phase != "ready":
                emit(_mcp_error(identifier, -32002, "Server is not initialized"))
                continue
            if method == "tools/list":
                # All three tools fit one page. Any cursor is invalid; echo none.
                emit(_mcp_error(identifier, -32602, "Invalid parameters") if params else
                     _mcp_result(identifier, {"tools": _mcp_tools()}))
            elif method == "tools/call":
                name = params.get("name")
                if (set(params) - {"name", "arguments"} or not isinstance(name, str)
                        or name not in _MCP_TOOLS
                        or not isinstance(params.get("arguments", {}), dict)
                        or params.get("arguments", {})):
                    emit(_mcp_error(identifier, -32602, "Unknown tool or invalid arguments"))
                    continue
                now = time.monotonic()
                while calls and calls[0] <= now - 60:
                    calls.popleft()
                if len(calls) >= MCP_MAX_CALLS:
                    emit(_mcp_result(identifier, _mcp_tool_result(
                        {"available": False, "error": "rate_limited",
                         "message": "Management observation limit reached; try again later."},
                        error=True)))
                    continue
                calls.append(now)
                try:
                    result = _MCP_TOOLS[name][0](read_status())
                except Exception as exc:
                    result = _mcp_failure(exc)
                else:
                    result = _mcp_tool_result(result)
                emit(_mcp_result(identifier, result))
            else:
                emit(_mcp_error(identifier, -32601, "Method not found"))
    except (OSError, UnicodeError):
        return 1
    except KeyboardInterrupt:
        return 130
