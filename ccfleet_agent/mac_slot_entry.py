"""Inference-only forced SSH entry for an explicitly opted-in private Mac owner."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional


def sibling(name: str):
    if name not in {"mac_policy", "mac_relay_client"}:
        raise ValueError("unsupported private owner module")
    spec = importlib.util.spec_from_file_location(
        "ccfleet_mac_entry_" + name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise ValueError("private owner runtime unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gateway = args == ["--gateway-only"]
    command = "ccfleet-gateway-v1" if gateway else "ccfleet-inference-v1"
    if (sys.platform != "darwin" or args not in ([], ["--gateway-only"])
            or os.environ.get("SSH_ORIGINAL_COMMAND") != command
            or sys.stdin.isatty() or sys.stdout.isatty()):
        print("unsupported private owner inference request", file=sys.stderr)
        return 2
    try:
        policy = sibling("mac_policy")
        owner = policy.authenticated_owner()
        directory = policy.open_directory(Path(__file__).absolute().parent)
        os.close(directory)
        policy.require_enabled(Path(owner.pw_dir))
        client = sibling("mac_relay_client")
        return client.forward(b"G" if gateway else b"D", home=Path(owner.pw_dir))
    except (OSError, ValueError):
        print("private owner inference is unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
