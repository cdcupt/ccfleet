"""Broker-to-home rendezvous. Only Fleet's local processes can request a stream."""

import contextlib
import os
import re
import secrets
import socket
import sqlite3
import stat
import time
from pathlib import Path

SUFFIX = ".home-mac.invalid"
ID = re.compile(r"[0-9a-f]{32}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS home_mac_streams(
 ticket TEXT PRIMARY KEY, registration_id TEXT NOT NULL, expires REAL NOT NULL,
 taken INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS home_mac_stream_wait ON home_mac_streams(registration_id,taken,expires);
"""


def registration(host):
    if not isinstance(host, str) or not host.endswith(SUFFIX):
        return None
    value = host[: -len(SUFFIX)]
    if not ID.fullmatch(value):
        raise OSError("Invalid home Mac route.")
    return value


def socket_path(db_path, ticket):
    if not ID.fullmatch(ticket):
        raise ValueError("Invalid stream identifier.")
    folder = Path(db_path).resolve().parent / "home-mac-streams"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("Unsafe home Mac stream directory.")
    return str(folder / ticket)


def open_stream(db_path, reg, timeout=9):
    try:
        return _open_stream(db_path, reg, timeout)
    except sqlite3.Error as exc:
        raise OSError("Home Mac route storage is unavailable.") from exc


def _open_stream(db_path, reg, timeout):
    if not ID.fullmatch(reg):
        raise OSError("Invalid home Mac registration.")
    ticket = secrets.token_hex(16)
    path = socket_path(db_path, ticket)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        try:
            listener.listen(1)
            listener.settimeout(timeout)
            with contextlib.closing(sqlite3.connect(db_path, timeout=2)) as db:
                now = time.time()
                with db:
                    # Recover socket files left behind if a broker died while waiting.
                    expired = db.execute(
                        "SELECT ticket FROM home_mac_streams WHERE expires<=? LIMIT 64", (now,)
                    ).fetchall()
                    for (old,) in expired:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(socket_path(db_path, old))
                        db.execute("DELETE FROM home_mac_streams WHERE ticket=?", (old,))
                    inserted = db.execute(
                        "INSERT INTO home_mac_streams(ticket,registration_id,expires) "
                        "SELECT ?,h.registration_id,? FROM home_macs h JOIN nodes n "
                        "ON n.id=h.node_id "
                        "WHERE h.registration_id=? AND h.removed=0 AND n.enabled=1 AND "
                        "(SELECT COUNT(*) FROM home_mac_streams WHERE registration_id=?)<8",
                        (ticket, now + timeout, reg, reg),
                    ).rowcount
                if inserted != 1:
                    raise OSError("Home Mac unavailable.")
                try:
                    peer, _ = listener.accept()
                    peer.settimeout(None)
                    return peer
                finally:
                    with db:
                        db.execute("DELETE FROM home_mac_streams WHERE ticket=?", (ticket,))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


def take(store, reg, now):
    # An idle poll only reads. Acquire the write lock when there is work.
    with store._lock:
        row = store._conn.execute(
            "SELECT ticket FROM home_mac_streams WHERE registration_id=? AND taken=0 "
            "AND expires>? ORDER BY expires LIMIT 1",
            (reg, now),
        ).fetchone()
    if row:
        with store._write_txn() as db:
            if db.execute(
                "UPDATE home_mac_streams SET taken=1 WHERE ticket=? AND taken=0 AND expires>?",
                (row[0], now),
            ).rowcount:
                return row[0]
    return None


def accept(store, db_path, reg, ticket):
    if not ID.fullmatch(ticket):
        return None
    with store._write_txn() as db:
        ok = db.execute(
            "UPDATE home_mac_streams SET taken=2 WHERE ticket=? "
            "AND registration_id=? AND taken=1 AND expires>?",
            (ticket, reg, time.time()),
        ).rowcount
    if not ok:
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(socket_path(db_path, ticket))
        sock.settimeout(None)
        with store._write_txn() as db:
            if (
                db.execute(
                    "UPDATE home_macs SET tunnel_seen=? WHERE registration_id=? AND removed=0",
                    (time.time(), reg),
                ).rowcount
                != 1
            ):
                sock.close()
                return None
        return sock
    except BaseException:
        sock.close()
        raise
