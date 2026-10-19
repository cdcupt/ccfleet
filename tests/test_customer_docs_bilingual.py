"""Customer guidance is understandable in either language without changing commands."""

from html.parser import HTMLParser

import pytest

from ccfleetd import bilingual, customer_docs, pricing, usersite
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document, normalized


class LanguageCopy(HTMLParser):
    """Read the authored language copies, omitting the frame and hidden source."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = {"en": [], "zh-Hans": []}
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        inherited = self.stack[-1][1] if self.stack else None
        language = attributes.get("lang", inherited)
        if tag not in {"br", "meta", "input", "link", "hr", "img"}:
            self.stack.append((tag, language))
        if language in self.parts:
            self.parts[language].append(" ")

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or any(tag in {"script", "style", "head"} for tag, _ in self.stack):
            return
        language = self.stack[-1][1]
        if language in self.parts:
            self.parts[language].append(data)

    def text(self, language):
        return normalized("".join(self.parts[language]))


@pytest.mark.parametrize(
    "page,english,chinese",
    [
        (customer_docs.overview, "Your computer. Your provider account.", "在你的电脑工作，使用你自己的账号"),
        (customer_docs.guide, "Getting started", "开始使用"),
        (customer_docs.how_it_works, "How it works", "CC Fleet 如何工作"),
        (customer_docs.terms, "The service", "服务内容"),
    ],
)
def test_complete_public_guidance_has_authored_english_and_chinese(page, english, chinese):
    source = page(Config())
    copy = LanguageCopy(source)
    assert english in copy.text("en")
    assert chinese in copy.text("zh-Hans")
    assert 'data-language="en"' in source
    assert "Claude" in copy.text("en") and "Claude" in copy.text("zh-Hans")
    assert 'lang="zh-Hans"' in source
    for language, expected, omitted in (("en", english, chinese), ("zh", chinese, english)):
        projected = bilingual.project_html(source, language, "/docs/guide")
        text = Document(projected).text
        assert expected in text and omitted not in text
        assert 'data-language="both"' not in projected
        assert "data-language-choice" not in projected
    document = Document(source)
    assert not document.duplicate_ids
    for target in (link[1:] for link in document.links if link.startswith("#")):
        assert target in document.ids


def test_slot_choice_and_safe_switching_are_explained_in_both_languages():
    source = customer_docs.guide(Config())
    copy = LanguageCopy(source)
    for phrase in (
        "Owning B does not add it automatically",
        "Priority",
        "Sticky",
        "Once public response headers are sent",
        "up to 24 hours",
        "not two live accounts",
        "Connect Claude",
    ):
        assert phrase in copy.text("en")
    for phrase in (
        "即使你还持有 B，也不会自动使用它",
        "按优先级选择",
        "保持会话账号",
        "即使屏幕还没出现文字",
        "最多保留 24 小时",
        "并非两个真实账号的线上实测",
        "我的槽位 → 连接 Claude",
    ):
        assert phrase in copy.text("zh-Hans")
    assert "API endpoints" not in copy.text("en")


def test_native_commands_stay_shared_and_website_is_an_alternative_to_cli():
    source = customer_docs.guide(Config())
    codes = Document(source).pre_codes
    install = (
        "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/"
        "laptop/install.sh | bash -s -- --setup"
    )
    assert sum(code.count(install) for code in codes) == 1
    assert (
        sum(
            code.count("ccfleet gateway setup --slots A B --routing sticky --replace")
            for code in codes
        )
        == 1
    )
    assert sum(code.count("ccfleet update --rollback") for code in codes) == 1
    text = Document(source).text
    assert "You do not need to configure both" in text
    assert "不需要把两种方式都操作一遍" in text
    assert "Updating the slot does not change your local Claude Code" in text
    assert "更新槽位不会改变你电脑上的 Claude Code" in text


def test_command_reference_explains_native_aliases_and_fleet_only_controls_in_both_languages():
    source = customer_docs.guide(Config())
    document = Document(source)
    assert "command-reference" in document.ids
    commands = (
        "ccfleet update", "ccfleet upgrade", "ccfleet -v", "ccfleet --version",
        "ccfleet version --json", "ccfleet local -c", "--continue", "ccfleet local -r",
        "--resume [NAME_OR_ID]", "ccfleet local --permission-mode plan",
        "ccfleet local --slot A --project PATH", "ccfleet local -- --verbose",
        "ccfleet list --remote",
    )
    for language in ("en", "zh"):
        rendered = bilingual.project_html(source, language, "/docs/guide")
        text = Document(rendered).text
        assert all(command in text for command in commands)
        assert "0.2.7" in text and "0.2.6" in text
        assert "--mode" in text
    copy = LanguageCopy(source)
    for phrase in ("pairing and local history are preserved", "Fleet-specific",
                   "already selected slot", "routing and authentication options",
                   "original command set"):
        assert phrase in copy.text("en")
    for phrase in ("配对与本地历史保持不变", "Fleet 专用", "已选中的槽位",
                   "路由与认证选项", "命令保持原样"):
        assert phrase in copy.text("zh-Hans")


def test_devin_cli_recipe_distinguishes_pairing_authorization_current_slot_and_provider():
    source = customer_docs.guide(Config())
    assert "devin-cli" in Document(source).ids
    recipe = ("ccfleet update\nccfleet list --remote --slot PAIRED_SLOT\n"
              "ccfleet gateway setup --slot PAIRED_SLOT --slots DEVIN_SLOT --provider devin\n"
              "ccfleet local --transport gateway --slot DEVIN_SLOT --model claude-opus-5-5 "
            "--effort max --new -- --safe-mode")
    assert recipe in Document(source).pre_codes
    for language in ("en", "zh"):
        rendered = bilingual.project_html(source, language, "/docs/guide")
        assert recipe in Document(rendered).pre_codes
    copy = LanguageCopy(source)
    for phrase in ("exactly one verified slot of your own", "does not sign in to Devin",
                   "new connection defaults to Anthropic", "same saved pairing and server",
                   "saved pairings, which can contain old names"):
        assert phrase in copy.text("en")
    for phrase in ("一个已验证的自有槽位", "不是登录 Devin", "新连接默认 Anthropic",
                   "同一配对和服务器保留已有服务选择", "可能有旧名称"):
        assert phrase in copy.text("zh-Hans")


def test_gateway_address_and_published_contact_are_escaped_in_both_copies():
    source = customer_docs.guide(
        Config(
            public_url='https://fleet.example/<unsafe>&"', contact_email="contact&help@example.com"
        )
    )
    assert "<unsafe>" not in source
    assert "https://fleet.example/&lt;unsafe&gt;&amp;&quot;" in source
    assert "contact&help@example.com" not in source
    assert 'href="mailto:contact&amp;help@example.com"' in source
    viewer = usersite.Viewer(
        {"id": "test", "role": "admin"},
        "test",
        slots_href='/account?x="<unsafe>',
        console_href='https://example.com/admin?x="<unsafe>',
    )
    landing = customer_docs.overview(Config(), viewer=viewer)
    assert "<unsafe>" not in landing
    assert 'href="/account?x=&quot;&lt;unsafe&gt;"' in landing


def test_terms_and_prices_do_not_imply_included_claude_or_reveal_raw_markup():
    source = customer_docs.terms(Config(), price=pricing.Price("<unsafe>", "USD"))
    assert "<unsafe>" not in source and "&lt;unsafe&gt;" not in source
    assert "Pricing is unavailable" in source
    copy = LanguageCopy(source)
    assert "does not provide access to Claude" in copy.text("en")
    assert "不提供 Claude 或 Devin 订阅或账号" in copy.text("zh-Hans")
    assert "not backed up" in copy.text("en") and "没有备份" in copy.text("zh-Hans")


def test_diagrams_and_privacy_have_distinct_language_ids_and_plain_explanations():
    source = customer_docs.how_it_works(Config())
    document = Document(source)
    for namespace in ("provider", "provider-zh"):
        assert f"{namespace}-title" in document.ids
        assert f'aria-labelledby="{namespace}-title {namespace}-desc"' in source
    assert not document.duplicate_ids
    copy = LanguageCopy(source)
    assert "The gateway can read model requests and responses" in copy.text("en")
    assert "网关可以读取模型请求和回复" in copy.text("zh-Hans")
    assert "机器管理员拥有 root 权限" in copy.text("zh-Hans")
    assert "真实线上验证使用了一个实际账号" in copy.text("zh-Hans")


@pytest.mark.parametrize(
    "page",
    [customer_docs.overview, customer_docs.guide, customer_docs.how_it_works, customer_docs.terms],
)
def test_customer_pages_only_teach_the_current_workflow(page):
    text = Document(page(Config())).text
    for removed in (
        "--transport bridge",
        "--legacy-history",
        "ccfleet-connect",
        "migration guide",
        "live-folder",
        "Remote-terminal compatibility",
        "old remote history",
        "旧桥接",
        "旧路线",
        "迁移指南",
    ):
        assert removed not in text
