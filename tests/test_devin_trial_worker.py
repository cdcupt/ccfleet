"""Actual trial framing/fresh-HOME wrapper with fake native only; no models."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import os
import struct
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from ccfleet_agent import devin_trial_worker as worker
from ccfleetd import trial_runtime
from ccfleetd.gateway_transport import GatewayTransportError
from ccfleetd.trial_worker_transport import TrialAttempt
from tests.test_devin_trial_runtime import MODEL, body


@pytest.fixture
def scene(tmp_path, monkeypatch):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    paths = [
        home / ".local/share/devin/credentials.toml",
        home / ".config/devin/config.json",
        home / ".config/devin/mcp_config.json",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(b"fixture secret" if index == 0 else b"{}")
        path.chmod(0o600)
    # An existing unrelated account/session history MUST never enter fresh HOME.
    private = home / ".local/share/devin/cli/sessions.db"
    private.parent.mkdir(mode=0o700)
    private.write_bytes(b"OTHER_USER_PRIVATE_HISTORY")
    policy = tmp_path / "worker.json"
    policy.write_bytes(b"fixture policy")
    monkeypatch.setattr(worker, "POLICY", policy)
    real_read = worker.native._read

    def read(path, *, uid, **kwargs):
        return real_read(path, uid=os.geteuid() if path == policy else uid, **kwargs)

    monkeypatch.setattr(worker.native, "_read", read)
    binding = worker.native.OwnershipBinding(
        "ccfleet-trial", "trial-worker", 1, "virtual-trial", "b" * 32, 1, 1
    )
    value = {
        "home": str(home),
        "state_dir": str(state),
        "model_uid": "native-opus-verified",
        "binding": asdict(binding),
        "files": {
            "native_config": {"path": str(paths[1]), "sha256": hashlib.sha256(b"{}").hexdigest()},
            "native_mcp_config": {
                "path": str(paths[2]),
                "sha256": hashlib.sha256(b"{}").hexdigest(),
            },
        },
        "request_models": [MODEL],
        "thought_level": "max",
    }

    class Source(worker.native.NativeConfig):
        def validate(self):
            if self.changed:
                raise worker.native.NativeError("policy_changed")
            return home, state

    source = Source(tmp_path / "profile", "a" * 64, json.dumps(value))
    object.__setattr__(source, "changed", False)
    settings = {
        "credential_sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest(),
        "_refresh": lambda: True,
    }
    sha = hashlib.sha256(policy.read_bytes()).hexdigest()
    yield source, settings, sha, home, state, paths


def frames(raw):
    first = {"version": 2, "operation": "status"}
    second = {
        "version": 2,
        "operation": "request",
        "method": "POST",
        "path": "/v1/messages",
        "headers": {},
        "body_size": len(raw),
    }

    def frame(v):
        raw = json.dumps(v).encode()
        return struct.pack("!I", len(raw)) + raw

    return io.BytesIO(frame(first) + frame(second) + raw)


def result(config, amount=7, cleanup=True):
    return {
        "mode": "native_agent_compatibility",
        "contract": "native-agent-v1",
        "native_cleanup_confirmed": cleanup,
        "prompt_requests": 1,
        "raw_Messages_semantic_parity": False,
        "model": config.data["model_uid"],
        "content": [{"type": "text", "text": "PRIVATE ANSWER"}],
        "stop_reason": "end_turn",
        "accounting": {
            "served_model_verified": True,
            "model_mismatch_detected": False,
            "unique_finalized_provider_ids": 1,
            "usage": {
                "input_tokens": amount,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 4,
            },
        },
    }


def unpack(raw):
    stream, messages = io.BytesIO(raw), []
    while stream.tell() < len(raw):
        size = struct.unpack("!I", stream.read(4))[0]
        meta = json.loads(stream.read(size))
        parts = bytearray()
        while True:
            size = struct.unpack("!I", stream.read(4))[0]
            if size == 0:
                break
            parts.extend(stream.read(size))
        messages.append((meta, json.loads(parts)))
    return messages


def test_worker_default_does_no_policy_or_native_io(monkeypatch, capsys):
    monkeypatch.setattr(worker, "policy", lambda: pytest.fail("inert default read policy"))
    assert worker.main([]) == 0
    assert json.loads(capsys.readouterr().out)["execute"] is False


@pytest.mark.parametrize("code", ["timeout", "native_step_incomplete", "PRIVATE FAILURE"])
def test_failed_result_keeps_only_static_private_diagnostic_after_history_cleanup(scene, code):
    source, settings, sha, _, state, _ = scene

    def run(config, *args, **kwargs):
        record = result(config)
        record.update(error_code=code, content=[], stop_reason=None)
        record["accounting"]["usage"]["cache_creation_tokens"] = None
        record["private_extra"] = "PRIVATE PROMPT HISTORY ID CREDENTIAL"
        return record

    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run) == 0
    receipt = unpack(out.getvalue())[-1][1]
    assert receipt["cleanup_confirmed"] and receipt["metering"] == "unconfirmed"
    assert not list(state.glob("trial-*"))
    path = state / worker.DIAGNOSTIC
    raw = path.read_bytes()
    diagnostic = json.loads(raw)
    assert diagnostic["native_error"] == (
        code if code != "PRIVATE FAILURE" else "other_native_failure"
    )
    assert diagnostic["phase"] == "result_received"
    assert diagnostic["native_cleanup_confirmed"] and not diagnostic["message_valid"]
    assert diagnostic["usage_fields"]["cache_creation_tokens"] == "missing"
    assert b"PRIVATE" not in raw and len(raw) < 2048
    assert path.stat().st_mode & 0o777 == 0o600


def test_diagnostic_is_bounded_replacement_and_never_follows_links(scene):
    *_, state, paths = scene
    assert worker._diagnostic(state, phase="started")
    assert worker._diagnostic(state, phase="worker_failed")
    assert set(state.iterdir()) == {state / worker.DIAGNOSTIC}
    path = state / worker.DIAGNOSTIC
    path.unlink()
    path.symlink_to(paths[0])
    assert not worker._diagnostic(state, phase="started")
    assert paths[0].read_bytes() == b"fixture secret"


def test_diagnostic_failure_does_not_hide_valid_accounting_or_repeat_model(scene, monkeypatch):
    source, settings, sha, _, _, _ = scene
    calls = []
    monkeypatch.setattr(worker, "_diagnostic", lambda *a, **kw: False)

    def run(config, *a, **kw):
        calls.append(1)
        return result(config)

    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run) == 0
    receipt = unpack(out.getvalue())[-1][1]
    assert receipt["metering"] == "complete" and receipt["cleanup_confirmed"]
    assert trial_runtime.TrialRuntime._receipt(receipt)[1].reported_tokens == 16
    assert calls == [1]


def test_readiness_failure_preserves_last_model_diagnostic(scene):
    source, settings, sha, _, state, _ = scene
    assert worker._diagnostic(state, phase="result_received", result={"error_code": "timeout"})
    before = (state / worker.DIAGNOSTIC).read_bytes()
    (state / "trial-quarantined").mkdir(mode=0o700)
    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=lambda: (source, settings, sha),
                        runner=lambda *a, **kw: pytest.fail("must not generate")) == 2
    assert (state / worker.DIAGNOSTIC).read_bytes() == before


def test_actual_framed_turn_is_credential_only_and_deletes_all_artifacts_before_reply(scene):
    source, settings, sha, home, state, paths = scene
    calls, homes = [], []

    def run(config, binding, request_id, body, *, authorize, cancel_event):
        assert binding.owner_id == "ccfleet-trial" and binding.slot_id == "virtual-trial"
        assert authorize(binding) is True
        fresh_home = Path(config.data["home"])
        homes.append(fresh_home)
        assert fresh_home != home
        assert (
            fresh_home / ".local/share/devin/credentials.toml"
        ).read_bytes() == b"fixture secret"
        assert not (fresh_home / ".local/share/devin/cli/sessions.db").exists()
        # Model runtime may create every kind of history/output; remove the
        # ENTIRE per-turn tree, not merely the synthetic HOME.
        step = Path(config.data["state_dir"]) / "step-result"
        step.mkdir()
        (step / "result.json").write_bytes(b"PRIVATE ANSWER")
        (step / "attempt.json").write_bytes(b"PRIVATE PROMPT")
        calls.append(request_id)
        return result(config)

    for _ in range(2):
        out = io.BytesIO()
        assert (
            worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run)
            == 0
        )
        [ready, response] = unpack(out.getvalue())
        assert ready[1]["policy_sha256"] == sha
        assert response[1]["cleanup_confirmed"] is True
        assert response[1]["message"]["model"] == MODEL
        assert trial_runtime.TrialRuntime._receipt(response[1])[1].reported_tokens == 16
        assert not homes[-1].parent.exists()
    assert len(set(calls)) == len(set(homes)) == 2
    assert (
        home / ".local/share/devin/cli/sessions.db"
    ).read_bytes() == b"OTHER_USER_PRIVATE_HISTORY"
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}
    assert paths[0].read_bytes() == b"fixture secret"


def test_unconfirmed_native_cleanup_keeps_private_tree_and_never_successfully_releases(scene):
    source, settings, sha, _, state, _ = scene
    out = io.BytesIO()
    assert (
        worker.serve(
            frames(body()),
            out,
            loader=lambda: (source, settings, sha),
            runner=lambda config, *a, **kw: result(config, cleanup=False),
        )
        == 0
    )
    response = unpack(out.getvalue())[-1][1]
    assert response["cleanup_confirmed"] is False and response["error"] == "native_request_failed"
    assert len(list(state.glob("trial-*"))) == 1


@pytest.mark.parametrize("change", ["source", "credential", "copy", "policy"])
def test_authorization_rechecks_source_revocation_original_and_fresh_credentials(scene, change):
    source, settings, sha, _, _, paths = scene
    config, root = worker.fresh_config(source, settings, sha)
    try:
        assert config.validate()
        if change == "source":
            object.__setattr__(source, "changed", True)
        elif change == "credential":
            paths[0].write_bytes(b"rotated credential")
        elif change == "copy":
            (Path(config.data["home"]) / ".local/share/devin/credentials.toml").write_bytes(
                b"changed"
            )
        else:
            worker.POLICY.write_bytes(b"changed")
        with pytest.raises(worker.native.NativeError):
            config.validate()
    finally:
        import shutil

        shutil.rmtree(root)


def test_worker_wire_error_redacts_provider_text_and_never_invokes_model(scene):
    source, settings, sha, _, _, _ = scene

    def run(*a, **kw):
        raise ValueError("PRIVATE PROVIDER SECRET")

    out = io.BytesIO()
    assert (
        worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run) == 2
    )
    assert b"PRIVATE PROVIDER" not in out.getvalue() and b"fixture secret" not in out.getvalue()


def test_transport_fixed_command_pinned_policy_and_no_owner_context():
    attempt = TrialAttempt({"policy_sha256": "a" * 64}, object())
    assert attempt._remote_command() == "ccfleet-devin-trial-v1"
    assert attempt._status_fields() == {"operation": "status"}
    assert attempt._ready_body() == {
        "ready": True,
        "protocol": 2,
        "trial": True,
        "model": MODEL,
        "policy_sha256": "a" * 64,
    }
    assert "binding" not in repr(attempt._request_fields({}, b"{}", "/v1/messages"))


@pytest.mark.parametrize("still_authorized", [False, True])
def test_prepared_transport_sends_only_after_fresh_authorization_on_same_connection(
    tmp_path, monkeypatch, still_authorized
):
    # A local framing peer exercises the actual pipe reader/writer lifecycle;
    # only the fixed SSH command is replaced, with no network or model calls.
    marker = tmp_path / "request-received"
    script = r'''
import json,pathlib,struct,sys
source,target=sys.stdin.buffer,sys.stdout.buffer
def frame():
    size=struct.unpack("!I",source.read(4))[0]
    return json.loads(source.read(size))
def send(value):
    raw=json.dumps(value).encode()
    target.write(struct.pack("!I",len(raw))+raw)
def response(value):
    send({"version":2,"status":200,"headers":{},"retryable":False})
    send(value)
    target.write(struct.pack("!I",0));target.flush()
assert frame()=={"version":2,"operation":"status"}
response(json.loads(sys.argv[1]))
request=frame()
assert request["operation"]=="request"
assert source.read(request["body_size"])==b"{}"
pathlib.Path(sys.argv[2]).write_text("received")
response({"reply":"clean"})
'''
    attempt = TrialAttempt({"policy_sha256": "a" * 64}, object())
    commands = []

    def command():
        commands.append(1)
        return [sys.executable, "-c", script, json.dumps(attempt._ready_body()), str(marker)]

    monkeypatch.setattr(attempt, "_command", command)
    try:
        attempt.prepare(lambda: True)
        process = attempt.process
        assert attempt.ready and not attempt.model_started and not marker.exists()
        # The execution phase must replace both the readiness timer and its
        # callback; a revoked caller must never write the request frame.
        process.stdout.deadline = time.monotonic() - 1
        process.stdout.authorized = lambda: False
        if still_authorized:
            assert attempt.perform(b"{}", lambda: True) == {"reply": "clean"}
            assert marker.read_text() == "received"
        else:
            with pytest.raises(GatewayTransportError, match="authorization changed"):
                attempt.perform(b"{}", lambda: False)
            assert not attempt.model_started and not marker.exists()
        assert attempt.process is process and len(commands) == 1
    finally:
        attempt.close()


def test_actual_shared_worker_lock_excludes_second_worker_before_fresh_copy(scene):
    source, settings, sha, _, state, _ = scene
    lock = os.open(state / ".trial.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = io.BytesIO()
        incoming = frames(body())
        status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
        assert (
            worker.serve(
                incoming,
                out,
                loader=lambda: (source, settings, sha),
                runner=lambda *a, **kw: pytest.fail("busy worker must not launch"),
            )
            == 2
        )
        [response] = unpack(out.getvalue())
        assert response[0]["status"] == 429
        assert response[1] == {"error": "trial_worker_busy"}
        assert incoming.tell() == status_end, "busy readiness consumed a request frame"
        assert not list(state.glob("trial-*"))
    finally:
        os.close(lock)


def test_ready_connection_keeps_shared_lock_between_status_and_request(scene):
    source, settings, sha, _, state, _ = scene
    raw = frames(body()).getvalue()
    status_end = 4 + struct.unpack("!I", raw[:4])[0]
    out, events = io.BytesIO(), []

    def contender():
        descriptor = os.open(state / ".trial.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    class InterleavedInput(io.BytesIO):
        def read(self, size):
            if self.tell() == status_end and not events:
                [ready] = unpack(out.getvalue())
                assert ready[0]["status"] == 200 and ready[1]["ready"] is True
                # Model-free refresh uses this same flock. It must observe a
                # busy worker after readiness, rather than steal admission.
                contender()
                events.append("refresh-skipped-busy")
            return super().read(size)

    def run(config, *args, **kwargs):
        assert events == ["refresh-skipped-busy"]
        contender()
        return result(config)

    assert worker.serve(
        InterleavedInput(raw), out, loader=lambda: (source, settings, sha), runner=run
    ) == 0
    assert unpack(out.getvalue())[-1][1]["cleanup_confirmed"] is True
    descriptor = os.open(state / ".trial.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_status_only_connection_releases_worker_without_fresh_home_or_model(scene):
    source, settings, sha, _, state, _ = scene
    raw = frames(body()).getvalue()
    status_end = 4 + struct.unpack("!I", raw[:4])[0]
    out = io.BytesIO()
    assert worker.serve(
        io.BytesIO(raw[:status_end]), out, loader=lambda: (source, settings, sha),
        runner=lambda *args, **kwargs: pytest.fail("status-only connection ran a model"),
    ) == 0
    [ready] = unpack(out.getvalue())
    assert ready[1]["ready"] is True and not list(state.glob("trial-*"))
    descriptor = os.open(state / ".trial.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_authority_is_reloaded_under_lock_before_readiness(scene):
    source, settings, sha, _, state, _ = scene
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 2:
            raise worker.native.NativeError("PRIVATE revoked source grant")
        return source, settings, sha

    incoming, out = frames(body()), io.BytesIO()
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    assert worker.serve(
        incoming, out, loader=load,
        runner=lambda *args, **kwargs: pytest.fail("revoked readiness ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503 and b"PRIVATE" not in out.getvalue()
    assert incoming.tell() == status_end and not list(state.glob("trial-*"))


def test_bad_grant_after_readiness_still_prevents_native_execution(scene):
    source, settings, sha, _, state, _ = scene
    raw = frames(body()).getvalue()
    status_end = 4 + struct.unpack("!I", raw[:4])[0]
    changed = []

    class RevokeAfterReady(io.BytesIO):
        def read(self, size):
            if self.tell() == status_end and not changed:
                settings["_refresh"] = lambda: False
                changed.append(True)
            return super().read(size)

    out = io.BytesIO()
    assert worker.serve(
        RevokeAfterReady(raw), out, loader=lambda: (source, settings, sha),
        runner=lambda *args, **kwargs: pytest.fail("BAD grant ran a model"),
    ) == 2
    assert [entry[0]["status"] for entry in unpack(out.getvalue())] == [200, 503]
    assert changed and not list(state.glob("trial-*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("served_model_verified", False),
        ("model_mismatch_detected", True),
        ("unique_finalized_provider_ids", None),
        ("unique_finalized_provider_ids", True),
    ],
)
def test_partial_native_generation_records_never_become_complete_usage(scene, field, value):
    source, settings, sha, _, state, _ = scene

    def run(config, *a, **kw):
        record = result(config)
        record["accounting"][field] = value
        return record

    out = io.BytesIO()
    assert (
        worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run) == 0
    )
    record = unpack(out.getvalue())[-1][1]
    assert record["usage"] is None and record["cleanup_confirmed"] is True
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}


def test_successful_native_cleanup_but_artifact_delete_failure_never_releases(scene, monkeypatch):
    source, settings, sha, _, _, _ = scene

    def blocked(*a, **kw):
        raise OSError("PRIVATE FILE CONTENT")

    monkeypatch.setattr(worker.shutil, "rmtree", blocked)
    out = io.BytesIO()
    with pytest.raises(OSError):
        worker.serve(
            frames(body()),
            out,
            loader=lambda: (source, settings, sha),
            runner=lambda config, *a, **kw: result(config),
        )
    records = unpack(out.getvalue())
    assert records[-1][0]["status"] == 503
    assert b"PRIVATE FILE" not in out.getvalue()


@pytest.fixture
def policy_scene(monkeypatch):
    from types import SimpleNamespace

    binding = worker.native.OwnershipBinding(
        "ccfleet-trial", "trial-worker", 1, "virtual-trial", "a" * 32, 1, 1
    )
    config = SimpleNamespace(
        data={
            "binding": asdict(binding),
            "request_models": [MODEL],
            "thought_level": "max",
            "timeout_s": 40,
            "enabled": True,
            "home": "/dedicated-trial",
        }
    )
    claim = {
        "owner_id": "owner",
        "slot_id": "source-slot",
        "claim_id": "c" * 32,
        "claim_generation": 1,
    }
    runtime = {
        "bridge_contract": "native-agent-v1",
        "native_version": "3000.1.1",
        "adapter_revision": "a" * 64,
        "model_map_digest": "b" * 64,
    }
    policy = {
        "version": 1,
        "enabled": True,
        "config_path": str(worker.POLICY.with_name("template.json")),
        "config_sha256": "f" * 64,
        "grant_path": str(worker.POLICY.with_name("grant.json")),
        "expected_account_fp": "d" * 64,
        "source_claim": claim,
        "source_profile_sha256": "e" * 64,
        "source_runtime": runtime,
    }
    credential = [b"fixture credential"]
    grant = {
        "version": 1,
        "enabled": True,
        "config_sha256": "f" * 64,
        "credential_sha256": hashlib.sha256(credential[0]).hexdigest(),
        "observed_at": 1000.0,
        "account_fp": "d" * 64,
        "source_claim": copy.deepcopy(claim),
        "source_profile_sha256": "e" * 64,
        "source_runtime": copy.deepcopy(runtime),
    }
    monkeypatch.setattr(worker.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(worker.time, "time", lambda: 1100.0)
    monkeypatch.setattr(worker.native, "_root_ancestry", lambda path: None)
    monkeypatch.setattr(worker.native.NativeConfig, "load", lambda *a, **kw: config)

    def read(path, **kw):
        if path == worker.POLICY:
            return json.dumps(policy).encode()
        if path == worker.POLICY.with_name("grant.json"):
            return json.dumps(grant).encode()
        if str(path) == "/dedicated-trial/.local/share/devin/credentials.toml":
            return credential[0]
        pytest.fail("worker must never read original owner HOME")

    monkeypatch.setattr(worker.native, "_read", read)
    return policy, grant, credential


def test_stable_policy_survives_verified_credential_renewal_but_old_turn_revokes(policy_scene):
    policy, grant, credential = policy_scene
    _, first, sha = worker.policy()
    assert first["_refresh"]() is True
    grant["observed_at"] = 1050.0  # Retain original GOOD time, not renewal wall time.
    assert first["_refresh"]() is True
    credential[0] = b"new verified same-account credential"
    grant["credential_sha256"] = hashlib.sha256(credential[0]).hexdigest()
    assert first["_refresh"]() is False
    _, second, new_sha = worker.policy()
    assert second["_refresh"]() is True and new_sha == sha
    assert "credential_sha256" not in policy  # No controller repin for normal renewals.


@pytest.mark.parametrize("age,accepted", [(120, True), (121, False)])
def test_new_turn_grant_has_margin_for_entire_180_second_lease(policy_scene, age, accepted):
    _, grant, _ = policy_scene
    grant["observed_at"] = 1100.0 - age
    if accepted:
        worker.policy()
    else:
        with pytest.raises(worker.native.NativeError, match="trial_grant_needs_refresh"):
            worker.policy()


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "near_expiry",
        "future",
        "different_account",
        "different_claim",
        "different_runtime",
        "different_profile",
        "different_config",
        "wrong_credential",
        "disabled",
        "unknown_field",
    ],
)
def test_worker_model_free_readiness_refuses_stale_bad_or_mismatched_root_grant(
    policy_scene, change
):
    _, grant, _ = policy_scene
    if change == "stale":
        grant["observed_at"] = 700.0
    elif change == "near_expiry":
        grant["observed_at"] = 850.0
    elif change == "future":
        grant["observed_at"] = 1101.0
    elif change == "different_account":
        grant["account_fp"] = "1" * 64
    elif change == "different_claim":
        grant["source_claim"]["claim_generation"] = 2
    elif change == "different_runtime":
        grant["source_runtime"]["adapter_revision"] = "1" * 64
    elif change == "different_profile":
        grant["source_profile_sha256"] = "1" * 64
    elif change == "different_config":
        grant["config_sha256"] = "1" * 64
    elif change == "wrong_credential":
        grant["credential_sha256"] = "1" * 64
    elif change == "disabled":
        grant["enabled"] = False
    else:
        grant["secret"] = "PRIVATE"
    with pytest.raises(worker.native.NativeError) as error:
        worker.policy()
    assert "PRIVATE" not in str(error.value)


def test_actual_worker_cache_read_only_missing_has_explicit_partial_state_after_cleanup(scene):
    source, settings, sha, _, state, _ = scene

    def run(config, *a, **kw):
        value = result(config)
        value["accounting"]["usage"]["cache_read_tokens"] = None
        return value

    out = io.BytesIO()
    assert (
        worker.serve(frames(body()), out, loader=lambda: (source, settings, sha), runner=run) == 0
    )
    value = unpack(out.getvalue())[-1][1]
    assert value["metering"] == "cache_read_unmeasured" and value["cleanup_confirmed"]
    assert "cache_read_input_tokens" not in value["message"]["usage"]
    assert value["usage"]["cache_read_tokens"] is None
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}
    clean, usage = trial_runtime.TrialRuntime._receipt(value)
    assert clean and type(usage).__name__ == "VerifiedCacheReadUnmeasured"


def test_quarantined_old_turn_prevents_new_native_even_with_unlocked_worker(scene):
    source, settings, sha, _, state, _ = scene
    quarantined = state / "trial-uncertain"
    quarantined.mkdir(mode=0o700)
    (quarantined / "private-result").write_bytes(b"PRIVATE UNKNOWN HISTORY")
    out = io.BytesIO()
    incoming = frames(body())
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    assert (
        worker.serve(
            incoming,
            out,
            loader=lambda: (source, settings, sha),
            runner=lambda *a, **kw: pytest.fail("quarantined trial UID cannot execute"),
        )
        == 2
    )
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503
    assert incoming.tell() == status_end, "quarantined worker consumed a request frame"
    assert (quarantined / "private-result").read_bytes() == b"PRIVATE UNKNOWN HISTORY"
    assert b"PRIVATE" not in out.getvalue()


def test_worker_reloads_same_policy_new_good_credential_under_lock_before_turn(scene):
    source, settings, sha, _, _, paths = scene
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 2:
            paths[0].write_bytes(b"new same-account verified credential")
            settings["credential_sha256"] = hashlib.sha256(paths[0].read_bytes()).hexdigest()
        return source, settings, sha

    def run(config, *a, **kw):
        path = Path(config.data["home"]) / ".local/share/devin/credentials.toml"
        assert path.read_bytes() == b"new same-account verified credential"
        return result(config)

    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=load, runner=run) == 0
    assert len(calls) == 3 and unpack(out.getvalue())[-1][1]["cleanup_confirmed"]


@pytest.fixture
def readiness_clock(monkeypatch):
    class Clock:
        now = 100.0
        stopped = False
        on_wait = None

        def __init__(self):
            self.waits = []

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            self.waits.append(seconds)
            self.now += seconds
            if self.on_wait is not None:
                self.on_wait()
            return self.stopped

    clock = Clock()
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock.now)
    return clock


def assert_refresh_can_take_lock(state):
    """The root refresher uses this same lock, and must run during the grace."""
    assert not list(state.glob("trial-*"))
    if not (state / ".trial.lock").exists():
        return
    descriptor = os.open(state / ".trial.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("phase", ["initial", "under_lock"])
def test_status_grace_reloads_stale_grant_without_model_or_request_frame(
        scene, readiness_clock, phase):
    source, settings, sha, _, state, _ = scene
    clock, calls = readiness_clock, []
    incoming = frames(body())
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    # EOF after status is a model-free readiness check, even after waiting.
    incoming = io.BytesIO(incoming.getvalue()[:status_end])
    clock.on_wait = lambda: assert_refresh_can_take_lock(state)

    def load():
        calls.append(clock.now)
        if phase == "under_lock" and len(calls) == 1:
            return source, settings, sha
        if clock.now < 100.3:
            assert incoming.tell() == (0 if phase == "initial" else status_end)
            raise worker.native.NativeError("trial_grant_needs_refresh")
        return source, settings, sha

    out = io.BytesIO()
    assert worker.serve(
        incoming, out, loader=load, cancelled=clock,
        runner=lambda *a, **kw: pytest.fail("status grace ran a model"),
    ) == 0
    [ready] = unpack(out.getvalue())
    assert ready[0]["status"] == 200 and ready[1]["ready"] is True
    assert ready[1]["policy_sha256"] == sha
    assert clock.waits and clock.now < 100 + worker.GRANT_REFRESH_GRACE_S
    assert incoming.tell() == status_end and not list(state.glob("trial-*"))
    assert_refresh_can_take_lock(state)


@pytest.mark.parametrize("phase", ["initial", "under_lock"])
def test_persistent_stale_grant_exhausts_original_grace_without_consuming_request(
        scene, readiness_clock, phase):
    source, settings, sha, _, state, _ = scene
    clock, calls = readiness_clock, []
    incoming, out = frames(body()), io.BytesIO()
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    clock.on_wait = lambda: assert_refresh_can_take_lock(state)

    def load():
        calls.append(clock.now)
        if phase == "under_lock" and len(calls) == 1:
            clock.now += worker.GRANT_REFRESH_GRACE_S - 0.2
            return source, settings, sha
        raise worker.native.NativeError("trial_grant_needs_refresh")

    assert worker.serve(
        incoming, out, loader=load, cancelled=clock,
        runner=lambda *a, **kw: pytest.fail("stale grant ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503
    assert clock.now == pytest.approx(100 + worker.GRANT_REFRESH_GRACE_S)
    assert sum(clock.waits) == pytest.approx(
        worker.GRANT_REFRESH_GRACE_S if phase == "initial" else 0.2)
    assert incoming.tell() == (0 if phase == "initial" else status_end)
    assert_refresh_can_take_lock(state)


@pytest.mark.parametrize("stale_first", [False, True])
@pytest.mark.parametrize("failure", [
    worker.native.NativeError("trial_grant_stale_or_changed"),
    worker.native.NativeError("trial_credential_needs_refresh"),
    worker.native.NativeError("trial_policy_changed"),
    worker.native.NativeError("trial_grant_needs_refresh PRIVATE"),
    FileNotFoundError("PRIVATE missing grant"),
])
def test_status_grace_never_retries_bad_changed_or_missing_grants(
        scene, readiness_clock, stale_first, failure):
    *_, state, _ = scene
    clock, calls = readiness_clock, []

    def load():
        calls.append(1)
        if stale_first and len(calls) == 1:
            raise worker.native.NativeError("trial_grant_needs_refresh")
        raise failure

    incoming, out = frames(body()), io.BytesIO()
    assert worker.serve(
        incoming, out, loader=load, cancelled=clock,
        runner=lambda *a, **kw: pytest.fail("BAD grant ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503 and b"PRIVATE" not in out.getvalue()
    assert len(calls) == (2 if stale_first else 1)
    assert len(clock.waits) == (1 if stale_first else 0)
    assert incoming.tell() == 0 and not list(state.glob("trial-*"))


@pytest.mark.parametrize("change,code", [
    ("credential", "trial_credential_needs_refresh"),
    ("config", "policy_guard_failed"),
])
def test_stale_margin_never_masks_other_full_policy_failures(
        policy_scene, readiness_clock, monkeypatch, change, code):
    _, grant, _ = policy_scene
    grant["observed_at"] = 950.0  # 150 seconds old, inside the unchanged 300-second bound.
    if change == "credential":
        grant["credential_sha256"] = "0" * 64
    else:
        def rejected(*args, **kwargs):
            raise worker.native.NativeError("policy_guard_failed")

        monkeypatch.setattr(worker.native.NativeConfig, "load", rejected)
    with pytest.raises(worker.native.NativeError, match=code):
        worker._status_policy(worker.policy, readiness_clock,
                              readiness_clock.now + worker.GRANT_REFRESH_GRACE_S)
    assert not readiness_clock.waits


@pytest.mark.parametrize("phase", ["before", "waiting", "loaded"])
def test_status_grace_cancellation_stops_before_readiness_or_model(scene, readiness_clock, phase):
    source, settings, sha, _, state, _ = scene
    clock, calls = readiness_clock, []
    if phase == "before":
        clock.set()
    elif phase == "waiting":
        clock.on_wait = clock.set

    def load():
        calls.append(1)
        if phase == "waiting":
            raise worker.native.NativeError("trial_grant_needs_refresh")
        clock.set()
        return source, settings, sha

    incoming, out = frames(body()), io.BytesIO()
    assert worker.serve(
        incoming, out, loader=load, cancelled=clock,
        runner=lambda *a, **kw: pytest.fail("canceled readiness ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503
    assert len(calls) == (0 if phase == "before" else 1)
    assert len(clock.waits) == (1 if phase == "waiting" else 0)
    assert incoming.tell() == 0 and not list(state.glob("trial-*"))


def test_fresh_status_does_not_wait_or_add_loader_attempts(scene, readiness_clock):
    source, settings, sha, _, _, _ = scene
    calls = []

    def load():
        calls.append(1)
        return source, settings, sha

    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=load, cancelled=readiness_clock,
                        runner=lambda config, *a, **kw: result(config)) == 0
    assert len(calls) == 3 and not readiness_clock.waits
    assert [record[0]["status"] for record in unpack(out.getvalue())] == [200, 200]


def test_stale_grace_deadline_includes_final_under_lock_validation(scene, readiness_clock):
    source, settings, sha, _, state, _ = scene
    clock, calls = readiness_clock, []

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise worker.native.NativeError("trial_grant_needs_refresh")
        if len(calls) == 2:
            clock.now = 100 + worker.GRANT_REFRESH_GRACE_S - 0.1
        else:
            clock.now += 0.2
        return source, settings, sha

    incoming, out = frames(body()), io.BytesIO()
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    assert worker.serve(
        incoming, out, loader=load, cancelled=clock,
        runner=lambda *a, **kw: pytest.fail("expired readiness grace ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503 and len(calls) == 3
    assert clock.now == pytest.approx(100 + worker.GRANT_REFRESH_GRACE_S + 0.1)
    assert incoming.tell() == status_end
    assert_refresh_can_take_lock(state)


def test_fresh_status_has_no_new_grace_deadline(scene, readiness_clock):
    source, settings, sha, _, _, _ = scene
    clock, calls = readiness_clock, []

    def load():
        calls.append(1)
        if len(calls) == 2:
            clock.now += worker.GRANT_REFRESH_GRACE_S + 0.1
        return source, settings, sha

    out = io.BytesIO()
    assert worker.serve(frames(body()), out, loader=load, cancelled=clock,
                        runner=lambda config, *a, **kw: result(config)) == 0
    assert len(calls) == 3 and not clock.waits
    assert [record[0]["status"] for record in unpack(out.getvalue())] == [200, 200]


def test_status_grace_keeps_policy_pin_after_under_lock_staleness(scene, readiness_clock):
    source, settings, sha, _, state, _ = scene
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 2:
            raise worker.native.NativeError("trial_grant_needs_refresh")
        return source, settings, sha if len(calls) == 1 else "0" * 64

    incoming, out = frames(body()), io.BytesIO()
    status_end = 4 + struct.unpack("!I", incoming.getvalue()[:4])[0]
    assert worker.serve(
        incoming, out, loader=load, cancelled=readiness_clock,
        runner=lambda *a, **kw: pytest.fail("changed policy ran a model"),
    ) == 2
    [response] = unpack(out.getvalue())
    assert response[0]["status"] == 503 and len(calls) == 3
    assert incoming.tell() == status_end
    assert_refresh_can_take_lock(state)


def test_stale_grant_after_request_does_not_receive_readiness_grace(scene, readiness_clock):
    source, settings, sha, _, state, _ = scene
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 3:
            raise worker.native.NativeError("trial_grant_needs_refresh")
        return source, settings, sha

    out = io.BytesIO()
    assert worker.serve(
        frames(body()), out, loader=load, cancelled=readiness_clock,
        runner=lambda *a, **kw: pytest.fail("stale admitted grant ran a model"),
    ) == 2
    assert [record[0]["status"] for record in unpack(out.getvalue())] == [200, 503]
    assert len(calls) == 3 and not readiness_clock.waits
    assert not list(state.glob("trial-*"))
