#!/usr/bin/env python3
"""ccfleet node agent: posts one heartbeat about this node to the fleet server.

Standard library only. Runs as the node owner's user, reads nothing but public
facts about the machine plus two non-secret fields of the Claude Code credentials
file (its modification time and the access-token expiry). Token values are never
read into the payload.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

AGENT_VERSION = "0.1.0"
DEFAULT_ENV_FILE = "~/.config/ccfleet/agent.env"
DEFAULT_EGRESS_TARGETS = ("https://api.ipify.org", "https://ifconfig.me/ip",
                          "https://icanhazip.com", "https://checkip.amazonaws.com")
DEFAULT_RC_SERVICE = "claude-remote-control.service"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
RETRY_DELAYS_S = (2.0, 4.0, 8.0)
MAX_EGRESS_BYTES = 64

log = logging.getLogger("ccfleet-agent")

Runner = Callable[..., subprocess.CompletedProcess]
Opener = Callable[..., Any]


class AgentConfigError(ValueError):
    """Raised when the agent environment is incomplete."""


def load_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``export`` prefixes, quotes and comments are tolerated."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class AgentConfig:
    url: str
    node_id: str
    token: str
    claude_config_dir: Path
    rc_service: str = DEFAULT_RC_SERVICE
    egress_targets: tuple[str, ...] = DEFAULT_EGRESS_TARGETS
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AgentConfig:
        url = env.get("CCFLEET_URL", "").strip().rstrip("/")
        node_id = env.get("CCFLEET_NODE_ID", "").strip()
        token = env.get("CCFLEET_NODE_TOKEN", "").strip()
        if not url.startswith(("http://", "https://")):
            raise AgentConfigError("CCFLEET_URL must start with http:// or https://")
        if not node_id or not token:
            raise AgentConfigError("CCFLEET_NODE_ID and CCFLEET_NODE_TOKEN are required")
        targets = tuple(t.strip() for t in env.get("CCFLEET_EGRESS_TARGETS", "").split(",")
                        if t.strip()) or DEFAULT_EGRESS_TARGETS
        config_dir = (env.get("CCFLEET_CLAUDE_CONFIG_DIR") or env.get("CLAUDE_CONFIG_DIR")
                      or "~/.claude")
        try:
            timeout = float(env.get("CCFLEET_TIMEOUT_S", "10"))
        except ValueError as exc:
            raise AgentConfigError("CCFLEET_TIMEOUT_S must be a number") from exc
        return cls(url=url, node_id=node_id, token=token,
                   claude_config_dir=Path(config_dir).expanduser(),
                   rc_service=env.get("CCFLEET_RC_SERVICE", DEFAULT_RC_SERVICE),
                   egress_targets=targets, timeout_s=timeout)


# -- collectors ------------------------------------------------------------------


def _run(runner: Runner, argv: Sequence[str], timeout: float = 20.0) -> Optional[str]:
    try:
        proc = runner(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("command %s failed: %s", argv[0], exc.__class__.__name__)
        return None
    return (proc.stdout or "").strip()


# Where the official installer puts the CLI. systemd's default PATH does not
# include ~/.local/bin, so a timer-run agent would report claude as missing on a
# node where it is installed and working.
EXTRA_CLAUDE_PATHS = ("~/.local/bin/claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude")


def find_claude() -> Optional[str]:
    """Absolute path to the claude binary, searching PATH and the usual install sites."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in EXTRA_CLAUDE_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def claude_info(runner: Runner = subprocess.run) -> dict[str, Any]:
    path = find_claude()
    if not path:
        return {"version": None, "path": None}
    output = _run(runner, [path, "--version"])
    match = VERSION_RE.search(output or "")
    return {"version": match.group(0) if match else None, "path": path}


def oauth_account_facts(config_dir: Path) -> dict[str, Any]:
    """Non-secret facts from ~/.claude.json, which exists on every platform.

    This is the only way to say anything about a login on macOS, where Claude Code
    keeps the credential in the Keychain and there is no file to stat. Reading the
    Keychain secret would mean handling the token, which this agent never does;
    the account block beside it is not secret and carries what we actually need.

    Deliberately narrow: whether an account is signed in, when its profile was
    last fetched (which only succeeds while the login works, so it doubles as a
    liveness signal), and the rate-limit tier. No email, no name, no identifiers.
    """
    path = config_dir.with_suffix(".json")          # ~/.claude -> ~/.claude.json
    facts: dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return facts
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return facts
    facts["account"] = True
    fetched = account.get("profileFetchedAt")
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
        facts["profile_fetched_at"] = fetched
    tier = account.get("organizationRateLimitTier")
    if isinstance(tier, str):
        facts["plan"] = tier[:40]
    return facts


def credentials_summary(config_dir: Path) -> dict[str, Any]:
    """Facts about the credentials file that contain no secret material."""
    path = config_dir / ".credentials.json"
    account = oauth_account_facts(config_dir)
    if not path.exists():
        if platform.system() == "Darwin":
            # No file to stat, so presence and freshness come from the account block.
            summary: dict[str, Any] = {"present": bool(account.get("account")) or None,
                                       "store": "keychain"}
            summary.update({k: v for k, v in account.items() if k != "account"})
            return summary
        return {"present": False, "store": "file"}
    summary: dict[str, Any] = {"present": True, "store": "file",
                               "mtime": path.stat().st_mtime, "expires_at": None,
                               "subscription_type": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        summary["parse_error"] = True
        return summary
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict):
        expires = oauth.get("expiresAt")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            summary["expires_at"] = expires
        sub = oauth.get("subscriptionType")
        if isinstance(sub, str):
            summary["subscription_type"] = sub[:40]
    # Useful on Linux too: the file's mtime moves on any write, while this only
    # moves when a profile fetch succeeded against the live login.
    summary.update({k: v for k, v in account.items() if k != "account"})
    return summary


def _read_first_line(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline()
    except OSError:
        return None


def _meminfo_used_pct() -> Optional[float]:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            fields = dict(line.split(":", 1) for line in fh if ":" in line)
        total = float(fields["MemTotal"].split()[0])
        available = float(fields["MemAvailable"].split()[0])
    except (OSError, KeyError, ValueError, IndexError):
        return None
    return round((1 - available / total) * 100, 1) if total else None


def system_info() -> dict[str, Any]:
    uptime_line = _read_first_line("/proc/uptime")
    uptime = None
    if uptime_line:
        try:
            uptime = float(uptime_line.split()[0])
        except (ValueError, IndexError):
            uptime = None
    try:
        load1, load5, load15 = os.getloadavg()
        load = {"1": round(load1, 2), "5": round(load5, 2), "15": round(load15, 2)}
    except (OSError, AttributeError):
        load = {"1": None, "5": None, "15": None}
    return {"hostname": socket.gethostname()[:100], "uptime_s": uptime, "load": load,
            "mem": {"used_pct": _meminfo_used_pct()}}


def disk_info(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(path if path.exists() else Path.home()))
    except OSError:
        return {"used_pct": None, "free_gb": None}
    used_pct = round(usage.used / usage.total * 100, 1) if usage.total else None
    return {"used_pct": used_pct, "free_gb": round(usage.free / 1e9, 1)}


def egress_ip(targets: Sequence[str], opener: Opener = urllib.request.urlopen,
              timeout: float = 5.0) -> dict[str, Any]:
    """Ask several public echo services; the first well-formed answer wins."""
    for target in targets:
        req = urllib.request.Request(
            target, headers={"User-Agent": f"ccfleet-agent/{AGENT_VERSION}"})
        try:
            with opener(req, timeout=timeout) as resp:
                text = resp.read(MAX_EGRESS_BYTES).decode("utf-8", "replace").strip()
            ip = str(ipaddress.ip_address(text))
        except (urllib.error.URLError, OSError, ValueError):
            continue
        return {"ip": ip, "source": target.split("//", 1)[-1].split("/", 1)[0]}
    return {"ip": None, "source": None}


def remote_control_state(service: str, runner: Runner = subprocess.run) -> dict[str, Any]:
    if not shutil.which("systemctl"):
        return {"state": "unknown"}
    output = _run(runner, ["systemctl", "--user", "is-active", service], timeout=10)
    return {"state": (output or "unknown").splitlines()[0][:40] if output else "unknown"}


def tmux_sessions(runner: Runner = subprocess.run) -> Optional[int]:
    if not shutil.which("tmux"):
        return None
    output = _run(runner, ["tmux", "ls"], timeout=10)
    if not output or "no server running" in output:
        return 0
    return len([line for line in output.splitlines() if line.strip()])


def build_payload(cfg: AgentConfig, runner: Runner = subprocess.run,
                  opener: Opener = urllib.request.urlopen,
                  now: Callable[[], float] = time.time) -> dict[str, Any]:
    payload: dict[str, Any] = {"node_id": cfg.node_id, "ts": now(),
                               "agent_version": AGENT_VERSION}
    payload.update(system_info())
    payload["claude"] = claude_info(runner)
    payload["credentials"] = credentials_summary(cfg.claude_config_dir)
    payload["disk"] = disk_info(cfg.claude_config_dir)
    payload["egress"] = egress_ip(cfg.egress_targets, opener, min(cfg.timeout_s, 5.0))
    payload["remote_control"] = remote_control_state(cfg.rc_service, runner)
    payload["tmux_sessions"] = tmux_sessions(runner)
    return payload


# -- transport -------------------------------------------------------------------


def send_heartbeat(cfg: AgentConfig, payload: Mapping[str, Any],
                   opener: Opener = urllib.request.urlopen,
                   sleep: Callable[[float], None] = time.sleep) -> tuple[int, str]:
    """POST the payload. Retries network errors and 5xx with backoff; never retries 4xx."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(f"{cfg.url}/api/heartbeat", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {cfg.token}",
                                          "User-Agent": f"ccfleet-agent/{AGENT_VERSION}"})
    attempts = len(RETRY_DELAYS_S) + 1
    for attempt in range(attempts):
        try:
            with opener(req, timeout=cfg.timeout_s) as resp:
                return int(getattr(resp, "status", 200)), resp.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = exc.read(4096).decode("utf-8", "replace") if hasattr(exc, "read") else ""
            if exc.code < 500:
                return exc.code, text
            log.warning("server error %s on attempt %d", exc.code, attempt + 1)
        except (urllib.error.URLError, OSError) as exc:
            log.warning("heartbeat attempt %d failed: %s", attempt + 1, exc.__class__.__name__)
        if attempt < attempts - 1:
            sleep(RETRY_DELAYS_S[attempt])
    return 0, "unreachable"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ccfleet-agent",
                                     description="Post one heartbeat to the ccfleet server.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the payload instead of sending it")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    env = {**load_env_file(Path(args.env_file).expanduser()), **os.environ}
    try:
        cfg = AgentConfig.from_env(env)
    except AgentConfigError as exc:
        print(f"error: {exc} (env file: {args.env_file})", file=sys.stderr)
        return 2
    payload = build_payload(cfg)
    if args.print_only:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    status, text = send_heartbeat(cfg, payload)
    if status == 200:
        log.info("heartbeat accepted: %s", text.strip()[:200])
        return 0
    log.error("heartbeat rejected: status=%s body=%s", status, text.strip()[:200])
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
