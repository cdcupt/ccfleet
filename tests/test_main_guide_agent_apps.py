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
                       for anchor in ("setup", "add-slot", "model-connections", "t3-gemini", "agent-compatibility", "agent-apps", "local-work", "slot-care")]
    assert all(target.split("#", 1)[1] in page.ids for target in targets)
    link = links[1]
    assert all(parent.tag != "details" for parent in link.parents)
    assert source.index('class="guide-jumps"') < source.index('id="agent-apps"')
    assert source.index('id="setup"') < source.index('id="agent-apps"')
    assert source.index('id="agent-apps"') < source.index('id="local-work"')
    assert section.attrs["aria-labelledby"] == "agent-apps-title"
    assert not page.duplicate_ids
    assert all(link[1:] in page.ids for link in page.links if link.startswith("#"))
    assert any(target and target[0] == DETAILS for target in map(bilingual.route_target, page.links))


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
    assert any("integrations/herdr/programs.json" in link for link in page.links)
    for card in cards[:2]:
        assert "CCFleet Gemini" in card.text and "CCFleet Claude" in card.text
    assert "Quick Commands" in cards[0].text and "ccfleet gemini" in cards[0].text
    assert "ccfleet pair gemini" in section.text
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
            "Herdr TUI is separate", "Full Claude app/model sessions remain unverified"),
     "完整的 Claude 应用／模型会话仍未验证"),
    ("zh", ("一个启动器固定一个槽位", "不会切换此启动器的 Fleet 槽位", "不会额外指定默认模型",
            "Codex 仍使用自己的连接", "Herdr TUI 与桌面应用是不同程序", "完整的 Claude 应用／模型会话仍未验证"),
     "Full Claude app/model sessions remain unverified"),
])
def test_main_guide_preserves_account_and_verification_boundaries(language, required, omitted):
    flow = Flow(rendered(language))
    section, = flow.find("section", id="agent-apps")
    text = normalized(section.text)
    assert all(value in text for value in required)
    assert omitted not in text
    assert "Devin" in text and "T3" in text and "claude --resume" in text


@pytest.mark.parametrize("language,heading", [
    ("en", "Use Claude and Gemini in Orca, Herdr or T3 Code"),
    ("zh", "在 Orca、Herdr 或 T3 Code 中使用 Claude 和 Gemini"),
])
def test_localized_public_guide_serves_the_app_setup(one_site, language, heading):
    code, body = one_site(f"/{language}/docs/guide")
    assert code == 200
    page = Document(body)
    assert ("h2", heading) in page.headings
    assert RECIPE in page.pre_codes and "agent-apps" in page.ids

@pytest.mark.parametrize("language,planned,other", [
    ("en", "Both use the Antigravity subscription", "两种方式都使用专属 Mac 槽位"),
    ("zh", "两种方式都使用专属 Mac 槽位", "Both use the Antigravity subscription"),
])
def test_subscription_guide_separates_fleet_from_publisher_support(language, planned, other):
    flow = Flow(rendered(language))
    section, = flow.find("section", id="agent-compatibility")
    assert planned in section.text and other not in section.text
    rows = flow.inside(section, "tr")
    assert {cell.text for cell in flow.inside(rows[0], "th")} == (
        {"Tool", "Claude subscription", "Antigravity subscription", "Sources"} if language == "en"
        else {"工具", "Claude 订阅", "Antigravity 订阅", "资料"})
    tools = {flow.inside(row, "th")[0].text for row in rows[1:]}
    assert {"T3 Code", "Pi", "Zed", "OpenCode · Kilo Code", "Cline", "Codex"} <= tools
    for row in rows[1:]:
        assert len(flow.inside(row, "td")) == 3
        assert flow.inside(row, "a")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_t3_subscription_recipe_matches_the_account_provider_steps(language):
    from ccfleetd import gemini_setup_ui

    html = rendered(language)
    flow = Flow(html)
    section, = flow.find("section", id="t3-gemini")
    assert "ccfleet update\nccfleet pair gemini" in Document(html).pre_codes
    links = [bilingual.route_target(e.attrs["href"]) for e in flow.inside(section, "a")]
    assert any(link and link[0] == "/account/gemini#subscription" for link in links)
    setup = bilingual.project_html(gemini_setup_ui.t3_setup(), language, "/account/gemini")
    for source in (section.text, normalized(Flow(setup).find("details")[0].text)):
        assert "Local ACP command" in source and "command -v ccfleet" in source
        assert "gemini" in source and "--acp" in source and "Supervised" in source
    assert "--preview" not in section.text and "ANTHROPIC_BASE_URL" not in section.text


@pytest.mark.parametrize("language,window,fresh,expired", [
    ("en", "five minutes", "fresh", "expire"),
    ("zh", "五分钟", "重新开始", "失效"),
])
def test_account_and_project_guides_explain_gemini_recovery(language, window, fresh, expired):
    from ccfleetd import gemini_setup_ui

    guide = Flow(rendered(language))
    heading, = guide.find("h3", id="gemini-recovery")
    t3, = guide.find("section", id="t3-gemini")
    apps, = guide.find("section", id="agent-apps")
    assert heading in guide.inside(t3, "h3")
    assert "Steer" in t3.text
    for section in (t3, apps):
        text = normalized(section.text)
        assert all(term in text for term in ("0.2.21", "0.2.22", "ccfleet update", "/reconnect",
                                             window, fresh, expired))
    account = Flow(bilingual.project_html(gemini_setup_ui.t3_setup(), language, "/account/gemini"))
    for source, section in ((guide, apps), (account, account.find("details")[0])):
        targets = [bilingual.route_target(link.attrs["href"]) for link in source.inside(section, "a")]
        assert any(target and target[0] == "/docs/guide#gemini-recovery" for target in targets)


@pytest.mark.parametrize("language,phrases", [
    ("en", ("offline", "standalone clone", "GitHub.com", "GitLab.com", "exact local commit")),
    ("zh", ("离线", "独立", "GitHub.com", "GitLab.com", "当前本地提交")),
])
def test_gemini_project_access_setup_links_and_commands(language, phrases):
    from ccfleetd import gemini_setup_ui

    guide = Flow(rendered(language))
    heading, = guide.find("h3", id="gemini-project-access")
    assert heading.text
    t3, = guide.find("section", id="t3-gemini")
    detail = normalized(t3.text)
    assert all(s in detail for s in ("0.2.23", "ENABLE", "--full-access", "--publish-to",
                                     "--revoke-full-access", "gh", "glab", *phrases))
    apps, = guide.find("section", id="agent-apps")
    account = Flow(bilingual.project_html(gemini_setup_ui.t3_setup(), language, "/account/gemini"))
    for source, section in ((guide, apps), (account, account.find("details")[0])):
        targets = [bilingual.route_target(link.attrs["href"]) for link in source.inside(section, "a")]
        assert any(t and t[0] == "/docs/guide#gemini-project-access" for t in targets)


@pytest.mark.parametrize("language,term", [("en", "without approval prompts"), ("zh", "不弹出确认请求")])
def test_all_projects_home_grant_uses_one_local_setup(language, term):
    html = rendered(language)
    guide = Flow(html)
    t3, = guide.find("section", id="t3-gemini")
    assert "ccfleet update\ncd ~\nccfleet gemini --full-access" in Document(html).pre_codes
    assert all(v in normalized(t3.text) for v in ("0.2.24", "/workspace ~/code/app", term,
                                                   "Orca", "Herdr", "--revoke-full-access"))
