"""Normal-use regressions: real framing/ledgers, synthetic native execution."""
import copy
import fcntl
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

from ccfleet_agent import devin_native as native
from ccfleet_agent import devin_trial_provision as provision
from ccfleet_agent import devin_trial_worker as worker
from ccfleetd import devin_trial as ledger
from ccfleetd import trial_runtime
from tests.test_devin_mcp import private as private
from tests.test_devin_native import policy as policy
from tests.test_devin_trial_provision import scene as _provision_scene
from tests.test_devin_trial_provision import write
from tests.test_devin_trial_runtime import FakeWorker, body, receipt
from tests.test_devin_trial_runtime import trial as trial
from tests.test_devin_trial_worker import frames, result, unpack
from tests.test_devin_trial_worker import scene as scene

provision_scene = _provision_scene


@pytest.mark.parametrize("code", ["timeout", "native_prompt_deadline_exceeded"])
def test_clean_known_model_timeout_returns_partial_hold_receipt_not_success(scene, code):
    source, settings, sha, _, state, _ = scene
    calls = []

    def run(config, *args, **kwargs):
        calls.append(1)
        value = result(config, amount=40000)
        value.update(error_code=code, content=[], stop_reason=None)
        return value

    output = io.BytesIO()
    assert worker.serve(frames(body()), output, loader=lambda: (source, settings, sha), runner=run) == 0
    value = unpack(output.getvalue())[-1][1]
    assert value['metering'] == 'interrupted' and value['message'] is None
    assert value['error'] == 'native_prompt_timed_out' and value['cleanup_confirmed']
    cleanup, usage = trial_runtime.TrialRuntime._receipt(value)
    assert cleanup and usage == ledger.VerifiedInterruptedUsage(40009, True)
    assert calls == [1] and not list(state.glob('trial-*'))


@pytest.mark.parametrize('broken', ['cleanup', 'model', 'mismatch', 'generation', 'usage', 'error', 'unsent'])
def test_timeout_recovery_never_accepts_missing_identity_usage_or_cleanup(scene, broken):
    source, settings, sha, *_ = scene

    def run(config, *args, **kwargs):
        value = result(config)
        value.update(error_code='native_prompt_deadline_exceeded', content=[], stop_reason=None)
        if broken == 'cleanup':
            value['native_cleanup_confirmed'] = False
        if broken == 'model':
            value['accounting']['served_model_verified'] = False
        if broken == 'mismatch':
            value['accounting']['model_mismatch_detected'] = True
        if broken == 'generation':
            value['accounting']['unique_finalized_provider_ids'] = 0
        if broken == 'usage':
            value['accounting']['usage']['output_tokens'] = None
        if broken == 'error':
            value['error_code'] = 'unsupported_native_content'
        if broken == 'unsent':
            value['prompt_requests'] = 0
        return value

    output = io.BytesIO()
    worker.serve(frames(body()), output, loader=lambda: (source, settings, sha), runner=run)
    value = unpack(output.getvalue())[-1][1]
    assert value['metering'] == 'unconfirmed'
    assert trial_runtime.TrialRuntime._receipt(value)[1] is None


def test_clean_timeout_keeps_larger_known_hold_allows_next_turn_and_never_retries(trial):
    calls = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            value = super().perform(raw, authorized)
            calls.append(1)
            trial.now[0] += 300 if len(calls) == 1 else 12
            if len(calls) == 1:
                value = receipt(40000, metering='interrupted', message=None,
                                error='native_prompt_timed_out')
            return value

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    with pytest.raises(trial_runtime.TrialUnavailable) as error:
        trial.runtime.turn(token, body())
    assert (error.value.code, error.value.status) == ('trial_turn_timed_out', 504)
    value = trial.runtime.allowance(trial.alice)
    assert not value['paused'] and value['reported_tokens'] == 0
    assert value['reserved_tokens'] == value['partial_reported_tokens'] == 40009
    assert value['remaining_runtime_seconds'] == 11700 and calls == [1]
    with trial.runtime._ledger() as book:
        row = book.connection.execute('SELECT reason FROM devin_trial_unmeasured').fetchone()
        assert row[0] == 'interrupted'
        assert book.connection.execute("SELECT COUNT(*) FROM devin_trial_sessions WHERE state!='closed'").fetchone()[0] == 0
    assert trial.runtime.turn(token, body())[0]['content']
    value = trial.runtime.allowance(trial.alice)
    assert calls == [1, 1] and value['remaining_runtime_seconds'] == 11688
    assert value['reserved_tokens'] == 40009 and value['reported_tokens'] == 19


@pytest.mark.parametrize('change', [
    {'cleanup_confirmed': False}, {'error': None}, {'message': {}},
    {'metering': 'complete'}, {'usage': {'input_tokens': 1}},
])
def test_interrupted_wire_receipt_cannot_be_promoted_to_complete_usage(change):
    value = receipt(metering='interrupted', message=None, error='native_prompt_timed_out')
    value.update(change)
    assert trial_runtime.TrialRuntime._receipt(value)[1] is None


def test_migrate_previous_time_and_partial_schema_preserves_pause_rows_and_holds(monkeypatch):
    db = sqlite3.connect(':memory:')
    account = ledger.VerifiedAccount('alice', True)
    now = 20000 * 86400 + 100
    with monkeypatch.context() as old:
        old.setattr(ledger, '_SESSION_SCHEMA', ledger._PREVIOUS_SESSION_SCHEMA)
        old.setattr(ledger, '_UNMEASURED_SCHEMA', ledger._LEGACY_UNMEASURED_SCHEMA)
        book = ledger.TrialLedger(db, enabled=True, clock=lambda: now)
        lease = book.admit(account).lease
        step = book.start_step(account, lease, request=b'{}', seconds=160).step
        book.finish_step(account, lease, step, cleanup_confirmed=True, usage=None)
        # The old seven-column cache-only table is preserved, not reinterpreted.
        db.execute("INSERT INTO devin_trial_unmeasured VALUES(?,?,?,?,?,?,?)",
                   ('a'*64, 'b'*64, 'alice', 20000, 20000, 123, now))
        db.commit()
        before = {t: list(db.execute('SELECT * FROM '+t)) for t in ledger._TABLES}
    book = ledger.TrialLedger(db, enabled=True, clock=lambda: now)
    for name, rows in before.items():
        expected = [(*r, 'cache_read_missing') for r in rows] if name == 'devin_trial_unmeasured' else rows
        assert list(db.execute('SELECT * FROM '+name)) == expected
    schema = list(db.execute('SELECT * FROM sqlite_master ORDER BY name'))
    ledger.TrialLedger(db, enabled=True, clock=lambda: now)
    assert list(db.execute('SELECT * FROM sqlite_master ORDER BY name')) == schema
    assert book.allowance(account).paused


def test_fresh_same_credential_authority_renews_during_active_turn(provision_scene):
    s = provision_scene
    s.run()
    credential = s.home / '.local/share/devin/credentials.toml'
    identity = credential.stat().st_ino, credential.stat().st_mtime_ns
    current = copy.deepcopy(s.machine)
    observation = current['devin_observations'][s.source.pw_name]
    observation['checked_at'] = observation['report']['observed_at'] = 1010
    write(Path(s.reg['state_path']), current)
    descriptor = os.open(s.state / '.trial.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (s.state / 'trial-active').mkdir(mode=0o700)
        assert s.run()['outcome'] == 'refreshed'
        assert json.loads(provision.GRANT.read_bytes())['observed_at'] == 1010
        assert (credential.stat().st_ino, credential.stat().st_mtime_ns) == identity
        write(Path(s.reg['state_path']), dict(current, devin_observations={}))
        assert s.run()['outcome'] == 'revoked' and not provision.GRANT.exists()
    finally:
        os.close(descriptor)


def test_native_search_hook_needs_root_selected_capability_and_keeps_other_tools_blocked(private):
    _, config, write_config, _, _, hook = private
    assert hook('web_search', {'query': 'Anthropic news'})[0] == 2
    assert hook('web_search', {'query': 'Anthropic news'}, native_web_search=True)[0] == 2
    write_config('config.json', dict(config, native_web_search=True))
    assert hook('web_search', {'query': 'Anthropic news'}) == (0, {'decision': 'approve'})
    for name in ['exec', 'read', 'webfetch', 'run_subagent']:
        assert hook(name, {'query': 'Anthropic news'})[0] == 2
    for arguments in [{}, {'query': ''}, {'query': 4}, {'query': 'x'*8193}]:
        assert hook('web_search', arguments)[0] == 2


def test_native_search_requires_separate_explicit_root_policy(policy):
    policy.settings['disabled_tools'].remove('web_search')
    with pytest.raises(native.NativeError, match='containment'):
        policy.load()
    policy.value.update(native_web_search=True, timeout_s=300)
    policy.settings['permissions']['allow'].append('web_search')
    assert policy.load().data['native_web_search'] is True
    policy.settings['disabled_tools'].remove('exec')
    with pytest.raises(native.NativeError, match='containment'):
        policy.load()


def test_native_search_progress_never_becomes_answer_text():
    notices = [
        {'sessionId': 'owned', 'update': {'sessionUpdate': 'tool_call', 'content': 'UNTRUSTED TOOL DATA'}},
        {'sessionId': 'owned', 'update': {'sessionUpdate': 'tool_call_update'}},
        {'sessionId': 'owned', 'update': {'sessionUpdate': 'agent_message_chunk',
                                         'content': {'type': 'text', 'text': 'Answer with sources'}}},
    ]
    assert native._final_text(notices, 'owned', {'stopReason': 'end_turn'}, native_web_search=True) == [
        {'type': 'text', 'text': 'Answer with sources'}]
    with pytest.raises(native.NativeError):
        native._final_text(notices, 'owned', {'stopReason': 'end_turn'})
    with pytest.raises(native.NativeError, match='session'):
        native._final_text(notices, 'foreign', {'stopReason': 'end_turn'}, native_web_search=True)


def test_normal_conversation_larger_than_old_trial_limit_reaches_worker_once(trial):
    calls = []

    class Worker(FakeWorker):
        def perform(self, raw, authorized):
            calls.append(len(raw))
            return super().perform(raw, authorized)

    trial.runtime.transport = Worker
    token = trial.runtime.create_key(trial.alice)
    raw = body(messages=[{'role': 'user', 'content': 'Synthetic file context. ' * 20000}])
    assert 256 * 1024 < len(raw) < ledger.MAX_REQUEST_BYTES
    assert trial.runtime.turn(token, raw)[0]['content']
    assert len(calls) == 1 and not trial.runtime.allowance(trial.alice)['paused']


def test_native_context_preflight_reserves_optional_search_prefix():
    value = {'messages': [{'role': 'user', 'content': 'synthetic' * 40000}]}
    raw = native.native_context(value, limit=native.TRIAL_MAX_PROMPT)
    limit = len(raw.encode()) + len(native.NATIVE_WEB_CONTEXT.encode())
    assert native.native_context(value, limit=limit) == raw
    with pytest.raises(native.NativeError, match='native_context_limit'):
        native.native_context(value, limit=limit - 1)
    with pytest.raises(native.NativeError, match='native_context_limit'):
        native.native_context(value)
