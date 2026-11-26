"""Owner-approved home Mac registration. Device check-ins survive network changes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import unicodedata

from ccfleet_agent.client_release import ReleaseError, trusted_public_key

from . import home_mac_transport, private_owner

PAGE = "/account/machines"
ADD = PAGE + "/add"
CONFIRM = PAGE + "/confirm"
POOL = "/account/dedicated"
PROBE = PAGE + "/probe"
STATUS = PAGE + "/status"
ADD_ACTION = PAGE + "/add-to-pool"
ACTIONS = {PAGE + "/approve", PAGE + "/remove", PROBE, ADD_ACTION, POOL + "/connect"}
API = "/api/home-macs/"
TTL = 600
OFFLINE_AFTER = 120
CAPACITY = 4096
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
VERIFY_SLOTS = threading.BoundedSemaphore(4)
SCHEMA = """
CREATE TABLE IF NOT EXISTS home_mac_pending (
 device_hash TEXT PRIMARY KEY, public_key TEXT NOT NULL, host_key TEXT NOT NULL,
 unix_user TEXT NOT NULL, expires REAL NOT NULL, code_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS home_mac_pending_expiry ON home_mac_pending(expires);
CREATE TABLE IF NOT EXISTS home_macs (
 registration_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 node_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, public_key TEXT NOT NULL,
 device_hash TEXT NOT NULL UNIQUE, seen REAL, tunnel_seen REAL, removed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS home_mac_active_key ON home_macs(public_key) WHERE removed=0;
"""


class Error(ValueError):
    def __init__(self, status, english, chinese):
        super().__init__(english)
        self.status = status
        self.message = (english, chinese)


def invalid():
    return Error(400, "Check the code on your Mac and try again.", "请核对 Mac 上的配对码后重试。")


def denied():
    return Error(403, "This Mac registration is unavailable.", "此 Mac 注册不可用。")


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def code_for(secret, device, expiry):
    # The Mac can recover its pending code after a lost HTTP response.
    raw = hmac.digest(secret.encode(), f"home-mac-v1:{device}:{expiry}".encode(), "sha256")
    bits = int.from_bytes(raw[:7], "big")
    return "".join(ALPHABET[(bits >> (i * 5)) & 31] for i in range(10))


def keys(public, host):
    try:
        if not all(isinstance(k, str) and len(k) < 160 for k in (public, host)):
            raise ValueError
        public, host = trusted_public_key(public), trusted_public_key(host)
        if public == host:
            raise ValueError
        return public, host
    except (ValueError, ReleaseError) as exc:
        raise invalid() from exc


def proof_message(token, public, host, unix_user):
    return f"ccfleet-home-enroll-v1\n{digest(token)}\n{public}\n{host}\n{unix_user}\n".encode()


def verify_enrollment(token, public, host, unix_user, proof):
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not isinstance(proof, str) or not proof.isascii() or len(proof) > 2048:
        raise invalid()
    if not VERIFY_SLOTS.acquire(blocking=False):
        raise Error(429, "Try registration again in a moment.", "请稍后重试注册。")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            signers, signature = Path(tmp) / "signers", Path(tmp) / "signature"
            signers.write_text("mac " + public + "\n")
            signature.write_text(proof)
            cmd = shutil.which("ssh-keygen", path=os.defpath)
            if not cmd:
                raise OSError("OpenSSH unavailable")
            result = subprocess.run(
                [
                    cmd,
                    "-Y",
                    "verify",
                    "-I",
                    "mac",
                    "-n",
                    "ccfleet-home-enroll-v1",
                    "-f",
                    str(signers),
                    "-s",
                    str(signature),
                ],
                input=proof_message(token, public, host, unix_user),
                capture_output=True,
                timeout=3,
            )
            if result.returncode:
                raise denied()
    except (OSError, subprocess.SubprocessError) as exc:
        raise Error(
            503, "Mac registration verification is unavailable.", "暂时无法验证 Mac 注册。"
        ) from exc
    finally:
        VERIFY_SLOTS.release()


def begin(store, secret, token, fields, now):
    if set(fields) != {"public_key", "host_key", "unix_user", "proof"}:
        raise invalid()
    public, host = keys(fields["public_key"], fields["host_key"])
    user = fields["unix_user"]
    if (
        not isinstance(user, str)
        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", user)
        or user == "root"
    ):
        raise invalid()
    verify_enrollment(token, public, host, user, fields["proof"])
    device = digest(token)
    with store._write_txn() as conn:
        current = conn.execute(
            "SELECT h.removed,h.public_key,n.ssh_host_key,n.owner FROM home_macs h "
            "JOIN nodes n ON n.id=h.node_id WHERE h.device_hash=?",
            (device,),
        ).fetchone()
        if current:
            if current["removed"] or (
                current["public_key"],
                current["ssh_host_key"],
                current["owner"],
            ) != (public, host, user):
                raise denied()
            return {"state": "registered"}
        conn.execute("DELETE FROM home_mac_pending WHERE expires<=?", (now,))
        row = conn.execute(
            "SELECT * FROM home_mac_pending WHERE device_hash=?", (device,)
        ).fetchone()
        if row:
            if (row["public_key"], row["host_key"], row["unix_user"]) != (public, host, user):
                raise denied()
            expiry = int(row["expires"])
        else:
            if conn.execute("SELECT COUNT(*) FROM home_mac_pending").fetchone()[0] >= CAPACITY:
                raise Error(429, "Try registration again later.", "请稍后重试注册。")
            expiry = int(now) + TTL
            code = code_for(secret, device, expiry)
            conn.execute(
                "INSERT INTO home_mac_pending VALUES (?,?,?,?,?,?)",
                (device, public, host, user, expiry, digest(code)),
            )
    return {"state": "pending", "code": code_for(secret, device, expiry), "expires_at": expiry}


def approve(store, owner, code, name, now, *, check_tunnel=False):
    code = code.strip().upper().replace("-", "")
    name = name.strip()
    if not re.fullmatch(f"[{ALPHABET}]{{10}}", code):
        raise invalid()
    if not 1 <= len(name) <= 48 or any(unicodedata.category(c).startswith("C") for c in name):
        raise Error(
            400, "Enter a machine name of 1–48 characters.", "请输入 1 至 48 个字符的设备名称。"
        )
    with store._write_txn() as conn:
        row = conn.execute(
            "SELECT * FROM home_mac_pending WHERE code_hash=? AND expires>?", (digest(code), now)
        ).fetchone()
        if not row:
            raise invalid()
        if conn.execute(
            "SELECT 1 FROM home_macs WHERE public_key=? AND removed=0", (row["public_key"],)
        ).fetchone():
            raise denied()
        # The report credential is activated only when the approved Mac checks in.
        node = "home-" + secrets.token_hex(12)
        registration = secrets.token_hex(16)
        dormant = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO nodes(id,owner,token_hash,created_at,ssh_host_key) VALUES (?,?,?,?,?)",
            (node, row["unix_user"], dormant, now, row["host_key"]),
        )
        conn.execute(
            "INSERT INTO slots(id,node_id,unix_user,state,held_by,claimed_at,kind,"
            "gateway_claim_id)"
            " VALUES (?,?,?,'active',?,?,'owner',?)",
            (node, node, row["unix_user"], owner, now, secrets.token_hex(16)),
        )
        conn.execute(
            "INSERT INTO private_owner_nodes(node_id,account_id,monthly_cost_usd_cents,"
            "created_at,updated_at) VALUES (?,?,0,?,?)",
            (node, owner, now, now),
        )
        conn.execute(
            "INSERT INTO home_macs(registration_id,owner_id,node_id,name,public_key,device_hash) "
            "VALUES (?,?,?,?,?,?)",
            (registration, owner, node, name, row["public_key"], row["device_hash"]),
        )
        conn.execute(
            "UPDATE nodes SET access_host=? WHERE id=?",
            (registration + home_mac_transport.SUFFIX, node),
        )
        conn.execute(
            "UPDATE private_owner_nodes SET inference_enabled=1,updated_at=? WHERE node_id=?",
            (now, node),
        )
        if check_tunnel:
            conn.execute(
                "UPDATE home_macs SET probe_requested=1 WHERE registration_id=?", (registration,)
            )
        conn.execute("DELETE FROM home_mac_pending WHERE device_hash=?", (row["device_hash"],))
        return node


def check_in(store, token, now):
    from .store import hash_token

    with store._write_txn() as conn:
        device = digest(token)
        row = conn.execute(
            "SELECT h.*,n.enabled FROM home_macs h JOIN nodes n ON n.id=h.node_id "
            "WHERE h.device_hash=?",
            (device,),
        ).fetchone()
        if not row:
            if conn.execute(
                "SELECT 1 FROM home_mac_pending WHERE device_hash=? AND expires>?", (device, now)
            ).fetchone():
                return {"state": "pending"}
            raise denied()
        if row["removed"] or not row["enabled"]:
            raise denied()
        reporting = hmac.digest(bytes.fromhex(token), b"ccfleet-home-report-v1", "sha256").hex()
        if row["seen"] is None:
            conn.execute(
                "UPDATE nodes SET token_hash=? WHERE id=?", (hash_token(reporting), row["node_id"])
            )
        if row["seen"] is not None and now - row["seen"] > OFFLINE_AFTER and row["probe_started"]:
            conn.execute(
                "UPDATE home_macs SET probe_requested=1,probe_started=NULL,"
                "probe_finished=NULL,probe_ok=0 WHERE registration_id=?",
                (row["registration_id"],),
            )
        conn.execute(
            "UPDATE home_macs SET seen=? WHERE registration_id=?", (now, row["registration_id"])
        )
        return {"state": "registered", "node_id": row["node_id"], "name": row["name"]}


def list_for_owner(store, owner, now, *, slot_id=None):
    with store._lock:
        rows = store._conn.execute(
            "SELECT h.*,h.registration_id AS id,s.unix_user,s.claimed_at FROM home_macs h "
            "JOIN slots s ON s.id=h.node_id AND s.held_by=h.owner_id "
            "WHERE h.owner_id=? AND h.removed=0 "
            + ("AND h.node_id=? " if slot_id is not None else "")
            + "ORDER BY s.claimed_at,h.node_id",
            (owner, slot_id) if slot_id is not None else (owner,),
        ).fetchall()
        return [_describe(store, row, now) for row in rows]


def for_slot(store, owner, slot_id, now):
    rows = list_for_owner(store, owner, now, slot_id=slot_id)
    return rows[0] if rows else None


def for_owner(store, owner, now):
    """Single-Mac compatibility accessor. Ambiguous selections require an explicit slot."""
    rows = list_for_owner(store, owner, now)
    return rows[0] if len(rows) == 1 else None


def _describe(store, row, now):
    import json

    from . import home_mac_probe

    db = store._conn
    state = (
        "waiting"
        if row["seen"] is None
        else ("online" if 0 <= now - row["seen"] <= OFFLINE_AFTER else "offline")
    )
    provider = "unconfirmed"
    latest = db.execute(
        "SELECT ts,payload FROM heartbeats WHERE node_id=? ORDER BY ts DESC,id DESC LIMIT 1",
        (row["node_id"],),
    ).fetchone()
    if latest and max(now - 180, row["claimed_at"]) <= latest["ts"] <= now:
        if store._gateway_report_ready(db, row["node_id"], row["unix_user"], private=True, now=now):
            provider = "ready"
        else:
            credentials = json.loads(latest["payload"]).get("credentials") or {}
            if credentials.get("logged_in") is False:
                provider = "sign_in"
    return {
        "id": row["id"],
        "name": row["name"],
        "slot_id": row["node_id"],
        "tunnel_state": home_mac_probe.state(row, now),
        "state": state,
        "provider": provider,
        "tunnel_seen": row["tunnel_seen"] is not None,
    }


def remove(store, owner, registration, now):
    with store._write_txn() as conn:
        row = conn.execute(
            "SELECT node_id,removed FROM home_macs WHERE owner_id=? AND registration_id=?",
            (owner, registration),
        ).fetchone()
        if row is None:
            raise denied()
        if row["removed"]:
            return
        node = row["node_id"]
        private_owner.invalidate(conn, node)
        conn.execute("DELETE FROM home_mac_streams WHERE registration_id=?", (registration,))
        conn.execute(
            "UPDATE nodes SET enabled=0,token_hash=?,access_host='',ssh_host_key='' WHERE id=?",
            (secrets.token_hex(32), node),
        )
        conn.execute(
            "UPDATE private_owner_nodes SET inference_enabled=0,updated_at=? WHERE node_id=?",
            (now, node),
        )
        conn.execute(
            "UPDATE home_macs SET removed=1,seen=NULL WHERE registration_id=?", (registration,)
        )


def disconnect(store, token, now):
    with store._lock:
        row = store._conn.execute(
            "SELECT owner_id,registration_id FROM home_macs WHERE device_hash=?", (digest(token),)
        ).fetchone()
    if row is None:
        raise denied()
    remove(store, row["owner_id"], row["registration_id"], now)


def active(store, token_or_hash, cfg, *, hashed=False):
    with store._lock:
        row = store._conn.execute(
            "SELECT h.registration_id,h.node_id,h.owner_id FROM home_macs h "
            "JOIN nodes n ON n.id=h.node_id JOIN private_owner_nodes p ON p.node_id=h.node_id "
            "WHERE h.device_hash=? AND h.removed=0 AND n.enabled=1 AND p.inference_enabled=1",
            (token_or_hash if hashed else digest(token_or_hash),),
        ).fetchone()
    if row and (row["owner_id"] in cfg.home_mac_owners or "*" in cfg.home_mac_owners):
        return dict(row)
    return None
