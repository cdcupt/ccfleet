"""Confirm that an enrolled Mac's restricted SSH entry answers over its outbound tunnel."""

import re
import threading
import time

from . import home_mac_transport, home_macs

WORKERS = threading.BoundedSemaphore(4)
DEADLINE = 16
FRESH = 120


def state(row, now):
    if row["seen"] is None:
        return "waiting"
    if now - row["seen"] > home_macs.OFFLINE_AFTER:
        return "offline"
    if row["probe_finished"] is not None:
        if row["probe_ok"] and now - row["probe_finished"] < FRESH:
            return "connected"
        return "idle" if row["probe_ok"] else "retry"
    if row["probe_started"] is not None:
        return "checking" if now - row["probe_started"] < DEADLINE else "retry"
    return "waiting" if row["probe_requested"] else "idle"


def request(store, owner, slot_id, now):
    with store._write_txn() as db:
        row = db.execute(
            "SELECT h.* FROM home_macs h JOIN slots s ON s.id=h.node_id "
            "AND s.held_by=h.owner_id WHERE h.owner_id=? "
            "AND h.node_id=? AND removed=0",
            (owner, slot_id),
        ).fetchone()
        if not row:
            raise home_macs.denied()
        if (
            row["probe_started"] is not None
            and row["probe_finished"] is None
            and now - row["probe_started"] < DEADLINE
        ):
            return
        db.execute(
            "UPDATE home_macs SET probe_requested=1,probe_started=NULL,"
            "probe_finished=NULL,probe_ok=0 WHERE registration_id=?",
            (row["registration_id"],),
        )


def start_if_requested(store, db_path, registration):
    with store._lock:
        row = store._conn.execute(
            "SELECT probe_requested FROM home_macs WHERE registration_id=? AND removed=0",
            (registration,),
        ).fetchone()
    if not row or not row[0] or not WORKERS.acquire(blocking=False):
        return
    try:
        started = time.time()
        with store._write_txn() as db:
            changed = db.execute(
                "UPDATE home_macs SET probe_requested=0,probe_started=? WHERE "
                "registration_id=? AND removed=0 AND probe_requested=1",
                (started, registration),
            ).rowcount
        if changed:
            threading.Thread(
                target=_run, args=(store, db_path, registration, started), daemon=True
            ).start()
            return
    except BaseException:
        WORKERS.release()
        raise
    WORKERS.release()


def _run(store, db_path, reg, started):
    ok = False
    try:
        with home_mac_transport.open_stream(db_path, reg, timeout=9) as peer:
            end = time.monotonic() + 3
            banner = bytearray()
            while len(banner) < 255 and b"\n" not in banner:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                peer.settimeout(remaining)
                part = peer.recv(255 - len(banner))
                if not part:
                    break
                banner.extend(part)
            ok = bool(re.fullmatch(rb"SSH-2\.0-[!-~]+(?: [ -~]*)?\r?\n", banner))
    except OSError:
        pass
    finally:
        try:
            with store._write_txn() as db:
                db.execute(
                    "UPDATE home_macs SET probe_finished=?,probe_ok=? WHERE "
                    "registration_id=? AND probe_started=? AND removed=0",
                    (time.time(), int(ok), reg, started),
                )
        finally:
            WORKERS.release()
