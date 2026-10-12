"""Public machine totals are separate from cohort-gated request performance."""
from copy import deepcopy
from html.parser import HTMLParser

import pytest

from ccfleetd import render, statuspage
from ccfleetd.bilingual import SCRIPT_TAG, project_html
from tests.conftest import refresh_of

NOW = 2_000_000_000.0
GIB = 1024 ** 3


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
        "state": "ready", "observed_at": NOW - 15, "interval_s": 15,
        "host": {
            "cpu_pct": 12.3, "io_wait_pct": 0.4, "steal_pct": 0.2,
            "logical_cpus": 4, "load_1m": 0.75,
            "memory_used_bytes": 3 * GIB, "memory_total_bytes": 8 * GIB,
            "memory_pct": 37.5,
        },
        "service": {
            "cpu_budget_pct": 5.5, "cpu_quota_cores": 1,
            "memory_used_bytes": GIB // 4, "memory_limit_bytes": GIB * 3 // 4,
            "memory_pct": 100 / 3,
        },
        "storage": {
            "used_bytes": 16 * GIB, "total_bytes": 64 * GIB,
            "available_bytes": 45 * GIB, "used_pct": 25,
        },
    }


def projected(report, language="en"):
    return project_html(statuspage.machine_panel(report, NOW), language, "/status")


def test_machine_card_exposes_totals_with_clear_gateway_host_and_service_scopes():
    raw = projected(ready_report())
    text = Text(raw).shown()
    assert 'id="gateway-machine"' in raw
    assert 'aria-labelledby="gateway-machine-title"' in raw
    assert "Gateway machine resources" in text
    assert "Gateway VPS dedicated to Fleet" in text
    assert "Host figures cover the entire VPS" in text
    assert "Host CPU use 12.3%" in text
    assert "Estimated host memory used 37.5% 3.0 GiB / 8.0 GiB" in text
    assert "Fleet data filesystem 25.0% 16.0 GiB / 64.0 GiB" in text
    assert "Fleet CPU budget used 5.5% CPU limit: 1.00 cores" in text
    assert "Fleet memory budget used 33.3% 256.0 MiB / 768.0 MiB" in text
    assert "Logical CPUs: 4 · one-minute load average: 0.75" in text
    assert "Host I/O wait: 0.4% · VPS CPU steal: 0.2%" in text
    assert "Data filesystem space available: 45.0 GiB" in text
    assert "Updated: less than a minute ago" in text


def test_machine_explanations_do_not_equate_capacity_or_cpu_with_proven_bottleneck():
    text = Text(projected(ready_report())).shown()
    assert "storage capacity, not disk speed" in text
    assert "do not prove that the gateway is the bottleneck" in text
    assert "your network, the slot machine and Anthropic" in text
    assert "total minus available memory" in text
    assert "including other files on that filesystem" in text
    assert "does not measure all disks or disk throughput" in text
    assert "includes cache and kernel memory" in text
    assert "rather than a percentage of the whole VPS" in text
    assert "about every 15 seconds" in text
    assert "page refreshes every minute" in text
    assert "do not need the three-account traffic threshold" in text


@pytest.mark.parametrize("report", [{}, {"state": "unavailable"}])
def test_unavailable_machine_numbers_stay_unknown_without_zero_or_fake_freshness(report):
    raw = projected(report)
    text = Text(raw).shown()
    assert "Waiting for a machine sample" in text
    assert "A dash means unknown, not zero" in text
    assert "CPU limit: unknown" in text
    assert "0.0%" not in text
    assert "Updated:" not in text
    assert "datetime=" not in raw


def test_stale_report_hides_every_measurement_but_explains_old_observation_age():
    report = ready_report()
    report.update(state="stale", observed_at=NOW - 600)
    raw = projected(report)
    text = Text(raw).shown()
    assert "machine report is out of date" in text
    assert "old values are hidden" in text
    assert "Last report: 10m ago" in text
    assert "Updated:" not in text
    for value in ("12.3%", "37.5%", "8.0 GiB", "64.0 GiB", "1.00 cores", "0.75"):
        assert value not in raw


def test_partial_report_keeps_known_values_but_never_fills_missing_cpu_with_zero():
    report = ready_report()
    report["state"] = "partial"
    report["host"]["cpu_pct"] = None
    report["service"]["cpu_budget_pct"] = None
    text = Text(projected(report)).shown()
    assert "Host CPU use —" in text
    assert "Fleet CPU budget used —" in text
    assert "Estimated host memory used 37.5%" in text
    assert "Some measurements are not available yet" in text
    assert "A dash means unknown, not zero" in text


def test_zero_is_displayed_when_actually_measured_and_service_budget_can_briefly_exceed_limit():
    report = ready_report()
    report["host"]["cpu_pct"] = 0
    report["service"]["cpu_budget_pct"] = 103.2
    report["service"]["memory_pct"] = 105.1
    text = Text(projected(report)).shown()
    assert "Host CPU use 0.0%" in text
    assert "Fleet CPU budget used 103.2%" in text
    assert "Fleet memory budget used 105.1%" in text


def test_unlimited_service_limits_are_unknown_even_if_accounted_memory_is_known():
    report = ready_report()
    report["service"].update(cpu_budget_pct=None, cpu_quota_cores=None,
                             memory_limit_bytes=None, memory_pct=None)
    text = Text(projected(report)).shown()
    assert "Fleet CPU budget used — CPU limit: unknown" in text
    assert "Fleet memory budget used — 256.0 MiB / —" in text
    assert "None" not in text


def test_english_and_chinese_are_actual_separate_copy_including_notes_and_details():
    english = projected(ready_report())
    chinese = projected(ready_report(), "zh")
    assert "网关主机资源" not in english and "copy-zh" not in english
    assert "Gateway machine resources" not in chinese and "copy-en" not in chinese
    text = Text(chinese).shown()
    assert "网关主机资源" in text
    assert "Fleet 专用网关 VPS" in text
    assert "主机 CPU 占用 12.3%" in text
    assert "主机内存估算占用 37.5%" in text
    assert "Fleet 数据所在文件系统 25.0%" in text
    assert "Fleet CPU 配额占用 5.5% CPU 配额：1.00 核" in text
    assert "更新于： 不到一分钟前" in text
    assert "不能直接证明网关是瓶颈" in text
    assert "these measurements" not in text
    stale = ready_report()
    stale.update(state="stale", observed_at=NOW - 120)
    assert "最近一次上报： 2 分钟前" in Text(projected(stale, "zh")).shown()


def test_invalid_values_and_unallowlisted_private_context_are_never_echoed():
    report = ready_report()
    report.update(hostname="secret-host", ipv4="203.0.113.7", account="secret-account",
                  process="secret-command", token="secret-token", observed_at=float("inf"))
    report["host"].update(cpu_pct="<script>secret-value</script>", logical_cpus=True,
                          memory_used_bytes=float("nan"), load_1m=10 ** 10000,
                          name="secret-other-app")
    report["service"]["cpu_quota_cores"] = -1
    before = deepcopy(report)
    raw = projected(report)
    assert "secret-" not in raw and "203.0.113.7" not in raw
    assert "nan" not in raw and "inf" not in raw
    assert "Host CPU use" in raw
    assert "CPU limit: unknown" in raw
    assert "Updated:" not in raw
    assert report.keys() == before.keys()
    assert report["host"]["cpu_pct"] == before["host"]["cpu_pct"]


def test_enabled_machine_panel_keeps_existing_page_refresh_and_authored_scripts(store, cfg):
    raw = statuspage.page(store, cfg, None, NOW, machine_report=ready_report())
    assert raw.count('id="gateway-machine"') == 1
    assert raw.count('id="performance"') == 1
    assert raw.index('id="performance"') < raw.index('id="gateway-machine"')
    assert raw.count('class="st-bar"') == 2
    assert refresh_of(raw) == (60, "")
    assert raw.count("<script") == 2
    assert render.LOCAL_TIMES_TAG in raw and SCRIPT_TAG in raw
    assert "<script src=" not in raw


def test_machine_panel_is_absent_when_not_configured(store, cfg):
    assert 'id="gateway-machine"' not in statuspage.page(store, cfg, None, NOW)


def test_hidden_transfer_note_explains_threshold_without_disclosing_current_contributors():
    text = Text(project_html(statuspage.performance_panel({"state": "limited_data"}, NOW),
                             "en", "/status")).shown()
    before_details = text.split("How we measure this", 1)[0]
    assert "At least 20 recorded attempts from 3 different Fleet accounts" in before_details
    assert "Each timing needs its own 20 samples from 3 accounts" in before_details
    assert "A dash means unknown, not zero" in before_details
