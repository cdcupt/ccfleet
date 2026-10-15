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
        "eligible_slots": 8, "reporting_slots": 5, "reporting_users": 1,
        "oldest_observed_at": NOW - 120, "requests": 25, "completed": 24,
        "completion_rate": 0.96,
        "latencies": {
            "connect_ms": {"count": 23, "mean": 240.0},
            "first_byte_ms": {"count": 24, "mean": 1250.0},
            "total_ms": {"count": 25, "mean": 30123.0},
        },
        "throughput": {"state": "ready", "samples": 20, "tokens_per_second": 40.0,
                       "window_start_day": "2033-05-12",
                       "collection_start_day": "2033-05-18"},
    }


def projected(report, language="en"):
    return project_html(statuspage.performance_panel(report, NOW), language, "/status")


def fleet_cards(report, language="en"):
    return projected(report, language).split('<section class="st-perf-reference"', 1)[0]


def external_reference(report, language="en"):
    return projected(report, language).split('<section class="st-perf-reference"', 1)[1].split(
        '</section>', 1)[0]


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



def test_all_six_metrics_and_their_sample_counts_are_visible_before_help():
    raw = projected(ready_report())
    visible = Text(raw).shown().split("How we measure this", 1)[0]
    assert raw.count('class="st-perf-metric"') == 6
    for metric in ("Transfer completion 96.00%", "Observed relay attempts 25",
                   "Average first response byte 1.25 s", "Average upstream connection 240 ms",
                   "Average full transfer 30.12 s", "Streamed output rate 40.00 tokens/s"):
        assert metric in visible
    for count in (23, 24, 25):
        assert f"{count} timing samples" in visible
    assert "24 of 25 transfers completed" in visible
    assert "20 streamed reply samples" in visible

@pytest.mark.parametrize("stage,title", [
    ("connect_ms", "Average upstream connection"),
    ("first_byte_ms", "Average first response byte"),
    ("total_ms", "Average full transfer"),
])
@pytest.mark.parametrize("sample", [{"count": 0, "mean": None}, {"count": None, "mean": None}])
def test_each_public_timing_card_independently_preserves_unknown(stage, title, sample):
    report = ready_report()
    report["latencies"][stage] = sample
    before_help = Text(projected(report)).shown().split("How we measure this", 1)[0]
    assert title + " —" in before_help
    assert "Transfer completion 96.00%" in before_help
    assert "Observed relay attempts 25" in before_help
    expected = "No timing samples" if sample["count"] == 0 else "More aggregate samples needed"
    assert expected in before_help


def test_measured_zero_completion_is_a_number_not_unknown():
    report = ready_report()
    report.update(requests=25, completed=0, completion_rate=0)
    text = Text(projected(report)).shown().split("How we measure this", 1)[0]
    assert "Transfer completion 0.00%" in text
    assert "0 of 25 transfers completed" in text


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
    report["throughput"] = {"state": state, "samples": 999888,
                            "tokens_per_second": 888777.0,
                            "collection_start_day": "2033-05-18"}
    raw = projected(report)
    text = Text(raw).shown()
    assert text.count("—") == 6
    assert "A dash means unknown, not zero" in text
    expected = ("No fresh slot-relay transfer data yet" if state == "no_data"
                else "Waiting for enough aggregated relay data")
    assert expected in text
    for secret in ("987654", "876543", "765432", "654321", "543210", "432109", "321098",
                   "999888", "888777"):
        assert secret not in raw
    assert "Oldest included report" not in text
    assert "timing samples" not in text
    assert "datetime=" not in raw
    assert "Earliest available output-rate samples" not in text


@pytest.mark.parametrize("relay_state", ["no_data", "limited_data"])
def test_ready_output_rate_is_visible_independently_of_suppressed_relay_figures(relay_state):
    report = ready_report()
    report["state"] = relay_state
    text = Text(projected(report)).shown()
    before_help = text.split("How we measure this", 1)[0]
    assert "Transfer completion —" in before_help
    assert "Observed relay attempts —" in before_help
    assert "Streamed output rate 40.00 tokens/s" in before_help
    assert "20 streamed reply samples" in before_help
    assert "Earliest available output-rate samples: 2033-05-18 (UTC)." in before_help
    assert "slot-relay" in before_help or "relay data" in before_help


@pytest.mark.parametrize("throughput", [
    None, {}, {"state": "no_data"},
    {"state": "limited_data", "samples": 19, "tokens_per_second": 99},
    {"state": "ready", "samples": 19, "tokens_per_second": 99},
    {"state": "ready", "samples": True, "tokens_per_second": 99},
    {"state": "ready", "samples": 20, "tokens_per_second": True},
    {"state": "ready", "samples": 20, "tokens_per_second": "99"},
    {"state": "ready", "samples": 20, "tokens_per_second": float("nan")},
    {"state": "ready", "samples": 20, "tokens_per_second": float("inf")},
    {"state": "ready", "samples": 20, "tokens_per_second": -1},
])
def test_unknown_or_invalid_output_rate_is_never_zero_or_a_raw_partial_count(throughput):
    report = ready_report()
    report["throughput"] = throughput
    text = Text(fleet_cards(report)).shown()
    assert "Streamed output rate —" in text
    assert "Transfer completion 96.00%" in text
    assert "tokens/s" not in text
    assert "streamed reply samples" not in text
    assert "Earliest available output-rate samples" not in text
    assert ("Waiting for 20 eligible streamed replies" if throughput and throughput.get(
        "state") == "limited_data" else "Needs 20 eligible streamed replies") in text


def test_each_metric_has_a_visible_direction_or_neutral_interpretation():
    english = Text(fleet_cards(ready_report())).shown()
    chinese = Text(fleet_cards(ready_report(), "zh")).shown()
    for hint in (
            "Higher is better for transport completion, not task success",
            "Lower means an earlier reply body; not time to first token (TTFT)",
            "Traffic volume, not a performance score",
            "Lower is better for connection setup; no equivalent external reference",
            "Answer-length dependent; no automatic better/worse verdict",
            "higher is faster for comparable models and workloads"):
        assert hint in english
    for hint in (
            "传输完成率越高越好；不代表任务成功率",
            "越低表示越早收到回复正文；不等同于首个 Token 时间（TTFT）",
            "这是流量规模，不是性能评分",
            "建连时间越低越好；没有同口径的外部参考",
            "受回复长度影响；不自动判定更好或更差",
            "模型和工作负载相近时，数值越高表示交付越快"):
        assert hint in chinese


def test_external_reference_is_dated_cited_accessible_and_separate_from_fleet_cards():
    raw = projected(ready_report())
    reference = external_reference(ready_report())
    text = Text(reference).shown()
    assert raw.index('class="st-perf-grid"') < raw.index('class="st-perf-reference"')
    assert raw.index('class="st-perf-reference"') < raw.index('class="st-perf-help"')
    for phrase in (
            "External provider reference", "Context only · Not directly comparable",
            "Snapshot checked: 2026-10-15", "Claude Opus 5.5 (Max, Default Fallback)",
            "10k input tokens", "72-hour median (P50)", "Output speed (AA tokens/s)",
            "Fixed snapshot; not automatically refreshed",
            "Google 98.3", "Microsoft Azure 96.7", "Anthropic 95.7", "Amazon Bedrock 93.5",
            "AA uses o200k_base tokens and medians", "Fleet mixes models",
            "provider-native output counts, including counted thinking",
            "Different definitions and workloads; no better/worse verdict",
            "No comparable external baseline for completion, connection, first byte or full transfer",
            "Fleet's first byte may be metadata, not TTFT"):
        assert phrase in text
    assert '<h3 id="performance-reference-title">' in reference
    assert '<table aria-labelledby="performance-reference-title">' in reference
    assert reference.count('<th scope="row">') == 4
    assert reference.count('<th scope="col">') == 2
    for url in (
            "https://artificialanalysis.ai/models/claude-opus-5-5/providers",
            "https://artificialanalysis.ai/methodology/performance-benchmarking"):
        assert 'href="' + url + '" rel="noopener noreferrer"' in reference
    for misleading in ("faster than", "slower than", "better than", "worse than", "679.24",
                       'class="ok"', 'class="warn"', 'class="critical"', '<script', 'fetch('):
        assert misleading not in reference


@pytest.mark.parametrize("state", ["no_data", "limited_data"])
def test_external_reference_remains_visible_without_filling_unknown_fleet_readings(state):
    report = ready_report()
    report.update(state=state, model="private-model", requests=987654)
    report["throughput"] = {"state": state, "samples": 19, "tokens_per_second": 999888}
    before = deepcopy(report)
    cards = Text(fleet_cards(report)).shown()
    assert cards.count("—") == 6
    assert "tokens/s" not in cards
    assert external_reference(report) == external_reference(ready_report())
    for private in ("private-model", "987654", "999888"):
        assert private not in projected(report)
    assert report == before


def test_external_reference_has_separate_complete_chinese_copy():
    english = Text(external_reference(ready_report())).shown()
    chinese = Text(external_reference(ready_report(), "zh")).shown()
    for phrase in (
            "外部服务商参考", "仅供参考 · 不可直接比较", "快照核对日期：2026-10-15",
            "Claude Opus 5.5（Max，默认回退）", "10k 输入 Token", "72 小时中位数（P50）",
            "固定快照，不自动刷新",
            "输出速率（AA Token/秒）", "o200k_base 标准化 Token 和中位数",
            "不判定更好或更差", "均没有可直接比较的外部基线",
            "不等同于首个 Token 时间（TTFT）", "服务商基准测试", "测试方法"):
        assert phrase in chinese
    assert "External provider reference" not in chinese
    assert "Not directly comparable" not in chinese
    assert "外部服务商参考" not in english


def test_zero_output_rate_is_a_measurement_and_untrusted_fields_are_never_rendered():
    report = ready_report()
    report["throughput"].update(tokens_per_second=0.0, model="secret-model",
                                email="secret-email", output_tokens=999888, stream_ms=888777,
                                collection_start_day="secret-date", timestamp=NOW - 1)
    before = deepcopy(report)
    raw = projected(report)
    assert "Streamed output rate 0.00 tokens/s" in Text(raw).shown()
    for private in ("secret-", "999888", "888777", str(NOW - 1)):
        assert private not in raw
    assert "Earliest available output-rate samples" not in raw
    assert report == before


def test_empty_output_rate_hint_states_the_publication_requirement_in_both_languages():
    report = ready_report()
    report["throughput"] = {"state": "no_data"}
    english = Text(projected(report)).shown().split("How we measure this", 1)[0]
    chinese = Text(projected(report, "zh")).shown().split("这些数字是怎么来的", 1)[0]
    assert "Streamed output rate — Needs 20 eligible streamed replies" in english
    assert "流式输出速率 — 需有 20 个符合条件的流式回复" in chinese
    assert "No eligible stream samples yet" not in english
    assert "暂无符合条件的流式样本" not in chinese


def test_missing_first_byte_sample_is_unknown_not_a_zero_latency():
    report = ready_report()
    report["latencies"]["first_byte_ms"] = {"count": 0, "mean": None}
    text = Text(projected(report)).shown()
    assert "Average first response byte —" in text
    assert "Average first response body byte: — · No timing samples" in text
    assert "Transfer completion 96.00%" in text
    assert "Observed relay attempts 25" in text
    assert "Average first response byte 0 ms" not in text
    assert "Average first response body byte: 0 ms" not in text


def test_a_timing_with_too_few_samples_stays_hidden_when_overall_traffic_is_ready():
    report = ready_report()
    report["latencies"]["first_byte_ms"] = {"count": None, "mean": None}
    text = Text(projected(report)).shown()
    assert "Average first response byte —" in text
    assert "Average first response body byte: — · More aggregate samples needed" in text
    assert "Transfer completion 96.00%" in text
    assert "Observed relay attempts 25" in text
    assert "Each timing separately requires at least 20 samples" in text
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
    assert "even if only one account contributes" in text
    assert "sum of provider-reported output tokens" in text
    assert "first to the last successfully forwarded response-body chunk" in text
    assert "native/bridge slot-relay measurements" in text
    assert "without counting a gateway reply twice" in text
    assert "At the observing relay" in text
    assert "At the gateway" not in text
    assert "at least two chunks and 100 ms" in text
    assert "Single-chunk or too-short replies are excluded" in text
    assert "buffering can affect the observed rate" in text
    assert "excludes the initial wait" in text
    assert "includes stream pauses and network delivery" in text
    assert "including thinking or tool output where counted" in text
    assert "not a pure model-speed benchmark" in text
    assert "at least 20 eligible streamed replies" in text
    assert "older traffic is not backfilled" in text
    assert "no user, slot or model breakdown or individual request timestamps" in text
    assert "homepage separately shows masked user usage and model details" in text


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
    assert "流式输出速率 40.00 Token/秒" in text
    assert "20 个流式回复样本" in text
    assert "最近 7 个 UTC 日期，包含今天" in text
    assert "最早的一份有效上报： 2 分钟前" in text
    assert "这些数字是怎么来的" in text
    assert "不是中位数或百分位数" in text
    assert "不包含最初等待回复的时间" in text
    assert "至少 20 个符合条件的流式回复" in text
    assert "不补填历史流量" in text
    assert "单个分块或过短的回复不计入" in text
    assert "缓冲可能影响观测到的速率" in text
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
