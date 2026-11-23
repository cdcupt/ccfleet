"""Temporary, device-bound Antigravity session profiles on the private Mac."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from . import mac_policy
else:
    import mac_policy

TTL = 300
MAX_PROFILES = 4
MAX_FILES = 4096
MAX_BYTES = 32 * 1024 * 1024
TOKEN = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[0-9a-f]{48}")
NO_SESSION = "The previous Gemini session cannot be resumed."


def root_path() -> Path:
    # A short absolute path keeps the agent's Unix-domain MCP socket within SUN_LEN.
    return Path("/private/tmp") / f"ccfleet-gr-{os.getuid()}"


def _directory(path: Path) -> int:
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("Unsafe Gemini recovery directory.")
    return fd


@contextlib.contextmanager
def _registry(root):
    root.mkdir(mode=0o700, exist_ok=True)
    fd = _directory(root)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _claim(path):
    fd = _directory(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _name(fingerprint, token):
    if (
        not isinstance(fingerprint, bytes)
        or len(fingerprint) != 32
        or not isinstance(token, str)
        or not TOKEN.fullmatch(token)
    ):
        raise ValueError(NO_SESSION)
    return hashlib.sha256(
        b"ccfleet-gemini-resume-v1\0" + fingerprint + bytes.fromhex(token)
    ).hexdigest()[:48]


def _state(path):
    fd = os.open(path / "detached", os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise ValueError(NO_SESSION)
        data = json.loads(stream.read(4097))
    if (
        not isinstance(data, dict)
        or set(data) != {"device", "sid", "model", "expires", "tick"}
        or not isinstance(data["device"], str)
        or not TOKEN.fullmatch(data["device"])
        or not isinstance(data["sid"], str)
        or not 0 < len(data["sid"]) <= 512
        or not isinstance(data["model"], str)
        or not data["model"].startswith("gemini-")
        or type(data["expires"]) not in (float, int)
        or type(data["tick"]) not in (float, int)
        or not 0 < data["expires"] - time.time() <= TTL
        or not 0 < data["tick"] - mac_policy.lease_clock() <= TTL
    ):
        raise ValueError(NO_SESSION)
    return data


def _sweep(root, permitted):
    """Remove only unlocked, expired, abandoned, or revoked profiles, without following links."""
    count = 0
    for path in root.iterdir():
        if not NAME.fullmatch(path.name):
            continue
        try:
            fd = _claim(path)
        except FileNotFoundError:
            continue
        except BlockingIOError:  # A live or currently resuming worker owns the profile.
            count += 1
            continue
        except (OSError, ValueError):
            # A malformed entry cannot prevent unrelated expired leases from being cleaned.
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISDIR(path.lstat().st_mode):
                    path.unlink()
            continue
        try:
            try:
                keep = permitted(bytes.fromhex(_state(path)["device"]))
            except (OSError, ValueError):
                keep = False
            if keep:
                count += 1
            else:
                shutil.rmtree(path)
        finally:
            os.close(fd)
    return count


def _bounded_size(folder):
    pending = [folder]
    total = entries = 0
    while pending:
        with os.scandir(pending.pop()) as listing:
            for e in listing:
                info = e.stat(follow_symlinks=False)
                entries += 1
                total += info.st_size
                if entries > MAX_FILES or total > MAX_BYTES:
                    return False
                if stat.S_ISDIR(info.st_mode):
                    pending.append(e.path)
    return True


WATCHER = "watcher"


def _watch_identity(path, directory, guard):
    try:
        here = path.lstat()
        marker = os.stat(WATCHER, dir_fd=directory, follow_symlinks=False)
        return (
            stat.S_ISDIR(here.st_mode)
            and here.st_uid == os.getuid()
            and stat.S_IMODE(here.st_mode) == 0o700
            and os.path.samestat(here, os.fstat(directory))
            and stat.S_ISREG(marker.st_mode)
            and marker.st_uid == os.getuid()
            and marker.st_nlink == 1
            and stat.S_IMODE(marker.st_mode) == 0o600
            and marker.st_size == 0
            and os.path.samestat(marker, os.fstat(guard))
        )
    except OSError:
        return False


@contextlib.contextmanager
def _watch_lock(path):
    """Hold the watcher lock before forking. The child inherits this same open file description."""
    with contextlib.ExitStack() as resources:
        try:
            directory = _directory(path)
            resources.callback(os.close, directory)
            guard = os.open(
                WATCHER,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            resources.callback(os.close, guard)
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pair = (directory, guard) if _watch_identity(path, directory, guard) else None
        except (OSError, ValueError):
            pair = None
        yield pair


def _schedule(path, spawn):
    with _watch_lock(path) as pair:
        if pair is None:
            return False
        spawn(path.name, *pair)
        return True


def start_reaper(name, directory, guard):
    # Neither an enrollment secret nor a model session ID is passed to the subprocess.
    subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("gemini_editor_slot.py")),
            "reap",
            name,
            str(directory),
            str(guard),
        ],
        start_new_session=True,
        pass_fds=(directory, guard),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_reapers(root, permitted, spawn=start_reaper):
    if not root.exists():
        return
    with _registry(root):
        _sweep(root, permitted)
        for path in root.iterdir():
            if NAME.fullmatch(path.name):
                _schedule(path, spawn)


def reap(name, permitted, *, root=None, interval=2.0, inherited=None):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return 2
    root = root or root_path()
    path = root / name
    with contextlib.ExitStack() as resources:
        if inherited is None:
            pair = resources.enter_context(_watch_lock(path))
            if pair is None:
                return 0
        else:
            pair = inherited
            for fd in pair:
                resources.callback(os.close, fd)
            if not _watch_identity(path, *pair):
                return 0
            try:
                fcntl.flock(pair[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
        while _watch_identity(path, *pair):
            with _registry(root):
                # A replaced or chmod'd registry entry is outside this watcher's authority.
                if not _watch_identity(path, *pair):
                    return 0
                _sweep(root, permitted)
                if not _watch_identity(path, *pair):
                    return 0
            time.sleep(interval)
        return 0


class Profile:
    """An exclusive native profile. Its guard and metadata are outside the Google sandbox."""

    def __init__(self, folder, lock, device, token, state=None):
        self.folder, self.lock = folder, lock
        self.device, self.token = device, token
        self.state = state
        self.sandbox = folder / "s"
        self.sid = state["sid"] if state else None
        self.model = state["model"] if state else None

    @classmethod
    def create(cls, root, device, permitted, spawn=start_reaper):
        token = secrets.token_hex(32)
        path = root / _name(device, token)
        with _registry(root):
            if _sweep(root, permitted) >= MAX_PROFILES:
                return None  # Ordinary sessions can still open when all recovery holds are in use.
            path.mkdir(mode=0o700)
            fd = _claim(path)
            try:
                (path / "s").mkdir(mode=0o700)
                if not _schedule(path, spawn):
                    raise OSError("Gemini recovery cleanup is unavailable.")
            except BaseException:
                shutil.rmtree(path)
                os.close(fd)
                raise
        return cls(path, fd, device, token)

    @classmethod
    def resume(cls, root, device, token, permitted):
        path = root / _name(device, token)
        try:
            with _registry(root):
                fd = _claim(path)
                try:
                    data = _state(path)
                    if not hmac.compare_digest(data["device"], device.hex()) or not permitted(
                        device
                    ):
                        raise ValueError(NO_SESSION)
                    (path / "detached").unlink()
                except BaseException:
                    os.close(fd)
                    raise
        except (OSError, ValueError):
            raise ValueError(NO_SESSION) from None
        return cls(path, fd, device, token, data)

    def finish(self, retain, permitted, *, renew=True):
        try:
            with _registry(self.folder.parent):
                try:
                    info = self.folder.lstat()
                except FileNotFoundError:
                    return
                if not os.path.samestat(os.fstat(self.lock), info):
                    return  # The old worker must never delete or write into a replacement lease.
                if retain and self._detach(permitted, renew):
                    return
                shutil.rmtree(self.folder)
        finally:
            os.close(self.lock)

    def _detach(self, permitted, renew):
        try:
            if not (
                self.sid and self.model and permitted(self.device) and _bounded_size(self.sandbox)
            ):
                return False
            now, tick = time.time(), mac_policy.lease_clock()
            expires, until = now + TTL, tick + TTL
            if self.state and not renew:
                # A second drop while reattaching does not reset the original expiry.
                expires, until = self.state["expires"], self.state["tick"]
                if not 0 < expires - now <= TTL or not 0 < until - tick <= TTL:
                    return False
            data = {
                "device": self.device.hex(),
                "sid": self.sid,
                "model": self.model,
                "expires": expires,
                "tick": until,
            }
            fd = os.open(
                "detached",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.lock,
            )
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            return True
        except (OSError, ValueError):
            return False
