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
an organisation; none of them are collected. One thing is derived from the
uuid: a one-way fingerprint, so the fleet server can tell when one Claude
account is signed in on two nodes (see account_fingerprint). It is also what makes a Mac
reportable at all, since the credential there lives in the Keychain and this
agent will not read it. Token values never reach the payload. Tests assert each
of these, including with a secret written into a fixture transcript.

One exception, on a shared machine only: a slot reports the email address of the
one Claude account signed in on it, so its holder's page can show which of their
accounts that slot is (see slot_account_labels). An owner node never does.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import logging
import math
import os
import platform
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
DEFAULT_SHELL_SERVICE = "ccfleet-shell.service"
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


# How much of the digest is sent: enough that two accounts in one small fleet
# never collide, short enough that it is plainly a label and not the id.
ACCOUNT_FP_HEX = 16


def account_fingerprint(global_config: Path) -> Optional[str]:
    """An opaque name for the Claude account a Claude Code config belongs to.

    The one rule ccfleet keeps is one Claude account, one node, and a server
    can only notice the same account on two nodes if both say which account
    they have. The account's id is not sent: this is a one-way digest of it —
    the same on every node the account is on, and no use for anything else. No
    email, name or organisation goes with it.
    """
    try:
        return fingerprint_of(global_config.read_bytes())
    except OSError:
        return None


def fingerprint_of(raw: Optional[bytes]) -> Optional[str]:
    """The same fingerprint, from a Claude Code config already read into memory."""
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    return hashlib.sha256(uuid.strip().encode("utf-8")).hexdigest()[:ACCOUNT_FP_HEX]


def global_config_of(config_dir: Path) -> Path:
    """~/.claude -> ~/.claude.json. Appended, never with_suffix (see above)."""
    return config_dir.parent / (config_dir.name + ".json")


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
        return {"present": False, "store": "file", "refresh_available": False}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    summary: dict[str, Any] = {"present": True, "store": "file",
                               "mtime": mtime, "expires_at": None,
                               "subscription_type": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        summary["parse_error"] = True
        return summary
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    summary["refresh_available"] = False
    if isinstance(oauth, dict):
        summary["refresh_available"] = (isinstance(oauth.get("refreshToken"), str)
                                        and bool(oauth["refreshToken"]))
        expires = oauth.get("expiresAt")
        if credential_expiry({"expires_at": expires}) is not None:
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


# Debian's and Ubuntu's own flag that an installed update needs a reboot to take
# effect (a new kernel, a new libc). Written by the packaging system; this only
# looks. Overridable so a test can point it at a file it controls.
REBOOT_REQUIRED_FILE = "/var/run/reboot-required"


def reboot_required() -> bool:
    """Whether the operating system has asked for a reboot."""
    return Path(os.environ.get("CCFLEET_REBOOT_REQUIRED_FILE",
                               REBOOT_REQUIRED_FILE)).exists()


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
            "mem": {"used_pct": _meminfo_used_pct()}, "reboot_required": reboot_required()}


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
    credentials["account_fp"] = account_fingerprint(global_config_of(cfg.claude_config_dir))
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
                    # Bounded by now, not by the end of this hour: the clock
                    # that wrote the record is this machine's own, so a record
                    # from even a minute ahead is a wrong one, not an early one.
                    if stamp is None or not since <= stamp <= now:
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
# Erik, 2026-09-24: every five minutes, and at once when somebody asks from
# a page (see quota_summary). It was half an hour.
QUOTA_REFRESH_S = 5 * 60
QUOTA_TIMEOUT_S = 90.0
# Claude Code keeps the last /usage answer in its global config, beside the
# account it was fetched for. Measured on 2.1.281: written whenever it fetches
# the windows — opening /usage does — at most once a minute, and not by an
# ordinary request (`claude -p`). Read and never reported: the account ids in
# it are compared here and dropped.
QUOTA_CACHE_KEY = "cachedUsageUtilization"
QUOTA_CACHE_WINDOWS = (("session", "five_hour"), ("week", "seven_day"))
# A reading Claude Code made this long before a probe began still answers it:
# it fetches at most once a minute, so the probe could have caused none newer.
QUOTA_CACHE_SLACK_S = 90.0
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
    """The home the probe's directory lives in, and the old probe worked in."""
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return None
    return str(home) if home.is_dir() else None


# The probe works in a directory of its own. Claude Code files every prompt in its
# history under the directory the session started in, and started in the home the
# probe put "/usage" there every half hour: 112 of one owner's 118 lines on
# 2026-09-23. An empty directory nobody works in takes those lines now, and the
# folder-trust answer the probe gives covers that directory instead of the home.
QUOTA_PROBE_DIR = (".cache", "ccfleet", "usage-probe")


def quota_probe_dir() -> tuple[Optional[str], Optional[str]]:
    """The probe's working directory, made 0700 if missing.

    Returns (path, None), or (None, why) when it is not a directory of this user's
    own. The trust prompt the probe answers applies to whatever its session starts
    in, so a symlink there, or something another user put there, must not be able
    to carry that answer anywhere else.
    """
    home = _quota_home()
    if home is None:
        return None, "no home directory to keep the usage probe in"
    path = os.path.join(home, *QUOTA_PROBE_DIR)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
    except OSError as exc:
        return None, f"cannot make the usage probe directory {path}: {exc.strerror or exc}"
    if stat.S_ISLNK(info.st_mode):
        return None, f"the usage probe directory {path} is a symlink"
    if not stat.S_ISDIR(info.st_mode):
        return None, f"the usage probe directory {path} is not a directory"
    if info.st_uid != os.getuid():
        return None, f"the usage probe directory {path} is not owned by this user"
    if stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            return None, f"cannot make the usage probe directory {path} private: " \
                         f"{exc.strerror or exc}"
    return path, None


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


def _native_probe_lock(probe: str, name: str = ".native-probe.lock") -> Optional[int]:
    """Serialize native maintenance; an unsafe/unavailable lock fails closed."""
    descriptor = None
    try:
        descriptor = os.open(os.path.join(probe, name),
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise OSError("unsafe probe lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return None


NATIVE_PROBE_OUTCOMES = frozenset({
    "native_refreshed", "native_refresh_not_due", "native_auth_rejected",
    "native_login_expired", "native_network_error", "native_rate_limited",
    "native_timeout", "native_launch_failed", "native_probe_busy",
    "native_refresh_unconfirmed",
})
NATIVE_TERMINAL_REASONS = frozenset({
    "refresh_unavailable", "refresh_expired", "native_login_expired", "native_auth_rejected",
})
NATIVE_RETRY_AFTER_MAX_S = 10 * 60
NATIVE_PANE_LIMIT = 32 * 1024


def native_probe_outcome(pane: str, now: float) -> dict[str, Any]:
    """Reduce an isolated native screen to fixed codes, never retain its text.

    A bare 401 or expired access token says nothing about refresh capability.
    Native contention/network messages can themselves suggest /login, so those
    recoverable markers take precedence over terminal login advice.
    """
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", pane[:NATIVE_PANE_LIMIT])
    lines = [line.strip().lstrip("⎿ ") for line in text.splitlines()]
    network_advice = any(line.lower().startswith(
        "this may be a temporary network issue, please try again") for line in lines)
    outcome = None
    for line in lines:
        lower = line.lower()
        if ("another claude code process is refreshing" in lower
                or lower.startswith("could not refresh your login because another")):
            outcome = "native_probe_busy"
            break
        if (lower.startswith("authentication error")
                and ("temporary network" in lower or network_advice)
                or re.match(r"^(?:api )?error: (?:connection error|fetch failed|econn|enotfound)",
                            lower)
                or lower.startswith("unable to connect to api")):
            outcome = "native_network_error"
            break
        if re.match(r"^(?:api )?error: 429\b", lower) or lower.startswith("rate limit exceeded"):
            outcome = "native_rate_limited"
            break
    if outcome is None:
        for line in lines:
            lower = line.lower()
            if (lower.startswith("oauth token revoked") and "/login" in lower
                    or lower.startswith("failed to refresh oauth token:")
                    and "invalid_grant" in lower and "/login" in lower):
                outcome = "native_auth_rejected"
                break
            if (lower.startswith("login expired") and "/login" in lower
                    or lower.startswith("refresh token has expired")
                    and ("/login" in lower or "log in again" in lower)):
                outcome = "native_login_expired"
                break
            if re.fullmatch(r"(?:token )?refresh (?:is )?(?:not due|not required|not needed)[.!]?",
                            lower):
                outcome = "native_refresh_not_due"
                break
    result: dict[str, Any] = {"outcome": outcome or "native_refresh_unconfirmed"}
    if outcome == "native_rate_limited":
        for line in lines:
            match = re.fullmatch(r"Retry-After:\s*([^\r\n]{1,80})", line, flags=re.I)
            if not match:
                continue
            raw = match.group(1).strip()
            delay = None
            if re.fullmatch(r"[0-9]{1,8}", raw):
                delay = float(raw)
            else:
                try:
                    instant = parsedate_to_datetime(raw)
                    if instant.tzinfo is not None:
                        delay = instant.timestamp() - now
                except (ValueError, TypeError, OverflowError):
                    pass
            if delay is not None and math.isfinite(delay) and delay >= 0:
                result["retry_after_s"] = min(delay, NATIVE_RETRY_AFTER_MAX_S)
                break
    return result


def read_quota(runner: Runner = subprocess.run,
               now: Optional[float] = None, *,
               timeout: float = QUOTA_TIMEOUT_S,
               probe_result: Optional[dict[str, Any]] = None,
               before_start: Optional[Callable[[], bool]] = None
               ) -> Optional[dict[str, Any]]:
    """Drive `claude` to its /usage screen once and read the windows off it."""
    observation = probe_result if probe_result is not None else {}
    observation.update(outcome="native_launch_failed", probe_started=False)
    path = find_claude()
    if not path:
        return None
    # Start it in the probe's own directory and nowhere else. The loop below
    # answers Claude Code's folder-trust prompt, and answering it means trusting
    # whatever directory this happened to start in — a checked-out project, if
    # someone ran the agent by hand from one. Pinning the directory is what makes
    # that answer safe, rather than assuming the service was launched somewhere
    # harmless; and an empty directory keeps the probe's "/usage" out of the
    # history of anywhere anybody works.
    probe, why = quota_probe_dir()
    if probe is None:
        observation["outcome"] = "native_probe_busy"
        log.warning("usage probe not started: %s", why)
        return None
    lock = _native_probe_lock(probe)
    if lock is None:
        observation["outcome"] = "native_probe_busy"
        return None
    try:
        if before_start is not None and not before_start():
            observation["outcome"] = "native_probe_busy"
            return None
        return _read_quota_locked(path, probe, runner, now, timeout, observation)
    finally:
        os.close(lock)


def _read_quota_locked(path: str, probe: str, runner: Runner,
                       now: Optional[float], timeout: float,
                       observation: Optional[dict[str, Any]] = None
                       ) -> Optional[dict[str, Any]]:
    observation = observation if observation is not None else {}
    original_runner = runner
    cleaning = False

    def observed_runner(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        try:
            return original_runner(argv, **kwargs)
        except subprocess.TimeoutExpired:
            if not cleaning:
                observation["outcome"] = "native_timeout"
            raise

    runner = observed_runner
    deadline = (time.time() if now is None else now) + min(timeout, QUOTA_TIMEOUT_S)

    def left() -> float:
        return max(0.1, min(15.0, deadline - time.time()))

    def command(*args: str) -> Optional[str]:
        if time.time() >= deadline:
            return None
        return _quota_tmux(runner, *args, timeout=left())

    command("kill-session", "-t", QUOTA_SESSION)
    if time.time() >= deadline:
        return None
    result: Optional[dict[str, Any]] = None
    asked = False
    try:
        # A timeout or nonzero client result does not prove the tmux server
        # rejected creation. Always clean up after even an ambiguous launch.
        observation.pop("probe_started", None)
        started = _tmux_ok_on(runner, QUOTA_TMUX_SOCKET, "new-session", "-d", "-s", QUOTA_SESSION,
                              "-c", probe, "-x", "180", "-y", "45", shlex.quote(path),
                              timeout=left())
        if not started:
            return None
        observation.update(probe_started=True, outcome="native_refresh_unconfirmed")
        while time.time() < deadline:
            time.sleep(min(3.0, max(0.0, deadline - time.time())))
            pane = command("capture-pane", "-p", "-J", "-t", QUOTA_SESSION) or ""
            classified = native_probe_outcome(pane, time.time())
            if classified["outcome"] != "native_refresh_unconfirmed":
                observation.update(classified)
                break
            # A fresh working directory asks whether the folder is trusted. It is
            # the probe's own empty directory; answer once and carry on.
            if "trust this folder" in pane:
                # Only ever the probe directory, checked above. Answer once and
                # let it settle.
                command("send-keys", "-t", QUOTA_SESSION, "Down")
                command("send-keys", "-t", QUOTA_SESSION, "Enter")
                continue
            if not asked:
                # Deliberately not keyed to a banner string: the welcome text
                # changes between releases, and an earlier version of this waited
                # for one that had scrolled away. The trust prompt being gone is
                # the only signal that means anything stable.
                command("send-keys", "-t", QUOTA_SESSION, "/usage")
                command("send-keys", "-t", QUOTA_SESSION, "Enter")
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
        if result is None and observation.get("outcome") == "native_refresh_unconfirmed":
            observation["outcome"] = "native_timeout"
    finally:
        # Cleanup has its own short allowance after the native-probe deadline.
        cleaning = True
        _quota_tmux(runner, "kill-session", "-t", QUOTA_SESSION, timeout=5.0)
    return result


def _instant(text: Any) -> Optional[float]:
    """An ISO 8601 time with its offset, as seconds since the epoch."""
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo is not None else None


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= 100 else None


def cached_usage(config_dir: Path) -> Optional[dict[str, Any]]:
    """The windows Claude Code last fetched for the account signed in now.

    Shaped as a report: each window's percentage used and the moment it resets,
    and when it was fetched. None when there is none, when it was for another
    account — one from before a change of account, which Claude Code itself
    throws away — or when it says nothing usable.
    """
    try:
        data = json.loads(global_config_of(config_dir).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    cache = data.get(QUOTA_CACHE_KEY) if isinstance(data, dict) else None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(cache, dict) or not isinstance(account, dict):
        return None
    owner = cache.get("accountUuid")
    fetched = cache.get("fetchedAtMs")
    windows = cache.get("utilization")
    if (not isinstance(owner, str) or owner != account.get("accountUuid")
            or isinstance(fetched, bool) or not isinstance(fetched, (int, float))
            or not isinstance(windows, dict)):
        return None
    out: dict[str, Any] = {}
    for name, key in QUOTA_CACHE_WINDOWS:
        window = windows.get(key)
        used = _percent(window.get("utilization")) if isinstance(window, dict) else None
        if used is None:
            continue
        at = _instant(window.get("resets_at"))
        out[name] = {"used_pct": used, **({"resets_at": at} if at is not None else {})}
    if not out:
        return None
    return {**out, "checked_at": fetched / 1000}


def _asked(wanted_at: Any, answered: float) -> bool:
    """A read asked for from a page since the one kept, and not tried yet."""
    return (isinstance(wanted_at, (int, float)) and not isinstance(wanted_at, bool)
            and wanted_at > answered)


def quota_summary(state: Mapping[str, Any], runner: Runner = subprocess.run,
                  now: Optional[float] = None, *, config_dir: Optional[Path] = None,
                  wanted_at: Any = None) -> tuple[Optional[dict[str, Any]],
                                                  Optional[dict[str, Any]]]:
    """The windows: kept for QUOTA_REFRESH_S, read again sooner when a page asks
    (`wanted_at`, tried once). Returns (report, to_store).

    Claude Code's own reading stands in whenever it is newer than the one
    kept — its holder opened /usage themselves — and is preferred to the screen
    after a probe: exact reset moments, and nothing mid-paint to misread.
    """
    now = time.time() if now is None else now
    config_dir = Path.home() / ".claude" if config_dir is None else config_dir
    cached = state.get("quota") if isinstance(state.get("quota"), Mapping) else None
    kept = float((cached or {}).get("ts") or 0)
    asked = _asked(wanted_at, max(kept, float((cached or {}).get("asked") or 0)))
    theirs = cached_usage(config_dir)
    if (theirs and theirs["checked_at"] > kept and now - theirs["checked_at"] < QUOTA_REFRESH_S
            and (not asked or theirs["checked_at"] >= wanted_at)):
        return theirs, {**theirs, "ts": theirs["checked_at"]}
    if cached and not asked and now - kept < QUOTA_REFRESH_S:
        return _quota_report(cached), None
    # A read asked for is tried once, whatever comes of it: a slot that cannot
    # read is not asked again every minute.
    tried = {"asked": wanted_at} if asked else {}
    probe, why = quota_probe_dir()
    if probe is None:
        # Said where it will be seen: in the log, and in the state beside the last
        # reading, which keeps its old stamp so the next run tries again. The
        # server keeps only the windows from a report; this is for whoever looks
        # at the node.
        log.warning("quota not read: %s", why)
        last = {k: v for k, v in (cached or {}).items() if k != "skipped"}
        return {**_quota_report(last), "skipped": why}, {**last, "skipped": why, **tried}
    fresh = read_quota(runner, now)
    theirs = cached_usage(config_dir)
    if theirs and theirs["checked_at"] >= now - QUOTA_CACHE_SLACK_S:
        return theirs, {**theirs, "ts": now, **tried}
    if fresh is None:
        # Keep showing the last known answer rather than blanking the card; it is
        # stamped, so the console can say how old it is.
        return ((_quota_report(cached) if cached else None),
                ({**(cached or {}), **tried} if tried else None))
    fresh["checked_at"] = now
    return fresh, {**fresh, "ts": now, **tried}


def _quota_report(cached: Mapping[str, Any]) -> dict[str, Any]:
    """A stored reading as it is reported: without its stamps or an old reason."""
    return {k: v for k, v in cached.items() if k not in ("ts", "skipped", "asked")}


# -- the old probe's lines in Claude Code's prompt history -------------------------
#
# Before the probe had a directory of its own it ran in the home, and each
# "/usage" it typed went into Claude Code's prompt history under the home. Those
# lines are taken out once. After that the probe never writes under the home
# again.

HISTORY_FILE = "history.jsonl"
HISTORY_CLEANED_KEY = "probe_history_cleaned"
# The lock Claude Code takes on its history to append a prompt, and to prune the
# file itself: a directory beside the file, where the file really lives (the
# proper-lockfile convention; Claude Code 2.1.281 counts one older than ten
# seconds as abandoned). While it is held Claude Code does not write the file,
# and an append that finds it waits and tries again.
HISTORY_LOCK_SUFFIX = ".lock"
# The history as it was stays beside it under this name, stamped.
HISTORY_BACKUP_INFIX = ".before-ccfleet-tidy-"


def _history_entry(line: bytes) -> Optional[dict[str, Any]]:
    try:
        entry = json.loads(line)
    except ValueError:                  # UnicodeDecodeError is one too
        return None
    return entry if isinstance(entry, dict) else None


def _probe_shaped(entry: Mapping[str, Any], home: str) -> bool:
    display = entry.get("display")
    return (isinstance(display, str) and display.strip() == "/usage"
            and entry.get("project") == home)


def _old_probe_lines(lines: Sequence[bytes], home: str) -> set[int]:
    """Which lines the old probe wrote, by position.

    Each probe run was a throwaway session that typed "/usage" in the home and
    nothing else. So a line goes only if it is exactly that and no line in the
    file shows its session typing anything else. A "/usage" the owner typed in
    the home in the middle of other work stays, and so does one with no session
    to judge by.
    """
    candidates: dict[int, str] = {}
    working: set[str] = set()
    for at, line in enumerate(lines):
        entry = _history_entry(line)
        if entry is None:
            continue
        session = entry.get("sessionId")
        if not isinstance(session, str):
            continue
        if _probe_shaped(entry, home):
            candidates[at] = session
        else:
            working.add(session)
    return {at for at, session in candidates.items() if session not in working}


def _lock_mark(lock: str) -> Optional[tuple[int, int]]:
    """What tells a lock apart from one made in its place later."""
    try:
        info = os.lstat(lock)
    except OSError:
        return None
    return info.st_ino, info.st_mtime_ns


def _file_mark(info: os.stat_result) -> tuple[int, int, int]:
    """What changes when anything is written to a file, or it is replaced."""
    return info.st_ino, info.st_size, info.st_mtime_ns


def clean_probe_history(config_dir: Path, home: str) -> Optional[int]:
    """Take the old probe's lines out of Claude Code's prompt history.

    Every other line, malformed ones included, is written back byte for byte, in
    the file's own mode. Returns how many went (0 when there is no history), or
    None when the file is locked, unreadable, not a plain file, changed under
    the tidy, or could not be rewritten, so the next run tries again.

    Nothing Claude Code writes may be lost to this:
    - The tidy holds Claude Code's own history lock from the read to the rename,
      the way Claude Code's own history prune does, so no append lands between.
    - The file is replaced only if it is still exactly what was read, for a
      writer that does not take the lock.
    - The file as it was stays beside it as a hard link, so the lines taken out,
      and anything such a writer still puts into it after the rename, stay on
      disk.
    """
    # Where it really lives, so a history kept elsewhere through a symlink is
    # tidied there and the link stays a link. Claude Code locks it there too.
    path = Path(os.path.realpath(config_dir / HISTORY_FILE))
    lock = f"{path}{HISTORY_LOCK_SUFFIX}"
    try:
        os.mkdir(lock, 0o700)
    except FileNotFoundError:
        return 0                            # no config directory, so no history
    except FileExistsError:
        log.info("%s is locked; tidying it next run", path)
        return None
    except OSError:
        return None
    mark = _lock_mark(lock)
    try:
        return _tidy_locked(path, home, lock, mark)
    finally:
        # Only the lock taken here. Held past Claude Code's ten seconds it may
        # have been counted as abandoned and taken over, and that one stays.
        if _lock_mark(lock) == mark:
            try:
                os.rmdir(lock)
            except OSError:
                pass


def _tidy_locked(path: Path, home: str, lock: str,
                 mark: Optional[tuple[int, int]]) -> Optional[int]:
    try:
        # Opened without blocking, so a pipe planted at the path cannot hang the
        # agent, and read only if it is a plain file.
        with open(path, "rb",
                  opener=lambda name, flags: os.open(name, flags | os.O_NONBLOCK)) as old:
            info = os.fstat(old.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            raw = old.read()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    # Split on newlines only: a stray carriage return inside a malformed line
    # must not cut it into pieces that could each pass for the probe's.
    pieces = raw.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
    gone = _old_probe_lines(lines, home)
    if not gone:
        return 0
    backup = f"{path}{HISTORY_BACKUP_INFIX}{time.time_ns()}"
    tmp, linked = "", False
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.ccfleet-")
        with os.fdopen(fd, "wb") as out:
            out.write(b"".join(line for at, line in enumerate(lines) if at not in gone))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, stat.S_IMODE(info.st_mode))
        now_there = os.stat(path)
        if _file_mark(now_there) != _file_mark(info) or _lock_mark(lock) != mark:
            os.unlink(tmp)
            log.info("%s changed while it was being tidied; trying again next run", path)
            return None
        os.link(path, backup)
        linked = True
        os.replace(tmp, path)
    except OSError as exc:
        for leftover in ([tmp] if tmp else []) + ([backup] if linked else []):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        log.info("could not tidy %s (%s); trying again next run", path, exc.__class__.__name__)
        return None
    log.info("took %d old usage-probe lines out of %s; the file as it was is %s",
             len(gone), path, backup)
    return len(gone)


def clean_probe_history_once(state: Mapping[str, Any], config_dir: Path, now: float,
                             save: Callable[[Mapping[str, Any]], bool]) -> Mapping[str, Any]:
    """The tidy, done once: the state as it was, or with the mark that it ran.

    The mark is saved before anything is taken out, and nothing is taken out
    unless it was. A mark that never reached the disk would let every later run
    tidy again, each taking a "/usage" the owner had since typed alone in a
    session in the home. A tidy that could not run takes its mark back so the
    next run tries again; if even that save fails, the mark stands and the old
    lines stay, which errs toward the owner's history.
    """
    if state.get(HISTORY_CLEANED_KEY):
        return state
    home = _quota_home()
    if home is None:
        return state
    marked = {**state, HISTORY_CLEANED_KEY: now}
    if not save(marked):
        return state
    if clean_probe_history(config_dir, home) is None:
        save(state)
        return state
    return marked


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


def write_state(path: Path, state: Mapping[str, Any]) -> bool:
    """Save the notes whole or not at all; True when they were saved.

    Written beside the file and renamed over it: a run cut short mid-write must
    not leave half a file, which would read back as no notes, marks included.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("could not write state: %s", exc.__class__.__name__)
        return False
    return _atomic_write(path, json.dumps(state, sort_keys=True))


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


#: A release as Anthropic's channel files spell it: what the server says a
#: channel stands at. Nothing looser is compared, and nothing else is kept.
RELEASE_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")


def channel_number(value: Any) -> Optional[str]:
    """The release the server says a channel stands at, checked, or None."""
    if isinstance(value, str) and RELEASE_RE.fullmatch(value.strip()):
        return value.strip()
    return None


def update_request(value: Any) -> Optional[float]:
    """When an update asked for from a page was asked, or None. A number and
    nothing else: it is what names the request in the answer."""
    requested_at = value.get("requested_at") if isinstance(value, Mapping) else None
    if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)):
        return None
    return requested_at


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


def _channel_moved(state: Mapping[str, Any], target: str, installed: Optional[str],
                   number: Optional[str]) -> bool:
    """The server says the channel stands at a release this does not run, and
    no install has been made for that release yet: go at once rather than wait
    out the daily re-check. Once per release, so an installer that lands on
    another number is not run again every minute."""
    if number is None or number == installed:
        return False
    channel = state.get("channel")
    channel = channel if isinstance(channel, Mapping) else {}
    return not (channel.get("target") == target and channel.get("number") == number)


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
                kind: str = "login", env_prefix: Sequence[str] = ()) -> bool:
    """Open a fresh pane running the flow the console asked for. True if started.

    Both flows are the same shape — a URL to approve and a code to type back —
    so they share the pane, the reader and the code path. They differ in the
    command and in what comes out at the end: a sign-in leaves a credential on
    the node, a token prints one for the owner to carry away.

    `env_prefix` starts the command under `env`, which is how a slot signs in
    again somewhere to one side before deciding to keep it (see
    reconcile_slot_login). A tmux server keeps the environment it started with
    and hands that to every later session, so a variable given to one tmux call
    does not reliably reach the command; it is named on the command instead.
    """
    path = find_claude()
    if not path:
        return False
    _tmux(runner, "kill-session", "-t", LOGIN_SESSION)
    if kind == "token":
        # No --email: setup-token does not take one, and the account is decided
        # by the login this node already has.
        argv = [*env_prefix, path, "setup-token"]
        email = None
    else:
        argv = [*env_prefix, path, "auth", "login", "--claudeai"]
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
                    runner: Runner = subprocess.run, *, env_prefix: Sequence[str] = (),
                    signed_in: Optional[Callable[[], bool]] = None
                    ) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Drive one step of a console-requested sign-in.

    Returns (progress to report, new state). Progress is None when there is
    nothing new to say, which keeps a fast poll from restating the same thing.

    `signed_in` replaces the plain "does the CLI say signed in?" test that ends
    a sign-in. A slot passes a stricter one (see reconcile_slot_login); left
    out, it is a node signing itself in, exactly as before.
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
        if not start_login(login_email(wanted.get("email")), runner, kind, env_prefix):
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
        worked = (signed_in() if signed_in is not None
                  else auth_status(runner).get("logged_in") is True)
        if worked:
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
                      now: Optional[float] = None, *,
                      asked: bool = False) -> Optional[dict[str, Any]]:
    """Bring the CLI to the pinned version. Returns a result to report, or None.

    None means nothing was attempted: no pin, already matching, claude not found,
    or still inside the back-off after a failure. Only a real attempt reports.

    A channel is installed again once the server says it moved on to a release
    this does not run (`channel_version`), and at once when somebody asked for
    it from their page (`asked`), which also skips the back-off: they pressed
    the button, and they are watching for the answer.
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
    number = channel_number(desired.get("channel_version")) \
        if target in VERSION_CHANNELS else None
    if (target in VERSION_CHANNELS and not asked
            and not _channel_moved(state, target, installed, number)
            and _channel_is_current(state, target, installed, now)):
        return None
    path = find_claude()
    if not path:
        return None

    last = state.get("upgrade")
    last = last if isinstance(last, Mapping) else {}
    if (not asked and last.get("ok") is False and last.get("to") == target
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
        if number is not None:
            # The release the server named when this ran: that one is tried.
            result["channel"]["number"] = number
    return result


def update_asked(raw: Any, state: Mapping[str, Any]) -> Optional[float]:
    """An update asked for from a page that has not been answered yet, or None."""
    requested_at = update_request(raw)
    said = state.get("claude_update")
    if requested_at is None or (isinstance(said, Mapping)
                                and said.get("requested_at") == requested_at):
        return None
    return requested_at


def settle_update(state: Mapping[str, Any], raw: Any) -> dict[str, Any]:
    """Forget an answer once the server stops asking: it has heard it."""
    said = state.get("claude_update")
    if isinstance(said, Mapping) and said.get("requested_at") == update_request(raw):
        return dict(state)
    return {k: v for k, v in state.items() if k != "claude_update"}


def update_answer(requested_at: float, target: Optional[str], installed: Optional[str],
                  result: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """What to tell the page about the update it asked for."""
    if result is None:
        # Nothing ran: the exact version asked for is here already, or there
        # is no Claude Code here to update.
        done = target is not None and target not in VERSION_CHANNELS and installed == target
        return {"requested_at": requested_at, "state": "done" if done else "failed",
                "to": (installed or "") if done else "",
                "detail": "" if done else "Claude Code was not found to update"}
    if result.get("ok") is True:
        return {"requested_at": requested_at, "state": "done",
                "to": str(result.get("to") or ""), "detail": ""}
    return {"requested_at": requested_at, "state": "failed", "to": "",
            "detail": str(result.get("error") or "the installer failed")[:200]}


def run_cycle(cfg: AgentConfig, state: Mapping[str, Any],
              login_progress: Optional[Mapping[str, Any]] = None,
              reconcile: bool = True) -> tuple[int, dict[str, Any], dict[str, Any],
                                               Optional[dict[str, Any]]]:
    """One post, and whatever acting on the reply calls for.

    Returns (status, desired, new state, login progress to report next time).
    """
    if reconcile:
        # Its mark is saved at once rather than with the rest of the state after
        # a good post: a node the server cannot hear would otherwise tidy on
        # every run.
        state = clean_probe_history_once(state, cfg.claude_config_dir, time.time(),
                                         lambda marked: write_state(cfg.state_path, marked))
    # Reading the windows starts a Claude Code session, so it runs on its own slow
    # schedule and the answer is cached between heartbeats.
    quota, remember = (quota_summary(state, config_dir=cfg.claude_config_dir) if reconcile
                       else (None, None))
    if remember is not None:
        state = {**state, "quota": remember}
    payload = build_payload(cfg, state=state, quota=quota)
    # What it did about an update asked for from its owner's page, until the
    # server stops asking.
    if isinstance(state.get("claude_update"), Mapping):
        payload.setdefault("reconcile", {})["claude_update"] = dict(state["claude_update"])
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
    state = settle_update(state, desired.get("update_now"))
    # Never under a sign-in: an install would swap the binary under the login
    # it is running. Nothing is installed until it is over, and an update asked
    # for meanwhile stays unanswered, so the first run after it takes it up.
    asked, result = None, None
    if not desired.get("login"):
        asked = update_asked(desired.get("update_now"), state)
        result = reconcile_version(desired, installed, state, asked=asked is not None)
    if asked is not None:
        state["claude_update"] = update_answer(
            asked, installable_version(desired.get("claude_version")), installed, result)
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


def retire_slot_remote_control(runner: Runner = subprocess.run) -> None:
    """Stop the superseded Cloud/client path on a hosted customer slot."""
    enabled = _rc_enabled(runner)
    active = remote_control_state(DEFAULT_RC_SERVICE, runner).get("state") == "active"
    if enabled or active:
        _run(runner, ["systemctl", "--user", "disable", "--now", DEFAULT_RC_SERVICE],
             timeout=60)


def ensure_slot_bypass_warning_is_accepted() -> None:
    """Keep the hosted default usable while restoring per-session effort control."""
    path = Path("~/.claude/settings.json").expanduser()
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return
    if not isinstance(current, dict):
        return
    changed = current.get("skipDangerousModePermissionPrompt") is not True
    current["skipDangerousModePermissionPrompt"] = True
    env = current.get("env")
    # Older CC Fleet releases forced max through an environment variable. That
    # made Claude Code's native /effort picker unable to change this session.
    # Only migrate the exact old platform default; preserve a holder's other
    # environment choices, including another explicitly selected effort.
    if isinstance(env, dict) and env.get("CLAUDE_CODE_EFFORT_LEVEL") == "max":
        env.pop("CLAUDE_CODE_EFFORT_LEVEL")
        changed = True
        if not env:
            current.pop("env")
    if not changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, json.dumps(current, indent=2) + "\n")


def sync_slot_terminal_unit(runner: Runner = subprocess.run) -> None:
    """Install the packaged persistent-session unit as the slot user."""
    root = Path(__file__).resolve().parents[1]
    source = root / "systemd" / DEFAULT_SHELL_SERVICE
    if not source.is_file():
        source = root / "node" / "systemd" / DEFAULT_SHELL_SERVICE
    try:
        wanted = source.read_text(encoding="utf-8")
    except OSError:
        return
    target = Path("~/.config/systemd/user").expanduser() / DEFAULT_SHELL_SERVICE
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current == wanted:
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _atomic_write(target, wanted, mode=0o644):
        # The superseded unit pre-warmed a general shell named `cc`. It has no
        # customer path now and could retain a process under the previous
        # account, so retire it during the same one-time migration.
        _run(runner, ["tmux", "kill-session", "-t", "cc"], timeout=15)
        # A unit change may alter the launch mode. Do not leave an already-open
        # Claude process on the old mode indefinitely; the next `ccfleet`
        # connection starts the packaged unit and reattaches from then on.
        _run(runner, ["tmux", "kill-session", "-t", "ccfleet"], timeout=15)
        _run(runner, ["systemctl", "--user", "daemon-reload"], timeout=30)
        _run(runner, ["systemctl", "--user", "enable", DEFAULT_SHELL_SERVICE], timeout=30)


def _tmux_gone(proc: subprocess.CompletedProcess, *, target: bool = False) -> bool:
    """Only known idempotent absence is success; never echo raw tmux errors."""
    error = proc.stderr if isinstance(proc.stderr, str) else ""
    if proc.returncode != 1 or len(error) > 4096:
        return False
    if error.startswith("no server running on "):
        return True
    if (error.startswith(("error connecting to ", "failed to connect to server"))
            and "No such file or directory" in error):
        return True
    return target and error.startswith(("can't find session:", "no such session:"))


def _slot_claude_sessions(output: str, home: Path) -> Optional[set[str]]:
    """Identify platform Claude panes without keeping or reporting their commands."""
    if not isinstance(output, str) or len(output) > 256 * 1024:
        return None
    lines = output.splitlines()
    if len(lines) > 4096:
        return None
    result = set()
    native = str(home / ".local/bin/claude")
    project = re.compile(r"[pl]_[0-9a-f]{32}_[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
    named = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}\Z")
    for line in lines:
        fields = line.split("\t")
        if len(line) > 8192 or len(fields) != 3:
            return None
        name, started, _current = fields
        if project.fullmatch(name):
            result.add(name)
        elif named.fullmatch(name):
            try:
                command = shlex.split(started)
            except ValueError:
                return None
            if command and command[0] == native:
                result.add(name)
    return result


def restart_slot_terminal(runner: Runner = subprocess.run) -> bool:
    """End every platform Claude session on an explicitly changed/refreshed login.

    A named project or older named terminal can cache its account just like the
    default session. Do not mark restart debt paid until all those sessions are
    gone. Other shell sessions and the separate login/quota tmux servers survive.
    """
    targets = {"ccfleet"}
    complete = True
    if (Path.home() / ".config/ccfleet/live").exists():
        # A held live mount must not retain the previous account's access, even
        # when there are no visible tmux panes or filesystem I/O is blocked.
        try:
            stopped = runner(["/usr/bin/python3", "-I",
                              str(Path(__file__).with_name("live_access.py")), "_stop-all"],
                             capture_output=True, text=True, timeout=60, check=False)
            if stopped.returncode:
                complete = False
        except (OSError, subprocess.SubprocessError):
            complete = False
    try:
        panes = runner(["tmux", "list-panes", "-a", "-F",
                        "#{session_name}\t#{pane_start_command}\t#{pane_current_command}"],
                       capture_output=True, text=True, timeout=15, check=False)
        if panes.returncode == 0:
            discovered = _slot_claude_sessions(panes.stdout, Path.home())
            if discovered is None:
                complete = False
            else:
                targets.update(discovered)
        elif not _tmux_gone(panes):
            complete = False
    except (OSError, subprocess.SubprocessError):
        complete = False
    # Always attempt the default even if discovery failed. Exact matching avoids
    # accidentally killing a similarly named unrelated session or a prefix.
    for target in sorted(targets):
        try:
            killed = runner(["tmux", "kill-session", "-t", "=" + target],
                            capture_output=True, text=True, timeout=15, check=False)
            if killed.returncode != 0 and not _tmux_gone(killed, target=True):
                complete = False
        except (OSError, subprocess.SubprocessError):
            complete = False
    if not complete:
        return False
    try:
        proc = runner(["systemctl", "--user", "restart", DEFAULT_SHELL_SERVICE],
                      capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _rc_enabled(runner: Runner) -> bool:
    return _run(runner, ["systemctl", "--user", "is-enabled", DEFAULT_RC_SERVICE],
                timeout=10) == "enabled"


def reconcile_slot_version(request: Mapping[str, Any], state: Mapping[str, Any],
                           installed: Optional[str], runner: Runner = subprocess.run,
                           now: Optional[float] = None) -> tuple[dict[str, Any], Optional[str]]:
    """Bring a held slot to its machine's pin. Returns (state, version now installed).

    The machine agent names the pin for every slot somebody holds, and says
    whether this slot may change right now — not while its holder is signing
    in. The installing is the owner agent's own, back-off and channels
    included: this only decides whether to ask. Installing never touches a
    session that is already running; it leaves the old version where it is.
    A running persistent terminal keeps the old process until its holder exits;
    the next session uses the installed version.
    """
    pin = {"claude_version": request.get("claude_version"),
           "channel_version": request.get("channel_version")}
    # Any restart debt came from the removed Remote Control slot mode.
    state = prune_state(state, pin, installed)
    state.pop("restart", None)
    state = settle_update(state, request.get("update_now"))
    if request.get("may_upgrade") is not True:
        # Its holder is signing in: an update asked for waits with the rest.
        return state, installed
    asked = update_asked(request.get("update_now"), state)
    result = reconcile_version(pin, installed, state, runner, now, asked=asked is not None)
    if asked is not None:
        state["claude_update"] = update_answer(
            asked, installable_version(pin["claude_version"]), installed, result)
    if result is None:
        return state, installed
    channel = result.pop("channel", None)
    state["upgrade"] = result
    if channel is not None:
        state["channel"] = channel
    if result["ok"] and result["to"] != installed:
        # A running CC Fleet tmux session keeps its current process. The next
        # session starts the newly installed binary; no Cloud/RC process is
        # restarted behind the holder's back.
        state.pop("restart", None)
        return state, result["to"]
    return state, installed


# -- one Claude account, in ~/.claude ------------------------------------------------
#
# A slot is signed in to one Claude account, its holder's own, and Claude Code
# keeps it where it always does: ~/.claude and ~/.claude.json. Nothing here ever
# points Claude Code anywhere else. A short-lived version of this agent could
# keep several accounts, each under ~/.config/ccfleet/claude-accounts/, with the
# one in use named by a CLAUDE_CONFIG_DIR line in the Remote Control env file.
# That broke the rule this project keeps — one account, one node — and is gone.
# Its directories are left alone: never read, never used, never deleted.

RC_ENV_FILE = "~/.config/ccfleet/remote-control.env"
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
# An address longer than this is not one the server would show; say nothing
# rather than send half of it.
MAX_ACCOUNT_EMAIL = 254


def _json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _epoch_s(raw: Any) -> Optional[float]:
    """A time Claude Code wrote, in seconds. It writes milliseconds."""
    if (type(raw) not in (int, float) or not 0 < raw < 1e14
            or not math.isfinite(raw)):
        return None
    seconds = raw / 1000 if raw > 1e11 else raw
    return finite_epoch(seconds)


def slot_account_labels(config_dir: Path) -> dict[str, Any]:
    """Which account the slot is signed in to, and how long that sign-in lasts.

    The one thing a slot reports that an owner node never does: the account's
    email address, so its holder's page can show which of their accounts this
    slot is. Read from two small files; the tokens beside these fields are never
    copied out, and nothing else about the account is.
    """
    profile = _json_object(config_dir.parent / (config_dir.name + ".json")).get("oauthAccount")
    email = profile.get("emailAddress") if isinstance(profile, dict) else None
    email = email.strip() if isinstance(email, str) else ""
    oauth = _json_object(config_dir / ".credentials.json").get("claudeAiOauth")
    expires = oauth.get("refreshTokenExpiresAt") if isinstance(oauth, dict) else None
    return {"email": email if len(email) <= MAX_ACCOUNT_EMAIL else "",
            "refresh_expires_at": _epoch_s(expires)}


def _credentials_stamp(config_dir: Path) -> Optional[list[int]]:
    """Which credential file is there now, to tell a new sign-in from the old one."""
    try:
        info = (config_dir / ".credentials.json").stat()
    except OSError:
        return None
    return [info.st_mtime_ns, info.st_size, info.st_ino]


# -- a slot keeps its account ----------------------------------------------------------
#
# A slot keeps the Claude account it was first signed in with. The binding is
# that account's fingerprint, kept in the slot's own state file, so it goes with
# the slot's home when the slot is wiped. Signing in again happens to one side,
# in a scratch Claude Code directory: the same account's fresh credential then
# replaces the old one in ~/.claude, whole; any other account's is thrown away,
# and the sign-in the slot already had is never touched.
#
# Its holder can move it to another Claude account of theirs — a change of
# account, kind "switch", which the server allows once a week. It signs in to
# scratch the same way, and only a sign-in that finishes there replaces the
# account the slot had: credential, account block and binding. Until then the
# slot keeps the old one, so it never holds two.

SIGNIN_SCRATCH = "~/.config/ccfleet/signin-scratch"
# Where Claude Code keeps its global config when CLAUDE_CONFIG_DIR names a
# directory: INSIDE it. Measured on 2.1.267 — `CLAUDE_CONFIG_DIR=$d claude auth
# status` creates $d/.claude.json (with $d/.claude.json.lock and $d/backups).
# Only the default ~/.claude keeps it beside, as ~/.claude.json, which is what
# global_config_of is for; it does not apply to the scratch directory.
SCRATCH_GLOBAL_CONFIG = ".claude.json"
OTHER_ACCOUNT = ("this slot stays with its own Claude account; to move it to another, "
                 "use Change account")
UNKNOWN_ACCOUNT = "could not tell which Claude account signed in; nothing was changed"
NOT_ADOPTED = "could not put the new sign-in in place; nothing was changed"
NO_SCRATCH = "could not make a place for the sign-in; nothing was changed"
HALF_DONE = ("it stopped halfway and could not be undone; change account again to "
             "finish it")
# How a change of account that went through ended, word for word: the server
# starts the week before the next change on the first alone, and the holder's
# page says which it was (ccfleetd/slots.py keeps the same two).
SWITCHED = "now signed in with another Claude account"
SAME_ACCOUNT = "signed in again with the same Claude account"


def bind_first_account(state: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the slot to the account signed in on it, once and for good.

    Whichever account has a credential in ~/.claude when nothing is bound yet
    is the slot's: its first sign-in, or — on a slot signed in before slots
    kept their account — the one it already had.
    """
    state = dict(state)
    if state.get("bound_fp"):
        return state
    home = Path.home()
    fp = account_fingerprint(global_config_of(home / ".claude"))
    if fp and (home / ".claude" / ".credentials.json").is_file():
        state["bound_fp"] = fp
    return state


def _in_config_dir(runner: Runner, place: Path) -> Runner:
    """`runner`, with Claude Code pointed at `place` for this one call."""
    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        env = dict(kwargs.pop("env", None) or os.environ)
        env[CONFIG_DIR_VAR] = str(place)
        return runner(argv, env=env, **kwargs)
    return run


def discard_scratch() -> None:
    """Delete the scratch directory, and whatever sign-in is in it."""
    scratch = Path(SIGNIN_SCRATCH).expanduser()
    try:
        if scratch.is_symlink():
            scratch.unlink()
        elif scratch.exists():
            shutil.rmtree(scratch)
    except OSError as exc:
        log.warning("could not clear the sign-in scratch: %s", exc.__class__.__name__)


def prepare_scratch() -> bool:
    """An empty, private place for a sign-in to land, past the one prompt that
    would otherwise wait for an answer nobody can give."""
    discard_scratch()
    scratch = Path(SIGNIN_SCRATCH).expanduser()
    try:
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
    except OSError as exc:
        log.warning("could not make the sign-in scratch: %s", exc.__class__.__name__)
        return False
    return _atomic_write(scratch / SCRATCH_GLOBAL_CONFIG,
                         json.dumps({"hasCompletedOnboarding": True}) + "\n")


def _read_once(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def adopt_sign_in(bound_fp: str, runner: Runner,
                  rebind: Optional[Callable[[str], bool]] = None
                  ) -> tuple[str, Optional[str]]:
    """Keep the scratch sign-in if it is the slot's own account — or, on a
    change of account, whichever account it is. Returns ("" when kept, else
    why not; the fingerprint of the account that signed in). The scratch
    directory is gone either way.

    `rebind` is what makes it a change of account: it saves the slot bound to
    another account, and says whether that was saved. Without it another
    account is refused, so a plain sign-in can never move the binding.

    Both files are read once, and what is written into ~/.claude is exactly the
    credential read beside the account that was checked — never the file again,
    which could have changed in between. Another account, kept, takes the
    slot's account block and its binding with it (see _move_account).

    What this is, and is not. It keeps the page's "Sign in again" from putting
    another account on the slot: somebody signing in, honestly, as the wrong
    account is refused. It is not a wall against the slot's holder. This agent
    runs as their Unix user, the scratch directory and ~/.claude are theirs,
    and nothing in those files ties a token to an account: a holder can put any
    credential beside any profile in ~/.claude directly, without this path, and
    no check running as them can stop it. What a deliberate change leaves is
    detection: a bound slot reports the account it keeps beside the one its
    profile names, the server raises account_changed when they differ — which
    Claude Code's own profile refresh brings about once another account's token
    is used — and using another account on a slot breaks the terms.
    """
    scratch = Path(SIGNIN_SCRATCH).expanduser()
    profile = _read_once(scratch / SCRATCH_GLOBAL_CONFIG)
    credential = _read_once(scratch / ".credentials.json")
    fp = fingerprint_of(profile)
    why = ""
    if fp is None:
        why = UNKNOWN_ACCOUNT
    elif fp != bound_fp and rebind is None:
        why = OTHER_ACCOUNT
        path = find_claude()
        if path:
            # Claude Code's own sign-out, for whatever it does beyond this
            # machine. Best effort: deleting the directory is what counts here.
            _run(_in_config_dir(runner, scratch), [path, "auth", "logout"], timeout=30.0)
    elif fp == bound_fp:
        why = _keep_credential(credential)
    else:
        why = _move_account(credential, profile, lambda: rebind(fp))
    discard_scratch()
    return why, fp


def _with_account(raw: Optional[bytes], account: Mapping[str, Any]) -> Optional[str]:
    """This ~/.claude.json, as read, with its account block replaced by
    `account` and every other key kept where it was. None unless it is a
    JSON object, and then nothing is written.

    Escaped to ASCII, so nothing the file holds can fail to be written back.
    """
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except ValueError:                    # not JSON, or not UTF-8 (UnicodeDecodeError)
        return None
    if not isinstance(data, dict):
        return None
    return json.dumps({**data, "oauthAccount": account}, indent=2)


def _decoded(credential: Optional[bytes]) -> Optional[str]:
    """The scratch credential as text, or None when there is none to keep."""
    try:
        return credential.decode("utf-8") if credential is not None else None
    except UnicodeDecodeError:
        return None


def _keep_credential(credential: Optional[bytes]) -> str:
    """The same account signed in again: its credential, whole, and nothing
    else. "" when kept, else why not."""
    text = _decoded(credential)
    target = Path.home() / ".claude" / ".credentials.json"
    return "" if text is not None and _atomic_write(target, text) else NOT_ADOPTED


def _move_account(credential: Optional[bytes], other: bytes, bind: Callable[[], bool]) -> str:
    """Another account, on a change of account. "" when moved, else why not.

    Three steps: the account block of ~/.claude.json, from `other`, the scratch
    profile it signed in with (the rest of that file is the holder's own
    settings and history, and stays); the credential; and `bind`, which saves
    the slot's binding to it. Only all three together are a change, and each
    step that fails puts back the ones before it: until the credential lands
    the slot signs in exactly as it did, and until the binding is saved nothing
    is said to have changed. Should putting back fail too, it says so, rather
    than that nothing was changed.
    """
    text = _decoded(credential)
    if text is None:
        return NOT_ADOPTED
    target = Path.home() / ".claude" / ".credentials.json"
    config = global_config_of(Path.home() / ".claude")
    was_block, was_credential = _read_once(config), _read_once(target)
    # fingerprint_of has read `other` as a JSON object with an account block.
    moved = _with_account(was_block, json.loads(other.decode("utf-8"))["oauthAccount"])
    if moved is None or not _atomic_write(config, moved):
        return NOT_ADOPTED
    if not _atomic_write(target, text):
        return NOT_ADOPTED if _put_back(config, was_block) else HALF_DONE
    if bind():
        return ""
    # Both put back, whichever of them fails.
    undone = [_put_back(target, was_credential), _put_back(config, was_block)]
    return NOT_ADOPTED if all(undone) else HALF_DONE


def _put_back(path: Path, raw: Optional[bytes]) -> bool:
    """A file as it was read: those bytes, or no file at all. True when it is."""
    if raw is not None:
        return _atomic_write(path, raw)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove %s: %s", path.name, exc.__class__.__name__)
        return False
    return True


def reconcile_slot_login(wanted: Any, state: Mapping[str, Any], runner: Runner, *,
                         save: Callable[[Mapping[str, Any]], bool]
                         ) -> tuple[Optional[dict[str, Any]], dict[str, Any], bool]:
    """A sign-in on a slot. Returns (progress, state, whether it finished now).

    Finished means the CLI says the place signed into is signed in AND its
    credential file changed since the attempt began. The first alone is already
    true of a slot signed in again while its old sign-in still works: it would
    call the attempt done — and close the pane — before the new one was written.

    The first sign-in goes straight into ~/.claude. Once the slot is bound (see
    bind_first_account), every sign-in goes to scratch and is kept only if it
    is the bound account (see adopt_sign_in). A device token is not a sign-in:
    it is minted from ~/.claude as it is.

    A change of account signs in to scratch too, and keeps another account:
    the slot is bound to it from then on — `save` writes that down before
    anything says so — and it says which way it ended in words the server
    knows (SWITCHED, SAME_ACCOUNT). On a slot not yet bound it is simply its
    first sign-in.
    """
    mine = state.get("login") if isinstance(state.get("login"), Mapping) else {}
    asked = isinstance(wanted, Mapping)
    if (asked and isinstance(mine.get("said"), Mapping)
            and mine.get("requested_at") == wanted.get("requested_at")):
        return dict(mine["said"]), dict(state), False       # not heard yet (see _to_say)
    fresh = asked and mine.get("requested_at") != wanted.get("requested_at")
    token = asked and wanted.get("kind") == "token"
    bound = state.get("bound_fp")
    scratch = (not token and bool(bound)) if fresh else bool(mine.get("scratch"))
    # Decided when the attempt starts and kept with it, like the place: what
    # may be kept at the end is what was asked for at the start.
    switch = wanted.get("kind") == "switch" if fresh else bool(mine.get("switch"))
    place = Path(SIGNIN_SCRATCH).expanduser() if scratch else Path.home() / ".claude"
    if fresh and scratch and not prepare_scratch():
        requested_at = wanted.get("requested_at")
        return ({"state": "failed", "detail": NO_SCRATCH, "requested_at": requested_at},
                {**state, "login": {"requested_at": requested_at, "phase": "failed"}}, False)
    before = _credentials_stamp(place) if fresh else mine.get("before")
    asks = _in_config_dir(runner, place) if scratch else runner

    def signed_in() -> bool:
        return (auth_status(asks).get("logged_in") is True
                and _credentials_stamp(place) != before)

    progress, new_state = reconcile_login(
        {"login": wanted}, state, runner,
        env_prefix=("env", f"{CONFIG_DIR_VAR}={place}") if scratch else (),
        signed_in=None if token else signed_in)
    if fresh and isinstance(new_state.get("login"), Mapping):
        new_state["login"] = {**new_state["login"], "before": before, "scratch": scratch,
                              "switch": switch}
    done = not token and (progress or {}).get("state") == "done"
    if done and scratch:
        def rebind(fp: str) -> bool:
            # Saved with the word the change ends on and the restart it owes:
            # a run cut short from here says it again and restarts Remote
            # Control on the next one.
            return save({**new_state, "bound_fp": fp, "account_restart": "owed",
                         "login": _to_say({**progress, "detail": SWITCHED})})

        why, fp = adopt_sign_in(str(bound), runner, rebind if switch else None)
        if why:
            return {**progress, "state": "failed", "detail": why}, new_state, False
        if switch:
            progress = {**progress, "detail": SWITCHED if fp != bound else SAME_ACCOUNT}
            new_state = {**new_state, "bound_fp": fp, "login": _to_say(progress)}
            if fp == bound:
                # Nothing moved, so there is nothing to undo; kept before the
                # restart all the same, as rebind keeps SWITCHED.
                save({**new_state, "account_restart": "owed"})
    return progress, new_state, done


def _to_say(progress: Mapping[str, Any]) -> dict[str, Any]:
    """How a change of account ended, kept until the server has heard it.

    The one sign-in whose end the server acts on: its word starts the week
    before the next change. Kept with the attempt it ends and said again on
    every run the server still asks for that attempt — a done row is never
    asked for (desired._login_block) — so a report lost to a run cut short, or
    a heartbeat that never landed, is not a change the server never hears of.
    """
    return {"requested_at": progress.get("requested_at"), "said": dict(progress)}


def _moved_on(state: Mapping[str, Any]) -> dict[str, Any]:
    """What a fresh sign-in makes stale, including legacy RC bookkeeping."""
    return {k: v for k, v in state.items()
            if k not in ("quota", "account_restart", "restart", "native_renewal")}


def _atomic_write(path: Path, text: str | bytes, mode: int = 0o600) -> bool:
    """Write a file whole or not at all, readable by this user alone.

    Written beside itself and renamed over, so a unit starting at the wrong
    moment reads the old file or the new one, never half of either. Bytes are
    written as they are: a file put back as it was read (see _put_back).
    """
    data = text.encode("utf-8") if isinstance(text, str) else text
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", path.name, exc.__class__.__name__)
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True


def _env_key(line: str) -> Optional[str]:
    """The variable a line of an env file sets, read as load_env_file reads it."""
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    key = text.partition("=")[0].strip()
    return key[len("export "):].strip() if key.startswith("export ") else key


def drop_config_dir_line() -> bool:
    """Take a CLAUDE_CONFIG_DIR line out of the Remote Control env file. True
    when one was taken out, and Remote Control must now be moved back.

    The several-accounts agent wrote one to run Remote Control as another of
    them. Left there, Remote Control would go on as an account this slot no
    longer has, while everything this agent reports is about ~/.claude. So the
    line goes and every other line stays. The directory it named is not touched.
    """
    path = Path(RC_ENV_FILE).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    kept = [line for line in lines if _env_key(line) != CONFIG_DIR_VAR]
    if len(kept) == len(lines) or not _atomic_write(
            path, "".join(f"{line}\n" for line in kept)):
        return False
    log.warning("Remote Control was pointed at another Claude directory; back on ~/.claude")
    return True


# Maintenance is scheduled before the relay's 30-second expiry cutoff. It uses
# native Claude, never a second OAuth implementation or credential writer.
NATIVE_RENEWAL_EARLY_S = 10 * 60
NATIVE_RENEWAL_TIMEOUT_S = 35.0
NATIVE_RENEWAL_RETRY_S = 60
NATIVE_RENEWAL_RETRY_MAX_S = 10 * 60
NATIVE_RENEWAL_TIMESTAMPS = ("last_attempt_at", "next_attempt_at", "last_success_at")


def finite_epoch(value: Any) -> Optional[float]:
    """A finite positive timestamp, excluding booleans and enormous integers."""
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if 0 < value < 1e11 and math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def credential_expiry(credentials: Mapping[str, Any]) -> Optional[float]:
    value = credentials.get("expires_at")
    if type(value) not in (int, float) or not 0 < value < 1e14:
        return None
    return finite_epoch(value / 1000)


def _slot_credential_facts(config_dir: Path, state: Mapping[str, Any],
                           status: Mapping[str, Any]) -> dict[str, Any]:
    credentials = credentials_summary(config_dir)
    credentials.update(slot_account_labels(config_dir))
    credentials["account_fp"] = account_fingerprint(global_config_of(config_dir))
    credentials["bound_fp"] = state.get("bound_fp")
    if status:
        credentials.update(status)
        credentials["present"] = status.get("logged_in", credentials.get("present"))
    return credentials


def native_retry_delay(outcome: str, failures: int, expiry: Optional[float], now: float,
                       retry_after: Any = None) -> float:
    """Bound temporary failures; only early eligibility checks converge on expiry."""
    failures = min(10, max(0, failures))
    base = min(NATIVE_RENEWAL_RETRY_S * 2 ** failures, NATIVE_RENEWAL_RETRY_MAX_S)
    if outcome in {"native_probe_busy", "native_refresh_not_due"}:
        base = NATIVE_RENEWAL_RETRY_S
    delay = min(NATIVE_RENEWAL_RETRY_MAX_S, base + random.uniform(0, base * 0.2))
    if outcome in {"native_refresh_not_due", "native_refresh_unconfirmed"}:
        delay = min(delay, max(NATIVE_RENEWAL_RETRY_S, ((expiry or now) - now) / 2))
    if (type(retry_after) in (int, float) and 0 <= retry_after <= NATIVE_RETRY_AFTER_MAX_S
            and math.isfinite(retry_after)):
        delay = max(delay, retry_after)
    return max(NATIVE_RENEWAL_RETRY_S, min(delay, NATIVE_RENEWAL_RETRY_MAX_S))


def _renewal_guard(credentials: Mapping[str, Any], previous: Mapping[str, Any],
                   stamp: Optional[list[int]], now: float) -> Optional[str]:
    if credentials.get("parse_error") or credentials.get("store") != "file":
        return "credential_unavailable"
    if credentials.get("refresh_available") is False:
        return "refresh_unavailable"
    refresh_expiry = finite_epoch(credentials.get("refresh_expires_at"))
    if refresh_expiry is not None and refresh_expiry <= now:
        return "refresh_expired"
    # Explicit native rejection is sticky only for the exact file observed then.
    # A normal native writer or completed sign-in changes the stamp and retries.
    if (isinstance(previous.get("reason"), str)
            and previous["reason"] in {"native_login_expired", "native_auth_rejected"}
            and stamp is not None and previous.get("credential_stamp") == stamp):
        return str(previous["reason"])
    if credential_expiry(credentials) is None:
        return "expiry_unknown"
    return None


def maintain_slot_credentials(state: Mapping[str, Any], request: Mapping[str, Any],
                              credentials: Mapping[str, Any], runner: Runner,
                              now: float, state_path: Path
                              ) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Native Claude is the sole writer; retain only bounded maintenance facts."""
    state = dict(state)
    previous = state.get("native_renewal")
    previous = dict(previous) if isinstance(previous, Mapping) else {}
    report = {key: finite_epoch(previous.get(key)) for key in NATIVE_RENEWAL_TIMESTAMPS
              if finite_epoch(previous.get(key)) is not None}
    for key in ("outcome", "probe_started"):
        value = previous.get(key)
        if (key == "outcome" and isinstance(value, str) and value in NATIVE_PROBE_OUTCOMES
                and value not in NATIVE_TERMINAL_REASONS
                or key == "probe_started" and isinstance(value, bool)):
            report[key] = value
    report.update(state="needed", checked_at=now)
    bound = state.get("bound_fp")
    config_dir = Path.home() / ".claude"

    def current_report(current: Mapping[str, Any]
                       ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        report["state"] = "current"
        for key in ("reason", "outcome", "probe_started", "next_attempt_at"):
            report.pop(key, None)
        saved = {**previous, **report, "failures": 0,
                 "credential_stamp": _credentials_stamp(config_dir)}
        for key in ("reason", "outcome", "probe_started", "next_attempt_at"):
            saved.pop(key, None)
        return {**current, "native_renewal": saved}, report, False

    def finish(reason: str, *, current_state: Optional[Mapping[str, Any]] = None,
               facts: Optional[Mapping[str, Any]] = None
               ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        nonlocal state
        if current_state is not None:
            state = dict(current_state)
        report.update(state="blocked", reason=reason)
        if facts is not None:
            saved = {**previous, **report, "credential_stamp": _credentials_stamp(config_dir)}
            if reason in NATIVE_PROBE_OUTCOMES:
                report["outcome"] = saved["outcome"] = reason
            else:
                saved.pop("outcome", None)
            saved.pop("next_attempt_at", None)
            report.pop("next_attempt_at", None)
            state["native_renewal"] = saved
        return state, report, False

    if not bound:
        return finish("account_unbound")
    if state.get("account_restart") or credentials.get("account_fp") != bound:
        return finish("account_transition")
    if request.get("login") or state.get("login"):
        return finish("sign_in_pending")
    if request.get("refresh_quota") is not True:
        reason = _renewal_guard(credentials, previous, _credentials_stamp(config_dir), now)
        if reason:
            return finish(reason, facts=credentials)
        if (credential_expiry(credentials) or 0) > now + NATIVE_RENEWAL_EARLY_S:
            return current_report(state)
        return state, report, False
    probe, _why = quota_probe_dir()
    lock = _native_probe_lock(probe, ".renewal.lock") if probe else None
    if lock is None:
        report.update(state="retrying", reason="native_probe_busy",
                      outcome="native_probe_busy", probe_started=False)
        return state, report, False
    try:
        current = read_state(state_path)
        if (current.get("bound_fp", bound) != bound or current.get("account_restart")
                or current.get("login")
                or account_fingerprint(Path.home() / ".claude.json") != bound):
            return finish("account_transition", current_state=current)
        latest = current.get("native_renewal")
        previous = dict(latest) if isinstance(latest, Mapping) else previous
        for key in NATIVE_RENEWAL_TIMESTAMPS:
            report.pop(key, None)
            observed_at = finite_epoch(previous.get(key))
            if observed_at is not None:
                report[key] = observed_at
        # A holder's independently running native Claude may already have rotated.
        fresh = _slot_credential_facts(config_dir, current, {})
        stamp = _credentials_stamp(config_dir)
        reason = _renewal_guard(fresh, previous, stamp, now)
        if reason:
            return finish(reason, current_state=current, facts=fresh)
        expiry = credential_expiry(fresh)
        if expiry is not None and expiry > now + NATIVE_RENEWAL_EARLY_S:
            return current_report(current)
        if (finite_epoch(previous.get("next_attempt_at")) or 0) > now:
            report.update({key: previous[key] for key in NATIVE_RENEWAL_TIMESTAMPS
                           if finite_epoch(previous.get(key)) is not None})
            outcome = previous.get("outcome")
            outcome = (outcome if isinstance(outcome, str) and outcome in NATIVE_PROBE_OUTCOMES
                       else "native_refresh_unconfirmed")
            report.update(state="retrying", reason=outcome, outcome=outcome)
            return dict(current), report, False
        failures = previous.get("failures", 0)
        failures = min(failures, 10) if type(failures) is int and failures >= 0 else 0
        reservation: dict[str, Any] = {}
        aborted: dict[str, Any] = {}

        def before_start() -> bool:
            # Called only while the quota lock is held, immediately before launch.
            current = read_state(state_path)
            facts = _slot_credential_facts(config_dir, current, {})
            if (current.get("bound_fp") != bound or current.get("account_restart")
                    or current.get("login") or facts.get("account_fp") != bound):
                aborted.update(reason="account_transition", state=current)
                return False
            reason = _renewal_guard(facts, previous, _credentials_stamp(config_dir), now)
            if reason:
                aborted.update(reason=reason, state=current, facts=facts)
                return False
            if (credential_expiry(facts) or 0) > now + NATIVE_RENEWAL_EARLY_S:
                aborted.update(reason="current", state=current)
                return False
            reservation.update({**report, "state": "retrying", "failures": failures,
                                "next_attempt_at": now + native_retry_delay(
                                    "native_refresh_unconfirmed", failures, expiry, now),
                                "reason": "native_refresh_unconfirmed",
                                "outcome": "native_refresh_unconfirmed",
                                "credential_stamp": _credentials_stamp(config_dir)})
            reservation.pop("probe_started", None)
            # A durable reservation prevents hot retries after a killed process,
            # without counting a launch/failure before one is confirmed.
            if not write_state(state_path, {**current, "native_renewal": reservation}):
                aborted.update(reason="maintenance_busy", state=current)
                return False
            return True

        observation: dict[str, Any] = {}
        read_quota(runner, timeout=NATIVE_RENEWAL_TIMEOUT_S,
                   probe_result=observation, before_start=before_start)
        if aborted:
            if aborted["reason"] == "current":
                return current_report(aborted["state"])
            return finish(aborted["reason"], current_state=aborted["state"],
                          facts=aborted.get("facts"))
        fresh = _slot_credential_facts(config_dir, read_state(state_path), {})
        after = credential_expiry(fresh)
        current = read_state(state_path)
        outcome = observation.get("outcome")
        outcome = (outcome if isinstance(outcome, str) and outcome in NATIVE_PROBE_OUTCOMES
                   else "native_refresh_unconfirmed")
        observed_stamp = _credentials_stamp(config_dir)
        if (outcome in NATIVE_TERMINAL_REASONS
                and observed_stamp != reservation.get("credential_stamp", stamp)):
            # A concurrent native writer changed the file since this screen's
            # session began. Its terminal advice cannot poison that new file,
            # even if expiry is unchanged and renewal cannot be proven.
            outcome = "native_refresh_unconfirmed"
        started = observation.get("probe_started")
        # Missing means an ambiguous tmux launch; schedule conservatively.
        attempted = started is not False
        saved = {**report, "state": "retrying", "reason": outcome, "outcome": outcome,
                 "failures": failures + int(attempted and outcome != "native_refresh_not_due"),
                 "next_attempt_at": now + native_retry_delay(
                     outcome, failures, expiry, now, observation.get("retry_after_s")),
                 "credential_stamp": observed_stamp}
        saved.pop("probe_started", None)
        if isinstance(started, bool):
            saved["probe_started"] = started
        if attempted:
            saved["last_attempt_at"] = now
        if (fresh.get("account_fp") != bound or current.get("bound_fp") != bound
                or current.get("account_restart") or current.get("login")):
            saved.update(state="blocked", reason="account_transition")
        elif (after is not None and expiry is not None and after > expiry
              and after > max(now, time.time()) + 30):
            saved.update(state="renewed", outcome="native_refreshed",
                         last_success_at=max(now, time.time()), failures=0)
            saved.pop("reason", None)
            saved.pop("next_attempt_at", None)
        else:
            reason = _renewal_guard(fresh, {}, _credentials_stamp(config_dir), now)
            if reason in NATIVE_TERMINAL_REASONS or outcome in NATIVE_TERMINAL_REASONS:
                saved.update(state="blocked", reason=reason or outcome)
                saved.pop("next_attempt_at", None)
        state = {**current, "native_renewal": saved}
        write_state(state_path, state)
        public = {key: value for key, value in saved.items()
                  if key not in ("failures", "credential_stamp")}
        return state, public, attempted
    finally:
        os.close(lock)


def _slot_relay_report(credentials: Mapping[str, Any], state: Mapping[str, Any],
                       now: float) -> Optional[dict[str, Any]]:
    """Read this slot's numeric aggregates as its user, never from the root agent."""
    bound = state.get("bound_fp")
    if (os.geteuid() == 0 or not isinstance(bound, str)
            or not re.fullmatch(r"[0-9a-f]{16}", bound)
            or credentials.get("bound_fp") != bound or credentials.get("account_fp") != bound
            or credentials.get("logged_in") is not True
            or state.get("login") or state.get("account_restart")):
        return None
    try:
        try:
            from . import relay_metrics
        except ImportError:
            # agent.py is also installed as a standalone -I script. Metrics are
            # optional; owner-only installations need not have this sibling.
            spec = importlib.util.spec_from_file_location(
                "ccfleet_relay_metrics", Path(__file__).with_name("relay_metrics.py"))
            if spec is None or spec.loader is None:
                return None
            relay_metrics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(relay_metrics)
        result = relay_metrics.validate_report(relay_metrics.report(Path.home(), bound, now))
        current = read_state(Path(SLOT_STATE_PATH).expanduser())
        if (current.get("bound_fp") != bound or current.get("login")
                or current.get("account_restart")
                or account_fingerprint(Path.home() / ".claude.json") != bound):
            return None
        return result
    except Exception:
        return None


def slot_facts(request: Mapping[str, Any], runner: Runner = subprocess.run,
               now: Optional[float] = None) -> dict[str, Any]:
    """What this slot looks like, collected as its own user.

    The same facts an owner node reports about its owner, and no more: the
    version, whether the login works and on which plan, token counts and the
    quota windows. A legacy Remote Control state may be reported while the old
    unit is being retired. The quota read starts a Claude
    Code session, so it is only refreshed when the machine agent asks —
    it spreads those across its slots rather than starting six at once.

    A sign-in its holder started from their page is carried one step further
    here, by the same code that signs an owner node in — the URL out, the code
    back — and its progress goes back with the facts. One more than a node
    reports: which Claude account the slot is signed in to (see
    slot_account_labels).
    """
    now = finite_epoch(now) or time.time()
    state_path = Path(SLOT_STATE_PATH).expanduser()
    state = read_state(state_path)
    # The slot's own history, once; its mark saved at once, as on an owner's node.
    state = dict(clean_probe_history_once(state, Path.home() / ".claude", now,
                                          lambda marked: write_state(state_path, marked)))
    # First, so a sign-in that completes in this step already reads as signed
    # in below — and the slot is active in the same heartbeat, not the next.
    moved = drop_config_dir_line()
    ensure_slot_bypass_warning_is_accepted()
    sync_slot_terminal_unit(runner)
    retire_slot_remote_control(runner)
    # Bound before the sign-in step, so a slot signed in before it kept its
    # account is held to that account from its very next sign-in. A first
    # sign-in binds here too, on the run that finds its credential: the code is
    # typed on one run and Claude Code writes the credential before the next.
    state = bind_first_account(state)
    # A change of account saves its new binding itself, the moment it has one:
    # before anything slow runs (Remote Control's restart alone may take a
    # minute), and before it is reported. See _move_account.
    progress, state, finished = reconcile_slot_login(
        request.get("login"), state, runner, save=lambda moved: write_state(state_path, moved))
    if "login" not in state:
        discard_scratch()                   # a sign-in cancelled, abandoned or over
    if finished:
        # Durable before touching the session: if this run dies while stopping
        # the old Claude process, the next run still owes the restart and no
        # process can remain indefinitely on the previous credential.
        state = {**state, "account_restart": "owed"}
        write_state(state_path, state)
    restart_owed = state.get("account_restart") == "owed"
    if moved or finished:
        state = _moved_on(state)
    if finished or restart_owed:
        if restart_slot_terminal(runner):
            state.pop("account_restart", None)
        else:
            state["account_restart"] = "owed"
    config_dir = Path.home() / ".claude"
    # And the account it keeps. Two fingerprints that differ are a slot signed
    # in to another account by some way other than its page, which the server
    # flags: the binding guards the page, and this says when it was gone round.
    status = auth_status(runner)
    credentials = _slot_credential_facts(config_dir, state, status)
    state, renewal, attempted = maintain_slot_credentials(
        state, request, credentials, runner, now, state_path)
    if attempted:
        status = auth_status(runner)
    # Also reflect a transition/backoff discovered under the maintenance lock.
    credentials = _slot_credential_facts(config_dir, state, status)
    credentials["renewal"] = renewal
    state, installed = reconcile_slot_version(request, state,
                                              claude_info(runner).get("version"), runner, now)
    remote = remote_control_state(DEFAULT_RC_SERVICE, runner)
    facts: dict[str, Any] = {
        "claude": {"version": installed},
        "credentials": credentials,
        "remote_control": remote,
        "usage": usage_summary(config_dir),
    }
    # A slot nobody has signed into yet has no windows to read, and the session
    # the read opens would start on the login screen — where the keystrokes it
    # types to reach /usage would land instead. Most slots spend their first
    # minutes exactly there, between being claimed and being signed into.
    if (request.get("refresh_quota") is True and credentials.get("logged_in") is True
            and not attempted and renewal.get("state") not in ("retrying", "needed")
            and renewal.get("reason") not in ("account_transition", "sign_in_pending",
                                               "maintenance_busy", "expiry_unknown",
                                               "credential_unavailable")
            and renewal.get("reason") not in NATIVE_TERMINAL_REASONS):
        quota, remember = quota_summary(state, runner, now, config_dir=config_dir,
                                        wanted_at=request.get("quota_wanted_at"))
        if remember is not None:
            state = {**state, "quota": remember}
        # Normal quota probes can also refresh native credentials.
        credentials.update(_slot_credential_facts(config_dir, state, status))
    else:
        cached = state.get("quota") if isinstance(state.get("quota"), Mapping) else None
        quota = _quota_report(cached) if cached else None
    upgrade = state.get("upgrade") if isinstance(state.get("upgrade"), Mapping) else {}
    if upgrade or state.get("restart"):
        facts["upgrade"] = {**upgrade, "restart": state.get("restart")}
    if isinstance(state.get("claude_update"), Mapping):
        facts["claude_update"] = dict(state["claude_update"])
    if state.get("restart") == "done":
        state.pop("restart")                # said once; there is nothing left to do
    write_state(state_path, state)
    if quota:
        facts["quota"] = quota
    if progress:
        facts["login"] = progress
    # Native probes above can take time. Date the aggregate at this fresh read,
    # not the beginning of reconciliation, which could hide concurrent requests.
    relay = _slot_relay_report(credentials, state, max(now, time.time()))
    if relay is not None:
        facts["relay"] = relay
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
    # Claude Code keeps a slot's one account in ~/.claude. A directory inherited
    # from whoever started this would quietly make every call about another.
    os.environ.pop(CONFIG_DIR_VAR, None)
    # Serialize the whole slot reconciliation, including sign-in adoption and
    # native maintenance. Timer/manual invocations must not race each other's
    # state or drive the same native probe. No lock means no mutation.
    probe, _why = quota_probe_dir()
    lock = _native_probe_lock(probe, ".slot-facts.lock") if probe else None
    if lock is None:
        print("slot maintenance is already running or its lock is unavailable", file=sys.stderr)
        return 2
    try:
        json.dump(slot_facts(request, runner), stdout, separators=(",", ":"))
    finally:
        os.close(lock)
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
