"""Bounded native safe-mode /usage probe for the same default Mac Keychain.

Only the native executable reads/renews its credential. No credential is copied
or supplied to the child. Customizations are disabled by verified native flags;
managed policy and every authentication-source guard remain authoritative.
"""
from __future__ import annotations

import codecs
import fcntl
import importlib.util
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path
from typing import Any, Callable

REQUIRED_FLAGS = ("--safe-mode", "--setting-sources", "--strict-mcp-config", "--mcp-config",
                  "--settings", "--tools", "--permission-mode")
SAFE_ARGUMENTS = ["--safe-mode", "--setting-sources", "", "--strict-mcp-config",
                  "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}',
                  "--tools", "", "--permission-mode", "dontAsk"]
MAX_SCREEN = 32 * 1024
MAX_TOTAL_OUTPUT = 512 * 1024


def sibling(name: str):
    if name not in {"mac_policy", "mac_credentials", "compatibility", "agent"}:
        raise ValueError("unsupported private owner probe module")
    module_name = "ccfleet_mac_safe_probe_" + name
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ValueError("private owner probe runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def supported_help(raw: bytes) -> bool:
    text = raw.decode("utf-8", "replace")
    if any(flag not in text for flag in REQUIRED_FLAGS):
        return False
    start = text.find("--safe-mode")
    end = text.find("--session-id", start)
    section = text[start:end if end > start else start + 1200]
    return "Auth" in section and "work normally" in section and "disabled" in section


def guard(home: Path, probe: Path) -> str | None:
    policy, compatibility = sibling("mac_policy"), sibling("compatibility")
    try:
        policy.authenticated_owner(home)
        descriptor = policy.open_directory(probe, private=True)
        os.close(descriptor)
    except (OSError, ValueError):
        return "native_probe_busy"
    if Path.home() != home or any(os.environ.get(key) for key in compatibility.CONFLICTING_ENV):
        return "native_auth_source"
    paths = compatibility.settings_paths(home, str(probe), home / ".claude")
    if len(paths) > 64:
        return "native_auth_source"
    for path in paths:
        parsed = compatibility._settings(path)
        if parsed is None:
            return "native_auth_source"
        managed = (not path.is_relative_to(home) or path.name in {
            "remote-settings.json", "managed-settings.json", "managed-mcp.json"})
        configs = [parsed, *(parsed[key] for key in ("settings", "managedSettings")
                             if isinstance(parsed.get(key), dict))]
        projects = parsed.get("projects", {})
        if isinstance(projects, dict):
            for directory in (home, probe):
                value = projects.get(str(directory))
                if isinstance(value, dict):
                    configs.append(value)
        for config in configs:
            env = config.get("env", {})
            if (config.get("apiKeyHelper") or config.get("primaryApiKey") or config.get("apiKey")
                    or config.get("forceLoginMethod") not in (None, "claudeai")
                    or not isinstance(env, dict)
                    or any(env.get(key) for key in compatibility.CONFLICTING_ENV)):
                return "native_auth_source"
            plugins = config.get("enabledPlugins", {})
            if not isinstance(plugins, dict):
                return "native_extensions"
            # Native safe-mode suppresses user customizations. Managed policy
            # still applies, so never use it to override managed extensions.
            if managed and (config.get("hooks") or config.get("mcpServers")
                            or config.get("managedMcpServers") or any(plugins.values())):
                return "native_extensions"
    return None


def _screen(raw: str) -> str:
    # Keep only a bounded recent native display, never persist or report it.
    raw = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", raw)
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
    raw = raw.replace("\r", "\n")
    return "".join(c for c in raw if c in "\n\t" or ord(c) >= 32)


def _trust_action(text: str) -> bytes | None:
    choices = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([❯>]?)\s*([12])[.)]\s*(Yes, I trust this folder|No, exit)\s*",
                             line)
        if match:
            choices.append((bool(match[1]), int(match[2]), match[3].startswith("Yes")))
    selected = next((choice for choice in reversed(choices) if choice[0]), None)
    yes = next((choice for choice in reversed(choices) if choice[2]), None)
    if selected is None or yes is None:
        return None
    if selected[2]:
        return b"\r"
    return b"\x1b[A" if yes[1] < selected[1] else b"\x1b[B"


def _drive(argv: list[str], environment: dict[str, str], probe: Path, timeout: float,
           *, now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    core = sibling("agent")
    pid = master = None
    result = {"outcome": "native_launch_failed", "probe_started": False,
              "cleanup_confirmed": False, "usage_seen": False}
    screen = ""
    typed = submitted = trusted = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.chdir(probe)
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 180, 0, 0))
                os.execve(argv[0], argv, environment)
            except BaseException:
                os._exit(127)
        result.update(outcome="native_timeout", probe_started=True)
        deadline = now() + timeout
        total = 0
        while now() < deadline:
            ready, _, _ = select.select([master], [], [], min(0.25, max(0, deadline - now())))
            if not ready:
                continue
            try:
                data = os.read(master, 8192)
            except OSError:
                break  # PTY peers commonly signal ordinary exit with EIO.
            if not data:
                break
            total += len(data)
            if total > MAX_TOTAL_OUTPUT:
                break
            screen = (screen + decoder.decode(data))[-MAX_SCREEN:]
            text = _screen(screen)
            classified = core.native_probe_outcome(text, time.time())
            outcome = classified.get("outcome")
            if outcome in {"native_auth_rejected", "native_login_expired", "native_network_error"}:
                result["outcome"] = outcome
                break
            if not trusted and "trust this folder" in text.lower():
                # This grants trust only to the separately checked private probe
                # directory; no user project or arbitrary server path is used.
                action = _trust_action(text)
                if action is not None:
                    os.write(master, action)
                    trusted = action == b"\r"
                    screen = ""
                    continue
            prompt = core._quota_prompt_input(text)
            if not typed:
                if prompt == "" or (prompt is not None and re.fullmatch(r'Try "[^"\r\n]{1,160}"',
                                                                          prompt)):
                    os.write(master, b"/usage")
                    typed = True
                    screen = ""
                continue
            if not submitted:
                if prompt == "/usage":
                    os.write(master, b"\r")
                    submitted = True
                    screen = ""
                continue
            if core.parse_quota(text):
                result.update(outcome="native_refresh_unconfirmed", usage_seen=True)
                break
    except (OSError, ValueError):
        pass
    finally:
        screen = ""
        if pid is not None and pid > 0:
            # The child is deliberately not reaped before group cleanup, so its
            # PID cannot be reused as an unrelated process-group identifier.
            group_cleaned = True
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                group_cleaned = False
            cleanup_deadline = time.monotonic() + 2
            while time.monotonic() < cleanup_deadline:
                try:
                    waited, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    waited = pid
                if waited == pid:
                    result["cleanup_confirmed"] = group_cleaned
                    break
                time.sleep(0.01)
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
    return result


def run(native_path: str, home: Path, probe: Path, *,
        before_start: Callable[[], bool], timeout: float = 45) -> dict[str, Any]:
    unavailable = {"outcome": "native_probe_busy", "probe_started": False,
                   "cleanup_confirmed": True, "usage_seen": False}
    reason = guard(home, probe)
    if reason:
        return {**unavailable, "outcome": reason}
    adapter = sibling("mac_credentials")
    try:
        code, help_output = adapter.bounded_output([native_path, "--help"], home,
                                                   timeout=10, limit=64 * 1024)
        if code != 0 or not supported_help(help_output):
            return {**unavailable, "outcome": "native_auth_source"}
        if not before_start() or guard(home, probe):
            return unavailable
        environment = adapter.native_environment(home)
        environment.update(TERM="xterm-256color", CLAUDE_CODE_SAFE_MODE="1")
    except (OSError, ValueError):
        return unavailable
    try:
        return _drive([native_path, *SAFE_ARGUMENTS], environment, probe, min(60, timeout))
    except (OSError, ValueError):
        # Once the process-driving boundary is entered, an exceptional return
        # cannot establish that no child started or that cleanup succeeded.
        return {**unavailable, "probe_started": True, "cleanup_confirmed": False}
