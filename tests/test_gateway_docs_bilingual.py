"""The public gateway guide is self-contained and preserves operational boundaries."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from ccfleetd import bilingual

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "public-gateway.html"


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.links = []
        self.pres = []
        self.scripts = []
        self.controls = []
        self.paragraph_languages = []
        self.stack = []
        self.in_pre = False
        self.in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "button" and attrs.get("data-language-choice"):
            self.controls.append(attrs["data-language-choice"])
        if tag == "p":
            self.paragraph_languages.append(next((parent.get("lang") for parent_tag, parent in
                                                 reversed(self.stack) if parent_tag != "html"
                                                 and parent.get("lang")), ""))
        if tag == "pre":
            self.in_pre = True
            self.pres.append("")
        if tag == "script":
            assert "src" not in attrs, "language controls must not need remote JavaScript"
            self.in_script = True
            self.scripts.append("")
        if tag not in {"br", "meta", "link", "input", "hr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False
        if tag == "script":
            self.in_script = False
        if self.stack:
            assert self.stack[-1][0] == tag, (tag, self.stack[-1][0])
            self.stack.pop()

    def handle_data(self, data):
        if self.in_pre:
            self.pres[-1] += data
        if self.in_script:
            self.scripts[-1] += data


def test_guide_controls_script_and_existing_anchors_are_self_contained():
    html = GUIDE.read_text()
    parsed = Document(html)
    assert parsed.controls == ["en", "zh", "both"]
    assert parsed.scripts == [bilingual.SCRIPT]
    assert bilingual.CSS in html
    assert 'data-language="both"' in html
    assert all(count == 1 for count in Counter(parsed.ids).values())
    anchors = {"public-messages-gateway", "overview", "start", "choose", "request", "urls",
               "failover", "privacy", "keys", "limits", "api", "operations", "verification"}
    assert anchors <= set(parsed.ids)
    assert all(link[1:] in parsed.ids for link in parsed.links if link.startswith("#"))
    assert 'lang="en"' in html and 'lang="zh-Hans"' in html
    assert html.count('class="copy-en"') == html.count('class="copy-zh"')
    # Except the bilingual eyebrow, prose paragraphs are inside explicit language copies.
    assert parsed.paragraph_languages[0] == ""
    assert all(lang in {"en", "zh-Hans"} for lang in parsed.paragraph_languages[1:])


def test_runnable_examples_are_shared_once_and_keep_configuration_placeholders():
    parsed = Document(GUIDE.read_text())
    assert len(parsed.pres) == 7
    commands = "\n".join(parsed.pres)
    assert commands.count("curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh") == 1
    assert commands.count("ccfleet gateway setup --slots work-primary work-backup --routing sticky") == 1
    assert commands.count("ccfleet gateway setup --slots work-primary work-backup --routing priority") == 1
    assert commands.count("ccfleet local --slot work-primary") == 1
    assert commands.count("https://ccfleet.daichenlab.com/v1/messages") == 1
    assert commands.count("CCFLEET_PUBLIC_URL=https://fleet.example.com") == 1
    assert "CCFLEET_GATEWAY_SSH_KEY_PATH=/run/ccfleet-secrets/gateway_ed25519" in commands
    assert "CCFLEET_GATEWAY_SSH_PUBLIC_KEY=ssh-ed25519 YOUR_MATCHING_PUBLIC_KEY" in commands
    assert "anthropic-version: 2023-06-01" in commands
    assert "anthropic-beta: oauth-2025-04-20" in commands


def test_guide_separates_fixed_priority_sticky_and_website_cli_keys():
    html = GUIDE.read_text()
    assert "Owning A and B does not mean both answer every question" in html
    assert "拥有 A 和 B，不等于每个问题都会让两个账号回答" in html
    assert "Sticky keeps that conversation on B" in html and "保持会话模式则会继续用 B" in html
    assert "up to 24 hours after a normal response starts" in html
    assert "从正常回复开始时算起最多保留 24 小时" in html
    assert "briefly move behind the other choices" in html and "短暂排在其他选择后面" in html
    assert "does not import the website key" in html and "不会导入网页密钥" in html
    assert "Only the current user's own held hosted slots are selectable" in html
    assert "只能选择当前用户自己持有的托管槽位" in html
    assert "does not silently reactivate an old selection" in html
    assert "旧选择不会自动恢复" in html


def test_guide_preserves_safety_privacy_deployment_defaults_and_proof_limits():
    html = GUIDE.read_text()
    assert "once writing the model request begins" in html and "一旦开始写入模型请求" in html
    assert "even if zero response-body bytes have arrived" in html
    assert "即使还没收到任何回答正文" in html
    assert "BWH can read model requests and responses" in html
    assert "BWH 能读取模型请求和回答" in html
    assert "Current deployment settings, verified 2026-10-11" in html
    assert "8 active requests globally, 4 per key, and 64 accepted connections" in html
    assert "全局最多 8 个活跃请求，每个密钥最多 4 个，最多接受 64 条连接" in html
    assert "Library configuration default" in html and "代码中的默认设置" in html
    assert "16 active requests globally" in html and "128 accepted connections" in html
    assert "live provider exercise covered one pinned slot" in html
    assert "真实服务商测试只覆盖一个固定槽位" in html
    assert "https://github.com/cdcupt/ccfleet/commit/1494f6b0646188bbdab622ddd4321627df328c7c" in html
    assert "5,337 passes and 5 intentional skips" in html
    assert "not require a new DNS record" in html and "不需要新增 DNS 记录" in html
