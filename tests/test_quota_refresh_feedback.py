"""Quota polling is bounded, GET-only and cannot erase connection forms."""

import base64
import hashlib
import json
import shutil
import subprocess

import pytest

from ccfleetd import bilingual, render, usersite
from ccfleetd.api import HTML_HEADERS
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document

NOW = 1_800_000_000.0


@pytest.mark.parametrize("language", ["en", "zh"])
def test_old_low_quota_is_historical_not_evidence_of_remaining_quota(language):
    report = {"credentials": {"logged_in": True},
              "quota": {"checked_at": NOW - 7127, "session": {"used_pct": 14},
                        "week": {"used_pct": 8}}}
    source = usersite._in_use(report, NOW)
    text = Document(bilingual.project_html(source, language, "/account")).text
    assert "14%" in text and "8%" in text
    assert ("Historical reading" if language == "en" else "历史读数") in text
    assert ("current quota unknown" if language == "en" else "当前额度未知") in text


def test_timeout_is_explicit_and_retry_does_not_claim_the_provider_read_failed():
    source = usersite._quota_refresh({"id": "test-slot", "quota_wanted_at": NOW - 391},
        {"quota": {"checked_at": NOW - 7127}}, "csrf", NOW)
    assert "No new quota reading was received" in source
    assert "machine may be busy" in source and "does not mean quota remains" in source
    assert 'action="/account/slots/test-slot/quota"' in source
    assert "data-quota-slot" not in source


@pytest.mark.parametrize("wanted", [True, float("inf"), float("nan"), NOW + 1, "now"])
def test_bad_or_future_request_time_never_starts_polling(wanted):
    assert render.quota_reading(wanted, NOW - 10, NOW) is False


def test_missing_five_hour_window_remains_visible_as_unknown():
    text = Document(usersite._in_use({"credentials": {"logged_in": True},
        "quota": {"checked_at": NOW, "week": {"used_pct": 8}}}, NOW)).text
    assert "5-hour session" in text and "Current quota is unavailable" in text
    assert "does not mean quota remains" in text and "8%" in text


def test_poll_script_has_only_exact_hash_and_same_origin_connect_permission():
    digest = base64.b64encode(hashlib.sha256(usersite.QUOTA_POLL_JS.encode()).digest()).decode()
    assert usersite.QUOTA_POLL_CSP == "'sha256-" + digest + "'"
    policy = HTML_HEADERS["Content-Security-Policy"]
    assert usersite.QUOTA_POLL_CSP in policy and "connect-src 'self'" in policy
    assert "unsafe-inline" not in policy.split("script-src", 1)[1]
    assert usersite.QUOTA_POLL_TAG in usersite._shell("your slots", "<p>test</p>")
    assert usersite.QUOTA_POLL_TAG not in usersite.privacy_page(Config())


HARNESS = r'''
const vm=require('vm'),fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{let now=0,jobs=[],next=0,calls=[],replaced=0,links=[],active=p.mode!=='idle';
const form={value:'KEEP PRIVATE INPUT'},details={open:true};
const current={replaceChildren(...children){replaced++;active=p.mode==='pending';}};
const n={dataset:{quotaSlot:'synthetic'},textContent:'waiting',append(...values){
links.push(...values.filter(v=>typeof v==='object'));},
 removeAttribute(name){if(name!=='data-quota-slot')throw Error('attribute');active=false;},
 closest(selector){if(selector!=='.usage-bars')throw Error('outside');return current;}};
const context={Date:{now:()=>now},AbortController,Promise,Array,Error,
 location:{pathname:'/zh/account'},
 document:{documentElement:{lang:p.language},createElement:t=>({tagName:t}),querySelectorAll(s){
 if(s!=='[data-quota-slot]')throw Error('outside');return active?[n]:[];},importNode:x=>x},
 setTimeout(f,ms){const id=++next;jobs.push({id,at:now+ms,f});return id;},
 clearTimeout(id){jobs=jobs.filter(j=>j.id!==id);},
 fetch(url,opts){calls.push({url,method:opts.method||'GET',credentials:opts.credentials,
 cache:opts.cache,redirect:opts.redirect});
 if(p.mode==='network')return Promise.reject(Error('network'));
 if(p.mode==='hang')return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',
 ()=>reject(Error('aborted'))));
 return Promise.resolve({ok:p.mode!=='http',text:()=>Promise.resolve(
 p.mode==='large'?'x'.repeat(2097153):'synthetic quota HTML')});},
 DOMParser:class{parseFromString(){return {getElementById(id){
 if(id!=='slot-synthetic')throw Error('identity');
 return p.mode==='missing'?null:{querySelector(s){if(s!=='.usage-bars')throw Error('outside');
 return {childNodes:['updated quota']};}};}};}}};
vm.runInNewContext(p.script,context);
for(let i=0;i<100&&jobs.length;i++){
 jobs.sort((a,b)=>a.at-b.at);const job=jobs.shift();now=job.at;job.f();
 await new Promise(setImmediate);await new Promise(setImmediate);
}
console.log(JSON.stringify({calls,replaced,active,text:n.textContent,links,form,details,now,jobs:jobs.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for pure JS fakes")
@pytest.mark.parametrize("mode", ["idle", "success", "pending", "network", "hang",
                                  "http", "missing", "large"])
def test_real_poll_script_is_bounded_get_only_and_replaces_only_quota_area(mode):
    result = subprocess.run([shutil.which("node"), "-e", HARNESS], input=json.dumps({
        "script": usersite.QUOTA_POLL_JS, "mode": mode, "language": "zh-Hans"}),
        capture_output=True, text=True, timeout=5, check=True)
    found = json.loads(result.stdout)
    assert found["form"]["value"] == "KEEP PRIVATE INPUT" and found["details"]["open"] is True
    assert found["active"] is False and found["jobs"] == 0
    assert len(found["calls"]) <= 30 and found["now"] <= 310000
    assert all(call == {"url": "/zh/account", "method": "GET", "credentials": "same-origin",
                        "cache": "no-store", "redirect": "error"} for call in found["calls"])
    if mode == "idle":
        assert found["calls"] == [] and found["replaced"] == 0
    elif mode == "success":
        assert len(found["calls"]) == found["replaced"] == 1
    else:
        assert "暂未收到新的额度读数" in found["text"]
        assert found["links"] == [{"tagName": "a", "href": "/zh/account",
                                   "textContent": "查看状态（重新载入页面）"}]
