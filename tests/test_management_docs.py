"""MCP is explicitly enabled and its model-visible observations are disclosed."""
from ccfleetd import customer_docs
from tests.test_html_document_preservation import ROOT, read_document, visible_html


def test_user_guide_discloses_opt_in_management_without_promising_anonymity(cfg):
    raw = customer_docs.guide(cfg)
    page = visible_html(raw)
    assert '<h3 id="management-mcp">' in raw
    assert ('<code class="code-wrap">claude mcp add --transport stdio ccfleet '
            '-- ccfleet mcp serve</code>') in raw
    for phrase in ("ccfleet mcp serve", "claude mcp add --transport stdio ccfleet",
                   "Nothing is registered automatically", "calling AI", "Missing observations",
                   "cannot invoke Claude", "seven UTC calendar days", "not billing"):
        assert phrase in page


def test_canonical_html_guides_describe_new_tools_and_keep_privacy_boundaries():
    local = read_document(ROOT / "docs/local-relay.html")
    assert "management-mcp" in local.ids
    for tool in ("ccfleet_health", "ccfleet_quota", "ccfleet_relay_usage"):
        assert tool in local.text
    privacy = read_document(ROOT / "docs/reliability-privacy.html").text
    assert "synthetic private-parameter tests" in privacy.lower()
    assert "not a fingerprint-free promise" in privacy
    comparison = read_document(ROOT / "docs/platform-comparison.html").text
    assert "Client 0.2.2" in comparison and "broader queries" in comparison
    assert "not remaining blockers" in comparison and "activation gate" in comparison
