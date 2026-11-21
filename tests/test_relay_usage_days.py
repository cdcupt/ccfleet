"""Daily numeric attribution from existing buckets; no model or credential access."""
import copy
import json

import pytest

from ccfleet_agent import model_usage
from ccfleet_agent import relay_metrics as metrics

NOW = 20000 * metrics.DAY_SECONDS + 3600
BOUND = "0123456789abcdef"
OTHER = "fedcba9876543210"
MODEL = "claude-opus-5-5"
SECOND_MODEL = "claude-sonnet-5-5"
SECRET = "PRIVATE_PROMPT_ACCOUNT_TOKEN_IP_PATH"


@pytest.fixture
def home(tmp_path):
    path = tmp_path.resolve() / "slot"
    path.mkdir(mode=0o700)
    return path


def record(home, when=NOW, *, usage=None, model=MODEL, eligible=True):
    sample = {"input_tokens": 10, "output_tokens": 3,
              "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0}
    metrics.record(home, BOUND, "success", {}, when, usage_eligible=eligible,
                   usage=(sample if usage is None else usage) if eligible else None,
                   model_eligible=eligible and model is not None, public_model=model)


def path(home):
    return home / ".config/ccfleet" / metrics.FILE_NAME


def current_day():
    return str(NOW // metrics.DAY_SECONDS)


@pytest.fixture
def sample_report(home):
    record(home, NOW - metrics.DAY_SECONDS, model=None,
           usage={"input_tokens": 4, "output_tokens": 2})
    record(home)
    return metrics.report(home, BOUND, NOW)


def test_days_come_from_existing_validated_buckets_without_rewriting_file(home, sample_report):
    before, modified = path(home).read_bytes(), path(home).stat().st_mtime_ns
    saved = json.loads(before)
    days = sample_report["usage_days"]
    assert len(days) == 2
    assert list(days) == sorted(days, key=int)
    for day, observed in days.items():
        bucket = saved["buckets"][day]
        assert observed["success"] == bucket["counts"]["success"]
        assert observed["token_usage"] == bucket["token_usage"]
        assert observed.get("model_usage") == bucket.get("model_usage")
        assert set(observed) <= {"success", "token_usage", "model_usage"}
    assert all(sum(day["token_usage"][field] for day in days.values())
               == sample_report["token_usage"][field] for field in metrics.USAGE_KEYS)
    assert metrics.report(home, BOUND, NOW) == sample_report
    assert metrics.report(home, BOUND, NOW) == sample_report
    assert path(home).read_bytes() == before and path(home).stat().st_mtime_ns == modified
    assert "usage_days" not in saved
    assert all("usage_days" not in bucket for bucket in saved["buckets"].values())


def test_legacy_days_are_omitted_instead_of_inventing_zero_usage(home):
    record(home, NOW - 2 * metrics.DAY_SECONDS, eligible=False, model=None)
    record(home, NOW - metrics.DAY_SECONDS, model=None)
    record(home)
    result = metrics.report(home, BOUND, NOW)
    assert result["success"] == 3
    assert sum(day["success"] for day in result["usage_days"].values()) == 2
    assert str(NOW // metrics.DAY_SECONDS - 2) not in result["usage_days"]
    assert result["token_usage"]["eligible"] == result["token_usage"]["samples"] == 2
    assert result["model_usage"]["eligible"] == result["model_usage"]["samples"] == 1


def test_unsampled_eligible_response_preserves_unknown_usage_coverage(home):
    metrics.record(home, BOUND, "success", {}, NOW, usage_eligible=True, model_eligible=True)
    result = metrics.report(home, BOUND, NOW)
    daily = result["usage_days"][current_day()]
    assert daily["token_usage"]["eligible"] == daily["model_usage"]["eligible"] == 1
    assert daily["token_usage"]["samples"] == daily["model_usage"]["samples"] == 0
    assert daily["model_usage"]["models"] == []
    assert metrics.validate_report(result) == result


def test_legacy_reports_stay_accepted_and_do_not_gain_daily_history(home, sample_report):
    old = copy.deepcopy(sample_report)
    old.pop("usage_days")
    assert metrics.validate_report(old) == old
    clean_home = home / "legacy"
    clean_home.mkdir(mode=0o700)
    record(clean_home, eligible=False, model=None)
    result = metrics.report(clean_home, BOUND, NOW)
    assert "token_usage" not in result and "usage_days" not in result
    assert metrics.validate_report(result) == result
    assert "usage_days" not in metrics.report(home, OTHER, NOW)


def test_daily_validation_returns_independent_copies(sample_report):
    original = copy.deepcopy(sample_report)
    clean = metrics.validate_report(sample_report)
    clean["usage_days"][current_day()]["token_usage"]["input_tokens"] += 1
    clean["usage_days"][current_day()]["model_usage"]["models"][0]["samples"] += 1
    assert sample_report == original
    assert clean["token_usage"] == original["token_usage"]
    assert clean["model_usage"] == original["model_usage"]


@pytest.mark.parametrize("day", [None, True, 20000, "020000", "20001", "19993", "-1", "1.0",
                                 "", "99999999", SECRET])
def test_invalid_alias_old_future_or_private_day_keys_are_rejected(sample_report, day):
    value = sample_report["usage_days"].pop(current_day())
    sample_report["usage_days"][day] = value
    with pytest.raises(metrics.MetricsError, match="daily usage date"):
        metrics.validate_report(sample_report)


def test_duplicate_day_and_leading_zero_alias_are_rejected(sample_report):
    class DuplicateDay(dict):
        def items(self):
            return [*super().items(), (current_day(), self[current_day()])]

    sample_report["usage_days"] = DuplicateDay(sample_report["usage_days"])
    with pytest.raises(metrics.MetricsError, match="daily usage date"):
        metrics.validate_report(sample_report)
    sample_report["usage_days"] = dict(sample_report["usage_days"])
    sample_report["usage_days"]["0" + current_day()] = sample_report["usage_days"][current_day()]
    with pytest.raises(metrics.MetricsError, match="daily usage date"):
        metrics.validate_report(sample_report)


def test_duplicate_persisted_dates_still_fail_closed(home):
    record(home)
    saved = json.loads(path(home).read_text())
    day = current_day()
    bucket = json.dumps(saved["buckets"][day])
    raw = ('{"version":1,"bound_fp":"' + BOUND + '","buckets":{'
           + json.dumps(day) + ':' + bucket + ',' + json.dumps(day) + ':' + bucket + '}}')
    path(home).write_text(raw)
    with pytest.raises(metrics.MetricsError):
        metrics.report(home, BOUND, NOW)


@pytest.mark.parametrize("value", [None, [], "private", True])
def test_daily_mapping_must_be_a_bounded_object(sample_report, value):
    sample_report["usage_days"] = value
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


def test_daily_mapping_cannot_exceed_seven_days(sample_report):
    value = sample_report["usage_days"][current_day()]
    sample_report["usage_days"] = {str(20000 - index): value for index in range(8)}
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


@pytest.mark.parametrize("field", sorted(metrics.USAGE_KEYS))
def test_every_daily_token_or_coverage_counter_must_match_top_totals(sample_report, field):
    sample_report["usage_days"][current_day()]["token_usage"][field] += 1
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


def test_missing_day_is_not_accepted_as_a_partial_snapshot(sample_report):
    sample_report["usage_days"].pop(current_day())
    with pytest.raises(metrics.MetricsError, match="aggregate coverage"):
        metrics.validate_report(sample_report)


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", metrics.MAX_COUNTER + 1])
def test_daily_success_is_strictly_numeric_and_bounded(sample_report, value):
    sample_report["usage_days"][current_day()]["success"] = value
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


def test_daily_success_cannot_exceed_overall_success(sample_report):
    sample_report["usage_days"][current_day()]["success"] = sample_report["success"] + 1
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


@pytest.mark.parametrize("field", ["account_id", "bound_fp", "request_id", "updated_at",
                                   "stream_output", "raw_response"])
def test_private_or_unreviewed_daily_fields_are_refused(sample_report, field):
    sample_report["usage_days"][current_day()][field] = SECRET
    with pytest.raises(metrics.MetricsError) as error:
        metrics.validate_report(sample_report)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("change", ["model", "private_model", "missing", "extra", "eligible"])
def test_daily_model_values_and_coverage_must_match_top_block(sample_report, change):
    day = sample_report["usage_days"][current_day()]
    if change == "model":
        day["model_usage"]["models"][0]["model"] = SECOND_MODEL
    elif change == "private_model":
        day["model_usage"]["models"][0]["model"] = SECRET
    elif change == "missing":
        day.pop("model_usage")
    elif change == "extra":
        sample_report.pop("model_usage")
    else:
        day["model_usage"]["eligible"] = 0
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(sample_report)


def test_daily_usage_requires_a_top_token_block(home):
    raw = metrics.report(home, BOUND, NOW)
    raw["usage_days"] = {}
    with pytest.raises(metrics.MetricsError):
        metrics.validate_report(raw)


def test_empty_daily_snapshot_can_match_explicit_zero_aggregate_blocks(home):
    raw = metrics.report(home, BOUND, NOW)
    raw.update(token_usage=dict.fromkeys(metrics.USAGE_KEYS, 0),
               model_usage={"eligible": 0, "samples": 0, "models": []}, usage_days={})
    assert metrics.validate_report(raw) == raw


def test_report_extension_is_not_accepted_in_persisted_bucket_schema(home):
    record(home)
    saved = json.loads(path(home).read_text())
    saved["buckets"][current_day()]["usage_days"] = {}
    path(home).write_text(json.dumps(saved))
    with pytest.raises(metrics.MetricsError, match="bucket"):
        metrics.report(home, BOUND, NOW)
    assert "usage_days" not in metrics.OPTIONAL_BLOCKS


def test_daily_and_top_models_use_identical_bounded_folding(home, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_MODELS", 2)
    for offset, name in enumerate((MODEL, SECOND_MODEL, "claude-fable-5-1")):
        record(home, NOW - (2 - offset) * metrics.DAY_SECONDS, model=name)
    result = metrics.report(home, BOUND, NOW)
    assert len(result["model_usage"]["models"]) == 2
    assert any(row["model"] == "other" for row in result["model_usage"]["models"])
    assert len(result["usage_days"]) == 3
    assert metrics.validate_report(result) == result


def test_only_current_visible_days_are_exported_and_reports_stay_within_bound(home):
    for day in range(8):
        for index, name in enumerate(model_usage.PUBLIC_MODELS):
            record(home, NOW - (7 - day) * metrics.DAY_SECONDS + index, model=name)
    result = metrics.report(home, BOUND, NOW + 30)
    assert len(result["usage_days"]) == 7
    assert min(map(int, result["usage_days"])) == 20000 - 6
    assert len(json.dumps(result).encode()) < metrics.MAX_BYTES
    before = path(home).read_bytes()
    assert metrics.report(home, BOUND, NOW + 8 * metrics.DAY_SECONDS)["requests"] == 0
    assert path(home).read_bytes() == before


def test_future_bucket_and_account_binding_do_not_escape(home):
    with pytest.raises(metrics.MetricsError):
        record(home, NOW + metrics.DAY_SECONDS, model=SECRET)
    assert not (home / ".config").exists()
    record(home, NOW + metrics.DAY_SECONDS, model="other")
    result = metrics.report(home, BOUND, NOW)
    assert result["requests"] == 0 and "usage_days" not in result
    result = metrics.report(home, BOUND, NOW + metrics.DAY_SECONDS)
    serialized = json.dumps(result)
    assert BOUND not in serialized and SECRET not in serialized and str(home) not in serialized
    assert result["usage_days"]["20001"]["model_usage"]["models"][0]["model"] == "other"
