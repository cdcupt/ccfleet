"""Signed, immutable local client bundles; never reads pairing or Claude data.

The trusted Ed25519 public key comes from the installed client/bootstrap, never
from downloaded JSON. A single atomic launcher replacement activates a complete
version and records its rollback target and version high-water mark.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

RAW_BASE = "https://raw.githubusercontent.com/cdcupt/ccfleet/"
RELEASE_BASE = "https://github.com/cdcupt/ccfleet/releases/download/"
DEFAULT_CHANNEL_URL = RAW_BASE + "main/laptop/releases/stable.json"
DEFAULT_CHANNEL_SIGNATURE_URL = DEFAULT_CHANNEL_URL + ".sig"
NAMESPACE = "ccfleet-release"
PRINCIPAL = "ccfleet"
MAX_MANIFEST = 128 * 1024
MAX_SIGNATURE = 8192
MAX_FILE = 4 * 1024 * 1024
MAX_BUNDLE = 16 * 1024 * 1024
MAX_CHANNEL_AGE = 90 * 24 * 60 * 60
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]{0,7})(?:\.(?:0|[1-9][0-9]{0,7})){1,3}\Z")
ID_RE = re.compile(r"(?:legacy-[0-9a-f]{64}|[0-9.]+-[0-9a-f]{64})\Z")
ROOT_NAME = ".ccfleet-releases"
LAUNCHER_PREFIX = "# ccfleet-release-state: "
HELPERS = {
    "PROJECT_FILES_SHA256": "project_files",
    "LIVE_FILES_SHA256": "live_files",
    "LIVE_CLIENT_SHA256": "live_client",
    "INFERENCE_CLIENT_SHA256": "inference_client",
    "CLIENT_EXPERIENCE_SHA256": "client_experience",
    "LOCAL_JOBS_SHA256": "local_jobs",
    "CLIENT_RELEASE_SHA256": "client_release",
}
SOURCES = {"laptop/ccfleet", *(f"ccfleet_agent/{name}.py" for name in HELPERS.values())}
Fetcher = Callable[[str, int], bytes]


class ReleaseError(ValueError):
    """A release could not be authenticated, verified or installed safely."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("duplicate release JSON field")
        result[key] = value
    return result


def _json(raw: bytes, limit: int = MAX_MANIFEST) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ReleaseError("release metadata exceeds its size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda value: None)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReleaseError("invalid release JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseError("release metadata must be an object")
    return value


def canonical(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode()


def version_key(version: Any) -> tuple[int, ...]:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseError("release version must contain 2 to 4 canonical numeric parts")
    return tuple([*(int(part) for part in version.split(".")), *([0] *
                 (4 - len(version.split("."))))])


def approved_url(url: str, *, channel: bool = False) -> str:
    """Literal URLs only: no credentials, query, encoding, fragments or redirects."""
    if channel and url in {DEFAULT_CHANNEL_URL, DEFAULT_CHANNEL_SIGNATURE_URL}:
        return url
    if not isinstance(url, str):
        raise ReleaseError("invalid release URL")
    if url.startswith(RAW_BASE):
        tail = url[len(RAW_BASE):]
        revision, separator, path = tail.partition("/")
        if separator and REVISION_RE.fullmatch(revision) and _plain_path(path):
            return url
    elif url.startswith(RELEASE_BASE):
        tail = url[len(RELEASE_BASE):]
        version, separator, path = tail.partition("/")
        if separator and VERSION_RE.fullmatch(version) and _plain_path(path):
            return url
    raise ReleaseError("release URL must use the fixed repository and an immutable revision")


def _plain_path(path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", path)
                and all(part not in ("", ".", "..") for part in path.split("/")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url: str, limit: int) -> bytes:
    approved_url(url, channel=True)
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    # Avoid user proxy configuration changing the authenticated release origin.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if response.getcode() != 200 or response.geturl() != url:
                raise ReleaseError("release download was redirected or rejected")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ReleaseError("encoded release downloads are not supported")
            deadline = time.monotonic() + 60
            parts, remaining = [], limit + 1
            while remaining:
                if time.monotonic() >= deadline:
                    raise ReleaseError("release download exceeded its total read deadline")
                part = response.read1(min(64 * 1024, remaining))
                if not part:
                    break
                parts.append(part)
                remaining -= len(part)
            raw = b"".join(parts)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError("release download failed; installed client was not changed") from exc
    if not 0 < len(raw) <= limit:
        raise ReleaseError("release download exceeds its size limit")
    return raw


def trusted_public_key(key: str) -> str:
    try:
        parts = key.strip().split()
        if len(parts) != 2 or parts[0] != "ssh-ed25519":
            raise ValueError
        blob = base64.b64decode(parts[1], validate=True)
        kind_size = struct.unpack("!I", blob[:4])[0]
        kind = blob[4:4 + kind_size]
        key_size = struct.unpack("!I", blob[4 + kind_size:8 + kind_size])[0]
        if kind != b"ssh-ed25519" or key_size != 32 or len(blob) != 8 + kind_size + key_size:
            raise ValueError
        return " ".join(parts)
    except (AttributeError, ValueError, struct.error) as exc:
        raise ReleaseError("a pinned Ed25519 release public key is required") from exc


def verify_signature(raw: bytes, signature: bytes, trusted_key: str) -> None:
    key = trusted_public_key(trusted_key)
    if not 0 < len(raw) <= MAX_MANIFEST or not 0 < len(signature) <= MAX_SIGNATURE:
        raise ReleaseError("invalid signed release metadata size")
    executable = shutil.which("ssh-keygen", path=os.defpath)
    if executable is None:
        raise ReleaseError("OpenSSH ssh-keygen is required to verify releases")
    with tempfile.TemporaryDirectory(prefix="ccfleet-signature-") as temporary:
        directory = Path(temporary)
        signers, detached = directory / "allowed_signers", directory / "manifest.sig"
        signers.write_text(f'{PRINCIPAL} namespaces="{NAMESPACE}" {key}\n')
        detached.write_bytes(signature)
        try:
            result = subprocess.run(
                [executable, "-Y", "verify", "-f", str(signers), "-I", PRINCIPAL,
                 "-n", NAMESPACE, "-s", str(detached)], input=raw,
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError("release signature verification could not complete") from exc
    if result.returncode != 0:
        raise ReleaseError("release signature does not match the pinned signing key")


def parse_manifest(raw: bytes) -> dict[str, Any]:
    value = _json(raw)
    if set(value) != {"schema", "version", "revision", "files"} \
            or type(value["schema"]) is not int or value["schema"] != 1:
        raise ReleaseError("unsupported release manifest schema")
    version_key(value["version"])
    revision = value["revision"]
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ReleaseError("release revision must be a full immutable Git commit")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= len(SOURCES):
        raise ReleaseError("invalid release file list")
    seen, total = set(), 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "url", "sha256", "size"}:
            raise ReleaseError("invalid release file entry")
        path, size, digest = entry["path"], entry["size"], entry["sha256"]
        if not isinstance(path, str) or path not in SOURCES or path in seen:
            raise ReleaseError("unknown or duplicate release file")
        if type(size) is not int or not 0 < size <= MAX_FILE \
                or not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
            raise ReleaseError("invalid release file size or digest")
        # Source files are tied to the manifest's revision, not merely a safe host.
        if entry["url"] != RAW_BASE + revision + "/" + path:
            raise ReleaseError("release file URL does not match its source revision")
        seen.add(path)
        total += size
    if "laptop/ccfleet" not in seen or total > MAX_BUNDLE:
        raise ReleaseError("release bundle is missing its client or exceeds its size limit")
    return value


def verify_manifest(raw: bytes, signature: bytes, trusted_key: str) -> dict[str, Any]:
    verify_signature(raw, signature, trusted_key)
    return parse_manifest(raw)


def helper_digests(source: bytes) -> dict[str, str]:
    try:
        tree = ast.parse(source)
        compile(tree, "ccfleet", "exec")
    except (ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
        raise ReleaseError("release client is not valid Python") from exc
    found = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        for name in names:
            if not name.endswith("_SHA256"):
                continue
            if name not in HELPERS or name in found or not isinstance(node, ast.Assign) \
                    or len(targets) != 1 or not isinstance(node.value, ast.Constant) \
                    or not isinstance(node.value.value, str) \
                    or not HASH_RE.fullmatch(node.value.value):
                raise ReleaseError("client helper digests must be known, unique SHA256 literals")
            found[name] = node.value.value
    if "PROJECT_FILES_SHA256" not in found or bool(found.get("LIVE_FILES_SHA256")) != \
            bool(found.get("LIVE_CLIENT_SHA256")):
        raise ReleaseError("release client declares an incomplete helper set")
    return found


def installed_name(path: str, digest: str) -> str:
    if path == "laptop/ccfleet":
        return "ccfleet"
    return "ccfleet-" + Path(path).stem.replace("_", "-") + "-" + digest + ".py"


def verify_bundle(manifest: dict[str, Any], contents: dict[str, bytes]) -> None:
    entries = {entry["path"]: entry for entry in manifest["files"]}
    if contents.keys() != entries.keys():
        raise ReleaseError("release bundle is incomplete")
    for path, source in contents.items():
        entry = entries[path]
        if len(source) != entry["size"] or hashlib.sha256(source).hexdigest() != entry["sha256"]:
            raise ReleaseError("release file checksum or size mismatch")
        try:
            compile(source, path, "exec")
        except (SyntaxError, ValueError, UnicodeError, RecursionError) as exc:
            raise ReleaseError("release contains invalid Python") from exc
    helpers = helper_digests(contents["laptop/ccfleet"])
    expected = {"laptop/ccfleet", *(f"ccfleet_agent/{HELPERS[key]}.py" for key in helpers)}
    if entries.keys() != expected:
        raise ReleaseError("manifest and client helper declarations do not match")
    if any(entries[f"ccfleet_agent/{HELPERS[key]}.py"]["sha256"] != digest
           for key, digest in helpers.items()):
        raise ReleaseError("manifest helper checksum does not match the client pin")


def _directory(path: Path, *, create: bool = False) -> int:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseError("installation paths must be absolute and contain no parent traversal")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _owned(info: os.stat_result, *, directory: bool = False) -> None:
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_type or info.st_uid != os.getuid() or info.st_mode & 0o022 \
            or (not directory and info.st_nlink != 1):
        raise ReleaseError("installation path is not a private user-owned regular file/directory")


def _read(path: Path, limit: int) -> bytes:
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _owned(info)
            if info.st_size > limit:
                raise ReleaseError("installed release file exceeds its size limit")
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ReleaseError("installed release file exceeds its size limit")
        return raw
    finally:
        os.close(parent)


def _write(directory: int, name: str, raw: bytes, mode: int = 0o600) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode, dir_fd=directory)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


@contextlib.contextmanager
def _installation(install_dir: Path):
    directory = _directory(install_dir, create=True)
    root = lock = None
    try:
        _owned(os.fstat(directory), directory=True)
        with contextlib.suppress(FileExistsError):
            os.mkdir(ROOT_NAME, 0o700, dir_fd=directory)
        root = os.open(ROOT_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        _owned(os.fstat(root), directory=True)
        if os.fstat(root).st_mode & 0o077:
            raise ReleaseError("release storage must have private directory permissions")
        lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=root)
        _owned(os.fstat(lock))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory, root
    except BlockingIOError as exc:
        raise ReleaseError("another client update is already running") from exc
    finally:
        for fd in (lock, root, directory):
            if fd is not None:
                os.close(fd)


def _launcher(install_dir: Path, state: dict[str, Any]) -> bytes:
    target = str(install_dir / ROOT_NAME / state["current"] / "ccfleet")
    # Isolation must apply to Python startup itself. A Python launcher which
    # re-execs with -I can already have imported hostile PYTHONPATH startup code.
    command = f'exec python3 -I {shlex.quote(target)} "$@"\n'
    return ("#!/bin/sh\n" + LAUNCHER_PREFIX +
            canonical(state).decode().strip() + "\n" + command).encode()


def _state(install_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[bytes]]:
    try:
        raw = _read(install_dir / "ccfleet", MAX_FILE)
    except FileNotFoundError:
        return None, None
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[1].startswith(LAUNCHER_PREFIX.encode()):
        return None, raw
    state = _json(lines[1][len(LAUNCHER_PREFIX):])
    if set(state) != {"schema", "current", "previous", "highest"} \
            or type(state["schema"]) is not int or state["schema"] != 1:
        raise ReleaseError("invalid installed release launcher state")
    for name in ("current", "previous", "highest"):
        value = state[name]
        if (name == "previous" and value is None):
            continue
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise ReleaseError("invalid installed release identifier")
    if raw != _launcher(install_dir, state):
        raise ReleaseError("installed release launcher was modified")
    return state, raw


def _stored(install_dir: Path, identifier: str, trusted_key: Optional[str]) -> dict[str, Any]:
    path = install_dir / ROOT_NAME / identifier
    raw = _read(path / "manifest.json", MAX_MANIFEST)
    if identifier.startswith("legacy-"):
        value = _json(raw)
        if set(value) != {"schema", "kind", "files"} or value["schema"] != 1 \
                or value["kind"] != "legacy" or not isinstance(value["files"], list):
            raise ReleaseError("invalid legacy backup metadata")
        if identifier != "legacy-" + hashlib.sha256(raw).hexdigest():
            raise ReleaseError("legacy backup metadata was modified")
        for entry in value["files"]:
            if not isinstance(entry, dict) or set(entry) != {"name", "sha256", "size"} \
                    or not isinstance(entry["name"], str) \
                    or not re.fullmatch(r"ccfleet(?:-[a-z-]+-[0-9a-f]{64}\.py)?", entry["name"]):
                raise ReleaseError("invalid legacy backup file")
            data = _read(path / entry["name"], MAX_FILE)
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ReleaseError("legacy backup checksum mismatch")
        return {"version": None, "revision": None, "legacy": True,
                "signature_verified": False}
    value = parse_manifest(raw)
    if identifier != value["version"] + "-" + hashlib.sha256(raw).hexdigest():
        raise ReleaseError("installed release manifest was modified")
    if trusted_key is not None:
        verify_signature(raw, _read(path / "manifest.sig", MAX_SIGNATURE), trusted_key)
    contents = {entry["path"]: _read(path / installed_name(entry["path"], entry["sha256"]),
                                     MAX_FILE) for entry in value["files"]}
    verify_bundle(value, contents)
    return {"version": value["version"], "revision": value["revision"], "legacy": False,
            "signature_verified": trusted_key is not None}


def status(install_dir: Path, trusted_key: Optional[str] = None) -> dict[str, Any]:
    install_dir = Path(install_dir)
    try:
        state, raw = _state(install_dir)
    except FileNotFoundError:
        state, raw = None, None
    if state is None:
        return {"installed": raw is not None, "managed": False, "version": None,
                "revision": None, "signature_verified": False, "rollback_available": False}
    current = _stored(install_dir, state["current"], trusted_key)
    highest = _stored(install_dir, state["highest"], trusted_key)
    return {**current, "installed": True, "managed": True, "current": state["current"],
            "previous": state["previous"], "highest_version": highest["version"],
            "rollback_available": state["previous"] is not None}


def _newer(install_dir: Path, version: str, identifier: Optional[str],
           trusted_key: str) -> None:
    state, _ = _state(install_dir)
    if state is None:
        return
    highest = _stored(install_dir, state["highest"], trusted_key)
    if highest["version"] is None:
        return
    candidate, high = version_key(version), version_key(highest["version"])
    if candidate < high or (candidate == high and identifier is not None
                            and identifier != state["highest"]):
        raise ReleaseError("release downgrade or same-version replacement refused; use rollback")


def resolve_channel(channel_url: str, signature_url: str, trusted_key: str, *,
                    install_dir: Optional[Path] = None, fetch: Fetcher = download,
                    now: Optional[float] = None) -> dict[str, Any]:
    approved_url(channel_url, channel=True)
    approved_url(signature_url, channel=True)
    if signature_url != channel_url + ".sig":
        raise ReleaseError("channel signature must accompany the same channel URL")
    raw, signature = fetch(channel_url, MAX_MANIFEST), fetch(signature_url, MAX_SIGNATURE)
    verify_signature(raw, signature, trusted_key)
    value = _json(raw)
    if set(value) != {"schema", "version", "revision", "manifest_url", "signature_url",
                      "expires_at"} or type(value["schema"]) is not int or value["schema"] != 1:
        raise ReleaseError("invalid signed release channel schema")
    version_key(value["version"])
    if not isinstance(value["revision"], str) or not REVISION_RE.fullmatch(value["revision"]):
        raise ReleaseError("invalid signed channel source revision")
    timestamp = time.time() if now is None else now
    if type(value["expires_at"]) is not int or not timestamp < value["expires_at"] <= \
            timestamp + MAX_CHANNEL_AGE:
        raise ReleaseError("signed release channel has expired or has invalid validity")
    approved_url(value["manifest_url"])
    approved_url(value["signature_url"])
    if value["signature_url"] != value["manifest_url"] + ".sig":
        raise ReleaseError("manifest signature must accompany its immutable manifest")
    if install_dir is not None:
        _newer(Path(install_dir), value["version"], None, trusted_key)
    return value


def _stage(root_path: Path, root_fd: int, identifier: str,
           files: dict[str, bytes]) -> None:
    temporary = ".stage-" + uuid.uuid4().hex
    os.mkdir(temporary, 0o700, dir_fd=root_fd)
    staging = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        for name, raw in files.items():
            _write(staging, name, raw, 0o555 if name == "ccfleet" else 0o444)
        os.fsync(staging)
        try:
            os.stat(identifier, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(temporary, identifier, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            return
        # Caller validates existing immutable releases before attempting reuse.
        raise ReleaseError("release directory already exists; refusing to overwrite it")
    finally:
        os.close(staging)
        for name in files:
            with contextlib.suppress(OSError):
                os.unlink(root_path / temporary / name)
        with contextlib.suppress(OSError):
            os.rmdir(root_path / temporary)


def _backup_legacy(install_dir: Path, root_fd: int, raw: bytes) -> str:
    files = {"ccfleet": raw}
    for key, digest in helper_digests(raw).items():
        path = f"ccfleet_agent/{HELPERS[key]}.py"
        name = installed_name(path, digest)
        source = _read(install_dir / name, MAX_FILE)
        if hashlib.sha256(source).hexdigest() != digest:
            raise ReleaseError("existing helper checksum failed; rollback cannot be preserved")
        files[name] = source
    manifest = canonical({"schema": 1, "kind": "legacy", "files": [
        {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in sorted(files.items())]})
    identifier = "legacy-" + hashlib.sha256(manifest).hexdigest()
    root_path = install_dir / ROOT_NAME
    if (root_path / identifier).exists():
        _stored(install_dir, identifier, None)
    else:
        _stage(root_path, root_fd, identifier, {**files, "manifest.json": manifest})
    return identifier


def _activate(install_dir: Path, directory: int, state: dict[str, Any]) -> None:
    temporary = ".ccfleet-launcher-" + uuid.uuid4().hex
    _, previous = _state(install_dir)
    activated = False
    try:
        _write(directory, temporary, _launcher(install_dir, state), 0o755)
        os.replace(temporary, "ccfleet", src_dir_fd=directory, dst_dir_fd=directory)
        activated = True
        os.fsync(directory)
    except BaseException:
        if activated:
            # A durability failure after replace must not report an unsuccessful
            # update while silently leaving the new launcher selected.
            if previous is None:
                os.unlink("ccfleet", dir_fd=directory)
            else:
                _write(directory, temporary, previous, 0o755)
                os.replace(temporary, "ccfleet", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _result(current: dict[str, Any], state: dict[str, Any], highest: Any) -> dict[str, Any]:
    return {**current, "installed": True, "managed": True, "current": state["current"],
            "previous": state["previous"], "highest_version": highest,
            "rollback_available": state["previous"] is not None}


def install(manifest_url: str, signature_url: str, trusted_key: str, install_dir: Path, *,
            fetch: Fetcher = download, expected: Optional[dict[str, Any]] = None
            ) -> dict[str, Any]:
    approved_url(manifest_url)
    if signature_url != manifest_url + ".sig":
        raise ReleaseError("signature must accompany its immutable manifest")
    approved_url(signature_url)
    raw, signature = fetch(manifest_url, MAX_MANIFEST), fetch(signature_url, MAX_SIGNATURE)
    manifest = verify_manifest(raw, signature, trusted_key)
    if expected is not None and any(expected[key] != manifest[key]
                                    for key in ("version", "revision")):
        raise ReleaseError("immutable manifest does not match its signed channel")
    contents = {entry["path"]: fetch(entry["url"], entry["size"])
                for entry in manifest["files"]}
    verify_bundle(manifest, contents)
    install_dir = Path(install_dir)
    identifier = manifest["version"] + "-" + hashlib.sha256(raw).hexdigest()
    with _installation(install_dir) as (directory, root):
        _newer(install_dir, manifest["version"], identifier, trusted_key)
        state, previous_raw = _state(install_dir)
        if state is not None and state["current"] == identifier:
            return status(install_dir, trusted_key)
        if state is not None:
            _stored(install_dir, state["current"], trusted_key)
            previous = state["current"]
        else:
            previous = _backup_legacy(install_dir, root, previous_raw) \
                if previous_raw is not None else None
        root_path = install_dir / ROOT_NAME
        if (root_path / identifier).exists():
            _stored(install_dir, identifier, trusted_key)
        else:
            files = {installed_name(entry["path"], entry["sha256"]): contents[entry["path"]]
                     for entry in manifest["files"]}
            _stage(root_path, root, identifier,
                   {**files, "manifest.json": raw, "manifest.sig": signature})
        state = {"schema": 1, "current": identifier, "previous": previous, "highest": identifier}
        _activate(install_dir, directory, state)
        return _result({"version": manifest["version"], "revision": manifest["revision"],
                        "legacy": False, "signature_verified": True}, state, manifest["version"])


def install_channel(trusted_key: str, install_dir: Path, *,
                    channel_url: str = DEFAULT_CHANNEL_URL,
                    signature_url: str = DEFAULT_CHANNEL_SIGNATURE_URL,
                    fetch: Fetcher = download, now: Optional[float] = None) -> dict[str, Any]:
    channel = resolve_channel(channel_url, signature_url, trusted_key,
                              install_dir=install_dir, fetch=fetch, now=now)
    return install(channel["manifest_url"], channel["signature_url"], trusted_key,
                   install_dir, fetch=fetch, expected=channel)


def rollback(install_dir: Path, trusted_key: str) -> dict[str, Any]:
    install_dir = Path(install_dir)
    with _installation(install_dir) as (directory, _):
        state, _ = _state(install_dir)
        if state is None or state["previous"] is None:
            raise ReleaseError("there is no previous client release to restore")
        previous = _stored(install_dir, state["previous"], trusted_key)
        highest = _stored(install_dir, state["highest"], trusted_key)
        state = {**state, "current": state["previous"], "previous": state["current"]}
        _activate(install_dir, directory, state)
        return _result(previous, state, highest["version"])
