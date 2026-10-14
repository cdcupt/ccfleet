"""Public ranking is a masked numeric DTO, never a private account page."""
from copy import deepcopy

import pytest

from ccfleet_agent.model_usage import LABELS, MODELS_URL, PRICING_URL
from ccfleetd import bilingual, customer_docs, usage
from ccfleetd.config import Config
from tests.test_customer_docs import ADMIN, _serve
from tests.test_html_document_preservation import Document


def ranking():
    tokens = {"input_tokens": 1000, "output_tokens": 200,
              "cache_read_input_tokens": 30, "cache_creation_input_tokens": 4,
              "total_tokens": 1234}
    models = [{"model": key, "label": LABELS[key], "samples": 2,
               "cache_read_samples": 1, "cache_creation_samples": 2,
               "tokens": tokens, "token_state": "partial",
               "estimated_usd": 0.5, "cost_state": "partial"}
              for key in ("claude-fable-5-1", "claude-mythos-5-1")]
    entry = {"rank": 1, "masked_handle": "a***e", "tokens": tokens,
             "token_state": "partial", "estimated_usd": 1.0, "cost_state": "partial",
             "usage_samples": 4, "usage_eligible_messages": 5, "priced_samples": 4,
             "models": models}
    unknown = {**entry, "rank": None, "masked_handle": None, "estimated_usd": None,
               "cost_state": "unknown", "priced_samples": 0, "models": []}
    return {"state": "partial", "entries": [entry, unknown], "usage_samples": 8,
            "usage_eligible_messages": 10, "priced_samples": 4, "omitted_entries": 0,
            "pricing": {"date": "2026-10-14", "url": PRICING_URL,
                        "models_url": MODELS_URL}}


def shown(report, language="en"):
    source = customer_docs.overview(Config(), ranking=report)
    return bilingual.project_html(source, language, "/")


def test_known_estimate_mask_model_subtotals_and_coverage_are_visible_on_homepage():
    page = shown(ranking())
    text = Document(page).text
    assert "Measured usage ranking" in text
    assert "a***e" in text and "User" in text
    assert "1,234" in text and "$1.00" in text and "Partial estimate" in text
    assert "Claude Fable 5.1" in text and "Claude Mythos 5.1" in text
    assert "Input 1,000" in text and "Output 200" in text
    assert "Cache read 30" in text and "Cache write 4" in text
    assert "Model price known: 4 of 4" in text
    assert "Cache field coverage: read 1/2 · write 2/2" in text
    assert "unknown values are not ranked" in text
    assert "not a Claude Pro/Max bill" in text
    assert "Haiku 5.5 remains unpriced" in text
    assert "5-minute cache writes" in text
    assert "not anonymity" in text
    assert f'href="{PRICING_URL}"' in page and f'href="{MODELS_URL}"' in page
    assert page.index('id="usage-ranking"') < page.index("Read next")
    assert page.count('<script') == 2 and '<script src=' not in page
    assert '<div class="usage-ranking-sheet" role="region"' in page


def test_unknown_api_value_is_not_zero_but_measured_zero_is_a_number():
    report = ranking()
    report["entries"] = [report["entries"][1]]
    assert "$0.00" not in shown(report)
    assert "1,234" in Document(shown(report)).text
    report["entries"][0].update(cost_state="complete", estimated_usd=0)
    assert "$0.00" in shown(report)
    report["entries"][0].update(estimated_usd=0.0001)
    assert "&lt;$0.01" in shown(report)


def test_renderer_has_no_identity_or_raw_report_fallback_and_only_exact_pricing_links():
    report = ranking()
    entry = report["entries"][0]
    entry.update(masked_handle='<script>PRIVATE_HANDLE</script>', handle='PRIVATE_HANDLE',
                 email='PRIVATE_EMAIL@example.com', id='PRIVATE_ACCOUNT',
                 hostname='PRIVATE_HOST', prompt='PRIVATE_PROMPT', last_request_at=1792000123)
    entry["models"][0].update(label='PRIVATE_MODEL_LABEL', raw_model='PRIVATE_MODEL')
    report["pricing"].update(url=PRICING_URL + '?email=PRIVATE_EMAIL',
                             models_url='https://platform.claude.com/PRIVATE_ACCOUNT')
    before = deepcopy(report)
    page = shown(report)
    assert 'PRIVATE_' not in page and '1792000123' not in page
    assert 'Claude Fable 5.1' in page and 'User' in Document(page).text
    assert '?email=' not in page and f'href="{PRICING_URL}"' not in page
    assert report == before


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1, True, "1.0"])
def test_bad_estimates_stay_unknown_without_hiding_known_tokens(value):
    report = ranking()
    report["entries"] = [report["entries"][1]]
    report["entries"][0].update(cost_state="complete", estimated_usd=value)
    page = shown(report)
    assert "$0.00" not in page and "$1.00" not in page
    assert "1,234" in Document(page).text


@pytest.mark.parametrize("report", [None, {}, {"state": "no_data"},
                                    {"state": [], "entries": [{"masked_handle": "PRIVATE"}]}])
def test_no_data_or_bad_dto_is_nonfatal_and_does_not_invent_zero_usage(report):
    text = Document(shown(report)).text
    assert "No publishable usage observations yet" in text
    assert "unknown is not zero" in text and "PRIVATE" not in text
    assert "$0.00" not in text


def test_renderer_bounds_rows_and_expanded_model_rows():
    report = ranking()
    report["entries"][0]["models"] *= 20
    report["entries"] = [report["entries"][0]] * 100
    report["omitted_entries"] = 50
    page = shown(report)
    assert page.count('<th scope="row">') == 50
    assert page.count('<div><dt>Claude ') == 50 * 16
    assert "50 further entries omitted" in Document(page).text


def test_chinese_is_complete_and_the_homepage_has_separate_language_views():
    english, chinese = shown(ranking()), shown(ranking(), "zh")
    assert 'class="copy-zh"' not in english and "观测用量排行" not in english
    assert 'class="copy-en"' not in chinese and "Measured usage ranking" not in chinese
    text = Document(chinese).text
    for phrase in ("观测用量排行", "模型明细", "已观测 Token", "估算 API 等价值", "部分估值",
                   "模型价格已知", "缓存字段覆盖：读取 1／2 · 写入 2／2", "这不等于匿名",
                   "不是 Claude Pro/Max 账单", "提示长度计价档位"):
        assert phrase in text
    assert 'data-language-link="both"' not in english + chinese


def test_http_home_aliases_alone_receive_ranking_and_non_home_pages_do_not(monkeypatch):
    calls = []
    monkeypatch.setattr(usage, 'public_summary', lambda *args: calls.append(args) or ranking())
    get, stop = _serve(admin_host=ADMIN)
    try:
        for path in ('/', '/en/', '/zh/', '/docs'):
            status, page = get(path)
            assert status == 200 and 'id="usage-ranking"' in page
            assert ('观测用量排行' if path == '/zh/' else 'Measured usage ranking') in page
        assert len(calls) == 4
        for path in ('/docs/guide', '/docs/how-it-works', '/docs/terms', '/privacy', '/account', '/status'):
            status, page = get(path)
            assert status == 200 and 'id="usage-ranking"' not in page
        assert len(calls) == 4
        status, page = get('/', host=ADMIN)
        assert status == 303 and 'id="usage-ranking"' not in page
        assert len(calls) == 4
    finally:
        stop()


def test_http_no_data_homepage_is_public_and_nonfatal(monkeypatch):
    monkeypatch.setattr(usage, 'public_summary', lambda *_: {'state': 'no_data'})
    get, stop = _serve()
    try:
        for path in ('/', '/en/', '/zh/', '/docs'):
            status, page = get(path)
            assert status == 200 and 'id="usage-ranking"' in page
            assert '$0.00' not in page
    finally:
        stop()
