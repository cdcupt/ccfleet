from ccfleetd.render import build_rows, render_dashboard
from tests.conftest import heartbeat

NOW = 3_000_000.0


def test_rows_merge_alerts_and_escape_html(cfg):
    nodes = [{"id": "node-a", "owner": "<script>alert(1)</script>", "region": "us",
              "pinned_version": "2.1.90", "rc_expected": True, "enabled": True, "created_at": 0},
             {"id": "node-b", "owner": "sam", "region": "", "pinned_version": "",
              "rc_expected": False, "enabled": False, "created_at": 0}]
    latest = {"node-a": heartbeat(NOW - 30)}
    alerts = [{"node_id": "node-a", "rule": "version_mismatch", "level": "warn",
               "message": "claude 2.1.92 differs from pinned 2.1.90", "opened_at": NOW - 100},
              {"node_id": "node-a", "rule": "disk_high", "level": "critical",
               "message": "disk 97% used", "opened_at": NOW - 50}]
    rows = build_rows(nodes, latest, alerts, NOW)
    assert rows[0]["status"] == "critical" and rows[0]["open_alerts"] == ["version_mismatch",
                                                                          "disk_high"]
    assert rows[1]["status"] == "disabled" and rows[1]["last_seen_ts"] is None
    html = render_dashboard(rows, alerts, NOW, cfg)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "≠ pinned" in html and "203.0.113.10" in html
    assert 'class="pill critical"' in html and "active (expected)" in html
    assert "refreshed 10m ago (max)" in html


def test_empty_dashboard_has_hint(cfg):
    html = render_dashboard([], [], NOW, cfg)
    assert "No nodes yet" in html and "none" in html


def test_add_result_page_is_escaped_and_complete(cfg):
    from ccfleetd.render import render_add_result
    page = render_add_result("node-a", "f" * 64, cfg, owner="erik")
    assert "f" * 64 in page
    assert "--owner erik" in page and "--node node-a" in page
    assert "CCFLEET_NODE_TOKEN=" in page, "the manual fallback is still offered"
    # an owner name is user input and must not be able to inject markup
    nasty = render_add_result("node-a", "f" * 64, cfg, owner='"><script>x</script>')
    assert "<script>" not in nasty


def test_install_command_is_shell_safe_even_if_a_bad_owner_got_stored(cfg):
    """Second line of defence: the store validates, and render quotes regardless."""
    from ccfleetd.render import render_add_result
    page = render_add_result("node-a", "f" * 64, cfg, owner="alice; rm -rf /")
    assert "--owner 'alice; rm -rf /'" in page or "--owner &#x27;alice; rm -rf /&#x27;" in page
    assert "--owner alice; rm -rf /" not in page, "an unquoted owner would execute on paste"


def _add_page(bypass: bool):
    from ccfleetd.config import Config
    from ccfleetd.render import render_add_result
    cfg = Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:",
                           "CCFLEET_BYPASS_BY_DEFAULT": "1" if bypass else "0"})
    return render_add_result("node-a", "f" * 64, cfg, owner="erik")


def test_add_page_omits_the_bypass_flag_by_default():
    page = _add_page(False)
    assert "--bypass-permissions" not in page
    assert "without permission prompts" not in page


def test_add_page_emits_the_bypass_flag_and_says_so_when_configured():
    page = _add_page(True)
    assert "--bypass-permissions" in page
    # The flag must never appear without the sentence explaining what it costs.
    assert "without permission prompts" in page
    assert "passwordless sudo" in page
