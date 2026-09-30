"""Simple user checks are separated from privacy guarantees and model audits."""
import pytest

from ccfleetd import customer_docs
from tests.test_html_document_preservation import ROOT, read_document, visible_html

PROMPT = ("Do not use tools, read files, or inspect my system or network. "
          "Reply only: CCFLEET_OK")


@pytest.fixture(params=["website", "guidebook"])
def section(request, cfg):
    if request.param == "website":
        source = customer_docs.guide(cfg)
        source = source.split('<h3 id="simple-verification">', 1)[1]
        source = source.split('<h3 id="management-mcp">', 1)[0]
    else:
        source = (ROOT / "docs/guidebook.html").read_text()
        source = source.split('<section class="card note" id="simple-verification">', 1)[1]
        source = source.split('</section>', 1)[0]
    return visible_html(source)


def test_simple_check_uses_current_client_and_no_system_inspection(section):
    for phrase in ("current native-local client", "normal terminal", "not inside Claude",
                   "ccfleet local --check", "does not scan projects", "start a Claude session",
                   "make a model request", "not proof that Anthropic will accept"):
        assert phrase in section
    assert PROMPT in section


def test_prompt_is_optional_inference_not_an_enforced_privacy_test(section):
    for phrase in ("optional", "makes a model request", "plan allowance",
                   "basic responsiveness", "zero metadata disclosure",
                   "not an enforced privacy safeguard", "Native context or hooks",
                   "local OS or directory information", "Do not ask Claude to print real IPs",
                   "BWH sees connection metadata", "for relayed model requests",
                   "slot's outbound IP", "Other native/tool network connections"):
        assert phrase in section
    for instruction in ("git clone", "pytest", "ipify", "ifconfig", "tcpdump", "printenv"):
        assert instruction not in section


def test_guidebook_preserves_historical_scope_and_links_current_user_check():
    page = read_document(ROOT / "docs/guidebook.html")
    assert "simple-verification" in page.ids
    assert "Historical hosted-terminal guidebook" in page.text[:1000]
    assert "https://ccfleet.daichenlab.com/docs/guide#simple-verification" in page.links
