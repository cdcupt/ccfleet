"""Authenticated trial page and bounded Messages boundary; content never logged."""

from __future__ import annotations

import contextlib
import datetime as dt
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


def page(runtime, cfg, account, session_id, *, token="", failed=False):
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
        '<section class="card"><h1>'
        + label("Try Devin in Claude Code", "在 Claude Code 中试用 Devin", inline=True)
        + "</h1>"
    )
    body += bi(
        "<p>No slot purchase or Devin login. Sign in to Fleet, create your trial key, "
        "then copy the command into a terminal on your computer with Claude Code installed.</p>",
        "<p>无需购买槽位或登录 Devin。登录 Fleet、创建试用密钥，然后在已安装 Claude Code "
        "的电脑终端中粘贴命令。</p>",
    )
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
        "per person and globally per UTC day; each native turn has at most 40 seconds of "
        "generation. Keep requests short (192 KiB including tools/history). Before each turn "
        "we reserve up to 20,000 remaining allowance units. Complete input, output, cache-read "
        "and cache-creation usage settles the actual total, including overshoot.</p>"
        "<p>If Devin omits only the cache-read meter after a verified, fully cleaned-up reply, "
        "the whole reservation stays held until UTC reset (more if the known subtotal exceeds "
        "it). It is not a claim about actual usage. Other unknown usage, model identity, lost "
        "execution or cleanup pauses new work. An in-flight turn can exceed its reservation; "
        "this is admission accounting, not a precise generation, vendor quota "
        "or USD hard cap.</p>",
        "<p>全局同时仅处理一轮；忙碌时请稍等再提交，空闲会话不会占用工作进程。"
        "每人及全局每日均最多 20 轮、预留 20 分钟运行时间；每轮原生生成最多 40 秒。"
        "请求（含工具及历史）最多 192 KiB。每轮先预留最多 20,000 剩余额度单位，"
        "完整的输入、输出、缓存读取及创建用量按实际总数结算，超额也完整计算。</p>"
        "<p>若已验证模型且整轮清理完成，但 Devin 只缺少缓存读取计量，则整笔预留额度保留至 UTC "
        "零点（已知部分超出预留时保留更多）；这不代表实际 token 用量。其他用量、身份、执行或清理"
        '不确定时暂停新任务。进行中的一轮可能超额；这是准入计量，不是精确生成、'
        '服务商配额或美元硬上限。</p>',
    )
    if allowance["paused"]:
        body += (
            '<p role="status">'
            + label(
                "Trial paused for verification. No new turn will start.",
                "试用暂停，等待核实；不会开始新一轮。",
                inline=True,
            )
            + "</p>"
        )
    if failed:
        body += (
            '<p role="status">'
            + label(
                "The action could not be confirmed. Refresh this page.",
                "操作暂时无法确认，请刷新本页。",
                inline=True,
            )
            + "</p>"
        )
    setup = ""
    if token:
        command = "env ANTHROPIC_BASE_URL=" + shlex.quote(cfg.public_url.rstrip("/") + "/trial")
        command += " ANTHROPIC_AUTH_TOKEN=" + shlex.quote(token) + " ANTHROPIC_API_KEY='' "
        for name in (
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            command += name + "=" + MODEL + " "
        command += (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 claude --model " + MODEL + " --effort max"
        )
        setup += "<h2>" + label(
            "Copy once on your own computer", "在自己的电脑上复制执行", inline=True
        )
        setup += (
            "</h2><pre><code>"
            + html.escape(command)
            + "</code></pre><p>"
            + label(
                "This private key is shown once. Keep it private; creating a replacement revokes "
                "the previous key. It is only a Fleet trial key, never a provider credential.",
                '此私密密钥仅显示一次。请妥善保存；创建新密钥会撤销旧密钥。它仅是 Fl'
                'eet 试用密钥，'
                "不是服务商凭证。",
                inline=True,
            )
            + "</p>"
        )
    for action, words, zh in (
        ("create", "Create / replace trial key", "创建或更换试用密钥"),
        ("revoke", "Revoke trial key", "撤销试用密钥"),
    ):
        setup += '<form method="post" action="' + PAGE + "/" + action + '">'
        setup += '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True)
        setup += '"><button class="btn" type="submit">' + label(words, zh, inline=True)
        setup += "</button></form>"
    body = introduction + setup + body
    body += privacy() + '<p><a href="/account">' + label("Your slots", "我的槽位", inline=True)
    body += "</a></p></section>"
    return 200, usersite._shell("Devin trial", body, door=True)


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
