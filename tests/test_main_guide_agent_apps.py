"""The public getting-started guide exposes usable, bounded app setup."""

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_account_setup_flow import Flow
from tests.test_customer_docs import one_site as one_site
from tests.test_html_document_preservation import Document, normalized

RECIPE = "ccfleet update\nccfleet launcher create daily-claude --slot SLOT"
DETAILS = "/docs/library/docs/platform-comparison.html#agent-apps"


def rendered(language, *, optional_sections=False):
    cfg = Config(devin_trial_enabled=optional_sections,
                 stripe_subscriptions_enabled=optional_sections)
    source = customer_docs.guide(cfg, per_slot_pricing=optional_sections,
                                 wallet_enabled=optional_sections)
    return bilingual.project_html(source, language, "/docs/guide")


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("optional_sections", [False, True])
def test_main_guide_app_setup_is_visible_and_linked_from_its_intro(language, optional_sections):
    source = rendered(language, optional_sections=optional_sections)
    page, flow = Document(source), Flow(source)
    section, = flow.find("section", id="agent-apps")
    assert all(parent.tag != "details" for parent in section.parents)
    navigation, = [element for element in flow.find("nav") if element.has_class("guide-jumps")]
    links = flow.inside(navigation, "a")
    targets = [bilingual.route_target(element.attrs["href"])[0] for element in links]
    assert targets == ["/docs/guide#" + anchor
                       for anchor in ("setup", "agent-apps", "local-work", "slot-care")]
    assert all(target.split("#", 1)[1] in page.ids for target in targets)
    link = links[1]
    assert all(parent.tag != "details" for parent in link.parents)
    assert source.index('class="guide-jumps"') < source.index('id="agent-apps"')
    assert source.index('id="setup"') < source.index('id="agent-apps"')
    assert source.index('id="agent-apps"') < source.index('id="local-work"')
    assert section.attrs["aria-labelledby"] == "agent-apps-title"
    assert not page.duplicate_ids
    assert all(link[1:] in page.ids for link in page.links if link.startswith("#"))
    assert any(bilingual.route_target(link)[0] == DETAILS for link in page.links)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_each_app_has_configuration_on_the_main_page_not_only_a_library_link(language):
    source = rendered(language)
    page, flow = Document(source), Flow(source)
    section, = flow.find("section", id="agent-apps")
    cards = flow.inside(section, "article")
    assert len(cards) == 3
    assert [flow.inside(card, "h3")[0].text for card in cards] == ["Orca", "Herdr", "T3 Code"]
    assert all(card.has_class("connection-choice") for card in cards)
    assert RECIPE in page.pre_codes
    assert '"run": ["/absolute/path/from-the-command"]' in page.pre_codes
    assert all(text in cards[0].text for text in ("Settings → Agents", "Command", "Arguments"))
    assert all(text in cards[1].text for text in ("programs.json", "run"))
    assert all(text in cards[2].text for text in (
        "Settings → Providers", "Binary path", "ANTHROPIC_BASE_URL", "https://ccfleet.io"))
    assert all(text in section.text for text in ("0.2.9", "macOS", "Linux", "--claude PATH"))
    assert '"connection-choices"' in source
    assert ".connection-choices{grid-template-columns:minmax(0,1fr)}" in source


@pytest.mark.parametrize("language,required,omitted", [
    ("en", ("One launcher, one selected slot.", "does not switch this launcher’s Fleet slot",
            "adds no default model, effort level or permission bypass", "Codex keeps its own connection",
            "Herdr TUI is separate", "Full app/model sessions remain unverified"),
     "完整的应用／模型会话仍未验证"),
    ("zh", ("一个启动器固定一个槽位", "不会切换此启动器的 Fleet 槽位", "不会额外指定默认模型",
            "Codex 仍使用自己的连接", "Herdr TUI 与桌面应用是不同程序", "完整的应用／模型会话仍未验证"),
     "Full app/model sessions remain unverified"),
])
def test_main_guide_preserves_account_and_verification_boundaries(language, required, omitted):
    flow = Flow(rendered(language))
    section, = flow.find("section", id="agent-apps")
    text = normalized(section.text)
    assert all(value in text for value in required)
    assert omitted not in text
    assert "Devin" in text and "T3" in text and "claude --resume" in text


@pytest.mark.parametrize("language,heading", [
    ("en", "Use Orca, Herdr or T3 Code"),
    ("zh", "使用 Orca、Herdr 或 T3 Code"),
])
def test_localized_public_guide_serves_the_app_setup(one_site, language, heading):
    code, body = one_site(f"/{language}/docs/guide")
    assert code == 200
    page = Document(body)
    assert ("h2", heading) in page.headings
    assert RECIPE in page.pre_codes and "agent-apps" in page.ids
