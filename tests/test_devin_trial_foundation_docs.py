"""The bilingual trial walkthrough matches the enforced local/native boundaries."""

import shlex
from pathlib import Path

import pytest

from ccfleet_agent import devin_mcp
from ccfleetd import bilingual, devin_trial, trial_worker_transport
from tests.test_html_document_preservation import Document

ROOT = Path(__file__).resolve().parents[1]
TARGET = "/docs/library/docs/devin-trial-foundation.html"


def document(language="en"):
    source = (ROOT / "docs/devin-trial-foundation.html").read_text()
    return Document(bilingual.project_html(source, language, TARGET))


@pytest.mark.parametrize("language", ["en", "zh"])
def test_foundation_is_a_bilingual_accessible_current_document(language):
    source = (ROOT / "docs/devin-trial-foundation.html").read_text()
    assert bilingual.CSS in source and bilingual.SCRIPT_TAG in source
    assert bilingual.route_target(f"/{language}{TARGET}") == (TARGET, language, True)
    page = document(language)
    assert not page.duplicate_ids
    assert {"availability", "request-cycle", "identity", "launch", "tools", "budgets",
            "clocks", "errors", "privacy", "operator-contract"} <= page.ids
    assert f"/en{TARGET}" in page.links and f"/zh{TARGET}" in page.links
    assert any(tag == "figure" and attrs.get("aria-labelledby") == "request-flow-caption"
               for tag, attrs in page.tags)
    assert len([1 for tag, attrs in page.tags if tag == "ol" and attrs.get("class") == "flow"]) == 1
    assert ".flow{grid-template-columns:1fr}" in source
    assert ".table-scroll{overflow-x:auto" in source
    assert not any(tag == "form" for tag, _ in page.tags)
    if language == "en":
        assert "How the daily Devin trial works" in page.text
        assert "每日 Devin 试用怎样工作" not in page.text
    else:
        assert "每日 Devin 试用怎样工作" in page.text
        assert "Follow one question from start to finish" not in page.text


def test_walkthrough_explains_local_execution_remote_identity_and_fresh_continuations():
    text = document().text
    for phrase in (
        "Your computer sends a request", "Fleet checks the entrance",
        "A fresh remote Devin process thinks", "Your computer executes a requested tool",
        "tool handoff and its continuation count as separate model turns",
        "continuity comes from the history Claude Code includes in the next request",
        "not a reused remote session", "freezes the list for that native turn",
        "Replies and SSE are buffered until usage and cleanup checks finish",
        "The interface is official Claude Code; the remote harness is native Devin",
        "Devin may correctly introduce itself as Devin",
        "does not rewrite an agent's identity", "root-controlled max-effort policy",
    ):
        assert phrase in text
    chinese = document("zh").text
    for phrase in ("本机发送请求", "Fleet 检查入口", "全新的远端 Devin 进程思考",
                   "本机执行所请求的工具", "分别计为模型轮次", "不代表支持 Claude 的全部功能"):
        assert phrase in chinese


def test_launch_retrieval_and_web_limits_are_explicit_without_claiming_no_network():
    page = document()
    command = next(code for code in page.codes if code.startswith("--safe-mode --permission-mode"))
    assert shlex.split(command) == ["--safe-mode", "--permission-mode", "bypassPermissions",
                                    "--disallowedTools", "WebSearch,WebFetch"]
    for phrase in (
        "It lasts 30 days across daily resets", "A GET does not rotate it",
        "Explicit replacement revokes the old key immediately",
        "Older random keys cannot be redisplayed", "Safe mode", "It is optional",
        "without individual approval prompts", "Saved settings are unchanged",
        "preserve its existing private key", "No new key or Claude /login is needed",
        "plain outer WebSearch description is not the same as its nested Anthropic server-tool request",
        "Fleet does not drop them or pretend they ran", "not a blanket ban on networking",
        "compatible local tools or separately configured MCP tools",
        "do not change the remote Devin model or its root-controlled thinking policy",
    ):
        assert phrase in page.text
    assert ("ENABLE_TOOL_SEARCH=false CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 "
            "CLAUDE_CODE_DISABLE_THINKING=1") in page.codes


def test_separate_daily_and_rate_budgets_match_source_without_promising200_full_turns():
    text = document().text
    assert f"{devin_trial.DEFAULT_DAILY_TOKENS:,} per account" in text
    assert f"{devin_trial.DEFAULT_GLOBAL_DAILY_TOKENS:,} shared daily" in text
    assert f"{devin_trial.DAILY_REQUESTS} model turns and {devin_trial.DAILY_LEASES} admitted leases" in text
    assert devin_trial.DAILY_REQUESTS == devin_trial.GLOBAL_DAILY_REQUESTS
    assert devin_trial.DAILY_LEASES == devin_trial.GLOBAL_DAILY_LEASES
    assert devin_trial.DAILY_RUNTIME_S == devin_trial.GLOBAL_DAILY_RUNTIME_S
    assert f"{devin_trial.DAILY_RUNTIME_S:,} seconds ({devin_trial.DAILY_RUNTIME_S // 60} minutes)" in text
    assert f"reserves {devin_trial.MAX_STEP_S} seconds even when the reply is quick" in text
    assert f"at most {devin_trial.DAILY_RUNTIME_S // devin_trial.MAX_STEP_S} full reservations" in text
    assert "Burst 3; refill 40 requests per minute per account" in text
    assert "invalid request can consume this bucket without using a model turn or runtime" in text
    assert "rejects known exhausted budgets before contacting the worker" in text
    assert "checks transactionally again after readiness" in text
    assert "Unknown holds are never automatically refunded or changed to zero" in text
    assert "does not clear a safety pause or certify old usage" in text
    assert "proven failure before any model request-frame write is different" in text
    assert "up to 20,000" in text.lower()


def test_timing_error_recovery_and_operator_guards_remain_visible():
    text = document().text
    for phrase in (
        f"{trial_worker_transport.READINESS_SECONDS}-second readiness check",
        "120-second native generation deadline",
        f"{trial_worker_transport.TURN_SECONDS}-second transport deadline",
        f"{devin_trial.MAX_STEP_S}-second runtime reservation",
        f"{devin_trial.MAX_RUNTIME_S}-second lease and MCP lifetime",
        f"{devin_mcp.SERVER_LIFETIME_S}-second outer bound",
        f"{devin_mcp.CALL_WAIT_S}-second bound",
        "120/300-second grant freshness", "preserves the original observation time",
        "400 — unsupported or too large", "401 — trial key",
        "429 — rate, busy, budget or pause", "503 — unavailable or unconfirmed",
        "Claude /login does not restore a Fleet key", "do not retry automatically",
        "missing, stale, BAD or mismatched grant", "single private, bounded diagnostic summary",
    ):
        assert phrase in text
    for phrase in (
        "hosted trial is enabled by the operator; the source default is still off",
        "CCFLEET_DEVIN_TRIAL_ENABLED=false", "No old session/history directory is copied",
        "private artifacts are quarantined", "root-owned full manifest",
        "fixed credential file", "No key/host/path comes from HTTP",
        "domain-separated HMAC-SHA256", "account ownership, expiry, revocation and the derived hash",
        "Tool receipts require atomic no-replace publication",
        "an existing destination is never replaced", "Missing syscall or filesystem support fails closed",
        "with no link/unlink or replace fallback", "not evidence that a deployed node has passed a Linux smoke test",
        "Only exact reviewed predecessor profiles can migrate",
        "existing usage, runtime, holds, sessions, pause state and history are preserved",
        "Offline fakes do not prove these provider behaviors",
    ):
        assert phrase in text
    assert "Implementation prepared" not in text
    assert "Keep the feature off until" not in text


def test_readme_trial_facts_are_current_and_link_to_the_technical_walkthrough():
    source = (ROOT / "README.html").read_text()
    section = source.split('<section class="notice" id="daily-trial">', 1)[1].split('</section>', 1)[0]
    page = Document(section)
    assert "docs/devin-trial-foundation.html" in page.links
    for phrase in ("1,000,000 tokens per verified Fleet account", "10,000,000 tokens shared",
                   "200 model turns", "200 reserved runtime minutes", "75 such reservations",
                   "120-second generation deadline", "160-second reservation",
                   "--disallowedTools WebSearch,WebFetch", "重新启动 Claude Code"):
        assert phrase in page.text
    for stale in ("100,000 per verified", "twenty turns/twenty", "forty-second generation",
                  "sixty-second reservation", "最多二十轮", "最多四十秒"):
        assert stale not in page.text
