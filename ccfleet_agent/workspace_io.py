"""Bounded transport records inside the paired device's encrypted SSH session."""

from __future__ import annotations

import contextlib
import os
import select
import socket
import struct

MAX_FRAME = 2 * 1024 * 1024


def frame(tag, body=b""):
    if len(body) > MAX_FRAME or len(tag) != 1:
        raise ValueError("invalid workspace frame")
    return tag + struct.pack(">I", len(body)) + body


def unpack(buffer):
    while len(buffer) >= 5:
        size = struct.unpack_from(">I", buffer, 1)[0]
        if size > MAX_FRAME:
            raise ValueError("workspace frame too large")
        if len(buffer) < 5 + size:
            return
        tag, body = bytes(buffer[:1]), bytes(buffer[5 : 5 + size])
        del buffer[: 5 + size]
        yield tag, body


def write(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if not count:
            raise EOFError()
        view = view[count:]


def pipe_socket(sock):
    """Bridge stdio to a private Unix socket. Close on either peer's EOF."""
    while True:
        ready, _, _ = select.select([0, sock], [], [])
        for item in ready:
            data = os.read(0, 65536) if item == 0 else sock.recv(65536)
            if not data:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                return
            if item == 0:
                sock.sendall(data)
            else:
                write(1, data)
