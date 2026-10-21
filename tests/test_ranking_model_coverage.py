"""Model labels follow recorded responses, including a mixed legacy collector window."""

import json
import re

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import bilingual, usersite
from tests.test_html_document_preservation import Document
from tests.test_performance import BOUND, NOW
from tests.test_public_usage import fleet_of, measure, reading


def rendered(report, language="en"):
    return bilingual.project_html(usersite.public_usage_panel(report), language, "/")


def main_row(source):
    return re.search(r'<tr class="usage-ranking-row">(.*?)</tr>', source, flags=re.S)[1]


@pytest.mark.parametrize("language,status,coverage", [
    ("en", "Model not recorded", "27 replies have no recorded model."),
    ("zh", "未记录模型", "27 个回复未记录模型。"),
])
def test_legacy_masked_user_has_visible_model_status_and_exact_missing_coverage(
        language, status, coverage):
    fleet = fleet_of([reading(samples=27, model=None)])
    fleet.list_accounts = lambda: [{"id": "holder-0", "email": "wa-private36@example.test"}]
    report = measure(fleet)
    source = rendered(report, language)
    assert "wa•••36" in main_row(source) and status in Document(main_row(source)).text
    assert coverage in Document(source).text
    assert report["entries"][0]["models"] == []
    assert report["entries"][0]["identified_model_samples"] == 0
    assert report["entries"][0]["unrecorded_model_samples"] == 27
    assert "wa-private" not in source and "example.test" not in source


@pytest.mark.parametrize("language,label,identified,unrecorded,unknown", [
    ("en", "Reported model: Claude Sonnet 4.5", "Identified models: 20 of 60",
     "20 replies have no recorded model.", "20 replies have an unrecognized or ambiguous model."),
    ("zh", "上报模型：Claude Sonnet 4.5", "已识别模型：20／60",
     "20 个回复未记录模型。", "20 个回复的模型未识别或存在歧义。"),
])
def test_partial_model_coverage_keeps_missing_and_unknown_observations_separate(
        language, label, identified, unrecorded, unknown):
    report = measure(fleet_of([reading(), reading(model="other"), reading(model=None)],
                              ["same"] * 3))
    source = rendered(report, language)
    assert label in Document(main_row(source)).text
    for phrase in (identified, unrecorded, unknown):
        assert phrase in Document(source).text
    row = report["entries"][0]
    assert row["identified_model_samples"] == row["unknown_model_samples"] == 20
    assert row["unrecorded_model_samples"] == 20 and row["model_state"] == "partial"


def test_known_but_unpriced_model_stays_visible_without_becoming_unknown_or_priced():
    report = measure(fleet_of([reading(model="claude-haiku-5-5")]))
    row = report["entries"][0]
    assert row["identified_model_samples"] == 20 and row["model_state"] == "complete"
    assert row["unknown_model_samples"] == row["unrecorded_model_samples"] == 0
    assert row["estimated_usd"] is None and row["cost_state"] == "unknown"
    assert "Reported model: Claude Haiku 5.5" in Document(main_row(rendered(report))).text


def test_future_response_model_reaches_public_ranking_without_relabeling_legacy_tokens(tmp_path):
    numbers = {"input_tokens": 20, "output_tokens": 9,
               "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0}
    for _ in range(27):
        relay_metrics.record(tmp_path, BOUND, "success", {}, NOW,
                             usage_eligible=True, usage=numbers)
    observer = relay_metrics.UsageCollector("application/json")
    observer.feed(json.dumps({"type": "message", "model": "claude-opus-5-5-20261001",
                              "stop_reason": "end_turn", "usage": numbers,
                              "content": [{"type": "text", "text": "PRIVATE_REPLY"}]}).encode())
    collected = observer.finish()
    relay_metrics.record(tmp_path, BOUND, "success", {}, NOW, usage_eligible=True,
                         usage=collected, model_eligible=True, public_model=observer.public_model)
    report = measure(fleet_of([relay_metrics.report(tmp_path, BOUND, NOW)]))
    row = report["entries"][0]
    assert row["usage_samples"] == 28 and row["identified_model_samples"] == 1
    assert row["unrecorded_model_samples"] == 27 and row["model_state"] == "partial"
    assert row["models"][0]["model"] == "claude-opus-5-5"
    assert row["models"][0]["samples"] == 1
    assert row["models"][0]["tokens"]["total_tokens"] == 37
    assert row["tokens"]["total_tokens"] == 28 * 37
    source = rendered(report)
    assert "Reported model: Claude Opus 5.5" in Document(main_row(source)).text
    assert "Identified models: 1 of 28 measured replies." in Document(source).text
    assert "27 replies have no recorded model." in Document(source).text
    assert "PRIVATE_REPLY" not in source and "20261001" not in source


@pytest.mark.parametrize("key,samples", [("PRIVATE_MODEL", 1), ("claude-opus-5-5", 0),
                                         ("claude-opus-5-5", True)])
def test_main_model_label_has_no_raw_or_unmeasured_fallback(key, samples):
    report = measure(fleet_of([reading(model=None)]))
    report["entries"][0]["models"] = [{"model": key, "label": "PRIVATE_LABEL", "samples": samples}]
    main = main_row(rendered(report))
    assert "Model not recorded" in Document(main).text
    assert "PRIVATE" not in main and "Reported model:" not in main


def test_unknown_response_model_has_a_distinct_visible_status():
    report = measure(fleet_of([reading(model="other")]))
    assert "Model not identified" in Document(main_row(rendered(report))).text
    assert "20 replies have an unrecognized or ambiguous model." in Document(rendered(report)).text


@pytest.mark.parametrize("value", [None, True, -1, "27", 28])
def test_invalid_model_coverage_is_unavailable_not_zero_or_fabricated(value):
    report = measure(fleet_of([reading(samples=27, model=None)]))
    report["entries"][0]["unrecorded_model_samples"] = value
    text = Document(rendered(report)).text
    assert "Model coverage unavailable." in text
    assert "0 of 27 measured replies" not in text
