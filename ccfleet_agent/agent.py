#!/usr/bin/env python3
"""ccfleet node agent: posts one heartbeat about this node to the fleet server.

Standard library only. Runs as the node owner's user and reads nothing but public
facts about the machine, plus a narrow set of non-secret fields from two Claude
Code files:

  ~/.claude/.credentials.json   modification time, access-token expiry, plan type
  ~/.claude.json                whether an account is signed in, when its profile
                                was last fetched, and the rate-limit tier

The second is what makes a Mac reportable at all, since the credential itself
lives in the Keychain there and this agent will not read it. That file also holds
an email address, a full name, an account uuid and an organisation name; none of
them are collected. Token values are never read into the payload, and the tests
assert both of those.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import platform
import re
import shlex
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
# The agent is one-shot under a timer, so anything it learns after posting has
# to survive on disk to be reported on the next beat.
DEFAULT_STATE_PATH = "~/.config/ccfleet/reconcile.json"
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
    state_path: Path = Path(DEFAULT_STATE_PATH)

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
                   egress_targets=targets, timeout_s=timeout,
                   state_path=Path(env.get("CCFLEET_STATE_FILE")
                                   or DEFAULT_STATE_PATH).expanduser())


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


# `claude auth status` answers the one question the filesystem cannot: is this
# login actually usable? It also returns the owner's email address, organisation
# name and organisation id, none of which this agent will report. Only these four
# keys are read; the rest are never copied out of the parsed object.
AUTH_STATUS_FIELDS = (("loggedIn", "logged_in"), ("authMethod", "auth_method"),
                      ("apiProvider", "api_provider"), ("subscriptionType", "subscription_type"))


def auth_status(runner: Runner = subprocess.run) -> dict[str, Any]:
    """Whether the node is signed in, straight from the CLI rather than inferred.

    Returns {} when the CLI is absent or says anything this cannot parse, so a
    caller can tell "not signed in" apart from "could not ask".
    """
    path = find_claude()
    if not path:
        return {}
    output = _run(runner, [path, "auth", "status"], timeout=30.0)
    if not output:
        return {}
    try:
        data = json.loads(output)
    except ValueError:
        return {}
    if not isinstance(data, Mapping):
        return {}
    out: dict[str, Any] = {}
    for source, name in AUTH_STATUS_FIELDS:
        value = data.get(source)
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, str) and value:
            out[name] = value[:40]
    return out


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
    # Append, never with_suffix: that REPLACES an existing suffix, so a config dir
    # named "claude.work" would silently read "claude.json" instead.
    path = config_dir.parent / (config_dir.name + ".json")   # ~/.claude -> ~/.claude.json
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
                  now: Callable[[], float] = time.time,
                  state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"node_id": cfg.node_id, "ts": now(),
                               "agent_version": AGENT_VERSION}
    payload.update(system_info())
    payload["claude"] = claude_info(runner)
    credentials = credentials_summary(cfg.claude_config_dir)
    # The CLI's own answer wins over anything inferred from a file's existence:
    # a present file can still be a dead login, and on macOS there is no file.
    status = auth_status(runner)
    if status:
        credentials.update(status)
        credentials["present"] = status.get("logged_in", credentials.get("present"))
    payload["credentials"] = credentials
    payload["disk"] = disk_info(cfg.claude_config_dir)
    payload["egress"] = egress_ip(cfg.egress_targets, opener, min(cfg.timeout_s, 5.0))
    payload["remote_control"] = remote_control_state(cfg.rc_service, runner)
    payload["tmux_sessions"] = tmux_sessions(runner)
    payload["usage"] = usage_summary(cfg.claude_config_dir)
    upgrade = (state or {}).get("upgrade")
    if isinstance(upgrade, Mapping):
        payload["reconcile"] = {"upgrade": dict(upgrade)}
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


# -- usage ----------------------------------------------------------------------
#
# Claude Code writes a JSONL transcript per session under the config directory,
# and each assistant record carries that turn's token counts. Reading those files
# is how this reports usage: they are local files the CLI wrote on its own
# machine. It is NOT the OAuth usage endpoint, which would mean using the owner's
# token outside Claude Code — see the project's compliance notes.
#
# What this can say: how much this node has consumed. What it cannot say: how
# much of a subscription window is left. That lives only behind /usage inside a
# session, and this deliberately does not go looking for it.

USAGE_WINDOW_DAYS = 14
# A busy node accumulates a lot of transcript. These bounds keep a five-minute
# heartbeat from turning into a filesystem scan.
USAGE_MAX_FILES = 400
USAGE_MAX_BYTES_PER_FILE = 8 * 1024 * 1024
USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens")


def _usage_files(root: Path, since: float) -> list[Path]:
    """Transcripts touched inside the window, newest first and capped."""
    try:
        found = [p for p in root.glob("**/*.jsonl") if p.is_file()]
    except OSError:
        return []
    fresh = []
    for path in found:
        try:
            if path.stat().st_mtime >= since:
                fresh.append((path.stat().st_mtime, path))
        except OSError:
            continue
    fresh.sort(reverse=True)
    return [p for _, p in fresh[:USAGE_MAX_FILES]]


def usage_summary(config_dir: Path, now: Optional[float] = None,
                  window_days: int = USAGE_WINDOW_DAYS) -> dict[str, Any]:
    """Token counts per day from local transcripts. Never reads message content."""
    now = time.time() if now is None else now
    since = now - window_days * 86400
    root = config_dir / "projects"
    oldest_day = time.strftime("%Y-%m-%d", time.gmtime(since))
    totals: dict[str, int] = {k: 0 for k in USAGE_TOKEN_KEYS}
    by_day: dict[str, int] = {}
    models: set[str] = set()
    sessions = 0

    for path in _usage_files(root, since):
        counted = False
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                read = 0
                for line in fh:
                    read += len(line)
                    if read > USAGE_MAX_BYTES_PER_FILE:
                        break
                    if not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    message = record.get("message")
                    message = message if isinstance(message, Mapping) else {}
                    usage = message.get("usage") or record.get("usage")
                    if not isinstance(usage, Mapping):
                        continue
                    stamp = _usage_day(record.get("timestamp"))
                    if not _in_window(stamp, oldest_day):
                        continue
                    counted = True
                    turn = 0
                    for key in USAGE_TOKEN_KEYS:
                        value = usage.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                            totals[key] += value
                            turn += value
                    if turn:
                        by_day[stamp] = by_day.get(stamp, 0) + turn
                    model = message.get("model")
                    if isinstance(model, str) and model:
                        models.add(model[:40])
        except OSError:
            continue
        if counted:
            sessions += 1

    return {
        "window_days": window_days,
        "sessions": sessions,
        "total_tokens": sum(totals.values()),
        "models": sorted(models)[:6],
        # Oldest to newest, so a sparkline can be drawn straight from it.
        "by_day": [{"day": d, "tokens": by_day[d]} for d in sorted(by_day)][-window_days:],
        **totals,
    }


def _usage_day(raw: Any) -> Optional[str]:
    """The YYYY-MM-DD an ISO timestamp falls on, or None."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    day = raw[:10]
    return day if day[4] == "-" and day[7] == "-" and day[:4].isdigit() else None


def _in_window(day: Optional[str], oldest_day: str) -> bool:
    """Is this record's own day inside the window?

    Selecting files by modification time is not enough: one long-lived session
    transcript touched today carries records from weeks ago, so a "last 14 days"
    total would quietly include them. Each record is judged on its own date, and
    a record whose date cannot be read is not counted — an unplaceable number is
    worse than a missing one in a figure that claims a window.
    """
    return day is not None and day >= oldest_day


# -- reconcile -------------------------------------------------------------------

# An install downloads a release, so it gets far longer than a fact-collecting
# probe. Still bounded: a hung installer must not wedge the timer unit.
INSTALL_TIMEOUT_S = 300.0
# A failing install is usually a bad release or a full disk, and retrying every
# five minutes fixes neither while burning the node's bandwidth. Back off an hour.
INSTALL_RETRY_AFTER_S = 3600.0
# A channel cannot be compared against an installed number, so left alone it would
# reinstall on every beat. Re-check it on a schedule instead: often enough to pick
# up a release, rarely enough that the installer is not run 288 times a day.
CHANNEL_RECHECK_AFTER_S = 24 * 3600.0
VERSION_CHANNELS = ("stable", "latest")


def read_state(path: Path) -> dict[str, Any]:
    """Last run's notes. A missing or corrupt file is simply no notes."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        log.debug("could not write state: %s", exc.__class__.__name__)


def parse_desired(body: str) -> dict[str, Any]:
    """The desired block from a heartbeat response, or {} when there isn't one."""
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    desired = data.get("desired") if isinstance(data, Mapping) else None
    return dict(desired) if isinstance(desired, Mapping) else {}


def installable_version(target: Any) -> Optional[str]:
    """Re-check the server's version string before it reaches a subprocess.

    argv is a list, so there is no shell to inject into. This is about argument
    choice rather than quoting: a server that has been tampered with should not
    get to hand the installer an arbitrary flag.
    """
    if not isinstance(target, str):
        return None
    target = target.strip()
    if not target or len(target) > 40:
        return None
    if target in VERSION_CHANNELS:
        return target
    if target[0].isdigit() and all(c.isalnum() or c in ".-+" for c in target):
        return target
    return None


def _channel_is_current(state: Mapping[str, Any], target: str,
                        installed: Optional[str], now: float) -> bool:
    """True when a channel was resolved recently and still holds.

    Without this a channel reinstalls on every heartbeat: there is no number to
    compare it against, so "different from desired" is always true.
    """
    channel = state.get("channel")
    if not isinstance(channel, Mapping):
        return False
    if channel.get("target") != target:
        return False            # they switched channels; resolve again
    if channel.get("resolved") != installed:
        return False            # something else moved the binary; resolve again
    ts = channel.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    return now - ts < CHANNEL_RECHECK_AFTER_S


# -- console-driven sign-in ------------------------------------------------------
#
# The owner clicks "Sign in" in the console; this runs the real `claude auth
# login` here, on the node, and carries only the verification URL back and the
# code forward. The credential is written by the CLI into the owner's own home
# directory and never leaves the machine — the server sees a URL and a state
# name, and is deleted from even that as soon as the login finishes.

# Its own tmux server, so a sign-in can never disturb (or be disturbed by) the
# session the owner is working in. Same reasoning as the Remote Control socket.
LOGIN_TMUX_SOCKET = "ccfleet-login"
LOGIN_SESSION = "login"
# Anthropic's verification URL. Matched rather than assumed so a prompt change
# that stops printing one is reported as a failure instead of hanging forever.
LOGIN_URL_RE = re.compile(r"https://\S*claude\.(?:com|ai)/\S+")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def _tmux(runner: Runner, *args: str, timeout: float = 10.0) -> Optional[str]:
    return _run(runner, ["tmux", "-L", LOGIN_TMUX_SOCKET, *args], timeout=timeout)


def _tmux_ok(runner: Runner, *args: str, timeout: float = 10.0) -> bool:
    """Did the command actually succeed? `_run` returns output, not a verdict,
    so a tmux that exited non-zero would otherwise read as success."""
    try:
        proc = runner(["tmux", "-L", LOGIN_TMUX_SOCKET, *args], capture_output=True,
                      text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("tmux %s failed: %s", args[0] if args else "", exc.__class__.__name__)
        return False
    return proc.returncode == 0


def login_email(raw: Any) -> Optional[str]:
    """The address to pre-fill, if it is one. This reaches argv, so it is checked."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    return candidate if EMAIL_RE.match(candidate) else None


def start_login(email: Optional[str], runner: Runner = subprocess.run) -> bool:
    """Open a fresh pane running `claude auth login`. True if it started."""
    path = find_claude()
    if not path:
        return False
    _tmux(runner, "kill-session", "-t", LOGIN_SESSION)
    argv = [path, "auth", "login", "--claudeai"]
    if email:
        argv += ["--email", email]
    # -d so nothing needs a terminal; the pane is driven and read by tmux alone.
    return _tmux_ok(runner, "new-session", "-d", "-s", LOGIN_SESSION,
                    "-x", "200", "-y", "50", " ".join(shlex.quote(a) for a in argv))


def read_login_pane(runner: Runner = subprocess.run) -> str:
    """Read the pane with wrapped lines joined.

    Measured on a live sign-in: the verification URL is ~496 characters and wraps
    across several rows. Without -J, capture-pane returns each row separately and
    the URL arrives truncated to its first 166 characters — long enough to look
    like a URL and useless to click.
    """
    return _tmux(runner, "capture-pane", "-p", "-J", "-t", LOGIN_SESSION) or ""


def find_login_url(pane: str) -> Optional[str]:
    match = LOGIN_URL_RE.search(pane)
    if not match:
        return None
    # tmux wraps long lines; strip anything a wrap or a quote left attached.
    return match.group(0).rstrip('"\'),.').strip()


def send_login_code(code: str, runner: Runner = subprocess.run) -> None:
    """Type the code into the waiting prompt. Never logged."""
    _tmux(runner, "send-keys", "-t", LOGIN_SESSION, code, "Enter")


def end_login(runner: Runner = subprocess.run) -> None:
    _tmux(runner, "kill-session", "-t", LOGIN_SESSION)


# How long the agent will stay resident driving one sign-in. Someone is watching
# the console, so it polls fast; but an abandoned attempt must not pin a process
# on the node forever.
LOGIN_WINDOW_S = 300.0
LOGIN_POLL_MIN_S = 1.0
LOGIN_POLL_MAX_S = 30.0


def reconcile_login(desired: Mapping[str, Any], state: Mapping[str, Any],
                    runner: Runner = subprocess.run) -> tuple[Optional[dict[str, Any]],
                                                              dict[str, Any]]:
    """Drive one step of a console-requested sign-in.

    Returns (progress to report, new state). Progress is None when there is
    nothing new to say, which keeps a fast poll from restating the same thing.
    """
    new_state = dict(state)
    wanted = desired.get("login")
    mine = state.get("login") if isinstance(state.get("login"), Mapping) else {}

    if not isinstance(wanted, Mapping):
        # Cancelled, finished, or never asked for. Tidy up if we had one open.
        if mine:
            end_login(runner)
            new_state.pop("login", None)
        return None, new_state

    requested_at = wanted.get("requested_at")
    if mine.get("requested_at") != requested_at:
        # A new request supersedes anything in flight, including a stuck one.
        if not start_login(login_email(wanted.get("email")), runner):
            new_state["login"] = {"requested_at": requested_at, "phase": "failed"}
            return {"state": "failed", "detail": "claude not found on this node",
                    "requested_at": requested_at}, new_state
        new_state["login"] = {"requested_at": requested_at, "phase": "started"}
        return {"state": "requested", "requested_at": requested_at}, new_state

    phase = mine.get("phase")

    if phase == "started":
        url = find_login_url(read_login_pane(runner))
        if not url:
            return None, new_state          # still printing; try again next poll
        new_state["login"] = {**mine, "phase": "url_ready", "url": url}
        return {"state": "url_ready", "url": url, "requested_at": requested_at}, new_state

    if phase == "url_ready":
        code = wanted.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, new_state          # waiting for someone to paste one
        send_login_code(code.strip(), runner)
        new_state["login"] = {**mine, "phase": "code_sent"}
        return {"state": "code_sent", "requested_at": requested_at}, new_state

    if phase == "code_sent":
        # The CLI is the judge of whether the sign-in worked, not the pane text.
        if auth_status(runner).get("logged_in") is True:
            end_login(runner)
            new_state.pop("login", None)
            return {"state": "done", "requested_at": requested_at}, new_state
        pane = read_login_pane(runner)
        if re.search(r"(?i)\b(invalid|expired|failed|error)\b", pane):
            end_login(runner)
            new_state.pop("login", None)
            return {"state": "failed", "detail": "the code was not accepted",
                    "requested_at": requested_at}, new_state
        return None, new_state              # still working

    return None, new_state


def prune_state(state: Mapping[str, Any], desired: Mapping[str, Any],
                installed: Optional[str]) -> dict[str, Any]:
    """Drop a recorded upgrade that no longer describes anything.

    Without this a failure sticks: unpin the node, pin something already
    installed, or fix it by hand, and the dashboard keeps reporting a failed
    upgrade that stopped being true days ago.
    """
    pruned = dict(state)
    upgrade = pruned.get("upgrade")
    if not isinstance(upgrade, Mapping):
        return pruned
    target = installable_version(desired.get("claude_version"))
    obsolete = (
        target is None                       # the pin is gone or unusable
        or upgrade.get("to") != target       # it was about a different target
        or (target not in VERSION_CHANNELS and installed == target)  # satisfied since
    )
    if obsolete:
        pruned.pop("upgrade", None)
    return pruned


def reconcile_version(desired: Mapping[str, Any], installed: Optional[str],
                      state: Mapping[str, Any], runner: Runner = subprocess.run,
                      now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Bring the CLI to the pinned version. Returns a result to report, or None.

    None means nothing was attempted: no pin, already matching, claude not found,
    or still inside the back-off after a failure. Only a real attempt reports.
    """
    now = time.time() if now is None else now
    target = installable_version(desired.get("claude_version"))
    if target is None:
        return None
    # An exact pin can be compared, and matching means there is nothing to do.
    if target not in VERSION_CHANNELS and installed == target:
        return None
    # A channel has no number to compare, so it is governed by time instead. Skip
    # while the last resolution still holds: same channel, the version it resolved
    # to is still what is installed, and the re-check window has not elapsed.
    if target in VERSION_CHANNELS and _channel_is_current(state, target, installed, now):
        return None
    path = find_claude()
    if not path:
        return None

    last = state.get("upgrade")
    last = last if isinstance(last, Mapping) else {}
    if (last.get("ok") is False and last.get("to") == target
            and isinstance(last.get("ts"), (int, float))
            and now - last["ts"] < INSTALL_RETRY_AFTER_S):
        log.debug("not retrying install of %s yet: backing off after a failure", target)
        return None

    log.info("installing claude %s (installed: %s)", target, installed)
    try:
        proc = runner([path, "install", target], capture_output=True, text=True,
                      timeout=INSTALL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"from": installed, "to": target, "ok": False, "ts": now,
                "error": exc.__class__.__name__}
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return {"from": installed, "to": target, "ok": False, "ts": now,
                "error": detail[:200] or f"exit {proc.returncode}"}
    # Report what is on disk now rather than what was asked for: a channel resolves
    # to a number, and an installer can succeed without changing anything.
    landed = claude_info(runner).get("version") or target
    result = {"from": installed, "to": landed, "ok": True, "ts": now, "error": None}
    if target in VERSION_CHANNELS:
        # Remember what the channel resolved to, so the next beat can tell that
        # this channel is already satisfied instead of installing it again.
        result["channel"] = {"target": target, "resolved": landed, "ts": now}
    return result


def run_cycle(cfg: AgentConfig, state: Mapping[str, Any],
              login_progress: Optional[Mapping[str, Any]] = None,
              reconcile: bool = True) -> tuple[int, dict[str, Any], dict[str, Any],
                                               Optional[dict[str, Any]]]:
    """One post, and whatever acting on the reply calls for.

    Returns (status, desired, new state, login progress to report next time).
    """
    payload = build_payload(cfg, state=state)
    if login_progress:
        payload.setdefault("reconcile", {})["login"] = dict(login_progress)
    status, text = send_heartbeat(cfg, payload)
    if status != 200:
        log.error("heartbeat rejected: status=%s body=%s", status, text.strip()[:200])
        return status, {}, dict(state), login_progress
    log.info("heartbeat accepted: %s", text.strip()[:200])
    if not reconcile:
        return status, {}, dict(state), None

    desired = parse_desired(text)
    installed = (payload.get("claude") or {}).get("version")
    progress, state = reconcile_login(desired, state)
    state = prune_state(state, desired, installed)
    result = reconcile_version(desired, installed, state)
    if result is not None:
        # The channel note is local bookkeeping, not something the server asked
        # for, so it is filed separately and never reported.
        channel = result.pop("channel", None)
        state = {**state, "upgrade": result}
        if channel is not None:
            state["channel"] = channel
    write_state(cfg.state_path, state)
    return status, desired, state, progress


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ccfleet-agent",
                                     description="Post one heartbeat to the ccfleet server.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the payload instead of sending it")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="report facts but never act on the server's desired state")
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
    state = read_state(cfg.state_path)
    if args.print_only:
        print(json.dumps(build_payload(cfg, state=state), indent=2, sort_keys=True))
        return 0

    status, desired, state, progress = run_cycle(cfg, state,
                                                 reconcile=not args.no_reconcile)
    if status != 200:
        return 1

    # Normally that is the whole run: one post, then exit and let the timer bring
    # us back. A sign-in is the exception — someone is watching the console for a
    # URL, and five minutes of dead air is not a sign-in flow. So stay resident,
    # but only while the server says a login is in flight and only for a bounded
    # window; an abandoned attempt must not pin a process on the node.
    deadline = time.monotonic() + LOGIN_WINDOW_S
    while desired.get("login") and time.monotonic() < deadline:
        delay = desired.get("poll_s")
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            delay = LOGIN_POLL_MAX_S
        time.sleep(max(LOGIN_POLL_MIN_S, min(float(delay), LOGIN_POLL_MAX_S)))
        status, desired, state, progress = run_cycle(cfg, state, progress)
        if status != 200:
            return 1
    if desired.get("login"):
        # Leaving it running would make every later run resident for another
        # window, forever, and leave a pane open on the node. Abandon it here and
        # tell the server, so the row goes away instead of being re-offered.
        log.warning("sign-in unfinished after %.0fs; abandoning it", LOGIN_WINDOW_S)
        end_login()
        state = {k: v for k, v in state.items() if k != "login"}
        write_state(cfg.state_path, state)
        abandoned = desired.get("login") or {}
        run_cycle(cfg, state, {"state": "failed",
                               "detail": f"not completed within {int(LOGIN_WINDOW_S)}s",
                               "requested_at": abandoned.get("requested_at")})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
