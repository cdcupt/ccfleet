"""Public performance copy is honest, aggregated and separately translated."""
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from ccfleetd import render, status, statuspage
from ccfleetd.bilingual import SCRIPT_TAG, project_html
from tests.conftest import refresh_of
from tests.test_admin_host import ADMIN, PRODUCT, split  # noqa: F401 (the fixture)

NOW = 2_000_000_000.0


class Text(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.parts = []
        self.feed(source)

    def handle_data(self, value):
        self.parts.append(value)

    def shown(self):
        return " ".join(" ".join(self.parts).split())


def ready_report():
    return {
        "state": "ready", "window_days": 7, "window_start_day": "2033-05-12",
        "eligible_slots": 8, "reporting_slots": 5, "reporting_users": 3,
        "oldest_observed_at": NOW - 120, "requests": 25, "completed": 24,
        "completion_rate": 0.96,
        "latencies": {
            "connect_ms": {"count": 23, "mean": 240.0},
            "first_byte_ms": {"count": 24, "mean": 1250.0},
            "total_ms": {"count": 25, "mean": 30123.0},
        },
    }


def projected(report, language="en"):
    return project_html(statuspage.performance_panel(report, NOW), language, "/status")


def test_ready_panel_explains_the_actual_measured_numbers_and_window():
    text = Text(projected(ready_report())).shown()
    assert "Fleet performance" in text
    assert "Transfer completion 96.00%" in text
    assert "Average first response byte 1.25 s" in text
    assert "Observed relay attempts 25" in text
    assert "Last 7 UTC calendar days, including today" in text
    assert "Average upstream connection: 240 ms · 23 timing samples" in text
    assert "Average full transfer: 30.12 s · 25 timing samples" in text
    assert "sample-weighted averages, not medians or percentiles" in text
    assert "not a measurement of every request" in text
    assert "Oldest included report: 2m ago" in text


def test_transfer_completion_never_rounds_a_failed_attempt_up_to_one_hundred_percent():
    report = ready_report()
    report.update(requests=100001, completed=100000, completion_rate=100000 / 100001)
    text = Text(projected(report)).shown()
    assert "Transfer completion 99.99%" in text
    assert "100%" not in text


@pytest.mark.parametrize("state", ["no_data", "limited_data"])
def test_suppressed_states_never_publish_raw_traffic_coverage_or_freshness(state):
    report = ready_report()
    report.update(state=state, requests=987654, completed=876543,
                  reporting_slots=765432, eligible_slots=654321, reporting_users=543210,
                  oldest_observed_at=NOW - 120)
    report["latencies"] = {
        key: {"count": 432109, "mean": 321098.0}
        for key in ("connect_ms", "first_byte_ms", "total_ms")
    }
    raw = projected(report)
    text = Text(raw).shown()
    assert text.count("—") == 3
    assert "A dash means unknown, not zero" in text
    expected = ("No fresh transfer data yet" if state == "no_data"
                else "Waiting for enough aggregated data")
    assert expected in text
    for secret in ("987654", "876543", "765432", "654321", "543210", "432109", "321098"):
        assert secret not in raw
    assert "Oldest included report" not in text
    assert "timing samples" not in text
    assert "datetime=" not in raw


def test_missing_first_byte_sample_is_unknown_not_a_zero_latency():
    report = ready_report()
    report["latencies"]["first_byte_ms"] = {"count": 0, "mean": None}
    text = Text(projected(report)).shown()
    assert "Average first response byte —" in text
    assert "Average first response body byte: — · No timing samples" in text
    assert "Transfer completion 96.00%" in text
    assert "Observed relay attempts 25" in text
    assert "0 ms" not in text.replace("240 ms", "")


def test_a_timing_with_too_few_contributors_stays_hidden_when_overall_traffic_is_ready():
    report = ready_report()
    report["latencies"]["first_byte_ms"] = {"count": None, "mean": None}
    text = Text(projected(report)).shown()
    assert "Average first response byte —" in text
    assert "Average first response body byte: — · More aggregate samples needed" in text
    assert "Transfer completion 96.00%" in text
    assert "Observed relay attempts 25" in text
    assert "Each timing separately requires at least 20 samples from 3 different accounts" in text
    assert "None" not in text


def test_timer_and_attempt_definitions_do_not_claim_end_to_end_task_success():
    text = Text(projected(ready_report())).shown()
    assert "both Messages and token-count calls" in text
    assert "One user request can produce more than one attempt" in text
    assert "Requests rejected before reaching a slot relay are not counted" in text
    assert "successful HTTP reply (2xx) was fully transferred" in text
    assert "Error replies, cancellations, and interrupted transfers are not successful" in text
    assert "does not tell us whether the model completed your task" in text
    assert "not a generated word" in text
    assert "excludes the trip from your computer to the gateway and SSH setup" in text
    assert "no account names, slots, messages, or individual request records" in text


def test_arbitrary_account_slot_content_and_raw_reports_are_never_rendered():
    report = ready_report()
    report.update(account_id="secret-account", slot_id="secret-slot",
                  account_email="someone-secret@example.com", model="secret-model",
                  messages="secret-message", token="secret-token")
    report["latencies"]["first_byte_ms"]["extra"] = "secret-latency"
    before = deepcopy(report)
    raw = projected(report)
    assert "secret-" not in raw
    assert "someone-secret@example.com" not in raw
    assert report == before


def test_english_and_chinese_are_actual_separate_prose_including_help_and_age():
    source = statuspage.performance_panel(ready_report(), NOW)
    english, chinese = projected(ready_report()), projected(ready_report(), "zh")
    assert "copy-zh" in source and "copy-en" in source
    assert "copy-zh" not in english and "传输完成率" not in english
    assert "copy-en" not in chinese and "Transfer completion" not in chinese
    text = Text(chinese).shown()
    assert "Fleet 性能" in text
    assert "传输完成率 96.00%" in text
    assert "平均首字节等待时间 1.25 秒" in text
    assert "已记录的转发尝试 25" in text
    assert "最近 7 个 UTC 日期，包含今天" in text
    assert "最早的一份有效上报： 2 分钟前" in text
    assert "这些数字是怎么来的" in text
    assert "不是中位数或百分位数" in text
    assert "not a measurement" not in text


def test_status_page_calls_only_the_safe_summary_and_keeps_existing_uptime_and_refresh(
        store, cfg, monkeypatch):
    calls = []

    def summary(given_store, given_cfg, now):
        calls.append((given_store, given_cfg, now))
        return ready_report()

    monkeypatch.setattr(statuspage.performance, "summary", summary)
    raw = statuspage.page(store, cfg, None, NOW)
    assert calls == [(store, cfg, NOW)]
    assert raw.count('id="performance"') == 1
    assert raw.count('class="st-bar"') == 2
    assert raw.count('class="d ') == 2 * status.HISTORY_DAYS
    assert refresh_of(raw) == (60, "")
    assert raw.count("<script") == 2
    assert render.LOCAL_TIMES_TAG in raw and SCRIPT_TAG in raw
    assert "<script src=" not in raw
    assert 'aria-labelledby="performance-title"' in raw
    assert '<h2 id="performance-title">' in raw
    assert '<details class="st-perf-help"><summary>' in raw


def test_anonymous_status_routes_serve_the_performance_panel_in_only_the_selected_language(
        split, monkeypatch):  # noqa: F811
    _, browser, _ = split
    monkeypatch.setattr(statuspage.performance, "summary", lambda *_: ready_report())
    for path in ("/status", "/en/status"):
        response = browser(PRODUCT).call("GET", path)
        assert response.status == 200
        assert 'id="performance"' in response.body
        assert "Transfer completion" in response.body
        assert "传输完成率" not in response.body
        assert 'class="copy-zh"' not in response.body
    chinese = browser(PRODUCT).call("GET", "/zh/status")
    assert chinese.status == 200
    assert 'id="performance"' in chinese.body
    assert "传输完成率" in chinese.body
    assert "Transfer completion" not in chinese.body
    assert 'class="copy-en"' not in chinese.body
    assert browser(ADMIN).call("GET", "/status").status == 404
