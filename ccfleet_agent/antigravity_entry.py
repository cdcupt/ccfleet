"""Forced SSH command for a separately paired private-owner Antigravity device."""

import os
import re
import socket
import sys
from pathlib import Path

# The private owner installs these fixed sibling modules in a protected directory.
if __package__:
    from . import mac_policy, mac_relay_client, workspace_io
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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
        or os.environ.get("SSH_ORIGINAL_COMMAND") != "ccfleet-antigravity-v1"
    ):
        return 2
    home = Path(mac_policy.authenticated_owner().pw_dir)
    mac_policy.require_enabled(home)
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
    except (OSError, ValueError, EOFError):
        print("Antigravity workspace connection unavailable.", file=sys.stderr)
        sys.exit(2)
