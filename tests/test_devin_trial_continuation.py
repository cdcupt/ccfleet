"""Synthetic full result path: no SSH/native executable/provider/model calls.

Uses the existing fake ACP peer with genuine run_step, SQLite accounting,
framed trial worker, result validation, and trial ledger. Authority checks and
process cleanup are fixture-controlled; it is not a production canary.
"""

import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from ccfleet_agent import devin_native as native
from ccfleet_agent import devin_trial_worker as worker
from ccfleetd import trial_runtime
from tests.test_devin_native import scenario as native_scenario
from tests.test_devin_trial_runtime import FakeWorker, body
from tests.test_devin_trial_runtime import trial as trial
from tests.test_devin_trial_worker import frames, unpack
from tests.test_devin_trial_worker import scene as worker_scene

REAL_ACCOUNTING = native._accounting
REAL_CONTROLLER = native._Controller


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "worker-fixture"
    root.mkdir(mode=0o700)
    yield from worker_scene.__wrapped__(root, monkeypatch)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    root = tmp_path / "native-fixture"
    root.mkdir(mode=0o700)
    return native_scenario.__wrapped__(root, monkeypatch)


@pytest.fixture
def full_path(trial, scene, scenario, monkeypatch):
    source, settings, sha, home, state, _ = scene
    # The trial's real fresh-HOME wrapper remains active. Its source policy is
    # fake, while the native adapter needs the fake peer's executable settings.
    combined = {**scenario.config.data, **source.data, "model_uid": "native-max"}
    combined["files"] = {**scenario.config.data["files"], **source.data["files"]}
    object.__setattr__(source, "_json", json.dumps(combined))
    monkeypatch.setattr(native, "_accounting", REAL_ACCOUNTING)
    original_close = native._Controller.close
    receipts, homes = [], []
    flags = {"input_tokens": 10, "cache_read": 8, "cache_creation": 4, "wrong_model": False,
             "extra_update": None}

    def close(peer):
        original_close(peer)
        fresh_home = Path(peer.options["env"]["HOME"])
        homes.append(fresh_home)
        assert fresh_home != home
        path = fresh_home / ".local/share/devin/cli/sessions.db"
        assert not path.exists()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metrics = {"input_tokens": flags["input_tokens"], "output_tokens": 2,
                   "cache_read_tokens": flags["cache_read"],
                   "cache_creation_tokens": flags["cache_creation"]}
        model = "wrong-model" if flags["wrong_model"] else "native-max"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)")
            db.execute("INSERT INTO message_nodes VALUES (?,?)", (peer.session, json.dumps({
                "role": "assistant", "metadata": {"request_id": "synthetic-provider-id",
                    "generation_model": model, "metrics": metrics}})))
            # Another session cannot supply identity or usage for this request.
            db.execute("INSERT INTO message_nodes VALUES (?,?)", ("foreign-session", json.dumps({
                "role": "assistant", "metadata": {"request_id": "foreign-provider-id",
                    "generation_model": "wrong-model", "metrics": {"input_tokens": 999999}}})))
        path.chmod(0o600)

    monkeypatch.setattr(native._Controller, "close", close)
    original_prompt = native._Controller.prompt

    def prompt(peer, session, context):
        answer = original_prompt(peer, session, context)
        if scenario.flags["mode"] == "final":
            if flags["extra_update"] is not None:
                peer.notices.append({"sessionId": session,
                    "update": {"sessionUpdate": flags["extra_update"]}})
        return answer

    monkeypatch.setattr(native._Controller, "prompt", prompt)

    class FramedWorker(FakeWorker):
        def perform(self, raw, authorized):
            assert authorized() is True
            self.model_started = True
            output = io.BytesIO()
            code = worker.serve(frames(raw), output,
                loader=lambda: (source, settings, sha), runner=native.run_step)
            records = unpack(output.getvalue())
            assert code == 0 and [entry[0]["status"] for entry in records] == [200, 200]
            receipts.append(records[-1][1])
            return records[-1][1]

    trial.runtime.transport = FramedWorker
    key = trial.runtime.create_key(trial.alice)

    def first(kind="tool", tool_count=1):
        if kind == "text":
            scenario.flags["mode"] = "final"
        initial = json.loads(body())
        initial["tools"] = [{"name": "Read" + str(index), "description": "Synthetic local tool",
            "input_schema": {"type": "object"}} for index in range(tool_count)]
        message, _ = trial.runtime.turn(key, json.dumps(initial).encode())
        assert message["stop_reason"] == ("tool_use" if kind == "tool" else "end_turn")
        continuation = initial
        continuation["messages"] += [
            {"role": "assistant", "content": message["content"]},
            {"role": "user", "content": ([{"type": "tool_result",
                "tool_use_id": message["content"][0]["id"],
                "content": "Synthetic client-local result; no tool executed."}]
                if kind == "tool" else "Synthetic follow-up question")},
        ]
        scenario.flags["mode"] = "final"
        return continuation

    return trial, first, key, flags, receipts, homes, scenario, state


@pytest.mark.parametrize("cache_read", [0, 8, None])
def test_tool_then_final_traverses_real_result_proof_and_preserves_history(full_path, cache_read):
    trial, first, key, flags, receipts, homes, scenario, state = full_path
    continuation = first()
    flags["cache_read"] = cache_read
    message, _ = trial.runtime.turn(key, json.dumps(continuation).encode())
    assert message["stop_reason"] == "end_turn"
    assert message["content"] == [{"type": "text", "text": "done"}]
    assert scenario.contexts[-1] == continuation
    assert len(set(homes)) == 2 and all(not path.parent.exists() for path in homes)
    assert len({peer.session for peer in scenario.instances}) == 2
    assert all(receipt["cleanup_confirmed"] for receipt in receipts)
    assert receipts[1]["metering"] == (
        "cache_read_unmeasured" if cache_read is None else "complete")
    usage = trial.runtime.allowance(trial.alice)
    assert usage["paused"] is False
    assert usage["unmeasured_reserved_tokens"] == (20000 if cache_read is None else 0)
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}


@pytest.mark.parametrize("tool_count", [1, 160])
@pytest.mark.parametrize("private_history", [False, True])
def test_text_then_text_with_current_catalog_and_private_reasoning_projection(
        full_path, tool_count, private_history):
    trial, first, key, flags, receipts, homes, scenario, state = full_path
    continuation = first("text", tool_count)
    if private_history:
        continuation["messages"][-2]["content"].insert(0, {
            "type": "thinking", "thinking": "PRIVATE_SYNTHETIC_REASONING",
            "signature": "PRIVATE_SYNTHETIC_SIGNATURE"})
    message, _ = trial.runtime.turn(key, json.dumps(continuation).encode())
    assert message["stop_reason"] == "end_turn" and len(scenario.instances) == 2
    assert len(scenario.contexts[-1]["tools"]) == tool_count
    assert "PRIVATE_SYNTHETIC" not in json.dumps(scenario.contexts[-1])
    assert scenario.contexts[-1]["messages"][-2]["content"] == [
        {"type": "text", "text": "done"}]
    assert scenario.contexts[-1]["messages"][-1]["content"] == "Synthetic follow-up question"
    assert trial.runtime.allowance(trial.alice)["paused"] is False
    assert all(not path.parent.exists() for path in homes)
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}


def test_large_prior_partial_does_not_turn_second_valid_partial_into_unknown(full_path):
    trial, first, key, flags, receipts, homes, scenario, state = full_path
    flags["cache_read"], flags["input_tokens"] = None, 86774
    continuation = first("text", 160)
    prior = trial.runtime.allowance(trial.alice)
    assert prior["unmeasured_reserved_tokens"] == 86780 and prior["available_tokens"] == 13220
    message, _ = trial.runtime.turn(key, json.dumps(continuation).encode())
    assert message["stop_reason"] == "end_turn"
    after = trial.runtime.allowance(trial.alice)
    assert after["paused"] is False and after["available_tokens"] == 0
    assert after["unmeasured_reserved_tokens"] == 173560
    assert all(receipt["metering"] == "cache_read_unmeasured" for receipt in receipts)


@pytest.mark.parametrize("failure", ["missing_cache_creation", "wrong_model", "tool_call_update"])
def test_second_turn_uncertainty_stays_fail_closed_without_replay(full_path, failure):
    trial, first, key, flags, receipts, homes, scenario, state = full_path
    continuation = first()
    if failure == "missing_cache_creation":
        flags["cache_creation"] = None
    elif failure == "wrong_model":
        flags["wrong_model"] = True
    else:
        flags["extra_update"] = failure
    with pytest.raises(trial_runtime.TrialUnavailable,
                       match="trial_result_unconfirmed_do_not_retry"):
        trial.runtime.turn(key, json.dumps(continuation).encode())
    assert len(scenario.instances) == 2 and len(receipts) == 2
    assert receipts[1]["metering"] == "unconfirmed"
    usage = trial.runtime.allowance(trial.alice)
    assert usage["paused"] is True and usage["reserved_tokens"] == 20000
    assert all(not path.parent.exists() for path in homes)
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}


def parser(tmp_path):
    controller = REAL_CONTROLLER(sys.executable, model="native-max", cwd=str(tmp_path),
        allowed_executables=[sys.executable], verified_models=["native-max"],
        allow_model_requests=True, env={})
    controller._sessions.add("synthetic-owned")
    return controller


def notification(kind, text="synthetic", session="synthetic-owned"):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session,
        "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}}


def test_actual_acp_parser_discards_owned_private_thoughts_before_final_text(tmp_path):
    controller = parser(tmp_path)
    for _ in range(256):
        controller._handle(notification("agent_thought_chunk", "PRIVATE_SYNTHETIC"),
            time.monotonic() + 1)
    controller._handle(notification("agent_message_chunk", "visible"), time.monotonic() + 1)
    content = native._final_text(controller.drain_notifications(), "synthetic-owned",
        {"stopReason": "end_turn"})
    assert content == [{"type": "text", "text": "visible"}]
    assert controller.pid is None
    controller.close()


def test_actual_acp_parser_keeps_foreign_thought_session_rejected(tmp_path):
    controller = parser(tmp_path)
    with pytest.raises(native.acp.ACPError):
        controller._handle(notification("agent_thought_chunk", session="foreign"),
            time.monotonic() + 1)
    assert controller.pid is None
    controller.close()


def test_actual_acp_parser_coalesces_short_final_response_fragments(tmp_path):
    controller = parser(tmp_path)
    for _ in range(129):
        controller._handle(notification("agent_message_chunk", "x"), time.monotonic() + 1)
    content = native._final_text(controller.drain_notifications(), "synthetic-owned",
        {"stopReason": "end_turn"})
    assert content == [{"type": "text", "text": "x" * 129}]
    assert controller.pid is None
    controller.close()


STREAM_PEER = r'''
import json, os, sqlite3, sys
from pathlib import Path
assert sys.argv[1:]==['acp','--model','native-max']
session='synthetic_'+str(os.getpid())
def send(value):
    os.write(1,(json.dumps(value,ensure_ascii=False)+'\n').encode())
for raw in sys.stdin.buffer:
    value=json.loads(raw)
    if 'method' not in value:continue
    method=value['method']; rpc_id=value['id']
    if method=='initialize':result={'protocolVersion':1}
    elif method=='session/new':
        result={'sessionId':session,'models':{'currentModelId':'native-max'}}
    elif method=='session/prompt':
        context=json.loads(value['params']['prompt'][0]['text'].split('\n',1)[1])
        assert value['params']['sessionId']==session
        continued=len(context['messages'])==3
        if continued:
            assert context['messages'][1]['content']==[{'type':'text','text':'first'}]
            assert context['messages'][2]['content']=='Synthetic second question'
        count=int(context['system'][0]['text']) if continued else 1
        for _ in range(count):
            send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
                'update':{'sessionUpdate':'agent_message_chunk',
                    'content':{'type':'text','text':'x' if continued else 'first'}}}})
        path=Path(os.environ['HOME'])/'.local/share/devin/cli/sessions.db'
        assert not path.exists()
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)')
            db.execute('INSERT INTO message_nodes VALUES (?,?)',(session,json.dumps({
                'role':'assistant','metadata':{'request_id':'synthetic-provider-id',
                    'generation_model':'native-max','metrics':{'input_tokens':10,
                        'output_tokens':2,'cache_read_tokens':None,'cache_creation_tokens':4}}})))
        path.chmod(0o600)
        result={'stopReason':'end_turn'}
    else:raise AssertionError('unrequested ACP method')
    send({'jsonrpc':'2.0','id':rpc_id,'result':result})
'''


@pytest.mark.parametrize("fragments,legacy_queue", [(129, False), (1024, False), (129, True)])
def test_real_acp_subprocess_followup_crosses_all_result_proof_layers(
        trial, scene, tmp_path, monkeypatch, fragments, legacy_queue):
    source, settings, sha, _, state, _ = scene
    executable = tmp_path / "synthetic-acp-peer"
    executable.write_text("#!" + sys.executable + "\n" + STREAM_PEER)
    executable.chmod(0o700)
    value = source.data
    value.update(enabled=True, model_uid="native-max", verified_models=["native-max"],
                 model_state_values=["native-max"], timeout_s=3)
    value["files"].update(executable={"path": str(executable)},
        python={"path": sys.executable}, mcp={"path": "/not-invoked/synthetic-mcp"})
    object.__setattr__(source, "_json", json.dumps(value))
    monkeypatch.setattr(native, "_isolated_subreaper", lambda: None)
    monkeypatch.setattr(native, "_identity", lambda *args: None)
    monkeypatch.setattr(native, "_cleanup_group", lambda *args: True)
    lifecycle = {"ready": True, "protocol_version": "2025-11-25"}
    monkeypatch.setattr(native, "_lifecycle", lambda *args: lifecycle)
    monkeypatch.setattr(native, "_wait_ready", lambda run, pid, *args: (
        {"pid": pid, "pgid": pid, "startticks": 1}, lifecycle))
    # Only MCP supervision/OS containment are synthetic. The actual native
    # ACPController starts our fixed local Python peer, parses every stream
    # frame, closes the child, and then reads the peer's real SQLite records.
    sessions, receipts, homes, peers = [], [], [], []
    controller_type = native._Controller

    class TrackedController(controller_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            peers.append(self)
            homes.append(Path(kwargs["env"]["HOME"]))

        def _queue_notification(self, params, *, provisional=False):
            if legacy_queue:
                # Negative control: restore only the old opaque visible queue.
                # The actual ACP envelope/session validation and the same
                # SQLite model/usage proof remain in force in both cases.
                return native.acp.ACPController._queue_notification(
                    self, params, provisional=provisional)
            return super()._queue_notification(params, provisional=provisional)

        def _rpc(self, method, params):
            result = super()._rpc(method, params)
            if method == "session/new":
                sessions.append(result["sessionId"])
            return result

    monkeypatch.setattr(native, "_Controller", TrackedController)

    class FramedWorker(FakeWorker):
        def perform(self, raw, authorized):
            assert authorized() is True
            self.model_started = True
            output = io.BytesIO()
            assert worker.serve(frames(raw), output,
                loader=lambda: (source, settings, sha), runner=native.run_step) == 0
            records = unpack(output.getvalue())
            assert [entry[0]["status"] for entry in records] == [200, 200]
            receipts.append(records[-1][1])
            return records[-1][1]

    trial.runtime.transport = FramedWorker
    token = trial.runtime.create_key(trial.alice)
    request = json.loads(body(system=[{"type": "text", "text": str(fragments)}]))
    first, _ = trial.runtime.turn(token, json.dumps(request).encode())
    assert first["content"] == [{"type": "text", "text": "first"}]
    request["messages"] += [{"role": "assistant", "content": first["content"]},
        {"role": "user", "content": "Synthetic second question"}]
    if legacy_queue:
        with pytest.raises(trial_runtime.TrialUnavailable) as error:
            trial.runtime.turn(token, json.dumps(request).encode())
        assert error.value.code == "trial_result_unconfirmed_do_not_retry"
        assert error.value.status == 503
    else:
        second, _ = trial.runtime.turn(token, json.dumps(request).encode())
        assert second["content"] == [{"type": "text", "text": "x" * fragments}]
    assert len(set(sessions)) == len(peers) == 2
    assert all(peer._closed and peer._process.returncode is not None for peer in peers)
    assert len(set(homes)) == 2 and all(not home.parent.exists() for home in homes)
    usage = trial.runtime.allowance(trial.alice)
    assert all(item["cleanup_confirmed"] for item in receipts)
    if legacy_queue:
        assert usage["paused"] is True and usage["reserved_tokens"] == 40000
        assert usage["unmeasured_reserved_tokens"] == 20000
        assert receipts[1]["metering"] == "unconfirmed"
        diagnostic = json.loads((state / worker.DIAGNOSTIC).read_bytes())
        assert diagnostic["native_error"] == "limit_exceeded"
        with trial.runtime._ledger() as book:
            assert book.connection.execute(
                "SELECT reason FROM devin_trial_control"
            ).fetchone() == ("reported_usage_unaccounted",)
            assert book.connection.execute(
                "SELECT active_tokens FROM devin_trial_sessions WHERE state='closing'"
            ).fetchone() == (20000,)
    else:
        assert all(item["metering"] == "cache_read_unmeasured" for item in receipts)
        assert usage["paused"] is False and usage["unmeasured_reserved_tokens"] == 40000
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}
