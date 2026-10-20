"""Routine partial actions use synthetic transports and disposable server state."""

import base64
import hashlib
import json
import shutil
import subprocess
import time

import pytest

from ccfleetd import render
from ccfleetd.api import HTML_HEADERS, partial_account_action
from tests.test_usersite import claimed, machine, report, site  # noqa: F401


@pytest.mark.parametrize("path", ["/account/outage-emails",
    "/account/slots/synthetic/quota", "/account/slots/synthetic/rename",
    "/account/slots/synthetic/update", "/account/slots/synthetic/stable"])
def test_server_partial_account_routes_are_explicit(path):
    assert partial_account_action(path)


@pytest.mark.parametrize("path", ["/account/claim", "/account/purchase", "/account/renew",
    "/account/topup", "/account/subscriptions/cancel", "/account/slots/synthetic/release",
    "/account/slots/synthetic/signin", "/account/slots/synthetic/switch",
    "/account/slots/synthetic/cli", "/account/gateway/create", "/auth/signout",
    "/actions/slot/synthetic/quota", "/account/slots/synthetic/quota/extra"])
def test_server_partial_policy_excludes_destructive_auth_and_payment_routes(path):
    assert not partial_account_action(path)


def test_panel_script_has_exact_csp_hash():
    digest = base64.b64encode(hashlib.sha256(render.PANEL_ACTION_JS.encode()).digest()).decode()
    assert render.PANEL_ACTION_CSP == "'sha256-" + digest + "'"
    assert render.PANEL_ACTION_CSP in HTML_HEADERS["Content-Security-Policy"]
    assert render.PANEL_ACTION_TAG == "<script>" + render.PANEL_ACTION_JS + "</script>"


def test_partial_quota_post_keeps_real_ownership_and_csrf_checks(site):  # noqa: F811
    store, sign_in, _ = site
    machine(store, users=("slot01",))
    owner = sign_in(quota=1)
    slot = claimed(store, owner)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
        "credentials": {"logged_in": True}}])
    path = f"/account/slots/{slot['id']}/quota"
    headers = {"X-Fleet-Partial": "1", "Accept": "application/json"}
    refused = owner.call("POST", path, form={"csrf": "0" * 64}, headers=headers)
    assert refused.status == 403 and store.get_slot(slot["id"])["quota_wanted_at"] is None
    stranger = sign_in("other-subject", "other@example.com", quota=1)
    missing = stranger.call("POST", path, form={"csrf": stranger.token()}, headers=headers)
    assert missing.status == 404 and store.get_slot(slot["id"])["quota_wanted_at"] is None
    accepted = owner.call("POST", path, form={"csrf": owner.token()}, headers=headers)
    assert accepted.status == 200
    body = json.loads(accepted.body)
    assert set(body) == {"location"} and body["location"].startswith("/account?note=reading")
    assert store.get_slot(slot["id"])["quota_wanted_at"] <= time.time()
    assert accepted.getheader("Cache-Control") == "no-store"


HARNESS = r'''
const vm=require('vm'),fs=require('fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{const calls=[],listeners={},timers=new Map(),messages=[],updates=[];
let serial=0,prevented=0;const button={disabled:false};
const input={value:'KEEP PRIVATE INPUT',placeholder:'old-placeholder'};
const details={open:true},window={scrollY:777};
function area(id,editable=false){return {id,feedback:null,childNodes:['synthetic child'],
 querySelector(selector){if(selector==='[data-fleet-feedback]')return this.feedback;
 return editable?{}:null;},
 replaceChildren(...children){updates.push(id);},
 appendChild(node){this.feedback=node;},
 setAttribute(){}};}
const oldName=area('name'),newName=area('new-name'),oldQuota=area('quota');
const oldVersion=area('version'),newQuota=area('new-quota'),newVersion=area('new-version');
const oldEmails=area('outage-emails'),newEmails=area('new-emails');
const readonly=area('readonly',p.mode==='status-editable');
const kind=p.kind||'quota',action=p.action||'/zh/account/slots/synthetic/quota';
const form={dataset:p.mode==='no-optin'?{}:{fleetAction:kind},
 action:new URL(action,'https://fleet.test').href,method:'post',feedback:null,
 fields:[['csrf','synthetic-csrf'],['name',input.value]],
 querySelectorAll:()=>[button],querySelector(selector){
 if(selector==='[data-fleet-feedback]')return this.feedback;
 if(selector==='input[name="name"]')return input;return null;},
 appendChild(node){this.feedback=node;},closest(selector){
 return selector==='.slot'?card:selector==='[data-fleet-quota]'?oldQuota:null;}};
const card={id:'slot-synthetic',querySelector(selector){return selector==='.usage-bars'?oldQuota:
 selector==='.row-line.cc'?oldVersion:selector==='.slot-display-name'?oldName:null;}};
const nextCard={querySelector(selector){return selector==='.usage-bars'?newQuota:
 selector==='.row-line.cc'?newVersion:selector==='.slot-display-name'?newName:
 selector==='input[name="name"]'?{placeholder:'new-placeholder'}:null;}};
const fresh={getElementById(id){return id===card.id?nextCard:
 id==='outage-emails'?newEmails:id==='quota'?newQuota:id==='readonly'?area('new-readonly'):null;},
 querySelector:()=>({textContent:'Saved synthetic action.'})};
const link=area('status-link');link.href='https://fleet.test/zh/account';
link.dataset={fleetStatus:'account'};link.closest=()=>link;
const context={URL,URLSearchParams,Set,Array,Object,JSON,Error,Promise,AbortController,
 Event:class{constructor(type){this.type=type;}},location:{href:'https://fleet.test/zh/account',
 origin:'https://fleet.test'},window,
 FormData:function(f){return f.fields;},
 document:{documentElement:{lang:'en'},addEventListener(name,fn){listeners[name]=fn;},
 dispatchEvent(){},importNode:n=>n,createElement:()=>({dataset:{},setAttribute(){}}),
 getElementById:id=>id==='outage-emails'?oldEmails:null,
 querySelectorAll:()=>[readonly]},
 setTimeout(fn){const id=++serial;timers.set(id,fn);return id;},
 clearTimeout:id=>timers.delete(id),
 DOMParser:class{parseFromString(){return fresh;}},
 fetch(url,options){const method=options.method||'GET';calls.push({url,method,
 credentials:options.credentials,redirect:options.redirect,cache:options.cache,
 header:options.headers&&options.headers['X-Fleet-Partial'],
 body:options.body&&options.body.toString()});
 if(p.mode==='network')return Promise.reject(Error('PRIVATE_NETWORK_DETAIL'));
 if(p.mode==='get-fails'&&method==='GET')return Promise.reject(Error('PRIVATE_GET_DETAIL'));
 const bad=p.mode==='forbidden',location=p.mode==='foreign'?'https://other.test/account':
 p.mode==='destination'?'/account/purchase':kind==='admin-quota'?'/admin#slots':'/zh/account?note=reading';
 return Promise.resolve({ok:!bad,status:bad?403:200,
 headers:{get:()=>method==='POST'?'application/json':'text/html'},text:()=>{
 if(p.mode==='body-hangs')return new Promise((resolve,reject)=>options.signal.addEventListener(
 'abort',()=>reject(Error('PRIVATE_ABORT_DETAIL'))));
 return Promise.resolve(method==='POST'?(p.mode==='bad-json'?'PRIVATE_BAD_JSON':
 p.mode==='extra-json'?JSON.stringify({location,raw:'PRIVATE_FIELD'}):JSON.stringify({location})):'HTML');}});}};
vm.runInNewContext(p.script,context);
const event={target:form,preventDefault(){prevented++;}};
let first=p.mode.startsWith('status')?listeners.click({target:link,
 preventDefault(){prevented++;}}):listeners.submit(event);
if(p.mode==='double')await listeners.submit(event);
if(p.mode==='body-hangs'){await new Promise(setImmediate);for(const fn of timers.values())fn();}
await first;
for(const node of [form,link])if(node.feedback)messages.push(node.feedback.textContent);
console.log(JSON.stringify({calls,prevented,messages,updates,input,details,scrollY:window.scrollY,
 disabled:button.disabled,timers:timers.size}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run_js(mode, *, kind="quota", action="/zh/account/slots/synthetic/quota"):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is required for pure JS fakes")
    result = subprocess.run([executable, "-e", HARNESS], input=json.dumps({
        "script": render.PANEL_ACTION_JS, "mode": mode, "kind": kind, "action": action}),
        text=True, capture_output=True, check=True, timeout=5)
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["success", "double", "forbidden", "network", "bad-json",
    "extra-json", "foreign", "destination", "get-fails", "body-hangs"])
def test_real_script_posts_at_most_once_and_preserves_dirty_ui(mode):
    result = run_js(mode)
    assert result["input"]["value"] == "KEEP PRIVATE INPUT"
    assert result["details"]["open"] is True and result["scrollY"] == 777
    assert result["disabled"] is False and result["timers"] == 0
    assert [call["method"] for call in result["calls"]].count("POST") == 1
    assert len(result["calls"]) <= 2
    assert all(call["credentials"] == "same-origin" and call["redirect"] == "error"
               and call["cache"] == "no-store" for call in result["calls"])
    assert result["calls"][0]["header"] == "1"
    assert "csrf=synthetic-csrf" in result["calls"][0]["body"]
    assert "PRIVATE_" not in " ".join(result["messages"])
    if mode in {"success", "double"}:
        assert result["updates"] == ["quota"] and len(result["calls"]) == 2
    else:
        assert result["updates"] == []
        assert ("refused" if mode == "forbidden" else "not confirmed") in result["messages"][0]


@pytest.mark.parametrize("kind,action", [("quota", "/account/slots/synthetic/release"),
    ("quota", "/account/purchase"), ("rename", "/account/slots/synthetic/switch"),
    ("emails", "/auth/signout"), ("unknown", "/account/slots/synthetic/quota"),
    ("quota", "https://other.test/account/slots/synthetic/quota")])
def test_real_script_does_not_intercept_nonroutine_or_foreign_forms(kind, action):
    result = run_js("success", kind=kind, action=action)
    assert result["calls"] == [] and result["prevented"] == 0


def test_form_without_explicit_optin_is_untouched():
    result = run_js("no-optin")
    assert result["calls"] == [] and result["prevented"] == 0


def test_rename_changes_placeholder_without_overwriting_dirty_value():
    result = run_js("success", kind="rename", action="/account/slots/synthetic/rename")
    assert result["updates"] == ["name"]
    assert result["input"] == {"value": "KEEP PRIVATE INPUT", "placeholder": "new-placeholder"}


@pytest.mark.parametrize("mode", ["status", "status-editable"])
def test_status_is_get_only_and_refuses_editable_regions(mode):
    result = run_js(mode)
    assert [call["method"] for call in result["calls"]] == ["GET"]
    assert result["updates"] == ([] if mode == "status-editable" else ["readonly"])
    assert result["input"]["value"] == "KEEP PRIVATE INPUT"
    assert result["details"]["open"] is True and result["scrollY"] == 777
