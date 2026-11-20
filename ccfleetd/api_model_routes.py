"""Explicit API-model grants for one endpoint, holder and rental.

The connection's original Claude/Devin route stays independent. Replacing a
binding or editing a connection requires an explicit new grant.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from ccfleet_agent import api_model_contract as contract

from . import private_owner

ALIASES = contract.ALIASES
LABELS = {"openai": "GPT", "gemini": "Gemini"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_model_bindings (
    id TEXT PRIMARY KEY,
    owner_account_id TEXT NOT NULL REFERENCES accounts(id),
    claim_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK(provider IN ('openai','gemini')),
    model TEXT NOT NULL,
    account_fp TEXT NOT NULL,
    credential_fp TEXT,
    UNIQUE(provider, owner_account_id),
    UNIQUE(provider, account_fp),
    UNIQUE(provider, credential_fp),
    UNIQUE(provider, claim_id)
);
CREATE TABLE IF NOT EXISTS api_model_grants (
    binding_id TEXT NOT NULL REFERENCES api_model_bindings(id) ON DELETE CASCADE,
    key_id TEXT NOT NULL REFERENCES gateway_keys(id) ON DELETE CASCADE,
    key_generation INTEGER NOT NULL,
    PRIMARY KEY(binding_id, key_id)
);
CREATE INDEX IF NOT EXISTS ix_api_model_grants_key ON api_model_grants(key_id,key_generation);
CREATE TRIGGER IF NOT EXISTS api_model_binding_removed AFTER DELETE ON api_model_bindings
BEGIN
    DELETE FROM api_model_grants WHERE binding_id=OLD.id;
END;
"""
FIELDS = contract.FIELDS


def enrollment(conn, key_id, slot_id):
    key = conn.execute("SELECT * FROM gateway_keys WHERE id=? AND revoked_at IS NULL",
                       (key_id,)).fetchone()
    slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    if (not key or not slot or not private_owner.slot_allowed(conn, slot_id, key["account_id"])
            or slot["state"] not in ("claimed", "active")
            or slot["held_by"] != key["account_id"]
            or len(json.loads(key["slot_ids"])) != 1):
        raise ValueError("select a single-slot endpoint and a current active rental of its owner")
    # The base route must still refer to its original rental, too.
    selected = json.loads(key["slot_ids"])[0]
    base = conn.execute("SELECT gateway_claim_id, held_by, state FROM slots WHERE id=?",
                        (selected,)).fetchone()
    if (not base or not private_owner.slot_allowed(conn, selected, key["account_id"])
            or base["held_by"] != key["account_id"]
            or base["state"] not in ("claimed", "active")
            or json.loads(key["claim_bindings"]).get(selected) != base["gateway_claim_id"]):
        raise ValueError("the base endpoint rental has changed")
    return key, slot


def reserve(conn, key_id, slot_id, provider, scope, model):
    key, slot = enrollment(conn, key_id, slot_id)
    receipt = {"provider": provider, "account_fp": contract.account_fingerprint(provider, scope),
               "model": model, "claim_id": slot["gateway_claim_id"], "binding_id": uuid.uuid4().hex}
    contract.validate(receipt, credential=False)
    previous = conn.execute("SELECT * FROM api_model_bindings WHERE claim_id=? AND provider=?",
                            (receipt["claim_id"], provider)).fetchone()
    if previous:
        receipt["binding_id"] = previous["id"]
        if any(previous[k] != receipt[k] for k in ("account_fp", "model")):
            raise ValueError("remove the previous provider binding before replacing it")
        return receipt
    try:
        conn.execute("INSERT INTO api_model_bindings "
                     "(id,owner_account_id,provider,model,claim_id,account_fp) "
                     "VALUES (?,?,?,?,?,?)",
                     (receipt["binding_id"], key["account_id"], provider, model,
                      receipt["claim_id"], receipt["account_fp"]))
    except sqlite3.IntegrityError:
        raise ValueError("the provider account already has an assigned slot") from None
    return receipt


def grant(conn, key_id, slot_id, receipt):
    contract.validate(receipt)
    key, slot = enrollment(conn, key_id, slot_id)
    binding = conn.execute("SELECT * FROM api_model_bindings WHERE id=? AND owner_account_id=?",
                           (receipt["binding_id"], key["account_id"])).fetchone()
    if (not binding or slot["gateway_claim_id"] != receipt["claim_id"]
            or any(binding[k] != receipt[k] for k in contract.RESERVATION if k != "binding_id")
            or binding["credential_fp"] not in (None, receipt["credential_fp"])):
        raise ValueError("reserve the current rental and enroll its matching credential")
    try:
        conn.execute("UPDATE api_model_bindings SET credential_fp=? WHERE id=?",
                     (receipt["credential_fp"], binding["id"]))
    except sqlite3.IntegrityError:
        raise ValueError("the provider key already has an assigned slot") from None
    conn.execute("INSERT OR REPLACE INTO api_model_grants VALUES (?,?,?)",
                 (binding["id"], key_id, key["generation"]))


def targets(conn, key_id, generation):
    rows = conn.execute("""
        SELECT b.id AS binding_id,b.provider,b.model,b.claim_id,b.account_fp,b.credential_fp,
               b.owner_account_id,
               s.id AS slot_id,s.node_id,s.unix_user,s.gateway_generation AS claim_generation,
               n.access_host,n.access_port,n.ssh_host_key,k.slot_ids,k.claim_bindings
        FROM gateway_keys k JOIN api_model_grants g
          ON g.key_id=k.id AND g.key_generation=k.generation
        JOIN api_model_bindings b ON b.id=g.binding_id AND b.owner_account_id=k.account_id
        JOIN slots s ON s.gateway_claim_id=b.claim_id AND s.held_by=k.account_id
        JOIN nodes n ON n.id=s.node_id
        WHERE k.id=? AND k.generation=? AND k.revoked_at IS NULL AND s.kind IN ('machine','owner')
          AND s.state IN ('claimed','active') AND n.enabled=1
          AND n.access_host!='' AND n.ssh_host_key!=''
          AND NOT EXISTS (SELECT 1 FROM logins l WHERE l.node_id='slot:' || s.id
                AND l.kind IN ('login','switch')
                AND l.state IN ('requested','url_ready','code_sent'))
        ORDER BY b.provider DESC
        """, (key_id, generation)).fetchall()
    if not rows:
        return []
    slots = json.loads(rows[0]["slot_ids"])
    claims = json.loads(rows[0]["claim_bindings"])
    if (len(slots) != 1
            or not private_owner.slot_allowed(conn, slots[0], rows[0]["owner_account_id"])):
        return []
    if not conn.execute(
            "SELECT 1 FROM slots s JOIN gateway_keys k ON k.account_id=s.held_by "
            "WHERE k.id=? AND s.id=? AND s.gateway_claim_id=? AND s.state IN ('claimed','active')",
            (key_id, slots[0], claims.get(slots[0]))).fetchone():
        return []
    internal = {"slot_ids", "claim_bindings", "owner_account_id"}
    return [{k: row[k] for k in row.keys() if k not in internal}
            for row in rows
            if private_owner.slot_allowed(conn, row["slot_id"], row["owner_account_id"])]
