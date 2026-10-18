"""The current gateway guide teaches one clear flow in separate language pages."""

from pathlib import Path

import pytest

from ccfleetd import bilingual
from tests.test_html_document_preservation import Document

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "public-gateway.html"
TARGET = "/docs/library/docs/public-gateway.html"


@pytest.mark.parametrize("language", ["en", "zh"])
def test_current_guide_navigation_and_anchors_are_self_contained(language):
    source = GUIDE.read_text()
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert "data-language-choice" not in source and 'data-language="both"' not in source
    html = bilingual.project_html(source, language, TARGET)
    parsed = Document(html)
    assert not parsed.duplicate_ids
    anchors = {
        "overview",
        "start",
        "choose",
        "urls",
        "failover",
        "privacy",
        "keys",
        "updates",
        "verification",
    }
    assert anchors <= parsed.ids
    assert all(link[1:] in parsed.ids for link in parsed.links if link.startswith("#"))
    assert f'href="/en{TARGET}"' in html and f'href="/zh{TARGET}"' in html
    for removed in (
        "--transport bridge",
        "--legacy-history",
        "CCFLEET_GATEWAY_SSH_KEY_PATH",
        "historical",
        "operator setup",
    ):
        assert removed.lower() not in parsed.text.lower()


def test_shared_runnable_examples_do_not_duplicate_or_expose_operator_configuration():
    commands = "\n".join(Document(GUIDE.read_text()).pre_codes)
    assert (
        commands.count(
            "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh"
        )
        == 1
    )
    assert commands.count("ccfleet gateway setup --slots A B --routing sticky --replace") == 1
    assert commands.count("ccfleet gateway setup --slots A B --routing priority --replace") == 1
    assert commands.count("ccfleet local --slot A") == 1
    assert commands.count("https://ccfleet.io/v1/messages") == 1
    assert "YOUR_MATCHING_PUBLIC_KEY" not in commands
    assert "CCFLEET_GATEWAY_SSH" not in commands


def test_account_choices_and_saved_url_are_explained_without_adding_accounts():
    text = Document(bilingual.project_html(GUIDE.read_text(), "en", TARGET)).text
    for phrase in (
        "reception desk",
        "seat reserved",
        "Only your own held slots",
        "does not merge credentials or combine plan limits",
        "Fixed",
        "Priority",
        "Sticky",
        "does not add B automatically",
        "up to 24 hours",
        "does not start a local HTTP server",
        "not the slot's Claude credential",
        "does not automatically restore an old selection",
    ):
        assert phrase in text
    text_zh = Document(bilingual.project_html(GUIDE.read_text(), "zh", TARGET)).text
    for phrase in (
        "收件前台",
        "账号的座位",
        "只能选择你自己持有的槽位",
        "固定账号",
        "按优先级选择",
        "保持会话账号",
        "最多 24 小时",
        "不表示消息存放在这里",
    ):
        assert phrase in text_zh


def test_safe_failover_and_content_visibility_keep_current_proof_limits():
    text = Document(bilingual.project_html(GUIDE.read_text(), "en", TARGET)).text
    for phrase in (
        "uncertain result is not replayed",
        "After public response headers",
        "even if no visible text",
        "The gateway can read model messages and replies",
        "directly from the laptop",
        "bypass mode",
        "root access",
        "one actual account",
        "not a live second-account pilot",
        "provider approval",
        "native TLS fingerprint parity",
    ):
        assert phrase in text
    text_zh = Document(bilingual.project_html(GUIDE.read_text(), "zh", TARGET)).text
    # Uncertainty is explained as a lost receipt rather than a transport-code catalog.
    assert "回执丢了" in text_zh
    assert "即使还没看到文字" in text_zh
    assert "一个实际账号" in text_zh and "不等于第二个真实账号" in text_zh


def test_public_performance_discloses_single_contributor_aggregates_and_sample_limits():
    source = GUIDE.read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in (
        "completed-transfer rate, observed relay attempts",
        "average upstream connection time",
        "average time to the first upstream response body byte",
        "average full-transfer time",
        "Each timing has its own sample count",
        "observations of reported traffic, not an active benchmark",
        "at least twenty observed attempts",
        "even if only one Fleet user contributes",
        "total or average can therefore reflect that one user’s traffic",
        "This performance panel does not list names",
        "or per-user or per-slot breakdowns",
        "homepage has a separate public usage ranking",
        "twenty samples; those samples can all come from a single Fleet user",
        "Missing, old or insufficient data stays unavailable",
        "Opening Status does not call a model",
    ):
        assert phrase in english
    for phrase in (
        "传输完成率、观测到的转发尝试次数",
        "连接上游的平均时长",
        "收到上游首个正文响应字节的平均等待时间",
        "完整传输的平均时长",
        "每项计时分别注明样本数量",
        "已上报流量的观测，不是主动运行的基准测试",
        "至少二十次尝试",
        "即使只有一个 Fleet 用户提供了统计",
        "总量或平均值因此可能反映这一个用户的流量",
        "不按用户或槽位分别展示统计",
        "首页另有公开的用量排行",
        "每项计时分别需要二十个样本",
        "这些样本可以全部来自一个 Fleet 用户",
        "数据缺失、过期或不足时，暂不显示数值",
        "打开状态页不会调用模型",
    ):
        assert phrase in chinese
    assert "three contributing Fleet users" not in english
    assert "three contributing accounts" not in english
    assert "三个 Fleet 用户" not in chinese and "三个账号参与" not in chinese


def test_streamed_output_rate_is_observed_delivery_with_independent_samples_and_no_backfill():
    source = GUIDE.read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in (
        "Streamed output rate combines matched native/bridge observations",
        "without counting gateway traffic twice",
        "summed tokens and summed time",
        "at least two chunks and at least 100 ms",
        "Single-chunk or too-short replies are excluded",
        "buffering can affect the observed rate",
        "excludes the initial wait",
        "thinking or tool output where counted",
        "comparable models and workloads",
        "twenty eligible replies are required independently of the other five metrics",
        "anonymous daily sample counts, output-token totals and delivery-time totals",
        "public output-rate panel shows no input/cache totals",
        "private account-bound seven-day relay file",
        "older traffic is not backfilled",
        "earliest available retained sample day",
    ):
        assert phrase in english
    for phrase in (
        "流式输出速率合并节点上符合条件的原生／桥接观测与 HTTPS 网关观测",
        "从首个到最后一个响应正文分块的时间总和",
        "至少两个分块",
        "至少持续 100 毫秒",
        "单个分块或过短的回复不计入",
        "缓冲可能影响观测到的速率",
        "速率不包含初始等待",
        "思考或工具输出",
        "不依赖另外五项指标",
        "每日样本数、输出 Token 总量和交付时间总和",
        "公开输出速率面板不展示输入或缓存总量",
        "不补填历史流量",
        "现有留存样本的最早日期",
    ):
        assert phrase in chinese


def test_public_usage_ranking_explains_masking_coverage_and_api_equivalent_estimates():
    source = GUIDE.read_text()
    english = Document(bilingual.project_html(source, "en", TARGET)).text
    chinese = Document(bilingual.project_html(source, "zh", TARGET)).text
    for phrase in ("Public measured usage ranking", "Unknown estimates are unranked",
                   "unpriced usage is not $0", "valid existing aggregate tokens",
                   "new model-aware observations", "never an email or account ID",
                   "Masking is not anonymity", "five-minute cache-write price",
                   "not a Claude Pro/Max bill", "native, bridge and gateway model calls",
                   "no HTTPS migration or model call is needed"):
        assert phrase.lower() in english.lower()
    for phrase in ("公开的观测用量排行", "未知估值不参与排名", "无法计价的用量不按 $0",
                   "已有经验证的汇总 Token", "新的带模型信息观测", "绝不以邮箱或账号 ID",
                   "遮盖不等于匿名", "五分钟缓存写入价格", "不是 Claude Pro/Max 账单",
                   "原生、桥接与网关模型调用", "不需要迁移到 HTTPS"):
        assert phrase in chinese
