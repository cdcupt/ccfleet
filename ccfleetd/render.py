"""Server-rendered fleet dashboard. Every value is HTML-escaped; no scripts."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from html import escape
from shlex import quote as shq
from typing import Any, Optional

from . import names as slotnames
from . import resets
from . import slots as slotstates
from .config import Config
from .desired import is_channel, is_login_url

LEVEL_ORDER = {"ok": 0, "warn": 1, "critical": 2}

#: Says a reset time in the viewer's own time zone. The page carries the instant
#: (<time datetime> from resets.iso) and, as text, how long until then, which
#: needs no zone at all and is what shows if this does not run. The one script
#: the site runs, allowed by its hash (LOCAL_TIMES_CSP) and nothing else.
LOCAL_TIMES_JS = (
    'document.querySelectorAll("time[data-local]").forEach(function(t){'
    'var d=new Date(t.dateTime);if(isNaN(d))return;'
    'var o={hour:"numeric",minute:"2-digit",timeZoneName:"short"};'
    'if(d-Date.now()>864e5)o.weekday="short";'
    't.textContent=d.toLocaleString(undefined,o)+" ("+t.textContent+")";});')
LOCAL_TIMES_TAG = f"<script>{LOCAL_TIMES_JS}</script>"
LOCAL_TIMES_CSP = ("'sha256-" + base64.b64encode(
    hashlib.sha256(LOCAL_TIMES_JS.encode("utf-8")).digest()).decode("ascii") + "'")

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
grid-template-columns:minmax(150px,.9fr) minmax(200px,1fr) minmax(190px,1.1fr);
gap:16px 22px;align-items:start;padding:16px 0;border-bottom:1px solid var(--rule-soft)}
.usage-row:last-of-type{border-bottom:0}
.usage-name{font-family:var(--mono);font-weight:650;font-size:13.5px;min-width:0;
overflow-wrap:anywhere}
.usage-name span{font-family:var(--sans);font-weight:400}
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
    if ts_ms is None:
        return "-"
    delta = ts_ms / 1000.0 - now
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


def build_rows(nodes: list[Mapping[str, Any]], latest: Mapping[str, Mapping[str, Any]],
               alerts: list[Mapping[str, Any]], now: float,
               slots: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
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
            "owner": node["owner"], "region": node["region"],
            "enabled": node["enabled"], "status": level if node["enabled"] else "disabled",
            "last_seen_ts": (hb or {}).get("ts"),
            "hostname": payload.get("hostname"),
            "claude_version": (facts.get("claude") or {}).get("version"),
            "pinned_version": node["pinned_version"],
            "egress_ip": (payload.get("egress") or {}).get("ip"),
            "disk_used_pct": (payload.get("disk") or {}).get("used_pct"),
            "load1": (payload.get("load") or {}).get("1"),
            "device_token_at": node.get("device_token_at"),
            "credentials_present": creds.get("present"),
            "credentials_mtime": creds.get("mtime"),
            "token_expires_at": creds.get("expires_at"),
            "subscription_type": creds.get("subscription_type"),
            "remote_control": (facts.get("remote_control") or {}).get("state"),
            "rc_expected": node["rc_expected"],
            # What the node did about its pin last time it was asked. A silent
            # reconcile is indistinguishable from one that never ran.
            "last_upgrade": ((payload.get("reconcile") or {}).get("upgrade") or None),
            # The OS asked for a reboot (a kernel, a libc). A badge, not an alert:
            # it is the operator's to schedule, and nothing is broken meanwhile.
            "reboot_required": payload.get("reboot_required") is True,
            "usage": facts.get("usage") or {},
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
                f'<button class="{cls}" type="submit">{escape(label)}</button></form>')
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
    """The name a node goes by, and where it is when that is not its id.

    A shared machine goes by its slot's name, which is its hostname and what
    claude.ai shows; its id stays the operator's handle for it on the server.
    Returned unescaped.
    """
    shown = row.get("name") or row["id"]
    return shown, (f"on {row['id']} \u00b7 " if shown != row["id"] else "")


def _row_html(row: Mapping[str, Any], now: float) -> str:
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
    creds = row["credentials_present"]
    cred_text = ("unknown" if creds is None else ("missing" if creds is False else
                 f"refreshed {_age(now, row['credentials_mtime'])} ago"))
    if row.get("machine"):
        count = row.get("slot_count") or 0
        cred_text = ("no slot yet" if count == 0 else
                     f"{count} slots, see Slots" if count > 1 else
                     "not signed in" if creds is False else cred_text)
    if row["subscription_type"]:
        # The plan is a label on the login, not a qualifier on the time. Trailing
        # it read as "refreshed 10m ago (max)", where (max) looks like it modifies
        # the age; leading it reads as what it is.
        cred_text = f"{row['subscription_type']} \u00b7 {cred_text}"
    rc = row["remote_control"] or "-"
    # Nothing is expected of a node that is switched off, so saying so is noise;
    # nor of a shared machine, whose Remote Control is its slot's to run.
    if row["rc_expected"] and row["enabled"] and not row.get("machine"):
        rc += " (expected)"
    shown, where = _called(row)
    return (
        f'<tr class="r-{escape(row["status"])}">'
        f"<td>{_pill(row['status'])}</td>"
        f'<td><span class="node-id">{escape(shown)}</span>'
        + (' <span class="pill warn">reboot needed</span>' if row.get("reboot_required") else "")
        + f"<br><span class=\"muted\">{escape(where)}{escape(row['owner'])}"
        f" · {escape(row['region'] or '-')}</span></td>"
        f"<td class=\"num\">{escape(_age(now, row['last_seen_ts']))}</td>"
        f'<td class="v">{version}</td>'
        f"<td><code>{_fmt(row['egress_ip'])}</code></td>"
        f"<td class=\"num\">{_fmt(row['disk_used_pct'], '%')}</td>"
        f"<td class=\"num\">{_fmt(row['load1'])}</td>"
        f"<td>{escape(cred_text)}<br><span class=\"muted\">token "
        f"{escape(_in(now, row['token_expires_at']))}</span></td>"
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
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

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
        shown, where = _called(row)
        items.append(f'<div class="row-line"><div class="row-name">{escape(shown)}'
                     f'<span class="muted"> · {escape(where)}{escape(row["owner"])}</span></div>'
                     f'<div class="actions">{"".join(buttons)}</div></div>')
    return ('<h2 id="manage">Manage nodes</h2><div class="card">' + "".join(items) +
            '<p class="note">'
            "New token replaces the node's credential immediately, so update the node after. "
            "Remove deletes its history and cannot be undone.</p></div>")


def _add_form(csrf: str) -> str:
    return (
        '<h2 id="add-node">Add a node</h2><div class="card form">'
        '<form method="post" action="/actions/node/add">'
        f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
        '<div class="fields">'
        '<div><label for="node_id">Node name</label>'
        '<input type="text" id="node_id" name="node_id" placeholder="laptop-erik" required '
        'pattern="[a-z0-9][a-z0-9-]{1,39}" title="lowercase letters, digits and hyphens"></div>'
        '<div><label for="owner">Owner</label>'
        '<input type="text" id="owner" name="owner" placeholder="erik" required></div>'
        '<div><label for="region">Region</label>'
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
            '<span class="tag">console</span>'
            f'<div class="topbar-end">{corner}</div></div></header>')


def _console_open(title: str, corner: str = "", home: str = "/") -> str:
    """The head and the bar of a console page that is not the dashboard."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>ccfleet · {escape(title)}</title>"
            f'<link rel="icon" href="{FAVICON}">'
            f"<style>{CSS}</style></head><body class=\"console\">"
            + _console_bar(corner, home) + "<div class=\"page\">")


# -- who is looking -------------------------------------------------------------------

#: The bar's corner for somebody not signed in: the way to their slots, which
#: begins by signing in.
SIGN_IN_LINK = '<a class="btn primary" href="/account">Sign in</a>'
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
    links = f'<a href="{escape(slots_href)}"><span>Your slots</span>{held}</a>'
    if operator:
        links += (f'<a href="{escape(console_href)}"><span>Console</span>'
                  '<span class="menu-pill">operator</span></a>')
    # Shown with a break allowed after the @, so a long address wraps there on a
    # phone rather than mid-word; each part is escaped on its own.
    local, at, domain = str(account.get("email") or "").partition("@")
    shown = escape(local) + (f"@<wbr>{escape(domain)}" if at else "")
    return ('<details class="usermenu">'
            f'<summary aria-label="Account menu for {email}" title="{email}">'
            + face() + "</summary>"
            '<div class="menu"><div class="menu-who">' + face(" big")
            + f"<p><span>Signed in as</span><strong>{shown}</strong></p></div>"
            f'<nav class="menu-links" aria-label="Your account">{links}</nav>'
            '<form method="post" action="/auth/signout">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
            '<button type="submit" class="signout">Sign out</button></form></div></details>')


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
        "<code>--remove</code> undoes it.</p>"
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
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

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
    return ('<h2 id="device-tokens">Device tokens</h2><div class="card">' + "".join(items) +
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
                    f'<button class="{cls}" type="submit">{escape(label)}</button></form>')

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
    return ('<h2 id="sign-in">Sign in</h2><div class="card">' + "".join(items) +
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


def _reset_foot(text: Any, read_at: Any, now: Optional[float]) -> str:
    """When a window resets: in the viewer's own zone where the words can be
    read (see LOCAL_TIMES_JS), and as Claude Code printed them where not."""
    if not text:
        return ""
    readable = (isinstance(read_at, (int, float)) and not isinstance(read_at, bool)
                and now is not None)
    at = resets.reset_at(text, float(read_at)) if readable else None
    if at is None:
        return f"resets {escape(str(text))}"
    return (f'resets <time datetime="{escape(resets.iso(at))}" data-local>'
            f"{escape(resets.until(now, at))}</time>")


def _meter(used: Any, label: str, resets: Any, read_at: Any = None,
           now: Optional[float] = None) -> str:
    """One quota window as a labelled bar.

    Colour carries the same meaning as everywhere else on this page: fine,
    getting close, nearly out. A bare number makes you do that comparison
    yourself, every time you look. ``read_at`` is when the window was read,
    which a reset time without a date needs to be placed.
    """
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return ""
    pct = max(0.0, min(100.0, float(used)))
    level = "crit" if pct >= 90 else "warn" if pct >= 75 else "ok"
    foot = _reset_foot(resets, read_at, now)
    return (f'<div class="meter"><div class="meter-head">'
            f'<span>{escape(label)}</span>'
            f'<span class="meter-pct">{pct:.0f}%</span></div>'
            f'<div class="meter-track"><i class="meter-fill {level}" '
            f'style="width:{pct:.0f}%"></i></div>'
            f'<div class="meter-foot">{foot}</div></div>')


def _quota_html(row: Mapping[str, Any], now: float) -> str:
    """The two windows an owner actually asks about: this session, this week.

    These belong to the Claude account, not to the node: every device signed
    in as that account spends them. Said above the bars, because beside a
    per-node token count they read as if they were the same thing, and the
    count then looks stuck while the bars move.
    """
    quota = row.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    if not session and not week:
        return '<p class="muted small">No window reading yet.</p>'
    checked = quota.get("checked_at")
    bars = ('<div class="usage-nums muted">Claude account &middot; every device</div>'
            + _meter(session.get("used_pct"), "5-hour session", session.get("resets"),
                     checked, now)
            + _meter(week.get("used_pct"), "This week", week.get("resets"), checked, now))
    if isinstance(checked, (int, float)) and not isinstance(checked, bool):
        bars += f'<p class="muted small">read {escape(_age(now, checked))} ago</p>'
    return bars


def _usage_html(rows: list[Mapping[str, Any]], now: float) -> str:
    """Per-node, per-account token use, counted from transcripts on each node."""
    def reporting(row: Mapping[str, Any]) -> bool:
        return bool((row.get("usage") or {}).get("total_tokens") or (row.get("quota") or {}))

    live = [r for r in rows if reporting(r)]
    if not live:
        return ""
    # A row of dashes says nothing an operator can act on, and four of them bury
    # the two that matter. Name the quiet nodes in one line instead.
    quiet = [_called(r)[0] for r in rows if not reporting(r)]
    tail = ""
    if quiet:
        names = ", ".join(escape(str(q)) for q in quiet[:8])
        more = f" and {len(quiet) - 8} more" if len(quiet) > 8 else ""
        tail = (f'<p class="quiet">No usage reported yet from <span class="mono">{names}'
                f'</span>{more}.</p>')
    items = []
    for row in live:
        usage = row.get("usage") or {}
        total = usage.get("total_tokens") or 0
        cached = usage.get("cache_read_input_tokens") or 0
        share = f"{cached / total * 100:.0f}%" if total else "-"
        spark, caption = _usage_chart(usage)
        # Which model did the work is left out: it is whatever each person
        # chose in their session, and can change turn by turn.
        shown, where = _called(row)
        place = "slot" if row.get("machine") else "node"
        items.append(
            f'<div class="usage-row"><div class="usage-name">{escape(shown)}'
            f'<span class="muted"> &middot; {escape(where)}{escape(row["owner"])}</span>'
            f'<div class="usage-total"><b>{escape(_human_tokens(total))}</b>'
            f'<span class="muted"> tokens run on this {place}, last '
            f'{escape(_usage_span(usage))}</span></div>'
            f'<div class="usage-meta muted">'
            f'{escape(_plural(usage.get("sessions") or 0, "session"))} &middot; '
            f'{escape(share)} cached</div></div>'
            f'<div class="usage-quota">{_quota_html(row, now)}</div>'
            f'<div class="usage-spark">{spark}{caption}</div></div>')
    return ('<h2>Usage and quota</h2><div class="card">' + "".join(items) + tail +
            '<p class="note">'
            "Windows come from <code>/usage</code> inside a Claude Code session on the node, "
            "or in the slot on a shared machine, "
            "read on a slow schedule &mdash; Claude Code reporting on itself, not a usage "
            "endpoint. The windows are the whole Claude account&#x27;s, used anywhere: "
            "claude.ai, the Claude app, and Claude Code on any computer, device tokens "
            "included. Token counts are only what ran on this node or slot, from the "
            "transcripts Claude Code writes there. Conversation content never leaves the "
            "node; only counts do.</p></div>")


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
        tiles.append(f'<div class="{cls}"><b>{n}</b><span>{escape(label)}</span></div>')
    return f'<div class="strip">{"".join(tiles)}</div>'


def render_dashboard(rows: list[Mapping[str, Any]], alerts: list[Mapping[str, Any]],
                     now: float, cfg: Config, csrf: str = "", who: Any = None,
                     logins: Optional[Mapping[str, Any]] = None, extra: str = "",
                     corner: str = "") -> str:
    # who is None for callers that predate per-user accounts, which are all
    # operator-side, so the default is the full-privilege view.
    is_admin = who is None or getattr(who, "is_admin", True)
    empty = ("No nodes yet. Use the form below." if is_admin else
             "No nodes are assigned to you yet. Your operator adds them.")
    body_rows = "".join(_row_html(r, now) for r in rows) or (
        f'<tr><td colspan="9" class="muted">{escape(empty)}</td></tr>')
    # An alert is about a node: say it by the name that node goes by.
    called = {}
    for r in rows:
        shown, _ = _called(r)
        called[r["id"]] = shown if shown == r["id"] else f"{shown} on {r['id']}"
    alert_items = "".join(
        f'<div class="alert">{_pill(a["level"])}'
        f'<span class="alert-rule">{escape(called.get(a["node_id"], a["node_id"]))} · '
        f'{escape(a["rule"])}</span>'
        f'<span class="alert-msg">{escape(a["message"])}</span>'
        f'<span class="muted small">{escape(_age(now, a["opened_at"]))} ago</span></div>'
        for a in alerts) or (
        '<p class="quiet">Nothing open. Every node is inside its thresholds.</p>')
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
        every, cadence = IDLE_REFRESH_S, "refreshing itself every minute"
    # Always to the console's own address, never the one the page was opened at.
    # After an action that address ends in #<card>, and a refresh naming no
    # address on a page whose address has a fragment is a fragment navigation:
    # the browser scrolls and reloads nothing, so the page never refreshed after
    # a press at all. The target never carries a fragment either, for the same
    # reason: from /admin#x a refresh to /admin#x would stick the same way.
    auto_refresh = ("" if every is None else
                    f'<meta http-equiv="refresh" content="{every};url={CONSOLE_PATH}">')
    whoami = ""
    if who is not None:
        whoami = (f" · signed in as <strong>{escape(str(getattr(who, 'label', '')))}</strong>"
                  + ("" if is_admin else " · showing only your nodes"))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'{auto_refresh}<title>ccfleet console</title>'
        f'<link rel="icon" href="{FAVICON}">'
        f"<style>{CSS}</style></head><body class=\"console\">"
        + _console_bar(corner, product_href(cfg, "/")) + "<div class=\"page\">"
        '<header class="mast"><div>'
        "<h1>Fleet</h1>"
        '<p class="sub">One owner, one account, one node · heartbeat max age '
        f"{cfg.heartbeat_max_age_s // 60} min · {cadence}{whoami}</p></div>"
        + _strip_html(counts) +
        "</header>"
        '<div class="wrap"><table><thead><tr><th>Status</th><th>Node</th><th>Last seen</th>'
        "<th>Claude Code</th><th>Egress IP</th><th>Disk</th><th>Load</th><th>Login</th>"
        "<th>Remote Control</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
        f'<h2>Open alerts</h2><div class="card">{alert_items}</div>'
        # Management is the operator's. An owner sees their nodes and nothing to
        # press, which is why they get no CSRF token either: there is no form.
        # The sign-in card belongs to whoever owns the node, admin or not: needing
        # the operator to sign you in would only move the bottleneck.
        + _usage_html(rows, now)
        + (_signin_html(rows, csrf, logins or {}) if csrf else "")
        + (_token_html(rows, csrf, logins or {}, now) if csrf else "")
        + (_manage_html(rows, csrf) + _add_form(csrf) if csrf and is_admin else "")
        + extra
        + "</div>" + LOCAL_TIMES_TAG + "</body></html>"
    )
