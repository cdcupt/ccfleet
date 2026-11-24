"""Forced SSH command for a separately paired private-owner Antigravity device."""

import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path

# The private owner installs these fixed sibling modules in a protected directory.
if __package__:
    from . import (
        antigravity_slot,
        gemini_distribution,
        gemini_preview,
        mac_policy,
        mac_relay_client,
        workspace_io,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import antigravity_slot
    import gemini_distribution
    import gemini_preview
    import mac_policy
    import mac_relay_client
    import workspace_io


def main():
    if (
        sys.platform != "darwin"
        or len(sys.argv) != 2
        or os.isatty(0)
        or os.isatty(1)
        or not re.fullmatch(r"[0-9a-f]{64}", sys.argv[1])
        or os.environ.get("SSH_ORIGINAL_COMMAND")
        not in ("ccfleet-antigravity-v1", "ccfleet-gemini-preview-v1", "ccfleet-gemini-release-v1",
                "ccfleet-gemini-editor-v1", "ccfleet-gemini-native-check-v1",
                "ccfleet-gemini-native-v1")
    ):
        return 2
    home = Path(mac_policy.authenticated_owner().pw_dir)
    mac_policy.require_enabled(home)
    if os.environ["SSH_ORIGINAL_COMMAND"] == "ccfleet-gemini-native-v1":
        # The Google sign-in lives in the login keychain, which an SSH forced command
        # cannot read; the owner's GUI session service runs the bridge instead.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(8)
            sock.connect(str(mac_policy.directory(home) / "workspace.sock"))
            mac_relay_client.require_peer(sock, os.getuid())
            sock.sendall(b"N" + bytes.fromhex(sys.argv[1]))
            sock.settimeout(None)
            workspace_io.pipe_socket(sock)
        return 0
    if os.environ["SSH_ORIGINAL_COMMAND"] == "ccfleet-gemini-native-check-v1":
        if __package__:
            from . import gemini_native_check
        else:
            import gemini_native_check
        fingerprint = bytes.fromhex(sys.argv[1])
        signal.alarm(gemini_native_check.TIMEOUT + 10)
        return gemini_native_check.slot(
            sys.stdin.buffer, sys.stdout.buffer, home / ".local/bin/agy",
            lambda: antigravity_slot.permitted(home, fingerprint),
        )
    if os.environ["SSH_ORIGINAL_COMMAND"] == "ccfleet-gemini-editor-v1":
        if __package__:
            from . import gemini_editor_slot
        else:
            import gemini_editor_slot
        return gemini_editor_slot.relay(home, bytes.fromhex(sys.argv[1]))
    if os.environ["SSH_ORIGINAL_COMMAND"] == "ccfleet-gemini-release-v1":
        def expired(*_):
            raise TimeoutError("private release request expired")

        signal.signal(signal.SIGALRM, expired)
        signal.alarm(480)
        return gemini_distribution.serve(
            sys.stdin.buffer,
            sys.stdout.buffer,
            home,
            lambda: antigravity_slot.permitted(home, bytes.fromhex(sys.argv[1])),
        )
    if os.environ["SSH_ORIGINAL_COMMAND"] == "ccfleet-gemini-preview-v1":
        signal.alarm(100)
        fingerprint = bytes.fromhex(sys.argv[1])
        return gemini_preview.slot(
            sys.stdin.buffer,
            sys.stdout.buffer,
            lambda: antigravity_slot.permitted(home, fingerprint),
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(8)
        sock.connect(str(mac_policy.directory(home) / "workspace.sock"))
        mac_relay_client.require_peer(sock, os.getuid())
        sock.sendall(b"S" + bytes.fromhex(sys.argv[1]))
        sock.settimeout(None)
        workspace_io.pipe_socket(sock)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, EOFError, subprocess.SubprocessError):
        print("Antigravity workspace connection unavailable.", file=sys.stderr)
        sys.exit(2)
