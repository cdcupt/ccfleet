"""Authored English/Chinese copy and a small, hash-allowed language control.

Only the language preference is stored in the browser. The controller never
reads form values, credentials or account data, and never makes a request.
"""

from __future__ import annotations

import base64
import hashlib
from html import escape


def bi(en_html: str, zh_html: str, *, inline: bool = False) -> str:
    """Pair trusted HTML; callers must escape any interpolated external data."""
    wrapper = "bi-inline" if inline else "bi"
    tag = "span" if inline else "div"
    return (f'<{tag} class="{wrapper}"><{tag} class="copy-en" lang="en">{en_html}</{tag}>'
            f'<{tag} class="copy-zh" lang="zh-Hans">{zh_html}</{tag}></{tag}>')


def label(en_text: str, zh_text: str, *, inline: bool = False) -> str:
    """Pair text labels, escaping both languages."""
    return bi(escape(en_text), escape(zh_text), inline=inline)


CONTROLS = (
    '<div class="language-control" role="group" aria-label="Language / 语言">'
    '<button type="button" data-language-choice="en" aria-pressed="false">English</button>'
    '<button type="button" data-language-choice="zh" aria-pressed="false">中文</button>'
    '<button type="button" data-language-choice="both" aria-pressed="true">双语 / Both</button>'
    '</div>'
)

CSS = """
.bi>.copy-en,.bi>.copy-zh{display:block}
.bi>.copy-en+.copy-zh{margin-top:.45em}
.bi-inline>.copy-en,.bi-inline>.copy-zh{display:inline}
body[data-language="both"] .bi-inline>.copy-zh::before{content:" / ";color:var(--muted,#667085)}
body[data-language="en"] .copy-zh,body[data-language="zh"] .copy-en{display:none!important}
body[data-language="en"] .bi>.copy-en,body[data-language="zh"] .bi>.copy-zh{margin-top:0}
.copy-zh{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
letter-spacing:0;line-height:1.8}
.language-control{display:inline-flex;align-items:center;gap:3px;padding:3px;
border:1px solid var(--rule,#d8dee8);border-radius:10px;background:var(--panel,#fff);
flex-shrink:0;max-width:100%}
.language-control button{appearance:none;border:0;border-radius:7px;padding:6px 9px;
background:transparent;color:var(--muted,#667085);font:600 12px/1.4 system-ui;
cursor:pointer;white-space:nowrap}
.language-control button[aria-pressed="true"]{background:var(--acc-soft,#f0efff);
color:var(--acc,#4932c5)}
.language-control button:focus-visible{outline:2px solid var(--acc,#4932c5);outline-offset:2px}
@media print{.language-control{display:none}.bi>.copy-en+.copy-zh{margin-top:.3em}}
"""

SCRIPT = r"""(()=>{
'use strict';
const modes=new Set(['en','zh','both']);
const storageKey='ccfleet.language';
const buttons=document.querySelectorAll('[data-language-choice]');
function choose(mode,remember){
  if(!modes.has(mode))mode='both';
  document.body.dataset.language=mode;
  document.documentElement.lang=mode==='zh'?'zh-Hans':'en';
  buttons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.languageChoice===mode)));
  document.querySelectorAll('option[data-copy-en][data-copy-zh]').forEach(option=>{
    option.textContent=mode==='en'?option.dataset.copyEn:mode==='zh'?option.dataset.copyZh:
      option.dataset.copyEn+' / '+option.dataset.copyZh;
  });
  document.querySelectorAll('[data-placeholder-en][data-placeholder-zh]').forEach(input=>{
    input.placeholder=mode==='en'?input.dataset.placeholderEn:mode==='zh'?input.dataset.placeholderZh:
      input.dataset.placeholderEn+' / '+input.dataset.placeholderZh;
  });
  document.querySelectorAll('[data-label-en][data-label-zh]').forEach(cell=>{
    cell.dataset.label=mode==='en'?cell.dataset.labelEn:mode==='zh'?cell.dataset.labelZh:
      cell.dataset.labelEn+' / '+cell.dataset.labelZh;
  });
  if(remember){
    try{localStorage.setItem(storageKey,mode);}catch(_error){}
    try{const url=new URL(location.href);url.searchParams.set('lang',mode);
      history.replaceState(null,'',url);}catch(_error){}
  }
}
let preferred='both';
try{const saved=localStorage.getItem(storageKey);
  if(modes.has(saved))preferred=saved;}catch(_error){}
try{const explicit=new URL(location.href).searchParams.get('lang');
  if(modes.has(explicit)){preferred=explicit;
    try{localStorage.setItem(storageKey,explicit);}catch(_error){}}}catch(_error){}
choose(preferred,false);
buttons.forEach(button=>button.addEventListener('click',()=>choose(button.dataset.languageChoice,true)));
})();"""

SCRIPT_TAG = f"<script>{SCRIPT}</script>"
SCRIPT_CSP = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"
