"""Private-slot distribution of fixed, public Antigravity CLI releases. No OAuth or model calls."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/"
ARCHES = ("arm64", "amd64")
MAX_ARCHIVE = 256 * 1024**2
BLOCK = 256 * 1024


class ArtifactUnavailable(ValueError):
    """Vendor response could not supply the requested artifact."""


def _fetch(url, dest, limit, timeout):
    # The slot supplies the entire URL and request configuration. Never carry caller headers,
    # credentials, user agent, proxy settings or curl configuration into this download.
    p = subprocess.run(
        [
            "/usr/bin/curl",
            "--disable",
            "--noproxy",
            "*",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--fail",
            "--silent",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "8",
            "--max-time",
            str(timeout),
            "--max-filesize",
            str(limit),
            "-o",
            str(dest),
            "-w",
            "%{http_code}",
            url,
        ],
        cwd=dest.parent,
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout + 5,
        check=True,
    )
    if p.stdout != b"200" or not 0 < dest.stat().st_size <= limit:
        raise ArtifactUnavailable("vendor artifact unavailable")


def _manifest(arch, stage):
    path = stage / "manifest.json"
    _fetch(BASE + "darwin_" + arch + ".json", path, 16384, 25)
    release = json.loads(path.read_bytes())
    if not isinstance(release, dict):
        raise ValueError("invalid release")
    version, digest, url = (release.get(k) for k in ("version", "sha512", "url"))
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}", version)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-fA-F]{128}", digest)
        or not isinstance(url, str)
        or len(url) > 1024
        or not re.fullmatch(
            r"https://storage\.googleapis\.com/antigravity-public/antigravity-cli/"
            r"[A-Za-z0-9_-][A-Za-z0-9._/-]*\.tar\.gz",
            url,
        )
        or any(part in ("", ".", "..") for part in url.split("/")[3:])
    ):
        raise ValueError("unrecognized Google release")
    return {"version": version, "sha512": digest.lower(), "arch": arch}, url


def _trusted_regular(path):
    info = path.lstat()
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077
        and 0 < info.st_size <= MAX_ARCHIVE
    )


def _archive_matches(path, expected):
    if not path.exists():
        return False
    if not _trusted_regular(path):
        raise ValueError("unsafe artifact cache")
    checksum = hashlib.sha512()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(BLOCK), b""):
            checksum.update(chunk)
    return checksum.hexdigest() == expected


def _stream_archive(out, home, manifest, url, authorize):
    cache = home / ".config/ccfleet/private-owner/cli-release-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    info = cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe artifact cache directory")
    arch, digest = manifest["arch"], manifest["sha512"]
    target = cache / (arch + ".tar.gz")
    lock = os.open(cache / (arch + ".lock"), os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not authorize():
            raise ValueError("grant expired")
        # A terminated transfer can leave its slot-owned temporary file. Keep at most
        # one in-progress download for this architecture, under its advisory lock.
        for orphan in cache.glob(".fetch-" + arch + "-*"):
            if orphan.is_file() and not orphan.is_symlink():
                orphan.unlink()
        if not _archive_matches(target, digest):
            with tempfile.NamedTemporaryFile(
                dir=cache, prefix=".fetch-" + arch + "-", delete=False
            ) as tmp:
                path = Path(tmp.name)
            try:
                _fetch(url, path, MAX_ARCHIVE, 180)
                if not _archive_matches(path, digest) or not authorize():
                    raise ValueError("release verification failed")
                os.replace(path, target)
            finally:
                path.unlink(missing_ok=True)
        with target.open("rb") as source:
            # An open FD retains this version even if a newer one is installed concurrently.
            fcntl.flock(lock, fcntl.LOCK_UN)
            deadline = 0.0
            for chunk in iter(lambda: source.read(BLOCK), b""):
                if time.monotonic() >= deadline:
                    if not authorize():
                        raise ValueError("grant expired")
                    deadline = time.monotonic() + 2
                out.write(chunk)
            out.flush()
    finally:
        os.close(lock)


def serve(inp, out, home, authorize):
    if not authorize():
        return 2
    raw = inp.readline(513)
    if not raw.endswith(b"\n") or len(raw) > 512:
        return 2
    q = json.loads(raw)
    if not isinstance(q, dict) or type(q.get("v")) is not int or q["v"] != 1:
        return 2
    # These exact public sources are pinned by the signed client. Untrusted requests
    # cannot choose a path or add credentials, query parameters or a download URL.
    components = {
        "helper": "gemini_preview.py",
        "editor-client": "gemini_editor_client.py",
        "workspace-tools": "workspace_tools.py",
    }
    if set(q) == {"v", "op"} and isinstance(q["op"], str) and q["op"] in components:
        data = Path(__file__).with_name(components[q["op"]]).read_bytes()
        if not 0 < len(data) <= 128 * 1024:
            return 2
        out.write(data)
        out.flush()
        return 0
    arch, op = q.get("arch"), q.get("op")
    if arch not in ARCHES or op not in ("manifest", "archive"):
        return 2
    fields = {"v", "op", "arch"} | ({"sha512"} if op == "archive" else set())
    if set(q) != fields or (
        op == "archive"
        and (not isinstance(q["sha512"], str) or not re.fullmatch("[0-9a-f]{128}", q["sha512"]))
    ):
        return 2
    with tempfile.TemporaryDirectory(prefix="ccfleet-public-release-") as raw_stage:
        stage = Path(raw_stage)
        manifest, url = _manifest(arch, stage)
        if not authorize():
            return 2
        if op == "manifest":
            out.write(json.dumps(manifest, separators=(",", ":")).encode())
            out.flush()
        elif manifest["sha512"] == q["sha512"]:
            _stream_archive(out, home, manifest, url, authorize)
        else:
            return 2
    return 0
