"""Authenticated trial page and bounded Messages boundary; content never logged."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import html
import json
import logging
import shlex
import socket
import threading

from ccfleet_agent import devin_relay

from . import gateway, usersite
from .bilingual import bi, label
from .devin_trial import MAX_REQUEST_BYTES
from .trial_runtime import TrialUnavailable
from .trial_worker_transport import MODEL

PAGE = "/account/devin-trial"
API = "/trial/v1/messages"
log = logging.getLogger(__name__)

# This helper only copies text already visible on the page, never sends a key.
COPY_COMMAND_JS = r'''(()=>{'use strict';
const button=document.querySelector('[data-trial-copy]');
if(!button)return;button.hidden=false;
button.addEventListener('click',async()=>{
const command=document.querySelector('#trial-command code');
const feedback=document.getElementById('trial-copy-feedback');
const zh=document.documentElement.lang.toLowerCase().startsWith('zh');
if(!command||!feedback)return;
try{await navigator.clipboard.writeText(command.textContent);
feedback.textContent=zh?'已复制。请在自己的电脑终端中粘贴。':
'Copied. Paste this into a terminal on your computer.';
}catch(error){const range=document.createRange();range.selectNodeContents(command);
const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
feedback.textContent=zh?'已选中命令。请按 Ctrl+C（Mac 上为 ⌘C）复制。':
'Command selected. Press Ctrl+C (⌘C on Mac) to copy.';}
});
})();'''
COPY_COMMAND_CSP = "'sha256-" + base64.b64encode(
    hashlib.sha256(COPY_COMMAND_JS.encode()).digest()
).decode() + "'"

TRIAL_CSS = """
.trial-page{max-width:1100px;margin:0 auto;min-width:0}
.trial-page p{max-width:70ch}
.trial-back{display:inline-flex;font-size:13px;text-decoration:none;margin-bottom:24px}
.trial-hero{margin-bottom:30px;max-width:760px}
.trial-eyebrow{color:var(--acc);font-size:12px;font-weight:700;letter-spacing:.08em;
text-transform:uppercase;margin:0 0 10px}
.trial-hero h1{font-size:clamp(30px,4vw,44px);line-height:1.15;letter-spacing:-.045em;
margin:0 0 16px;max-width:20ch}
.trial-hero>p:last-child{font-size:16px;line-height:1.7;color:var(--muted);margin:0;max-width:62ch}
.trial-status{display:flex;align-items:flex-start;gap:12px;border:1px solid var(--acc-line);
background:var(--acc-soft);border-radius:14px;padding:18px 22px;margin-bottom:22px}
.trial-status-dot{width:9px;height:9px;border-radius:50%;background:var(--acc);
margin-top:8px;flex:none}
.trial-status strong{font-size:14px;display:block}
.trial-status p{margin:4px 0 0;font-size:13px;color:var(--muted);line-height:1.65}
.trial-status.paused{background:var(--warn-bg);border-color:var(--warn-line)}
.trial-status.paused .trial-status-dot{background:var(--warn)}
.trial-status.success{background:var(--ok-bg);border-color:var(--ok-line)}
.trial-status.success .trial-status-dot{background:var(--ok)}
.trial-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:22px;align-items:start}
.trial-panel{background:var(--panel);border:1px solid var(--rule);border-radius:18px;
box-shadow:var(--shadow);min-width:0;overflow:hidden}
.trial-panel-heading{padding:24px 28px 20px;border-bottom:1px solid var(--rule-soft)}
.trial-panel h2{font-size:20px;letter-spacing:-.025em;margin:0}
.trial-panel-heading p{font-size:13px;color:var(--muted);margin:5px 0 0}
.trial-steps{list-style:none;padding:0 28px;margin:0;counter-reset:trial-step}
.trial-step{padding:26px 0;border-bottom:1px solid var(--rule-soft);counter-increment:trial-step}
.trial-step:last-child{border:0}
.trial-step h3{display:flex;align-items:center;gap:11px;font-size:16px;letter-spacing:-.01em;
margin:0 0 10px;line-height:1.5}
.trial-step h3::before{content:counter(trial-step);display:grid;place-items:center;flex:none;
width:27px;height:27px;border:1px solid var(--acc-line);border-radius:8px;
background:var(--acc-soft);
color:var(--acc);font-size:13px;font-weight:650;font-variant-numeric:tabular-nums}
.trial-step p{font-size:14px;color:var(--muted);margin:0 0 15px;line-height:1.75}
.trial-step p:last-child{margin-bottom:0}
.trial-page form{margin:0}
.trial-page .btn{min-height:43px;white-space:normal;text-align:center}
.trial-page .trial-primary{padding:12px 18px;font-size:14px}
.trial-step .trial-key-note{font-size:12px;margin:12px 0 0;line-height:1.7}
.trial-command{margin:14px 0;border-radius:12px;overflow:hidden;border:1px solid var(--rule)}
.trial-command-bar{padding:10px 14px;background:var(--inset);font:11px/1.5 var(--mono);
color:var(--muted);border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;
align-items:center;gap:10px;min-width:0}
.trial-command pre{margin:0;padding:18px;background:var(--inset);border:0;border-radius:0;
max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
font-size:12px;line-height:1.85;user-select:all;-webkit-user-select:all}
.trial-command pre code{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
background:transparent;padding:0;border:0;border-radius:0}
.trial-copy{min-height:34px;font:600 12px/1.4 var(--sans);padding:7px 10px;flex:none}
.trial-copy[hidden]{display:none}
.trial-step .trial-copy-feedback{font-size:12px;min-height:20px;margin:0;color:var(--acc)}
.trial-placeholder{border:1px dashed var(--rule);background:var(--inset);border-radius:12px;
padding:18px;margin-top:14px}
.trial-placeholder p{font-size:13px;margin:0}
.trial-allowance{padding:24px}
.trial-allowance .trial-eyebrow{margin-bottom:15px}
.trial-allowance h2{font-size:38px;line-height:1.1;font-variant-numeric:tabular-nums;
letter-spacing:-.05em;margin:0 0 7px;overflow-wrap:anywhere}
.trial-allowance .trial-budget{font-size:13px;color:var(--muted);margin:0 0 16px}
.trial-allowance meter{display:block;width:100%;height:10px;margin:0 0 17px;appearance:none;
background:var(--inset);border:0;border-radius:99px;overflow:hidden}
.trial-allowance meter::-webkit-meter-bar{background:var(--inset);border:0;border-radius:99px}
.trial-allowance meter::-webkit-meter-optimum-value{background:var(--acc);border-radius:99px}
.trial-allowance meter::-moz-meter-bar{background:var(--acc);border-radius:99px}
.trial-budget-notes{display:grid;gap:12px;margin:0;border-top:1px solid var(--rule-soft);
padding-top:17px}
.trial-budget-notes div{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.trial-budget-notes dt,.trial-budget-notes dd{font-size:12px;margin:0}
.trial-budget-notes dt{color:var(--muted)}
.trial-budget-notes dd{font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.trial-allowance .trial-reset{font-size:12px;line-height:1.7;color:var(--muted);margin:17px 0 0}
.trial-side-note{padding:20px 24px;font-size:13px;color:var(--muted)}
.trial-side-note strong{display:block;color:var(--ink);font-size:13px;margin-bottom:6px}
.trial-side-note p{font-size:12px;line-height:1.75;margin:0}
.trial-details{margin-top:22px;background:var(--panel);border:1px solid var(--rule);
border-radius:14px}
.trial-details>summary{font-size:13px;font-weight:600;padding:18px 22px;cursor:pointer}
.trial-details[open]>summary{border-bottom:1px solid var(--rule-soft)}
.trial-details-content{padding:6px 24px 22px;font-size:13px;color:var(--muted);line-height:1.8}
.trial-details-content h3{font-size:14px;margin:22px 0 8px;color:var(--ink)}
.trial-details-content p{margin:10px 0;max-width:none}
.trial-key-management{border-top:1px solid var(--rule-soft);margin-top:20px;padding-top:18px}
.trial-key-management .btn{font-size:12px;min-height:38px}
@media(max-width:900px){.trial-grid{grid-template-columns:minmax(0,1fr) 265px;gap:16px}
.trial-steps{padding:0 22px}.trial-panel-heading{padding:22px}.trial-allowance{padding:22px}}
@media(max-width:700px){.trial-grid{grid-template-columns:minmax(0,1fr)}
.trial-hero{margin-bottom:24px}.trial-back{margin-bottom:20px}
.trial-hero h1{max-width:22ch}.trial-hero>p:last-child{font-size:15px}
.trial-status{padding:16px}.trial-step{padding:22px 0}
.trial-allowance h2{font-size:34px}.trial-side-note{padding:16px 4px 0}
.trial-command-bar{padding:9px 11px}.trial-command pre{padding:14px;font-size:11px}
.trial-details>summary{padding:17px}.trial-details-content{padding:4px 18px 18px}}
@media(max-width:380px){.trial-steps{padding:0 18px}.trial-panel-heading{padding:20px 18px}
.trial-page .trial-primary{width:100%}.trial-command-bar{flex-wrap:wrap}}
"""


def entry(cfg):
    if not cfg.devin_trial_enabled or cfg.broker_only:
        return ""
    return (
        '<section class="card"><h2>'
        + label("Try Devin", "试用 Devin", inline=True)
        + (
            "</h2><p>"
            + label(
                "Use local Claude Code with the limited Fleet-managed daily trial. "
                "No slot purchase or Devin sign-in is needed.",
                "在本地 Claude Code 中使用 Fleet 管理的限额每日试用，无需购买槽位或登录 Devin。",
                inline=True,
            )
            + '</p><a class="btn primary" href="'
            + PAGE
            + '">'
            + label("Try Devin", "试用 Devin", inline=True)
            + "</a></section>"
        )
    )


def guide():
    return (
        '<section class="card"><h2>'
        + label("Optional daily Devin trial", "可选每日 Devin 试用", inline=True)
        + "</h2>"
        + bi(
            '<p>When <a href="/account/devin-trial">Try Devin</a> appears: sign in to Fleet with '
            "Google, create a trial key, then copy its command into your local "
            "Claude Code terminal. "
            "No slot purchase or Devin login. This uses a separate Fleet-managed native worker, "
            "not another customer&#x27;s private slot. Provider credentials are never given to "
            "visitors; local tools and history stay on your computer, while included model "
            "context is sent to Fleet and Devin.</p><p>The daily allowance is "
            "100,000 per account "
            "and 1,000,000 shared, reset at 00:00 UTC. Fully metered usage is "
            "charged as reported; "
            "if only native cache-read metering is missing, up to 20,000 units "
            "stay conservatively "
            "held for that turn (or the larger known subtotal). This is not an "
            "exact token or USD "
            "hard cap. One global active turn, twenty turns/twenty reserved minutes per day; "
            "a busy worker asks you to wait. Unknown execution or cleanup pauses new work. "
            "Paid private slots and their own-provider subscriptions remain separate.</p>",
            '<p>显示<a href="/account/devin-trial"'
            '>试用 Devin</a>时：用 Google 登录 Fleet、创建'
            '试用密钥，'
            "然后在本地 Claude Code 终端粘贴命令。无需购买槽位或登录 Devin。试用使用独立的 Fleet "
            "管理工作进程，不会借用其他用户的私人槽位，也不会将服务商凭证交给访客。"
            "本地工具及历史留在你的电脑，包含在模型请求中的内容会发送给 Fleet 和 Devin。</p>"
            '<p>每日每账号额度为 100,000，所有用户共享 1,000,00'
            '0，UTC 零点重置。完整计量按已报告用量结算；'
            "只缺少原生缓存读取计量时，每轮保守保留最多 20,000 单位，已知部分更大则保留更多。"
            "这不是精确 token 或美元硬上限。全局同时仅一轮，每日最多二十轮、预留二十分钟；"
            "忙碌时请稍候。执行或清理不确定时暂停。付费私人槽位及自有服务商订阅仍独立使用。</p>",
        )
        + "</section>"
    )


def privacy():
    return bi(
        "<p><b>Optional Devin trial:</b> the limited trial uses a Fleet-managed provider "
        "account, unlike private paid slots with your own provider sign-in. Your local Claude "
        "Code sends the current request and its included conversation/tool history to Fleet "
        "and Devin. A fresh worker HOME is used for every turn; after confirmed process cleanup "
        "and usage collection, its history and artifacts are removed. Trial accounting keeps "
        "account IDs, hashed keys and usage totals, not prompts or replies. Administrators "
        "technically control the worker. Do not send secrets. Your local Claude Code history "
        "remains on your computer.</p><p>Fleet receives the original request. Before forwarding "
        "to Devin, the bridge omits known private assistant reasoning and signatures while "
        "keeping visible text, local tool history and turn order. A bridge note replaces a "
        "private-only assistant turn. Reasoning continuity is lost and answers may change.</p>",
        "<p><b>可选 Devin 试用：</b>限额试用使用 Fleet 管理的服务账号；付费私人槽位仍使用你自己的"
        "服务登录。本地 Claude Code 会将当前请求及其中包含的对话、工具历史发送给 Fleet 和 Devin。"
        "每轮使用全新的工作目录；确认进程清理并记录用量后，删除该轮历史及产物。"
        "试用计量仅保存 Fleet 账号 ID、密钥哈希及用量，不保存提示词或回复。"
        "管理员在技术上控制工作机器。"
        "请勿发送秘密信息；本地 Claude Code 历史仍保存在你的电脑上。</p>"
        "<p>Fleet 会收到原始请求；转发给 Devin 前，桥接会省略已知格式的助手私有思考及签名，"
        "保留可见文本、本地工具历史和轮次顺序。只有私有思考的助手轮次会替换为明确的桥接说明。"
        "思考过程无法延续，回答可能因此改变。</p>",
    )


def _trial_status(title, title_zh, detail, detail_zh, *, tone="", alert=False):
    return (
        '<div class="trial-status ' + tone + '" role="' + ("alert" if alert else "status")
        + '"><span class="trial-status-dot" aria-hidden="true"></span><div><strong>'
        + label(title, title_zh, inline=True) + "</strong><p>"
        + label(detail, detail_zh, inline=True) + "</p></div></div>"
    )


def _trial_setup(cfg, csrf, token):
    create_form = (
        '<form method="post" action="' + PAGE + '/create">'
        '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True)
        + '"><button class="btn primary trial-primary" type="submit">'
        + label("Create / replace trial key", "创建或更换试用密钥", inline=True)
        + "</button></form>"
    )
    setup = (
        '<section class="trial-panel" aria-labelledby="trial-setup-title">'
        '<div class="trial-panel-heading"><h2 id="trial-setup-title">'
        + label("Set up in three steps", "三步开始试用", inline=True)
        + "</h2><p>"
        + label("Use a terminal on your own computer.", "请在自己的电脑终端中操作。", inline=True)
        + '</p></div><ol class="trial-steps"><li class="trial-step"><h3>'
        + label("Create your trial key", "创建试用密钥", inline=True) + "</h3><p>"
        + label(
            "Think of this as a temporary pass. It lets Claude Code use your Fleet trial.",
            "把密钥当作一张临时通行证，让本机 Claude Code 使用你的 Fleet 试用额度。",
            inline=True,
        ) + "</p>" + create_form + '<p class="trial-key-note">'
        + label(
            "A replacement immediately stops the old key from working. Keys last 30 days.",
            "更换后，旧密钥会立即失效。密钥有效期为 30 天。", inline=True,
        ) + '</p></li><li class="trial-step"><h3>'
        + label("Copy your launch command", "复制启动命令", inline=True) + "</h3>"
    )
    if token:
        command = "env ANTHROPIC_BASE_URL=" + shlex.quote(cfg.public_url.rstrip("/") + "/trial")
        command += " ANTHROPIC_AUTH_TOKEN=" + shlex.quote(token) + " ANTHROPIC_API_KEY='' "
        for name in (
            "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            command += name + "=" + MODEL + " "
        command += (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 claude --model " + MODEL + " --effort max"
        )
        setup += (
            "<p>" + label(
                "Copy the whole command. It includes your private trial key and opens Claude Code.",
                "复制完整命令。其中已包含你的私密试用密钥，执行后会启动 Claude Code。", inline=True,
            ) + '</p><div class="trial-command" id="trial-command"><div class="trial-command-bar">'
            '<span>' + label("Terminal", "终端", inline=True)
            + '</span><button class="trial-copy" type="button" data-trial-copy hidden>'
            + label("Copy command", "复制命令", inline=True)
            + '</button></div><pre><code>' + html.escape(command) + '</code></pre></div>'
            '<p class="trial-copy-feedback" id="trial-copy-feedback" role="status" '
            'aria-live="polite"></p><p class="trial-key-note">'
            + label(
                "Shown once: save this command before leaving this page or switching language. "
                "Keep it private. This is a Fleet trial key, never a provider credential.",
                "仅显示一次：离开本页或切换语言前，请先保存命令并妥善保管。"
                "它仅是 Fleet 试用密钥，不是服务商凭证。", inline=True,
            ) + "</p>"
        )
    else:
        setup += (
            '<div class="trial-placeholder"><p>' + label(
                "Your command will appear here after you create a key. "
                "Already saved a command? You can keep using it until its key expires "
                "or is replaced.",
                "创建密钥后，命令会显示在这里。已保存过命令？只要密钥未过期或被更换，便可继续使用。",
                inline=True,
            ) + "</p></div>"
        )
    setup += (
        '</li><li class="trial-step"><h3>'
        + label("Paste, launch, and ask something small", "粘贴启动，先问一个小问题", inline=True)
        + "</h3><p>" + label(
            "Open Terminal on macOS or Linux, or a WSL terminal on Windows. "
            "With Claude Code already installed, paste the command and press Enter.",
            "打开 macOS 或 Linux 的终端，Windows 请使用 WSL 终端。"
            "确认已安装 Claude Code，粘贴命令，然后按回车。", inline=True,
        ) + "</p><p>" + label(
            "Start with a short question. The trial shares one worker, so if it is busy, "
            "wait a few seconds and try again.",
            "先试一个简短问题。所有人共用一个工作进程；如果提示忙碌，稍等几秒再试。", inline=True,
        ) + "</p></li></ol></section>"
    )
    return setup


def page(runtime, cfg, account, session_id, *, token="", failed=False, revoked=False):
    if runtime is None:
        return 404, usersite._shell("Devin trial", "<h1>Not found</h1>")
    if account is None:
        return 200, usersite._signed_out(cfg).replace("next=/account", "next=/account/devin-trial")
    csrf = usersite.csrf_for(session_id, cfg.cookie_secret)
    try:
        allowance = runtime.allowance(account)
    except Exception:
        return 403, usersite._shell(
            "Devin trial",
            label(
                "A verified Fleet Google sign-in is required.",
                "请先使用已验证的 Google 账号登录 Fleet。",
            ),
        )
    reset = dt.datetime.fromtimestamp(allowance["resets_at"], dt.timezone.utc).isoformat()
    body = (
        '<div class="trial-page"><a class="trial-back" href="/account">← '
        + label("Your slots", "我的槽位", inline=True)
        + '</a><header class="trial-hero"><p class="trial-eyebrow">'
        + label("A small daily trial", "每日限额试用", inline=True)
        + "</p><h1>"
        + label("Try Devin in Claude Code", "在 Claude Code 中试用 Devin", inline=True)
        + "</h1>"
    )
    body += bi(
        "<p>Bring a small task. Create a key, copy one command, and try Devin from Claude Code "
        "on your computer. No slot purchase or Devin login.</p>",
        "<p>从一个小任务开始：创建密钥，复制一条命令，就能在本机 Claude Code 中试用 Devin。"
        "无需购买槽位或登录 Devin。</p>",
    )
    body += "</header>"
    introduction, body = body, ""
    body += (
        "<p>"
        + label(
            f"Daily trial allowance: {allowance['available_tokens']:,} available of 100,000. "
            "All users share 1,000,000 allowance units per UTC day.",
            f"每日试用额度：可用 {allowance['available_tokens']:,}，每人每日总额 100,000；"
            "所有用户共享每日 1,000,000 额度单位。",
            inline=True,
        )
        + "</p><p>"
    )
    body += (
        label(
            f"Fully metered reported tokens: {allowance['reported_tokens']:,}. "
            f"Reserved allowance: {allowance['reserved_tokens']:,}, including "
            f"{allowance['unmeasured_reserved_tokens']:,} held for a missing cache-read meter. "
            "Those partial readings report at least "
            f"{allowance['partial_reported_tokens']:,} tokens; "
            "they are included in the hold, not added twice.",
            f"完整计量的已报告 token：{allowance['reported_tokens']:,}。"
            f"已预留额度 {allowance['reserved_tokens']:,}，其中 "
            f"{allowance['unmeasured_reserved_tokens']:,} 因缓存读取计量缺失而保守保留；"
            f"这些不完整读数至少报告 {allowance['partial_reported_tokens']:,} token，"
            "已包含在保留额度中，不重复相加。",
            inline=True,
        )
        + "</p><p>"
    )
    body += (
        label(
            "Resets at " + reset + ". Keys last 30 days across daily resets.",
            "重置时间：" + reset + "。密钥有效期 30 天，可跨每日重置使用。",
            inline=True,
        )
        + "</p>"
    )
    body += bi(
        "<p>One active turn globally. If busy, wait a few seconds and submit again; "
        "no idle session holds the worker. At most 20 turns and 20 reserved runtime minutes "
        "per person and globally per UTC day; each native turn has at most 120 seconds of "
        "generation and reserves 160 seconds of runtime. Reserved runtime can reach the daily "
        "time limit before the request cap. Keep requests short (192 KiB including "
        "tools/history). Before each turn "
        "we reserve up to 20,000 remaining allowance units. Complete input, output, cache-read "
        "and cache-creation usage settles the actual total, including overshoot.</p>"
        "<p>If Devin omits only the cache-read meter after a verified, fully cleaned-up reply, "
        "the whole reservation stays held until UTC reset (more if the known subtotal exceeds "
        "it). It is not a claim about actual usage. Other unknown usage, model identity, lost "
        "execution or cleanup pauses new work. An in-flight turn can exceed its reservation; "
        "this is admission accounting, not a precise generation, vendor quota "
        "or USD hard cap.</p>",
        "<p>全局同时仅处理一轮；忙碌时请稍等再提交，空闲会话不会占用工作进程。"
        "每人及全局每日均最多 20 轮、预留 20 分钟运行时间；每轮原生生成最多 120 秒，"
        "预留 160 秒运行时间。预留运行时间可能先达到每日上限。"
        "请求（含工具及历史）最多 192 KiB。每轮先预留最多 20,000 剩余额度单位，"
        "完整的输入、输出、缓存读取及创建用量按实际总数结算，超额也完整计算。</p>"
        "<p>若已验证模型且整轮清理完成，但 Devin 只缺少缓存读取计量，则整笔预留额度保留至 UTC "
        "零点（已知部分超出预留时保留更多）；这不代表实际 token 用量。其他用量、身份、执行或清理"
        '不确定时暂停新任务。进行中的一轮可能超额；这是准入计量，不是精确生成、'
        '服务商配额或美元硬上限。</p>',
    )
    if allowance["paused"]:
        introduction += _trial_status(
            "Trial temporarily paused", "试用暂时暂停",
            "Trial paused for verification. No new turn will start. Fleet needs to check the "
            "trial worker. Creating another key will not restart it. Please try again later.",
            "试用暂停，等待核实；不会开始新一轮。Fleet 需要检查试用工作进程。"
            "更换密钥无法恢复服务，请稍后再试。", tone="paused",
        )
    elif (
        allowance["available_tokens"] <= 0 or allowance["global_available_tokens"] <= 0
        or allowance["remaining_requests"] <= 0
    ):
        introduction += _trial_status(
            "Today's trial limit has been reached", "今日试用额度已用完",
            "A personal or shared daily limit is used up. Your saved key will work again "
            "after the daily reset; you do not need a replacement.",
            "个人或共享的每日额度已用完。每日重置后，原密钥仍可继续使用，无需更换。", tone="paused",
        )
    if failed:
        introduction += _trial_status(
            "The action could not be confirmed", "尚未确认操作结果",
            "Refresh this page to check the trial status. If you did not receive a command, "
            "create a replacement key when you are ready to save it.",
            "请刷新本页查看试用状态。如果没有收到启动命令，可在准备好保存时更换密钥。",
            tone="paused", alert=True,
        )
    elif token:
        introduction += _trial_status(
            "Trial key created", "试用密钥已创建",
            "Your previous key, if any, has been replaced. Copy and save the command in step 2.",
            "旧密钥（如有）已被替换。请复制并保存第 2 步中的启动命令。", tone="success",
        )
    elif revoked:
        introduction += _trial_status(
            "Trial key revoked", "试用密钥已撤销",
            "Commands using that key can no longer start trial requests. "
            "Create a new key whenever you want to return.",
            "使用旧密钥的命令已无法发起试用请求。想再次试用时，可创建新密钥。", tone="success",
        )
    available = max(0, min(allowance["available_tokens"], 100000))
    aside = (
        '<aside><section class="trial-panel trial-allowance" aria-labelledby="trial-budget-title">'
        '<p class="trial-eyebrow" id="trial-budget-title">'
        + label("Your daily allowance", "你的每日额度", inline=True)
        + '</p><h2>' + f"{available:,}" + '</h2><p class="trial-budget">'
        + label("of 100,000 units available", "可用额度 / 每日 100,000 单位", inline=True)
        + '</p><meter min="0" max="100000" value="' + str(available)
        + '" aria-label="Daily trial allowance available" '
        'data-aria-label-en="Daily trial allowance available" '
        'data-aria-label-zh="每日可用试用额度">'
        + str(available) + ' / 100000</meter><dl class="trial-budget-notes"><div><dt>'
        + label("Reported usage", "已报告用量", inline=True) + "</dt><dd>"
        + f"{allowance['reported_tokens']:,}" + "</dd></div><div><dt>"
        + label("Held for accounting", "计量预留", inline=True) + "</dt><dd>"
        + f"{allowance['reserved_tokens']:,}" + "</dd></div><div><dt>"
        + label("Request cap remaining", "剩余请求上限", inline=True) + "</dt><dd>"
        + str(allowance["remaining_requests"]) + '</dd></div></dl><p class="trial-reset">'
        + label("Resets every day at 00:00 UTC.", "每天 UTC 00:00 重置。", inline=True)
        + "<br>" + label("All users share 1,000,000 units per day.",
                                 "所有用户共享每日 1,000,000 单位。", inline=True)
        + '</p></section><div class="trial-side-note"><strong>'
        + label("Keep your first task small", "先从小任务开始", inline=True)
        + "</strong><p>" + label(
            "Each reply has up to 2 minutes to generate. Keep conversations short; "
            "long histories and large tool lists may not fit.",
            "每轮最多生成 2 分钟。请保持对话简短；过长的历史或过大的工具列表可能超出限制。",
            inline=True,
        ) + "</p></div></aside>"
    )
    details = (
        '<details class="trial-details"><summary>'
        + label("How limits and privacy work", "额度与隐私说明", inline=True)
        + '</summary><div class="trial-details-content"><h3>'
        + label("Daily limits and accounting", "每日限制与计量方式", inline=True)
        + "</h3>" + body + "<h3>" + label("Your data and privacy", "你的数据与隐私", inline=True)
        + "</h3>" + privacy() + '<div class="trial-key-management"><h3>'
        + label("Stop using this key", "停用此密钥", inline=True)
        + "</h3><p>" + label(
            "Revoke your trial key if it was shared or you no longer need it.",
            "如果密钥已泄露或不再需要，可以在这里撤销。", inline=True,
        ) + '</p><form method="post" action="' + PAGE + '/revoke">'
        '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True)
        + '"><button class="btn danger" type="submit">'
        + label("Revoke trial key", "撤销试用密钥", inline=True)
        + "</button></form></div></div></details>"
    )
    body = introduction + '<div class="trial-grid">' + _trial_setup(cfg, csrf, token)
    body += aside + "</div>" + details + "</div>"
    if token:
        body += "<script>" + COPY_COMMAND_JS + "</script>"
    return 200, usersite._shell("Devin trial", body, extra_css=TRIAL_CSS, door=True, width="wide")


def handle(runtime, handler):
    handler._gateway_native_no_retry = True
    handler.close_connection = True
    if runtime is None:
        gateway.model_error(handler, 404, "The trial is not enabled.")
        return
    try:
        names = [name.lower() for name in handler.headers]
        lengths = handler.headers.get_all("Content-Length", [])
        if (
            handler.path not in {API, API + "?beta=true"}
            or len(names) != len(set(names))
            or len(lengths) != 1
            or len(lengths[0]) > 8
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or str(int(lengths[0])) != lengths[0]
            or any(
                handler.headers.get_all(name)
                for name in ("Origin", "Cookie", "Transfer-Encoding", "Content-Encoding", "Expect")
            )
            or handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise TrialUnavailable("invalid_trial_request_framing", 400)
        size = int(lengths[0])
        if not 0 < size <= MAX_REQUEST_BYTES:
            raise TrialUnavailable("trial_context_too_large", 413)
        auth, keys = (
            handler.headers.get_all("Authorization", []),
            handler.headers.get_all("x-api-key", []),
        )
        if len(auth) + len(keys) != 1:
            raise TrialUnavailable("trial_key_required", 401)
        token = keys[0] if keys else auth[0][7:] if auth[0].startswith("Bearer ") else ""
        runtime.admit_api(token)  # Account-based rate bound before JSON/body/SSH work.

        def expired():
            with contextlib.suppress(OSError):
                handler.connection.shutdown(socket.SHUT_RDWR)

        handler.connection.settimeout(10)
        timer = threading.Timer(10, expired)
        timer.daemon = True
        timer.start()
        try:
            raw = gateway.wire.read_exact(handler.rfile, size)
        finally:
            timer.cancel()
        gateway.check_json_complexity(raw)
        message, streaming = runtime.turn(token, raw)
        raw = b"".join(devin_relay._sse(message)) if streaming else json.dumps(message).encode()
        handler.send_response(200)
        handler.send_header(
            "Content-Type", "text/event-stream" if streaming else "application/json"
        )
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.send_header("x-should-retry", "false")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.connection.settimeout(10)
        handler.wfile.write(raw)
    except TrialUnavailable as error:
        if error.request_error is not None:
            request_error = error.request_error
            log.info("Devin request rejected before execution: code=%s", request_error.code)
            gateway.model_error(handler, request_error.status, request_error.message,
                                code=request_error.code)
            return
        handler._trial_busy = error.code in {"trial_busy", "trial_request_rate_limit"}
        messages = {
            "trial_worker_unavailable": "The trial worker is temporarily unavailable. "
            "No model request was sent. Wait a moment and try again.",
            "trial_busy": "The trial worker is busy. Wait a few seconds and submit again.",
            "trial_request_rate_limit": "Too many trial requests. Wait a few seconds.",
            "daily_limit_reached": "Your daily trial allowance or turn/time limit is used. "
            "It resets at 00:00 UTC.",
            "global_daily_limit_reached": "The shared daily trial allowance or twenty-turn "
            "limit is used. It resets at 00:00 UTC.",
            "trial_paused": "The trial is paused while an unconfirmed turn is checked.",
            "trial_result_unconfirmed_do_not_retry": "The trial result could not be verified. "
            "Do not retry automatically.",
            "trial_context_too_large": "Trial requests including tools/history may use at most "
            "192 KiB. Start a smaller conversation.",
            "trial_requires_supported_opus_5_5_request": "Use claude-opus-5-5 and supported "
            "native-compatible request features.",
        }
        gateway.model_error(handler, error.status, messages.get(error.code, error.code))
    except (ValueError, OSError, EOFError, RecursionError):
        gateway.model_error(handler, 400, "The trial request could not be confirmed.")
    except Exception:
        gateway.model_error(
            handler, 503, "The trial is temporarily unavailable; do not retry automatically."
        )
