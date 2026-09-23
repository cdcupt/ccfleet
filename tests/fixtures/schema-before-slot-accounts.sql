-- The schema a live database had before slot accounts (main at 6822013),
-- dumped from sqlite_master after Store() opened it: every table as created,
-- every column added since. Opening a database made from this must work.
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    pinned_version TEXT NOT NULL DEFAULT '',
    rc_expected INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    -- When a device token was last handed over for this node. The time only;
    -- the credential itself lives in `logins` for the life of the request and
    -- nowhere else.
    device_token_at REAL NOT NULL DEFAULT 0
, capacity INTEGER NOT NULL DEFAULT 1, tier TEXT NOT NULL DEFAULT 'dedicated');
CREATE TABLE logins (
    node_id TEXT PRIMARY KEY,
    requested_at REAL NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'requested',
    url TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    -- 'login' signs the node in; 'token' mints a device credential the owner
    -- carries away. Same dance, different command, so one table drives both.
    kind TEXT NOT NULL DEFAULT 'login',
    -- Only ever set for kind='token', and only between the node reporting it
    -- and the console showing it. Readable until the attempt expires or
    -- somebody says they are done with it. See read_secret().
    secret TEXT NOT NULL DEFAULT ''
);
CREATE TABLE heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX ix_heartbeats_node_ts ON heartbeats(node_id, ts DESC);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX ix_alerts_node_rule ON alerts(node_id, rule);
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,                  -- ours, not Google's
    google_sub TEXT NOT NULL UNIQUE,      -- stable even when the email changes
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',    -- 'user' | 'admin'
    -- How many slots this person may hold. Zero until the operator grants
    -- some: a bug that grants nobody anything is a support message, and a bug
    -- that grants everybody a slot is a bill.
    slot_quota INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE slots (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    unix_user TEXT NOT NULL,              -- unique on that machine
    state TEXT NOT NULL,                  -- see ccfleetd/slots.py
    held_by TEXT REFERENCES accounts(id), -- NULL only while free
    claimed_at REAL,
    released_at REAL,
    device_token_at REAL NOT NULL DEFAULT 0,
    -- What the machine last said about this slot's Linux user: 1 it exists,
    -- 0 it does not, NULL never reported. A slot is only handed out on a 0,
    -- because free is a claim about the machine and only the machine can
    -- confirm it.
    present INTEGER,
    reported_at REAL,
    UNIQUE (node_id, unix_user)
);
CREATE INDEX ix_slots_state ON slots(state);
CREATE INDEX ix_slots_held_by ON slots(held_by);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_minor INTEGER NOT NULL,        -- hundredths: 3050 is 30.50
    currency TEXT NOT NULL,               -- a three-letter code, e.g. USD
    paid_through TEXT NOT NULL,           -- YYYY-MM-DD, the last day covered
    note TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    voided_at REAL
);
CREATE INDEX ix_payments_account ON payments(account_id);
CREATE TABLE sessions (
    id_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    -- Which site the session was made on: 'product' or 'admin'. Honoured
    -- only there — two sites, two sessions — so a product session cookie
    -- carried to the operator's hostname opens nothing.
    site TEXT NOT NULL DEFAULT 'product'
);
CREATE INDEX ix_sessions_account ON sessions(account_id);
CREATE INDEX ix_sessions_expires ON sessions(expires_at);
CREATE TABLE oauth_flows (
    state TEXT PRIMARY KEY,
    verifier TEXT NOT NULL,
    next_url TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    owner TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
