"""Install the signed standalone Antigravity editor runtime on an enrolled Mac slot."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

if __package__:
    from . import gemini_distribution, mac_policy
else:  # Sibling modules in the owner-managed slot installation.
    import gemini_distribution
    import mac_policy

REGISTRY = "https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json"
GOOGLE = "https://dl.google.com/agy-extensions/releases/macos"
BASELINE = "1.5.2"
# Pinned from agentclientprotocol/registry at e29b5bf3bfd8756db5d901690b43dc31e926ccd5.
PLATFORMS = {
    "arm64": ("darwin-aarch64", "123a16d5c40a8ac99e20f01a62b68dd860fd96b1c528dfb8eaa37ca8733d4845"),
    "x86_64": ("darwin-x86_64", "92addb5580577b5356d35484d72d1a113d9c5312ad766bcb304983aa8e3fbb06"),
}
IDENTITIES = {
    "agy_acp_server.par": "agy_acp_server",
    "localharness_external": "localharness_external",
}
MAX_BINARY = 256 * 1024 * 1024
BINARY = "agy_acp_server.par"


def _version(version):
    number = r"(?:0|[1-9][0-9]{0,5})"
    if isinstance(version, str) and re.fullmatch(rf"{number}\.{number}\.{number}", version):
        return tuple(int(part) for part in version.split("."))
    return ()


def _url(version, arch):
    return f"{GOOGLE}/agy-acp-server-{version}-darwin-{arch}.zip"


def _latest(stage, arch):
    path = stage / "manifest.json"
    gemini_distribution._fetch(REGISTRY, path, 32 * 1024, 10)
    manifest = json.loads(path.read_bytes())
    if not isinstance(manifest, dict) or manifest.get("id") != "antigravity-acp":
        raise ValueError("unexpected Antigravity registry entry")
    version = manifest.get("version")
    if not _version(version):
        raise ValueError("invalid Antigravity registry version")
    if _version(version) < _version(BASELINE):
        return BASELINE, PLATFORMS[arch][1]
    try:
        entry = manifest["distribution"]["binary"][PLATFORMS[arch][0]]
    except (KeyError, TypeError):
        raise ValueError("incomplete Antigravity registry entry") from None
    digest = entry.get("sha256") if isinstance(entry, dict) else None
    if (not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
            or entry.get("archive") != _url(version, arch) or entry.get("cmd") != "./" + BINARY
            or entry.get("args") not in ([], None)
            or version == BASELINE and digest != PLATFORMS[arch][1]):
        raise ValueError("unrecognized Antigravity editor build")
    return version, digest


def _verified(folder):
    os.close(mac_policy.open_directory(folder, private=True))
    for name, identifier in IDENTITIES.items():
        path = folder / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o500 or not 0 < info.st_size <= MAX_BINARY):
            raise ValueError("unsafe Antigravity runtime file")
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "-R",
             f'=identifier "{identifier}" and anchor apple generic and '
             'certificate leaf[subject.OU] = "EQHXZ8M8AV"', str(path)],
            env={"PATH": os.defpath}, cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
    return folder / BINARY


def _extract(archive, folder, expected):
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("Google editor archive checksum mismatch")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != len(IDENTITIES) or {i.filename for i in entries} != set(IDENTITIES):
            raise ValueError("unexpected Antigravity archive contents")
        for item in entries:
            if (not stat.S_ISREG(item.external_attr >> 16) or item.flag_bits & 1
                    or not 0 < item.file_size <= MAX_BINARY):
                raise ValueError("invalid Antigravity archive member")
            with bundle.open(item) as src, (folder / item.filename).open("xb") as dst:
                shutil.copyfileobj(src, dst, length=256 * 1024)
                if dst.tell() != item.file_size:
                    raise ValueError("incomplete Antigravity archive")
                os.fchmod(dst.fileno(), 0o500)
    _verified(folder)


def _installed(root, arch):
    return sorted(
        ((p.name.removesuffix("-" + arch), p) for p in root.iterdir()
         if p.name.endswith("-" + arch)
         and _version(p.name.removesuffix("-" + arch)) >= _version(BASELINE)),
        key=lambda item: _version(item[0]), reverse=True,
    )


def ensure_runtime(home):
    """Check the upstream stable release and return the verified immutable binary."""
    arch = platform.machine()
    if sys.platform != "darwin" or arch not in PLATFORMS:
        raise ValueError("Antigravity editor requires a supported Mac slot")
    owner = mac_policy.directory(home)
    os.close(mac_policy.open_directory(owner, private=True))
    root = owner / "editor-runtime"
    root.mkdir(mode=0o700, exist_ok=True)
    directory = mac_policy.open_directory(root, private=True)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        installed = _installed(root, arch)
        with tempfile.TemporaryDirectory(dir=root, prefix=".prepare-") as stage:
            stage = Path(stage)
            try:
                version, checksum = _latest(stage, arch)
            except (OSError, subprocess.SubprocessError, gemini_distribution.ArtifactUnavailable):
                # Offline starts can still use an authenticated, previously installed build.
                version, checksum = BASELINE, PLATFORMS[arch][1]
            if installed and _version(installed[0][0]) >= _version(version):
                return _verified(installed[0][1])
            archive = stage / "editor.zip"
            try:
                gemini_distribution._fetch(_url(version, arch), archive,
                                           gemini_distribution.MAX_ARCHIVE, 180)
            except (OSError, subprocess.SubprocessError, gemini_distribution.ArtifactUnavailable):
                if installed:
                    return _verified(installed[0][1])
                raise
            candidate = stage / "bin"
            candidate.mkdir(mode=0o700)
            _extract(archive, candidate, checksum)
            target = root / f"{version}-{arch}"
            candidate.rename(target)
            return target / BINARY
    finally:
        os.close(directory)
