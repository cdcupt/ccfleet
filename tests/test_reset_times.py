"""Reset times in the viewer's own time zone.

Claude Code says when a usage window resets in its machine's zone, and the
pages used to repeat that as printed: one machine's "(UTC)" beside another's
"(Asia/Shanghai)", and a machine's clock saying where somebody might be. Now
the words are read into an instant, the page carries the instant, and one
small script, allowed by its hash and nothing else, says it in the viewer's
zone. Words that cannot be read are shown as printed, as before.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ccfleetd import resets
from ccfleetd.api import HTML_HEADERS
from ccfleetd.render import LOCAL_TIMES_CSP, LOCAL_TIMES_JS, LOCAL_TIMES_TAG, _meter

SHANGHAI = ZoneInfo("Asia/Shanghai")


def at(text, read):
    got = resets.reset_at(text, read.timestamp())
    return None if got is None else datetime.fromtimestamp(got, timezone.utc)


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,read,expected", [
    # the time alone: the next time the clock reads it
    ("1:10am (Asia/Shanghai)", datetime(2026, 9, 24, 21, 33, tzinfo=SHANGHAI),
     utc(2026, 9, 24, 17, 10)),
    ("1:50pm (UTC)", utc(2026, 9, 24, 13, 33), utc(2026, 9, 24, 13, 50)),
    ("3pm (UTC)", utc(2026, 9, 24, 13, 33), utc(2026, 9, 24, 15, 0)),
    ("12:30am (UTC)", utc(2026, 9, 24, 13, 33), utc(2026, 9, 25, 0, 30)),
    ("12pm (UTC)", utc(2026, 9, 24, 9, 0), utc(2026, 9, 24, 12, 0)),
    # a date: this year's, or next year's when that is the one ahead
    ("Sep 26, 7pm (Asia/Shanghai)", datetime(2026, 9, 24, 21, 33, tzinfo=SHANGHAI),
     utc(2026, 9, 26, 11, 0)),
    ("Sep 30, 3pm (UTC)", utc(2026, 9, 24, 13, 33), utc(2026, 9, 30, 15, 0)),
    ("Jan 2, 9am (UTC)", utc(2026, 12, 30, 8, 0), utc(2027, 1, 2, 9, 0)),
])
def test_claude_codes_words_become_an_instant(text, read, expected):
    assert at(text, read) == expected


@pytest.mark.parametrize("text", [
    "", "soon", "at 11:40pm", "on Friday", "1:10am", "1:10am (Mars/Base)",
    "13pm (UTC)", "0am (UTC)", "1:75am (UTC)", "Foo 3, 1pm (UTC)", "Feb 30, 1pm (UTC)",
    None, 12,
])
def test_words_it_does_not_know_are_left_alone(text):
    assert resets.reset_at(text, utc(2026, 9, 24).timestamp()) is None


@pytest.mark.parametrize("read_at", [10**15, -10**15, float("inf"), float("nan")])
def test_a_reading_time_out_of_the_calendar_is_left_alone(read_at):
    assert resets.reset_at("1:10am (UTC)", read_at) is None
    assert resets.reset_at("Dec 31, 1am (UTC)", read_at) is None


def test_a_reset_past_the_end_of_the_calendar_is_left_alone():
    last_evening = datetime(9999, 12, 31, 23, 0, tzinfo=timezone.utc).timestamp()
    assert resets.reset_at("1:10am (UTC)", last_evening) is None, "tomorrow is year 10000"


def test_a_meter_with_a_mad_reading_time_still_draws():
    bar = _meter(4, "5-hour", "1:10am (UTC)", 10**15, 1000.0)
    assert "resets 1:10am (UTC)" in bar


@pytest.mark.parametrize("seconds,said", [
    (13_020, "in 3h 37m"), (190_800, "in 2d 5h"), (720, "in 12m"), (30, "now"), (-5, "now"),
])
def test_how_long_needs_no_time_zone(seconds, said):
    assert resets.until(1000.0, 1000.0 + seconds) == said


def test_a_read_reset_carries_its_instant_for_the_page():
    read = datetime(2026, 9, 24, 21, 33, tzinfo=SHANGHAI).timestamp()
    bar = _meter(4, "5-hour session", "1:10am (Asia/Shanghai)", read, read)
    assert '<time datetime="2026-09-24T17:10:00Z" data-local>in 3h 37m</time>' in bar
    assert "Asia/Shanghai" not in bar, "no machine's zone reaches the page"


def test_words_that_cannot_be_read_are_shown_as_printed():
    read = utc(2026, 9, 24).timestamp()
    assert "resets on Friday" in _meter(61, "This week", "on Friday", read, read)
    assert "resets 1:10am (Mars/Base)" in _meter(4, "5-hour", "1:10am (Mars/Base)", read, read)
    # With no reading time there is nothing to place a bare time against.
    assert "resets 1:10am (UTC)" in _meter(4, "5-hour", "1:10am (UTC)")


def test_what_is_printed_is_shown_not_run():
    read = utc(2026, 9, 24).timestamp()
    bar = _meter(4, "5-hour", "<b>soon</b>", read, read)
    assert "<b>" not in bar and "&lt;b&gt;soon&lt;/b&gt;" in bar


def test_the_one_script_is_allowed_by_its_hash_and_nothing_else():
    digest = base64.b64encode(hashlib.sha256(LOCAL_TIMES_JS.encode()).digest()).decode()
    assert LOCAL_TIMES_CSP == f"'sha256-{digest}'"
    policy = HTML_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    script = re.search(r"script-src ([^;]*)", policy).group(1).split()
    assert script == [LOCAL_TIMES_CSP], "no unsafe-inline, no other source"
    # The tab's icon is a data: image, and images may be nothing else.
    assert re.search(r"img-src ([^;]*)", policy).group(1).split() == ["data:"]
    assert LOCAL_TIMES_TAG == f"<script>{LOCAL_TIMES_JS}</script>"


def test_the_script_writes_text_never_markup():
    assert "textContent" in LOCAL_TIMES_JS and "innerHTML" not in LOCAL_TIMES_JS
    assert "time[data-local]" in LOCAL_TIMES_JS


def test_the_holders_page_says_resets_in_their_own_zone():
    from ccfleetd.usersite import _in_use
    read = datetime(2026, 9, 24, 21, 33, tzinfo=SHANGHAI).timestamp()
    report = {"credentials": {"logged_in": True}, "remote_control": {"state": "active"},
              "quota": {"checked_at": read,
                        "session": {"used_pct": 4, "resets": "1:10am (Asia/Shanghai)"},
                        "week": {"used_pct": 26, "resets": "Sep 26, 7pm (Asia/Shanghai)"}}}
    block = _in_use(report, read)
    assert '<time datetime="2026-09-24T17:10:00Z" data-local>' in block
    assert '<time datetime="2026-09-26T11:00:00Z" data-local>' in block
    assert "Asia/Shanghai" not in block


def test_how_it_works_says_the_clocks_are_utc():
    from ccfleetd import customer_docs
    from ccfleetd.config import Config
    page = customer_docs.how_it_works(Config(admin_token="x" * 32))
    assert "clocks on UTC" in page and "your own time zone" in page


def test_the_console_carries_the_script_that_says_them():
    from ccfleetd.config import Config
    from ccfleetd.render import render_dashboard
    page = render_dashboard([], [], 3_000_000.0, Config(admin_token="x" * 32))
    assert page.count("<script") == 1 and LOCAL_TIMES_TAG in page
