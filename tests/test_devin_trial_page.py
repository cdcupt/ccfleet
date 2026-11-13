"""The trial setup explains the next action and keeps secret/limit states distinct."""

from __future__ import annotations

import html
import re
import shlex
from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from ccfleetd import trial_routes, usersite
from ccfleetd.bilingual import project_html
from ccfleetd.config import Config


class TrialPage(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.forms = []
        self.details = []
        self.meters = []
        self.current_form = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.current_form is None, "trial forms must not be nested"
            self.current_form = {"attrs": attrs, "inputs": []}
            self.forms.append(self.current_form)
        elif tag == "input" and self.current_form is not None:
            self.current_form["inputs"].append(attrs)
        elif tag == "details" and "trial-details" in attrs.get("class", ""):
            self.details.append(attrs)
        elif tag == "meter":
            self.meters.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current_form = None


@pytest.fixture
def context():
    allowance = {
        "token_limit": 1000000, "global_token_limit": 10000000, "available_tokens": 982500, "reported_tokens": 15000, "reserved_tokens": 2500,
        "unmeasured_reserved_tokens": 2500, "partial_reported_tokens": 1000,
        "global_available_tokens": 9982500, "remaining_requests": 198,
        "remaining_global_requests": 198, "remaining_leases": 198,
        "remaining_global_leases": 198, "remaining_turns": 73,
        "remaining_runtime_seconds": 11680, "global_remaining_runtime_seconds": 11680,
        "runtime_limit_seconds": 12000, "global_runtime_limit_seconds": 12000,
        "turn_reservation_seconds": 160, "request_limit": 200, "global_request_limit": 200,
        "resets_at": 1735776000, "paused": False,
    }
    return SimpleNamespace(
        allowance=allowance,
        runtime=SimpleNamespace(allowance=lambda account: allowance.copy(), launch_key=lambda account: ""),
        cfg=replace(Config(), public_url="https://fleet.invalid/", cookie_secret="c" * 32),
        account={"id": "alice"}, session_id="session-alice",
    )


def render(context, *, language="en", **state):
    status, markup = trial_routes.page(
        context.runtime, context.cfg, context.account, context.session_id, **state
    )
    assert status == 200
    return project_html(markup, language, "/account/devin-trial")


def test_setup_is_three_ordered_steps_and_main_action_has_own_csrf(context):
    markup = render(context)
    steps = ["Create your trial key", "Copy your launch command", "Paste, launch, and ask something small"]
    positions = [markup.index(step) for step in steps]
    assert positions == sorted(positions)
    assert '<ol class="trial-steps">' in markup
    assert markup.count('<li class="trial-step">') == 3
    assert 'class="page wide"' in markup
    assert "Your command will appear here after you create a key." in markup
    assert 'id="trial-command"' not in markup
    parsed = TrialPage(markup)
    assert [form["attrs"]["action"] for form in parsed.forms] == [
        "/account/devin-trial/create", "/account/devin-trial/revoke"
    ]
    for form in parsed.forms:
        assert form["attrs"]["method"] == "post"
        assert form["inputs"] == [{
            "type": "hidden", "name": "csrf",
            "value": usersite.csrf_for(context.session_id, context.cfg.cookie_secret),
        }]


def test_key_creation_has_visible_success_and_copyable_command(context):
    token = "cct_" + "a" * 64
    markup = render(context, token=token)
    assert markup.index("Trial key created") < markup.index("Set up in three steps")
    assert "Copy and save the command in step 2." in markup
    assert "This command stays available here when you sign in" in markup
    command = html.unescape(re.search(r"<pre><code>(.*?)</code></pre>", markup)[1])
    words = shlex.split(command)
    assert words[0] == "env"
    assert "ANTHROPIC_AUTH_TOKEN=" + token in words
    assert "ANTHROPIC_BASE_URL=https://fleet.invalid/trial" in words
    executable = words.index("claude")
    assert words[executable:] == [
        "claude", "--model", "claude-opus-5-5", "--effort", "max",
        "--permission-mode", "bypassPermissions",
        "--disallowedTools", "WebSearch,WebFetch",
    ]
    assert "ENABLE_TOOL_SEARCH=false" in words[1:executable]
    assert "CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1" in words[1:executable]
    assert "CLAUDE_CODE_DISABLE_THINKING=1" in words[1:executable]
    assert "data-trial-copy" in markup and 'aria-live="polite"' in markup
    assert trial_routes.COPY_COMMAND_JS in markup
    assert trial_routes.COPY_COMMAND_JS not in render(context)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_saved_command_update_keeps_key_and_explains_session_only_defaults(context, language):
    markup = render(context, language=language)
    assert "ENABLE_TOOL_SEARCH=false" in markup
    assert "CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1" in markup
    assert "CLAUDE_CODE_DISABLE_THINKING=1" in markup
    assert "--permission-mode bypassPermissions" in markup
    assert "--disallowedTools WebSearch,WebFetch" in markup
    assert 'id="trial-command"' not in markup
    if language == "en":
        for phrase in (
            "keep its private key", "You do not need a replacement key",
            "project instructions", "compatible MCP servers and plugins",
            "Local coding tools run without approval prompts", "saved settings stay unchanged",
        ):
            assert phrase in markup
        assert "无需更换密钥" not in markup
    else:
        for phrase in (
            "保留原有私密密钥", "无需更换密钥", "暂时关闭自定义配置", "项目指令",
            "本地编程工具无需逐次批准即可运行", "已保存的设置不会改变",
        ):
            assert phrase in markup
        assert "You do not need a replacement key" not in markup


def test_command_text_escapes_token_and_keeps_it_out_of_attributes(context):
    token = "private<&'\"token"
    markup = render(context, token=token)
    command = html.unescape(re.search(r"<pre><code>(.*?)</code></pre>", markup)[1])
    assert "ANTHROPIC_AUTH_TOKEN=" + token in shlex.split(command)
    assert token not in markup
    assert html.escape(token) not in markup.split('<pre><code>')[0]


def test_allowance_meter_displays_remaining_units_and_holds_separately(context):
    markup = render(context)
    parsed = TrialPage(markup)
    assert parsed.meters == [{
        "min": "0", "max": "1000000", "value": "982500",
        "aria-label": "Daily trial allowance available",
    }]
    assert "982,500" in markup and "15,000" in markup and "2,500" in markup
    assert "Reported tokens" in markup and "Tokens held for accounting" in markup
    assert "Full-length turns covered now" in markup and ">73</dd>" in markup
    assert "Your runtime remaining" in markup and "Shared runtime remaining" in markup
    assert "11,680 seconds" in markup and "Each model turn reserves 160 seconds" in markup
    assert "Each reply has up to 2 minutes to generate." in markup
    assert "unused reserved seconds are returned" in markup
    assert "All users share 10,000,000 tokens per day." in markup
    assert "tokens available / 1,000,000 per day" in markup
    assert "US$0.20–US$19.65 API value" in markup
    assert "Estimated API value depends on input, cache and output mix" in markup
    assert "Unmeasured holds are budget reservations, not measured token use" in markup


def test_allowance_uses_runtime_limits_instead_of_hardcoded_meter(context):
    context.allowance.update(token_limit=250000, global_token_limit=2500000,
                             available_tokens=125000)
    markup = render(context)
    assert TrialPage(markup).meters[0]["max"] == "250000"
    assert "tokens available / 250,000 per day" in markup
    assert "All users share 2,500,000 tokens per day" in markup
    assert "US$0.03–US$2.50 API value" in markup


@pytest.mark.parametrize("shared", [False, True])
def test_runtime_exhaustion_shows_zero_turns_and_keeps_token_allowance_visible(context, shared):
    field = "global_remaining_runtime_seconds" if shared else "remaining_runtime_seconds"
    context.allowance.update({field: 80, "remaining_turns": 0, "remaining_requests": 13})
    markup = render(context)
    assert "Today&#x27;s trial runtime cannot fit another turn" in markup
    assert "80 seconds" in markup and "Each model turn reserves 160 seconds" in markup
    assert "Remaining tokens do not override the time limit" in markup
    assert "Full-length turns covered now" in markup and ">0</dd>" in markup
    assert "982,500" in markup
    chinese = render(context, language="zh")
    assert "今日剩余运行时间不足以开始下一轮" in chinese
    assert "预留 160 秒" in chinese and "80 秒" in chinese
    assert "Today's trial runtime" not in chinese


def test_limits_copy_uses_actual_runtime_and_request_caps(context):
    context.allowance.update(request_limit=90, global_request_limit=100,
                             runtime_limit_seconds=5400, global_runtime_limit_seconds=6000)
    markup = render(context)
    assert "90 turns and 90 reserved runtime minutes per person" in markup
    assert "100 turns and 100 minutes shared" in markup
    assert "quick replies use their elapsed time" in markup
    assert "rejected before execution do not reserve model runtime" in markup


@pytest.mark.parametrize("language", ["en", "zh"])
def test_builtin_web_tools_are_explicitly_unavailable_without_removing_mcp_option(context, language):
    markup = render(context, language=language)
    assert "--disallowedTools WebSearch,WebFetch" in markup
    if language == "en":
        assert "hosted WebSearch and WebFetch remain disabled" in markup
        assert "compatible local or MCP" in markup
    else:
        assert "托管的 WebSearch 和 WebFetch 仍关闭" in markup
        assert "兼容的本地或 MCP 工具" in markup


@pytest.mark.parametrize("language", ["en", "zh"])
def test_refresh_and_language_change_redisplay_key_without_creation_banner(context, language):
    token = "cct_" + "d" * 64
    accounts = []
    def launch_key(account):
        accounts.append(account)
        return token
    context.runtime.launch_key = launch_key
    for _ in range(2):
        markup = render(context, language=language)
        assert token in markup
        assert 'id="trial-command"' in markup
        assert "Trial key created" not in markup and "试用密钥已创建" not in markup
        assert trial_routes.COPY_COMMAND_JS in markup
    assert accounts == [context.account, context.account]
    assert token not in render(context, language=language, revoked=True)


def test_technical_accounting_privacy_and_revocation_start_in_closed_details(context):
    markup = render(context)
    parsed = TrialPage(markup)
    assert len(parsed.details) == 1 and "open" not in parsed.details[0]
    disclosure = markup[markup.index('<details class="trial-details">'):]
    assert "including overshoot" in disclosure
    assert "not a precise generation, vendor quota or USD hard cap" in disclosure
    assert "not added twice" in disclosure
    assert "Fleet receives the original request" in disclosure
    assert "Administrators technically control the worker" in disclosure
    assert 'action="/account/devin-trial/revoke"' in disclosure


def test_paused_service_and_successful_key_creation_are_explained_separately(context):
    context.allowance["paused"] = True
    markup = render(context, token="cct_" + "b" * 64)
    assert markup.index("Trial temporarily paused") < markup.index("Trial key created")
    assert "Creating another key will not restart it" in markup
    assert "Trial paused for verification. No new turn will start." in markup
    assert "Trial key created" in markup


@pytest.mark.parametrize("limit", ["available_tokens", "global_available_tokens", "remaining_requests",
                                   "remaining_global_requests", "remaining_leases",
                                   "remaining_global_leases"])
def test_exhausted_daily_limits_explain_reset_without_replacement(context, limit):
    context.allowance[limit] = 0
    markup = render(context)
    assert "Today's trial limit has been reached" in html.unescape(markup)
    assert "you do not need a replacement" in markup


def test_revocation_has_clear_confirmation_and_no_old_command(context):
    markup = render(context, revoked=True)
    assert "Trial key revoked" in markup
    assert "can no longer start trial requests" in markup
    assert 'id="trial-command"' not in markup
    assert "Trial key revoked" not in render(context)
    assert "The action could not be confirmed" in render(context, failed=True)


def test_chinese_setup_and_status_have_selected_language_only(context):
    context.allowance["paused"] = True
    markup = render(context, language="zh", token="cct_" + "c" * 64)
    assert "三步开始试用" in markup and "试用密钥已创建" in markup
    assert "更换密钥无法恢复服务" in markup and "刷新或切换语言不会丢失" in markup
    assert "Try Devin in Claude Code" not in markup
    assert 'action="/zh/account/devin-trial/create"' in markup
    assert TrialPage(markup).meters[0]["aria-label"] == "每日可用试用额度"
