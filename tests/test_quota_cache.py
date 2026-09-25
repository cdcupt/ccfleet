"""Usage windows, fresher (Erik, 2026-09-24): every five minutes, at once when a
page asks, and from Claude Code's own saved reading where it has one.

Claude Code keeps its last /usage answer in ~/.claude.json, beside the account
it was fetched for. The agent reads it and never reports the account ids in it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ccfleet_agent import agent

NOW = 1_790_275_000.0
UUID = "11111111-2222-3333-4444-555555555555"
RESET_5H = "2026-09-24T22:19:59.628508+00:00"
RESET_7D = "2026-09-26T10:59:59Z"


def saved(tmp_path, *, fetched=NOW - 60, owner=UUID, account=UUID, five=0, seven=27,
          five_reset=RESET_5H, seven_reset=RESET_7D):
    """~/.claude.json as Claude Code writes it, and the ~/.claude beside it."""
    config = tmp_path / ".claude"
    config.mkdir(exist_ok=True)
    data = {"oauthAccount": {"accountUuid": account, "emailAddress": "a@example.com"},
            agent.QUOTA_CACHE_KEY: {
                "fetchedAtMs": fetched * 1000 if isinstance(fetched, (int, float)) and
                not isinstance(fetched, bool) else fetched,
                "accountUuid": owner,
                "utilization": {
                    "five_hour": {"utilization": five, "resets_at": five_reset},
                    "seven_day": {"utilization": seven, "resets_at": seven_reset},
                    "seven_day_opus": None}}}
    if owner is None:
        del data[agent.QUOTA_CACHE_KEY]["accountUuid"]
    (tmp_path / ".claude.json").write_text(json.dumps(data))
    return config


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


# -- Claude Code's own reading ------------------------------------------------------

def test_claude_codes_reading_is_read_as_a_report(tmp_path):
    got = agent.cached_usage(saved(tmp_path))
    assert got == {"session": {"used_pct": 0, "resets_at": at(RESET_5H)},
                   "week": {"used_pct": 27, "resets_at": at(RESET_7D)},
                   "checked_at": NOW - 60}
    assert UUID not in json.dumps(got) and "a@example.com" not in json.dumps(got)


@pytest.mark.parametrize("owner", ["someone-else", None])
def test_a_reading_for_another_account_is_no_reading(tmp_path, owner):
    """From before a change of account, as Claude Code itself treats it."""
    assert agent.cached_usage(saved(tmp_path, owner=owner)) is None


def test_no_account_on_either_side_is_no_reading(tmp_path):
    assert agent.cached_usage(saved(tmp_path, owner=None, account=None)) is None


@pytest.mark.parametrize("fetched", [True, "yesterday", None])
def test_a_reading_with_no_moment_is_no_reading(tmp_path, fetched):
    assert agent.cached_usage(saved(tmp_path, fetched=fetched)) is None


@pytest.mark.parametrize("bad", [True, -1, 101, "27", None])
def test_a_window_that_is_not_a_percentage_is_left_out(tmp_path, bad):
    got = agent.cached_usage(saved(tmp_path, five=bad))
    assert "session" not in got and got["week"]["used_pct"] == 27


def test_nothing_usable_is_no_reading(tmp_path):
    assert agent.cached_usage(saved(tmp_path, five=None, seven="x")) is None


@pytest.mark.parametrize("reset", ["2026-09-24T22:19:59", "soon", "", 17])
def test_a_reset_that_is_no_moment_is_left_out_not_guessed(tmp_path, reset):
    got = agent.cached_usage(saved(tmp_path, five_reset=reset))
    assert got["session"] == {"used_pct": 0}


def test_no_file_or_no_account_is_no_reading(tmp_path):
    config = tmp_path / ".claude"
    assert agent.cached_usage(config) is None
    (tmp_path / ".claude.json").write_text("{not json")
    assert agent.cached_usage(config) is None
    (tmp_path / ".claude.json").write_text(json.dumps({agent.QUOTA_CACHE_KEY: {}}))
    assert agent.cached_usage(config) is None


# -- when to read ---------------------------------------------------------------------

@pytest.fixture
def probe(monkeypatch, tmp_path):
    """The /usage probe, counted; what it finds is set per test."""
    calls = []
    found = {"screen": {"session": {"used_pct": 50, "resets": "3pm (UTC)"}}, "writes": None}

    def read_quota(runner, now):
        calls.append(now)
        if found["writes"]:
            found["writes"]()
        return dict(found["screen"]) if found["screen"] else None
    monkeypatch.setattr(agent, "read_quota", read_quota)
    monkeypatch.setattr(agent, "quota_probe_dir", lambda: (str(tmp_path), None))
    return calls, found


def kept(ts, **extra):
    return {"quota": {"week": {"used_pct": 10}, "checked_at": ts, "ts": ts, **extra}}


def test_every_five_minutes_now():
    assert agent.QUOTA_REFRESH_S == 5 * 60


def test_a_reading_kept_under_five_minutes_is_not_read_again(tmp_path, probe):
    calls, _ = probe
    report, store = agent.quota_summary(kept(NOW - 200), now=NOW,
                                        config_dir=tmp_path / ".claude")
    assert calls == [] and store is None and report["week"]["used_pct"] == 10


def test_an_older_one_is_read_again(tmp_path, probe):
    calls, _ = probe
    report, store = agent.quota_summary(kept(NOW - 400), now=NOW,
                                        config_dir=tmp_path / ".claude")
    assert calls == [NOW] and report["session"]["used_pct"] == 50 and store["ts"] == NOW


def test_claude_codes_newer_reading_stands_in_without_a_probe(tmp_path, probe):
    calls, _ = probe
    config = saved(tmp_path, fetched=NOW - 30)
    report, store = agent.quota_summary(kept(NOW - 200), now=NOW, config_dir=config)
    assert calls == [] and report["week"]["used_pct"] == 27
    assert store["ts"] == NOW - 30, "the next read is five minutes after Claude Code's"


def test_claude_codes_reading_no_newer_than_ours_is_ignored(tmp_path, probe):
    calls, _ = probe
    config = saved(tmp_path, fetched=NOW - 250)
    report, store = agent.quota_summary(kept(NOW - 200), now=NOW, config_dir=config)
    assert calls == [] and store is None and report["week"]["used_pct"] == 10


def test_claude_codes_old_reading_does_not_stand_in(tmp_path, probe):
    calls, _ = probe
    config = saved(tmp_path, fetched=NOW - 400)
    agent.quota_summary({}, now=NOW, config_dir=config)
    assert calls == [NOW]


def test_after_a_probe_claude_codes_numbers_win_over_the_screen(tmp_path, probe):
    calls, found = probe
    config = tmp_path / ".claude"
    found["writes"] = lambda: saved(tmp_path, fetched=NOW + 5)
    report, store = agent.quota_summary(kept(NOW - 400), now=NOW, config_dir=config)
    assert calls == [NOW] and report["week"] == {"used_pct": 27, "resets_at": at(RESET_7D)}
    assert store["ts"] == NOW


def test_a_reading_claude_code_made_just_before_the_probe_still_answers_it(tmp_path, probe):
    """It fetches at most once a minute, so the probe could cause none newer:
    what it finds after the probe, that recent, is the answer."""
    calls, found = probe
    found["writes"] = lambda: saved(tmp_path, fetched=NOW - agent.QUOTA_CACHE_SLACK_S)
    report, _ = agent.quota_summary(kept(NOW - 400), now=NOW, config_dir=tmp_path / ".claude")
    assert calls == [NOW] and report["week"]["used_pct"] == 27


def test_a_reading_older_than_that_leaves_the_screen_to_answer(tmp_path, probe):
    calls, found = probe
    found["writes"] = lambda: saved(tmp_path, fetched=NOW - agent.QUOTA_CACHE_SLACK_S - 1)
    report, _ = agent.quota_summary(kept(NOW - 400), now=NOW, config_dir=tmp_path / ".claude")
    assert calls == [NOW] and report["session"]["used_pct"] == 50


def test_a_read_asked_for_is_read_now_whatever_the_age(tmp_path, probe):
    calls, _ = probe
    agent.quota_summary(kept(NOW - 60), now=NOW, config_dir=tmp_path / ".claude",
                        wanted_at=NOW - 10)
    assert calls == [NOW]


def test_a_read_asked_for_before_the_one_kept_is_answered_already(tmp_path, probe):
    calls, _ = probe
    agent.quota_summary(kept(NOW - 60), now=NOW, config_dir=tmp_path / ".claude",
                        wanted_at=NOW - 90)
    assert calls == []


@pytest.mark.parametrize("wanted", [True, "now", None])
def test_anything_but_a_moment_asks_nothing(tmp_path, probe, wanted):
    calls, _ = probe
    agent.quota_summary(kept(NOW - 60), now=NOW, config_dir=tmp_path / ".claude",
                        wanted_at=wanted)
    assert calls == []


def test_a_read_asked_for_is_tried_once_even_when_it_fails(tmp_path, probe):
    """A slot that cannot read is not asked again every minute."""
    calls, found = probe
    found["screen"] = None
    state = kept(NOW - 60)
    report, store = agent.quota_summary(state, now=NOW, config_dir=tmp_path / ".claude",
                                        wanted_at=NOW - 10)
    assert calls == [NOW] and report["week"]["used_pct"] == 10
    assert store["asked"] == NOW - 10 and store["ts"] == NOW - 60
    assert "asked" not in report
    again, _ = agent.quota_summary({"quota": store}, now=NOW + 60,
                                   config_dir=tmp_path / ".claude", wanted_at=NOW - 10)
    assert calls == [NOW], "the same request is not tried twice"
    assert "asked" not in again, "the mark is the slot's own, never reported"


def test_a_failed_read_with_nothing_kept_is_marked_tried_too(tmp_path, probe):
    calls, found = probe
    found["screen"] = None
    report, store = agent.quota_summary({}, now=NOW, config_dir=tmp_path / ".claude",
                                        wanted_at=NOW - 10)
    assert calls == [NOW] and report is None and store == {"asked": NOW - 10}
    agent.quota_summary({"quota": store}, now=NOW + 60, config_dir=tmp_path / ".claude",
                        wanted_at=NOW - 10)
    assert calls == [NOW, NOW + 60], "nothing kept: read on schedule, not for the request"
    assert agent._asked(NOW - 10, max(0.0, store["asked"])) is False


def test_a_request_is_marked_tried_when_there_is_nowhere_to_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "quota_probe_dir", lambda: (None, "no home"))
    _, store = agent.quota_summary(kept(NOW - 400), now=NOW, config_dir=tmp_path / ".claude",
                                   wanted_at=NOW - 10)
    assert store["asked"] == NOW - 10 and store["skipped"] == "no home"


def test_true_is_no_request(tmp_path, probe):
    _, store = agent.quota_summary({}, now=NOW, config_dir=tmp_path / ".claude",
                                   wanted_at=True)
    assert "asked" not in store


def test_claude_codes_reading_answers_a_request_it_is_newer_than(tmp_path, probe):
    calls, _ = probe
    config = saved(tmp_path, fetched=NOW - 5)
    report, _ = agent.quota_summary(kept(NOW - 60), now=NOW, config_dir=config,
                                    wanted_at=NOW - 10)
    assert calls == [] and report["week"]["used_pct"] == 27


def test_claude_codes_reading_older_than_the_request_does_not(tmp_path, probe):
    calls, _ = probe
    config = saved(tmp_path, fetched=NOW - 20)
    agent.quota_summary(kept(NOW - 60), now=NOW, config_dir=config, wanted_at=NOW - 10)
    assert calls == [NOW]


def test_the_instant_parser():
    assert agent._instant("2026-09-24T22:19:59Z") == datetime(
        2026, 9, 24, 22, 19, 59, tzinfo=timezone.utc).timestamp()
    assert agent._instant("2026-09-24T22:19:59") is None
    assert agent._instant(None) is None and agent._instant("x") is None
