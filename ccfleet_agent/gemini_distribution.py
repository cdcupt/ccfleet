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

EDITOR_LIMIT = 128 * 1024
# These public snapshots remain available to installed clients. Sources are pinned
# for fresh slots that do not already have older helpers on disk.
EDITOR_FILES = {
    "editor-client": "gemini_editor_client.py",
    "workspace-tools": "workspace_tools.py",
    "acp-client": "gemini_acp_client.py",
    "project-publish": "gemini_publish.py",
    "native-check": "gemini_native_check.py",
    "native-terminal": "gemini_native_terminal.py",
}
EDITOR_VERSION = "0.2.28"
EDITOR_HISTORY = (
    ("0.2.15", "ee147f63c559f4f8deb9e5962501412168edf27e", {
        "editor-client": "0a5afe3d72241511cea9581b3e5bcb2278ead352d0f9f5da375d70f4fb2daad0",
        "workspace-tools": "55153b8a796d059c3fd9b5b3f96247ed843d067ff7c7d5d910e6e6a95fc639f3",
    }),
    ("0.2.16", "4b093ba94033a41c86f0716b9a24482ff7b88da2", {
        "editor-client": "1d1f6fcc92b94136ba525d8c87d3dc1d1828cbe0a72d3a9519507e29e9d34bf8",
        "workspace-tools": "0d981a56e278249c174cdad7e010313456a67d4f263f2fa2ad585efd8ce4ddb0",
    }),
    ("0.2.17", "7dd73257b9a3add782b0837ce1ec90744916499c", {
        "editor-client": "5bbe2e808877f25e1be746e94111fb7b3c1551b9c3c45ba8bcde0352ddb12906",
        "workspace-tools": "0d981a56e278249c174cdad7e010313456a67d4f263f2fa2ad585efd8ce4ddb0",
    }),
    ("0.2.18", "237b8fe69c45521e7a121dc1f64a5e48877c206f", {
        "editor-client": "5bbe2e808877f25e1be746e94111fb7b3c1551b9c3c45ba8bcde0352ddb12906",
        "workspace-tools": "a25cae8d7ef75f66b194cac2f61b3547ebf94afd6452620bad3f06a985f68c70",
        "acp-client": "a5177386f6ca96f4c89e0c2157a7867114125f62b75d0db71363738cd6841ddd",
    }),
    ("0.2.20", "e056fb4127d34ee9e1918e0fbe5fc3e909a2368d", {
        "editor-client": "5bbe2e808877f25e1be746e94111fb7b3c1551b9c3c45ba8bcde0352ddb12906",
        "workspace-tools": "a25cae8d7ef75f66b194cac2f61b3547ebf94afd6452620bad3f06a985f68c70",
        "acp-client": "d014f38cbca2258a2a6c2432382c145146fb4eab619cf71530cb9a00f1a46152",
    }),
    ("0.2.21", "21bf1aff120b78749240ee96daa0a3c3fafef853", {
        "editor-client": "5bbe2e808877f25e1be746e94111fb7b3c1551b9c3c45ba8bcde0352ddb12906",
        "workspace-tools": "a25cae8d7ef75f66b194cac2f61b3547ebf94afd6452620bad3f06a985f68c70",
        "acp-client": "415ae6ae910b582a2b1e09006b9a3f660cde72f9a44a0a4e4732857897c1b0fc",
    }),
    ("0.2.22", "a415c3af190b160b5f31de594802c553a0393794", {
        "editor-client": "8418832422a73d721b1dcf14380ed210731ec191cba087212d793933f6370c24",
        "workspace-tools": "a25cae8d7ef75f66b194cac2f61b3547ebf94afd6452620bad3f06a985f68c70",
        "acp-client": "8028b5eb2ec51c2322942e04608b2a003884a23c8874c493ef3925e4cd08f64d",
    }),
    ("0.2.23", "baa0cbc1bb5a7e3e93d9ca2ca2bd69747acc7b70", {
        "editor-client": "80db3dae15b8d23830ef47f439d7d456db963f35ccd9438217b89a66774fc5c7",
        "workspace-tools": "8083d29214ee5819b28e459470e746b75601552baaf5fb4dfe642a63516587b3",
        "acp-client": "bd217cadfe47a7d435a536a1399bd41e0a9d7cab531e608172d7bc5107ff7634",
        "project-publish": "9e5d10057dc525da5349c383c0a7e3ddc160d6d76b5139016ab287385a6ea098",
    }),
    ("0.2.28", "fa6b29371b2f8cc909c89e052c224ca5804f11d9", {
        "editor-client": "3dd1e095d41d26a3b4e39acd0bbcd4fd461b05a16170139c2e6c6ac4bab0d18b",
        "workspace-tools": "8083d29214ee5819b28e459470e746b75601552baaf5fb4dfe642a63516587b3",
        "acp-client": "0f6db63e11ef31d6714859f14df4654a34622cc6b5797a69fc025cf4ae42d579",
        "project-publish": "9e5d10057dc525da5349c383c0a7e3ddc160d6d76b5139016ab287385a6ea098",
        "native-check": "e5fed949241811152e371158645b99c832c47a57575bb2d6098e2cf213ffb682",
        "native-terminal": "1615fa56f72e7fa1fc59c643968379898268ed9b3ecb6248f4de4149f496db07",
    }),
)


def editor_snapshot(filename, version=EDITOR_VERSION):
    if version == "0.2.15":
        return filename  # Keep the original names intact for in-flight readers.
    return filename.removesuffix(".py") + f"-{version}.py"


def _editor_bytes(op, request):
    if set(request) == {"v", "op"}:
        expected = EDITOR_HISTORY[0][2].get(op)
        if expected is None:
            return None
    elif set(request) == {"v", "op", "sha256"} and isinstance(request["sha256"], str):
        expected = request["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return None
    else:
        return None
    # Only server-selected versions and component names may form a disk path.
    version = next((v for v, _, pins in EDITOR_HISTORY if pins.get(op) == expected), EDITOR_VERSION)
    filename = editor_snapshot(EDITOR_FILES[op], version)
    return pinned_editor_file(Path(__file__).with_name(filename), expected)


def pinned_editor_file(path, expected):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= EDITOR_LIMIT:
            return None
        raw = source.read(EDITOR_LIMIT + 1)
    return raw if len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == expected else None


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
    op = q.get("op")
    if isinstance(op, str) and op in EDITOR_FILES:
        data = _editor_bytes(op, q)
        if data is None or not authorize():
            return 2
        out.write(data)
        out.flush()
        return 0
    if set(q) == {"v", "op"} and op == "helper":
        data = Path(__file__).with_name("gemini_preview.py").read_bytes()
        if not 0 < len(data) <= EDITOR_LIMIT:
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
