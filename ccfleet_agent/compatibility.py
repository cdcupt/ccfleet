"""Bounded, non-model upgrade checks; reports contain fixed metadata only.

The code/runtime digest attributes a check to installed bytes. It is not JA3,
provider acceptance, or an attestation against the owner of the slot.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import selectors
import signal
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Optional

STATES = frozenset({"pending", "passed", "failed", "blocked"})
CHECKS = frozenset({"native_version", "auth_interface", "usage_parser",
                    "relay_protocol", "tls_policy"})
REASONS = frozenset({
    "native_unavailable", "native_version_unknown", "native_version_changed", "native_timeout",
    "native_interface_changed", "usage_pending", "usage_unavailable", "relay_unavailable",
    "relay_protocol_mismatch", "relay_tls_policy", "account_unbound", "account_transition",
    "sign_in_pending", "credential_unavailable", "probe_busy", "check_interrupted",
    "native_auth_source", "native_extensions",
})
VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
ACCOUNT = re.compile(r"[0-9a-f]{16}\Z")
MAX_OUTPUT = 64 * 1024
MAX_SETTINGS = 4 * 1024 * 1024
COMMAND_TIMEOUT = 10.0
RETRY_S = 5 * 60
MAX_RETRY_S = 60 * 60
CONFLICTING_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
})


def instant(value: Any) -> Optional[float]:
    if type(value) in (int, float) and 0 < value < 1e11 and math.isfinite(value):
        return float(value)
    return None


def report(value: Any) -> dict[str, Any]:
    """Shared agent/server allowlist; never reflect error text or private output."""
    if not isinstance(value, Mapping):
        return {}
    state = value.get("state")
    if not isinstance(state, str) or state not in STATES:
        return {}
    out: dict[str, Any] = {"state": value["state"]}
    for field, pattern in (("native_version", VERSION), ("runtime_fp", HASH),
                           ("account_fp", ACCOUNT)):
        item = value.get(field)
        if isinstance(item, str) and pattern.fullmatch(item):
            out[field] = item
    checked = instant(value.get("checked_at"))
    if checked is None:
        return {}
    out["checked_at"] = checked
    for field in ("next_check_at", "last_success_at", "usage_observed_at"):
        at = instant(value.get(field))
        if at is not None:
            out[field] = at
    reason = value.get("reason")
    if isinstance(reason, str) and reason in REASONS:
        out["reason"] = reason
    checks = value.get("checks")
    if isinstance(checks, Mapping):
        out["checks"] = {key: checks[key] for key in sorted(CHECKS)
                         if isinstance(checks.get(key), bool)}
    if state == "passed" and (any(key not in out for key in (
            "native_version", "runtime_fp", "account_fp", "usage_observed_at"))
            or "reason" in out
            or any(out.get("checks", {}).get(key) is not True for key in CHECKS)):
        return {}
    return out


def runtime_fingerprint(directory: Path) -> Optional[str]:
    """Observe code/runtime changes without executing a request or reading secrets."""
    digest = hashlib.sha256()
    try:
        for name in ("agent.py", "compatibility.py", "local_relay.py",
                     "inference_policy.py", "inference_client.py"):
            raw = (directory / name).read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                return None
            digest.update(name.encode() + b"\0" + raw)
        digest.update(repr(sys.version_info[:3]).encode())
        digest.update(ssl.OPENSSL_VERSION.encode())
        paths = ssl.get_default_verify_paths()
        for path in (paths.cafile, paths.capath):
            if path:
                info = Path(path).stat()
                digest.update(str((info.st_mtime_ns, info.st_size)).encode())
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def native_stamp(path: Optional[str]) -> Optional[list[int]]:
    if not path:
        return None
    try:
        info = Path(path).stat()
        return [info.st_ino, info.st_size, info.st_mtime_ns]
    except OSError:
        return None


def _settings(path: Path) -> Optional[dict[str, Any]]:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS or info.st_nlink != 1:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            parsed = json.loads(stream.read(MAX_SETTINGS + 1))
        return parsed if isinstance(parsed, dict) else None
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def settings_paths(home: Path, probe: Optional[str],
                   expected_config_dir: Optional[Path] = None) -> list[Path]:
    directories = [home, Path(probe)] if probe else [home]
    config_dir = expected_config_dir or home / ".claude"
    paths = [config_dir / name for name in ("settings.json", "settings.local.json")]
    if probe:
        paths.extend(Path(probe) / ".claude" / name
                     for name in ("settings.json", "settings.local.json"))
    paths.extend(directory / ".mcp.json" for directory in directories)
    explicit = expected_config_dir is not None and (bool(os.environ.get("CLAUDE_CONFIG_DIR"))
                or config_dir != home / ".claude")
    paths.extend([config_dir / ".claude.json" if explicit else home / ".claude.json",
                  config_dir / "remote-settings.json",
                  Path("/etc/claude-code/managed-settings.json"),
                  Path("/etc/claude-code/managed-mcp.json"),
                  Path("/Library/Application Support/ClaudeCode/managed-settings.json")])
    paths.append(Path("/Library/Application Support/ClaudeCode/managed-mcp.json"))
    paths.extend(Path("/etc/claude-code/managed-settings.d").glob("*.json"))
    return paths


def auth_context_reason(home: Path, probe: Optional[str],
                        expected_config_dir: Optional[Path] = None) -> Optional[str]:
    """Conservative known effective config guard, without retaining config values."""
    def conflict(name: str, value: Any) -> bool:
        if not value:
            return False
        if name != "CLAUDE_CONFIG_DIR" or expected_config_dir is None or not isinstance(value, str):
            return True
        try:
            return Path(value).expanduser().resolve() != expected_config_dir.resolve()
        except (OSError, RuntimeError):
            return True

    if any(conflict(name, os.environ.get(name)) for name in CONFLICTING_ENV):
        return "native_auth_source"
    paths = (settings_paths(home, probe) if expected_config_dir is None
             else settings_paths(home, probe, expected_config_dir))
    for path in paths:
        parsed = _settings(path)
        if parsed is None:
            return "native_auth_source"
        # Cached managed settings can wrap the native settings block.
        configurations = [parsed, *(parsed[key] for key in ("settings", "managedSettings")
                                   if isinstance(parsed.get(key), dict))]
        projects = parsed.get("projects", {})
        if isinstance(projects, dict):
            for directory in [home, *([Path(probe)] if probe else [])]:
                for key in {str(directory), str(directory.resolve())}:
                    project = projects.get(key)
                    if isinstance(project, dict):
                        configurations.append(project)
        for config in configurations:
            if (config.get("apiKeyHelper") or config.get("primaryApiKey") or config.get("apiKey")
                    or config.get("forceLoginMethod") not in (None, "claudeai")):
                return "native_auth_source"
            environment = config.get("env", {})
            if (not isinstance(environment, dict)
                    or any(conflict(k, environment.get(k)) for k in CONFLICTING_ENV)):
                return "native_auth_source"
            plugins = config.get("enabledPlugins", {})
            if (config.get("hooks") or config.get("mcpServers") or config.get("managedMcpServers")
                    or not isinstance(plugins, dict) or any(plugins.values())):
                return "native_extensions"
    return None


def auth_context_clean(home: Path, probe: Optional[str]) -> bool:
    return auth_context_reason(home, probe) is None


def auth_context_stamp(home: Path, probe: Optional[str]) -> Optional[str]:
    """Hash only relevant config choices/presence, never token or private values."""
    if auth_context_reason(home, probe) is not None:
        return None
    choices = []
    for path in settings_paths(home, probe):
        parsed = _settings(path)
        if parsed is None:
            return None
        for config in [parsed, *(parsed[key] for key in ("settings", "managedSettings")
                                 if isinstance(parsed.get(key), dict))]:
            environment = config.get("env", {})
            if not isinstance(environment, dict):
                return None
            choices.append({"login_method": config.get("forceLoginMethod"),
                            "credential_helper": bool(config.get("apiKeyHelper")),
                            "stored_key": bool(config.get("apiKey") or config.get("primaryApiKey")),
                            "hooks": bool(config.get("hooks")),
                            "mcp": bool(config.get("mcpServers")),
                            "plugins": bool(any(config.get("enabledPlugins", {}).values()))
                            if isinstance(config.get("enabledPlugins", {}), dict) else True,
                            "overrides": {k: bool(environment.get(k))
                                          for k in sorted(CONFLICTING_ENV)}})
    return hashlib.sha256(json.dumps(choices, sort_keys=True).encode()).hexdigest()


def probe_environment(home: Path, expected_config_dir: Optional[Path] = None) -> dict[str, str]:
    """Explicit same-context child env; never inherit the old tmux server's auth."""
    environment = {**{name: os.environ[name] for name in (
        "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        if name in os.environ}, "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(home / ".local/bin"),
        "TERM": "screen-256color", "DISABLE_AUTOUPDATER": "1"}
    if expected_config_dir is not None and (expected_config_dir != home / ".claude"
                                           or os.environ.get("CLAUDE_CONFIG_DIR")):
        environment["CLAUDE_CONFIG_DIR"] = str(expected_config_dir)
    return environment


def command(path: str, arguments: list[str], runner: Callable[..., Any]
            ) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Strict output/time budget, no stderr retention, and no model command."""
    argv = [path, *arguments]
    if arguments not in (["--version"], ["auth", "--help"], ["auth", "status"]):
        return None, None, "native_interface_changed"
    environment = probe_environment(Path.home())
    if runner is not subprocess.run:
        try:
            result = runner(argv, capture_output=True, text=True, check=False,
                            timeout=COMMAND_TIMEOUT, env=environment)
        except subprocess.TimeoutExpired:
            return None, None, "native_timeout"
        except (OSError, subprocess.SubprocessError):
            return None, None, "native_unavailable"
        raw = result.stdout
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_OUTPUT:
            return None, None, "native_interface_changed"
        return result.returncode, raw, None
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "P_PID", "WEXITED")):
        return None, None, "native_unavailable"
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        return None, None, "native_unavailable"
    process = None
    group_owned = True
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=environment,
                                   start_new_session=True)
        deadline = time.monotonic() + COMMAND_TIMEOUT
        collected = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(collected)))
                if not chunk:
                    # Do not reap the leader before its owned process group is
                    # cleaned up: its unreaped PID prevents group-ID reuse.
                    while time.monotonic() < deadline:
                        result = os.waitid(os.P_PID, process.pid,
                                           os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        if result is not None:
                            code = (result.si_status if result.si_code == os.CLD_EXITED
                                    else -result.si_status)
                            return code, collected.decode("utf-8", "replace"), None
                        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                    return None, None, "native_timeout"
                collected.extend(chunk)
                if len(collected) > MAX_OUTPUT:
                    return None, None, "native_interface_changed"
        return None, None, "native_timeout"
    except ChildProcessError:
        group_owned = False
        return None, None, "native_unavailable"
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None, None, "native_timeout"
    except OSError:
        return None, None, "native_unavailable"
    finally:
        if process is not None:
            # Always stop same-group descendants, including those whose leader
            # already exited or whose stdout was redirected. The leader has
            # not been reaped above, so this cannot target a reused PGID.
            if group_owned:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.stdout is not None:
                process.stdout.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def native_checks(path: Optional[str], expected: str, runner: Callable[..., Any], *,
                  observation: Optional[dict[str, Any]] = None
                  ) -> tuple[dict[str, bool], Optional[str]]:
    checks: dict[str, bool] = {}
    if not path:
        return checks, "native_unavailable"
    code, output, reason = command(path, ["--version"], runner)
    if reason:
        return checks, reason
    match = re.search(r"(?<![0-9.])[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?![0-9.])", output or "")
    checks["native_version"] = code == 0 and match is not None and match.group(0) == expected
    if not checks["native_version"]:
        return checks, "native_version_changed"
    code, output, reason = command(path, ["auth", "--help"], runner)
    if reason:
        return checks, reason
    if code != 0 or not re.search(r"\bstatus\b", output or ""):
        checks["auth_interface"] = False
        return checks, "native_interface_changed"
    code, output, reason = command(path, ["auth", "status"], runner)
    if reason:
        return checks, reason
    try:
        parsed = json.loads(output or "")
    except (ValueError, RecursionError):
        parsed = None
    # Signed out is valid interface output. It is not provider acceptance or
    # evidence that a refreshable access token requires permanent re-login.
    checks["auth_interface"] = (code in (0, 1) and isinstance(parsed, dict)
                                and isinstance(parsed.get("loggedIn"), bool))
    if checks["auth_interface"] and observation is not None:
        observation["signed_in"] = parsed["loggedIn"]
        observation["source_matches"] = (parsed.get("authMethod") == "claude.ai"
                                         and parsed.get("apiProvider") == "firstParty")
    return checks, None if checks["auth_interface"] else "native_interface_changed"


def relay_checks(relay: Any, client: Any, home: Path
                 ) -> tuple[dict[str, bool], Optional[str]]:
    """Exercise the real status framing and verified-TLS policy, without POSTs."""
    checks: dict[str, bool] = {}
    connection = None
    try:
        connection = relay.connect_upstream()  # constructor only; never connect/request
        context = getattr(connection, "_context", None)
        checks["tls_policy"] = (connection.host == "api.anthropic.com" and connection.port == 443
                                and isinstance(context, ssl.SSLContext) and context.check_hostname
                                and context.verify_mode == ssl.CERT_REQUIRED
                                and context.cert_store_stats().get("x509_ca", 0) > 0)
    except Exception:
        checks["tls_policy"] = False
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if not checks["tls_policy"]:
        return checks, "relay_tls_policy"

    def forbidden_connect() -> Any:
        raise RuntimeError("compatibility never opens inference")

    try:
        if client.VERSION != 2:
            return checks, "relay_protocol_mismatch"
        output = io.BytesIO()
        code = relay.serve_one(io.BytesIO(client.preamble({"operation": "status"})), output, home,
                               connect=forbidden_connect)
        response = io.BytesIO(output.getvalue())
        metadata = client.metadata(response)
        if metadata["status"] in (401, 403, 409):
            checks["relay_protocol"] = True
            return checks, "credential_unavailable"
        body = client.chunk(response)
        ending = client.chunk(response)
        checks["relay_protocol"] = (code == 0 and metadata["status"] == 200 and not ending
                                    and body == b'{"ready":true,"protocol":2}'
                                    and response.read() == b"")
    except Exception:
        checks["relay_protocol"] = False
    return checks, None if checks["relay_protocol"] else "relay_protocol_mismatch"
