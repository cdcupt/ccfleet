"""Owner retrieval keeps bearer values out of the isolated access database."""

import hashlib
import secrets

import pytest

from ccfleetd.devin_trial import TrialError, VerifiedAccount
from ccfleetd.trial_access import TTL_S, AccessKeys

SECRET = "synthetic-deployment-secret-for-trial-tests"
ALICE = VerifiedAccount("alice", True)
BOB = VerifiedAccount("bob", True)


@pytest.fixture
def access(tmp_path):
    path = tmp_path / "access.sqlite3"
    path.touch(mode=0o600)
    now = [float(20000 * 86400 + 100)]
    keys = AccessKeys(path, secret=SECRET, clock=lambda: now[0])
    return keys, path, now


def snapshot(keys):
    with keys._db() as connection:
        return list(connection.iterdump())


def test_launch_key_survives_restart_without_database_or_rate_changes(access):
    keys, path, now = access
    token = keys.create(ALICE)
    before = snapshot(keys)
    assert keys.launch_key(ALICE) == token
    reopened = AccessKeys(path, secret=SECRET, clock=lambda: now[0])
    assert reopened.launch_key(ALICE) == reopened.launch_key(ALICE) == token
    assert snapshot(reopened) == before
    raw = path.read_bytes()
    assert token.encode() not in raw and SECRET.encode() not in raw
    assert hashlib.sha256(token.encode()).hexdigest().encode() in raw
    assert reopened.resolve(token)["account_id"] == ALICE.account_id
    assert reopened.resolve(token)["id"].startswith("v1_")


def test_each_verified_owner_retrieves_only_their_own_active_key(access):
    keys, _, _ = access
    alice = keys.create(ALICE)
    assert keys.launch_key(BOB) == ""
    bob = keys.create(BOB)
    assert alice != bob
    assert keys.launch_key(ALICE) == alice and keys.launch_key(BOB) == bob
    # Moving a row cannot turn its original secret into another owner's command.
    with keys._db() as connection:
        connection.execute("UPDATE trial_access SET account_id='mallory' WHERE account_id='alice'")
    assert keys.launch_key(ALICE) == ""
    assert keys.launch_key(VerifiedAccount("mallory", True)) == ""


@pytest.mark.parametrize("action", ["expires", "revokes", "replaces"])
def test_retrieval_obeys_existing_lifecycle_without_reviving_old_keys(access, action):
    keys, _, now = access
    old = keys.create(ALICE)
    if action == "expires":
        now[0] += TTL_S
        expected = ""
    elif action == "revokes":
        keys.revoke(ALICE.account_id)
        expected = ""
    else:
        expected = keys.create(ALICE)
        assert expected != old
    before = snapshot(keys)
    assert keys.launch_key(ALICE) == expected
    assert keys.resolve(old) is None
    assert snapshot(keys) == before


def test_changed_deployment_secret_does_not_revoke_or_replace_api_key(access):
    keys, path, now = access
    token = keys.create(ALICE)
    before = snapshot(keys)
    rotated = AccessKeys(path, secret="different-synthetic-deployment-secret", clock=lambda: now[0])
    assert rotated.launch_key(ALICE) == ""
    assert rotated.resolve(token)["account_id"] == ALICE.account_id
    assert rotated.active(keys.resolve(token))
    assert snapshot(rotated) == before
    assert keys.launch_key(ALICE) == token


def test_legacy_random_key_stays_valid_until_explicit_replacement(access):
    keys, path, now = access
    token = "cct_" + secrets.token_hex(32)
    identifier = secrets.token_hex(16)
    with keys._db() as connection:
        connection.execute("INSERT INTO trial_access VALUES(?,?,?,?,?,0)",
                           (identifier, ALICE.account_id, hashlib.sha256(token.encode()).hexdigest(),
                            now[0], now[0] + TTL_S))
    before = snapshot(keys)
    reopened = AccessKeys(path, secret=SECRET, clock=lambda: now[0])
    assert reopened.launch_key(ALICE) == ""
    assert reopened.resolve(token)["id"] == identifier
    assert snapshot(reopened) == before
    replacement = reopened.create(ALICE)
    assert reopened.launch_key(ALICE) == replacement
    assert reopened.resolve(token) is None
    # No schema expansion: the previous release's six-value insert remains valid.
    with reopened._db() as connection:
        assert len(connection.execute("PRAGMA table_info(trial_access)").fetchall()) == 6


@pytest.mark.parametrize("principal", [None, {"account_id": "alice"},
                                       VerifiedAccount("alice"), VerifiedAccount("alice", 1)])
def test_retrieval_requires_the_trusted_verified_account_type(access, principal):
    keys, _, _ = access
    keys.create(ALICE)
    before = snapshot(keys)
    with pytest.raises(TrialError, match="verified_account_required"):
        keys.launch_key(principal)
    assert snapshot(keys) == before


@pytest.mark.parametrize("secret", ["", None, b"secret"])
def test_no_default_derivation_secret_can_create_recoverable_keys(access, secret):
    _, path, _ = access
    with pytest.raises(TrialError, match="trial_key_derivation_secret_required"):
        AccessKeys(path, secret=secret)
