"""Upgrade 0.1.0's per-account Mac constraints, preserving node and claim identities."""

import re


def upgrade(store):
    connection=store._conn
    foreign_keys=connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if foreign_keys:
        if connection.in_transaction:
            raise RuntimeError("Mac schema migration requires a transaction boundary")
        connection.execute("PRAGMA foreign_keys=OFF")
    try:
        _upgrade(store)
    finally:
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")


def _upgrade(store):
    # The FK declarations and all dependent tables keep their original names and contents.
    with store._write_txn() as conn:
        for table, owner in (("private_owner_nodes", "account_id"), ("home_macs", "owner_id")):
            sql = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            ddl, count = re.subn(
                r"(?i)(\b" + owner + r"\s+TEXT\s+NOT\s+NULL)\s+UNIQUE\b", r"\1", sql
            )
            if not count:
                continue
            objects = conn.execute(
                "SELECT sql FROM sqlite_schema WHERE tbl_name=? AND "
                "type IN ('trigger','index') AND sql IS NOT NULL",
                (table,),
            ).fetchall()
            temporary = table + "_multi_new"
            definition = ddl[ddl.index("(") :]
            conn.execute(f"CREATE TABLE {temporary} " + definition)
            cols = ",".join(
                '"' + row[1].replace('"', '""') + '"'
                for row in conn.execute(f"PRAGMA table_info({table})")
            )
            conn.execute(f"INSERT INTO {temporary} ({cols}) SELECT {cols} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for obj in objects:
                conn.execute(obj[0])
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_personal_mac_owner ON home_macs(owner_id,removed)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_private_owner_account ON private_owner_nodes(account_id)"
        )
