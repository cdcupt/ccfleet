"""The current gateway guide teaches one clear flow in separate language pages."""

from pathlib import Path

import pytest

from ccfleetd import bilingual
from tests.test_html_document_preservation import Document

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "public-gateway.html"
TARGET = "/docs/library/docs/public-gateway.html"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_current_guide_navigation_and_anchors_are_self_contained(language):
    source = GUIDE.read_text()
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert "data-language-choice" not in source and 'data-language="both"' not in source
    html = bilingual.project_html(source, language, TARGET)
    parsed = Document(html)
    assert not parsed.duplicate_ids
    anchors = {
        "overview",
        "start",
        "choose",
        "urls",
        "failover",
        "privacy",
        "keys",
        "updates",
        "verification",
    }
    assert anchors <= parsed.ids
    assert all(link[1:] in parsed.ids for link in parsed.links if link.startswith("#"))
    assert f'href="/en{TARGET}"' in html and f'href="/zh{TARGET}"' in html
    for removed in (
        "--transport bridge",
        "--legacy-history",
        "CCFLEET_GATEWAY_SSH_KEY_PATH",
        "historical",
        "operator setup",
    ):
        assert removed.lower() not in parsed.text.lower()


def test_shared_runnable_examples_do_not_duplicate_or_expose_operator_configuration():
    commands = "\n".join(Document(GUIDE.read_text()).pre_codes)
    assert (
        commands.count(
            "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh"
        )
        == 1
    )
    assert commands.count("ccfleet gateway setup --slots A B --routing sticky --replace") == 1
    assert commands.count("ccfleet gateway setup --slots A B --routing priority --replace") == 1
    assert commands.count("ccfleet local --slot A") == 1
    assert commands.count("https://ccfleet.daichenlab.com/v1/messages") == 1
    assert "YOUR_MATCHING_PUBLIC_KEY" not in commands
    assert "CCFLEET_GATEWAY_SSH" not in commands


def test_account_choices_and_saved_url_are_explained_without_adding_accounts():
    text = Document(bilingual.project_html(GUIDE.read_text(), "en", TARGET)).text
    for phrase in (
        "reception desk",
        "seat reserved",
        "Only your own held slots",
        "does not merge credentials or combine plan limits",
        "Fixed",
        "Priority",
        "Sticky",
        "does not add B automatically",
        "up to 24 hours",
        "does not start a local HTTP server",
        "not the slot's Claude credential",
        "does not automatically restore an old selection",
    ):
        assert phrase in text
    text_zh = Document(bilingual.project_html(GUIDE.read_text(), "zh", TARGET)).text
    for phrase in (
        "收件前台",
        "账号的座位",
        "只能选择你自己持有的槽位",
        "固定账号",
        "按优先级选择",
        "保持会话账号",
        "最多 24 小时",
        "不表示消息存放在这里",
    ):
        assert phrase in text_zh


def test_safe_failover_and_content_visibility_keep_current_proof_limits():
    text = Document(bilingual.project_html(GUIDE.read_text(), "en", TARGET)).text
    for phrase in (
        "uncertain result is not replayed",
        "After public response headers",
        "even if no visible text",
        "BWH can read model messages and replies",
        "directly from the laptop",
        "bypass mode",
        "root access",
        "one actual account",
        "not a live second-account pilot",
        "provider approval",
        "native TLS fingerprint parity",
    ):
        assert phrase in text
    text_zh = Document(bilingual.project_html(GUIDE.read_text(), "zh", TARGET)).text
    # Uncertainty is explained as a lost receipt rather than a transport-code catalog.
    assert "回执丢了" in text_zh
    assert "即使还没看到文字" in text_zh
    assert "一个实际账号" in text_zh and "不等于第二个真实账号" in text_zh
