"""Independent checks that the headline tokens and estimate share one cohort."""

import copy
from html.parser import HTMLParser

import pytest

from ccfleet_agent import relay_metrics
from ccfleetd import usersite
from ccfleetd.bilingual import project_html
from tests.test_public_usage import fleet_of, measure, reading

MODEL = "claude-opus-5-5"
MATCHED = {"input_tokens": 1243, "output_tokens": 24874,
           "cache_read_input_tokens": 4341125, "cache_creation_input_tokens": 225684}
LEGACY = 2145853715


class Table(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.words = []
        self.main_words = []
        self.detail_words = []
        self.main_attrs = []
        self.scope = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr":
            classes = attributes.get("class", "").split()
            self.scope = ("main" if "usage-ranking-row" in classes else
                          "detail" if "usage-ranking-detail" in classes else None)
        if self.scope == "main":
            self.main_attrs.append(attributes)

    def handle_endtag(self, tag):
        if tag == "tr":
            self.scope = None

    def handle_data(self, data):
        self.words.append(data)
        if self.scope == "main":
            self.main_words.append(data)
        elif self.scope == "detail":
            self.detail_words.append(data)

    @staticmethod
    def shown(parts):
        return " ".join(" ".join(parts).split())

    def main(self):
        return self.shown(self.main_words)

    def details(self):
        return self.shown(self.detail_words)


def snapshot():
    raw = reading(samples=29, model=MODEL, native=False,
                  input_tokens=MATCHED["input_tokens"], output=MATCHED["output_tokens"],
                  read_tokens=MATCHED["cache_read_input_tokens"],
                  write_tokens=MATCHED["cache_creation_input_tokens"])
    # The broad category mix is synthetic. Only its observed total and known
    # subset are reproduced; no legacy model or historical mix is inferred.
    unknown = {"input_tokens": 1000000, "output_tokens": 8000000,
               "cache_read_input_tokens": LEGACY - 9500000,
               "cache_creation_input_tokens": 500000}
    raw.update(requests=6136, success=6136, connection_errors=0)
    raw["token_usage"].update(samples=6136, eligible=6136,
                              cache_read_samples=6136, cache_creation_samples=6136,
                              **{field: MATCHED[field] + unknown[field] for field in MATCHED})
    return measure(fleet_of([relay_metrics.validate_report(raw)]))


def show(data, language="en"):
    return Table(project_html(usersite.public_usage_panel(data), language, "/"))


def test_latest_matched_fields_sum_and_estimate_are_exact_without_repricing_legacy_data():
    data = snapshot()
    assert data["priced_tokens"] == {**MATCHED, "total_tokens": 4592926}
    assert data["estimated_usd"] == pytest.approx(2.499097, abs=1e-12)
    assert data["tokens"]["total_tokens"] == 2150446641
    assert data["unpriced_tokens"]["total_tokens"] == LEGACY
    assert data["priced_samples"] == 29 and data["usage_samples"] == 6136
    assert data["unpriced_samples"] == 6107


@pytest.mark.parametrize("language", ["en", "zh"])
def test_main_row_contains_only_matching_priceable_total_output_and_reply_count(language):
    data = snapshot()
    before = copy.deepcopy(data)
    shown = show(data, language)
    assert "4.59M" in shown.main() and "24.87K" in shown.main() and "$2.50" in shown.main()
    assert "29" in shown.main()
    for broader in ("2.15B", "8.02M", "6,136", "6,107", "8,024,874"):
        assert broader not in shown.main()
    exact = "4,592,926"
    assert any(exact in attrs.get("aria-label", "") for attrs in shown.main_attrs)
    assert "2.15B" in shown.details() and "8.02M" in shown.details()
    assert data == before


def test_headers_and_scope_copy_distinguish_the_priceable_cohort_and_broader_details():
    english = show(snapshot())
    chinese = show(snapshot(), "zh")
    assert "Priced tokens" in english.shown(english.words)
    assert "已计价 Token" in chinese.shown(chinese.words)
    assert "Broader reported usage" in english.details()
    assert "older" in english.details() or "unpriced" in english.details()
    assert "2.15B" not in english.main()


@pytest.mark.parametrize("model", [None, "other", "claude-haiku-5-5"])
def test_legacy_unknown_or_unpriced_tier_has_no_primary_token_or_money_value(model):
    data = measure(fleet_of([reading(model=model, native=False)]))
    shown = show(data)
    assert "$" not in shown.main()
    assert "330" not in shown.main()
    assert "—" in shown.main() and "330" in shown.details()
    assert data["priced_tokens"] is None and data["estimated_usd"] is None


def test_positive_samples_with_measured_zero_keep_zero_tokens_and_zero_estimate():
    data = measure(fleet_of([reading(model=MODEL, native=False, input_tokens=0, output=0,
                                    read_tokens=0, write_tokens=0)]))
    shown = show(data)
    assert "$0.00" in shown.main() and "0" in shown.main()
    assert "20" in shown.main() and "—" not in shown.main()


@pytest.mark.parametrize("field,value", [
    ("priced_tokens", None), ("priced_tokens", {}),
    ("priced_tokens", {"total_tokens": True, "output_tokens": 24874}),
    ("priced_tokens", {"total_tokens": "4592926", "output_tokens": 24874}),
    ("estimated_usd", None), ("estimated_usd", float("nan")), ("estimated_usd", -1),
])
def test_corrupt_or_missing_matching_scope_never_borrows_legacy_tokens(field, value):
    data = snapshot()
    data["entries"][0][field] = value
    main = show(data).main()
    assert "$2.50" not in main and "4.59M" not in main and "24.87K" not in main
    assert "2.15B" not in main and "8.02M" not in main
    assert "—" in main


def test_partial_model_cache_coverage_is_not_filled_from_broader_known_cache_totals():
    raw = reading(model=MODEL, native=False)
    raw["model_usage"]["models"][0].update(cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0,
                                            cache_read_samples=0, cache_creation_samples=0)
    data = measure(fleet_of([relay_metrics.validate_report(raw)]))
    assert data["priced_tokens"]["total_tokens"] == 300
    assert data["tokens"]["total_tokens"] == 330
    assert data["priced_token_state"] == data["cost_state"] == "partial"
    main = show(data).main()
    assert "300" in main and "330" not in main and "Partial" in main


@pytest.mark.parametrize("value", [None, True, "24874"])
def test_unknown_priced_output_never_falls_back_to_overall_output_or_zero(value):
    data = snapshot()
    data["entries"][0]["priced_tokens"]["output_tokens"] = value
    main = show(data).main()
    assert "Output —" in main
    assert "8.02M" not in main and "24.87K" not in main and "Output 0" not in main


@pytest.mark.parametrize("value", [None, True, "29"])
def test_unknown_priceable_reply_count_never_uses_the_broader_reply_count(value):
    data = snapshot()
    data["entries"][0]["priced_samples"] = value
    main = show(data).main()
    assert "— replies in priced scope" in main
    assert "6,136" not in main and "6,107" not in main and "0 replies" not in main
