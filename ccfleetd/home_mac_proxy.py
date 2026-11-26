"""OpenSSH ProxyCommand for a server-owned home Mac registration."""

import os
import socket
import sys
import threading

from .home_mac_transport import open_stream


def main(args):
    if len(args) != 2:
        return 1
    try:
        with open_stream(*args) as peer:

            def upload():
                try:
                    while data := os.read(0, 65536):
                        peer.sendall(data)
                    peer.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            threading.Thread(target=upload, daemon=True).start()
            while data := peer.recv(65536):
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        return 0
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
