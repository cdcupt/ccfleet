"""Public ranking is a masked numeric DTO, never a private account page."""
import re
from copy import deepcopy

import pytest

from ccfleet_agent.model_usage import LABELS, MODELS_URL, PRICING_URL
from ccfleetd import bilingual, customer_docs, usage, usersite
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
             "priced_tokens": tokens, "priced_token_state": "partial",
             "unpriced_tokens": dict.fromkeys(tokens, 0), "unpriced_token_state": "partial",
             "token_state": "partial", "estimated_usd": 1.0, "cost_state": "partial",
             "usage_samples": 4, "usage_eligible_messages": 5, "priced_samples": 4,
             "models": models}
    unknown = {**entry, "rank": None, "masked_handle": None, "estimated_usd": None,
               "cost_state": "unknown", "priced_samples": 0, "models": [],
               "priced_tokens": None, "priced_token_state": "unknown",
               "unpriced_tokens": tokens, "unpriced_token_state": "partial"}
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
    assert "1.23K" in text and "$1.00" in text and "Partial" in text
    assert 'title="1,234 tokens"' in page
    assert "for 1.23K priced tokens" in text
    assert "Claude Fable 5.1" in text and "Claude Mythos 5.1" in text
    assert "Input 1K" in text and "Output 200" in text
    assert "Cache read 30" in text and "Cache write 4" in text
    assert "Model price known: 4 of 4" in text
    assert "Cache field coverage: read 1/2 · write 2/2" in text
    assert "Unknown values are not ranked" in text
    assert "not a Claude Pro/Max bill" in text
    assert "Haiku 5.5 remains unpriced" in text
    assert "5-minute cache writes" in text
    assert "not anonymity" in text
    assert f'href="{PRICING_URL}"' in page and f'href="{MODELS_URL}"' in page
    assert page.index('id="usage-ranking"') < page.index("Read next")
    assert page.count('<script') == 2 and '<script src=' not in page
    assert '<div class="usage-ranking-sheet" role="region"' in page
    assert '<details class="usage-method">' in page
    assert "cached context counted again on each request" in text


def test_unknown_api_value_is_not_zero_but_measured_zero_is_a_number():
    report = ranking()
    report["entries"] = [report["entries"][1]]
    assert "$0.00" not in shown(report)
    assert "1.23K" in Document(shown(report)).text
    report["entries"][0].update(cost_state="partial", estimated_usd=0,
                                priced_tokens=dict.fromkeys(report["entries"][0]["tokens"], 0))
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
    assert "1.23K" in Document(page).text


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
    assert page.count('scope="row"') == 50
    assert page.count('<span class="usage-model-name">Claude ') == 50 * 16
    assert "50 further entries omitted" in Document(page).text


def test_chinese_is_complete_and_the_homepage_has_separate_language_views():
    english, chinese = shown(ranking()), shown(ranking(), "zh")
    assert 'class="copy-zh"' not in english and "观测用量排行" not in english
    assert 'class="copy-en"' not in chinese and "Measured usage ranking" not in chinese
    text = Document(chinese).text
    for phrase in ("观测用量排行", "模型明细", "已计价 Token", "API 估值", "部分覆盖",
                   "模型价格已知", "缓存字段覆盖：读取 1／2 · 写入 2／2", "这不等于匿名",
                   "不是 Claude Pro/Max 账单", "提示长度计价档位"):
        assert phrase in text
    assert 'data-language-link="both"' not in english + chinese


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (999, "999"), (1000, "1K"), (1005, "1.01K"),
    (999994, "999.99K"), (999995, "1M"), (1_200_803, "1.2M"),
    (999_994_999, "999.99M"), (999_995_000, "1B"), (2_147_054_518, "2.15B"),
])
def test_compact_token_units_round_up_at_boundaries_and_keep_exact_accessible_counts(value, expected):
    source = usersite._public_usage_token(value)
    assert Document(source).text == expected
    assert f'title="{value:,} tokens"' in source
    assert f'aria-label="{value:,} tokens"' in source
    chinese = bilingual.project_html(source, "zh", "/")
    assert f'title="{value:,} Token"' in chinese
    assert f'aria-label="{value:,} Token"' in chinese


@pytest.mark.parametrize("value", [None, True, "1000", -1, float("nan"), float("inf")])
def test_invalid_token_counts_stay_unknown_and_never_enter_tooltips(value):
    source = usersite._public_usage_token(value)
    assert Document(bilingual.project_html(source, "en", "/")).text == "—"
    assert 'title=' not in source and 'aria-label=' not in source


def test_legacy_total_is_never_the_denominator_of_a_small_new_api_estimate():
    observed = {"input_tokens": 3_912_469, "output_tokens": 8_721_456,
                "cache_read_input_tokens": 2_062_021_318,
                "cache_creation_input_tokens": 72_399_275, "total_tokens": 2_147_054_518}
    priced = {"input_tokens": 1207, "output_tokens": 5025,
              "cache_read_input_tokens": 1_054_178, "cache_creation_input_tokens": 140_393,
              "total_tokens": 1_200_803}
    entry = {"rank": 1, "masked_handle": "e***k", "tokens": observed,
             "token_state": "complete", "priced_tokens": priced, "priced_token_state": "complete",
             "unpriced_tokens": {key: observed[key] - priced[key] for key in observed},
             "unpriced_token_state": "complete", "estimated_usd": 1.0181286,
             "cost_state": "partial", "usage_samples": 6118, "usage_eligible_messages": 6118,
             "priced_samples": 11, "models": []}
    source = shown({**ranking(), "entries": [entry]})
    cell = re.search(r'<td class="usage-estimate">(.*?)</td>', source, flags=re.S)[1]
    assert "$1.02" in cell and "Partial" in Document(cell).text
    assert "for 1.2M priced tokens" in Document(cell).text
    assert 'title="1,200,803 tokens"' in cell
    assert "2.15B" not in cell and "2,147,054,518" not in cell
    visible = Document(source).text
    assert "2.15B" in visible and "Unpriced 2.15B" in visible and "Output 8.72M" in visible
    main = re.search(r'<tr class="usage-ranking-row">(.*?)</tr>', source, flags=re.S)[1]
    assert "1.2M" in main and "Output" not in Document(main).text
    assert "replies in priced scope" not in Document(main).text
    assert "2.15B" not in main and "8.72M" not in main and "Unpriced" not in main
    assert "Model price known: 11 of 6,118" in visible
    assert 'title="2,145,853,715 tokens"' in source
    assert "not generated or unique text" in visible


def test_exact_user_model_counters_and_cost_share_the_primary_29_reply_cohort():
    priced = {"input_tokens": 1243, "output_tokens": 24874,
              "cache_read_input_tokens": 4341125, "cache_creation_input_tokens": 225684}
    priced["total_tokens"] = sum(priced.values())
    assert priced["total_tokens"] == 4_592_926
    unpriced = {"input_tokens": 3_911_262, "output_tokens": 8_716_431,
                "cache_read_input_tokens": 2_060_967_140,
                "cache_creation_input_tokens": 72_258_882, "total_tokens": 2_145_853_715}
    observed = {key: priced[key] + unpriced[key] for key in priced}
    assert observed["total_tokens"] == 2_150_446_641
    model = {"model": "claude-opus-5-5", "tokens": priced, "samples": 29,
             "cache_read_samples": 29, "cache_creation_samples": 29,
             "estimated_usd": 2.499097, "cost_state": "complete"}
    entry = {"rank": 1, "masked_handle": "e***k", "tokens": observed,
             "token_state": "complete", "priced_tokens": priced, "priced_token_state": "complete",
             "unpriced_tokens": unpriced, "unpriced_token_state": "complete",
             "estimated_usd": 2.499097, "cost_state": "partial", "models": [model],
             "priced_samples": 29, "usage_samples": 6136, "usage_eligible_messages": 6136}
    for language in ("en", "zh"):
        source = shown({**ranking(), "entries": [entry]}, language)
        main = re.search(r'<tr class="usage-ranking-row">(.*?)</tr>', source, flags=re.S)[1]
        details = re.search(r'<tr class="usage-ranking-detail">(.*?)</tr>', source, flags=re.S)[1]
        text = Document(main).text
        assert "4.59M" in text and "$2.50" in text
        assert "24.87K" not in text
        assert "Output" not in text and "replies in priced scope" not in text
        assert "输出" not in text and "个回复" not in text
        assert "24.87K" in Document(details).text
        replies = "29 replies" if language == "en" else "29 个回复"
        assert replies in Document(details).text
        assert "2.15B" not in main and "2,150,446,641" not in main and "2,145,853,715" not in main
        assert "2.15B" in Document(details).text
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens"):
            assert f'title="{priced[field]:,}' in details
        assert 'title="4,592,926' in main
        assert 'title="2,150,446,641' in details and 'title="2,145,853,715' in details


def test_unpriced_user_shows_recorded_usage_and_status_instead_of_an_empty_row():
    report = ranking()
    report["entries"] = [report["entries"][1]]
    main = re.search(r'<tr class="usage-ranking-row">(.*?)</tr>', shown(report), flags=re.S)[1]
    assert "1.23K" in main and "$0.00" not in main
    assert "Output" not in Document(main).text
    assert "Not ranked yet" in Document(main).text
    assert "Recorded · unpriced" in Document(main).text
    assert "Pricing unavailable" in Document(main).text
    assert "Member ••01" in Document(main).text
    assert "—" not in Document(main).text


def test_matching_priced_scope_is_required_to_publish_any_main_estimate():
    report = ranking()
    report["entries"] = [report["entries"][0]]
    for scope in (None, {}, {"total_tokens": True}, {"total_tokens": -1}, {"total_tokens": "1234"}):
        report["entries"][0]["priced_tokens"] = scope
        cell = re.search(r'<td class="usage-estimate">(.*?)</td>', shown(report), flags=re.S)[1]
        assert "$1.00" not in cell and "Pricing unavailable" in Document(cell).text
        assert "priced tokens" not in Document(cell).text


def test_live_shaped_legacy_user_is_visible_with_masked_member_code_in_both_languages():
    report = ranking()
    entry = report["entries"][1]
    entry.update(member_code="28456317", usage_samples=27, usage_eligible_messages=27)
    entry["tokens"] = {**entry["tokens"], "total_tokens": 10_948_800}
    entry["unpriced_tokens"] = entry["tokens"]
    for language, status in (("en", "Not ranked yet"), ("zh", "暂未排名")):
        main = re.findall(r'<tr class="usage-ranking-row">(.*?)</tr>',
                          shown(report, language), flags=re.S)[1]
        text = Document(main).text
        assert "••28456317" in text and "10.95M" in text and status in text
        suffix = "tokens" if language == "en" else "Token"
        assert f'title="10,948,800 {suffix}"' in main
        assert "—" not in text and "$0.00" not in text


@pytest.mark.parametrize("code", [None, True, 12345678, "<script>PRIVATE</script>",
                                "PRIVATE@example.com", "12345678\n", "１２３４５６７８"])
def test_untrusted_member_codes_never_become_public_identity(code):
    report = ranking()
    report["entries"][1].update(member_code=code)
    main = re.findall(r'<tr class="usage-ranking-row">(.*?)</tr>', shown(report), flags=re.S)[1]
    assert "Member ••02" in Document(main).text and "PRIVATE" not in main


def test_missing_token_measurement_stays_unavailable_and_does_not_invent_zero():
    report = ranking()
    entry = report["entries"][1]
    entry["tokens"] = None
    main = re.findall(r'<tr class="usage-ranking-row">(.*?)</tr>', shown(report), flags=re.S)[1]
    assert "Usage unavailable" in Document(main).text and "—" not in Document(main).text
    assert "No measurement yet" in Document(main).text and "Recorded · unpriced" not in main
    assert "Recorded usage is shown" not in main
    assert 'title="0 tokens"' not in main and "$0.00" not in main


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
