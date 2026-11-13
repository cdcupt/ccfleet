"""Private Mac owner heartbeat and narrowly typed inference-key convergence.

No project/history scan, account login, version update, user lifecycle, terminal
management, or arbitrary server command is implemented. Native Claude alone may
renew its own Keychain credential through a bounded native safe-mode /usage probe.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


def sibling(name: str):
    if name not in {"mac_policy", "mac_credentials", "mac_keys", "local_relay", "relay_metrics",
                    "agent", "mac_native_probe"}:
        raise ValueError("unsupported private owner module")
    module_name = "ccfleet_private_owner_" + name
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ValueError("private owner runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


AGENT_VERSION = "private-mac-1"
RENEW_EARLY = 600
RENEW_RETRY = 300
MAX_REPLY = 64 * 1024
QUOTA_INTERVAL = 300


def _number(value):
    return type(value) in (int, float) and 0 <= value < 1e14


def load_config(home: Path) -> dict[str, Any]:
    policy = sibling("mac_policy")
    value = policy.read_json(home, "agent.json")
    required = {"version", "url", "node_id", "node_token", "native_claude"}
    if (set(value) - {"native_tmux"} != required or value.get("version") != 1
            or not isinstance(value.get("node_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value["node_id"])
            or not isinstance(value.get("node_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{16,512}", value["node_token"])):
        raise ValueError("private owner agent configuration is invalid")
    url = value.get("url")
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ("", "/")
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise ValueError("private owner controller URL is invalid")
    value["url"] = url.rstrip("/")
    for key in ("native_claude", "native_tmux"):
        path = value.get(key)
        if path is not None and (not isinstance(path, str) or not Path(path).is_absolute()
                                 or any(ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ValueError("private owner native executable is invalid")
    return value


def _native_path(path: str) -> str:
    # Native installers use a versioned symlink. Validate the resolved binary
    # and all of its real parents; no server-provided executable is accepted.
    resolved = Path(path).resolve(strict=True)
    policy = sibling("mac_policy")
    directory = policy.open_directory(resolved.parent)
    try:
        descriptor = os.open(resolved.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or info.st_mode & 0o022 or not info.st_mode & 0o111):
                raise ValueError("private owner native executable is unsafe")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    return str(resolved)


def native_status(cfg: dict, home: Path) -> tuple[dict[str, Any], Optional[str]]:
    adapter = sibling("mac_credentials")
    try:
        path = _native_path(cfg["native_claude"])
        code, raw = adapter.bounded_output([path, "auth", "status"], home, timeout=20)
        status = json.loads(raw) if code in (0, 1) else {}
        version_code, version_raw = adapter.bounded_output([path, "--version"], home, timeout=10,
                                                          limit=4096)
        match = re.search(rb"(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+(?![0-9.])", version_raw)
        version = match.group().decode() if version_code == 0 and match else None
    except (OSError, ValueError, RecursionError):
        return {}, None
    if not isinstance(status, dict):
        return {}, version
    facts = {}
    allowed = {"authMethod": {"claude.ai", "api_key"},
               "apiProvider": {"firstParty", "bedrock", "vertex", "foundry"},
               "subscriptionType": {"free", "pro", "max", "team", "enterprise"}}
    for source, target in (("loggedIn", "logged_in"), ("authMethod", "auth_method"),
                           ("apiProvider", "api_provider"),
                           ("subscriptionType", "subscription_type")):
        value = status.get(source)
        if (source == "loggedIn" and isinstance(value, bool)
                or isinstance(value, str) and value in allowed.get(source, set())):
            facts[target] = value
    return facts, version


def credential_facts(home: Path, status: dict[str, Any]) -> dict[str, Any]:
    relay = sibling("local_relay")
    relay.MAC_PRIVATE_OWNER = True
    facts = {"store": "keychain", "present": False, "logged_in": False}
    try:
        bound = relay.bound_account(home)
        oauth = sibling("mac_credentials").read_oauth(home)
        expiry = oauth.get("expiresAt")
        token = oauth.get("accessToken")
        valid = (isinstance(token, str) and 1 <= len(token) <= 8192 and token.isascii()
                 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token))
        facts.update(account_fp=bound, bound_fp=bound, present=valid)
        if _number(expiry):
            facts["expires_at"] = expiry
        facts.update(status)
        facts["logged_in"] = (valid and status.get("logged_in") is True
                              and status.get("auth_method") == "claude.ai"
                              and status.get("api_provider") == "firstParty")
    except (OSError, ValueError):
        pass
    return facts


def _native_renewal_probe(cfg: dict, home: Path, bound: str) -> dict[str, Any]:
    """Run native safe-mode /usage; never implement OAuth refresh ourselves."""
    policy = sibling("mac_policy")
    relay = sibling("local_relay")
    relay.MAC_PRIVATE_OWNER = True
    try:
        claude = _native_path(cfg["native_claude"])
        base = policy.open_directory(policy.directory(home), private=True)
        try:
            try:
                os.mkdir("native-probe", 0o700, dir_fd=base)
            except FileExistsError:
                pass
        finally:
            os.close(base)
        probe = home.joinpath(*policy.CONFIG_PARTS, "native-probe")
        descriptor = policy.open_directory(probe, private=True)
        os.close(descriptor)
        return sibling("mac_native_probe").run(
            claude, home, probe, timeout=45,
            before_start=lambda: relay.bound_account(home) == bound)
    except (OSError, ValueError):
        return {"outcome": "native_probe_busy", "probe_started": False,
                "cleanup_confirmed": True, "usage_seen": False}


def maintain_native(cfg: dict, home: Path, facts: dict[str, Any], now: float) -> dict[str, Any]:
    policy = sibling("mac_policy")
    expiry = facts.get("expires_at")
    bound = facts.get("bound_fp")
    if not _number(expiry) or not isinstance(bound, str):
        return {"state": "blocked", "reason": "credential_unavailable", "checked_at": now}
    try:
        previous = policy.read_json(home, "maintenance.json")
    except ValueError:
        previous = {}
    if previous.get("bound_fp") == bound and previous.get("cleanup_confirmed") is False:
        return {"state": "blocked", "reason": "native_probe_busy", "checked_at": now}
    if expiry / 1000 > now + RENEW_EARLY:
        return {"state": "current", "checked_at": now}
    next_attempt = previous.get("next_attempt_at")
    if previous.get("bound_fp") == bound and _number(next_attempt) and next_attempt > now:
        return {"state": "retrying", "reason": "native_refresh_unconfirmed", "checked_at": now}
    # Store only a bound fingerprint, timestamps and fixed outcomes, never token
    # bytes, token hashes, or captured native output.
    reservation = {**(previous if previous.get("bound_fp") == bound else {}),
                   "version": 1, "bound_fp": bound, "last_attempt_at": now,
                   "next_attempt_at": now + RENEW_RETRY, "cleanup_confirmed": False}
    policy.write_json(home, "maintenance.json", reservation)
    observed = _native_renewal_probe(cfg, home, bound)
    fresh = credential_facts(home, {key: facts[key] for key in
                                  ("logged_in", "auth_method", "api_provider", "subscription_type")
                                  if key in facts})
    after = fresh.get("expires_at")
    cleaned = observed.get("cleanup_confirmed") is True
    renewed = (cleaned and fresh.get("bound_fp") == bound and _number(after) and after > expiry
               and after / 1000 > time.time() + 30)
    report = {"state": "renewed" if renewed else "retrying" if cleaned else "blocked",
              "checked_at": now,
              "outcome": "native_refreshed" if renewed else observed["outcome"]}
    if not renewed:
        report["reason"] = observed["outcome"] if cleaned else "native_probe_busy"
    policy.write_json(home, "maintenance.json", {**reservation, **report,
                                                "cleanup_confirmed": cleaned})
    return report


def _quota_cache(home: Path, bound: str, now: float) -> dict[str, Any]:
    value = sibling("agent").cached_usage(home / ".claude", global_config=home / ".claude.json",
                                         account_fp=bound)
    return value if value and value["checked_at"] <= now + 60 else {}


def quota_report(cfg: dict, home: Path, facts: dict[str, Any], now: float,
                 *, maintenance: bool = True) -> dict[str, Any]:
    """Read native account quota, refreshing only through the existing safe /usage probe.

    A recent native renewal also satisfies the five-minute probe budget. No OAuth request,
    model request, credential copy or user project is introduced by this reader.
    """
    bound = facts.get("bound_fp")
    if (facts.get("logged_in") is not True or not isinstance(bound, str)
            or facts.get("account_fp") != bound):
        return {}
    report = _quota_cache(home, bound, now)
    if not maintenance:
        return report
    policy = sibling("mac_policy")
    try:
        previous = policy.read_json(home, "maintenance.json")
    except ValueError:
        previous = {}
    if previous.get("bound_fp") != bound:
        previous = {}
    wanted = previous.get("quota_requested_at")
    wanted = wanted if _number(wanted) and 0 < wanted <= now + 60 else None
    ack = previous.get("quota_refresh")
    ack = ack if isinstance(ack, dict) else {}
    answered = ack.get("requested_at")
    answered = answered if _number(answered) else 0
    asked = wanted is not None and wanted > answered
    due = not report or now - report["checked_at"] >= QUOTA_INTERVAL
    attempts = [previous.get("last_attempt_at"), previous.get("quota_attempted_at")]
    throttled = any(_number(value) and now - value < QUOTA_INTERVAL for value in attempts)
    cleaned = previous.get("cleanup_confirmed") is not False
    outcome = "quota_read" if report and not due else "native_probe_busy"
    if due and not throttled and cleaned:
        reservation = {**previous, "version": 1, "bound_fp": bound,
                       "quota_attempted_at": now, "cleanup_confirmed": False}
        policy.write_json(home, "maintenance.json", reservation)
        observed = _native_renewal_probe(cfg, home, bound)
        report = _quota_cache(home, bound, time.time())
        cleaned = observed.get("cleanup_confirmed") is True
        outcome = ("quota_read" if cleaned and report and
                   report["checked_at"] >= now - 90 else observed.get("outcome"))
        previous = {**reservation, "cleanup_confirmed": cleaned}
        policy.write_json(home, "maintenance.json", previous)
    if asked:
        refresh = sibling("agent")._quota_refresh(wanted, max(now, time.time()), outcome)
        if refresh:
            ack = refresh["refresh"]
            previous = {**previous, "version": 1, "bound_fp": bound, "quota_refresh": ack}
            policy.write_json(home, "maintenance.json", previous)
    if ack:
        # The helper validates fixed outcomes and numeric timestamps before the
        # persisted acknowledgement can cross the heartbeat boundary.
        report.update(sibling("agent")._quota_refresh(
            ack.get("requested_at"), ack.get("attempted_at"), ack.get("outcome")))
    return report


def remember_quota_request(home: Path, request: Any, *, now: Optional[float] = None) -> None:
    """Accept one typed refresh timestamp; generic controller commands stay ignored."""
    now = time.time() if now is None else now
    if not isinstance(request, dict) or set(request) != {"requested_at"}:
        return
    wanted = request["requested_at"]
    if not _number(wanted) or not 0 < wanted <= now + 60:
        return
    policy = sibling("mac_policy")
    bound = policy.policy(home)["bound_fp"]
    try:
        previous = policy.read_json(home, "maintenance.json")
    except ValueError:
        previous = {}
    if previous.get("bound_fp") != bound:
        previous = {}
    remembered = previous.get("quota_requested_at")
    if _number(remembered) and wanted <= remembered:
        return
    policy.write_json(home, "maintenance.json", {**previous, "version": 1, "bound_fp": bound,
                                                "quota_requested_at": wanted})


def build_payload(cfg: dict, home: Path, *, now: Optional[float] = None,
                  maintenance: bool = True) -> dict[str, Any]:
    now = time.time() if now is None else now
    policy = sibling("mac_policy")
    owner = policy.authenticated_owner(home)
    status, version = native_status(cfg, home)
    facts = credential_facts(home, status)
    if maintenance:
        facts["renewal"] = maintain_native(cfg, home, facts, now)
        # Native maintenance may have renewed the Keychain record.
        facts.update(credential_facts(home, status))
    slot: dict[str, Any] = {"unix_user": owner.pw_name, "present": True,
                            "claude": {"version": version}, "credentials": facts}
    quota = quota_report(cfg, home, facts, now, maintenance=maintenance)
    if quota:
        slot["quota"] = quota
    if facts.get("logged_in") is True and isinstance(facts.get("bound_fp"), str):
        try:
            metrics = sibling("relay_metrics")
            slot["relay"] = metrics.validate_report(metrics.report(home, facts["bound_fp"], now))
        except (OSError, ValueError):
            pass
    return {"node_id": cfg["node_id"], "ts": now, "agent_version": AGENT_VERSION,
            "mode": "private_owner", "claude": {"version": version},
            "credentials": facts, "slots": [slot], **({"quota": quota} if quota else {})}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_heartbeat(cfg: dict, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(cfg["url"] + "/api/heartbeat", method="POST",
                                     data=json.dumps(payload, separators=(",", ":")).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + cfg["node_token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(MAX_REPLY + 1)
            if response.status != 200 or len(raw) > MAX_REPLY:
                return int(response.status), {}
        policy = sibling("mac_policy")
        value = json.loads(raw, object_pairs_hook=policy._pairs,
                           parse_constant=policy._nonfinite)
        return 200, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, {}
    except (OSError, ValueError, urllib.error.URLError):
        return 0, {}


def converge_access(home: Path, access: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    policy, keys = sibling("mac_policy"), sibling("mac_keys")
    owner = policy.authenticated_owner(home)
    disabled = {"version": 1, "unix_user": owner.pw_name, "enabled": False,
                "device_public_keys": [], "gateway_public_keys": []}
    try:
        snapshot = keys.validate_snapshot(access)
        if snapshot["unix_user"] != owner.pw_name or not policy.policy(home)["enabled"]:
            snapshot = disabled
    except (ValueError, OSError):
        snapshot = disabled
    result = keys.reconcile(snapshot, home=home)
    policy.write_json(home, "grants.json", {"version": 1, "clock": policy.LEASE_CLOCK_FORMAT,
                      "received_at": time.time() if now is None else now,
                      "received_monotonic": policy.lease_clock(), "access": snapshot})
    return result


def main(argv: Optional[list[str]] = None) -> int:
    if (list(sys.argv[1:] if argv is None else argv) or sys.platform != "darwin"):
        print("unsupported private owner agent invocation", file=sys.stderr)
        return 2
    lock = base = None
    try:
        policy = sibling("mac_policy")
        home = Path(policy.authenticated_owner().pw_dir)
        if Path.home() != home or os.environ.get("CLAUDE_CONFIG_DIR"):
            raise ValueError("private owner requires its native default home")
        cfg = load_config(home)
        base = policy.open_directory(policy.directory(home), private=True)
        lock = os.open("agent.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=base)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("private owner agent lock is unsafe")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Only fixed typed grants and quota-read timestamps are acted on. Generic
        # desired state, login, upgrade, wipe and arbitrary commands are ignored.
        status, response = post_heartbeat(cfg, build_payload(cfg, home))
        if status != 200:
            print("private owner heartbeat unavailable", file=sys.stderr)
            return 1
        converge_access(home, response.get("private_owner_access"))
        remember_quota_request(home, response.get("private_owner_quota"))
        print("private owner heartbeat accepted", file=sys.stderr)
        return 0
    except (OSError, ValueError):
        print("private owner agent configuration or runtime is unavailable", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            os.close(lock)
        if base is not None:
            os.close(base)


if __name__ == "__main__":
    raise SystemExit(main())
