"""Admin quota must distinguish observed totals, missing data and asynchronous results."""

import base64
import hashlib
import json
import shutil
import subprocess

import pytest

from ccfleet_agent import relay_metrics as metrics
from ccfleetd import bilingual, render
from ccfleetd.api import HTML_HEADERS
from tests.test_html_document_preservation import Document

NOW = 1_800_000_000.0


def visible(source, language='en'):
    return Document(bilingual.project_html(source, language, '/admin')).text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_stale_admin_quota_is_historical_and_missing_window_is_unknown(language):
    row = {'slot_id': 'synthetic', 'quota': {'checked_at': NOW - 7127,
                                            'week': {'used_pct': 8}}}
    source = render._quota_html(row, NOW, 'synthetic-csrf')
    text = visible(source, language)
    assert ('Historical reading' if language == 'en' else '历史读数') in text
    assert ('current quota unknown' if language == 'en' else '当前额度未知') in text
    assert '8%' in text and '0%' not in text
    assert 'quota-historical' in source
    assert ('does not mean quota remains' if language == 'en' else '不代表还有剩余额度') in text


def test_acknowledged_failure_stops_polling_and_explains_retry_without_new_values():
    wanted = NOW - 20
    row = {'slot_id': 'synthetic', 'quota_wanted_at': wanted, 'quota': {
        'checked_at': NOW - 7127, 'week': {'used_pct': 8}, 'refresh': {
            'requested_at': wanted, 'attempted_at': NOW - 2, 'outcome': 'native_timeout'}}}
    source = render._quota_html(row, NOW, 'synthetic-csrf')
    assert 'No new quota reading was received' in visible(source)
    assert 'data-admin-quota-pending' not in source
    assert 'data-fleet-action="admin-quota"' in source
    assert '8%' in source and '100%' not in source


def test_request_polls_only_until_a_real_answer_and_does_not_restamp_cached_reading():
    wanted = NOW - 20
    row = {'slot_id': 'synthetic', 'quota_wanted_at': wanted, 'quota': {
        'checked_at': NOW - 90, 'week': {'used_pct': 8}}}
    assert 'data-admin-quota-pending="synthetic"' in render._quota_html(row, NOW, 'csrf')
    row['quota']['refresh'] = {'requested_at': wanted, 'attempted_at': NOW - 2,
                               'outcome': 'quota_read'}
    source = render._quota_html(row, NOW, 'csrf')
    assert 'latest available native reading' in visible(source)
    assert 'read 1m ago' in visible(source)
    assert 'data-admin-quota-pending' not in source


def test_missing_transcript_and_gateway_totals_are_unknown_including_first_read_slot():
    source = render._usage_html([{'id': 'synthetic', 'slot_id': 'synthetic',
        'owner': 'operator', 'slot_state': 'active', 'usage': {}, 'quota': {}}], NOW, 'csrf')
    text = visible(source)
    assert 'Transcript usage: not reported; total unknown' in text
    assert 'Gateway response usage: not reported; total unknown' in text
    assert '0 transcript tokens' not in text
    assert 'Refresh' in text


def test_gateway_usage_is_counted_separately_with_real_sample_coverage(tmp_path):
    home = tmp_path / 'synthetic-slot'
    home.mkdir()
    bound = '0123456789abcdef'
    metrics.record(home, bound, 'success', {}, NOW, usage_eligible=True,
                   usage={'input_tokens': 20, 'output_tokens': 9, 'cache_read_input_tokens': 8})
    metrics.record(home, bound, 'success', {}, NOW, usage_eligible=True)
    row = {'id': 'synthetic', 'owner': 'operator', 'last_seen_ts': NOW,
           'usage': {'total_tokens': 0, 'sessions': 0, 'window_days': 7},
           'relay': metrics.report(home, bound, NOW)}
    text = visible(render._usage_html([row], NOW))
    assert '20 input' in text and '9 output tokens' in text
    assert '1 complete samples / 2 eligible responses' in text
    assert 'Cache-write input: unknown' in text
    assert 'Zero transcript tokens does not mean zero account usage' in text
    row['usage_since'] = NOW + 1
    assert 'Gateway response usage: not reported' in visible(render._usage_html([row], NOW))


def test_admin_poll_script_is_allowed_only_by_its_exact_hash():
    digest = base64.b64encode(hashlib.sha256(render.ADMIN_QUOTA_POLL_JS.encode()).digest()).decode()
    assert render.ADMIN_QUOTA_POLL_CSP == "'sha256-" + digest + "'"
    assert render.ADMIN_QUOTA_POLL_CSP in HTML_HEADERS['Content-Security-Policy']


HARNESS = r'''
const vm=require('vm'),fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{let now=0,jobs=[],next=0,calls=[],replaced=0,active=p.mode!=='idle';
const form={value:'KEEP PRIVATE INPUT'},details={open:true};
const button={disabled:true};
const area={id:'admin-quota-synthetic',querySelector:()=>button,replaceChildren(){replaced++;active=p.mode==='pending';}};
const node={textContent:'reading',closest:()=>area,removeAttribute(){active=false;}};
const context={Date:{now:()=>now},AbortController,Promise,Array,Error,
 Event:class{constructor(type){this.type=type;}},location:{pathname:'/zh/admin'},document:{documentElement:{lang:'zh-Hans'},
 addEventListener(){},dispatchEvent(){},querySelectorAll:()=>active?[node]:[],importNode:x=>x},
 setTimeout(f,ms){const id=++next;jobs.push({id,at:now+ms,f});return id;},
 clearTimeout(id){jobs=jobs.filter(j=>j.id!==id);},
 fetch(url,opts){calls.push({url,method:opts.method||'GET',credentials:opts.credentials,
 cache:opts.cache,redirect:opts.redirect});
 if(p.mode==='network')return Promise.reject(Error('PRIVATE_FAILURE'));
 if(p.mode==='hang')return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',
 ()=>reject(Error('PRIVATE_ABORT'))));
 return Promise.resolve({ok:p.mode!=='http',text:()=>Promise.resolve(
 p.mode==='large'?'x'.repeat(2097153):'synthetic quota HTML')});},
 DOMParser:class{parseFromString(){return {getElementById(id){
 if(id!==area.id)throw Error('identity');return p.mode==='missing'?null:{
 hasAttribute:()=>true,querySelector:()=>p.mode==='editable'?{}:null,
 childNodes:['updated quota']};}};}}};
vm.runInNewContext(p.script,context);
for(let i=0;i<100&&jobs.length;i++){
 jobs.sort((a,b)=>a.at-b.at);const job=jobs.shift();now=job.at;job.f();
 await new Promise(setImmediate);await new Promise(setImmediate);
}
console.log(JSON.stringify({calls,replaced,active,text:node.textContent,form,details,button,now,jobs:jobs.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for script tests')
@pytest.mark.parametrize('mode', ['idle', 'success', 'pending', 'network', 'hang',
                                  'http', 'missing', 'large', 'editable'])
def test_admin_poll_is_bounded_get_only_and_keeps_other_forms(mode):
    result = subprocess.run([shutil.which('node'), '-e', HARNESS], input=json.dumps({
        'script': render.ADMIN_QUOTA_POLL_JS, 'mode': mode}), text=True,
        capture_output=True, check=True, timeout=5)
    found = json.loads(result.stdout)
    assert found['form']['value'] == 'KEEP PRIVATE INPUT' and found['details']['open'] is True
    assert found['active'] is False and found['jobs'] == 0
    assert len(found['calls']) <= 30 and found['now'] <= 310000
    assert all(call == {'url': '/zh/admin', 'method': 'GET', 'credentials': 'same-origin',
                       'cache': 'no-store', 'redirect': 'error'} for call in found['calls'])
    assert 'PRIVATE_' not in found['text']
    if mode == 'idle':
        assert found['calls'] == [] and found['replaced'] == 0
    elif mode == 'success':
        assert found['replaced'] == 1 and len(found['calls']) == 1
    else:
        assert '未收到新的额度读数' in found['text']
        assert found['button']['disabled'] is False
        if mode != 'pending':
            assert found['replaced'] == 0


def test_quota_from_before_current_claim_or_account_switch_is_not_attributed_to_holder():
    row = {'slot_id': 'synthetic', 'usage_since': NOW - 10, 'quota': {
        'checked_at': NOW - 100, 'week': {'used_pct': 8}}}
    source = render._quota_html(row, NOW, 'csrf')
    assert '8%' not in source
    assert 'Waiting for a quota reading for the current account' in visible(source)
    assert 'Current quota is unavailable' in visible(source)
    # A genuinely new observation restores the meter.
    row['quota']['checked_at'] = NOW - 2
    assert '8%' in render._quota_html(row, NOW, 'csrf')


def test_reset_times_localize_new_readings_without_formatting_old_times_twice():
    executable = shutil.which('node')
    if executable is None:
        pytest.skip('Node is required for script tests')
    script = r"""
const vm=require('vm'),fs=require('fs'),script=JSON.parse(fs.readFileSync(0,'utf8'));
const listeners={},nodes=[];
function node(){return {textContent:'in 2h',dateTime:'valid',formatted:false,
 hasAttribute(){return this.formatted;},setAttribute(){this.formatted=true;}};}
nodes.push(node());
class Date {constructor(){} toLocaleString(){return 'viewer time';}valueOf(){return 0;}}
Date.now=()=>0;
vm.runInNewContext(script,{Date,isNaN:()=>false,document:{
 querySelectorAll:()=>nodes,addEventListener:(event,fn)=>listeners[event]=fn}});
nodes.push(node());listeners['ccfleet:quota']();listeners['ccfleet:quota']();
console.log(JSON.stringify(nodes.map(n=>n.textContent)));
"""
    result = subprocess.run([executable, '-e', script], input=json.dumps(render.LOCAL_TIMES_JS),
                            capture_output=True, text=True, check=True, timeout=5)
    assert json.loads(result.stdout) == ['viewer time (in 2h)', 'viewer time (in 2h)']
