"""Build an immutable signed home-Mac connector directory, entirely offline."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccfleetd import home_mac_bundle as bundle


def build(output, origin, version, signing_key):
    import re

    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not origin.isascii()
    ):
        raise ValueError("Provide a canonical HTTPS origin.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Provide a release version.")
    output = Path(output)
    if output.exists():
        raise ValueError("The release directory already exists.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=output.parent))
    try:
        content = bundle.build(origin)
        program = temporary / bundle.COMMAND
        program.write_bytes(content)
        program.chmod(0o755)
        manifest = {
            "version": 1,
            "kind": "home-mac",
            "release": version,
            "origin": origin,
            "file": bundle.COMMAND,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        metadata = temporary / bundle.MANIFEST
        metadata.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-Y",
                "sign",
                "-f",
                str(signing_key),
                "-n",
                "ccfleet-release",
                str(metadata),
            ],
            check=True,
        )
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    args = parser.parse_args()
    build(args.output, args.origin, args.version, args.signing_key)
