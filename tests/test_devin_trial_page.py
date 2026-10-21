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
        "available_tokens": 82500, "reported_tokens": 15000, "reserved_tokens": 2500,
        "unmeasured_reserved_tokens": 2500, "partial_reported_tokens": 1000,
        "global_available_tokens": 982500, "remaining_requests": 18,
        "resets_at": 1735776000, "paused": False,
    }
    return SimpleNamespace(
        allowance=allowance,
        runtime=SimpleNamespace(allowance=lambda account: allowance.copy()),
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
    assert "save this command before leaving this page or switching language" in markup
    command = html.unescape(re.search(r"<pre><code>(.*?)</code></pre>", markup)[1])
    words = shlex.split(command)
    assert words[0] == "env"
    assert "ANTHROPIC_AUTH_TOKEN=" + token in words
    assert "ANTHROPIC_BASE_URL=https://fleet.invalid/trial" in words
    assert words[-5:] == ["claude", "--model", "claude-opus-5-5", "--effort", "max"]
    assert "data-trial-copy" in markup and 'aria-live="polite"' in markup
    assert trial_routes.COPY_COMMAND_JS in markup
    assert trial_routes.COPY_COMMAND_JS not in render(context)


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
        "min": "0", "max": "100000", "value": "82500",
        "aria-label": "Daily trial allowance available",
    }]
    assert "82,500" in markup and "15,000" in markup and "2,500" in markup
    assert "Reported usage" in markup and "Held for accounting" in markup
    assert "Your turns left" in markup and ">18</dd>" in markup
    assert "All users share 1,000,000 units per day." in markup


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


@pytest.mark.parametrize("limit", ["available_tokens", "global_available_tokens", "remaining_requests"])
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
    assert "更换密钥无法恢复服务" in markup and "切换语言前" in markup
    assert "Try Devin in Claude Code" not in markup
    assert 'action="/zh/account/devin-trial/create"' in markup
    assert TrialPage(markup).meters[0]["aria-label"] == "每日可用试用额度"
