"""Users can connect a new slot without inventing a second computer pairing."""

import re
import runpy
import shlex
from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document
from tests.test_technical_docs_design import Placement

ROOT = Path(__file__).resolve().parents[1]


def guide(language):
    return bilingual.project_html(customer_docs.guide(Config()), language, "/docs/guide")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_additional_slot_and_routing_instructions_are_visible_before_app_integrations(language):
    source = guide(language)
    parsed = Document(source)
    assert {"setup", "add-slot", "model-connections", "agent-apps"} <= parsed.ids
    assert source.index('id="setup"') < source.index('id="add-slot"')
    assert source.index('id="add-slot"') < source.index('id="model-connections"')
    assert source.index('id="model-connections"') < source.index('id="agent-apps"')
    for _, attrs, parents in Placement(source).elements:
        if attrs.get("id") in {"add-slot", "model-connections"}:
            assert all(tag != "details" for tag, _ in parents)
    assert not parsed.duplicate_ids
    assert ("Advanced connections" if language == "en" else "高级连接设置") in parsed.text
    assert all(link[1:] in parsed.ids for link in parsed.links if link.startswith("#"))


@pytest.mark.parametrize("language", ["en", "zh"])
def test_add_slot_and_pool_recipes_are_runnable_and_distinguish_discovery_from_selection(language):
    parser = runpy.run_path(str(ROOT / "laptop/ccfleet"), run_name="guide_parser")["parser"]()
    parsed = Document(guide(language))
    single_slot = (
        "ccfleet update\nccfleet list --remote\n"
        "ccfleet gateway setup --slots slot-5678 --provider anthropic\n"
        "ccfleet local --check\nccfleet local"
    )
    assert single_slot in parsed.pre_codes
    assert "ccfleet use --pool\nccfleet gateway status\nccfleet local --check\nccfleet local" in parsed.pre_codes
    modes = {}
    for block in parsed.pre_codes:
        for line in block.splitlines():
            if not line.startswith("ccfleet "):
                continue
            args = parser.parse_args(shlex.split(line)[1:])
            if args.command == "gateway" and getattr(args, "slots", None) == ["slot-1234", "slot-5678"]:
                modes[args.routing] = args
    assert set(modes) == {"sticky", "priority"}
    assert all(args.provider == "anthropic" for args in modes.values())
    assert "configured slot pool" in parsed.text
    if language == "en":
        assert "You do not need another computer or a new pairing for each slot" in parsed.text
        assert "only discovers your slots; it does not add them" in parsed.text
        assert "complete selected list" in parsed.text
        assert "a new conversation" in parsed.text and "for every request" in parsed.text
        assert "released or revoked pairing cannot authorize" in parsed.text
        assert "saves a single-slot override" in parsed.text
    else:
        assert "无需换电脑，也无需重新配对" in parsed.text
        assert "只是查询你的槽位，不会把它们加入" in parsed.text
        assert "完整的槽位选择列表" in parsed.text
        assert "新会话优先" in parsed.text and "每条请求时重新检查" in parsed.text
        assert "已归还槽位或已撤销的配对不能授权" in parsed.text
        assert "会保存单槽位固定选择" in parsed.text
    assert not re.search(r"\b(?:pool-[A-Za-z0-9]+|erik-[12]|PAIRED_SLOT|DEVIN_SLOT)\b", parsed.text)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_canonical_gateway_reference_has_the_same_onboarding_and_pool_controls(language):
    source = (ROOT / "docs/public-gateway.html").read_text()
    parsed = Document(bilingual.project_html(source, language))
    assert {"add-slot", "choose"} <= parsed.ids
    assert not parsed.duplicate_ids
    assert ("Advanced connections" if language == "en" else "高级连接设置") in parsed.text
    assert "ccfleet use --pool\nccfleet gateway status\nccfleet local --check\nccfleet local" in parsed.pre_codes
    commands = "\n".join(parsed.pre_codes)
    assert "ccfleet gateway setup --slots slot-1234 slot-5678 --routing sticky" in commands
    assert "ccfleet gateway setup --slots slot-1234 slot-5678 --routing priority" in commands
    assert "ccfleet gateway setup --slots slot-5678 --provider anthropic" in commands
    assert not re.search(r"\b(?:pool-[A-Za-z0-9]+|erik-[12]|PAIRED_SLOT|DEVIN_SLOT)\b", parsed.text)
