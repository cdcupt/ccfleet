"""The retired local-agent model relay must fail closed, including old clients."""

import subprocess
import sys
from pathlib import Path

from ccfleet_agent import local_relay


def test_retired_relay_does_not_read_credentials_or_open_network(capsys):
    assert local_relay.main() == 2
    assert "retired" in capsys.readouterr().err


def test_old_forced_command_has_no_inference_route():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-I", str(root / "ccfleet_agent/local_relay.py")],
        input=b'not-a-model-request', capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
    assert b"retired" in result.stderr
