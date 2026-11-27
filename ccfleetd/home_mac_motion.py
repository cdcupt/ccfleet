"""Mac-to-Fleet connection feedback. The controller owns every displayed probe state."""

import base64
import hashlib
from html import escape
from urllib.parse import urlencode

from . import home_macs
from .bilingual import label

STATES = {
    "waiting": ("Waiting for your Mac", "等待 Mac 连接"),
    "idle": ("Ready to check the tunnel", "可以检测隧道"),
    "checking": ("Checking the tunnel…", "正在检测隧道…"),
    "connected": ("Tunnel verified", "隧道已验证"),
    "offline": ("Mac is offline", "Mac 离线"),
    "retry": ("Tunnel needs a new check", "请重新检测隧道"),
}
CSS = """
.ds-head.hm-head{flex-direction:row;align-items:flex-end;justify-content:space-between;flex-wrap:wrap}
.hm-route[data-state=checking] form{display:none}
.ds-picker{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:18px 22px;
background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
box-shadow:var(--shadow-sm)}
.ds-picker label{font-size:14px;font-weight:620;color:var(--ink)}
.ds-picker select{flex:1 1 220px;max-width:100%;min-height:44px;padding:8px 12px;
border:1px solid var(--rule);border-radius:10px;background:var(--panel);color:var(--ink);
font:inherit;font-size:14.5px;transition:border-color .15s var(--ease),box-shadow .15s var(--ease)}
.ds-picker select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft);outline:none}
.ds-slot-banner{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin:4px 0 -6px}
.ds-slot-banner h2{margin:0;font-family:var(--serif);font-size:26px;font-weight:500;
letter-spacing:-.02em;line-height:1.2;color:var(--ink);overflow-wrap:anywhere}
.ds-service{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
gap:12px 16px;border-top:1px solid var(--rule-soft);padding:16px 0}
.ds-service:first-of-type{border-top:0;padding-top:4px}
.ds-service:last-of-type{padding-bottom:2px}
.ds-service-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;min-width:0}
.ds-service h3,.ds-service p,.ds-service form{margin:0}
.ds-service h3{font-size:15.5px;font-weight:640;color:var(--ink)}
.ds-service-detail{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;
font-size:13.5px;line-height:1.55;color:var(--muted)}
.ds-service-detail code{background:var(--inset);padding:2px 6px;border-radius:5px;
border:1px solid var(--rule-soft);color:var(--ink);font-size:12.5px}
.hm-route{background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
box-shadow:var(--shadow);padding:20px 24px;display:flex;flex-direction:column;gap:12px;
animation:cf-rise .44s var(--ease) both}
.hm-route .hm-track{display:inline-flex;align-items:center;gap:10px;color:var(--muted);
font-size:13px;font-weight:600}
.hm-route .hm-mac-icon{display:inline-block;width:24px;height:13px;border:1.5px solid;
border-radius:4px;position:relative}
.hm-mac-icon:after{content:"";position:absolute;width:3px;height:3px;border-radius:50%;
background:currentColor;bottom:2px;right:3px}
.hm-fleet-icon{display:inline-block;width:18px;height:16px;border:1.5px solid;border-radius:3px;
background:repeating-linear-gradient(0deg,transparent 0 4px,currentColor 4px 5px)}
.hm-route .hm-line{height:2px;width:clamp(64px,18vw,150px);background:var(--rule);
border-radius:2px;position:relative;transition:background-color .25s var(--ease)}
.hm-line:after{display:none;content:"";width:8px;height:8px;border-radius:50%;
background:var(--acc);position:absolute;top:-3px}
.hm-route[data-state=checking] .hm-line:after{display:block;
animation:hm-transfer 1.3s ease-in-out infinite}
.hm-route[data-state=connected] .hm-line{background:var(--ok)}
.hm-route[data-state=connected] .hm-track{color:var(--ok)}
.hm-route[data-state=waiting] .hm-line,
.hm-route[data-state=retry] .hm-line{background:var(--warn-line)}
@keyframes hm-transfer{from{left:0}to{left:calc(100% - 8px)}}
@media(prefers-reduced-motion:reduce){.hm-route{animation:none!important}
.hm-route[data-state=checking] .hm-line:after{animation:none;left:50%}}
.hm-route [role=status]{font-weight:620;font-size:14.5px;margin:0;color:var(--ink)}
.hm-route[data-state=connected] [role=status]{color:var(--ok)}
.hm-route[data-state=waiting] [role=status],
.hm-route[data-state=retry] [role=status]{color:var(--warn)}
.hm-route[data-state=offline] [role=status],
.hm-route[data-state=unavailable] [role=status]{color:var(--muted)}
.hm-route form{margin:0}
@media(max-width:640px){.ds-picker{padding:16px}
.ds-picker select,.ds-picker .btn{width:100%}
.ds-service{flex-direction:column;align-items:flex-start}
.ds-service-detail .btn,.ds-service-detail form,.ds-service-detail form .btn{width:100%}}
"""
SCRIPT = """(() => {
  const field=document.querySelector('[data-hm-code]');
  if (location.pathname.endsWith('/machines/confirm')) {
    const key='ccfleet-mac-approval-v1';
    const code=new URLSearchParams(location.hash.slice(1)).get('code') || '';
    if (location.hash) history.replaceState(null,'',location.pathname+location.search);
    let value=/^[A-HJ-NP-Z2-9]{10}$/.test(code)?code:'';
    try {
      if (value) sessionStorage.setItem(key,JSON.stringify([value,Date.now()+600000]));
      const saved=JSON.parse(sessionStorage.getItem(key)||'null');
      if (!value && saved && saved[1]>Date.now()) value=saved[0];
      if (field || (saved && saved[1]<=Date.now())) sessionStorage.removeItem(key);
    } catch (_) {}
    if (field && /^[A-HJ-NP-Z2-9]{10}$/.test(value)) {
      field.value=value.slice(0,5)+'-'+value.slice(5);
    }
  }
  const panel=document.querySelector('[data-hm-status-url]');
  if (!panel) return;
  let timer, active=true, attempts=0;
  const labels=JSON.parse(panel.dataset.labels);
  const zh=document.documentElement.lang.startsWith('zh');
  const line=panel.querySelector('[role="status"]');
  async function poll() {
    if (!active || document.hidden) return;
    try {
      const res=await fetch(panel.dataset.hmStatusUrl,{credentials:'same-origin',
        cache:'no-store',redirect:'error',signal:AbortSignal.timeout(6000)});
      if (!res.ok) throw new Error();
      const result=await res.json();
      if (!Object.hasOwn(labels,result.state)) throw new Error();
      if (panel.dataset.state!==result.state) {
        panel.dataset.state=result.state;
        line.textContent=labels[result.state][zh?1:0];
        if (result.state==='connected') {
          const page=await fetch(location.pathname+location.search,{credentials:'same-origin',
            cache:'no-store',redirect:'error',signal:AbortSignal.timeout(6000)});
          if (!page.ok) throw new Error();
          const fresh=new DOMParser().parseFromString(await page.text(),'text/html');
          const section=fresh.getElementById('ds-services');
          if (section) document.getElementById('ds-services')?.replaceWith(section);
        }
      }
      attempts++;
      if (['waiting','checking'].includes(result.state)) {
        if (attempts>=80) throw new Error();
        timer=setTimeout(poll,1400);
      }
    } catch (_) {
      panel.dataset.state='unavailable';
      line.textContent=zh?'状态暂不可用，请重新检测。':
        'Status unavailable. Check the connection again.';
    }
  }
  if (['waiting','checking'].includes(panel.dataset.state)) timer=setTimeout(poll,350);
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(timer);
    if (!document.hidden && ['waiting','checking'].includes(panel.dataset.state)) {
      timer=setTimeout(poll,200);
    }
  });
  window.addEventListener('pagehide',()=>{active=false;clearTimeout(timer)});
})();"""
CSP = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"


def script_tag():
    return "<script>" + SCRIPT + "</script>"


def panel(machine, csrf):
    import json

    # Choose the language on the client from the projected, single-language page.
    state = machine["tunnel_state"]
    slot = escape(machine["slot_id"], quote=True)
    url = home_macs.STATUS + "?" + urlencode({"slot": machine["slot_id"]})
    labels = escape(json.dumps(STATES, ensure_ascii=False), quote=True)
    return (
        f'<section class="hm-route" data-state="{state}" data-hm-status-url="{escape(url)}" '
        f'data-labels="{labels}"><div class="hm-track" aria-hidden="true">'
        '<span class="hm-mac-icon"></span>Mac<span class="hm-line"></span>'
        '<span class="hm-fleet-icon"></span>Fleet</div><p role="status" aria-live="polite">'
        + label(*STATES[state], inline=True)
        + '</p><form method="post" action="'
        + home_macs.PROBE
        + '">'
        + f'<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">'
        + f'<input type="hidden" name="slot" value="{slot}"><button class="btn" type="submit">'
        + label("Check connection", "检测连接", inline=True)
        + "</button></form></section>"
    )
