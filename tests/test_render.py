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
    assert "max \u00b7 refreshed 10m ago" in html


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


def _row_with_upgrade(upgrade):
    from ccfleetd.render import build_rows
    nodes = [{"id": "node-a", "owner": "erik", "region": "us", "pinned_version": "2.1.99",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    hb = heartbeat(NOW - 30)
    hb["payload"]["reconcile"] = {"upgrade": upgrade}
    return build_rows(nodes, {"node-a": hb}, [], NOW)[0]


def test_row_carries_the_last_upgrade_result():
    row = _row_with_upgrade({"from": "2.1.90", "to": "2.1.99", "ok": True,
                             "ts": 1.0, "error": None})
    assert row["last_upgrade"]["ok"] is True
    # A node that has never reported one must not look like a failure.
    from ccfleetd.render import build_rows
    nodes = [{"id": "node-a", "owner": "erik", "region": "us", "pinned_version": "",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    assert build_rows(nodes, {"node-a": heartbeat(NOW - 30)}, [], NOW)[0]["last_upgrade"] is None


def test_a_failed_upgrade_is_visible_and_escaped_in_the_dashboard():
    from ccfleetd.render import _row_html
    row = _row_with_upgrade({"from": "2.1.90", "to": "2.1.99", "ok": False, "ts": 1.0,
                             "error": "<script>alert(1)</script> no such version"})
    html = _row_html(row, NOW)
    assert "upgrade to 2.1.99 failed" in html
    # The error text comes from a node, so it is untrusted input on an admin page.
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_a_successful_upgrade_does_not_shout():
    from ccfleetd.render import _row_html
    html = _row_html(_row_with_upgrade({"from": "2.1.90", "to": "2.1.99", "ok": True,
                                        "ts": 1.0, "error": None}), NOW)
    assert "failed" not in html


def _signin(logins, present=False):
    from ccfleetd.render import _signin_html
    return _signin_html([{"id": "node-a", "credentials_present": present}], "TOK", logins)


def test_signin_card_offers_a_start_when_nothing_is_in_flight():
    html = _signin({})
    assert "Sign in" in html and 'name="email"' in html
    assert "not signed in" in html
    assert "paste the code" not in html


def test_signin_card_shows_the_url_and_asks_for_the_code():
    html = _signin({"node-a": {"state": "url_ready",
                               "url": "https://claude.ai/oauth/authorize?code=1"}})
    assert "https://claude.ai/oauth/authorize?code=1" in html
    assert 'name="code"' in html and "Send code" in html
    assert 'rel="noopener noreferrer"' in html


def test_a_url_from_a_node_cannot_inject_into_the_console():
    """The node supplies this string, so it is untrusted input on an admin page."""
    nasty = 'https://claude.ai/x?a="><script>alert(1)</script>'
    html = _signin({"node-a": {"state": "url_ready", "url": nasty}})
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_signin_card_waits_quietly_once_the_code_is_sent():
    html = _signin({"node-a": {"state": "code_sent"}})
    assert "Waiting" in html
    # Nothing to paste any more, but cancelling must stay possible.
    assert 'name="code"' not in html and "login-cancel" in html


def test_signin_card_says_when_a_node_is_already_signed_in():
    assert "signed in" in _signin({}, present=True)


def _vcell(installed, pinned):
    import re

    from ccfleetd.render import _row_html
    row = {"id": "n", "owner": "e", "region": "", "status": "ok", "enabled": True,
           "last_seen_ts": 1.0, "hostname": "h", "claude_version": installed,
           "pinned_version": pinned, "egress_ip": "1.2.3.4", "disk_used_pct": 10.0,
           "load1": 0.1, "credentials_present": True, "credentials_mtime": 1.0,
           "token_expires_at": None, "subscription_type": "max", "remote_control": "active",
           "rc_expected": True, "last_upgrade": None, "open_alerts": [], "usage": {}}
    return re.search(r'<td class="v">(2\.[^<]*?(?:<span[^>]*>[^<]*</span>)?)</td>',
                     _row_html(row, 100.0)).group(1)


def test_a_satisfied_pin_is_not_printed_twice():
    """It rendered "2.1.278 2.1.278", which reads as a glitch rather than a state."""
    assert _vcell("2.1.278", "2.1.278") == "2.1.278"


def test_a_pin_is_shown_when_it_still_says_something():
    drifted = _vcell("2.1.276", "2.1.278")
    assert "2.1.276" in drifted and "2.1.278" in drifted and "pinned" in drifted
    # A channel is worth showing even when satisfied: it names what is tracked.
    assert "stable" in _vcell("2.1.278", "stable")
    assert _vcell("2.1.278", "") == "2.1.278"


def test_usage_card_draws_a_sparkline_and_says_what_it_cannot_tell_you():
    from ccfleetd.render import _usage_html
    rows = [{"id": "att3", "owner": "erik", "usage": {
        "total_tokens": 1_240_000, "sessions": 12, "window_days": 14,
        "cache_read_input_tokens": 900_000, "models": ["claude-opus-5"],
        "by_day": [{"day": "2026-09-18", "tokens": 260000},
                   {"day": "2026-09-19", "tokens": 140000}]}}]
    html = _usage_html(rows, NOW)
    assert "1.2M" in html and "12 sessions" in html
    assert '<svg class="spark"' in html and "<polyline" in html
    # The card now carries the subscription windows too, so the old disclaimer is
    # gone. What still has to be said is where the numbers come from and what
    # never leaves the node.
    assert "/usage" in html and "Conversation content never leaves the node" in html
    # A node with no window reading yet says so, rather than showing empty bars.
    assert "No window reading yet" in html
    # No node reporting usage means no card at all, rather than an empty one.
    assert _usage_html([{"id": "n", "owner": "e", "usage": {}}], NOW) == ""


def test_a_node_supplied_usage_series_cannot_break_the_chart():
    from ccfleetd.render import _sparkline
    assert "no activity yet" in _sparkline([])
    assert "no activity yet" in _sparkline([{"day": "x", "tokens": "lots"}])
    # All-zero days must not divide by zero.
    flat = _sparkline([{"day": "a", "tokens": 0}, {"day": "b", "tokens": 0}])
    assert "<polyline" in flat
    # One reading is not a trend. Drawn, it normalised against itself and
    # became a full-width bar at the top of the frame, which reads as a node at
    # its ceiling — the opposite of what one quiet day means.
    single = _sparkline([{"day": "a", "tokens": 5}])
    assert "one day so far" in single and "<svg" not in single
    # A flat run does have a shape to draw, but not at the top of the frame:
    # normalised against its own peak it would pin there and read as full.
    flat_high = _sparkline([{"day": "a", "tokens": 9}, {"day": "b", "tokens": 9}])
    ys = {p.split(",")[1] for p in
          flat_high.split('class="spark-line" points="')[1].split('"')[0].split()}
    assert len(ys) == 1, "a flat series is flat"
    assert 12.0 < float(ys.pop()) < 26.0, "and sits mid-frame, not pinned to the top"


def test_quota_meters_show_both_windows_and_colour_by_pressure():
    """The two windows an owner asks about, as bars rather than bare numbers."""
    from ccfleetd.render import _quota_html
    row = {"quota": {"session": {"used_pct": 3, "resets": "7:50pm (UTC)"},
                     "week": {"used_pct": 15, "resets": "Sep 23, 3pm (UTC)"},
                     "checked_at": NOW - 600}}
    html = _quota_html(row, NOW)
    assert "5-hour session" in html and "This week" in html
    assert "3%" in html and "15%" in html
    assert "7:50pm (UTC)" in html and "Sep 23, 3pm (UTC)" in html
    assert "read 10m ago" in html
    assert html.count("meter-track") == 2
    # Colour is the same three-level scale the rest of the page uses.
    assert "meter-fill ok" in html
    assert "meter-fill warn" in _quota_html({"quota": {"week": {"used_pct": 80}}}, NOW)
    assert "meter-fill crit" in _quota_html({"quota": {"week": {"used_pct": 95}}}, NOW)
    # Nothing read yet is a state, not an empty bar.
    assert "No window reading yet" in _quota_html({"quota": {}}, NOW)


def test_a_node_supplied_quota_cannot_break_a_meter():
    from ccfleetd.render import _meter
    assert _meter(None, "x", None) == "" and _meter("80", "x", None) == ""
    assert _meter(True, "x", None) == "", "a bool is not a percentage"
    # Out of range clamps rather than drawing a bar past its track.
    assert "width:100%" in _meter(4000, "x", None) and "width:0%" in _meter(-9, "x", None)
    assert "&lt;script&gt;" in _meter(5, "x", "<script>")


def test_the_strip_counts_the_fleet_and_keeps_zeroes_quiet():
    """The summary before the detail: four tiles, not a count inside a sentence."""
    from ccfleetd.render import _strip_html
    html = _strip_html({"ok": 2, "critical": 1})
    assert '<div class="tile ok"><b>2</b>' in html
    assert '<div class="tile critical"><b>1</b>' in html
    # A window with nothing in it must not compete with the one that matters.
    assert '<div class="tile zero"><b>0</b><span>warning</span>' in html
    assert _strip_html({}).count("tile zero") == 4


def test_the_dashboard_shell_is_themed_and_bounded(cfg):
    html = render_dashboard([], [], NOW, cfg)
    # Every colour goes through a token, so dark mode is not an afterthought.
    assert "prefers-color-scheme:dark" in html and 'data-theme="light"' in html
    assert "#eef0f3" not in html, "no hard-coded greys left over from the old sheet"
    assert 'class="page"' in html and "max-width:1200px" in html
    assert 'rel="icon"' in html
    assert "Nothing open" in html, "an empty alert list should say so in words"


def test_the_chart_caption_does_not_outlive_the_chart():
    """It names the axes of a drawing; with no drawing it captioned a sentence."""
    from ccfleetd.render import _usage_html
    one_day = [{"id": "n", "owner": "e", "usage": {
        "total_tokens": 50, "sessions": 1, "window_days": 14,
        "by_day": [{"day": "2026-09-21", "tokens": 50}]}}]
    html = _usage_html(one_day, NOW)
    assert "one day so far" in html and "daily tokens, last" not in html
    # Singular, too.
    assert "1 session " in html and "1 sessions" not in html

    two_days = [{"id": "n", "owner": "e", "usage": {
        "total_tokens": 90, "sessions": 2, "window_days": 14,
        "by_day": [{"day": "2026-09-20", "tokens": 40}, {"day": "2026-09-21", "tokens": 50}]}}]
    drawn = _usage_html(two_days, NOW)
    assert "<svg" in drawn and "daily tokens, last 14d" in drawn
    assert "2 sessions" in drawn
