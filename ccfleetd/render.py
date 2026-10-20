"""Server-rendered fleet dashboard. Every value is HTML-escaped; no scripts."""

from __future__ import annotations

import base64
import hashlib
import math
from collections.abc import Mapping, Sequence
from html import escape
from shlex import quote as shq
from typing import Any, Optional

from . import bilingual, compatibility, plans, resets
from . import names as slotnames
from . import slots as slotstates
from .admin_locale import attributes as admin_attributes
from .admin_locale import label as admin_label
from .bilingual import bi
from .bilingual import label as bi_label
from .config import Config
from .credential_health import access_expiry
from .desired import is_channel, is_login_url
from .heartbeat import relay_report

LEVEL_ORDER = {"ok": 0, "warn": 1, "critical": 2}

#: Says a reset time in the viewer's own time zone. The page carries the instant
#: (<time datetime> from resets.iso) and, as text, how long until then, which
#: needs no zone at all and is what shows if this does not run. The one script
#: the site runs, allowed by its hash (LOCAL_TIMES_CSP) and nothing else.
LOCAL_TIMES_JS = (
    '(function(){function update(){'
    'document.querySelectorAll("time[data-local]").forEach(function(t){'
    'if(t.hasAttribute("data-local-formatted"))return;'
    'var d=new Date(t.dateTime);if(isNaN(d))return;'
    'var o={hour:"numeric",minute:"2-digit",timeZoneName:"short"};'
    'if(d-Date.now()>864e5)o.weekday="short";'
    't.textContent=d.toLocaleString(undefined,o)+" ("+t.textContent+")";'
    't.setAttribute("data-local-formatted","");});}'
    'document.addEventListener("ccfleet:quota",update);update();})();')
LOCAL_TIMES_TAG = f"<script>{LOCAL_TIMES_JS}</script>"
LOCAL_TIMES_CSP = ("'sha256-" + base64.b64encode(
    hashlib.sha256(LOCAL_TIMES_JS.encode("utf-8")).digest()).decode("ascii") + "'")

# Only explicitly opted-in, routine controls use this enhancement. A POST gets
# one closed redirect envelope; it is never retried or followed automatically.
PANEL_ACTION_JS = r'''(function(){
var busy=new Set();
function plain(path){return path.replace(/^\/(?:en|zh)(?=\/)/,'');}
function allowed(kind,path){path=plain(path);return (
kind==='emails'&&path==='/account/outage-emails'||
kind==='quota'&&/^\/account\/slots\/[^/]+\/quota$/.test(path)||
kind==='rename'&&/^\/account\/slots\/[^/]+\/rename$/.test(path)||
kind==='version'&&/^\/account\/slots\/[^/]+\/(?:update|stable)$/.test(path)||
kind==='admin-quota'&&/^\/actions\/slot\/[^/]+\/quota$/.test(path));}
function same(value){var url=new URL(value,location.href);
if(url.origin!==location.origin)throw Error('origin');return url;}
function safe(node){return node&&!node.querySelector(
'input:not([type="hidden"]),textarea,select,[contenteditable]');}
function replace(old,fresh){if(!safe(old)||!safe(fresh))throw Error('editable');
old.replaceChildren(...Array.from(fresh.childNodes).map(function(n){
return document.importNode(n,true);}));}
function message(node,text){var status=node.querySelector('[data-fleet-feedback]');
if(!status){status=document.createElement('span');status.dataset.fleetFeedback='';
status.className='small muted';status.setAttribute('role','status');
status.setAttribute('aria-live','polite');node.appendChild(status);}status.textContent=text;}
function words(en,zh){return document.documentElement.lang.toLowerCase().startsWith('zh')?zh:en;}
async function request(url,options){var controller=new AbortController();
var timer=setTimeout(function(){controller.abort();},10000);
try{var r=await fetch(url,Object.assign({credentials:'same-origin',cache:'no-store',
redirect:'error',signal:controller.signal},options));
if(!r.ok)throw Error(r.status===403?'forbidden':'response');
var text=await r.text();if(text.length>2097152)throw Error('size');
return {text:text,type:r.headers.get('Content-Type')||''};
}finally{clearTimeout(timer);}}
async function page(url){var r=await request(url,{});
return new DOMParser().parseFromString(r.text,'text/html');}
function refresh(current,fresh){Array.from(current.querySelectorAll('[data-fleet-readonly]'))
.slice(0,256).forEach(function(n){var next=fresh.getElementById(n.id);
if(next)replace(n,next);});}
function update(form,kind,fresh){var card=form.closest('.slot');
var next=card&&fresh.getElementById(card.id);
if(kind==='emails'){replace(document.getElementById('outage-emails'),
fresh.getElementById('outage-emails'));}
else if(kind==='admin-quota'){var area=form.closest('[data-fleet-quota]');
if(!area)throw Error('target');replace(area,fresh.getElementById(area.id));}
else if(kind==='rename'){if(!next)throw Error('target');
replace(card.querySelector('.slot-display-name'),next.querySelector('.slot-display-name'));
var input=form.querySelector('input[name="name"]'),other=next.querySelector('input[name="name"]');
if(input&&other)input.placeholder=other.placeholder;}
else{if(!next)throw Error('target');var selector=kind==='quota'?'.usage-bars':'.row-line.cc';
replace(card.querySelector(selector),next.querySelector(selector));}
document.dispatchEvent(new Event('ccfleet:quota'));}
document.addEventListener('submit',async function(event){var form=event.target;
if(!form||!form.dataset||!form.dataset.fleetAction)return;
var kind=form.dataset.fleetAction,url;
try{url=same(form.action);if(form.method.toLowerCase()!=='post'||!allowed(kind,url.pathname))return;}
catch(e){return;}event.preventDefault();if(busy.has(url.pathname))return;
busy.add(url.pathname);var buttons=Array.from(form.querySelectorAll(
'button[type="submit"],input[type="submit"]'));
var previous=buttons.map(function(b){return b.disabled;}),sent=false;
try{var body=new URLSearchParams(new FormData(form));
if(body.toString().length>8192)throw Error('size');
buttons.forEach(function(b){b.disabled=true;});message(form,words('Saving…','正在保存…'));
sent=true;var r=await request(url.href,{method:'POST',body:body,
headers:{'X-Fleet-Partial':'1','Accept':'application/json'}});
if(!r.type.startsWith('application/json'))throw Error('format');
var result=JSON.parse(r.text);if(!result||typeof result!=='object'||Array.isArray(result)||
Object.keys(result).length!==1||typeof result.location!=='string'||
result.location.length>1024)throw Error('format');
var target=same(result.location),expected=kind==='admin-quota'?'/admin':'/account';
if(plain(target.pathname)!==expected)throw Error('destination');
var fresh=await page(target.pathname+target.search);update(form,kind,fresh);
var banner=fresh.querySelector('.note-banner');
message(form,banner?banner.textContent:words('Saved. Status updated.','已保存，状态已更新。'));
}catch(error){message(form,error.message==='forbidden'?
words('This request was refused. Check your sign-in or permissions.',
'请求被拒绝，请检查登录状态或权限。'):
sent?words('Result not confirmed. Check status before trying again; no request was repeated.',
'尚未确认结果。请先查看状态再重试；本页没有重复提交请求。'):
words('Could not submit this form. Your inputs are kept.','无法提交，已保留输入内容。'));
}finally{buttons.forEach(function(b,i){b.disabled=previous[i];});busy.delete(url.pathname);}});
document.addEventListener('click',async function(event){
var link=event.target.closest&&event.target.closest('a[data-fleet-status]');
if(!link)return;var kind=link.dataset.fleetStatus,url;
try{url=same(link.href);if(!['account','admin'].includes(kind)||plain(url.pathname)!=='/'+kind)return;}
catch(e){return;}event.preventDefault();if(busy.has('status'))return;busy.add('status');
try{message(link,words('Checking…','正在检查…'));var fresh=await page(url.pathname);
refresh(document,fresh);message(link,words('Status updated.','状态已更新。'));
document.dispatchEvent(new Event('ccfleet:quota'));
}catch(e){message(link,words('Could not check status. Your inputs are kept.',
'无法查看状态，已保留输入内容。'));}
finally{busy.delete('status');}});
})();'''
PANEL_ACTION_CSP = "'sha256-" + base64.b64encode(
    hashlib.sha256(PANEL_ACTION_JS.encode()).digest()).decode() + "'"
PANEL_ACTION_TAG = '<script>' + PANEL_ACTION_JS + '</script>'

# A quota request is asynchronous: poll only its read-only region until the
# machine acknowledges it, a new reading arrives, or the five-minute bound ends.
ADMIN_QUOTA_POLL_JS = r'''(function(){
var timer=null,running=false,until=0;
function pending(){return Array.from(document.querySelectorAll('[data-admin-quota-pending]'));}
function stop(nodes){nodes.forEach(function(n){n.removeAttribute('data-admin-quota-pending');
var area=n.closest('[data-fleet-quota]'),button=area&&area.querySelector('button[type="submit"]');
if(button)button.disabled=false;
n.textContent=document.documentElement.lang.toLowerCase().startsWith('zh')?
'未收到新的额度读数。请稍后重试；旧读数不能证明还有剩余额度。':
'No new quota reading arrived. Try again later; old readings do not confirm remaining quota.';});}
async function poll(){timer=null;var nodes=pending();if(!nodes.length)return;
if(Date.now()>=until){stop(nodes);return;}running=true;
var controller=new AbortController(),abort=setTimeout(function(){controller.abort();},10000);
try{var path=location.pathname;
if(!/^\/(?:en\/|zh\/)?admin\/?$/.test(path))throw Error('path');
var r=await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',
signal:controller.signal});if(!r.ok)throw Error('response');
var text=await r.text();if(text.length>2097152)throw Error('size');
var page=new DOMParser().parseFromString(text,'text/html');
nodes.forEach(function(n){var current=n.closest('[data-fleet-quota]');
var fresh=current&&page.getElementById(current.id);
if(!fresh||!fresh.hasAttribute('data-fleet-quota')||fresh.querySelector(
'input:not([type="hidden"]),textarea,select,[contenteditable]'))throw Error('target');
current.replaceChildren(...Array.from(fresh.childNodes).map(function(c){
return document.importNode(c,true);}));});
document.dispatchEvent(new Event('ccfleet:quota'));
}catch(e){stop(pending());}
finally{clearTimeout(abort);running=false;if(pending().length)timer=setTimeout(poll,10000);}}
function start(){if(timer!==null||running||!pending().length)return;
until=Date.now()+300000;timer=setTimeout(poll,10000);}
document.addEventListener('ccfleet:quota',start);start();
})();'''
ADMIN_QUOTA_POLL_CSP = "'sha256-" + base64.b64encode(
    hashlib.sha256(ADMIN_QUOTA_POLL_JS.encode()).digest()).decode() + "'"
ADMIN_QUOTA_POLL_TAG = '<script>' + ADMIN_QUOTA_POLL_JS + '</script>'

# How often the page comes back for more. Fast enough while a sign-in is moving
# that a step finishing on the node shows up almost at once; slow the rest of
# the time, because nothing else here changes minute to minute.
IDLE_REFRESH_S = 60
ACTIVE_REFRESH_S = 4

#: The operator's console. The bare address belongs to the people who use the
#: product; the console sits under its own path on the same host (or at the
#: root of a dedicated CCFLEET_ADMIN_HOST, which forwards / here). Kept here,
#: beside the page, because the page's own refresh has to name it.
CONSOLE_PATH = "/admin"

#: The mark: three units of a rack, one of them lit — the machine a slot lives
#: on, which stays on. Inline, because the page may load nothing it did not
#: render itself (the CSP is default-src 'none').
MARK = ('<svg class="mark" viewBox="0 0 28 28" aria-hidden="true" focusable="false">'
        '<rect class="m-bg" width="28" height="28" rx="7"/>'
        '<rect class="m-unit" x="6" y="6.5" width="16" height="4" rx="1.4"/>'
        '<rect class="m-unit" x="6" y="12" width="16" height="4" rx="1.4"/>'
        '<rect class="m-unit" x="6" y="17.5" width="16" height="4" rx="1.4"/>'
        '<circle class="m-led" cx="18.6" cy="14" r="1.35"/></svg>')

#: The same mark for the browser tab, as a data URI so there is no second request.
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E"
           "%3Crect width='28' height='28' rx='7' fill='%234338ca'/%3E"
           "%3Crect x='6' y='6.5' width='16' height='4' rx='1.4' fill='white'/%3E"
           "%3Crect x='6' y='12' width='16' height='4' rx='1.4' fill='white'/%3E"
           "%3Crect x='6' y='17.5' width='16' height='4' rx='1.4' fill='white'/%3E"
           "%3Ccircle cx='18.6' cy='14' r='1.8' fill='%2322c55e'/%3E%3C/svg%3E")

CSS = """
/* Tokens. Light is the bare :root; dark redefines only the tokens, guarded so an
   explicit light choice still wins. Nothing below hard-codes a colour.
   One accent, for what you can press and for work under way; four status
   colours, each meaning one thing: green running, amber waiting on you, red
   trouble, grey off. */
:root{color-scheme:light;
--bg:#f5f6fa;--panel:#fff;--inset:#f0f2f8;--ink:#0c111c;--muted:#596277;
--rule:#dce0ea;--rule-soft:#e9ecf3;
--acc:#4338ca;--acc-strong:#3730a3;--acc-soft:#eef0ff;--acc-line:#c9cdf8;--on-acc:#fff;
--ok:#0b7a3b;--ok-bg:#e2f4e9;--ok-line:#a8dbbd;
--warn:#98580a;--warn-bg:#fcf0da;--warn-line:#f0cf95;
--bad:#b42318;--bad-bg:#fde7e4;--bad-line:#f4b8b0;
--off:#667085;--off-bg:#edeff4;--led:#22c55e;
/* Avatar grounds: the accent's family, each dark enough for a white initial,
   and the same in both themes because the initial on them does not change. */
--av0:#4338ca;--av1:#3730a3;--av2:#5b21b6;--av3:#6d28d9;--av4:#1d4ed8;--av5:#1e40af;
--av-ink:#fff;
--shadow:0 1px 2px rgba(12,17,28,.05),0 8px 24px -14px rgba(12,17,28,.16);
--radius:14px;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--bg:#0a0d14;--panel:#111621;--inset:#171d2a;--ink:#eceff6;--muted:#9aa3b6;
--rule:#262d3c;--rule-soft:#1c2230;
--acc:#a9b3ff;--acc-strong:#c6cdff;--acc-soft:#1b1e40;--acc-line:#3b4180;--on-acc:#0a0d14;
--ok:#4ade80;--ok-bg:#0d2919;--ok-line:#1f5a36;
--warn:#f4b04c;--warn-bg:#2c200b;--warn-line:#5c4417;
--bad:#ff8a80;--bad-bg:#36130f;--bad-line:#6e2a22;
--off:#8c94a7;--off-bg:#181d29;--led:#4ade80;
--shadow:0 1px 2px rgba(0,0,0,.5)}}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--sans);
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.page{max-width:1200px;margin:0 auto;padding-block:28px 48px;padding-left:20px;
padding-right:20px}
a{color:var(--acc);text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{color:var(--acc-strong)}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
code,.mono,td.num,.v{font-family:var(--mono);font-variant-numeric:tabular-nums}
/* A code chip never breaks across lines: half a command on each line reads as
   two commands. */
code{font-size:.88em;background:var(--inset);border:1px solid var(--rule-soft);
border-radius:6px;padding:.08em .4em;white-space:nowrap;
-webkit-box-decoration-break:clone;box-decoration-break:clone}
code.code-wrap{white-space:normal;overflow-wrap:anywhere}
pre code{font-size:inherit;background:none;border:0;padding:0;white-space:inherit}
h1{font-size:30px;line-height:1.15;letter-spacing:-.024em;margin:0;font-weight:720;
text-wrap:balance}
h2{font-size:19px;line-height:1.3;letter-spacing:-.012em;margin:30px 0 10px;
font-weight:680;text-wrap:balance}
h3{font-size:16px;line-height:1.35;letter-spacing:-.006em;margin:18px 0 6px;font-weight:650}
.sub{color:var(--muted);font-size:14px;margin:6px 0 0}
.sub strong{color:var(--ink);font-weight:600}
.muted{color:var(--muted)}.small{font-size:13px}
.bad-text{color:var(--bad);font-weight:600}
.nowrap{white-space:nowrap}

/* The brand: the mark, the name, and on the console a tag saying which side
   this is. */
.brand{display:inline-flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;
font-weight:760;letter-spacing:-.022em;line-height:1}
.brand:hover{color:var(--ink)}
.mark{width:28px;height:28px;flex:none;display:block}
.mark .m-bg{fill:var(--acc)}.mark .m-unit{fill:var(--on-acc)}.mark .m-led{fill:var(--led)}
.tag{font-size:12px;font-weight:650;letter-spacing:.01em;color:var(--acc);
background:var(--acc-soft);border:1px solid var(--acc-line);border-radius:999px;
padding:3px 9px}

/* The bar on top of every page: the mark, where you are, and who you are. */
.topbar{background:var(--panel);border-bottom:1px solid var(--rule);position:sticky;top:0;
z-index:10}
.topbar-in{max-width:1120px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;
gap:8px 22px;flex-wrap:wrap}
.console .topbar-in{max-width:1200px;gap:8px 12px}
.topbar .brand{font-size:18px}
.topbar-end{margin-left:auto;display:flex;align-items:center;gap:10px}
.topbar-end .btn,.topbar-end button{padding:7px 13px}

/* Who is signed in: an initial that opens a menu. A details element is the
   popover, so it needs no script and its summary takes the keyboard. */
.usermenu{position:relative}
.usermenu>summary{list-style:none;cursor:pointer;border-radius:50%;display:block}
.usermenu>summary::-webkit-details-marker{display:none}
.usermenu>summary::marker{content:""}
.usermenu>summary:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
.avatar{display:flex;align-items:center;justify-content:center;width:34px;height:34px;
border-radius:50%;color:var(--av-ink);background:var(--av0);font-weight:700;font-size:15px;
line-height:1;user-select:none;box-shadow:0 0 0 2px var(--panel),0 0 0 3px var(--rule)}
.usermenu>summary:hover .avatar,.usermenu[open]>summary .avatar{
box-shadow:0 0 0 2px var(--panel),0 0 0 4px var(--acc)}
.avatar.t1{background:var(--av1)}.avatar.t2{background:var(--av2)}
.avatar.t3{background:var(--av3)}.avatar.t4{background:var(--av4)}
.avatar.t5{background:var(--av5)}
.menu{position:absolute;right:0;top:calc(100% + 10px);z-index:30;width:260px;
max-width:calc(100vw - 32px);background:var(--panel);border:1px solid var(--rule);
border-radius:12px;padding:6px;box-shadow:0 1px 2px rgba(12,17,28,.06),
0 18px 40px -18px rgba(12,17,28,.35)}
/* The menu's head: the same face, larger, beside who it is. */
.menu-who{display:flex;align-items:center;gap:12px;padding:10px 10px 12px;
border-bottom:1px solid var(--rule-soft)}
.avatar.big{width:40px;height:40px;font-size:17px;flex:none;box-shadow:none}
.menu-who p{margin:0;display:grid;gap:1px;min-width:0;line-height:1.35}
.menu-who p span{color:var(--muted);font-size:14px}
.menu-who strong{font-size:15.5px;font-weight:700;overflow-wrap:anywhere}
.menu-links{display:grid;gap:2px;padding:6px 0;border-bottom:1px solid var(--rule-soft)}
.menu-links a{display:flex;align-items:center;justify-content:space-between;gap:10px;
padding:9px 10px;border-radius:8px;color:var(--ink);text-decoration:none;font-size:15px;
font-weight:450}
.menu-links a:hover,.menu-links a:focus-visible{background:var(--acc-soft);color:var(--acc)}
.menu-pill{font-size:12px;line-height:1;padding:3px 8px;border-radius:999px;
border:1px solid var(--rule);color:var(--muted);background:var(--panel);font-weight:500;
white-space:nowrap}
.menu form{display:block;padding:6px 0 0}
.usermenu .menu button.signout{width:100%;justify-content:flex-start;border:0;
box-shadow:none;background:none;padding:9px 10px;border-radius:8px;font-size:15px;
font-weight:450;color:var(--bad)}
.usermenu .menu button.signout:hover,.usermenu .menu button.signout:focus-visible{
background:var(--bad-bg);color:var(--bad)}

/* Console masthead: what this is, then the fleet in one glance. */
.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:16px 24px;
flex-wrap:wrap;margin:0 0 20px}
h1.brand{font-size:24px}
h1.brand .mark{width:32px;height:32px}
.strip{display:grid;grid-template-columns:repeat(4,minmax(76px,1fr));gap:8px;
width:100%;max-width:440px}
.tile{background:var(--panel);border:1px solid var(--rule);border-radius:12px;
padding:9px 12px 8px;box-shadow:var(--shadow)}
.tile b{display:block;font-family:var(--mono);font-size:22px;line-height:1.15;
font-weight:650;font-variant-numeric:tabular-nums}
.tile span{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
font-weight:600}
.tile.ok{border-color:var(--ok-line);background:var(--ok-bg)}.tile.ok b{color:var(--ok)}
.tile.warn{border-color:var(--warn-line);background:var(--warn-bg)}
.tile.warn b{color:var(--warn)}
.tile.critical{border-color:var(--bad-line);background:var(--bad-bg)}
.tile.critical b{color:var(--bad)}
.tile.zero b{color:var(--muted);font-weight:400}

/* The console is dense on purpose: section titles are labels, not headlines. */
.console h2{font-size:12px;line-height:1.4;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted);margin:30px 0 10px;font-weight:680}
.console .card h2{margin:16px 0 10px}
.console .admin-block>h2{font-size:22px;line-height:1.3;letter-spacing:-.02em;
text-transform:none;color:var(--ink);margin:32px 0 6px;scroll-margin-top:24px}
.admin-section-intro{font-size:14px;color:var(--muted);margin:0 0 14px}
.admin-tasks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:24px 0}
.admin-tasks>a{display:flex;align-items:center;gap:14px;padding:20px;min-width:0;
text-decoration:none;color:var(--ink);border:1px solid var(--rule);border-radius:16px;
background:var(--panel);box-shadow:var(--shadow)}
.admin-tasks>a:hover{border-color:var(--acc-line);background:var(--acc-soft)}
.admin-tasks strong,.admin-tasks small{display:block}
.admin-tasks strong{font-size:15px}
.admin-tasks small{color:var(--muted);font-size:12px;margin-top:3px}
.admin-task-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;
background:var(--acc-soft);color:var(--acc);font-size:24px;font-weight:650;flex-shrink:0}
.admin-accounts .card{padding:4px 20px 16px}
.admin-accounts .admin-account{display:grid;grid-template-columns:minmax(0,1fr) auto auto;
align-items:center;gap:12px 24px;padding:22px 0}
.console .admin-account .row-name{font-family:var(--sans);font-size:15px;overflow-wrap:anywhere}
.admin-account .row-name>.muted{display:block;font-size:12px;font-weight:400;margin-top:5px}
.admin-role{font-size:10px;font-weight:650;border-radius:5px;padding:3px 6px;
margin-left:8px;color:var(--acc);background:var(--acc-soft)}
.admin-credit-balance{text-align:right;min-width:0;max-width:100%;overflow-wrap:anywhere}
.admin-credit-balance>span{display:block;font-size:11px;color:var(--muted)}
.admin-credit-balance>strong{font-size:24px;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.admin-credit-action>summary{list-style:none;padding:9px 16px;cursor:pointer;font-size:13px;
font-weight:650;color:var(--on-acc);background:var(--acc);border-radius:10px;text-align:center}
.admin-credit-action>summary::-webkit-details-marker{display:none}
.admin-credit-action[open]{grid-column:1/-1}
.admin-credit-action[open]>summary{display:inline-block}
.admin-account .ledger{grid-column:1/-1;margin:0;color:var(--muted);font-size:12px}
.admin-account .admin-credit-history{grid-column:1/-1;color:var(--muted);font-size:12px}
.admin-credit-history>summary{cursor:pointer}
.admin-account .admin-monthly-history{grid-column:1/-1;font-size:12px;color:var(--muted)}
.admin-monthly-history>summary{cursor:pointer;font-weight:600;color:var(--acc)}
.admin-monthly-table{overflow-x:auto;max-width:100%;margin-top:12px;border:1px solid var(--rule);
border-radius:10px}.admin-monthly-table table{min-width:520px;font-size:12px}
.admin-help{margin-top:16px;font-size:13px;color:var(--muted)}
.admin-help>summary,.admin-price-details>summary{cursor:pointer;font-weight:600;padding:12px 0}
.admin-price-details>summary{font-size:14px;color:var(--acc)}
.admin-provider{display:block;width:fit-content;margin:3px 0;padding:3px 7px;border-radius:6px;
font-size:11px;line-height:1.5;font-weight:600;white-space:normal}
.admin-provider.host-provider{color:var(--acc);background:var(--acc-soft)}
.admin-provider.ip-provider{color:var(--ok);background:var(--ok-bg)}
.admin-advanced{margin-top:24px;border:1px solid var(--rule);border-radius:14px;
background:var(--panel);padding:4px 18px 16px;min-width:0;max-width:100%}
.admin-advanced>summary{padding:14px 0;font-size:15px;font-weight:650;cursor:pointer}
.admin-advanced:not([open]){padding-bottom:4px}
.admin-advanced h2:first-of-type{margin-top:10px}
@media(max-width:720px){.admin-tasks{grid-template-columns:1fr;gap:8px}
.admin-tasks>a{padding:14px 16px}.admin-task-icon{width:36px;height:36px;font-size:20px}
.admin-accounts .admin-account{grid-template-columns:minmax(0,1fr) auto;gap:12px}
.admin-account-heading{grid-column:1/-1}.admin-credit-balance{text-align:left}
.admin-credit-action{justify-self:end}.admin-credit-action[open]{justify-self:stretch}
.admin-accounts .card{padding-left:16px;padding-right:16px}}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
padding:4px 20px 16px;margin:0;box-shadow:var(--shadow)}
.card.form{max-width:760px;margin-top:26px}
.note{color:var(--muted);font-size:13px;line-height:1.55;margin:14px 0 0;
padding-top:12px;border-top:1px solid var(--rule-soft)}

/* The table. A rail on the left edge of each row carries state as form, so a
   glance down the column finds trouble without reading any number. */
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--rule);
border-radius:var(--radius);box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;min-width:880px;font-size:14px}
th,td{padding:12px 14px;text-align:left;border-bottom:1px solid var(--rule-soft);
vertical-align:top;white-space:nowrap}
th{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
background:var(--inset);font-weight:650;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{padding-left:16px}
tbody tr:last-child td{border-bottom:0}
tbody tr{border-left:3px solid transparent}
tbody tr.r-ok{border-left-color:var(--ok)}
tbody tr.r-warn{border-left-color:var(--warn)}
tbody tr.r-critical{border-left-color:var(--bad)}
tbody tr.r-disabled{border-left-color:var(--rule)}
tbody tr.r-disabled td{opacity:.62}
td.wrap{white-space:normal;min-width:130px}
.node-id{font-family:var(--mono);font-weight:650;font-size:14px}


/* Supplier inputs are operator-only; the calculated customer price is the result. */
.offer-pricing{padding-top:18px}
.offer-rule{margin:0 0 16px;padding:12px 14px;border:1px solid var(--acc-line);
border-radius:10px;background:var(--acc-soft);font-size:14px}
.offer-costs{overflow:auto;border:1px solid var(--rule-soft);border-radius:10px}
.offer-costs table{min-width:760px;font-size:13px}
.offer-costs th,.offer-costs td{padding:12px;white-space:normal;overflow-wrap:anywhere}
.offer-costs tbody th{text-transform:none;letter-spacing:normal;font-size:13px;
color:var(--ink);background:var(--panel)}
.offer-costs .sub{display:block;font-size:11px;line-height:1.5;margin-top:4px;color:var(--muted)}
.manage-panel>.mpart:has(.offer-fields){flex-basis:100%;border-top:1px solid var(--rule);
padding-top:12px}
.offer-costs td:last-child{font-family:var(--mono);font-weight:700;white-space:nowrap;
color:var(--acc);background:var(--acc-soft);font-size:15px}
.offer-costs td:nth-last-child(2){white-space:nowrap;font-variant-numeric:tabular-nums}
.offer-fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
width:100%;margin:12px 0}
.offer-fields fieldset{min-width:0;margin:0;padding:12px;border:1px solid var(--rule);
border-radius:10px;display:grid;gap:8px;align-content:start}
.offer-fields legend{font-size:12px;font-weight:650;color:var(--muted);padding:0 5px}
.offer-fields label{display:grid;gap:5px;font-size:12px;min-width:0}
.offer-fields input,.offer-fields select{min-width:0;max-width:100%;width:100%!important}
.offer-preview{margin:8px 0 12px;line-height:1.6}
.offer-preview strong{font-size:17px;color:var(--acc)}
@media(max-width:900px){.offer-fields{grid-template-columns:1fr}.offer-costs table{min-width:680px}}

/* A state, said the same way everywhere: a coloured dot and a word. */
/* A pill may wrap: a few carry a whole sentence ("hostname pending: …"), and
   those must fit a phone. A short one never has a reason to. */
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:1.25;
padding:3px 10px 3px 8px;border-radius:999px;font-weight:650;max-width:100%;
overflow-wrap:break-word;vertical-align:middle;border:1px solid var(--rule);
color:var(--off);background:var(--off-bg);font-family:var(--sans);letter-spacing:0}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;
flex:none}
.pill.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-line)}
.pill.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-line)}
.pill.critical{color:var(--bad);background:var(--bad-bg);border-color:var(--bad-line)}
.pill.disabled{color:var(--off);background:var(--off-bg);border-color:var(--rule)}
.pill.busy{color:var(--acc);background:var(--acc-soft);border-color:var(--acc-line)}

/* Alerts: severity first, rule name in mono, prose in sans. */
.alert{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;padding:12px 0;
border-bottom:1px solid var(--rule-soft)}
.alert:last-of-type{border-bottom:0}
.alert-rule{font-family:var(--mono);font-size:13px;font-weight:650}
.alert-msg{font-size:14px;min-width:0;overflow-wrap:anywhere}
.quiet{color:var(--muted);font-size:14px;padding:12px 0 4px;margin:0}

/* Controls */
form.inline{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0;
vertical-align:middle;max-width:100%}
/* A field beside its button stays beside it, until there is no room at all. A
   fixed width, not a flex basis: the form is sized from the field's width, and
   a basis it grows past would push the button onto a line of its own. */
form.inline input[type=text],form.inline input[type=email]{width:230px;flex:0 1 auto;
min-width:0;max-width:100%}
/* A field and its button on one line: the slot and account rows. */
form.field{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0}
form.field input[type=text],form.field input[type=email]{width:auto;padding:6px 10px;
font-size:13px;min-height:32px}
form.field input.count{width:4.8em}
form.field input.price{width:7em}
/* The price card's currency: the same box as the fields beside it. */
form.field select{font:inherit;font-size:13px;padding:5px 8px;min-height:32px;
border-radius:10px;border:1px solid var(--rule);background:var(--inset);color:var(--ink)}
form.field select:focus{border-color:var(--acc);outline:3px solid var(--acc-soft);
outline-offset:0}
form.field input[type=date]{font-size:13px;padding:5px 8px;min-height:32px;width:auto}
/* An account's payments, folded under its row. */
details.ledger{flex-basis:100%;font-size:13px}
details.ledger summary{cursor:pointer;color:var(--muted);font-size:12.5px;font-weight:600}
.payment{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 0;
overflow-wrap:anywhere}
.payment.voided{color:var(--muted)}
details.ledger form.field{margin-top:6px}
/* The slots card: a row a machine, its one slot being the row, under column
   heads. Each row's actions fold under its Manage, which opens below the row
   across its whole width; on a phone the rows stack as small cards. */
.slots .releases{margin:14px 0 2px}
.slots .note{border-top:0;padding-top:0}
.slothead,.slotline{display:grid;gap:4px 14px;padding-right:100px;
grid-template-columns:minmax(0,1.35fr) minmax(0,1.25fr) 150px minmax(0,1.35fr) 52px
minmax(0,1.45fr)}
.slothead{padding-top:12px;padding-bottom:8px;border-bottom:1px solid var(--rule-soft);
font-size:11px;line-height:1.3;letter-spacing:.07em;text-transform:uppercase;
color:var(--muted);font-weight:650}
.slotrow{position:relative;padding:12px 0;border-bottom:1px solid var(--rule-soft)}
.slotline{align-items:baseline;font-size:13px}
.slotline>div{min-width:0;overflow-wrap:anywhere}
.slotline .row-name{display:block}
.slotline .sub{display:block;margin-top:2px;font-size:12px;color:var(--muted)}
.c-state .pill{white-space:nowrap}
.c-age{color:var(--muted);font-variant-numeric:tabular-nums}
.c-flags{display:flex;flex-wrap:wrap;gap:4px 6px}
.c-empty{grid-column:2/6;color:var(--muted)}
.c-cc .pill,.c-cc .cc-new{margin-left:2px}
.cc-new{color:var(--acc);font-size:12px;font-weight:650;white-space:nowrap}
.lbl,.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);
white-space:nowrap}
.slot-notes{list-style:none;margin:6px 0 0;padding:0;font-size:12.5px;line-height:1.45}
.slot-notes li{margin-top:2px;overflow-wrap:anywhere}
details.manage>summary{position:absolute;top:8px;right:0;list-style:none;cursor:pointer;
display:inline-flex;align-items:center;gap:5px;font-size:12.5px;font-weight:600;
padding:5px 10px;border:1px solid var(--rule);border-radius:8px;background:var(--panel);
color:var(--ink);white-space:nowrap}
details.manage>summary::-webkit-details-marker{display:none}
details.manage>summary::marker{content:""}
details.manage>summary:hover{border-color:var(--acc);color:var(--acc)}
details.manage>summary:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
details.manage[open]>summary{background:var(--acc-soft);border-color:var(--acc-line);
color:var(--acc)}
details.manage .caret{display:inline-block;font-size:12px;line-height:1;
transition:transform .15s}
details.manage[open] .caret{transform:rotate(180deg)}
.manage-panel{display:flex;flex-wrap:wrap;gap:14px 32px;margin-top:10px;padding:12px 14px;
background:var(--inset);border:1px solid var(--rule-soft);border-radius:10px}
.mpart{flex:1 1 250px;min-width:0}
.mpart .mhead{margin:0 0 7px;font-size:11px;line-height:1.3;letter-spacing:.07em;
text-transform:uppercase;color:var(--muted);font-weight:650}
.mpart .mhint{margin:7px 0 0;font-size:12.5px;line-height:1.45;color:var(--muted)}
.mpart form.field{display:flex}
.mpart form.field+form.field{margin-top:8px}
.mpart button.danger{color:var(--bad);border-color:var(--bad-line)}
/* Narrower, the attention pills leave their column for a line under the row,
   so an address keeps room enough not to break mid-word. */
@media (max-width:1100px){
.slothead,.slotline{grid-template-columns:minmax(0,1.35fr) minmax(0,1.25fr) 150px
minmax(0,1.35fr) 52px}
.slothead span:last-child{display:none}
.c-flags{grid-column:1/-1}
.c-flags:empty{display:none}
.c-empty{grid-column:2/-1}
}
@media (max-width:960px){
.slothead{display:none}
.slotrow{padding:12px 14px;margin:10px 0;border:1px solid var(--rule);border-radius:12px}
.slotline{padding-right:0;gap:6px 10px;grid-template-columns:minmax(0,1fr) auto}
.c-name{grid-area:1/1}.c-state{grid-area:1/2;justify-self:end}
/* The age shares the holder's line, not the state's column: an address
   squeezed beside "Ready to sign in" would break mid-word. */
.c-holder{grid-area:2/1/3/3;padding-right:84px}.c-age{grid-area:2/1/3/3;justify-self:end}
.c-cc,.c-flags,.c-empty{grid-column:1/-1}
.c-cc.none,.c-flags:empty{display:none}
.lbl{position:static;width:auto;height:auto;overflow:visible;clip:auto;color:var(--muted)}
details.manage>summary{position:static;margin-top:10px}
.manage-panel{padding:12px}
}
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;font:inherit;
font-size:13.5px;font-weight:600;line-height:1.25;padding:8px 14px;border-radius:10px;
border:1px solid var(--rule);background:var(--panel);color:var(--ink);cursor:pointer;
text-decoration:none;white-space:nowrap;box-shadow:0 1px 0 rgba(12,17,28,.04)}
button:hover,.btn:hover{border-color:var(--acc);color:var(--acc)}
button.primary,.btn.primary{background:var(--acc);border-color:var(--acc);color:var(--on-acc)}
button.primary:hover,.btn.primary:hover{background:var(--acc-strong);
border-color:var(--acc-strong);color:var(--on-acc)}
button.danger:hover{border-color:var(--bad);color:var(--bad);background:var(--bad-bg)}
.btn.big{font-size:15px;padding:11px 18px;border-radius:12px}
.console button,.console .btn{font-size:12.5px;padding:6px 11px;border-radius:8px}
.console button.primary{font-size:13px;padding:8px 16px}
label{display:block;font-size:12.5px;color:var(--muted);margin:0 0 6px;font-weight:600}
input[type=text],input[type=email],input[type=date]{font:inherit;font-size:14px;
padding:8px 12px;border-radius:10px;border:1px solid var(--rule);background:var(--inset);
color:var(--ink);width:100%;max-width:300px;min-height:38px}
input[type=text]:focus,input[type=email]:focus,input[type=date]:focus{
border-color:var(--acc);outline:3px solid var(--acc-soft);outline-offset:0;
background:var(--panel)}
input::placeholder{color:var(--muted)}
input[type=checkbox]{width:16px;height:16px;margin:3px 0 0;accent-color:var(--acc);flex:none}
.fields{display:flex;flex-wrap:wrap;gap:14px 18px;margin:0 0 16px}
.check{display:flex;align-items:flex-start;gap:8px;font-size:13.5px;color:var(--ink);
margin:0;font-weight:400;line-height:1.5}
.fields .check{margin-top:28px}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row-line{display:flex;align-items:center;justify-content:space-between;gap:10px 16px;
flex-wrap:wrap;padding:13px 0;border-bottom:1px solid var(--rule-soft)}
.row-line:last-of-type{border-bottom:0}
.row-name{font-weight:650;font-size:14px}
.console .row-name{font-family:var(--mono);font-size:13.5px}
.row-line.stacked{display:block}
.row-line.stacked .row-name{margin-bottom:8px}
.login-url{display:block;font-family:var(--mono);font-size:12.5px;line-height:1.5;
word-break:break-all;margin:10px 0 12px;padding:10px 12px;border-radius:10px;
background:var(--inset);border:1px solid var(--rule);text-decoration:none}
.login-url:hover{border-color:var(--acc)}
.login-say{font-size:14px;margin-right:8px;font-weight:600}
pre{background:var(--inset);border:1px solid var(--rule);border-radius:12px;
padding:14px 16px;overflow-x:auto;font-family:var(--mono);font-size:13px;
line-height:1.6;margin:0 0 14px;white-space:pre}
.ok-banner{border:1px solid var(--ok-line);background:var(--ok-bg);color:var(--ok);
border-radius:12px;padding:13px 17px;margin:0 0 20px;font-weight:550}
a.back{font-size:14px;font-weight:600;text-decoration:none}

/* Usage: who, the two windows, then the trend. */
.usage-row{display:grid;
grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);
gap:16px 22px;align-items:start;padding:16px 0;border-bottom:1px solid var(--rule-soft)}
.usage-row:last-of-type{border-bottom:0}
.usage-name{font-family:var(--sans);font-weight:400;font-size:13.5px;min-width:0;
overflow-wrap:anywhere}
.usage-slot-title{font-family:var(--mono);font-size:16px;margin:0 0 12px}
.quota-historical .meter-fill{background:var(--muted);opacity:.5}
.usage-quota,.usage-spark{min-width:0}
.usage-total{margin-top:9px;font-size:13px}
.usage-total b{font-family:var(--mono);font-size:22px;font-weight:650;
letter-spacing:-.01em;line-height:1.1}
.usage-total span{font-family:var(--sans);font-weight:400}
.usage-meta{font-family:var(--sans);font-weight:400;font-size:12.5px;margin-top:5px;
line-height:1.45;overflow-wrap:anywhere}
.usage-nums{font-size:11px;letter-spacing:.06em;text-transform:uppercase;margin-top:6px;
font-weight:600}
svg.spark{display:block;width:100%;height:46px;overflow:visible}
.spark-fill{fill:var(--acc-soft);stroke:none}
.spark-base{stroke:var(--rule);stroke-width:1;vector-effect:non-scaling-stroke}
.spark-line{fill:none;stroke:var(--acc);stroke-width:1.6;vector-effect:non-scaling-stroke}
.spark-dot{fill:var(--acc)}

/* One quota window. The bar is the point; the number confirms it. */
.meter{margin:0 0 12px}
.meter:last-child{margin-bottom:2px}
.meter-head{display:flex;justify-content:space-between;align-items:baseline;
font-size:13px;color:var(--muted);margin:0 0 6px}
.meter-pct{font-family:var(--mono);font-weight:700;color:var(--ink);
font-variant-numeric:tabular-nums;font-size:13.5px}
.meter-track{height:8px;border-radius:99px;background:var(--inset);overflow:hidden;
box-shadow:inset 0 0 0 1px var(--rule-soft)}
.meter-fill{display:block;height:100%;border-radius:99px;min-width:3px}
.meter-fill.ok{background:var(--acc)}
.meter-fill.warn{background:var(--warn)}
.meter-fill.crit{background:var(--bad)}
.meter-foot{font-size:12px;color:var(--muted);margin-top:5px}

@media (max-width:820px){.usage-row{grid-template-columns:1fr;gap:10px}
.strip{max-width:none}h1{font-size:25px}.page{padding-block:20px 36px}
.topbar-in{padding:10px 16px}}
@media (max-width:420px){.strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""


def _age(now: float, ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    seconds = max(0, int(now - ts))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    days = seconds / 86400
    return f"{days:.1f}d" if days < 10 else f"{days:.0f}d"


def _in(now: float, ts_ms: Optional[float]) -> str:
    expiry = access_expiry({"expires_at": ts_ms})
    if expiry is None:
        return "unavailable"
    delta = expiry - now
    return ("in " if delta >= 0 else "") + _age(0, -abs(delta)) + ("" if delta >= 0 else " ago")


def _slot_facts(node: Mapping[str, Any], payload: Mapping[str, Any],
                mine: list[Mapping[str, Any]]) -> tuple[Mapping[str, Any], str]:
    """What a shared machine is running, and the name it goes by.

    Root on a shared machine runs no Claude Code and is signed in to nothing:
    its version, sign-in, Remote Control and usage are its slot's. Read from
    the machine's own fields they said "-" and "unknown" beside a slot that was
    signed in and in use. With its one slot (every machine since one slot per
    machine) that slot's report stands in for the machine; with none, or
    several from before, there is no single answer, and the Slots card says
    each.
    """
    if len(mine) != 1:
        return {}, node["id"]
    report = next((r for r in payload.get("slots") or []
                   if isinstance(r, Mapping) and r.get("unix_user") == mine[0]["unix_user"]),
                  {})
    return report, slotnames.display(mine[0])


def _free_pool_slot(slot: Mapping[str, Any]) -> bool:
    """A neutral pool label only after the holder is gone and the wipe is confirmed."""
    return (slot.get("kind") == slotstates.MACHINE_SLOT
            and slot.get("state") == slotstates.FREE and slot.get("held_by") is None
            and slot.get("present") == 0
            and slotnames.valid_pool_name(slot.get("pool_name")))


def build_rows(nodes: list[Mapping[str, Any]], latest: Mapping[str, Mapping[str, Any]],
               alerts: list[Mapping[str, Any]], now: float,
               slots: Sequence[Mapping[str, Any]] = (), *, max_age: float = 15 * 60,
               listening_since: Optional[float] = None) -> list[dict[str, Any]]:
    """Merge node records, latest heartbeats and open alerts into dashboard rows.

    ``slots`` are the store's slot rows. A node with a machine slot, or whose
    agent says it is a shared machine, is described by its slot and named
    after it, the way claude.ai and the Slots card name it.
    """
    alerts_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for alert in alerts:
        alerts_by_node.setdefault(alert["node_id"], []).append(alert)
    machine_slots: dict[str, list[Mapping[str, Any]]] = {}
    for slot in slots:
        if slot.get("kind") == slotstates.MACHINE_SLOT:
            machine_slots.setdefault(slot["node_id"], []).append(slot)
    rows = []
    for node in nodes:
        hb = latest.get(node["id"])
        payload = (hb or {}).get("payload") or {}
        node_alerts = alerts_by_node.get(node["id"], [])
        level = "ok"
        for alert in node_alerts:
            if LEVEL_ORDER.get(alert["level"], 0) > LEVEL_ORDER[level]:
                level = alert["level"]
        mine = machine_slots.get(node["id"], [])
        machine = bool(mine) or payload.get("mode") == slotstates.MACHINE_MODE
        facts, shown = _slot_facts(node, payload, mine) if machine else (payload, node["id"])
        creds = facts.get("credentials") or {}
        rows.append({
            "id": node["id"], "name": shown, "machine": machine, "slot_count": len(mine),
            # Named while somebody holds it: from the claim until the wipe that
            # frees it (see _called).
            "held_name": len(mine) == 1 and bool(mine[0].get("name")),
            "free_pool": len(mine) == 1 and _free_pool_slot(mine[0]),
            # Its one slot, for the usage card's Refresh (see _quota_refresh).
            "slot_id": mine[0]["id"] if len(mine) == 1 else None,
            "slot_state": mine[0].get("state") if len(mine) == 1 else None,
            "slot_present": (bool(mine[0]["present"]) if len(mine) == 1
                             and mine[0].get("present") is not None else None),
            "pool_reserved": bool(node.get("reserved_for")),
            "quota_wanted_at": mine[0].get("quota_wanted_at") if len(mine) == 1 else None,
            "owner": node["owner"], "region": node["region"],
            "enabled": node["enabled"], "status": level if node["enabled"] else "disabled",
            "last_seen_ts": (hb or {}).get("ts"),
            "hostname": payload.get("hostname"),
            "claude_version": (facts.get("claude") or {}).get("version"),
            "compatibility": compatibility.observation(
                facts.get("compatibility"), facts.get("claude"), creds,
                heard=(hb or {}).get("ts"), now=now, max_age=max_age,
                listening_since=listening_since,
                since=max(mine[0].get("claimed_at") or 0,
                          mine[0].get("account_switched_at") or 0) if len(mine) == 1 else 0),
            "pinned_version": node["pinned_version"],
            "egress_ip": (payload.get("egress") or {}).get("ip"),
            "disk_used_pct": (payload.get("disk") or {}).get("used_pct"),
            "load1": (payload.get("load") or {}).get("1"),
            "device_token_at": node.get("device_token_at"),
            "credentials_present": creds.get("present"),
            "credentials_logged_in": creds.get("logged_in"),
            "credentials_mtime": creds.get("mtime"),
            "token_expires_at": creds.get("expires_at"),
            "subscription_type": creds.get("subscription_type"),
            "plan": creds.get("plan"),
            "remote_control": (facts.get("remote_control") or {}).get("state"),
            "rc_expected": node["rc_expected"],
            # What the node did about its pin last time it was asked. A silent
            # reconcile is indistinguishable from one that never ran.
            "last_upgrade": ((payload.get("reconcile") or {}).get("upgrade") or None),
            # The OS asked for a reboot (a kernel, a libc). A badge, not an alert:
            # it is the operator's to schedule, and nothing is broken meanwhile.
            "reboot_required": payload.get("reboot_required") is True,
            "usage": facts.get("usage") or {},
            "relay": facts.get("relay"),
            "usage_since": max(mine[0].get("claimed_at") or 0,
                               mine[0].get("account_switched_at") or 0) if len(mine) == 1 else 0,
            "quota": facts.get("quota") or {},
            "open_alerts": [a["rule"] for a in node_alerts],
        })
    return rows


def _pill(level: str) -> str:
    return f'<span class="pill {escape(level)}">{escape(level)}</span>'


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return '<span class="muted">-</span>'
    if isinstance(value, float):
        return escape(f"{value:.0f}{suffix}") if suffix == "%" else escape(f"{value:.2f}{suffix}")
    return escape(f"{value}{suffix}")


def _actions_html(row: Mapping[str, Any], csrf: str) -> str:
    """Per-node buttons. Every write is a POST carrying the CSRF token."""
    node = escape(row["id"])
    def form(action: str, label: str, extra: str = "", cls: str = "") -> str:
        return (f'<form class="inline" method="post" action="/actions/node/{node}/{action}">'
                f'<input type="hidden" name="csrf" value="{escape(csrf)}">{extra}'
                f'<button class="{cls}" type="submit"{admin_attributes(label)}>'
                f'{escape(label)}</button></form>')
    toggle = form("disable", "Disable") if row["enabled"] else form("enable", "Enable")
    rc = form("rc-off", "RC alert off") if row["rc_expected"] else form("rc-on", "RC alert on")
    pin = ""
    if row["claude_version"] and row["claude_version"] != row["pinned_version"]:
        version = escape(str(row["claude_version"]))
        pin = form("pin", f"Pin {row['claude_version']}",
                   f'<input type="hidden" name="version" value="{version}">')
    rotate = form("rotate-token", "New token", cls="danger")
    remove = form("remove", "Remove", '<input type="hidden" name="confirm" value="'
                  + node + '">', cls="danger")
    return f'<div class="actions">{toggle}{rc}{pin}{rotate}{remove}</div>'


def _called(row: Mapping[str, Any]) -> tuple[str, str]:
    """The name a node goes by, and what is said beside it, if anything.

    A shared machine goes by its slot's name, which is its hostname and what
    claude.ai shows. While somebody holds the slot the name is theirs
    (alice-1), and it is said alone: not the machine's id, which is the
    operator's handle for it on the server, nor the operator who owns it
    (Erik, 2026-09-24). A confirmed empty pool slot is also said by its
    neutral pool name alone; its internal identity stays in management
    controls. `ccfleetd slot list` still says which machine a name is on.
    Returned unescaped; "" for nothing.
    """
    shown = row.get("name") or row["id"]
    if row.get("held_name") or row.get("free_pool"):
        return shown, ""
    return shown, (f"on {row['id']} \u00b7 " if shown != row["id"] else "") + row["owner"]


def _row_html(row: Mapping[str, Any], now: float) -> str:
    free = row.get("machine") and row.get("slot_state") == slotstates.FREE
    version = _fmt(row["claude_version"])
    pinned = row["pinned_version"]
    # Only say the pin when it tells you something. Repeating it beside an equal
    # installed version rendered "2.1.278 2.1.278", which reads as a glitch.
    if pinned and is_channel(pinned):
        version += f' <span class="muted">{escape(pinned)}</span>'
    elif pinned and row["claude_version"] != pinned:
        drift = pinned + " \u2260 pinned"
        version += f' <span class="muted">{escape(drift)}</span>'
    # A drifted version with no explanation reads as "not tried yet". Say when the
    # node tried and failed, because that is the case an operator must act on.
    upgrade = row.get("last_upgrade") or {}
    if upgrade.get("ok") is False:
        detail = escape(str(upgrade.get("error") or "")[:120])
        version += (f'<br><span class="bad-text">upgrade to '
                    f'{escape(str(upgrade.get("to") or "?"))} failed</span>'
                    + (f' <span class="muted">{detail}</span>' if detail else ""))
    validation = row.get("compatibility") or {}
    check_state = validation.get("state")
    if check_state in compatibility.LABELS:
        tone = ("bad-text" if check_state == "failed"
                or validation.get("reason") in compatibility.OPERATOR_REVIEW_REASONS else "muted")
        version += (f'<br><span class="{tone}" '
                    f'title="{escape(compatibility.description(validation))}">'
                    f'{escape(compatibility.LABELS[check_state])}</span>')
    if free:
        # Its next holder gets a fresh installation. A cached report from the
        # previous holder says nothing about that installation.
        version = '<span class="muted">Installed on claim</span>'
    creds = row["credentials_present"]
    signed_out = creds is False or row.get("credentials_logged_in") is False
    cred_text = ("not signed in" if row.get("credentials_logged_in") is False else
                 "unknown" if creds is None else "missing" if creds is False else
                 f"refreshed {_age(now, row['credentials_mtime'])} ago")
    if row.get("machine"):
        count = row.get("slot_count") or 0
        cred_text = ("no slot yet" if count == 0 else
                     f"{count} slots, see Slots" if count > 1 else
                     "not signed in" if signed_out else cred_text)
    plan = plans.label(row["subscription_type"], row.get("plan"))
    if free:
        present = row.get("slot_present")
        if present is True:
            cred_text = "unallocated · cleanup required"
        elif present is None:
            cred_text = "unallocated · waiting for machine"
        elif not row["enabled"]:
            cred_text = "unallocated · machine disabled"
        else:
            cred_text = ("available · reserved in pool" if row.get("pool_reserved") else
                         "available · in pool")
    elif plan and not signed_out:
        # The plan is a label on the login, not a qualifier on the time. Trailing
        # it read as "refreshed 10m ago (max)", where (max) looks like it modifies
        # the age; leading it reads as what it is.
        cred_text = f"{plan} \u00b7 {cred_text}"
    rc = row["remote_control"] or "-"
    if row.get("machine") and (row["remote_control"] in (None, "", "inactive", "disabled")
                               or free and row.get("slot_present") is False):
        # Hosted slots use the CLI relay. Their agents retire the legacy Remote
        # Control unit, so an inactive unit is the intended state. A reported
        # active or failed legacy unit remains visible for the operator.
        rc = "disabled (hosted)"
    # Nothing is expected of a node that is switched off, so saying so is noise;
    # nor of a shared machine, whose Remote Control is its slot's to run.
    if row["rc_expected"] and row["enabled"] and not row.get("machine"):
        rc += " (expected)"
    shown, beside = _called(row)
    byline = ("" if row.get("free_pool") else
              " \u00b7 ".join(part for part in (beside, row["region"] or "-") if part))
    login = escape(cred_text)
    if not free:
        login += ('<br><span class="muted">token '
                  + escape(_in(now, row["token_expires_at"])) + "</span>")
    return (
        f'<tr class="r-{escape(row["status"])}">'
        f"<td>{_pill(row['status'])}</td>"
        f'<td><span class="node-id">{escape(shown)}</span>'
        + (' <span class="pill warn">reboot needed</span>' if row.get("reboot_required") else "")
        + (f'<br><span class="muted">{escape(byline)}</span>' if byline else "") + "</td>"
        f"<td class=\"num\">{escape(_age(now, row['last_seen_ts']))}</td>"
        f'<td class="v">{version}</td>'
        f"<td><code>{_fmt(row['egress_ip'])}</code></td>"
        f"<td class=\"num\">{_fmt(row['disk_used_pct'], '%')}</td>"
        f"<td class=\"num\">{_fmt(row['load1'])}</td>"
        f"<td>{login}</td>"
        f"<td>{escape(rc)}</td>"
        "</tr>"
    )


def _manage_html(rows: list[Mapping[str, Any]], csrf: str) -> str:
    """Per-node controls, kept out of the status table so neither gets cramped."""
    if not rows:
        return ""
    items = []
    for row in rows:
        node = escape(row["id"])

        def form(action: str, label: str, extra: str = "", cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{extra}'
                    f'<button class="{cls}" type="submit"{admin_attributes(label)}>'
                    f'{escape(label)}</button></form>')

        buttons = [form("disable", "Disable") if row["enabled"] else form("enable", "Enable")]
        # A shared machine is never judged on Remote Control: its slot runs it,
        # and only once somebody has signed in. A switch there would do nothing.
        if not row.get("machine"):
            buttons.append(form("rc-off", "RC alert off") if row["rc_expected"] else
                           form("rc-on", "RC alert on"))
        if row["claude_version"] and row["claude_version"] != row["pinned_version"]:
            version = escape(str(row["claude_version"]))
            buttons.append(form("pin", f"Pin {row['claude_version']}",
                                f'<input type="hidden" name="version" value="{version}">'))
        buttons.append(form("rotate-token", "New token", cls="danger"))
        buttons.append(form("remove", "Remove",
                            f'<input type="hidden" name="confirm" value="{node}">', cls="danger"))
        shown, beside = _called(row)
        by = f'<span class="muted"> · {escape(beside)}</span>' if beside else ""
        items.append(f'<div class="row-line"><div class="row-name">{escape(shown)}{by}</div>'
                     f'<div class="actions">{"".join(buttons)}</div></div>')
    return ('<h2 id="manage" data-copy-zh=\"管理节点\">Manage nodes'
        '</h2><div class="card">' + "".join(items) +
            '<p class="note">'
            "New token replaces the node's credential immediately, so update the node after. "
            "Remove deletes its history and cannot be undone.</p></div>")


def _add_form(csrf: str) -> str:
    return (
        '<h2 id="add-node" data-copy-zh=\"添加节点\">Add a node</h2><div class="card form">'
        '<form method="post" action="/actions/node/add">'
        f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
        '<div class="fields">'
        '<div><label for="node_id" data-copy-zh=\"节点名称\">Node name</label>'
        '<input type="text" id="node_id" name="node_id" placeholder="laptop-erik" required '
        'pattern="[a-z0-9][a-z0-9-]{1,39}" title="lowercase letters, digits and hyphens"></div>'
        '<div><label for="owner" data-copy-zh=\"所有者\">Owner</label>'
        '<input type="text" id="owner" name="owner" placeholder="erik" required></div>'
        '<div><label for="region" data-copy-zh=\"地区\">Region</label>'
        '<input type="text" id="region" name="region" placeholder="us-west"></div>'
        '<label class="check"><input type="checkbox" name="rc_expected" value="1"> '
        'Alert if Remote Control stops</label>'
        '</div>'
        '<button class="primary" type="submit">Add node</button>'
        '</form></div>')


def render_add_result(node_id: str, token: str, cfg: Config, owner: str = "",
                      corner: str = "") -> str:
    """Shown once, right after a node is created. This is the only time the token exists."""
    url = cfg.public_url or f"http://127.0.0.1:{cfg.bind_port}"
    steps = (f"CCFLEET_URL={url}\n"
             f"CCFLEET_NODE_ID={node_id}\n"
             f"CCFLEET_NODE_TOKEN={token}")
    # Every value below is quoted before it reaches a command an operator will paste
    # as root. The store validates these too; this is the second line of defence.
    install_cmd = (
        "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/install.sh \\\n"
        "  | sudo bash -s -- \\\n"
        f"      --server {shq(url)} \\\n"
        f"      --node {shq(node_id)} \\\n"
        f"      --token {shq(token)} \\\n"
        f"      --owner {shq(owner) if owner else '<owner>'}")
    if cfg.bypass_by_default:
        install_cmd += " \\\n      --bypass-permissions"
    bypass_note = (
        "<p class=\"muted\"><strong>This fleet runs without permission prompts.</strong> "
        "<code>--bypass-permissions</code> is in the command above because "
        "<code>CCFLEET_BYPASS_BY_DEFAULT</code> is set on this server. The owner of this node "
        "also has passwordless sudo, so with prompts off nothing stands between a tool call and "
        "root. Drop the flag for a node where that is not wanted.</p>"
        if cfg.bypass_by_default else "")
    return (
        _console_open(f"{node_id} added", corner, product_href(cfg, "/"))
        + f"<h1>{escape(node_id)} added</h1>"
        "<div class=\"ok-banner\">Copy the three lines below now. The token is shown once "
        "and is not stored in readable form.</div>"
        "<div class=\"card\"><h2>1. Run this on a fresh server, as root</h2>"
        f"<pre>{escape(install_cmd)}</pre>"
        "<p class=\"muted\">It hardens the machine, installs Claude Code, starts the agent, and "
        "stops at the sign-in. Remote Control is enabled but not started: that needs a login "
        "which does not exist yet, so the owner starts it in step 2. "
        "Add <code>--ssh-key \"ssh-ed25519 …\"</code> "
        "with the owner's public key, or SSH hardening is skipped so nobody is locked out.</p>"
        f"{bypass_note}"
        "<h2>2. The owner signs in, on that machine</h2>"
        "<pre>claude          # choose the claude.ai login, approve, paste the code back\n"
        "/status         # confirms their account, no base URL, no auth token\n"
        "systemctl --user start claude-remote-control.service   # once, for claude.ai access</pre>"
        "<p class=\"muted\">Nobody else can do this step: a subscription login has to complete "
        "through Anthropic's own flow. Everything before it is the command above.</p>"
        "<h2>If you would rather not paste a token around</h2>"
        f"<pre>{escape(steps)}</pre>"
        "<p class=\"muted\">Those three lines are what the command writes to "
        "<code>~/.config/ccfleet/agent.env</code>; you can place them by hand and run "
        "<code>node/setup-owner.sh</code> instead.</p>"
        "</div>"
        f"<p><a class=\"back\" href=\"{CONSOLE_PATH}\">&larr; back to the fleet</a></p>"
        "</div></body></html>")


def _console_bar(corner: str = "", home: str = "/") -> str:
    """The console's bar: the mark, which side this is, and who is signed in.

    The mark goes home, to the product's front page, as it does on every other
    page; the console is one press away from there, in the avatar's menu.
    ``home`` is that page as a link from here (see product_href).
    """
    return ('<header class="topbar"><div class="topbar-in">'
            f'<a class="brand" href="{escape(home)}">{MARK}<span>ccfleet</span></a>'
            '<span class="tag" data-copy-zh=\"管理后台\">console</span>'
            f'<div class="topbar-end">{bilingual.CONTROLS}{corner}</div></div></header>')


def _console_open(title: str, corner: str = "", home: str = "/") -> str:
    """The head and the bar of a console page that is not the dashboard."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>ccfleet · {escape(title)}</title>"
            f'<link rel="icon" href="{FAVICON}">'
            f"<style>{CSS}{bilingual.CSS}</style></head><body class=\"console\">"
            + _console_bar(corner, home) + "<div class=\"page\">")


# -- who is looking -------------------------------------------------------------------

#: The bar's corner for somebody not signed in: the way to their slots, which
#: begins by signing in.
SIGN_IN_LINK = '<a class="btn primary" href="/account" data-copy-zh=\"登录\">Sign in</a>'
#: How many avatar grounds there are (--av0 to --av5 in the stylesheet).
AVATAR_TONES = 6


def _initial(account: Mapping[str, Any]) -> str:
    """One character for an avatar: the handle's first, else the email's.

    The first letter or digit, so "_ops@" still gets one; a question mark when
    there is nothing to take one from.
    """
    source = str(account.get("handle") or str(account.get("email") or "").split("@", 1)[0])
    for char in source:
        if char.isalnum():
            return char.upper()[:1]
    return "?"


def _tone(account: Mapping[str, Any]) -> int:
    """Which avatar ground: fixed by the account, so it is the same on every
    page and every visit, and two people side by side are told apart."""
    key = str(account.get("id") or account.get("email") or "")
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % AVATAR_TONES


def user_menu(account: Mapping[str, Any], csrf: str, *, operator: bool,
              slots_href: str = "/account", console_href: str = CONSOLE_PATH,
              slots_held: int = 0) -> str:
    """The signed-in person's corner of the bar: an initial that opens a menu.

    No script: a details element is the popover, and its summary is a real
    control, so the keyboard opens it the way it opens anything else. Signing
    out stays a POST carrying this session's token. A link would let any page
    that can make a browser fetch a URL sign people out. The console is
    offered only to an operator, and the console checks again anyway: a link
    is not a permission. ``slots_held`` is this account's own count, shown on
    Your slots, and not shown at all when it is none.
    """
    email = escape(str(account.get("email") or ""))
    tone, initial = _tone(account), escape(_initial(account))

    def face(size: str = "") -> str:
        return f'<span class="avatar t{tone}{size}" aria-hidden="true">{initial}</span>'

    held = f'<span class="menu-pill">{int(slots_held)}</span>' if slots_held > 0 else ""
    links = (f'<a href="{escape(slots_href)}"><span data-copy-zh=\"我的槽位\">Your slots</span>'
        f'{held}</a>')
    if operator:
        links += (f'<a href="{escape(console_href)}"><span data-copy-zh=\"管理后台\">Console</span>'
                  '<span class="menu-pill" data-copy-zh=\"管理员\">operator</span></a>')
    # Shown with a break allowed after the @, so a long address wraps there on a
    # phone rather than mid-word; each part is escaped on its own.
    local, at, domain = str(account.get("email") or "").partition("@")
    shown = escape(local) + (f"@<wbr>{escape(domain)}" if at else "")
    return ('<details class="usermenu">'
            f'<summary aria-label="Account menu for {email}" title="{email}">'
            + face() + "</summary>"
            '<div class="menu"><div class="menu-who">' + face(" big")
            + f"<p><span data-copy-zh=\"当前登录\">Signed in as</span><strong>{shown}</strong>"
                f"</p></div>"
            f'<nav class="menu-links" aria-label="Your account">{links}</nav>'
            '<form method="post" action="/auth/signout">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            '<button type="submit" class="signout" data-copy-zh=\"退出登录\">Sign out</button>'
                '</form></div></details>')


def console_href(cfg: Config) -> str:
    """The console, as a link from the product: its own path with one site,
    the admin host's with two, since there the product has no console."""
    if not cfg.admin_host:
        return CONSOLE_PATH
    scheme = "http" if cfg.public_url.startswith("http://") else "https"
    return f"{scheme}://{cfg.admin_host}{CONSOLE_PATH}"


def product_href(cfg: Config, path: str) -> str:
    """A product page, as a link from the console: the same site with one, the
    product's own address with two, since the console's host has no such page."""
    if not cfg.admin_host or not cfg.public_url:
        return path
    return cfg.public_url.rstrip("/") + path


def render_token_result(node_id: str, token: str, cfg: Config, owner: str = "",
                        corner: str = "") -> str:
    """The minted credential, for as long as the request it belongs to lasts.

    It was minted on the node from the account that node is signed in as and
    rode up on one heartbeat. It stays readable until the request expires or
    somebody says they are done with it, which is what lets a second machine
    have the same token without minting another. Nothing keeps it after that:
    not this server, not the node.
    """
    if not token:
        return (
            _console_open("device token", corner, product_href(cfg, "/"))
            + "<h1>Nothing to show</h1>"
            "<p class=\"sub\">No token is waiting for this node. Either it was finished "
            "with, or the request expired. Start a new one from the fleet page.</p>"
            f"<p><a class=\"back\" href=\"{CONSOLE_PATH}\">&larr; back to the fleet</a></p>"
            "</div></body></html>")
    who = f" for {escape(owner)}" if owner else ""
    return (
        _console_open("device token", corner, product_href(cfg, "/"))
        + f"<h1>Device token{who}</h1>"
        "<p class=\"sub\">Minted on <strong>" + escape(node_id) + "</strong>, from the "
        "account that node is signed in as. Good for one year.</p>"
        "<div class=\"ok-banner\">You can come back and show this again while the "
        "request lasts, so a second machine can have the same token. Press "
        "<strong>Done with it</strong> on the fleet page when you have finished, or "
        "leave it and it expires on its own.</div>"
        f"<pre>{escape(token)}</pre>"
        "<div class=\"card\">"
        "<h2>Put it on a machine</h2>"
        "<p>One command, on any Mac or Linux box. It asks for the token and hides "
        "what you paste.</p>"
        "<pre>bash -c \"$(curl -fsSL https://raw.githubusercontent.com/cdcupt/"
        "ccfleet/main/laptop/ccfleet-connect.sh)\"</pre>"
        "<p class=\"muted\">Run through <code>bash -c</code> rather than piped into a "
        "shell, so the prompt can still read from your terminal &mdash; a pipe would "
        "take the keyboard away from it. It installs itself to "
        "<code>~/.local/bin</code> on the way past.</p>"
        "<p class=\"muted\">Then <code>claude</code> works there on that machine\u2019s own "
        "files, with no login. <code>ccfleet-connect --status</code> checks it, "
        "<code>--remove</code> undoes it, and <code>--off</code> and <code>--on</code> "
        "switch that computer to its own Claude login and back.</p>"
        "<p class=\"note\">Scope is <code>user:inference</code> only, which is Anthropic\u2019s "
        "limit on long-lived tokens, not ours. So it runs Claude Code and it cannot drive "
        "Remote Control \u2014 that needs a full sign-in, which is what the Sign in card does. "
        "Revoke it from the Claude account it belongs to; there is nothing to revoke here, "
        "because nothing here kept it.</p>"
        "</div>"
        f"<p><a class=\"back\" href=\"{CONSOLE_PATH}\">&larr; back to the fleet</a></p>"
        "</div></body></html>")


# Signing in, from the console. The server carries a URL back and a code
# forward; the credential itself is written by the CLI on the node and never
# comes near this process.
LOGIN_WORDS = {
    "requested": "Asking the node\u2026 it checks in every few minutes, so this can "
                 "take a moment. Leave the page open.",
    "url_ready": "Open the link, approve, then paste the code below.",
    "code_sent": "Code sent to the node. Waiting for it to finish\u2026",
}
# The same three steps, said for the flow that ends in a credential you carry
# away rather than one written on the node.
TOKEN_WORDS = {
    # The node reports on a timer, so it can be a few minutes before it even
    # hears the request. A bare "asking..." with no sense of that reads as
    # broken and gets abandoned, which is exactly what happened.
    "requested": "Asking the node\u2026 it checks in every few minutes, so this can "
                 "take a moment. Leave the page open.",
    "ready_note": "Your token is ready \u2014 show it as often as you need while "
                  "this lasts.",
    "url_ready": "Open the link, approve, then paste the code below.",
    "code_sent": "Code sent. Minting the token\u2026",
    "ready": "Your token is ready.",
}


#: Said under the sign-in and device-token cards when shared machines are left out.
SHARED_ELSEWHERE = ("Shared machines are not listed here: whoever holds a slot signs it in, "
                    "and gets device tokens, on their own page.")


def _token_html(rows: list[Mapping[str, Any]], csrf: str,
                logins: Mapping[str, Any], now: float) -> str:
    """Mint a credential for a machine that is not a node.

    The node is already signed in, so it can mint one on request. This card is
    how that is asked for and collected without anybody opening a terminal,
    which was the last thing still requiring SSH. An owner's node only: a
    shared machine's slot gets its tokens on its holder's page.
    """
    shared = any(r.get("machine") for r in rows)
    rows = [r for r in rows if not r.get("machine")]
    if not rows:
        return ""
    items = []
    # A flow needing a person goes first; the rest are just buttons.
    ordered = sorted(rows, key=lambda r: not (logins.get(r["id"]) or {}).get("state"))
    for row in ordered:
        node = escape(row["id"])
        login = logins.get(row["id"]) or {}
        # This card owns only the token flow; a sign-in in flight belongs to the
        # card above and must not be shown twice or cancelled from here.
        state = login.get("state") or "" if login.get("kind") == "token" else ""

        def form(action: str, inner: str, label: str, cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" '
                    f'action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
                    f'<button class="{cls}" type="submit"{admin_attributes(label)}>'
                    f'{escape(label)}</button></form>')

        if not state:
            # Say if one has been issued before. The flow deletes itself when it
            # finishes, so without this the card after a success is identical to
            # the card before you ever started — and someone reasonably wonders
            # whether anything happened.
            issued = row.get("device_token_at")
            if isinstance(issued, (int, float)) and issued > 0:
                said = (f'<span class="pill ok">last issued {escape(_age(now, issued))} '
                        f'ago</span> ')
                label = "Get another"
            else:
                said = '<span class="muted small">for a laptop, desktop or phone</span> '
                label = "Get a device token"
            body = said + form("token-start", "", label)
        elif state == "ready":
            # Shown as often as you like while the attempt lasts, because a
            # second machine needs the same token and minting another for it
            # is a worse answer than reading this one again.
            body = (f'<span class="pill ok">{escape(TOKEN_WORDS["ready"])}</span> '
                    + form("token-show", "", "Show it", cls="primary")
                    + " " + form("token-done", "", "Done with it"))
        else:
            body = f'<span class="login-say">{escape(TOKEN_WORDS.get(state, state))}</span>'
            url = login.get("url") or ""
            if is_login_url(url) and state in ("url_ready", "code_sent"):
                body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer">{escape(url)}</a>')
            if state == "url_ready":
                body += form("login-code",
                             '<input type="text" name="code" placeholder="paste the code" '
                             'autocomplete="off" required>', "Send code")
            body += " " + form("login-cancel", "", "Cancel", cls="danger")
        cls = "row-line stacked" if state and state != "ready" else "row-line"
        items.append(f'<div class="{cls}"><div class="row-name">{node}</div>'
                     f'<div class="actions">{body}</div></div>')
    return ('<h2 id="device-tokens" data-copy-zh=\"设备令牌\">Device tokens</h2><div '
        'class="card">' + "".join(items) +
            '<p class="note">'
            "A device token lets <code>claude</code> run on your own machine, on that "
            "machine's own files, with no login. It is minted on the node from the account "
            "that node is signed in as, and shown here for as long as the request lasts "
            "&mdash; so a second machine can have the same one &mdash; then forgotten. One "
            "year, inference scope &mdash; Anthropic's limit, not ours, which is why it "
            "cannot drive Remote Control."
            + (f" {SHARED_ELSEWHERE}" if shared else "") + "</p></div>")


def _signin_html(rows: list[Mapping[str, Any]], csrf: str,
                 logins: Mapping[str, Any]) -> str:
    """One block per node: start a sign-in, or carry the one in flight forward.

    An owner's node only. A shared machine's root has no Claude Code to sign in;
    its slot is signed in by whoever holds it, on their own page.
    """
    shared = any(r.get("machine") for r in rows)
    rows = [r for r in rows if not r.get("machine")]
    if not rows:
        return ""
    # A sign-in in flight is the only row here anyone has to act on. Settled rows
    # are reference; put the work first rather than making someone find it.
    def own(node_id: str) -> Mapping[str, Any]:
        """The sign-in for this node, or nothing if the row is a token flow.

        One table drives both flows, so each card has to say which rows are
        its own. Without this a token in flight showed up here too, with a
        Cancel button that would kill it from either place.
        """
        login = logins.get(node_id) or {}
        return {} if login.get("kind") == "token" else login

    ordered = sorted(rows, key=lambda r: not own(r["id"]).get("state"))
    items = []
    for row in ordered:
        node = escape(row["id"])
        login = own(row["id"])
        state = login.get("state") or ""

        def form(action: str, inner: str, label: str, cls: str = "", node=node) -> str:
            return (f'<form class="inline" method="post" '
                    f'action="/actions/node/{node}/{action}">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
                    f'<button class="{cls}" type="submit"{admin_attributes(label)}>'
                    f'{escape(label)}</button></form>')

        if not state:
            signed_in = row.get("credentials_present")
            # "signed in" beside a button labelled "Sign in" read as a
            # contradiction. Say the state as a state, and let the button say
            # what pressing it would do to that state.
            status, tone, verb = (("signed in", "ok", "Sign in again") if signed_in else
                                  ("not signed in", "warn", "Sign in") if signed_in is False
                                  else ("unknown", "disabled", "Sign in"))
            field = ("" if signed_in else
                     '<input type="email" name="email" placeholder="email (optional)">')
            body = (f'<span class="pill {tone}">{escape(status)}</span> '
                    + form("login-start", field, verb))
        else:
            body = (f'<span class="login-say">'
                    f'{escape(LOGIN_WORDS.get(state, state))}</span>')
            url = login.get("url") or ""
            # Checked again here: a row written before this rule existed, or by
            # anything but the path above, must still not become a live link.
            if is_login_url(url) and state in ("url_ready", "code_sent"):
                # The node supplied this. It is escaped and its full text is shown,
                # so nobody is asked to trust a link whose target they cannot read.
                body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                         f'rel="noopener noreferrer">{escape(url)}</a>')
            if state == "url_ready":
                body += form("login-code",
                             '<input type="text" name="code" placeholder="paste the code" '
                             'autocomplete="off" required>', "Send code")
            body += " " + form("login-cancel", "", "Cancel", cls="danger")
        # A sign-in in flight carries a sentence, a long URL and two controls.
        # Held on one flex line those wrap into a shape nobody designed, so give
        # it a block of its own and keep the one-line form for settled states.
        cls = "row-line stacked" if state else "row-line"
        items.append(f'<div class="{cls}"><div class="row-name">{node}</div>'
                     f'<div class="actions">{body}</div></div>')
    return ('<h2 id="sign-in" data-copy-zh=\"登录\">Sign in</h2>'
        '<div class="card">' + "".join(items) +
            '<p class="note">'
            "Starting a sign-in runs Claude Code's own login on the node. The credential is "
            "written there and never reaches this server; only the verification URL and the "
            "code you paste pass through, and both are discarded when it finishes."
            + (f" {SHARED_ELSEWHERE}" if shared else "") + "</p></div>")


def _plural(n: Any, word: str) -> str:
    """"1 sessions" is the sort of thing that makes a page look unfinished."""
    count = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _human_tokens(n: Any) -> str:
    """Token counts run to millions; a raw integer is unreadable at a glance."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n <= 0:
        return "0"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit:
            trimmed = n / limit
            return f"{trimmed:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _sparkline(series: list[Mapping[str, Any]], width: int = 240, height: int = 38,
               unit: str = "day") -> str:
    """Tokens over time as a filled area. No script, no library, sized by viewBox."""
    points = [p for p in series if isinstance(p.get("tokens"), (int, float))]
    if not points:
        return '<span class="muted">no activity yet</span>'
    values = [float(p["tokens"]) for p in points]
    if len(values) < 2:
        # One day is not a trend. Drawn, it became a flat line across the whole
        # frame, which reads as a full bar rather than as a single reading.
        return '<span class="muted">one day so far</span>'
    peak = max(values) or 1.0
    step = width / max(len(values) - 1, 1)
    # y is inverted: SVG grows downward, a chart grows upward. A flat series has
    # no shape to show, and normalising it against its own peak would pin it to
    # the top of the frame — the one height that implies a maximum. Sit it in
    # the middle instead, where it reads as "unvarying" and not as "full".
    flat = peak == min(values)
    coords = [(i * step, height - 3 - (0.5 if flat else v / peak) * (height - 8))
              for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"0,{height} " + line + f" {coords[-1][0]:.1f},{height}"
    last_x, last_y = coords[-1]
    label = (f"{len(values)} {unit}(s) of token use, peak {_human_tokens(peak)}, "
             f"latest {_human_tokens(values[-1])}")
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(label)}" preserveAspectRatio="none">'
            # A baseline gives the area something to sit on, so a quiet week
            # reads as low rather than as a chart that failed to draw.
            f'<line class="spark-base" x1="0" y1="{height - 0.5}" '
            f'x2="{width}" y2="{height - 0.5}"/>'
            f'<polygon class="spark-fill" points="{area}"/>'
            f'<polyline class="spark-line" points="{line}"/>'
            f'<circle class="spark-dot" cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5"/></svg>')


def _reset_foot(text: Any, read_at: Any, now: Optional[float], exact: Any = None) -> str:
    """When a window resets: in the viewer's own zone where the moment is
    known (see LOCAL_TIMES_JS), and as Claude Code printed it where not.

    `exact` is the moment itself, which the agent reads from Claude Code's own
    saved reading; the printed words are placed only without it.
    """
    at = exact if (isinstance(exact, (int, float)) and not isinstance(exact, bool)
                   and now is not None) else None
    if at is None and not text:
        return ""
    readable = (isinstance(read_at, (int, float)) and not isinstance(read_at, bool)
                and now is not None)
    if at is None:
        at = resets.reset_at(text, float(read_at)) if readable else None
    if at is None:
        return f"resets {escape(str(text))}"
    return (f'resets <time datetime="{escape(resets.iso(at))}" data-local>'
            f"{escape(resets.until(now, at))}</time>")


def _meter(used: Any, label: str, resets: Any, read_at: Any = None,
           now: Optional[float] = None, resets_at: Any = None) -> str:
    """One quota window as a labelled bar.

    Colour carries the same meaning as everywhere else on this page: fine,
    getting close, nearly out. A bare number makes you do that comparison
    yourself, every time you look. ``read_at`` is when the window was read,
    which a reset time without a date needs to be placed.
    """
    if (not isinstance(used, (int, float)) or isinstance(used, bool)
            or not math.isfinite(used)):
        return ""
    pct = max(0.0, min(100.0, float(used)))
    level = "crit" if pct >= 90 else "warn" if pct >= 75 else "ok"
    foot = _reset_foot(resets, read_at, now, resets_at)
    return (f'<div class="meter"><div class="meter-head">'
            f'<span>{escape(label)}</span>'
            f'<span class="meter-pct">{pct:.0f}%</span></div>'
            f'<div class="meter-track"><i class="meter-fill {level}" '
            f'style="width:{pct:.0f}%"></i></div>'
            f'<div class="meter-foot">{foot}</div></div>')


#: How long a usage read asked for is said to be under way; a slot tries it
#: once, within a minute or two, so a request this old has had its answer.
QUOTA_READ_WAIT_S = 5 * 60
QUOTA_READ_FAILURES = frozenset({
    "native_refreshed", "native_refresh_not_due", "native_auth_rejected",
    "native_login_expired", "native_network_error", "native_rate_limited",
    "native_timeout", "native_launch_failed", "native_probe_busy",
    "native_refresh_unconfirmed", "native_auth_source", "native_extensions",
})


def quota_refresh_completed(wanted_at: Any, refresh: Any, now: float) -> bool:
    """Only a matching closed, completed acknowledgement can end a pending read."""
    if (not isinstance(refresh, Mapping)
            or set(refresh) != {"requested_at", "attempted_at", "outcome"}
            or type(wanted_at) not in (int, float) or not math.isfinite(wanted_at)):
        return False
    requested, attempted = refresh.get("requested_at"), refresh.get("attempted_at")
    return (type(requested) in (int, float) and type(attempted) in (int, float)
            and math.isfinite(requested) and math.isfinite(attempted)
            and requested == wanted_at and 0 < requested <= attempted <= now + 60
            and type(refresh.get("outcome")) is str
            and refresh["outcome"] in QUOTA_READ_FAILURES | {"quota_read"})


def quota_refresh_failed(wanted_at: Any, refresh: Any, now: float) -> bool:
    return (quota_refresh_completed(wanted_at, refresh, now)
            and refresh["outcome"] != "quota_read")


def quota_reading(wanted_at: Any, checked_at: Any, now: float, refresh: Any = None) -> bool:
    """Whether a read asked for from a page is still to be answered."""
    if (quota_refresh_completed(wanted_at, refresh, now)
            or type(wanted_at) not in (int, float) or not math.isfinite(wanted_at)
            or not 0 <= now - wanted_at < QUOTA_READ_WAIT_S):
        return False
    read = (checked_at if type(checked_at) in (int, float)
            and math.isfinite(checked_at) else 0)
    return wanted_at > read


def _quota_notice(en: str, zh: str, cls: str = "muted small") -> str:
    return f'<p class="{cls}">' + bi(en, zh, inline=True) + '</p>'


def _quota_refresh(row: Mapping[str, Any], checked: Any, now: float, csrf: str) -> str:
    """Request once, then report the machine's result without restamping its data."""
    slot_id = row.get("slot_id")
    if not csrf or not slot_id:
        return ""
    if row.get('slot_state') not in (None, slotstates.ACTIVE):
        return _quota_notice('Quota refresh is available once the slot is in use.',
                              '槽位开始使用后即可刷新额度。')
    wanted = row.get("quota_wanted_at")
    acknowledgement = (row.get("quota") or {}).get("refresh")
    form = (f'<form class="inline" method="post" '
            f'action="/actions/slot/{escape(slot_id)}/quota" data-fleet-action="admin-quota">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            '<button type="submit">' + bi_label('Refresh', '刷新', inline=True)
            + '</button></form>')
    if quota_reading(wanted, checked, now, acknowledgement):
        return ('<p class="muted small" role="status" data-admin-quota-pending="'
                + escape(slot_id) + '">' + bi_label(
                    'Reading now… This panel checks for a result for up to five minutes.',
                    '正在读取… 此面板会在五分钟内检查结果。', inline=True) + '</p>'
                + form.replace('<button type="submit">', '<button type="submit" disabled>'))
    known = type(checked) in (int, float) and math.isfinite(checked) and 0 < checked <= now
    requested = type(wanted) in (int, float) and math.isfinite(wanted) and 0 < wanted <= now
    completed = quota_refresh_completed(wanted, acknowledgement, now)
    unanswered = requested and (not known or wanted > checked)
    failed = unanswered and (quota_refresh_failed(wanted, acknowledgement, now)
                            or not completed and now - wanted >= QUOTA_READ_WAIT_S)
    result = (_quota_notice(
        'No new quota reading was received. The machine may be busy or unable to read '
        'Claude usage. Missing data does not mean quota remains.',
        '未收到新的额度读数。机器可能繁忙或无法读取 Claude 用量；没有读数不代表还有剩余额度。',
        'small lapsed') if failed else '')
    if completed and not failed and known and requested and checked < wanted:
        result += _quota_notice(
            'Claude returned its latest available native reading; its actual reading time '
            'is shown above and may be earlier than this request.',
            'Claude 返回了原版客户端最新可用的读数；上方保留真实读取时间，可能早于本次请求。')
    return result + form


def _quota_html(row: Mapping[str, Any], now: float, csrf: str = "") -> str:
    """Keep historical account readings visibly distinct from current availability."""
    quota = row.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    checked = quota.get("checked_at")
    known = (type(checked) in (int, float) and math.isfinite(checked)
             and 0 < checked <= now)
    since = row.get('usage_since', 0)
    previous_account = (known and type(since) in (int, float)
                        and math.isfinite(since) and checked < since)
    if previous_account:
        session, week, checked, known = {}, {}, None, False
    historical = not known or now - checked > 10 * 60
    region = (f'<div id="admin-quota-{escape(row["slot_id"])}" data-fleet-quota '
              'data-fleet-readonly>' if row.get("slot_id") else '<div>')
    bars = ''
    if previous_account:
        bars += _quota_notice('Waiting for a quota reading for the current account.',
                              '正在等待当前账号的额度读数。')
    elif not session and not week:
        bars += _quota_notice('No window reading yet. Current quota is unknown.',
                              '尚无窗口读数，当前额度未知。')
    elif historical:
        bars += _quota_notice('<strong>Historical reading · current quota unknown.</strong>',
                              '<strong>历史读数 · 当前额度未知。</strong>', 'small lapsed')
    bars += '<div class="usage-nums muted">' + bi_label(
        'Claude account · every device · reported usage',
        'Claude 账号 · 所有设备合计 · 上报用量', inline=True) + '</div>'
    for window, en, zh in ((session, '5-hour session', '五小时窗口'),
                           (week, 'This week', '本周窗口')):
        meter = _meter(window.get('used_pct'), en, window.get('resets'),
                       checked if known else None, now, window.get('resets_at'))
        if meter:
            meter = meter.replace(f'<span>{en}</span>', '<span>' + bi_label(en, zh, inline=True)
                                  + '</span>').replace('<div class="meter-foot">resets ',
                '<div class="meter-foot">' + bi_label('resets', '重置时间', inline=True) + ' ')
        else:
            meter = '<div class="meter">' + bi_label(en, zh, inline=True) + _quota_notice(
                'Current quota is unavailable. This does not mean quota remains.',
                '当前额度未知，不代表还有剩余额度。') + '</div>'
        bars += meter
    if known:
        age = escape(_age(now, checked))
        bars += _quota_notice(f'read {age} ago', f'最近读取：{age} 前')
    return (region + ('<div class="quota-historical">' if historical else '<div>')
            + bars + '</div>' + _quota_refresh(row, checked, now, csrf) + '</div>')


def _admin_relay_usage(row: Mapping[str, Any], now: float) -> str:
    report = relay_report(row.get('relay'), now)
    if report is None or report['observed_at'] < row.get('usage_since', 0):
        return _quota_notice('Gateway response usage: not reported; total unknown.',
                              '网关响应用量：尚未上报，总数未知。')
    observed = report['observed_at']
    age = escape(_age(now, min(observed, now)))
    heard = row.get('last_seen_ts')
    fresh = (type(heard) in (int, float) and now - 15 * 60 <= heard <= now + 60
             and observed >= now - 15 * 60)
    text = _quota_notice(
        f'Gateway responses · seven UTC calendar days · observed {age} ago',
        f'网关响应 · 过去七个 UTC 日 · {age} 前观测')
    if not fresh:
        text += _quota_notice('Historical report; current totals unknown.',
                              '历史报告，当前总数未知。', 'small lapsed')
    usage = report.get('token_usage')
    if not usage or not usage['samples']:
        return text + _quota_notice('Response token totals unknown: no complete usage samples.',
                                    '没有完整用量样本，响应 Token 总数未知。')
    text += _quota_notice(
        f"<strong>{usage['input_tokens']:,}</strong> input · "
        f"<strong>{usage['output_tokens']:,}</strong> output tokens",
        f"输入 <strong>{usage['input_tokens']:,}</strong> · "
        f"输出 <strong>{usage['output_tokens']:,}</strong> Token")
    for prefix, en, zh in (('cache_read', 'Cache-read input', '缓存读取输入'),
                           ('cache_creation', 'Cache-write input', '缓存写入输入')):
        count = usage[prefix + '_samples']
        text += _quota_notice(
            f"{en}: {usage[prefix + '_input_tokens']:,} tokens ({count} samples)" if count
            else f'{en}: unknown (no samples)',
            f"{zh}：{usage[prefix + '_input_tokens']:,} Token（{count} 个样本）" if count
            else f'{zh}：未知（没有样本）')
    return text + _quota_notice(
        f"Coverage: {usage['samples']} complete samples / {usage['eligible']} eligible "
        'responses. Missing usage is not zero. These observed sums are not account quota.',
        f"覆盖：{usage['samples']} 个完整样本 / {usage['eligible']} 个可统计响应。"
        '缺失用量不是零；观测总数不是账号额度。')


def _usage_html(rows: list[Mapping[str, Any]], now: float, csrf: str = "") -> str:
    """Separate account limits, gateway responses and on-slot transcript counts."""
    def reporting(row: Mapping[str, Any]) -> bool:
        return bool(row.get('slot_id')
                    and row.get('slot_state') in (slotstates.CLAIMED, slotstates.ACTIVE)
                    or (row.get('usage') or {}).get('total_tokens') or row.get('quota')
                    or relay_report(row.get('relay'), now))

    live = [r for r in rows if reporting(r)]
    if not live:
        return ""
    quiet = [_called(r)[0] for r in rows if not reporting(r)]
    tail = ""
    if quiet:
        names = ", ".join(escape(str(q)) for q in quiet[:8])
        more = f" and {len(quiet) - 8} more" if len(quiet) > 8 else ""
        tail = _quota_notice(f'No usage reported yet from {names}{more}.',
                              f'以下机器尚未上报用量：{names}{more}。')
    items = []
    for row in live:
        usage = row.get('usage') or {}
        shown, beside = _called(row)
        by = f'<span class="muted"> &middot; {escape(beside)}</span>' if beside else ''
        transcript = _quota_notice('Transcript usage: not reported; total unknown.',
                                   '原版对话记录用量：尚未上报，总数未知。')
        chart = ''
        if usage:
            total = usage.get('total_tokens')
            if type(total) in (int, float) and math.isfinite(total):
                transcript = _quota_notice(
                    f'<b>{escape(_human_tokens(total))}</b> transcript tokens on this '
                    f'{"slot" if row.get("machine") else "node"}, last '
                    f'{escape(_usage_span(usage))} · '
                    f'{escape(_plural(usage.get("sessions") or 0, "session"))}',
                    f'<b>{escape(_human_tokens(total))}</b> Token · 此机器过去 '
                    f'{escape(_usage_span(usage))} 的原版对话记录 · '
                    f'{escape(str(usage.get("sessions") or 0))} 个会话')
                spark, caption = _usage_chart(usage)
                chart = spark + caption
        transcript += _quota_notice(
            'Transcript counts exclude gateway requests from local clients. '
            'Zero transcript tokens does not mean zero account usage.',
            '原版对话记录不包含本机客户端经网关发出的请求；记录为零不代表账号没有使用。')
        items.append('<div class="usage-row"><div class="usage-name">'
                     + '<h3 class="usage-slot-title">' + escape(shown) + by + '</h3>'
                     + _admin_relay_usage(row, now)
                     + '<details><summary>' + bi_label('On-slot transcripts', '槽位原版记录',
                        inline=True) + '</summary>' + transcript + chart + '</details></div>'
                     + '<div class="usage-quota">' + _quota_html(row, now, csrf)
                     + '</div></div>')
    return ('<h2 id="usage">' + bi_label('Usage and quota', '用量与额度', inline=True)
            + '</h2><div class="card">' + ''.join(items) + tail + _quota_notice(
                'Quota windows come from <code>/usage</code> in Claude Code and cover the '
                'whole Claude account on every device. Gateway counts include only validated '
                'completed response samples. Transcript counts cover only the node or slot. '
                'Conversation content never leaves the node in these numeric reports.',
                '额度窗口来自 Claude Code 的 <code>/usage</code>，包含账号在所有设备的用量。'
                '网关统计仅包含已验证的完整响应样本；对话记录统计仅覆盖当前机器或槽位。'
                '这些数字报告不包含对话内容。', 'note') + '</div>')


def _usage_span(usage: Mapping[str, Any]) -> str:
    """How far back the token count reaches, in days: "7 days"."""
    hours, days = usage.get("window_hours"), usage.get("window_days")
    count = int(hours // 24) if hours else int(days) if days else 0
    return _plural(count, "day") if count else "?"


def _usage_chart(usage: Mapping[str, Any]) -> tuple[str, str]:
    """The chart and its caption. Hourly from agents that report it, daily
    from older ones, and a plain sentence when the window holds nothing."""
    hourly = usage.get("by_hour")
    if isinstance(hourly, Mapping) and hourly.get("tokens"):
        if not usage.get("total_tokens"):
            # A flat line along the floor reads as a chart that failed to draw.
            return ('<span class="muted">nothing on this node in the last '
                    f'{escape(_usage_span(usage))}</span>'), ""
        spark = _sparkline([{"tokens": v} for v in hourly["tokens"]], unit="hour")
        unit = "per hour"
    else:
        spark = _sparkline(usage.get("by_day") or [])
        unit = "per day"
    # The caption names the axes of a chart. With no chart drawn it sat under a
    # sentence, captioning nothing.
    caption = (f'<div class="usage-nums muted">tokens {unit}, last '
               f'{escape(_usage_span(usage))}</div>' if "<svg" in spark else "")
    return spark, caption


TILES = (("ok", "healthy"), ("warn", "warning"), ("critical", "critical"),
         ("disabled", "disabled"))


def _strip_html(counts: Mapping[str, int]) -> str:
    """The fleet in one glance, before any detail.

    A count buried in a sentence ("ok 2") makes you read to find out whether
    anything is wrong. Four tiles answer that from across the room, and a zero
    stays quiet rather than competing with the number that matters.
    """
    tiles = []
    for key, label in TILES:
        n = int(counts.get(key) or 0)
        cls = f"tile {key}" if n else "tile zero"
        tiles.append(f'<div class="{cls}"><b>{n}</b><span>{admin_label(label)}</span></div>')
    return f'<div class="strip">{"".join(tiles)}</div>'


def render_dashboard(rows: list[Mapping[str, Any]], alerts: list[Mapping[str, Any]],
                     now: float, cfg: Config, csrf: str = "", who: Any = None,
                     logins: Optional[Mapping[str, Any]] = None, extra: str = "",
                     corner: str = "") -> str:
    # who is None for callers that predate per-user accounts, which are all
    # operator-side, so the default is the full-privilege view.
    is_admin = who is None or getattr(who, "is_admin", True)
    # Disabled, empty shared machines are retained operational records, not
    # current inventory. Keep owned or unfinished slots visible for recovery.
    retired = {row["id"] for row in rows
               if row.get("machine") and row.get("enabled") is False
               and (row.get("slot_count") == 0
                    or (row.get("slot_count") == 1
                        and row.get("slot_state") == slotstates.FREE))}
    rows = [row for row in rows if row["id"] not in retired]
    alerts = [alert for alert in alerts if alert.get("node_id") not in retired]
    logins = {node: login for node, login in (logins or {}).items() if node not in retired}
    empty = ("No nodes yet. Use the form below." if is_admin else
             "No nodes are assigned to you yet. Your operator adds them.")
    body_rows = "".join(_row_html(r, now) for r in rows) or (
        f'<tr><td colspan="9" class="muted">{admin_label(empty)}</td></tr>')
    # An alert is about a node: say it by the name that node goes by.
    called = {r["id"]: _called(r)[0] for r in rows}
    alert_items = "".join(
        f'<div class="alert">{_pill(a["level"])}'
        f'<span class="alert-rule">{escape(called.get(a["node_id"], a["node_id"]))} · '
        f'{escape(a["rule"])}</span>'
        f'<span class="alert-msg">{escape(a["message"])}</span>'
        f'<span class="muted small">{escape(_age(now, a["opened_at"]))} ago</span></div>'
        for a in alerts) or (
        '<p class="quiet" data-copy-zh=\"当前没有告警，'
            '所有节点均在阈值范围内。\">Nothing open. Every '
            'node is inside its thresholds.</p>')
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    # How often to come back, decided by what the page is currently showing.
    #
    # Idle, a minute is plenty. Mid-flow it is not: a step completes on the node
    # in seconds and then sits unseen for the rest of the minute, which reads as
    # nothing happening. And while someone is being asked to paste a code, any
    # reload at all lands mid-typing and throws away what they had — that is the
    # one state where nothing can arrive anyway, so there is nothing to fetch
    # and everything to lose.
    #
    # Say which of the three the page is doing, so a reload that does not come
    # is a stated choice rather than something that looks broken.
    states = {(login or {}).get("state") for login in (logins or {}).values()}
    if "url_ready" in states:
        every, cadence = None, "waiting for you to paste a code"
    elif states & {"requested", "code_sent"}:
        every, cadence = ACTIVE_REFRESH_S, "keeping up with a sign-in"
    else:
        every, cadence = IDLE_REFRESH_S, "status updates keep your open forms"
    # Always to the console's own address, never the one the page was opened at.
    # After an action that address ends in #<card>, and a refresh naming no
    # address on a page whose address has a fragment is a fragment navigation:
    # the browser scrolls and reloads nothing, so the page never refreshed after
    # a press at all. The target never carries a fragment either, for the same
    # reason: from /admin#x a refresh to /admin#x would stick the same way.
    auto_refresh = ("" if every is None or every == IDLE_REFRESH_S else
                    f'<meta http-equiv="refresh" content="{every};url={CONSOLE_PATH}">')
    whoami = ""
    if who is not None:
        whoami = (" · " + bi_label("signed in as", "当前登录", inline=True)
                  + f" <strong>{escape(str(getattr(who, 'label', '')))}</strong>"
                  + ("" if is_admin else " · " + bi_label(
                      "showing only your nodes", "仅显示您的节点", inline=True)))
    diagnostics = ('<h2 id="machine-diagnostics" data-copy-zh=\"机器诊断\">Machine diagnostics</h2>'
        '<div class="wrap"><table><thead><tr><th data-copy-zh=\"状态\">Status</th><th '
            'data-copy-zh=\"节点\">Node</th><th data-copy-zh=\"最近在线\">Last seen</th>'
        '<th>Claude Code</th><th data-copy-zh=\"出口 IP\">Egress IP</th><th '
            'data-copy-zh=\"磁盘\">Disk</th><th data-copy-zh=\"负载\">Load</th><th '
            'data-copy-zh=\"Claude 登录\">Claude sign-in</th>'
        '<th>Remote Control</th></tr></thead>'
        f'<tbody id="admin-machine-readings" data-fleet-readonly>{body_rows}</tbody></table></div>'
        + _usage_html(rows, now, csrf if is_admin else ''))
    if is_admin:
        diagnostics = ('<details class="admin-advanced"><summary data-copy-zh=\"诊断与用量\">'
            'Diagnostics &amp; usage</summary>'
                       + diagnostics + '</details>')
    management = _manage_html(rows, csrf) + _add_form(csrf) if csrf and is_admin else ''
    if management:
        management = ('<details class="admin-advanced"><summary data-copy-zh=\"高级机器控制\">'
            'Advanced machine controls</summary>'
                      + management + '</details>')
    heading = 'Fleet admin' if is_admin else 'Your machines'
    introduction = ('Manage accounts, credit and slots.' if is_admin
                    else 'Check your machines and sign-in status.')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{auto_refresh}<title>ccfleet console</title>'
        f'<link rel="icon" href="{FAVICON}">'
        f"<style>{CSS}{bilingual.CSS}</style></head><body class=\"console\">"
        + _console_bar(corner, product_href(cfg, "/")) + "<div class=\"page\">"
        '<header class="mast"><div>'
        f'<h1>{admin_label(heading)}</h1><p class="sub">{admin_label(introduction)}</p>'
        f'<p class="sub small">{admin_label(cadence.capitalize())}{whoami}</p></div>'
        + '<div id="admin-health-counts" data-fleet-readonly>' + _strip_html(counts) + '</div>' +
        "</header>" + extra
        + '<p><a href="/admin" data-fleet-status="admin" data-copy-zh=\"刷新状态\">Refresh '
            'status</a></p>'
        + '<h2 data-copy-zh=\"当前告警\">Open alerts</h2><div class="card" '
            'id="admin-alerts" data-fleet-readonly>'
        + alert_items + '</div>'
        # Management is the operator's. An owner sees their nodes and nothing to
        # press, which is why they get no CSRF token either: there is no form.
        # The sign-in card belongs to whoever owns the node, admin or not: needing
        # the operator to sign you in would only move the bottleneck.
        + diagnostics
        + (_signin_html(rows, csrf, logins or {}) if csrf else "")
        + (_token_html(rows, csrf, logins or {}, now) if csrf else "")
        + management
        + "</div>" + LOCAL_TIMES_TAG + PANEL_ACTION_TAG + ADMIN_QUOTA_POLL_TAG
        + bilingual.SCRIPT_TAG + "</body></html>"
    )
