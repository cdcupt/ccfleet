#!/usr/bin/env python3
"""ccfleet machine agent: the one agent on a shared machine, running as root.

A shared machine carries one slot: a Linux user with its own home, its own
Claude Code and its own login. (It looks after a list of them all the same: a
machine from before one slot per machine may still carry several.) The
single-owner agent cannot look after them: it runs as its owner, creating and
removing Linux users is root's work, and its unit sets NoNewPrivileges so it
could not borrow root if it tried. So a shared machine runs this instead, once
a minute, under a system timer.

What it does as root, and nothing else:

  provision a slot somebody has claimed        node/slot-add.sh --slot <user>
  wipe a slot somebody has released            node/slot-remove.sh --slot <user>
  ask each slot about itself                   agent.py --slot-facts, as <user>
  answer to the name the server gives it       hostnamectl, /etc/hosts; then, as
                                               each slot's user, restart its
                                               Remote Control to take the name

The third is the one to be careful with. Everything under a slot's home belongs
to whoever holds it — every file and every symlink — so root never opens
anything there. It starts the ordinary agent as that user, with a clean
environment and no supplementary groups, and reads back a JSON report whose
size it bounds and whose content it treats as untrusted: the slot's holder can
shape it, and the server validates it again. What a report can never carry is
whether the user exists, or whether it was provisioned or wiped. Root answers
those from its own records, because they are what moves a slot between people.

It reports first and acts second, like the owner agent: each run posts what the
last run did and what the machine looks like now, then acts on the reply.
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import pwd
import re
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from ccfleet_agent import agent as core
except ImportError:  # installed as plain files side by side, not as a package
    # Run with -I, which keeps even this script's own directory off the path.
    # Put it back explicitly: it is root's, and nothing else on it is trusted.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agent as core  # type: ignore[no-redef]

log = logging.getLogger("ccfleet-machine")

DEFAULT_ENV_FILE = "/etc/ccfleet/agent.env"
DEFAULT_STATE_PATH = "/var/lib/ccfleet/machine.json"
DEFAULT_LIB_DIR = "/usr/local/lib/ccfleet"
# Where slot homes live, for the disk reading. Slots fill /home, not /.
SLOT_HOMES = "/home"

# The same rules slot-add.sh uses to decide an account is a slot: made by it,
# so in its group, and an ordinary login uid rather than a system account.
SLOT_GROUP = "ccfleet-slots"
FIRST_UID, LAST_UID = 1000, 59999
UNIX_USER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SLOT_STATES = ("free", "claiming", "claimed", "active", "releasing")

# Provisioning installs Claude Code for the new user, which downloads a
# release; a wipe stops services and deletes a home. Both bounded, generously.
SLOT_ADD_TIMEOUT_S = 15 * 60
SLOT_REMOVE_TIMEOUT_S = 5 * 60
# A failed wipe is usually something still running as the user. Trying again
# every minute fixes nothing and fills the log; try again every ten.
WIPE_RETRY_AFTER_S = 10 * 60
# Asking a slot about itself: normally seconds, but a quota read opens a
# Claude Code session and waits for it, which can take a minute and a half.
SLOT_FACTS_TIMEOUT_S = 150
# What a slot may say about itself. Its holder can shape this output, so it is
# read no further than a real report could ever need.
MAX_SLOT_REPORT_BYTES = 256 * 1024
# The server's reply names every slot on the machine, which outgrows the few
# hundred bytes an owner node gets back. Cut short, it would parse as nothing
# and the machine would quietly stop provisioning and wiping.
MAX_REPLY_BYTES = 1024 * 1024
# While somebody is signing in, the machine stays resident and polls fast; a
# full report of every slot still goes out this often, so provisioning and
# wiping do not wait out somebody else's sign-in.
FULL_EVERY_S = 60.0
# After this long a run takes on no new sign-ins; the next run starts them.
MAX_RESIDENT_S = 60 * 60
# The slot states a sign-in can run in, and the kinds it can be.
SIGN_IN_STATES = ("claimed", "active")
LOGIN_KINDS = ("login", "token")
MAX_LOGIN_FIELD = 512
# How long a child that has closed its output gets to exit before it is killed.
EXIT_GRACE_S = 5.0
# Only these reach the server from a slot's own report. Everything else in the
# entry — presence, provisioning, wipes — is root's own knowledge.
SLOT_FACT_KEYS = ("claude", "credentials", "remote_control", "quota", "usage", "upgrade")
# The states in which a slot's Linux user exists and belongs to somebody: the
# only ones whose Claude Code follows the machine's pin. A claiming slot is
# still being made, and a releasing one is about to be deleted.
UPGRADE_STATES = SIGN_IN_STATES


@dataclass(frozen=True)
class MachineConfig:
    url: str
    node_id: str
    token: str
    state_path: Path
    slot_add: Path
    slot_remove: Path
    slot_agent: Path
    egress_targets: tuple[str, ...] = core.DEFAULT_EGRESS_TARGETS
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MachineConfig:
        base = core.AgentConfig.from_env(env)   # the same URL, id and token rules
        lib = Path(env.get("CCFLEET_LIB_DIR") or DEFAULT_LIB_DIR)
        return cls(url=base.url, node_id=base.node_id, token=base.token,
                   state_path=Path(env.get("CCFLEET_STATE_FILE") or DEFAULT_STATE_PATH),
                   slot_add=lib / "slot-add.sh", slot_remove=lib / "slot-remove.sh",
                   # The agent that ships beside this file, so a slot is always
                   # asked by the same version that is doing the asking.
                   slot_agent=Path(__file__).resolve().with_name("agent.py"),
                   egress_targets=base.egress_targets, timeout_s=base.timeout_s)


Spawn = Callable[..., tuple[Optional[int], Optional[str]]]


def lookup_user(name: str) -> Optional[pwd.struct_passwd]:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def group_names(account: pwd.struct_passwd) -> set[str]:
    names = set()
    for gid in os.getgrouplist(account.pw_name, account.pw_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def run_bounded(argv: Sequence[str], *, input_text: str, limit: int, timeout: float,
                **popen: Any) -> tuple[Optional[int], Optional[str]]:
    """Run a child whose output is not trusted. Returns (exit code, output).

    Bounded twice over. At most `limit` bytes are read; past that the output is
    refused, not truncated, because half a JSON report is not a report. And the
    wait is bounded by the clock rather than by the pipe closing: a child can
    leave something behind holding its stdout open, and waiting for EOF would
    then wait for ever — one slot's leftover process stalling the agent that
    looks after every slot on the machine. Output is None when either bound hit.
    """
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True, **popen)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not start %s: %s", argv[0], exc.__class__.__name__)
        return None, None
    try:
        proc.stdin.write(input_text.encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass                                     # it exited without reading; see below
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size, complete = 0, False
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while size <= limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not sel.select(remaining):
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                complete = True
                break
            chunks.append(chunk)
            size += len(chunk)
    proc.stdout.close()
    finished = complete and size <= limit
    if finished:
        # It said everything and closed its output; give it a moment to exit
        # rather than killing a child that is only on its way out.
        try:
            return proc.wait(timeout=EXIT_GRACE_S), b"".join(chunks).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            pass
    # Abandoned: the whole group, not only the child. It runs as the slot's
    # user, and anything it started on the way is abandoned with it.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        code = proc.wait(timeout=EXIT_GRACE_S)
    except subprocess.TimeoutExpired:
        code = None
    return code, None


# -- the machine's name ---------------------------------------------------------------
#
# One machine is one slot (Erik, 2026-09-23), and claude.ai/code shows a machine
# by its hostname: the server says what this one should answer to — its holder's
# name while the slot is held, its own id while it is free — and root makes it so.

#: One hostname label, as the server sends it and hostnamectl takes it. Checked
#: here as well as there: this string reaches hostnamectl and /etc/hosts as root.
HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HOSTS_FILE = Path("/etc/hosts")
CLOUD_CFG_DIR = Path("/etc/cloud/cloud.cfg.d")
CLOUD_CFG_NAME = "99-ccfleet.cfg"
# cloud-init puts the provider's name back at boot unless told to leave the
# hostname, and /etc/hosts, alone.
CLOUD_CFG = "preserve_hostname: true\nmanage_etc_hosts: false\n"
RC_UNIT = "claude-remote-control.service"
SYSTEMCTL_TIMEOUT_S = 60
HOSTNAMECTL_TIMEOUT_S = 30


def set_hostname_now(name: str) -> bool:
    """hostnamectl, which also writes /etc/hostname. True when it worked."""
    try:
        proc = subprocess.run(["hostnamectl", "set-hostname", name], capture_output=True,
                              text=True, timeout=HOSTNAMECTL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0




@dataclass(frozen=True)
class System:
    """Everything this agent reaches outside itself, so a test can stand in."""

    lookup: Callable[[str], Optional[pwd.struct_passwd]] = lookup_user
    groups_of: Callable[[pwd.struct_passwd], set[str]] = group_names
    spawn: Spawn = run_bounded
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    opener: Callable[..., Any] = urllib.request.urlopen
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    # The machine's own name, and where it is kept.
    hostname: Callable[[], str] = socket.gethostname
    set_hostname: Callable[[str], bool] = set_hostname_now
    hosts_path: Path = HOSTS_FILE
    cloud_cfg_dir: Path = CLOUD_CFG_DIR


# -- what the server asks for ----------------------------------------------------


def wanted_slots(desired: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The slots the server says this machine carries, checked before use.

    A name here becomes an argument to a script run as root, so it is held to
    the rule slot-add.sh itself applies; a state this does not know, or a claim
    with no timestamp to report back against, is dropped rather than guessed at.
    """
    raw = desired.get("slots")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, Mapping):
            continue
        user, state = entry.get("unix_user"), entry.get("state")
        if not isinstance(user, str) or not UNIX_USER_RE.match(user) or user in seen:
            continue
        if state not in SLOT_STATES:
            continue
        claimed_at = entry.get("claimed_at")
        if state == "claiming" and (isinstance(claimed_at, bool)
                                    or not isinstance(claimed_at, (int, float))):
            continue
        seen.add(user)
        item: dict[str, Any] = {"unix_user": user, "state": state,
                                "claimed_at": claimed_at if state == "claiming" else None}
        login = _login_request(entry.get("login")) if state in SIGN_IN_STATES else None
        if login is not None:
            item["login"] = login
        out.append(item)
    return out


def _login_request(raw: Any) -> Optional[dict[str, Any]]:
    """A sign-in the server asks a slot to run, checked and copied field by field.

    It is handed on to a process running as the slot's user, which checks it
    again before anything reaches a command line. Only these fields go.
    """
    if not isinstance(raw, Mapping):
        return None
    requested_at = raw.get("requested_at")
    if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)):
        return None
    out: dict[str, Any] = {"requested_at": requested_at,
                           "kind": raw.get("kind") if raw.get("kind") in LOGIN_KINDS
                           else "login"}
    for key in ("email", "code"):
        value = raw.get(key)
        if isinstance(value, str) and value and len(value) <= MAX_LOGIN_FIELD:
            out[key] = value
    return out


def is_slot_account(account: pwd.struct_passwd, groups: set[str]) -> bool:
    """Only an account slot-add.sh made is one this agent will act as.

    Without this a slot declared with the operator's own login name would have
    the operator's Claude Code driven and reported on as though it were a
    customer's. The scripts refuse such an account for the same reason.
    """
    return FIRST_UID <= account.pw_uid <= LAST_UID and SLOT_GROUP in groups


def slot_env(account: pwd.struct_passwd) -> dict[str, str]:
    """A clean environment for a slot's process: its own, and nothing of root's.

    Built from nothing rather than inherited, so the machine's token — which is
    in root's environment — can never reach a process a slot's holder can
    inspect. The slot's own bin directory is last on the path, so a `tmux` or
    `systemctl` dropped there cannot stand in for the system's.
    """
    runtime = f"/run/user/{account.pw_uid}"
    return {"HOME": account.pw_dir, "USER": account.pw_name, "LOGNAME": account.pw_name,
            "PATH": f"/usr/local/bin:/usr/bin:/bin:{account.pw_dir}/.local/bin",
            "LANG": "C.UTF-8",
            # The probe runs `claude --version`, which can start an update and
            # swap the binary out from under the next call.
            "DISABLE_AUTOUPDATER": "1",
            # So `systemctl --user` reaches the slot's own manager.
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def ask_slot(account: pwd.struct_passwd, cfg: MachineConfig, system: System,
             request: Mapping[str, Any],
             timeout: float = SLOT_FACTS_TIMEOUT_S) -> dict[str, Any]:
    """The slot's own report on itself, collected as its own user."""
    code, output = system.spawn(
        [sys.executable, "-I", str(cfg.slot_agent), "--slot-facts"],
        input_text=json.dumps(dict(request)), limit=MAX_SLOT_REPORT_BYTES,
        timeout=timeout,
        user=account.pw_uid, group=account.pw_gid, extra_groups=[],
        env=slot_env(account), cwd="/")
    if code != 0 or output is None:
        log.warning("%s did not report on itself (exit %s)", account.pw_name, code)
        return {}
    try:
        facts = json.loads(output)
    except ValueError:
        log.warning("%s reported something that is not JSON", account.pw_name)
        return {}
    if not isinstance(facts, Mapping):
        return {}
    # Sign-in progress rides along too: its URL and any minted token are for
    # this slot's holder, and the server checks the URL and bounds the token.
    return {k: facts[k] for k in (*SLOT_FACT_KEYS, "login")
            if isinstance(facts.get(k), Mapping)}


# -- reporting -------------------------------------------------------------------


def slot_report(user: str, state: Mapping[str, Any], cfg: MachineConfig,
                system: System, refresh_quota: bool, ask: bool = True,
                abandoned: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """One slot, as root knows it and as it says of itself.

    `ask` False reuses what the slot said last time rather than asking again,
    for the fast polls while somebody is signing in: the machine's own facts
    are always fresh, and every slot is still in every report, so nothing
    reads as gone between two full ones.
    """
    account = system.lookup(user)
    entry: dict[str, Any] = {"unix_user": user, "present": account is not None}
    claim = (state.get("provisioned") or {}).get(user)
    if claim is not None:
        entry["provisioned_for"] = claim
    failed = (state.get("provision_failed") or {}).get(user)
    if isinstance(failed, Mapping):
        entry["provision_failed_for"] = failed.get("for")
        entry["provision_error"] = failed.get("error")
    wipe = (state.get("wipe_failed") or {}).get(user)
    if isinstance(wipe, Mapping):
        entry["wipe_error"] = wipe.get("error")
    if account is None or not is_slot_account(account, system.groups_of(account)):
        return entry
    # Root's facts are written first and the slot's cannot overwrite them:
    # only the named fact keys are taken from what the slot said.
    if ask:
        login = (state.get("slot_logins") or {}).get(user)
        request: dict[str, Any] = {"refresh_quota": refresh_quota, "login": login}
        timeout = SLOT_FACTS_TIMEOUT_S
        if (state.get("slot_states") or {}).get(user) in UPGRADE_STATES:
            # The machine's pin, and whether this slot may move to it now: not
            # while its holder is signing in, which a restart would cut short.
            request["claude_version"] = state.get("claude_version") or ""
            request["may_upgrade"] = login is None
            if request["may_upgrade"] and request["claude_version"]:
                # Room for the installer too: a download, not a probe.
                timeout = SLOT_FACTS_TIMEOUT_S + core.INSTALL_TIMEOUT_S
        heard = ask_slot(account, cfg, system, request, timeout)
    else:
        heard = dict((state.get("heard") or {}).get(user) or {})
    entry.update(heard)
    if abandoned and user in abandoned:
        entry["login"] = {"state": "failed", "requested_at": abandoned[user],
                          "detail": f"not completed within {int(core.LOGIN_WINDOW_S)}s"}
    return entry


def machine_payload(cfg: MachineConfig, state: Mapping[str, Any], system: System,
                    refresh_for: Optional[str] = None, fast: bool = False,
                    abandoned: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The machine itself, then each slot. No owner login: the machine has none."""
    payload: dict[str, Any] = {"node_id": cfg.node_id, "ts": system.clock(),
                               "agent_version": core.AGENT_VERSION, "mode": "machine"}
    payload.update(core.system_info())
    # The name the machine answers to, which the console compares with the
    # one it should: its slot's.
    payload["hostname"] = system.hostname()[:100]
    payload["disk"] = core.disk_info(Path(SLOT_HOMES))
    payload["egress"] = core.egress_ip(cfg.egress_targets, system.opener,
                                       min(cfg.timeout_s, 5.0))
    signing_in = set(state.get("slot_logins") or {}) | set(abandoned or {})
    payload["slots"] = [slot_report(user, state, cfg, system, user == refresh_for,
                                    ask=not fast or user in signing_in, abandoned=abandoned)
                        for user in state.get("slots") or []]
    return payload


def wanted_hostname(desired: Mapping[str, Any]) -> Optional[str]:
    """The name the server says this machine should answer to, if it is one."""
    raw = desired.get("hostname")
    if isinstance(raw, str) and HOSTNAME_RE.fullmatch(raw):
        return raw
    return None


def hosts_with(text: str, names: Sequence[str]) -> str:
    """/etc/hosts with one 127.0.1.1 line naming `names`, each once, in order.
    Every other line stays as it was."""
    unique = list(dict.fromkeys(n for n in names if n))
    line = "127.0.1.1\t" + " ".join(unique)
    out: list[str] = []
    placed = False
    for existing in text.splitlines():
        if existing.split()[:1] == ["127.0.1.1"]:
            if not placed:
                out.append(line)
                placed = True
            continue
        out.append(existing)
    if not placed:
        out.append(line)
    return "\n".join(out) + "\n"


def _write_file(path: Path, text: str) -> None:
    """Replaced whole, never half-written: a torn /etc/hosts breaks sudo."""
    temp = path.with_name(f".{path.name}.ccfleet-tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


def apply_hostname(name: str, cfg: MachineConfig, system: System) -> bool:
    """Make the machine answer to `name`. True when it changed.

    /etc/hosts first, naming the new name, the node's id and the name it
    answers to now, so sudo resolves whichever the machine goes by at every
    moment. Once the name has taken, the old one is dropped: it may be the last
    holder's, which is theirs, and /etc/hosts is readable by the next holder.
    Then cloud-init is told to leave both alone at the next boot.
    """
    current = system.hostname()
    if current == name:
        return False
    try:
        text = system.hosts_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    _write_file(system.hosts_path, hosts_with(text, [name, cfg.node_id, current]))
    if not system.set_hostname(name):
        log.error("could not take the name %s; still answering to %s", name, current)
        return False
    # The name has taken. What follows is tidying: a failure in it is said,
    # and never hides the rename from Remote Control, which must follow it.
    try:
        _write_file(system.hosts_path, hosts_with(text, [name, cfg.node_id]))
        if system.cloud_cfg_dir.is_dir():
            _write_file(system.cloud_cfg_dir / CLOUD_CFG_NAME, CLOUD_CFG)
    except OSError as exc:
        log.warning("now answering to %s, but could not tidy up after it: %s",
                    name, exc.__class__.__name__)
    log.info("this machine now answers to %s", name)
    return True


def _systemctl(account: pwd.struct_passwd, system: System, *args: str) -> Optional[int]:
    """`systemctl --user` as the slot's own user, in its own environment."""
    code, _ = system.spawn(["systemctl", "--user", *args], input_text="", limit=4096,
                           timeout=SYSTEMCTL_TIMEOUT_S, user=account.pw_uid,
                           group=account.pw_gid, extra_groups=[],
                           env=slot_env(account), cwd="/")
    return code


def _follow_the_name(account: pwd.struct_passwd, system: System) -> None:
    """One slot, after a rename: its manager re-reads its units — Remote
    Control's `--name %H` is fixed when a unit loads — and a Remote Control
    already running restarts, so claude.ai/code shows the machine's new name.
    One that is not running is left stopped; it starts under the new name."""
    _systemctl(account, system, "daemon-reload")
    if _systemctl(account, system, "is-active", "--quiet", RC_UNIT) == 0:
        if _systemctl(account, system, "restart", RC_UNIT) == 0:
            log.info("%s: Remote Control restarted under the new name", account.pw_name)
        else:
            log.warning("%s: Remote Control did not restart", account.pw_name)


def _take_name(desired: Mapping[str, Any], cfg: MachineConfig, state: Mapping[str, Any],
               system: System) -> bool:
    """Answer to the name the server gives, and have every slot's Remote
    Control follow it. True when the machine was renamed."""
    name = wanted_hostname(desired)
    if name is None:
        return False
    try:
        renamed = apply_hostname(name, cfg, system)
    except OSError as exc:
        log.error("could not rename this machine: %s", exc.__class__.__name__)
        return False
    if renamed:
        for user in [u for u in state.get("slots") or [] if isinstance(u, str)]:
            account = system.lookup(user)
            if account is None or not is_slot_account(account, system.groups_of(account)):
                continue
            _follow_the_name(account, system)
    return renamed


# -- acting ----------------------------------------------------------------------


def run_script(path: Path, user: str, timeout: float,
               system: System) -> tuple[bool, str]:
    """Run slot-add or slot-remove for one user. Returns (worked, why not).

    Output goes to a file rather than a pipe, so a process the script leaves
    running cannot hold this run open waiting for the pipe to close.
    """
    with tempfile.TemporaryFile() as out:
        try:
            proc = system.runner([str(path), "--slot", user], stdout=out,
                                 stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return False, f"{path.name} did not finish within {int(timeout)}s"
        except OSError as exc:
            return False, f"{path.name} could not run ({exc.__class__.__name__})"
        out.seek(0)
        tail = out.read()[-4000:].decode("utf-8", "replace")
    if proc.returncode == 0:
        return True, ""
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return False, (lines[-1] if lines else f"exit {proc.returncode}")[:200]


def act_on_slots(slots: list[dict[str, Any]], state: Mapping[str, Any],
                 cfg: MachineConfig, system: System) -> dict[str, Any]:
    """Provision what was claimed and wipe what was released. Returns new state.

    Wipes go first. Handing a slot back is the half with no recovery if it is
    left undone, and it frees the machine for whatever is claimed next.
    """
    provisioned = dict(state.get("provisioned") or {})
    failed = dict(state.get("provision_failed") or {})
    wipes = dict(state.get("wipe_failed") or {})
    now = system.clock()
    for slot in sorted(slots, key=lambda s: s["state"] != "releasing"):
        user, want = slot["unix_user"], slot["state"]
        if want == "releasing":
            provisioned.pop(user, None)          # that claim is over, however it ended
            if system.lookup(user) is None:
                wipes.pop(user, None)            # nothing left to wipe; reported absent
                continue
            last = wipes.get(user)
            if isinstance(last, Mapping) and now - float(last.get("ts") or 0) < WIPE_RETRY_AFTER_S:
                continue
            ok, why = run_script(cfg.slot_remove, user, SLOT_REMOVE_TIMEOUT_S, system)
            if ok:
                wipes.pop(user, None)
                log.info("wiped %s", user)
            else:
                wipes[user] = {"error": why, "ts": now}
                log.error("could not wipe %s: %s", user, why)
        elif want == "claiming":
            claim = slot["claimed_at"]
            # Once per claim, whichever way it went: the server moves the slot
            # on as soon as it hears, and running the script again for a claim
            # already reported would only repeat the answer.
            if provisioned.get(user) == claim or (failed.get(user) or {}).get("for") == claim:
                continue
            ok, why = run_script(cfg.slot_add, user, SLOT_ADD_TIMEOUT_S, system)
            if ok:
                provisioned[user] = claim
                failed.pop(user, None)
                log.info("provisioned %s", user)
            else:
                failed[user] = {"for": claim, "error": why}
                log.error("could not provision %s: %s", user, why)
        else:
            wipes.pop(user, None)
            if want == "free":
                # A free slot has no claim; anything remembered about one is
                # about a person who no longer holds it.
                provisioned.pop(user, None)
                failed.pop(user, None)
    wanted = {s["unix_user"] for s in slots}
    for table in (provisioned, failed, wipes):
        for user in [u for u in table if u not in wanted]:
            table.pop(user)
    return {**state, "provisioned": provisioned, "provision_failed": failed,
            "wipe_failed": wipes, "slots": [s["unix_user"] for s in slots],
            "slot_states": {s["unix_user"]: s["state"] for s in slots},
            "slot_logins": {s["unix_user"]: s["login"] for s in slots if "login" in s}}


def run_cycle(cfg: MachineConfig, state: Mapping[str, Any], system: System,
              fast: bool = False, abandoned: Optional[Mapping[str, Any]] = None
              ) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """One post, then whatever the reply calls for. Returns (status, desired, state)."""
    users = [u for u in state.get("slots") or [] if isinstance(u, str)]
    turn = int(state.get("quota_turn") or 0)
    # One slot's quota per full run, and none on the fast polls. Each read
    # starts a Claude Code session; six at once on one machine is a spike.
    refresh_for = users[turn % len(users)] if users and not fast else None
    payload = machine_payload(cfg, state, system, refresh_for, fast, abandoned)
    # What each slot said, kept for the fast polls to repeat. Never the sign-in
    # progress: that is news once, and a device token must not sit on disk.
    heard = dict(state.get("heard") or {})
    for entry in payload["slots"]:
        facts = {k: entry[k] for k in SLOT_FACT_KEYS if k in entry}
        if facts:
            heard[entry["unix_user"]] = facts
    status, text = core.send_heartbeat(cfg, payload, system.opener,  # type: ignore[arg-type]
                                       max_reply=MAX_REPLY_BYTES)
    if status != 200:
        log.error("heartbeat rejected: status=%s body=%s", status, text.strip()[:200])
        return status, {}, dict(state)
    desired = core.parse_desired(text)
    state = {**state, "heard": {u: f for u, f in heard.items() if u in users}}
    # Before provisioning, so a claim's Remote Control first registers under
    # its holder's name.
    _take_name(desired, cfg, state, system)
    new_state = act_on_slots(wanted_slots(desired), state, cfg, system)
    # The version this machine's slots should run, checked the way the owner
    # agent checks its own before it reaches an installer. Empty: leave them be.
    new_state["claude_version"] = core.installable_version(desired.get("claude_version")) or ""
    new_state["quota_turn"] = (turn + (0 if fast else 1)) if users else 0
    core.write_state(cfg.state_path, new_state)
    return status, desired, new_state


def signing_in(desired: Mapping[str, Any]) -> dict[str, Any]:
    """The slots somebody is signing into right now: user -> requested_at."""
    return {s["unix_user"]: s["login"]["requested_at"] for s in wanted_slots(desired)
            if "login" in s}


def stay_for_sign_ins(cfg: MachineConfig, state: dict[str, Any], desired: dict[str, Any],
                      system: System) -> int:
    """Stay resident while anybody on this machine is signing in.

    Their sign-in runs in a pane that belongs to this run, and a person is
    watching their page for the URL; a minute between steps reads as broken.
    So poll fast, the way the owner agent does, and send a full report of
    every slot once a minute throughout, so nobody else's claim or release
    waits on somebody's sign-in.

    Bounded twice, and neither bound cuts anybody short. Each attempt gets its
    own window, counted from when this run first saw it — one shared deadline
    gave a sign-in started late in the run almost none. And a run stops taking
    on new attempts after an hour: one that arrives later is never started
    here, and so is not failed here either. The next run starts it fresh, with
    a whole window of its own. The run itself ends once the attempts it did
    take on are finished or out of time.
    """
    started = last_full = system.monotonic()
    first_seen: dict[tuple[str, Any], float] = {}
    # Given up on once, never again this run. The server drops an attempt it
    # is told failed, but if it did not, re-reading the same attempt would
    # otherwise give it up again at once, in a loop with no sleep in it.
    given_up: set[tuple[str, Any]] = set()
    while True:
        now = system.monotonic()
        live = signing_in(desired)
        for attempt in live.items():
            first_seen.setdefault(attempt, now)
        # Arrived after this run stopped taking new ones: left, untouched, for
        # the next run. Never handed to its slot here, so no pane is started
        # that this run's end would kill halfway through.
        later = {attempt for attempt in live.items()
                 if first_seen[attempt] - started >= MAX_RESIDENT_S}
        state = {**state, "slot_logins": {
            user: block for user, block in (state.get("slot_logins") or {}).items()
            if (user, block.get("requested_at")) not in later}}
        current = {user: requested for user, requested in live.items()
                   if (user, requested) not in given_up and (user, requested) not in later}
        if not current:
            return 0
        expired = {user: requested for user, requested in current.items()
                   if now - first_seen[(user, requested)] >= core.LOGIN_WINDOW_S}
        if expired:
            given_up.update(expired.items())
            status, desired, state = _abandon(cfg, state, expired, system)
            if status != 200:
                return 1
            continue
        delay = desired.get("poll_s")
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            delay = core.LOGIN_POLL_MAX_S
        system.sleep(max(core.LOGIN_POLL_MIN_S, min(float(delay), core.LOGIN_POLL_MAX_S)))
        full = system.monotonic() - last_full >= FULL_EVERY_S
        status, desired, state = run_cycle(cfg, state, system, fast=not full)
        if full:
            last_full = system.monotonic()
        if status != 200:
            return 1


def _abandon(cfg: MachineConfig, state: dict[str, Any], expired: Mapping[str, Any],
             system: System) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Give up on these attempts, and only these.

    Each of their slots is asked once more with no sign-in handed on, which is
    what tells it to let its pane go; the server is told they failed, so they
    end there too rather than being handed to the next run to start over.
    Anybody else still signing in carries on untouched.
    """
    log.warning("sign-in unfinished on %s after %.0fs; abandoning",
                ", ".join(sorted(expired)), core.LOGIN_WINDOW_S)
    keep = {user: block for user, block in (state.get("slot_logins") or {}).items()
            if user not in expired}
    return run_cycle(cfg, {**state, "slot_logins": keep}, system, fast=True,
                     abandoned=expired)


def main(argv: Optional[Sequence[str]] = None, system: Optional[System] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccfleet-machine",
        description="Look after a shared machine's slots and report on them.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print the payload instead of sending it")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    env = {**core.load_env_file(Path(args.env_file)), **os.environ}
    try:
        cfg = MachineConfig.from_env(env)
    except core.AgentConfigError as exc:
        print(f"error: {exc} (env file: {args.env_file})", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("error: the machine agent runs as root: it creates and removes slot users",
              file=sys.stderr)
        return 2
    system = system or System()
    if args.print_only:
        print(json.dumps(machine_payload(cfg, core.read_state(cfg.state_path), system),
                         indent=2, sort_keys=True))
        return 0
    lock = core.hold_the_only_run(cfg.state_path)
    if lock is None:
        log.info("another run is already working; leaving it to it")
        return 0
    status, desired, state = run_cycle(cfg, core.read_state(cfg.state_path), system)
    if status != 200:
        return 1
    return stay_for_sign_ins(cfg, state, desired, system)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
