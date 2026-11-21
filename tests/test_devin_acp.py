"""Fake executable ACP peers only; no Devin, account, auth, or model requests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from ccfleet_agent import devin_acp
from ccfleet_agent.devin_acp import ACPController, ACPError

PEER = r'''
import json, os, signal, subprocess, sys, time
mode = os.environ.get('ACP_FAKE_MODE', 'ok')
record = os.environ['ACP_FAKE_RECORD']
def send(value):
    os.write(1, (json.dumps(value, ensure_ascii=False)+'\n').encode())
def answer(i, value):
    send({'jsonrpc':'2.0','id':i,'result':value})
for raw in sys.stdin.buffer:
    value=json.loads(raw)
    with open(record,'a') as f:f.write(json.dumps(value)+'\n')
    if 'method' not in value:continue
    i=value['id']; method=value['method']
    if mode.startswith('vendor') and method=='initialize':
        notice={'jsonrpc':'2.0','method':'_cognition.ai/mcp/serversChanged',
                'params':{'private':'must not be retained'}}
        if mode=='vendor-params':notice['params']=None
        if mode=='vendor-invalid-id':notice['id']=True
        if mode=='vendor-null-id':notice['id']=None
        if mode=='vendor-request':notice['id']='vendor-1'
        if mode=='vendor-prefix-unknown':notice['method']='_cognition.ai/newNotice'
        for _ in range(300 if mode=='vendor-flood' else 1):send(notice)
    if mode=='outside-update' and method=='initialize':
        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake','update':{}}})
    if mode=='timeout':time.sleep(30)
    if mode=='malformed':os.write(1,b'not json\n');time.sleep(30)
    if mode=='oversize':os.write(1,b'x'*5000);time.sleep(30)
    if mode=='stderr':os.write(2,b'x'*5000);time.sleep(30)
    if mode=='unknown-id':answer(i+1,{});time.sleep(30)
    if mode=='duplicate':
        line=(json.dumps({'jsonrpc':'2.0','id':i,'result':{'protocolVersion':1}})+'\n').encode()
        os.write(1,line+line);time.sleep(30)
    if mode=='duplicate-json':os.write(1,b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n');time.sleep(30)
    if mode=='rpc-error':send({'jsonrpc':'2.0','id':i,'error':{'code':-1,'message':'SECRET must not surface'}});continue
    if mode=='unknown-notification':send({'jsonrpc':'2.0','method':'run/arbitrary','params':{}});continue
    if mode=='child':
        child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])
        with open(record+'.child','w') as f:f.write(str(child.pid))
    if mode in ('permission','tool','duplicate-agent') and method=='initialize':
        name='session/request_permission' if mode=='permission' else 'terminal/create'
        req={'jsonrpc':'2.0','id':'agent-1','method':name,'params':{'command':'must never execute'}}
        send(req)
        if mode=='duplicate-agent':send(req)
    if method=='initialize':
        answer(i,{'protocolVersion':2 if mode=='version' else 1,
                  'agentCapabilities':{},'authMethods':[],'agentInfo':{'name':'fake'}})
    elif method=='session/new':
        if mode=='prefix-provisional':
            send({'jsonrpc':'2.0','method':'_cognition.ai/newNotice',
                  'params':{'sessionId':'session_fake'}})
        if mode.startswith('early-'):
            notice={'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake',
                    'update':{'sessionUpdate':'config_option_update','configOptions':[]}}}
            for _ in range(3 if mode=='early-flood' else 1):send(notice)
            if mode=='early-multiple':
                notice['params']['sessionId']='other';send(notice)
            if mode=='early-timeout':time.sleep(30)
            if mode=='early-error':send({'jsonrpc':'2.0','id':i,'error':{'code':-1,'message':'private'}})
            else:answer(i,{'sessionId':'other' if mode=='early-mismatch' else 'session_fake'})
        elif mode in ('coalesced','wrong-session'):
            reply={'jsonrpc':'2.0','id':i,'result':{'sessionId':'session_fake'}}
            update={'jsonrpc':'2.0','method':'session/update','params':{
              'sessionId':'wrong' if mode=='wrong-session' else 'session_fake',
              'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'hello'}}}}
            os.write(1,(json.dumps(reply)+'\n'+json.dumps(update)+'\n').encode())
        else:answer(i,{'sessionId':'session_fake'})
    elif method=='session/prompt':
        if mode.startswith('stream-'):
            _,kind,count=mode.split('-')
            for n in range(int(count)):
                if kind in ('thought','mixed','private'):
                    send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake',
                        'update':{'sessionUpdate':'agent_thought_chunk',
                            'content':{'type':'text','text':'PRIVATE_SYNTHETIC'}}}})
                if kind in ('text','mixed'):
                    send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake',
                        'update':{'sessionUpdate':'agent_message_chunk',
                            'content':{'type':'text','text':'日本語'}}}})
            if kind=='private':
                send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake',
                    'update':{'sessionUpdate':'agent_message_chunk',
                        'content':{'type':'text','text':'日本語'}}}})
            answer(i,{'stopReason':'end_turn'})
            continue
        if mode in ('prefix-owned','prefix-foreign'):
            send({'jsonrpc':'2.0','method':'_cognition.ai/newNotice','params':{
                'sessionId':'session_fake' if mode=='prefix-owned' else 'foreign'}})
        for n in range(3 if mode=='flood' else 1):
            send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'session_fake',
                  'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'日本語'}}}})
        answer(i,{} if mode=='missing-stop' else {'stopReason':'unknown' if mode=='unknown-stop' else 'end_turn'})
'''


@pytest.fixture
def fake(tmp_path):
    executable = tmp_path / "devin-fake"
    executable.write_text("#!" + sys.executable + "\n" + PEER)
    executable.chmod(0o700)
    record = tmp_path / "calls.jsonl"

    def make(mode="ok", **changes):
        options = {"model": "claude-opus-5-5-max", "cwd": str(tmp_path),
                   "allowed_executables": [str(executable)],
                   "verified_models": {"claude-opus-5-5-max"}, "timeout_s": 1.5,
                   "env": {"ACP_FAKE_MODE": mode, "ACP_FAKE_RECORD": str(record)}}
        options.update(changes)
        controller_type = options.pop('_controller_type', ACPController)
        return controller_type(str(executable), **options)

    return make, record, executable


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_initialize_exact_model_fixed_argv_and_no_client_actions(fake):
    make, record, _ = fake
    with make() as controller:
        assert controller.pid is not None
        assert controller._process.args[-3:] == ['acp', '--model', 'claude-opus-5-5-max']
    calls = records(record)
    assert len(calls) == 1
    assert calls[0] == {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": 1, "clientCapabilities": {},
        "clientInfo": {"name": "ccfleet", "version": "0.1.0"}}}


def test_default_guard_blocks_sessions_and_prompts_without_spawning(fake):
    make, record, _ = fake
    controller = make()
    for operation in (controller.new_session, lambda: controller.prompt("s", "hello")):
        with pytest.raises(ACPError) as error:
            operation()
        assert error.value.code == "model_requests_disabled"
    assert controller.pid is None and not record.exists()
    controller.close()


def test_guarded_session_prompt_and_opaque_notifications(fake):
    make, record, _ = fake
    with make(allow_model_requests=True) as controller:
        session = controller.new_session()["sessionId"]
        assert controller.prompt(session, "Synthetic test only") == {"stopReason": "end_turn"}
        updates = controller.drain_notifications()
        assert updates[0]["update"]["content"]["text"] == "日本語"
        assert controller.drain_notifications() == []
    calls = records(record)
    assert [call["method"] for call in calls] == ["initialize", "session/new", "session/prompt"]
    assert calls[1]["params"]["mcpServers"] == []
    assert calls[2]["params"]["prompt"] == [{"type": "text", "text": "Synthetic test only"}]


def test_original_prompt_bound_remains_default_and_rejects_before_send(fake):
    make, record, _ = fake
    with make(allow_model_requests=True) as controller:
        session = controller.new_session()["sessionId"]
        with pytest.raises(ACPError):
            controller.prompt(session, "x" * (64 * 1024 + 1))
    assert [call["method"] for call in records(record)] == ["initialize", "session/new"]


def test_trusted_larger_prompt_limit_transmits_full_unicode_context(fake):
    make, record, _ = fake
    context = "é" * 40000
    with make(allow_model_requests=True, max_prompt_bytes=90000) as controller:
        session = controller.new_session()["sessionId"]
        assert controller.prompt(session, context) == {"stopReason": "end_turn"}
    assert records(record)[-1]["params"]["prompt"] == [{"type": "text", "text": context}]


def test_prompt_limit_is_utf8_bytes_and_checked_before_rpc(fake):
    make, record, _ = fake
    with make(allow_model_requests=True, max_prompt_bytes=4) as controller:
        session = controller.new_session()["sessionId"]
        assert controller.prompt(session, "éé") == {"stopReason": "end_turn"}
        with pytest.raises(ACPError):
            controller.prompt(session, "ééé")
    assert len([call for call in records(record) if call["method"] == "session/prompt"]) == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 2 * 1024 * 1024 + 4097, "65536"])
def test_trusted_prompt_limit_still_has_a_hard_validated_bound(fake, limit):
    make, record, _ = fake
    with pytest.raises(ACPError):
        make(max_prompt_bytes=limit)
    assert not record.exists()


@pytest.mark.parametrize("mode, code", [
    ("malformed", "protocol_error"), ("oversize", "limit_exceeded"),
    ("stderr", "limit_exceeded"), ("timeout", "timeout"),
    ("unknown-id", "protocol_error"), ("duplicate", "protocol_error"),
    ("duplicate-json", "protocol_error"), ("rpc-error", "rpc_error"),
    ("unknown-notification", "unsupported_method"), ("version", "version_mismatch"),
    ("duplicate-agent", "protocol_error"),
])
def test_protocol_failures_close_the_owned_process_without_retry(fake, mode, code):
    make, record, _ = fake
    controller = make(mode, max_line_bytes=1024, max_output_bytes=2048)
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == code
    assert "SECRET" not in str(error.value)
    assert len([call for call in records(record) if call.get("method") == "initialize"]) == 1
    assert controller._process.returncode is not None
    with pytest.raises(ACPError):
        controller.initialize()
    assert len([call for call in records(record) if call.get("method") == "initialize"]) == 1


@pytest.mark.parametrize("mode", ["permission", "tool"])
def test_unsolicited_agent_requests_are_cancelled_or_unsupported(fake, mode):
    make, record, _ = fake
    with make(mode) as controller:
        assert controller.rejected_methods == [
            "session/request_permission" if mode == "permission" else "terminal/create"]
        # Ask a guarded second RPC so the peer has consumed our denial response.
        controller.allow_model_requests = True
        controller.new_session()
    replies = [call for call in records(record) if "method" not in call]
    assert len(replies) == 1
    if mode == "permission":
        assert replies[0]["result"] == {"outcome": {"outcome": "cancelled"}}
    else:
        assert replies[0]["error"]["code"] == -32601


def test_notification_flood_is_bounded_and_cleans_up(fake):
    make, _, _ = fake
    with make("flood", allow_model_requests=True, max_notifications=2) as controller:
        session = controller.new_session()["sessionId"]
        with pytest.raises(ACPError) as error:
            controller.prompt(session, "Synthetic")
        assert error.value.code == "limit_exceeded"
        assert controller._process.returncode is not None


def test_coalesced_session_response_binds_before_its_notification(fake):
    make, _, _ = fake
    with make("coalesced", allow_model_requests=True) as controller:
        assert controller.new_session()["sessionId"] == "session_fake"
        assert controller.drain_notifications()[0]["sessionId"] == "session_fake"


def test_coalesced_notification_for_different_session_still_fails_closed(fake):
    make, _, _ = fake
    with make("wrong-session", allow_model_requests=True) as controller:
        with pytest.raises(ACPError, match="active session"):
            controller.new_session()


def test_model_enabled_mode_cannot_inherit_ambient_credentials(fake):
    make, record, _ = fake
    with pytest.raises(ACPError, match="explicit environment"):
        make(allow_model_requests=True, env=None)
    assert not record.exists()


def test_cleanup_kills_owned_same_group_descendant(fake):
    make, record, _ = fake
    controller = make("child")
    controller.initialize()
    child = int(record.with_suffix(record.suffix + ".child").read_text())
    controller.close()
    controller.close()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        # A killed descendant can remain an orphan zombie briefly; it must not
        # retain open pipes or run. The owned leader is always reaped here.
        import subprocess
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(child)],
                               capture_output=True, text=True, check=False).stdout.strip()
        assert not state or state.startswith("Z")
    assert controller._process.returncode is not None


@pytest.mark.parametrize("changes", [
    {"allowed_executables": []}, {"verified_models": []}, {"verified_models": None},
    {"verified_models": "claude-opus-5-5-max"},
    {"model": "unknown"}, {"model": "--dangerous"}, {"model": "model\ncommand"},
    {"cwd": "relative"}, {"timeout_s": float("nan")}, {"timeout_s": True},
    {"max_line_bytes": 10}, {"max_notifications": 0}, {"allow_model_requests": 1},
])
def test_constructor_rejects_untrusted_configs_before_launch(fake, changes):
    make, record, _ = fake
    with pytest.raises(ACPError):
        make(**changes)
    assert not record.exists()


def test_no_shell_or_extra_arguments_and_closed_controller_stays_closed(fake):
    make, record, executable = fake
    with pytest.raises(ACPError):
        ACPController("relative-devin", model="m", cwd=str(executable.parent),
                      allowed_executables=[str(executable)], verified_models={"m"})
    controller = make()
    controller.close()
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == "closed"
    assert not record.exists()


def test_unknown_session_and_invalid_prompt_do_not_send_requests(fake):
    make, record, _ = fake
    with make(allow_model_requests=True) as controller:
        with pytest.raises(ACPError):
            controller.prompt("unknown", "Synthetic")
        session = controller.new_session()["sessionId"]
        with pytest.raises(ACPError):
            controller.prompt(session, "\x00")
    assert len(records(record)) == 2


@pytest.mark.parametrize("mode", ["missing-stop", "unknown-stop"])
def test_prompt_requires_a_recognized_completion_reason(fake, mode):
    make, _, _ = fake
    with make(mode, allow_model_requests=True) as controller:
        session = controller.new_session()["sessionId"]
        with pytest.raises(ACPError, match="stop reason"):
            controller.prompt(session, "Synthetic")
        assert controller._process.returncode is not None


def test_close_during_process_start_cannot_lose_the_owned_child(fake, monkeypatch):
    make, _, _ = fake
    controller = make()
    entered, release = threading.Event(), threading.Event()
    original = devin_acp.subprocess.Popen

    def paused_popen(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(devin_acp.subprocess, "Popen", paused_popen)
    failures = []

    def initialize():
        try:
            controller.initialize()
        except (ACPError, OSError, ValueError) as error:
            failures.append(type(error).__name__)

    worker = threading.Thread(target=initialize)
    closer = threading.Thread(target=controller.close)
    worker.start()
    assert entered.wait(1)
    try:
        closer.start()
        time.sleep(0.02)
        assert closer.is_alive()  # close waits for publication before cleanup.
    finally:
        release.set()
        worker.join(3)
        closer.join(3)
        controller.close()
    assert not worker.is_alive() and not closer.is_alive()
    assert controller._process is not None and controller._process.returncode is not None


VENDOR_NOTICE = '_cognition.ai/mcp/serversChanged'


def test_vendor_notification_is_rejected_by_default(fake):
    make, _, _ = fake
    controller = make('vendor-notice')
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == 'unsupported_method'
    assert controller._process.returncode is not None


def test_exact_trusted_vendor_notification_is_ignored_without_a_queue(fake):
    make, _, _ = fake
    with make('vendor-flood', ignored_notifications=(VENDOR_NOTICE,),
              max_notifications=1) as controller:
        assert controller.drain_notifications() == []
        assert controller.rejected_methods == []
        assert controller._messages == 301


@pytest.mark.parametrize('mode', ['vendor-params', 'vendor-invalid-id'])
def test_allowlisted_notice_does_not_hide_malformed_parameters_or_ids(fake, mode):
    make, _, _ = fake
    controller = make(mode, ignored_notifications=(VENDOR_NOTICE,))
    with pytest.raises(ACPError):
        controller.initialize()
    assert controller._process.returncode is not None


def test_allowlisted_name_with_request_id_is_still_explicitly_unsupported(fake):
    make, record, _ = fake
    with make('vendor-request', ignored_notifications=(VENDOR_NOTICE,),
              allow_model_requests=True) as controller:
        assert controller.rejected_methods == [VENDOR_NOTICE]
        controller.new_session()  # Make the fake consume the rejection response.
    reply = next(item for item in records(record) if item.get('id') == 'vendor-1')
    assert reply['error']['code'] == -32601


@pytest.mark.parametrize('limit', ['messages', 'bytes'])
def test_ignored_notifications_still_count_toward_existing_bounds(fake, monkeypatch, limit):
    make, _, _ = fake
    if limit == 'messages':
        monkeypatch.setattr(devin_acp, 'MAX_RPC_MESSAGES', 5)
    options = {'max_output_bytes': 1024} if limit == 'bytes' else {}
    controller = make('vendor-flood', ignored_notifications=(VENDOR_NOTICE,), **options)
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == 'limit_exceeded'
    assert controller.drain_notifications() == []


@pytest.mark.parametrize('names', [None, VENDOR_NOTICE, ['session/update'], ['_'],
                                 ['_bad notice'], [123], ['_' + 'a' * 100],
                                 ['_notice' + str(i) for i in range(9)]])
def test_vendor_notice_allowlist_requires_a_bounded_exact_name_collection(fake, names):
    make, record, _ = fake
    with pytest.raises(ACPError) as error:
        make(ignored_notifications=names)
    assert error.value.code == 'invalid_config'
    assert not record.exists()


def test_early_session_notification_is_released_only_after_matching_response(fake):
    make, _, _ = fake
    with make('early-valid', allow_model_requests=True) as controller:
        session = controller.new_session()['sessionId']
        updates = controller.drain_notifications()
        assert session == 'session_fake'
        assert updates == [{'sessionId': session, 'update': {
            'sessionUpdate': 'config_option_update', 'configOptions': []}}]
        assert controller._provisional_notifications == []
        assert controller._provisional_session_id is None


@pytest.mark.parametrize('mode, code', [
    ('early-mismatch', 'protocol_error'), ('early-multiple', 'protocol_error'),
    ('early-timeout', 'timeout'), ('early-error', 'rpc_error'),
    ('early-flood', 'limit_exceeded'),
])
def test_unbound_provisional_notifications_are_discarded_on_failure(fake, mode, code):
    make, _, _ = fake
    with make(mode, allow_model_requests=True, max_notifications=2) as controller:
        with pytest.raises(ACPError) as error:
            controller.new_session()
        assert error.value.code == code
        assert controller.drain_notifications() == []
        assert controller._provisional_notifications == []
        assert controller._provisional_session_id is None
        assert controller._process.returncode is not None


def test_unknown_session_update_outside_pending_new_is_never_quarantined(fake):
    make, _, _ = fake
    controller = make('outside-update', allow_model_requests=True)
    with pytest.raises(ACPError, match='active session'):
        controller.initialize()
    assert controller._provisional_notifications == []
    assert controller.drain_notifications() == []


PREFIX = "_cognition.ai/"
PREFIX_NOTICE = "_cognition.ai/newNotice"


def test_prefix_policy_is_explicit_and_default_remains_strict(fake):
    make, record, _ = fake
    controller = make("vendor-prefix-unknown")
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == "unsupported_method"
    assert len(records(record)) == 1


def test_explicit_prefix_ignores_unknown_global_notices_without_acks_or_queue(fake):
    make, record, _ = fake
    prefixes = [PREFIX]
    controller = make("vendor-prefix-unknown", ignored_notification_prefixes=prefixes)
    prefixes.clear()  # Constructor must freeze the trusted configuration.
    with controller:
        assert controller.drain_notifications() == []
        assert controller._messages == 2 and controller._total > 0
        assert controller.rejected_methods == []
    assert [call["method"] for call in records(record)] == ["initialize"]


def test_prefix_notices_with_owned_session_are_discarded_without_losing_real_updates(fake):
    make, _, _ = fake
    with make("prefix-owned", allow_model_requests=True,
              ignored_notification_prefixes=(PREFIX,)) as controller:
        session = controller.new_session()["sessionId"]
        assert controller.prompt(session, "Synthetic test") == {"stopReason": "end_turn"}
        assert len(controller.drain_notifications()) == 1


@pytest.mark.parametrize("mode", ["prefix-foreign", "prefix-provisional"])
def test_prefix_policy_rejects_foreign_and_unbound_session_notices(fake, mode):
    make, _, _ = fake
    with make(mode, allow_model_requests=True,
              ignored_notification_prefixes=(PREFIX,)) as controller:
        with pytest.raises(ACPError) as error:
            session = controller.new_session()["sessionId"]
            controller.prompt(session, "Synthetic test")
        assert error.value.code == "protocol_error"
        assert controller._process.returncode is not None


@pytest.mark.parametrize("session", [None, "", True, [], {}, "foreign", "bad session"])
def test_provided_prefix_session_must_be_valid_and_owned(fake, session):
    make, _, _ = fake
    controller = make(ignored_notification_prefixes=(PREFIX,))
    with pytest.raises(ACPError):
        controller._handle({"jsonrpc": "2.0", "method": PREFIX_NOTICE,
                            "params": {"sessionId": session}}, time.monotonic() + 1)
    assert controller._messages == 1 and controller.pid is None
    assert controller.drain_notifications() == []
    controller.close()


def test_legacy_exact_notice_still_supports_empty_global_session_before_prefix_policy(fake):
    make, _, _ = fake
    controller = make(ignored_notifications=(VENDOR_NOTICE,),
                      ignored_notification_prefixes=(PREFIX,))
    controller._handle({"jsonrpc": "2.0", "method": VENDOR_NOTICE,
                        "params": {"sessionId": ""}}, time.monotonic() + 1)
    assert controller._messages == 1 and controller.drain_notifications() == []
    controller.close()


@pytest.mark.parametrize("rpc_id", [None, True, 1, "vendor-request"])
def test_id_bearing_messages_never_enter_prefix_ignore_path(fake, rpc_id):
    make, _, _ = fake
    controller = make(ignored_notification_prefixes=(PREFIX,))
    replies = []
    controller._write = lambda response, deadline: replies.append(response)
    message = {"jsonrpc": "2.0", "method": PREFIX_NOTICE, "id": rpc_id, "params": {}}
    if rpc_id is None or rpc_id is True:
        with pytest.raises(ACPError):
            controller._handle(message, time.monotonic() + 1)
        assert replies == []
    else:
        controller._handle(message, time.monotonic() + 1)
        assert replies == [{"jsonrpc": "2.0", "id": rpc_id,
                            "error": {"code": -32601, "message": "Client method unsupported"}}]
        assert controller.rejected_methods == [PREFIX_NOTICE]
    assert controller._messages == 1 and controller.drain_notifications() == []
    controller.close()


@pytest.mark.parametrize("change", [
    {"params": None}, {"params": []}, {"extra": True}, {"method": "_cognition.ai/bad name"}])
def test_prefix_policy_does_not_skip_envelope_method_or_parameter_validation(fake, change):
    make, _, _ = fake
    controller = make(ignored_notification_prefixes=(PREFIX,))
    message = {"jsonrpc": "2.0", "method": PREFIX_NOTICE, "params": {}}
    message.update(change)
    with pytest.raises(ACPError):
        controller._handle(message, time.monotonic() + 1)
    assert controller._messages == 1 and controller.drain_notifications() == []
    controller.close()


@pytest.mark.parametrize("prefixes", [None, PREFIX, {}, [None], [""], ["_/"], ["_"],
    ["cognition.ai/"], ["_cognition.ai"], ["_cognition.ai/*/"], ["_cognition.ai//"],
    ["_cognition.ai/../"], ["_cognition.ai/\n"], ["_" + "x" * 80 + "/"],
    ["_one/", "_two/", "_three/", "_four/", "_five/"]])
def test_prefix_configuration_is_bounded_explicit_namespaces_only(fake, prefixes):
    make, record, _ = fake
    with pytest.raises(ACPError) as error:
        make(ignored_notification_prefixes=prefixes)
    assert error.value.code == "invalid_config" and not record.exists()


@pytest.mark.parametrize("limit", ["messages", "bytes"])
def test_prefix_notices_keep_wire_and_message_bounds_even_without_queue(fake, monkeypatch, limit):
    make, _, _ = fake
    options = {"max_notifications": 1, "ignored_notification_prefixes": (PREFIX,)}
    if limit == "messages":
        monkeypatch.setattr(devin_acp, "MAX_RPC_MESSAGES", 4)
    else:
        options["max_output_bytes"] = 512
    controller = make("vendor-flood", **options)
    with pytest.raises(ACPError) as error:
        controller.initialize()
    assert error.value.code == "limit_exceeded"
    assert controller.drain_notifications() == []
    assert controller._process.returncode is not None
