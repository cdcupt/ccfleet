"""Actual ACP subprocess handoffs with real receipt/SQLite/ledger proof, synthetic peer only."""

import io
import json
import sys
from pathlib import Path

import pytest

from ccfleet_agent import devin_native as native
from ccfleet_agent import devin_trial_worker as worker
from ccfleetd import devin_trial, trial_runtime
from tests.test_devin_trial_continuation import scene as scene
from tests.test_devin_trial_runtime import FakeWorker, body
from tests.test_devin_trial_runtime import trial as trial
from tests.test_devin_trial_worker import frames, unpack

HANDOFF_PEER = r'''
import json, os, runpy, sqlite3, sys, time
from pathlib import Path
assert sys.argv[1:]==['acp','--model','native-max']
session='synthetic_'+str(os.getpid())
def send(value):os.write(1,(json.dumps(value)+'\n').encode())
def account():
    path=Path(os.environ['HOME'])/'.local/share/devin/cli/sessions.db'
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)')
        db.execute('INSERT INTO message_nodes VALUES (?,?)',(session,json.dumps({
            'role':'assistant','metadata':{'request_id':'synthetic-provider-id',
                'generation_model':'native-max','metrics':{'input_tokens':10,
                    'output_tokens':2,'cache_read_tokens':0,'cache_creation_tokens':0}}})))
    path.chmod(0o600)
for raw in sys.stdin.buffer:
    value=json.loads(raw)
    if 'method' not in value:continue
    method=value['method'];rpc_id=value['id']
    if method=='initialize':result={'protocolVersion':1}
    elif method=='session/new':
        server=value['params']['mcpServers'][0]
        scope=runpy.run_path(server['args'][1]);run=Path(server['args'][-1])
        result={'sessionId':session,'models':{'currentModelId':'native-max'}}
    elif method=='session/prompt':
        context=json.loads(value['params']['prompt'][0]['text'].split('\n',1)[1])
        count=len(context['messages']);late=context['system'][0]['text']=='late-accounting'
        assert count in (1,3,5)
        if count>1:
            for index in range(1,count,2):
                offered=context['messages'][index]['content'][0]
                returned=context['messages'][index+1]['content'][0]
                assert offered['type']=='tool_use' and returned['type']=='tool_result'
                assert offered['id']==returned['tool_use_id']
                assert returned['content']=='synthetic-local-result-'+str(index)
        if not late:account()
        if count<5:
            directory=scope['RunDirectory'](run)
            try:
                config=directory.read('config.json');bound=directory.read('session.json')
                catalog=scope['ToolCatalog'](config['tools'])
                handoff=scope['ToolHandoff'](catalog,binding=bound['binding'],session_id=session,server_id=bound['server_id'])
                tool=handoff.begin({'jsonrpc':'2.0','id':'call-1','method':'tools/call','params':{
                    'name':'Read' if count==1 else 'Bash','arguments':{'value':'synthetic'}}},
                    binding=bound['binding'],session_id=session,server_id=bound['server_id'],remaining_seconds=1)
                directory.write_exclusive('call.json',{'version':1,'frontend_tool_use':tool.as_tool_use(),
                    'native_rpc_id':'call-1','context':bound,'process':{'pid':os.getpid(),'pgid':os.getpid(),'startticks':1}})
            finally:directory.close()
            # Handoff must be detected while prompt is still waiting, then child reaped.
            time.sleep(2)
            if late:account()
            result={'stopReason':'cancelled'}
        else:
            send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,'update':{
                'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'verified final'}}}})
            result={'stopReason':'end_turn'}
    else:raise AssertionError('unexpected ACP method')
    send({'jsonrpc':'2.0','id':rpc_id,'result':result})
'''


@pytest.mark.parametrize("late_accounting", [False, True])
def test_real_child_pending_tool_handoffs_then_final_or_unknown_usage_pause(
        trial, scene, tmp_path, monkeypatch, late_accounting):
    source, settings, sha, _, state, _ = scene
    executable = tmp_path / "synthetic-handoff-peer"
    executable.write_text("#!" + sys.executable + "\n" + HANDOFF_PEER)
    executable.chmod(0o700)
    value = source.data
    value.update(enabled=True, model_uid="native-max", verified_models=["native-max"],
                 model_state_values=["native-max"], timeout_s=3)
    value["files"].update(executable={"path": str(executable)}, python={"path": sys.executable},
                          mcp={"path": str(Path(native.mcp.__file__).resolve())})
    object.__setattr__(source, "_json", json.dumps(value))
    monkeypatch.setattr(native, "_isolated_subreaper", lambda: None)
    monkeypatch.setattr(native, "_identity", lambda *args: None)
    monkeypatch.setattr(native, "_cleanup_group", lambda *args: True)
    lifecycle = {"ready": True, "protocol_version": "2025-11-25"}
    monkeypatch.setattr(native, "_lifecycle", lambda *args: lifecycle)
    monkeypatch.setattr(native, "_wait_ready", lambda run, pid, *args: (
        {"pid": pid, "pgid": pid, "startticks": 1}, lifecycle))
    peers, homes, receipts = [], [], []
    controller_type = native._Controller

    class TrackedController(controller_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            peers.append(self)
            homes.append(Path(kwargs["env"]["HOME"]))

    monkeypatch.setattr(native, "_Controller", TrackedController)

    class FramedWorker(FakeWorker):
        def perform(self, raw, authorized):
            assert authorized()
            self.model_started = True
            output = io.BytesIO()
            assert worker.serve(frames(raw), output,
                                loader=lambda: (source, settings, sha), runner=native.run_step) == 0
            records = unpack(output.getvalue())
            assert [r[0]["status"] for r in records] == [200, 200]
            receipts.append(records[-1][1])
            return records[-1][1]

    trial.runtime.transport = FramedWorker
    token = trial.runtime.create_key(trial.alice)
    request = json.loads(body(system=[{"type": "text", "text": (
        "late-accounting" if late_accounting else "committed-accounting")}], tools=[
        {"name": name, "description": "Synthetic local tool", "input_schema": {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"]}} for name in ("Read", "Bash")]))
    if late_accounting:
        with pytest.raises(trial_runtime.TrialUnavailable) as error:
            trial.runtime.turn(token, json.dumps(request).encode())
        assert error.value.code == "trial_result_unconfirmed_do_not_retry"
        allowance = trial.runtime.allowance(trial.alice)
        assert allowance["paused"] and allowance["reserved_tokens"] == devin_trial.TURN_RESERVATION_TOKENS
        assert receipts[0]["metering"] == "unconfirmed"
        assert len(peers) == 1
    else:
        for index, expected in enumerate(("Read", "Bash", None)):
            message, _ = trial.runtime.turn(token, json.dumps(request).encode())
            if expected:
                assert message["stop_reason"] == "tool_use"
                offered = message["content"][0]
                assert offered["name"] == expected
                request["messages"] += [{"role": "assistant", "content": message["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": offered["id"],
                        "content": "synthetic-local-result-" + str(2 * index + 1)}]}]
            else:
                assert message["stop_reason"] == "end_turn"
                assert message["content"] == [{"type": "text", "text": "verified final"}]
        allowance = trial.runtime.allowance(trial.alice)
        assert not allowance["paused"] and allowance["reserved_tokens"] == 0
        assert allowance["reported_tokens"] == 36
        assert all(r["metering"] == "complete" for r in receipts)
        assert len(peers) == 3
    assert all(peer._closed and peer._process.returncode is not None for peer in peers)
    assert len(set(homes)) == len(peers) and all(not home.parent.exists() for home in homes)
    assert all(r["cleanup_confirmed"] for r in receipts)
    assert set(state.iterdir()) == {state / ".trial.lock", state / worker.DIAGNOSTIC}
