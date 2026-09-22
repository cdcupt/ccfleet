#!/usr/bin/env python3
"""ccfleet node agent: posts one heartbeat about this node to the fleet server.

Standard library only. Runs as the node owner's user. What it opens, and what it
takes from each:

  ~/.claude/.credentials.json   modification time, access-token expiry, plan type
  ~/.claude.json                whether an account is signed in, when its profile
                                was last fetched, and the rate-limit tier
  claude auth status            the CLI's own answer on whether the login works
  ~/.claude/projects/**.jsonl   session transcripts, for per-turn token counts

Be exact about the last one, because it is the sensitive one. Those transcripts
are the owner's conversations, and parsing a record decodes the whole of it,
content included. What leaves the node is token counts and per-hour totals —
nothing else. Conversation content is never copied out of
the parsed record, never stored, never logged and never sent. See usage_summary.

`~/.claude.json` likewise holds an email address, a full name, an account uuid
and an organisation name, and `claude auth status` returns an email address and
an organisation; none of them are collected. It is also what makes a Mac
reportable at all, since the credential there lives in the Keychain and this
agent will not read it. Token values never reach the payload. Tests assert each
of these, including with a secret written into a fixture transcript.
"""

from __future__ import annotations

import argparse
import fcntl
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
from datetime import datetime, timezone
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
                  state: Optional[Mapping[str, Any]] = None,
                  quota: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
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
    if quota:
        payload["quota"] = dict(quota)
    upgrade = (state or {}).get("upgrade")
    if isinstance(upgrade, Mapping):
        payload["reconcile"] = {"upgrade": dict(upgrade)}
    return payload


# -- transport -------------------------------------------------------------------


# The reply to an owner node is a few hundred bytes. A shared machine's names
# every one of its slots, so it passes its own, larger, bound.
MAX_REPLY_BYTES = 4096


def send_heartbeat(cfg: AgentConfig, payload: Mapping[str, Any],
                   opener: Opener = urllib.request.urlopen,
                   sleep: Callable[[float], None] = time.sleep,
                   max_reply: int = MAX_REPLY_BYTES) -> tuple[int, str]:
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
                return (int(getattr(resp, "status", 200)),
                        resp.read(max_reply).decode("utf-8", "replace"))
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
# Be precise about the privacy claim. Parsing a record decodes the whole of it,
# conversation content included, so it IS read into memory here. What is
# guaranteed is narrower and still worth having: only token counts and
# per-hour totals are retained or reported. The content is never copied
# out of the parsed record, never stored, never logged and never sent.
#
# What this can say: how much this node has consumed. What it cannot say: how
# much of a subscription window is left. That lives only behind /usage inside a
# session, and this deliberately does not go looking for it.

# A rolling week, counted in whole hours ending with the current one: the same
# span as the weekly quota window, and it moves through the day rather than
# jumping at midnight.
USAGE_WINDOW_HOURS = 7 * 24
# A busy node accumulates a lot of transcript. These bounds keep a five-minute
# heartbeat from turning into a filesystem scan.
USAGE_MAX_FILES = 200
USAGE_MAX_BYTES_PER_FILE = 4 * 1024 * 1024
# The real guard is the total, not the per-file cap: 200 files at 4 MiB each
# would be 800 MiB of reading on every five-minute heartbeat. Files are taken
# newest first, so exhausting this budget loses the oldest data in the window
# rather than the most recent.
USAGE_MAX_BYTES_TOTAL = 32 * 1024 * 1024
# Enumerating every transcript is itself the unbounded part: the file cap only
# applies after the walk. Stop walking at this many paths.
USAGE_MAX_SCAN = 5000
# A single transcript line can be enormous — one pasted file or tool result. It
# is read in chunks and abandoned past this, so no one record can pull an
# unbounded amount into memory during a heartbeat.
USAGE_MAX_LINE = 1024 * 1024
USAGE_CHUNK = 64 * 1024
USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens")


def _usage_files(root: Path, since: float) -> list[Path]:
    """Transcripts touched inside the window, newest first and capped."""
    fresh = []
    seen = 0
    try:
        for path in root.glob("**/*.jsonl"):
            seen += 1
            if seen > USAGE_MAX_SCAN:
                log.debug("stopped walking transcripts at %d paths", USAGE_MAX_SCAN)
                break
            try:
                # One stat, not two: this runs over every transcript on the node.
                info = path.stat()
            except OSError:
                continue
            if info.st_mtime >= since:
                fresh.append((info.st_mtime, path))
    except OSError:
        return []
    fresh.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in fresh[:USAGE_MAX_FILES]]


def _bounded_lines(fh: Any, limit: int) -> Any:
    """Yield ``(line, bytes_read)`` pairs, bounding any single line.

    `for line in fh` materialises a whole line before anything can measure it, so
    one oversized record would defeat every byte cap below it. Reading in chunks
    keeps the ceiling real; an over-long line is dropped rather than buffered.

    The byte count is what was *read*, not what was yielded, and it is reported
    even for lines that are discarded. Charging only the yielded lines would let
    a file full of oversized records consume its whole allowance for free, which
    is exactly the read the global budget exists to prevent.
    """
    buf = ""
    spent = 0
    while spent < limit:
        chunk = fh.read(USAGE_CHUNK)
        if not chunk:
            break
        spent += len(chunk)
        buf += chunk
        charge = len(chunk)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if len(line) <= USAGE_MAX_LINE:
                yield line, charge
                charge = 0
        if len(buf) > USAGE_MAX_LINE:
            buf = ""          # an unterminated monster; abandon it
        if charge:
            # Read but nothing surfaced from it — still charged.
            yield None, charge
    if buf and len(buf) <= USAGE_MAX_LINE:
        yield buf, 0


def _seek_to_tail(fh: Any, path: Path) -> int:
    """For an oversized transcript, start near its end rather than its start.

    Transcripts are append-only, so the newest records are last. Reading the
    first N bytes of a large one skips exactly the recent usage this is meant to
    report — the cap would silently invert the answer rather than trim it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= USAGE_MAX_BYTES_PER_FILE:
        return 0
    try:
        fh.seek(size - USAGE_MAX_BYTES_PER_FILE)
        # The seek lands mid-line; that partial line is discarded. Read it in
        # chunks rather than with readline(), which is unbounded: an enormous
        # unterminated record would otherwise be materialised whole, defeating
        # the cap this function exists to apply. Charged to the budget either way.
        skipped = 0
        while skipped <= USAGE_MAX_LINE:
            chunk = fh.read(USAGE_CHUNK)
            if not chunk:
                break
            skipped += len(chunk)
            newline = chunk.find("\n")
            if newline != -1:
                # Step back to just after that newline so reading resumes clean.
                fh.seek(fh.tell() - (len(chunk) - newline - 1))
                break
        return skipped
    except (OSError, ValueError):
        try:
            fh.seek(0)
        except OSError:
            pass
        return 0


def usage_summary(config_dir: Path, now: Optional[float] = None,
                  window_hours: int = USAGE_WINDOW_HOURS) -> dict[str, Any]:
    """Token counts per hour, over a rolling window, from local transcripts.

    Parsing a record decodes conversation content along with everything else;
    what this guarantees is that nothing but counts is kept or returned.
    """
    now = time.time() if now is None else now
    # Whole hours, the last of them the current one, so the first bucket and
    # the file cutoff agree about where the window starts.
    this_hour = now - (now % 3600)
    since = this_hour - (window_hours - 1) * 3600
    until = this_hour + 3600
    root = config_dir / "projects"
    budget = USAGE_MAX_BYTES_TOTAL
    totals: dict[str, int] = {k: 0 for k in USAGE_TOKEN_KEYS}
    hours = [0] * window_hours
    sessions = 0

    for path in _usage_files(root, since):
        if budget <= 0:
            break
        counted = False
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                budget -= _seek_to_tail(fh, path)
                read = 0
                allowance = min(USAGE_MAX_BYTES_PER_FILE, max(budget, 0))
                for line, consumed in _bounded_lines(fh, allowance):
                    read += consumed
                    budget -= consumed
                    if read > USAGE_MAX_BYTES_PER_FILE or budget <= 0:
                        break
                    if line is None:
                        continue
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
                    stamp = _usage_epoch(record.get("timestamp"))
                    if stamp is None or not since <= stamp < until:
                        continue
                    counted = True
                    turn = 0
                    for key in USAGE_TOKEN_KEYS:
                        value = usage.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                            totals[key] += value
                            turn += value
                    hours[int((stamp - since) // 3600)] += turn
        except OSError:
            continue
        if counted:
            sessions += 1

    return {
        "window_hours": window_hours,
        # What a console predating the hourly series labels the total with.
        "window_days": window_hours // 24,
        "sessions": sessions,
        "total_tokens": sum(totals.values()),
        # Oldest hour first, so a chart can be drawn straight from it; `start`
        # is when the first hour began. A compact list rather than one record
        # per hour: a week of them is 168 numbers on every heartbeat.
        "by_hour": {"start": since, "tokens": hours},
        **totals,
    }


def _usage_epoch(raw: Any) -> Optional[float]:
    """When a transcript record was written, in seconds, or None.

    Selecting files by modification time is not enough: one long-lived session
    transcript touched today carries records from weeks ago, so a "last week"
    total would quietly include them. Each record is judged on its own time,
    and a record whose time cannot be read is not counted — an unplaceable
    number is worse than a missing one in a figure that claims a window.
    """
    if not isinstance(raw, str) or len(raw) < 19:
        return None
    try:
        stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)   # Claude Code writes UTC
    return stamp.timestamp()


# -- quota windows ---------------------------------------------------------------
#
# How much of the subscription window is left lives behind `/usage` inside a
# Claude Code session and nowhere else. This drives the real CLI in its own tmux
# server and reads what it prints — the same shape as the sign-in flow. It is
# NOT the OAuth usage endpoint, which would mean using the owner's token outside
# Claude Code; here Claude Code is the one reporting on itself.
#
# Starting a session is heavy compared with a heartbeat, so this runs on its own
# slow schedule and the answer is cached in the agent's state between runs.

QUOTA_TMUX_SOCKET = "ccfleet-quota"
QUOTA_SESSION = "quota"
QUOTA_REFRESH_S = 30 * 60
QUOTA_TIMEOUT_S = 90.0
# Labels /usage prints, mapped to the names reported. "Current session" is the
# five-hour window; the weekly ones reset together.
QUOTA_LABELS = (
    ("Current session", "session"),
    ("Current week (all models)", "week"),
)
PERCENT_RE = re.compile(r"(\d{1,3})%\s+used")
RESETS_RE = re.compile(r"Resets\s+([^\n]{1,40})")
# /usage draws each window as a label line, a bar, then a reset line. Reading
# further than that lets a label whose own block has not been painted yet borrow
# the number from the block below it, and the screen is captured mid-paint as a
# matter of course.
QUOTA_BLOCK_LINES = 3


def _quota_tmux(runner: Runner, *args: str, timeout: float = 15.0) -> Optional[str]:
    return _run(runner, ["tmux", "-L", QUOTA_TMUX_SOCKET, *args], timeout=timeout)


def _quota_home() -> Optional[str]:
    """The one directory this is willing to open a trusted session in."""
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return None
    return str(home) if home.is_dir() else None


def parse_quota(pane: str) -> dict[str, Any]:
    """Pull the windows out of what /usage drew.

    Each block is a label line, then a bar ending "N% used", then "Resets ...".
    Matching forward from the label keeps this working when the bar characters
    or the column width change.
    """
    out: dict[str, Any] = {}
    lines = pane.splitlines()
    for label, name in QUOTA_LABELS:
        at = next((i for i, line in enumerate(lines) if label in line), -1)
        if at < 0:
            continue
        # The block is the few lines under its own label and nothing beyond, so
        # a half-drawn window reads as absent rather than as the next one's
        # figure. Another known label ends it early whatever the line count.
        block = []
        for line in lines[at + 1:at + 1 + QUOTA_BLOCK_LINES]:
            if any(other in line for other, _ in QUOTA_LABELS):
                break
            block.append(line)
        window = "\n".join(block)
        percent = PERCENT_RE.search(window)
        if not percent:
            continue
        value = int(percent.group(1))
        if not 0 <= value <= 100:
            continue
        entry: dict[str, Any] = {"used_pct": value}
        resets = RESETS_RE.search(window)
        if resets:
            entry["resets"] = " ".join(resets.group(1).split())[:40]
        out[name] = entry
    return out


def read_quota(runner: Runner = subprocess.run,
               now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Drive `claude` to its /usage screen once and read the windows off it."""
    path = find_claude()
    if not path:
        return None
    # Start it in the owner's home and nowhere else. The loop below answers
    # Claude Code's folder-trust prompt, and answering it means trusting whatever
    # directory this happened to start in — a checked-out project, if someone ran
    # the agent by hand from one. Pinning the directory is what makes that answer
    # safe, rather than assuming the service was launched somewhere harmless.
    home = _quota_home()
    if home is None:
        return None
    _quota_tmux(runner, "kill-session", "-t", QUOTA_SESSION)
    started = _tmux_ok_on(runner, QUOTA_TMUX_SOCKET, "new-session", "-d", "-s", QUOTA_SESSION,
                          "-c", home, "-x", "180", "-y", "45", shlex.quote(path))
    if not started:
        return None
    deadline = (time.time() if now is None else now) + QUOTA_TIMEOUT_S
    result: Optional[dict[str, Any]] = None
    asked = False
    try:
        while time.time() < deadline:
            time.sleep(3)
            pane = _quota_tmux(runner, "capture-pane", "-p", "-J", "-t", QUOTA_SESSION) or ""
            # A fresh working directory asks whether the folder is trusted. It is
            # the node's own home; answer once and carry on.
            if "trust this folder" in pane:
                # The node's own home directory. Answer once and let it settle.
                _quota_tmux(runner, "send-keys", "-t", QUOTA_SESSION, "Down")
                _quota_tmux(runner, "send-keys", "-t", QUOTA_SESSION, "Enter")
                continue
            if not asked:
                # Deliberately not keyed to a banner string: the welcome text
                # changes between releases, and an earlier version of this waited
                # for one that had scrolled away. The trust prompt being gone is
                # the only signal that means anything stable.
                _quota_tmux(runner, "send-keys", "-t", QUOTA_SESSION, "/usage")
                _quota_tmux(runner, "send-keys", "-t", QUOTA_SESSION, "Enter")
                asked = True
                continue
            if asked:
                found = parse_quota(pane)
                # A pane captured mid-draw can hold the session block with the
                # weekly one still to come. Taking that would cache a half
                # answer for the next half hour, so keep the best seen and wait
                # for both; settle for a partial one only when time runs out.
                if len(found) > len(result or {}):
                    result = found
                if "session" in found and "week" in found:
                    break
    finally:
        _quota_tmux(runner, "kill-session", "-t", QUOTA_SESSION)
    return result


def quota_summary(state: Mapping[str, Any], runner: Runner = subprocess.run,
                  now: Optional[float] = None) -> tuple[Optional[dict[str, Any]],
                                                        Optional[dict[str, Any]]]:
    """Cached windows, refreshed on the slow schedule. Returns (report, to_store)."""
    now = time.time() if now is None else now
    cached = state.get("quota") if isinstance(state.get("quota"), Mapping) else None
    if cached:
        age = now - (cached.get("ts") or 0)
        if age < QUOTA_REFRESH_S:
            return {k: v for k, v in cached.items() if k != "ts"}, None
    fresh = read_quota(runner, now)
    if fresh is None:
        # Keep showing the last known answer rather than blanking the card; it is
        # stamped, so the console can say how old it is.
        return ({k: v for k, v in cached.items() if k != "ts"} if cached else None), None
    fresh["checked_at"] = now
    return fresh, {**fresh, "ts": now}


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


def _tmux_ok_on(runner: Runner, socket: str, *args: str, timeout: float = 15.0) -> bool:
    """As _tmux_ok, on a named socket."""
    try:
        proc = runner(["tmux", "-L", socket, *args], capture_output=True, text=True,
                      timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


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


# A setup-token credential. Anthropic's prefix, and long enough that a short
# lookalike in the surrounding text cannot be mistaken for one.
TOKEN_RE = re.compile(r"\bsk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{20,}")


def find_token(pane: str) -> Optional[str]:
    """The minted credential, read off the screen that printed it."""
    match = TOKEN_RE.search(pane)
    return match.group(0) if match else None


def start_login(email: Optional[str], runner: Runner = subprocess.run,
                kind: str = "login") -> bool:
    """Open a fresh pane running the flow the console asked for. True if started.

    Both flows are the same shape — a URL to approve and a code to type back —
    so they share the pane, the reader and the code path. They differ in the
    command and in what comes out at the end: a sign-in leaves a credential on
    the node, a token prints one for the owner to carry away.
    """
    path = find_claude()
    if not path:
        return False
    _tmux(runner, "kill-session", "-t", LOGIN_SESSION)
    if kind == "token":
        # No --email: setup-token does not take one, and the account is decided
        # by the login this node already has.
        argv = [path, "setup-token"]
        email = None
    else:
        argv = [path, "auth", "login", "--claudeai"]
    if email:
        argv += ["--email", email]
    # -d so nothing needs a terminal; the pane is driven and read by tmux alone.
    # The trailing sleep is load-bearing: without it the session dies with the
    # command and takes the final screen with it. tmux runs this string through
    # a shell, so the sequence is honoured as written.
    command = " ".join(shlex.quote(a) for a in argv) + f"; sleep {LOGIN_HOLD_S}"
    return _tmux_ok(runner, "new-session", "-d", "-s", LOGIN_SESSION,
                    "-x", str(LOGIN_PANE_WIDTH), "-y", "50", command)


def read_login_pane(runner: Runner = subprocess.run) -> str:
    """Read the pane with wrapped lines joined.

    Measured on a live sign-in: the verification URL is ~496 characters and wraps
    across several rows. Without -J, capture-pane returns each row separately and
    the URL arrives truncated to its first 166 characters — long enough to look
    like a URL and useless to click.
    """
    return _tmux(runner, "capture-pane", "-p", "-J", "-t", LOGIN_SESSION) or ""


# The pane this agent opens, so the width a line is broken at is known rather
# than inferred. Used to create the session and to read it back.
LOGIN_PANE_WIDTH = 200
# tmux destroys a session the moment its command exits, and `capture-pane` on a
# dead session returns nothing at all. `claude setup-token` prints the
# credential and exits immediately, so the one thing the whole flow exists to
# read was gone before the next poll could see it. Holding the pane open after
# the command finishes is what makes its last screen readable. Bounded well
# past the server's own fifteen-minute expiry, and killed by end_login long
# before that in the normal case.
LOGIN_HOLD_S = 1200
# How many following lines a URL may be stitched from. The real ones run to a
# few hundred characters in a 200-column pane, so two is already generous; the
# cap is what stops a runaway from swallowing the rest of the screen.
URL_CONTINUATION_LINES = 4
# A continuation is a whole line of URL-safe characters and nothing else. Any
# space means it is prose, which is what ends the stitch.
URL_TAIL_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]+$")


def find_login_url(pane: str) -> Optional[str]:
    """The verification URL, rejoined if the screen broke it across lines.

    `capture-pane -J` joins lines *tmux* wrapped, which is not the same as
    lines the program wrapped itself. Claude Code prints its own newline inside
    the URL, so tmux sees two ordinary lines and leaves them apart: measured on
    a live `setup-token`, a 346-character URL arrived as 200 characters plus a
    separate 146. Long enough to look like a URL, and broken when clicked.
    """
    match = LOGIN_URL_RE.search(pane)
    if not match:
        return None
    url = match.group(0)
    lines = pane.splitlines()
    # Which line the match ended on; continuations can only follow that one.
    at = next((i for i, line in enumerate(lines) if url in line), -1)
    if at >= 0:
        for offset in range(URL_CONTINUATION_LINES):
            here = at + offset
            # Only a line broken by the edge of the pane has a continuation.
            # Without this, a bare "Esc" or "Continue" printed directly under a
            # short URL is all URL-safe characters and gets appended to it.
            if here >= len(lines) or len(lines[here]) < LOGIN_PANE_WIDTH:
                break
            tail = lines[here + 1].strip() if here + 1 < len(lines) else ""
            if not tail or not URL_TAIL_RE.match(tail):
                break
            url += tail
    # Strip anything a wrap or a quote left attached.
    return url.rstrip('"\'),.').strip()


# Claude Code's prompt swallows an Enter that arrives in the same burst as a
# hundred characters of pasted code: it is still handling the paste, and the
# newline goes with it. Measured on a live sign-in — the code sat typed at the
# prompt indefinitely, and a single Enter sent by hand completed it at once.
CODE_SETTLE_S = 1.0


def send_login_code(code: str, runner: Runner = subprocess.run) -> None:
    """Type the code into the waiting prompt, then submit it. Never logged.

    Two sends, with a pause. One send with the key appended looks equivalent
    and is not: the prompt is still busy with the text when the Enter lands.
    """
    _tmux(runner, "send-keys", "-t", LOGIN_SESSION, code)
    time.sleep(CODE_SETTLE_S)
    _tmux(runner, "send-keys", "-t", LOGIN_SESSION, "Enter")


def end_login(runner: Runner = subprocess.run) -> None:
    _tmux(runner, "kill-session", "-t", LOGIN_SESSION)


# How long the agent will stay resident driving one sign-in. Someone is watching
# the console, so it polls fast; but an abandoned attempt must not pin a process
# on the node forever.
# Long enough for a person. Someone has to read a link, approve it in a
# browser, copy a code and paste it back, and four minutes of that is a race
# they lose while looking at a phone. The server drops an unfinished attempt
# after fifteen minutes, so the agent stays just inside that and lets the
# server's expiry be the thing that ends it — one deadline, not two disagreeing.
#
# This used to have to be shorter than the five-minute timer to stop two runs
# overlapping. The lock does that now, so the window is free to be about the
# person instead. A resident run keeps heartbeating throughout, so nothing is
# paused by it holding on.
LOGIN_WINDOW_S = 840.0
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
    # The server decides which flow this is; an unknown word means sign-in
    # rather than a guess, because this picks the command that gets run.
    kind = "token" if wanted.get("kind") == "token" else "login"
    if mine.get("requested_at") != requested_at:
        # A new request supersedes anything in flight, including a stuck one.
        if not start_login(login_email(wanted.get("email")), runner, kind):
            new_state["login"] = {"requested_at": requested_at, "phase": "failed"}
            return {"state": "failed", "detail": "claude not found on this node",
                    "requested_at": requested_at}, new_state
        new_state["login"] = {"requested_at": requested_at, "phase": "started",
                              "kind": kind}
        return {"state": "requested", "requested_at": requested_at}, new_state

    phase = mine.get("phase")
    kind = mine.get("kind") or kind

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

    if phase == "ready":
        # Said once already and not yet released, which means the report has not
        # been confirmed. The pane still holds it, so read the same token and
        # say the same thing again; a repeated report is a no-op on the server.
        token = find_token(read_login_pane(runner))
        if token:
            return {"state": "ready", "secret": token,
                    "requested_at": requested_at}, new_state
        # The pane is gone and delivery was never confirmed. Say so plainly
        # rather than silently: a credential was minted and is now unreachable,
        # which the owner needs to know to revoke it from their account.
        end_login(runner)
        new_state.pop("login", None)
        return {"state": "failed",
                "detail": "a token was minted but could not be delivered; "
                          "revoke it from the Claude account",
                "requested_at": requested_at}, new_state

    if phase == "code_sent" and kind == "token":
        # A token flow has no auth state to check: the node was already signed
        # in, and nothing about it changes. The credential itself is the only
        # evidence the flow worked, and it exists only on the screen.
        pane = read_login_pane(runner)
        token = find_token(pane)
        if token:
            # Deliberately not torn down here. The credential already exists on
            # Anthropic's side the moment this screen prints it, so closing the
            # pane before the report has landed would strand a live token that
            # nobody can see and nobody knows to revoke. Hold the pane and stay
            # in this phase; the teardown happens when the server stops asking,
            # which is how it learns the report arrived.
            new_state["login"] = {**mine, "phase": "ready"}
            return {"state": "ready", "secret": token,
                    "requested_at": requested_at}, new_state
        if re.search(r"(?i)\b(invalid|expired|failed|error)\b", pane):
            end_login(runner)
            new_state.pop("login", None)
            return {"state": "failed", "detail": "the code was not accepted",
                    "requested_at": requested_at}, new_state
        return None, new_state

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
    # Reading the windows starts a Claude Code session, so it runs on its own slow
    # schedule and the answer is cached between heartbeats.
    quota, remember = quota_summary(state) if reconcile else (None, None)
    if remember is not None:
        state = {**state, "quota": remember}
    payload = build_payload(cfg, state=state, quota=quota)
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


# -- one slot on a shared machine -------------------------------------------------
#
# On a shared machine the agent that talks to the server runs as root, because
# creating and removing slot users is root's work. It must not then read a
# slot's files as root: everything in a slot's home belongs to its holder,
# symlinks included, and root following one of them reads whatever it points
# at. So it runs this — the ordinary collectors above — as the slot's own user,
# and takes back a JSON report whose content the server validates again.

SLOT_STATE_PATH = "~/.config/ccfleet/slot-state.json"
MAX_SLOT_REQUEST = 64 * 1024


def start_remote_control(runner: Runner = subprocess.run) -> None:
    """Start a signed-in slot's Remote Control, if it is enabled and not running.

    It is how the slot's holder reaches it at all: a slot has no SSH key and no
    shell anybody can open. slot-add.sh enables the unit but cannot start it,
    because Remote Control needs a login that does not exist until the holder
    signs in — so the first report after they do starts it. The unit runs
    under the slot's own systemd manager, not under whoever asked.
    """
    if _run(runner, ["systemctl", "--user", "is-enabled", DEFAULT_RC_SERVICE],
            timeout=10) != "enabled":
        return
    _run(runner, ["systemctl", "--user", "start", DEFAULT_RC_SERVICE], timeout=60)


def slot_facts(request: Mapping[str, Any], runner: Runner = subprocess.run,
               now: Optional[float] = None) -> dict[str, Any]:
    """What this slot looks like, collected as its own user.

    The same facts an owner node reports about its owner, and no more: the
    version, whether the login works and on which plan, Remote Control's
    state, token counts and the quota windows. The quota read starts a Claude
    Code session, so it is only refreshed when the machine agent asks —
    it spreads those across its slots rather than starting six at once.

    A sign-in its holder started from their page is carried one step further
    here, by the same code that signs an owner node in — the URL out, the code
    back — and its progress goes back with the facts.
    """
    now = time.time() if now is None else now
    state_path = Path(SLOT_STATE_PATH).expanduser()
    state = read_state(state_path)
    # First, so a sign-in that completes in this step already reads as signed
    # in below — and the slot is active in the same heartbeat, not the next.
    progress, state = reconcile_login({"login": request.get("login")}, state, runner)
    config_dir = Path.home() / ".claude"
    credentials = credentials_summary(config_dir)
    status = auth_status(runner)
    if status:
        credentials.update(status)
        credentials["present"] = status.get("logged_in", credentials.get("present"))
    remote = remote_control_state(DEFAULT_RC_SERVICE, runner)
    if credentials.get("logged_in") is True and remote.get("state") != "active":
        start_remote_control(runner)
        remote = remote_control_state(DEFAULT_RC_SERVICE, runner)
    facts: dict[str, Any] = {
        "claude": {"version": claude_info(runner).get("version")},
        "credentials": credentials,
        "remote_control": remote,
        "usage": usage_summary(config_dir),
    }
    # A slot nobody has signed into yet has no windows to read, and the session
    # the read opens would start on the login screen — where the keystrokes it
    # types to reach /usage would land instead. Most slots spend their first
    # minutes exactly there, between being claimed and being signed into.
    if request.get("refresh_quota") is True and credentials.get("logged_in") is True:
        quota, remember = quota_summary(state, runner, now)
        if remember is not None:
            state = {**state, "quota": remember}
    else:
        cached = state.get("quota") if isinstance(state.get("quota"), Mapping) else None
        quota = {k: v for k, v in cached.items() if k != "ts"} if cached else None
    write_state(state_path, state)
    if quota:
        facts["quota"] = quota
    if progress:
        facts["login"] = progress
    return facts


def slot_facts_main(stdin: Any, stdout: Any, runner: Runner = subprocess.run) -> int:
    """`--slot-facts`: read a request on stdin, write this slot's facts to stdout."""
    if os.geteuid() == 0:
        # As root the collectors would read the slot's files with root's
        # authority, which is the one thing this mode exists to avoid.
        print("error: --slot-facts runs as the slot's own user, never as root",
              file=sys.stderr)
        return 2
    try:
        request = json.loads(stdin.read(MAX_SLOT_REQUEST) or "{}")
    except ValueError:
        request = {}
    if not isinstance(request, Mapping):
        request = {}
    # Everything below resolves paths against the home directory, and so does
    # the session the quota read opens. Start from there rather than wherever
    # the caller happened to leave us.
    os.chdir(Path.home())
    json.dump(slot_facts(request, runner), stdout, separators=(",", ":"))
    return 0


# Distinct from None, which means "another run holds it". This means "there is
# no lock to hold", which is not a reason to refuse to run.
_UNLOCKED = object()


def hold_the_only_run(state_path: Path) -> Optional[Any]:
    """Take an exclusive lock for this run, or return None if one is running.

    Two agents on one node fight: they drive the same tmux session and write
    the same state file, so one calls `start_login` and kills the pane the
    other is reading, and a sign-in dies with no error anywhere. The timer and
    a resident login run overlapped often enough that this was the common case,
    not an edge one.

    The lock is advisory and held by an open file descriptor, so it is released
    when the process ends however it ends — including a kill.
    """
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(state_path.with_suffix(".lock"), "w")
    except OSError as exc:
        # A node that cannot make a lock file should still report facts.
        log.debug("no lock file (%s); continuing unlocked", exc.__class__.__name__)
        return _UNLOCKED
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ccfleet-agent",
                                     description="Post one heartbeat to the ccfleet server.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the payload instead of sending it")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="report facts but never act on the server's desired state")
    parser.add_argument("--slot-facts", action="store_true",
                        help="for the machine agent: report this user's slot as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if args.slot_facts:
        # Needs no configuration and must not read any: it is started with a
        # clean environment by the machine agent, which is the one that holds
        # the machine's token and talks to the server.
        return slot_facts_main(sys.stdin, sys.stdout)
    env = {**load_env_file(Path(args.env_file).expanduser()), **os.environ}
    try:
        cfg = AgentConfig.from_env(env)
    except AgentConfigError as exc:
        print(f"error: {exc} (env file: {args.env_file})", file=sys.stderr)
        return 2
    if args.print_only:
        print(json.dumps(build_payload(cfg, state=read_state(cfg.state_path)),
                         indent=2, sort_keys=True))
        return 0

    # One run at a time. Held for the whole run, taken before the state file is
    # read so two runs cannot both act on the same picture of it.
    lock = hold_the_only_run(cfg.state_path)
    if lock is None:
        log.info("another run is already working; leaving it to it")
        return 0
    state = read_state(cfg.state_path)

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
