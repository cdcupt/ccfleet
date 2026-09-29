#!/usr/bin/env python3
"""Build an immutable client manifest from a committed revision, optionally sign it.

Never generates or publishes a signing key. The operator supplies an existing
private key outside the repository and its separately pinned public key.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

_spec = importlib.util.spec_from_file_location(
    "ccfleet_client_release", Path(__file__).resolve().parents[1] /
    "ccfleet_agent/client_release.py")
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def manifest_for_revision(repo: Path, revision: str, version: str) -> bytes:
    release.version_key(version)
    if not release.REVISION_RE.fullmatch(revision):
        raise release.ReleaseError("--revision must be a complete 40-character Git commit")

    def source(path: str) -> bytes:
        size = subprocess.run(["git", "cat-file", "-s", f"{revision}:{path}"], cwd=repo,
                              capture_output=True, timeout=15, check=False)
        if size.returncode != 0 or not size.stdout.strip().isdigit() \
                or not 0 < int(size.stdout) <= release.MAX_FILE:
            raise release.ReleaseError("committed client source is missing or oversized: " + path)
        result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=repo,
                                capture_output=True, timeout=15, check=False)
        if result.returncode != 0 or not 0 < len(result.stdout) <= release.MAX_FILE:
            raise release.ReleaseError("committed client source is missing or oversized: " + path)
        return result.stdout

    client = source("laptop/ccfleet")
    helpers = release.helper_digests(client)
    contents = {"laptop/ccfleet": client}
    contents.update({f"ccfleet_agent/{release.HELPERS[key]}.py":
                     source(f"ccfleet_agent/{release.HELPERS[key]}.py") for key in helpers})
    value = {"schema": 1, "version": version, "revision": revision, "files": [
        {"path": path, "url": release.RAW_BASE + revision + "/" + path,
         "sha256": release.hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        for path, raw in sorted(contents.items())]}
    raw = release.canonical(value)
    release.parse_manifest(raw)
    release.verify_bundle(value, contents)
    return raw


def build(repo: Path, revision: str, version: str, output: Path, *,
          signing_key: Optional[Path] = None,
          public_key: Optional[str] = None) -> dict[str, object]:
    raw = manifest_for_revision(repo, revision, version)
    if (signing_key is None) != (public_key is None):
        raise release.ReleaseError("signing requires an existing private key and pinned public key")
    if signing_key is not None:
        info = signing_key.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or signing_key.resolve().is_relative_to(repo.resolve())):
            raise release.ReleaseError("signing key must be a private owned file outside the repo")
        release.trusted_public_key(public_key)
        if os.path.lexists(str(output) + ".sig"):
            raise release.ReleaseError("signature output already exists; refusing to replace it")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if signing_key is not None:
        executable = release.shutil.which("ssh-keygen", path=os.defpath)
        if executable is None:
            raise release.ReleaseError("OpenSSH ssh-keygen is required to sign releases")
        result = subprocess.run(
            [executable, "-Y", "sign", "-f", str(signing_key), "-n", release.NAMESPACE,
             str(output)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode != 0:
            raise release.ReleaseError("release signing failed; nothing was published")
        signature = release._read(Path(str(output) + ".sig"), release.MAX_SIGNATURE)
        release.verify_signature(raw, signature, public_key)
    return {"version": version, "revision": revision, "manifest": str(output),
            "signed": signing_key is not None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--public-key", type=Path,
                        help="public key file; its key must also be pinned in the client")
    args = parser.parse_args(argv)
    try:
        public_key = None
        if args.public_key:
            # OpenSSH .pub files can have a comment; only the two key fields are pinned.
            parts = release._read(args.public_key.absolute(), 4096).decode().strip().split()
            public_key = " ".join(parts[:2])
        result = build(args.repo.absolute(), args.revision, args.version, args.output.absolute(),
                       signing_key=args.signing_key.absolute() if args.signing_key else None,
                       public_key=public_key)
    except (release.ReleaseError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"ccfleet release: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
