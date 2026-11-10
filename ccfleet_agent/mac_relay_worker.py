"""One GUI-session-owned private relay request, with an out-of-band exit result."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import select
import signal
import struct
import sys
import threading
import time
from pathlib import Path


def sibling(name):
    if name not in {"mac_policy", "mac_credentials", "local_relay"}:
        raise ValueError("unsupported GUI relay worker module")
    spec = importlib.util.spec_from_file_location(
        "ccfleet_gui_worker_" + name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ValueError("GUI relay runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status_snapshot(home: Path) -> dict:
    answer = {"available": False, "expires_in_seconds": None,
              "binding_matches": False, "policy_enabled": False}
    try:
        policy = sibling("mac_policy")
        before = policy.policy(home)
        answer["policy_enabled"] = before["enabled"]
        relay = sibling("local_relay")
        relay.MAC_PRIVATE_OWNER = True
        answer["binding_matches"] = relay.bound_account(home) == before["bound_fp"]
        if not answer["binding_matches"]:
            return answer
        oauth = sibling("mac_credentials").read_oauth(home)
        token, expiry = oauth.get("accessToken"), oauth.get("expiresAt")
        valid = (isinstance(token, str) and 1 <= len(token) <= 8192 and token.isascii()
                 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token))
        numeric = type(expiry) in (int, float) and math.isfinite(expiry) and 0 < expiry < 1e14
        answer["available"] = valid and numeric
        if numeric:
            answer["expires_in_seconds"] = int(expiry / 1000 - time.time())
        token = None
        oauth.clear()
        if policy.policy(home) != before:
            answer.update(available=False, binding_matches=False)
    except (OSError, ValueError):
        pass
    return answer


def run(role: str) -> int:
    if sys.platform != "darwin" or role not in ("--device", "--gateway", "--status"):
        return 2
    policy = sibling("mac_policy")
    home = Path(policy.authenticated_owner().pw_dir)
    if role == "--status":
        print(json.dumps(status_snapshot(home), sort_keys=True))
        return 0
    policy.require_enabled(home)
    relay = sibling("local_relay")
    relay.MAC_PRIVATE_OWNER = True
    return relay.main(["--protocol-v2" if role == "--device" else "--gateway-protocol-v1"])


def start_parent_watch(descriptor: int) -> None:
    if os.getpgrp() != os.getpid() or select.select([descriptor], [], [], 0)[0]:
        raise ValueError("private relay parent lease is unavailable")

    def watch():
        try:
            os.read(descriptor, 1)
        except OSError:
            pass
        # We are the verified, still-live leader of this new process group.
        # Parent death cancels the worker and its credential subprocesses.
        os.killpg(os.getpid(), signal.SIGKILL)

    threading.Thread(target=watch, daemon=True).start()


_RESULT_PUBLISHED = False
_LEASE_FD = None
_WATCH_STARTED = False


def main(argv=None) -> int:
    global _RESULT_PUBLISHED, _LEASE_FD, _WATCH_STARTED
    _RESULT_PUBLISHED = _WATCH_STARTED = False
    args = list(sys.argv[1:] if argv is None else argv)
    if (len(args) != 5 or args[0] not in ("--device", "--gateway", "--status")
            or args[1] != "--status-fd" or not args[2].isdigit()
            or args[3] != "--lease-fd" or not args[4].isdigit()):
        return 2
    descriptor = int(args[2])
    lease = int(args[4])
    if (not 3 <= descriptor <= 65535 or not 3 <= lease <= 65535 or lease == descriptor):
        return 2
    _LEASE_FD = lease
    os.set_inheritable(lease, False)
    # Native credential subprocesses must never inherit this private control FD.
    os.set_inheritable(descriptor, False)
    code = 255
    try:
        start_parent_watch(lease)
        _WATCH_STARTED = True
        code = run(args[0])
        sys.stdout.buffer.flush()
    except BaseException:
        code = 255
    try:
        data = memoryview(struct.pack("!i", code if code in (0, 2) else 255))
        while data:
            count = os.write(descriptor, data)
            if count <= 0:
                break
            data = data[count:]
        _RESULT_PUBLISHED = not data
    except OSError:
        code = 255
    finally:
        os.close(descriptor)
    return code


if __name__ == "__main__":
    result = main()
    if _RESULT_PUBLISHED and _LEASE_FD is not None and _WATCH_STARTED:
        # The watcher owns the sole lease read. It cancels this owned group on
        # parent death; normal completion waits for explicit daemon cleanup.
        threading.Event().wait()
    os._exit(result)
