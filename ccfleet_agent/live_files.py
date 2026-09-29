"""Selected-root SFTP v3 over an already authenticated byte stream.

There is no listener, shell, model client, eager tree walk, or whole-file quota.
Paths on the wire belong to a virtual ``/``; host paths and UID/GID are not sent.
Descriptor-relative operations refuse external symlinks, protected control data,
hardlinked regular files and observed directory detachments. This is not an OS
sandbox against another process running as the same user: that process can race
normal filesystem operations despite ancestry checks. Keep that trust boundary
explicit when presenting the live filesystem feature.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import os
import posixpath
import secrets
import stat
import struct
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

MAX_PACKET = 2 * 1024 * 1024
MAX_IO = 1024 * 1024
MAX_HANDLES = 256
MAX_PATH = 32768
READDIR_BATCH = 128
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_NOFOLLOW | os.O_NONBLOCK
OK, EOF, NO_FILE, DENIED, FAILURE, BAD_MESSAGE, UNSUPPORTED = 0, 1, 2, 3, 4, 5, 8


class ProtocolError(ValueError):
    """An invalid packet; messages never contain a physical filename."""


class Refused(PermissionError):
    pass


def _u32(value: int) -> bytes:
    return struct.pack("!I", value)


def _u64(value: int) -> bytes:
    return struct.pack("!Q", value)


def _string(value: bytes) -> bytes:
    return _u32(len(value)) + value


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class Reader:
    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def take(self, length: int) -> bytes:
        if length < 0 or self.position + length > len(self.data):
            raise ProtocolError("truncated request")
        result = self.data[self.position:self.position + length]
        self.position += length
        return result

    def u32(self) -> int:
        return struct.unpack("!I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("!Q", self.take(8))[0]

    def string(self) -> bytes:
        return self.take(self.u32())

    def attrs(self) -> dict:
        flags, result = self.u32(), {}
        if flags & ~0x8000000F:
            raise ProtocolError("unknown attribute flags")
        if flags & 1:
            result["size"] = self.u64()
        if flags & 2:
            result["owner"] = (self.u32(), self.u32())
        if flags & 4:
            result["mode"] = self.u32()
        if flags & 8:
            result["times"] = (self.u32(), self.u32())
        if flags & 0x80000000:
            count = self.u32()
            if count > 128:
                raise ProtocolError("too many extended attributes")
            for _ in range(count):
                self.string(), self.string()
        return result

    def end(self) -> None:
        if self.position != len(self.data):
            raise ProtocolError("unexpected request fields")


def _attributes(info: os.stat_result) -> bytes:
    return (_u32(15) + _u64(info.st_size) + _u32(0) + _u32(0)
            + _u32(info.st_mode & 0o177777) + _u32(max(0, int(info.st_atime)) & 0xFFFFFFFF)
            + _u32(max(0, int(info.st_mtime)) & 0xFFFFFFFF))


@dataclass
class Handle:
    fd: int
    parent: int
    name: bytes
    parts: tuple[bytes, ...]
    readable: bool = True
    writable: bool = False
    append: bool = False
    iterator: Optional[object] = None

    def close(self) -> None:
        if self.iterator is not None:
            self.iterator.close()
        os.close(self.fd)
        os.close(self.parent)


class SFTPServer:
    """Persistent serial SFTP state. handle_packet includes framing on both sides."""

    def __init__(self, root: Path, protected: Iterable[Path] = ()):
        self.root = os.fsencode(os.path.realpath(os.path.abspath(root)))
        self.root_fd = os.open(self.root, _DIR)
        self.root_id = _identity(os.fstat(self.root_fd))
        self.protected = []
        for path in protected:
            for spelling in (os.path.abspath(path), os.path.realpath(path)):
                raw = os.fsencode(spelling)
                if os.path.commonpath((self.root, raw)) == self.root:
                    relative = os.path.relpath(raw, self.root)
                    parts = () if relative == b"." else tuple(relative.split(b"/"))
                    if parts not in self.protected:
                        self.protected.append(parts)
        self.protected_ids: set[tuple[int, int]] = set()
        self.handles: dict[bytes, Handle] = {}
        self.initialized = False
        self.lock = threading.RLock()
        try:
            self._guard(())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self.lock:
            for handle in self.handles.values():
                handle.close()
            self.handles.clear()
            if self.root_fd >= 0:
                os.close(self.root_fd)
                self.root_fd = -1

    def protect(self, path: Path) -> None:
        """Register connector-owned runtime state before exposing a new link."""
        with self.lock:
            for spelling in (os.path.abspath(path), os.path.realpath(path)):
                raw = os.fsencode(spelling)
                if os.path.commonpath((self.root, raw)) != self.root:
                    continue
                relative = os.path.relpath(raw, self.root)
                if relative == b".":
                    raise Refused("cannot protect the entire selected root")
                parts = tuple(relative.split(b"/"))
                if parts not in self.protected:
                    self.protected.append(parts)
            self._refresh_protected()

    def _guard(self, parts: tuple[bytes, ...], *, subtree: bool = False) -> None:
        # Protect case/Unicode aliases on case-insensitive and normalizing hosts.
        def canonical(value):
            return tuple(unicodedata.normalize("NFC", os.fsdecode(part)).casefold()
                         for part in value)
        parts = canonical(parts)
        for protected in self.protected:
            protected = canonical(protected)
            if (parts[:len(protected)] == protected
                    or (subtree and protected[:len(parts)] == parts)):
                raise Refused("protected control data")

    def _refresh_protected(self) -> None:
        # Retain previously observed identities as well as current replacements:
        # moving an app control file must not suddenly make its old inode shareable.
        for parts in self.protected:
            try:
                self.protected_ids.add(_identity(os.stat(os.path.join(self.root, *parts))))
            except FileNotFoundError:
                pass

    def _check_directory(self, fd: int) -> None:
        current = os.dup(fd)
        try:
            for _ in range(1024):
                identity = _identity(os.fstat(current))
                if identity in self.protected_ids:
                    raise Refused("protected control data")
                if identity == self.root_id:
                    return
                parent = os.open(b"..", _DIR, dir_fd=current)
                parent_id = _identity(os.fstat(parent))
                os.close(current)
                current = parent
                if parent_id == identity:
                    break
            raise Refused("directory moved outside selected root")
        finally:
            os.close(current)

    @staticmethod
    def _components(raw: bytes) -> list[bytes]:
        if len(raw) > MAX_PATH or b"\0" in raw:
            raise ProtocolError("invalid path")
        return [part for part in raw.split(b"/") if part not in (b"", b".")]

    @contextlib.contextmanager
    def _resolve(self, raw: bytes, *, follow: bool = True, missing: bool = False):
        queue = collections.deque(self._components(raw))
        parts: list[bytes] = []
        fd = os.dup(self.root_fd)
        links = 0
        try:
            while queue:
                component = queue.popleft()
                if component == b"..":
                    if not parts:
                        raise Refused("path escapes selected root")
                    parts.pop()
                    queue.extendleft(reversed(parts))
                    parts = []
                    os.close(fd)
                    fd = os.dup(self.root_fd)
                    continue
                self._check_directory(fd)
                target = tuple([*parts, component])
                self._guard(target)
                try:
                    info = os.stat(component, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if missing and not queue:
                        yield fd, component, target, None
                        return
                    raise
                if stat.S_ISLNK(info.st_mode) and (queue or follow):
                    links += 1
                    if links > 40:
                        raise Refused("too many symlinks")
                    link = os.readlink(component, dir_fd=fd)
                    if link.startswith(b"/"):
                        normalized = os.path.normpath(link)
                        if os.path.commonpath((self.root, normalized)) != self.root:
                            raise Refused("symlink leaves selected root")
                        destination = os.path.relpath(normalized, self.root)
                    else:
                        destination = b"/".join([*parts, link])
                    queue.extendleft(reversed(self._components(destination)))
                    parts = []
                    os.close(fd)
                    fd = os.dup(self.root_fd)
                    continue
                if not queue:
                    if _identity(info) in self.protected_ids:
                        raise Refused("protected control data")
                    yield fd, component, target, info
                    return
                child = os.open(component, _DIR, dir_fd=fd)
                os.close(fd)
                fd = child
                parts.append(component)
            self._check_directory(fd)
            yield fd, b".", tuple(parts), os.fstat(fd)
        finally:
            os.close(fd)

    def _regular(self, info: os.stat_result) -> None:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or _identity(info) in self.protected_ids):
            raise Refused("special, hardlinked or protected file")

    def _handle(self, token: bytes, *, directory: bool = False) -> Handle:
        handle = self.handles.get(token)
        if handle is None or (handle.iterator is not None) != directory:
            raise ProtocolError("invalid handle")
        self._check_directory(handle.parent)
        current = os.stat(handle.name, dir_fd=handle.parent, follow_symlinks=False)
        info = os.fstat(handle.fd)
        if _identity(current) != _identity(info):
            raise Refused("open object moved or replaced")
        if directory:
            self._check_directory(handle.fd)
        else:
            self._regular(info)
        self._guard(handle.parts)
        return handle

    def _save(self, handle: Handle) -> bytes:
        if len(self.handles) >= MAX_HANDLES:
            handle.close()
            raise Refused("too many open handles")
        token = secrets.token_bytes(16)
        self.handles[token] = handle
        return _string(token)

    @staticmethod
    def _set_attributes(fd: int, attrs: dict) -> None:
        if attrs.get("owner", (0, 0)) != (0, 0):
            raise Refused("ownership is synthetic")
        if attrs.get("mode", 0) & 0o7000:
            raise Refused("privileged permission bits are unavailable")
        if "size" in attrs:
            os.ftruncate(fd, attrs["size"])
        if "mode" in attrs:
            os.fchmod(fd, attrs["mode"] & 0o777)
        if "times" in attrs:
            os.utime(fd, attrs["times"])

    def _open(self, request: Reader, *, directory: bool) -> bytes:
        path = request.string()
        flags, attrs = (1, {}) if directory else (request.u32(), request.attrs())
        request.end()
        if flags & ~63 or not flags & 3 or flags & 0x34 and not flags & 2:
            raise ProtocolError("invalid open flags")
        if flags & 32 and not flags & 8:
            raise ProtocolError("exclusive open requires create")
        with self._resolve(path, missing=bool(flags & 8)) as (parent, name, parts, info):
            self._check_directory(parent)
            if directory:
                fd = os.open(name, _DIR, dir_fd=parent)
            else:
                if info is not None:
                    self._regular(info)
                access = os.O_RDWR if flags & 3 == 3 else os.O_WRONLY if flags & 2 else os.O_RDONLY
                access |= (_FILE | (os.O_CREAT if flags & 8 else 0)
                           | (os.O_EXCL if flags & 32 else 0))
                access |= os.O_APPEND if flags & 4 else 0
                fd = os.open(name, access, attrs.get("mode", 0o600) & 0o777, dir_fd=parent)
            saved = False
            try:
                self._check_directory(parent)
                if directory:
                    self._check_directory(fd)
                else:
                    self._regular(os.fstat(fd))
                    if flags & 16:
                        os.ftruncate(fd, 0)
                    if info is None:
                        self._set_attributes(fd, attrs)
                iterator = os.scandir(fd) if directory else None
                handle = Handle(fd, os.dup(parent), name, parts, bool(flags & 1),
                                bool(flags & 2), bool(flags & 4), iterator)
                saved = True
                return self._save(handle)
            finally:
                if not saved:
                    os.close(fd)

    def _name(self, name: bytes, info: os.stat_result) -> bytes:
        # A conservative longname contains no real username, group or host path.
        longname = stat.filemode(info.st_mode).encode() + b" 1 0 0 " + str(info.st_size).encode()
        return _string(name) + _string(longname + b" " + name) + _attributes(info)

    def _stat(self, path: bytes, *, follow: bool) -> tuple[bytes, os.stat_result]:
        with self._resolve(path, follow=follow) as (_, _, parts, info):
            if stat.S_ISREG(info.st_mode):
                self._regular(info)
            if stat.S_ISLNK(info.st_mode):
                with self._resolve(path, missing=True):
                    pass
            return b"/" + b"/".join(parts), info

    def _read_directory(self, handle: Handle) -> bytes:
        entries = []
        while len(entries) < READDIR_BATCH:
            try:
                entry = next(handle.iterator)
            except StopIteration:
                break
            name = os.fsencode(entry.name)
            path = b"/" + b"/".join([*handle.parts, name])
            try:
                _, info = self._stat(path, follow=False)
            except (OSError, ProtocolError):
                continue
            if (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)):
                entries.append(self._name(name, info))
        return _u32(len(entries)) + b"".join(entries) if entries else b""

    def _rename(self, old: bytes, new: bytes, *, replace: bool) -> None:
        with self._resolve(old, follow=False) as (src, name, source_parts, info):
            with self._resolve(new, follow=False, missing=True) as destination:
                dst, target, target_parts, exists = destination
                self._guard(source_parts, subtree=True)
                self._guard(target_parts, subtree=True)
                if not source_parts or not target_parts:
                    raise Refused("cannot rename selected root")
                self._check_directory(src)
                self._check_directory(dst)
                if stat.S_ISREG(info.st_mode):
                    self._regular(info)
                if exists is not None and stat.S_ISREG(exists.st_mode):
                    self._regular(exists)
                if exists is not None and not replace:
                    raise FileExistsError()
                os.rename(name, target, src_dir_fd=src, dst_dir_fd=dst)
                # Maintain an open file's identity after an acknowledged rename.
                for handle in self.handles.values():
                    if handle.parts == source_parts:
                        os.close(handle.parent)
                        handle.parent, handle.name = os.dup(dst), target
                    if handle.parts[:len(source_parts)] == source_parts:
                        handle.parts = target_parts + handle.parts[len(source_parts):]

    def _dispatch(self, kind: int, request: Reader) -> tuple[int, bytes]:
        if kind in (3, 11):
            return 102, self._open(request, directory=kind == 11)
        if kind == 4:
            token = request.string()
            request.end()
            handle = self.handles.pop(token, None)
            if handle is None:
                raise ProtocolError("invalid handle")
            handle.close()
        elif kind in (5, 6):
            handle = self._handle(request.string())
            offset = request.u64()
            value = request.u32() if kind == 5 else request.string()
            request.end()
            if offset > 0x7FFFFFFFFFFFFFFF:
                raise ProtocolError("invalid offset")
            if kind == 5:
                if not handle.readable:
                    raise Refused("handle is not readable")
                data = os.pread(handle.fd, min(value, MAX_IO), offset)
                return (103, _string(data)) if data else (101, self._status(EOF))
            if not handle.writable:
                raise Refused("handle is not writable")
            if len(value) > MAX_IO:
                raise ProtocolError("write request exceeds per-I/O limit")
            while value:
                written = (os.write(handle.fd, value) if handle.append
                           else os.pwrite(handle.fd, value, offset))
                if written <= 0:
                    raise OSError("incomplete write")
                offset, value = offset + written, value[written:]
        elif kind in (7, 17, 16, 19):
            path = request.string()
            request.end()
            if kind == 19:
                with self._resolve(path, follow=False) as (_, _, parts, info):
                    if not stat.S_ISLNK(info.st_mode):
                        raise OSError("not a symlink")
                    with self._resolve(path, missing=True) as (_, _, target, _):
                        relative = posixpath.relpath(b"/" + b"/".join(target),
                                                    b"/" + b"/".join(parts[:-1]))
                    return 104, _u32(1) + self._name(relative, info)
            virtual, info = self._stat(path, follow=kind != 7)
            if kind == 16:
                return 104, _u32(1) + self._name(virtual, info)
            return 105, _attributes(info)
        elif kind in (8, 10):
            token = request.string()
            handle = self.handles.get(token)
            handle = self._handle(token, directory=handle is not None
                                  and handle.iterator is not None)
            attrs = request.attrs() if kind == 10 else {}
            request.end()
            if kind == 8:
                return 105, _attributes(os.fstat(handle.fd))
            self._guard(handle.parts, subtree=handle.iterator is not None)
            self._set_attributes(handle.fd, attrs)
        elif kind == 12:
            handle = self._handle(request.string(), directory=True)
            request.end()
            entries = self._read_directory(handle)
            return (104, entries) if entries else (101, self._status(EOF))
        elif kind in (9, 13, 14, 15):
            path = request.string()
            attrs = request.attrs() if kind in (9, 14) else {}
            request.end()
            with self._resolve(path, follow=kind == 9, missing=kind == 14) as resolved:
                parent, name, parts, info = resolved
                self._guard(parts, subtree=True)
                if not parts:
                    raise Refused("cannot modify selected root")
                self._check_directory(parent)
                if kind == 9:
                    if stat.S_ISREG(info.st_mode):
                        self._regular(info)
                    elif not stat.S_ISDIR(info.st_mode):
                        raise Refused("special file")
                    flags = os.O_RDWR if "size" in attrs else os.O_RDONLY
                    fd = os.open(name, flags | _FILE, dir_fd=parent)
                    try:
                        self._check_directory(parent)
                        if _identity(os.fstat(fd)) != _identity(info):
                            raise Refused("object changed")
                        self._set_attributes(fd, attrs)
                    finally:
                        os.close(fd)
                elif kind == 13:
                    if stat.S_ISDIR(info.st_mode):
                        raise Refused("use rmdir for directories")
                    os.unlink(name, dir_fd=parent)
                elif kind == 14:
                    os.mkdir(name, attrs.get("mode", 0o700) & 0o777, dir_fd=parent)
                else:
                    os.rmdir(name, dir_fd=parent)
        elif kind == 18:
            old, new = request.string(), request.string()
            request.end()
            self._rename(old, new, replace=False)
        elif kind == 20:
            # OpenSSH/sshfs compatibility: target first, then new link name.
            target, path = request.string(), request.string()
            request.end()
            with self._resolve(path, follow=False, missing=True) as (parent, name, parts, _):
                combined = target if target.startswith(b"/") else b"/".join([*parts[:-1], target])
                with self._resolve(combined, missing=True) as (_, _, target_parts, _):
                    relative = posixpath.relpath(b"/" + b"/".join(target_parts),
                                                b"/" + b"/".join(parts[:-1]))
                self._check_directory(parent)
                os.symlink(relative, name, dir_fd=parent)
        elif kind == 200:
            extension = request.string()
            if extension == b"posix-rename@openssh.com":
                old, new = request.string(), request.string()
                request.end()
                self._rename(old, new, replace=True)
            elif extension == b"fsync@openssh.com":
                handle = self._handle(request.string())
                request.end()
                os.fsync(handle.fd)
            elif extension == b"limits@openssh.com":
                request.end()
                return 201, b"".join(_u64(n) for n in (MAX_PACKET + 4, MAX_IO, MAX_IO, MAX_HANDLES))
            else:
                return 101, self._status(UNSUPPORTED)
        else:
            return 101, self._status(UNSUPPORTED)
        return 101, self._status(OK)

    @staticmethod
    def _status(code: int) -> bytes:
        messages = {OK: b"OK", EOF: b"End of file", NO_FILE: b"Not found",
                    DENIED: b"Access refused", FAILURE: b"Filesystem operation failed",
                    BAD_MESSAGE: b"Invalid request", UNSUPPORTED: b"Unsupported operation"}
        return _u32(code) + _string(messages[code]) + _string(b"")

    def handle_packet(self, packet: bytes) -> bytes:
        with self.lock:
            if (self.root_fd < 0 or len(packet) < 5 or len(packet) > MAX_PACKET + 4
                    or struct.unpack("!I", packet[:4])[0] != len(packet) - 4):
                raise ProtocolError("invalid packet framing")
            request = Reader(packet[5:])
            kind = packet[4]
            if kind == 1:
                if self.initialized or request.u32() < 3:
                    raise ProtocolError("unsupported initialization")
                while request.position < len(request.data):
                    request.string(), request.string()
                self.initialized = True
                result = b"\x02" + _u32(3)
                for name in (b"posix-rename@openssh.com", b"fsync@openssh.com",
                             b"limits@openssh.com"):
                    result += _string(name) + _string(b"1")
            else:
                if not self.initialized:
                    raise ProtocolError("initialization required")
                identifier = request.u32()
                try:
                    self._refresh_protected()
                    response, data = self._dispatch(kind, request)
                except ProtocolError:
                    response, data = 101, self._status(BAD_MESSAGE)
                except OSError as exc:
                    code = NO_FILE if exc.errno in (errno.ENOENT, errno.ENOTDIR) else (
                        DENIED if isinstance(exc, PermissionError) or exc.errno in
                        (errno.EACCES, errno.EPERM, errno.ELOOP) else FAILURE)
                    response, data = 101, self._status(code)
                except (OverflowError, ValueError):
                    response, data = 101, self._status(BAD_MESSAGE)
                result = bytes([response]) + _u32(identifier) + data
            return _u32(len(result)) + result

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        try:
            while True:
                prefix = _exact(stdin, 4, eof=True)
                if not prefix:
                    return
                length = struct.unpack("!I", prefix)[0]
                if not 1 <= length <= MAX_PACKET:
                    raise ProtocolError("packet exceeds transport limit")
                response = self.handle_packet(prefix + _exact(stdin, length))
                remaining = memoryview(response)
                while remaining:
                    written = stdout.write(remaining)
                    if written is None or written <= 0:
                        raise ProtocolError("response stream is unavailable")
                    remaining = remaining[written:]
                stdout.flush()
        finally:
            self.close()


def _exact(stream: BinaryIO, size: int, *, eof: bool = False) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            if not result and eof:
                return b""
            raise ProtocolError("truncated packet")
        result.extend(chunk)
    return bytes(result)


def serve(root: Path, protected: Iterable[Path], stdin: BinaryIO, stdout: BinaryIO) -> None:
    SFTPServer(root, protected).serve(stdin, stdout)
