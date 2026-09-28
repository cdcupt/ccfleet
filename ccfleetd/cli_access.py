"""The CLI access boundary: device keys, WebSocket framing and byte relay.

The browser-facing fleet server authenticates a ccfleet *device* token.  It
never sees a Claude credential.  Once authenticated it carries an SSH byte
stream to the slot; SSH is the end-to-end encrypted layer between the user's
computer and the slot, so the broker cannot read terminal contents.

Only the small WebSocket subset used by ``laptop/ccfleet`` lives here.  Frames
are binary, complete and masked by the client.  Control frames are handled as
RFC 6455 requires; extensions and fragmented data are deliberately refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import socket
import struct
import threading
from typing import Callable, Optional

PAIR_PREFIX = "ccf_pair_"
DEVICE_PREFIX = "ccf_dev_"
PAIRING_TTL_S = 10 * 60
MAX_DEVICES_PER_SLOT = 10
MAX_DEVICE_NAME = 60
MAX_SSH_KEY_CHARS = 16 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

SSH_KEY_TYPES = frozenset({
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "ssh-rsa",
})


class WebSocketError(ValueError):
    """A peer sent a frame this narrow transport does not accept."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_has_prefix(token: object, prefix: str) -> bool:
    return (isinstance(token, str) and token.startswith(prefix)
            and 40 <= len(token) <= 100 and token.isascii())


def normalize_public_key(value: object) -> Optional[str]:
    """Return one canonical OpenSSH public key, without its comment."""
    if not isinstance(value, str) or len(value) > MAX_SSH_KEY_CHARS:
        return None
    if "\n" in value or "\r" in value:
        return None
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        return None
    key_type, encoded = parts[:2]
    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None
    if len(blob) < 8:
        return None
    size = struct.unpack(">I", blob[:4])[0]
    try:
        embedded = blob[4:4 + size].decode("ascii")
    except UnicodeDecodeError:
        return None
    if size <= 0 or 4 + size > len(blob) or embedded != key_type:
        return None
    return f"{key_type} {base64.b64encode(blob).decode('ascii')}"


def key_fingerprint(key: str) -> str:
    normalized = normalize_public_key(key)
    if normalized is None:
        raise ValueError("not an SSH public key")
    blob = base64.b64decode(normalized.split()[1].encode("ascii"), validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def websocket_accept(key: str) -> Optional[str]:
    """The RFC 6455 accept value, or None for a malformed client key."""
    try:
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None
    if len(raw) != 16:
        return None
    digest = hashlib.sha1(key.encode("ascii") + WS_GUID).digest()  # noqa: S324 - protocol
    return base64.b64encode(digest).decode("ascii")


def websocket_frame(payload: bytes, opcode: int = 2) -> bytes:
    """One unmasked server frame."""
    length = len(payload)
    head = bytes((0x80 | opcode,))
    if length < 126:
        return head + bytes((length,)) + payload
    if length <= 0xFFFF:
        return head + bytes((126,)) + struct.pack(">H", length) + payload
    return head + bytes((127,)) + struct.pack(">Q", length) + payload


def _read_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        part = sock.recv(length - len(chunks))
        if not part:
            raise EOFError
        chunks.extend(part)
    return bytes(chunks)


def read_client_frame(sock: socket.socket) -> tuple[int, bytes]:
    """Read one complete, masked client frame."""
    first, second = _read_exact(sock, 2)
    fin, rsv, opcode = bool(first & 0x80), first & 0x70, first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if not fin or rsv or not masked:
        raise WebSocketError("fragmented, extended or unmasked frame")
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    if length > MAX_FRAME_BYTES or (opcode >= 8 and length > 125):
        raise WebSocketError("frame too large")
    mask = _read_exact(sock, 4)
    payload = bytearray(_read_exact(sock, length))
    for index in range(length):
        payload[index] ^= mask[index & 3]
    return opcode, bytes(payload)


def relay_websocket(client: socket.socket, upstream: socket.socket,
                    authorized: Optional[Callable[[], bool]] = None) -> None:
    """Carry an SSH stream between a WebSocket client and one TCP endpoint.

    ``authorized`` is checked beside the byte pumps so deleting a device,
    releasing its slot or disabling its node also closes an already-open
    transport. Removing an authorized key alone cannot terminate an SSH
    connection that has already authenticated.
    """
    write_lock = threading.Lock()
    stopped = threading.Event()

    def send_client(payload: bytes, opcode: int = 2) -> None:
        with write_lock:
            client.sendall(websocket_frame(payload, opcode))

    def from_upstream() -> None:
        try:
            while not stopped.is_set():
                payload = upstream.recv(64 * 1024)
                if not payload:
                    break
                send_client(payload)
        except OSError:
            pass
        finally:
            stopped.set()
            try:
                send_client(b"", 8)
            except OSError:
                pass

    worker = threading.Thread(target=from_upstream, name="ccfleet-cli-upstream", daemon=True)
    worker.start()

    def watch_authorization() -> None:
        if authorized is None:
            return
        while not stopped.wait(1.0):
            try:
                allowed = authorized()
            except Exception:  # pragma: no cover - a closed store must fail closed
                allowed = False
            if allowed:
                continue
            stopped.set()
            for peer in (upstream, client):
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return

    watcher = threading.Thread(target=watch_authorization,
                               name="ccfleet-cli-authorization", daemon=True)
    watcher.start()
    try:
        while not stopped.is_set():
            opcode, payload = read_client_frame(client)
            if opcode == 2:
                upstream.sendall(payload)
            elif opcode == 8:
                break
            elif opcode == 9:
                send_client(payload, 10)
            elif opcode != 10:
                raise WebSocketError("only binary data is accepted")
    except (EOFError, OSError, WebSocketError):
        pass
    finally:
        stopped.set()
        for peer in (upstream, client):
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        worker.join(timeout=1)
        watcher.join(timeout=1)


def masked_client_frame(payload: bytes, opcode: int = 2) -> bytes:
    """Useful to the standalone client and the protocol tests."""
    mask = os.urandom(4)
    body = bytearray(payload)
    for index in range(len(body)):
        body[index] ^= mask[index & 3]
    length = len(body)
    head = bytes((0x80 | opcode,))
    if length < 126:
        size = bytes((0x80 | length,))
    elif length <= 0xFFFF:
        size = bytes((0x80 | 126,)) + struct.pack(">H", length)
    else:
        size = bytes((0x80 | 127,)) + struct.pack(">Q", length)
    return head + size + mask + bytes(body)


def secrets_equal(left: str, right: str) -> bool:
    """Named here so token comparisons are never accidentally ordinary equality."""
    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
