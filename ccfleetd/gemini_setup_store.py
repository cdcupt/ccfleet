"""Dashboard enrollment is available only to the signed-in owner of an enabled private Mac."""

import json
import time

from . import api_model_routes, private_owner


def retiring(binding):
    return binding["credential_fp"] == "disconnecting:" + binding["id"]


class SetupError(ValueError):
    pass


def state(conn, owner):
    slot = conn.execute(
        "SELECT s.id,s.name,s.unix_user,s.gateway_claim_id AS claim_id,n.access_host,"
        "n.access_port,n.ssh_host_key FROM private_owner_nodes p JOIN nodes n ON n.id=p.node_id "
        "JOIN slots s ON s.id=p.node_id WHERE p.account_id=?",
        (owner,),
    ).fetchone()
    if not slot or not private_owner.eligible(conn, slot["id"], owner):
        raise PermissionError("private Mac unavailable")
    slot = dict(slot)
    keys = {}
    for row in conn.execute(
        "SELECT id,name,generation,slot_ids,claim_bindings FROM gateway_keys "
        "WHERE account_id=? AND revoked_at IS NULL ORDER BY last_seen_at DESC",
        (owner,),
    ):
        if (
            json.loads(row["slot_ids"]) == [slot["id"]]
            and json.loads(row["claim_bindings"]).get(slot["id"]) == slot["claim_id"]
        ):
            keys[row["id"]] = {k: row[k] for k in ("id", "name", "generation")}
    binding = conn.execute(
        "SELECT * FROM api_model_bindings WHERE claim_id=? AND "
        "owner_account_id=? AND provider='gemini'",
        (slot["claim_id"], owner),
    ).fetchone()
    grants = set()
    if binding:
        grants = {
            row["key_id"]
            for row in conn.execute(
                "SELECT key_id,key_generation FROM api_model_grants WHERE binding_id=?",
                (binding["id"],),
            )
            if row["key_id"] in keys and keys[row["key_id"]]["generation"] == row["key_generation"]
        }
    return slot, keys, dict(binding) if binding else None, grants


def reservation(binding):
    return {
        "binding_id": binding["id"],
        **{k: binding[k] for k in ("claim_id", "provider", "model", "account_fp")},
    }


class GeminiSetupStore:
    def gemini_setup_state(self, owner):
        with self._lock:
            return state(self._conn, owner)

    def gemini_setup_start(self, owner, key_id, project, model):
        with self._write_txn() as conn:
            slot, keys, binding, _ = state(conn, owner)
            if (key_id != "new" and key_id not in keys) or (binding and retiring(binding)):
                raise SetupError("connection unavailable")
            if binding:
                if project or model:
                    raise SetupError("binding changed")
                selected = reservation(binding)
            else:
                selected = api_model_routes.reserve_on_slot(
                    conn, owner, slot["claim_id"], "gemini", project, model
                )
            conn.execute("UPDATE api_model_bindings SET web_access_until=? WHERE id=?",
                         (time.time() + 300, selected["binding_id"]))
            return (
                slot,
                selected,
                (0 if key_id == "new" else keys[key_id]["generation"]),
                binding["credential_fp"] if binding else None,
            )

    def gemini_setup_continue(self, owner, flow, receipt=None, *, now=None):
        with self._write_txn() as conn:
            slot, keys, binding, _ = state(conn, owner)
            new_computer = flow["key"] == "new"
            key = keys.get(flow["key"])
            if (
                (not new_computer and (not key or key["generation"] != flow["generation"]))
                or not binding
                or retiring(binding)
                or reservation(binding) != flow["route"]
            ):
                raise SetupError("endpoint or slot changed")
            if receipt is not None:
                if not isinstance(receipt, dict) or any(
                    receipt.get(k) != v for k, v in flow["route"].items()
                ):
                    raise SetupError("unexpected receipt")
                if new_computer:
                    api_model_routes.confirm_receipt(conn, owner, slot["claim_id"], receipt)
                    return self._mint_cli_pairing(
                        conn, slot["id"], owner, now=now, model_binding=binding["id"]
                    )
                api_model_routes.grant(conn, key["id"], slot["id"], receipt)
            return slot

    def gemini_setup_disconnect(self, owner, binding_id, *, completed=False):
        with self._write_txn() as conn:
            slot, _, binding, _ = state(conn, owner)
            if not binding or binding["id"] != binding_id:
                raise SetupError("binding changed")
            if completed:
                if not retiring(binding):
                    raise SetupError("binding changed")
                conn.execute("DELETE FROM api_model_bindings WHERE id=?", (binding_id,))
            else:
                conn.execute("DELETE FROM api_model_grants WHERE binding_id=?", (binding_id,))
                conn.execute(
                    "UPDATE api_model_bindings SET credential_fp=?,web_access_until=? WHERE id=?",
                    ("disconnecting:" + binding_id, time.time() + 300, binding_id),
                )
            return slot, reservation(binding)
