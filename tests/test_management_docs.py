"""MCP is explicitly enabled and its model-visible observations are disclosed."""

from ccfleetd import customer_docs
from tests.test_html_document_preservation import ROOT, read_document, visible_html


def test_user_guide_discloses_opt_in_management_without_promising_anonymity(cfg):
    from tests.test_html_document_preservation import Document

    raw = customer_docs.guide(cfg)
    page = visible_html(raw)
    assert '<h3 id="management-mcp">' in raw
    assert any(
        "claude mcp add --transport stdio ccfleet -- ccfleet mcp serve" in code
        for code in Document(raw).pre_codes
    )
    for phrase in (
        "Nothing is registered automatically",
        "calling AI",
        "Missing observations",
        "cannot invoke Claude",
        "seven UTC calendar days",
        "not billing",
    ):
        assert phrase in page


def test_technical_sources_remain_while_current_comparison_uses_current_evidence():
    local = read_document(ROOT / "docs/local-relay.html")
    assert "management-mcp" in local.ids
    for tool in ("ccfleet_health", "ccfleet_quota", "ccfleet_relay_usage"):
        assert tool in local.text
    privacy = read_document(ROOT / "docs/reliability-privacy.html").text
    assert "synthetic private-parameter tests" in privacy.lower()
    assert "not a fingerprint-free promise" in privacy
    comparison = read_document(ROOT / "docs/platform-comparison.html").text
    for phrase in (
        "no live second-account switch was exercised",
        "not been proven equivalent",
        "no blanket superiority",
        "Vendor documentation; internals not audited",
    ):
        assert phrase in comparison
    assert "Earlier product-scope" not in comparison
