"""Synthetic selected roots exercise the actual framed live-filesystem protocol."""

from __future__ import annotations

import io
import os
import shlex
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import live_files as live


def u32(value):
    return struct.pack("!I", value)


def u64(value):
    return struct.pack("!Q", value)


def string(value):
    value = os.fsencode(value)
    return u32(len(value)) + value


def packet(kind, payload):
    return u32(1 + len(payload)) + bytes([kind]) + payload


class Client:
    def __init__(self, root, protected=()):
        self.server = live.SFTPServer(root, protected)
        self.last = self.server.handle_packet(packet(1, u32(3)))
        assert self.last[4] == 2
        self.sequence = 0

    def request(self, kind, payload=b""):
        self.sequence += 1
        self.last = self.server.handle_packet(packet(kind, u32(self.sequence) + payload))
        assert struct.unpack("!I", self.last[:4])[0] == len(self.last) - 4
        assert struct.unpack("!I", self.last[5:9])[0] == self.sequence
        return self.last[4], live.Reader(self.last[9:])

    def status(self, kind, payload=b""):
        response, reader = self.request(kind, payload)
        assert response == 101
        return reader.u32()

    def open(self, path, flags=3 | 8, attrs=b"\0\0\0\0"):
        kind, reader = self.request(3, string(path) + u32(flags) + attrs)
        assert kind == 102, self.last
        return reader.string()

    def write(self, handle, offset, data):
        return self.status(6, string(handle) + u64(offset) + string(data))

    def read(self, handle, offset, length):
        kind, reader = self.request(5, string(handle) + u64(offset) + u32(length))
        assert kind == 103, self.last
        return reader.string()


@pytest.fixture
def root(tmp_path):
    path = tmp_path.resolve() / "synthetic-root"
    path.mkdir()
    return path


@pytest.fixture
def client(root):
    value = Client(root)
    yield value
    value.server.close()


def test_stream_supports_version_and_stat_without_host_metadata(root):
    output = io.BytesIO()
    requests = packet(1, u32(3)) + packet(16, u32(1) + string("."))
    live.serve(root, [], io.BytesIO(requests), output)
    wire = output.getvalue()
    assert os.fsencode(root) not in wire
    assert b"posix-rename@openssh.com" in wire
    assert b"fsync@openssh.com" in wire
    assert b"limits@openssh.com" in wire


def test_stdio_wrapper_handles_partial_reads_and_writes(root):
    class ShortInput(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 2))

    class ShortOutput(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    output = ShortOutput()
    live.serve(root, [], ShortInput(packet(1, u32(3))), output)
    wire = output.getvalue()
    assert struct.unpack("!I", wire[:4])[0] == len(wire) - 4
    assert wire[4] == 2


def test_file_open_write_read_stat_truncate_chmod_times_fsync_close(client, root):
    handle = client.open("file")
    assert client.write(handle, 0, b"hello world") == live.OK
    assert client.read(handle, 6, 5) == b"world"
    kind, reader = client.request(8, string(handle))
    assert kind == 105
    attrs = reader.attrs()
    assert attrs["owner"] == (0, 0) and attrs["size"] == 11
    assert client.status(10, string(handle) + u32(1 | 4 | 8) + u64(5)
                         + u32(0o640) + u32(1000) + u32(2000)) == live.OK
    assert client.status(200, string("fsync@openssh.com") + string(handle)) == live.OK
    assert (root / "file").read_bytes() == b"hello"
    assert stat.S_IMODE((root / "file").stat().st_mode) == 0o640
    assert int((root / "file").stat().st_mtime) == 2000
    assert client.status(4, string(handle)) == live.OK
    assert client.status(4, string(handle)) == live.BAD_MESSAGE


def test_append_and_exclusive_create(client, root):
    (root / "file").write_bytes(b"a")
    handle = client.open("file", 2 | 4)
    assert client.write(handle, 0, b"b") == live.OK
    assert client.write(handle, 0, b"c") == live.OK
    assert (root / "file").read_bytes() == b"abc"
    assert client.status(3, string("file") + u32(2 | 8 | 32) + u32(0)) == live.FAILURE


def test_runtime_control_directory_is_protected_before_link_is_exposed(client, root):
    control = root / "temporary-control"
    control.mkdir()
    (control / "socket-state").write_bytes(b"private")
    handle = client.open("temporary-control/socket-state", flags=1)
    client.server.protect(control)
    assert client.status(5, string(handle) + u64(0) + u32(7)) == live.DENIED
    assert client.status(13, string("temporary-control/socket-state")) == live.DENIED
    assert client.status(18, string("temporary-control") + string("moved")) == live.DENIED
    assert (control / "socket-state").read_bytes() == b"private"


def test_large_file_is_accessed_in_chunks_without_whole_file_caps(client, root):
    offset = 64 * 1024 * 1024 + 19
    handle = client.open("large")
    assert client.write(handle, offset, b"after-sixty-four-MiB") == live.OK
    assert (root / "large").stat().st_size > 64 * 1024 * 1024
    assert client.read(handle, offset, 20) == b"after-sixty-four-MiB"
    assert client.status(5, string(handle) + u64(offset + 100) + u32(1)) == live.EOF


def test_directory_enumeration_exceeds_snapshot_file_count_without_eager_scan(client, root, monkeypatch):
    for index in range(1101):
        (root / f"file-{index}").touch()
    original = os.scandir
    calls = []
    monkeypatch.setattr(os, "scandir", lambda fd: calls.append(fd) or original(fd))
    assert calls == []
    kind, reader = client.request(11, string("/"))
    assert kind == 102 and len(calls) == 1
    handle, names = reader.string(), []
    while True:
        kind, reader = client.request(12, string(handle))
        if kind == 101:
            assert reader.u32() == live.EOF
            break
        assert kind == 104
        for _ in range(reader.u32()):
            names.append(reader.string())
            reader.string(), reader.attrs()
    assert len(names) == 1101 and len(set(names)) == 1101
    assert client.status(4, string(handle)) == live.OK


def test_arbitrary_filename_bytes_and_dotfiles_are_not_snapshot_filtered(client, root):
    names = ["oddéname".encode(), b".env", b"with space", b"with\nnewline", b".gitconfig"]
    for name in names:
        handle = client.open(name)
        assert client.write(handle, 0, name) == live.OK
        assert client.read(handle, 0, 100) == name
        assert client.status(4, string(handle)) == live.OK
    assert len(os.listdir(root)) == len(names)


def test_non_utf8_filename_matches_native_filesystem_support(client, root):
    name = b"odd\xffname"
    try:
        fd = os.open(os.fsencode(root) + b"/" + name, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        # APFS rejects invalid UTF-8 even for native POSIX byte-string calls.
        assert client.status(3, string(name) + u32(3 | 8) + u32(0)) == live.FAILURE
    else:
        os.close(fd)
        handle = client.open(name)
        assert client.write(handle, 0, b"native bytes") == live.OK
        assert client.read(handle, 0, 20) == b"native bytes"


@pytest.mark.parametrize("path", [b"../outside", b"/../outside", b"dir/../../outside"])
def test_traversal_never_creates_or_reads_outside_root(client, root, path):
    (root / "dir").mkdir(exist_ok=True)
    outside = root.parent / "outside"
    outside.write_text("do not change")
    assert client.status(3, string(path) + u32(3 | 8 | 16) + u32(0)) == live.DENIED
    assert outside.read_text() == "do not change"
    assert os.fsencode(root) not in client.last


def test_inside_symlinks_work_and_readlink_never_exports_absolute_host_path(client, root):
    (root / "file").write_bytes(b"inside")
    (root / "absolute-link").symlink_to(root / "file")
    (root / "relative-link").symlink_to("file")
    for name in ("absolute-link", "relative-link"):
        handle = client.open(name, 1)
        assert client.read(handle, 0, 20) == b"inside"
        kind, reader = client.request(19, string(name))
        assert kind == 104 and reader.u32() == 1 and reader.string() == b"file"
        assert os.fsencode(root) not in client.last
    assert client.status(20, string("/file") + string("new-link")) == live.OK
    assert os.readlink(root / "new-link") == "file"
    assert client.read(client.open("new-link", 1), 0, 20) == b"inside"


def test_outside_symlinks_and_loops_are_refused(client, root):
    outside = root.parent / "outside"
    outside.write_text("outside secret")
    (root / "escape").symlink_to(outside)
    (root / "loop").symlink_to("loop")
    for name in ("escape", "loop"):
        assert client.status(3, string(name) + u32(3 | 16) + u32(0)) == live.DENIED
        assert client.status(19, string(name)) == live.DENIED
    assert outside.read_text() == "outside secret"
    assert client.status(20, string("../outside") + string("new-escape")) == live.DENIED
    assert not (root / "new-escape").exists()


def test_hardlinked_files_cannot_read_write_or_truncate_outside_alias(client, root):
    outside = root.parent / "outside"
    outside.write_bytes(b"preserve outside alias")
    os.link(outside, root / "inside-link")
    assert client.status(3, string("inside-link") + u32(3 | 16) + u32(0)) == live.DENIED
    assert client.status(9, string("inside-link") + u32(1) + u64(0)) == live.DENIED
    assert outside.read_bytes() == b"preserve outside alias"


def test_protected_files_subtrees_and_aliases_cannot_be_read_written_or_renamed(root):
    config = root / "configuration"
    config.mkdir()
    (config / "token").write_text("private token")
    (root / "key").write_text("private key")
    (root / "alias").symlink_to("key")
    client = Client(root, [config, root / "key"])
    try:
        for name in ("configuration/token", "configuration/new", "key", "alias"):
            assert client.status(3, string(name) + u32(3 | 8 | 16) + u32(0)) == live.DENIED
        assert client.status(18, string("configuration") + string("moved")) == live.DENIED
        assert client.status(9, string("configuration") + u32(4) + u32(0o777)) == live.DENIED
        assert (config / "token").read_text() == "private token"
        assert (root / "key").read_text() == "private key"
    finally:
        client.server.close()


def test_open_parent_moved_outside_root_is_refused_before_later_write(client, root):
    directory = root / "directory"
    directory.mkdir()
    handle = client.open("directory/file")
    assert client.write(handle, 0, b"before") == live.OK
    outside = root.parent / "moved-outside"
    directory.rename(outside)
    assert client.write(handle, 0, b"ATTACK") == live.DENIED
    assert (outside / "file").read_bytes() == b"before"


def test_protected_ancestor_case_alias_cannot_be_renamed(root):
    protected = root / "Configuration/control"
    protected.mkdir(parents=True)
    (protected / "private").write_text("do not share")
    client = Client(root, [protected])
    try:
        # On a case-sensitive filesystem this is simply absent; on APFS the
        # same directory must not be moved around its protected descendant.
        assert client.status(18, string("CONFIGURATION") + string("renamed")) in (
            live.DENIED, live.NO_FILE)
        assert (protected / "private").read_text() == "do not share"
    finally:
        client.server.close()


def test_previously_protected_inode_remains_protected_after_local_rename(root):
    protected = root / "key"
    protected.write_text("private material")
    client = Client(root, [protected])
    try:
        assert client.status(17, string("key")) == live.DENIED
        protected.rename(root / "renamed-key")
        assert client.status(3, string("renamed-key") + u32(1) + u32(0)) == live.DENIED
    finally:
        client.server.close()


def test_path_and_handle_setstat_directory_stat_and_limits(client, root):
    handle = client.open("file")
    assert client.status(9, string("file") + u32(1 | 4) + u64(1234) + u32(0o640)) == live.OK
    assert (root / "file").stat().st_size == 1234
    kind, attrs = client.request(7, string("file"))
    assert kind == 105 and attrs.attrs()["size"] == 1234
    assert client.status(14, string("directory") + u32(0)) == live.OK
    assert client.status(9, string("directory") + u32(4) + u32(0o750)) == live.OK
    kind, reader = client.request(11, string("directory"))
    assert kind == 102
    directory = reader.string()
    kind, reader = client.request(8, string(directory))
    assert kind == 105 and stat.S_ISDIR(reader.attrs()["mode"])
    kind, reader = client.request(200, string("limits@openssh.com"))
    assert kind == 201
    assert [reader.u64() for _ in range(4)] == [live.MAX_PACKET + 4, live.MAX_IO,
                                               live.MAX_IO, live.MAX_HANDLES]
    assert client.status(4, string(handle)) == live.OK


def test_directory_detachment_during_resolution_cannot_create_outside_file(client, root, monkeypatch):
    (root / "directory").mkdir()
    outside = root.parent / "moved-outside"
    original = os.open
    triggered = False

    def moving_open(path, flags, *args, **kwargs):
        nonlocal triggered
        fd = original(path, flags, *args, **kwargs)
        if path == b"directory" and flags & os.O_DIRECTORY and not triggered:
            triggered = True
            (root / "directory").rename(outside)
        return fd

    monkeypatch.setattr(os, "open", moving_open)
    assert client.status(3, string("directory/new") + u32(3 | 8) + u32(0)) == live.DENIED
    assert triggered and not (outside / "new").exists()


def test_rename_updates_open_handles_and_posix_replace_is_supported(client, root):
    handle = client.open("old")
    assert client.write(handle, 0, b"a") == live.OK
    assert client.status(18, string("old") + string("new")) == live.OK
    assert client.write(handle, 1, b"b") == live.OK
    (root / "replace").write_bytes(b"old target")
    assert client.status(18, string("new") + string("replace")) == live.FAILURE
    assert client.status(200, string("posix-rename@openssh.com")
                         + string("new") + string("replace")) == live.OK
    assert client.read(handle, 0, 100) == b"ab"
    assert (root / "replace").read_bytes() == b"ab"


def test_mkdir_setstat_remove_rmdir_and_virtual_realpath(client, root):
    assert client.status(14, string("directory") + u32(4) + u32(0o750)) == live.OK
    handle = client.open("directory/file")
    assert client.status(4, string(handle)) == live.OK
    kind, reader = client.request(16, string("directory/../directory/file"))
    assert kind == 104 and reader.u32() == 1 and reader.string() == b"/directory/file"
    assert client.status(13, string("directory/file")) == live.OK
    assert client.status(15, string("directory")) == live.OK
    assert not (root / "directory").exists()


def test_unknown_operations_and_extensions_never_execute_a_command(client, root):
    assert client.status(222, string("touch forbidden")) == live.UNSUPPORTED
    assert client.status(200, string("exec") + string("touch forbidden")) == live.UNSUPPORTED
    assert not list(root.iterdir())


def test_synthetic_ownership_cannot_change_real_ownership(client):
    handle = client.open("file")
    assert client.status(10, string(handle) + u32(2) + u32(501) + u32(20)) == live.DENIED
    assert client.status(10, string(handle) + u32(4) + u32(0o4755)) == live.DENIED


def test_packet_and_io_bounds_do_not_create_whole_file_limits(client):
    handle = client.open("file")
    assert client.write(handle, 0, b"x" * (live.MAX_IO + 1)) == live.BAD_MESSAGE
    assert client.write(handle, 0, b"x" * live.MAX_IO) == live.OK
    assert len(client.read(handle, 0, live.MAX_IO * 2)) == live.MAX_IO
    with pytest.raises(live.ProtocolError):
        client.server.handle_packet(u32(live.MAX_PACKET + 1) + b"x" * (live.MAX_PACKET + 1))


@pytest.mark.parametrize("data", [b"\0", u32(0), u32(100) + b"short"])
def test_malformed_stream_closes_without_host_path_leak(root, data):
    with pytest.raises(live.ProtocolError) as error:
        live.serve(root, [], io.BytesIO(data), io.BytesIO())
    assert str(root) not in str(error.value)


def test_trailing_request_data_fails_before_creating_file(client, root):
    assert client.status(3, string("not-created") + u32(3 | 8) + u32(0) + b"bad") == live.BAD_MESSAGE
    assert not (root / "not-created").exists()


def test_close_releases_handles_and_is_idempotent(client):
    client.open("file")
    descriptors = [handle.fd for handle in client.server.handles.values()]
    client.server.close()
    client.server.close()
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_native_openssh_sftp_interoperability_without_network(root):
    sftp = shutil.which("sftp")
    if sftp is None:
        pytest.skip("OpenSSH sftp client is not installed")
    source = root.parent / "sftp-stdio-server.py"
    repo = Path(__file__).resolve().parents[1]
    source.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from pathlib import Path\n"
        "from ccfleet_agent.live_files import serve\n"
        f"serve(Path({str(root)!r}), [], sys.stdin.buffer, sys.stdout.buffer)\n")
    (root / "upload.txt").write_text("native OpenSSH round trip\n")
    command = shlex.quote(sys.executable) + " " + shlex.quote(str(source))
    completed = subprocess.run(
        [sftp, "-q", "-b", "-", "-D", command], cwd=root, capture_output=True,
        input="pwd\nls\nput upload.txt remote.txt\nget remote.txt downloaded.txt\n"
              "mkdir folder\nrename remote.txt folder/renamed.txt\n"
              "rm folder/renamed.txt\nrmdir folder\n", text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert (root / "downloaded.txt").read_bytes() == (root / "upload.txt").read_bytes()
    assert not (root / "folder").exists()
