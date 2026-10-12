"""Customer controls remain discoverable, truthful and isolated from raw health data."""

import html
import re
import runpy
import shlex
from pathlib import Path

import pytest

from ccfleetd import bilingual, customer_docs, usersite
from ccfleetd.config import Config
from ccfleetd.store import Store
from tests.test_cli_access import active_slot
from tests.test_html_document_preservation import read_document

NOW = 1_800_000_000.0
ROOT = Path(__file__).resolve().parents[1]


def credentials(**changes):
    return {"present": True, "logged_in": True, "bound_fp": "a" * 16,
            "account_fp": "a" * 16, "expires_at": (NOW + 3600) * 1000, **changes}


def badge(creds=None, *, login=None, heard=NOW, state="active"):
    return usersite._account_health({"state": state}, {"credentials": creds or credentials()},
                                    login or {}, heard, Config(), NOW)


@pytest.mark.parametrize("changes,label,code", [
    ({}, "Reported ready", "ready"),
    ({"expires_at": (NOW + 100) * 1000}, "Reported ready", "ready"),
    ({"expires_at": (NOW + 20) * 1000}, "Renewal pending", "renewal_pending"),
    ({"expires_at": (NOW - 60) * 1000}, "Renewal pending", "renewal_pending"),
    ({"logged_in": False}, "Sign-in required", "sign_in_required"),
    ({"account_fp": "b" * 16}, "Account maintenance", "switching"),
    ({"bound_fp": None}, "Readiness unverified", "degraded"),
])
def test_health_badge_reports_distinct_recovery_states_without_acceptance_promise(changes, label, code):
    body = badge(credentials(**changes))
    assert label in body and f'data-health="{code}"' in body
    assert "A heartbeat observation does not prove" in body
    assert "Anthropic will accept a model request" in body
    assert "ccfleet doctor --privacy" in body


def test_unexpired_access_keeps_reported_readiness_and_warns_about_renewal():
    creds = credentials(expires_at=(NOW + 100) * 1000)
    body = badge(creds)
    assert 'data-health="ready"' in body and "Reported ready" in body
    observation = usersite.client_status.health({"state": "active"}, {"credentials": creds},
                                               None, heard=NOW, now=NOW, max_age=900)
    assert observation["renewal_warning"] == "renewal_due"
    assert "Access renewal has not been verified" in body
    assert "Renewal pending" not in body and "Sign-in required" not in body
    assert "A heartbeat observation does not prove" in body


@pytest.mark.parametrize("heard", [None, NOW - 1000, NOW + 1000])
def test_stale_or_future_health_never_claims_ready(heard):
    body = badge(heard=heard)
    assert "Readiness unverified" in body and "Reported ready" not in body


def test_a_claimed_not_active_slot_does_not_promise_ready_from_account_facts_alone():
    body = badge(state="claimed")
    assert "Reported ready" not in body


@pytest.mark.parametrize("untrusted", ["<script>SECRET</script>", {"SECRET": "value"}, None])
def test_badge_does_not_interpolate_unrecognized_health_or_private_payload(monkeypatch, untrusted):
    monkeypatch.setattr(usersite.client_status, "health", lambda *args, **kwargs: {
        "health": untrusted, "reason": "SECRET_TOKEN", "email": "SECRET_EMAIL",
        "path": "SECRET_PATH", "raw_error": "SECRET_ACCOUNT"})
    body = badge()
    assert 'data-health="degraded"' in body
    assert "SECRET" not in body and "<script>" not in body


def test_health_badge_itself_never_exports_account_identity():
    body = badge(credentials(email="SECRET_EMAIL", accountUuid="SECRET_UUID",
                             accessToken="SECRET_TOKEN", local_path="SECRET_PATH"))
    assert "Reported ready" in body and "SECRET" not in body


def test_badge_addition_keeps_pairing_and_reauthentication_csrf_forms():
    store = Store(":memory:")
    try:
        slot = active_slot(store)
        node = store.get_node(slot["node_id"])
        heartbeat = {"ts": NOW, "payload": {"mode": "machine", "slots": [
            {"unix_user": slot["unix_user"], "credentials": credentials(),
             "claude": {"version": "2.1.284"}}]}}
        body = usersite._slot_card(slot, node, heartbeat, {}, "CSRF_SYNTHETIC", Config(), NOW)
        assert "Reported ready" in body
        for action, label in (("signin", "Sign in again"), ("cli", "Connect this computer")):
            form = re.search(rf'<form[^>]+action="/account/slots/s1/{action}".*?</form>', body)
            assert form and label in form[0]
            assert 'name="csrf" value="CSRF_SYNTHETIC"' in form[0]
        assert "ccfleet start" in body and "ccfleet sessions" in body
    finally:
        store.close()


@pytest.mark.parametrize("login_state", ["requested", "url_ready", "code_sent"])
def test_signin_controls_remain_usable_while_health_is_switching(login_state):
    store = Store(":memory:")
    try:
        slot = active_slot(store)
        node = store.get_node(slot["node_id"])
        heartbeat = {"ts": NOW, "payload": {"slots": [
            {"unix_user": slot["unix_user"], "credentials": credentials()}]}}
        login = {"state": login_state, "kind": "switch",
                 "url": "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"}
        body = usersite._slot_card(slot, node, heartbeat, login, "CSRF_SWITCH", Config(), NOW)
        assert "Account maintenance" in body and "Reported ready" not in body
        assert 'action="/account/slots/s1/cancel"' in body
        assert 'name="csrf" value="CSRF_SWITCH"' in body
        if login_state == "url_ready":
            assert 'action="/account/slots/s1/code"' in body and "Send code" in body
    finally:
        store.close()


def test_documented_new_command_examples_are_accepted_by_the_real_parser():
    parser = runpy.run_path(str(ROOT / "laptop/ccfleet"), run_name="ccfleet_docs")["parser"]()
    fragment = customer_docs._client_tools()
    parsed = []
    from tests.test_html_document_preservation import Document
    examples = Document(bilingual.project_html(fragment, "en")).pre_codes
    for sample in examples:
        for line in sample.splitlines():
            if line.startswith("ccfleet "):
                parsed.append(parser.parse_args(shlex.split(line)[1:]))
    commands = {args.command for args in parsed}
    assert {
        "start",
        "sessions",
        "preferences",
        "doctor",
        "status",
        "version",
        "update",
        "jobs",
        "local",
    } <= commands
    assert any(args.command == "update" and args.rollback for args in parsed)
    assert any(
        args.command == "doctor" and args.privacy and args.json and args.export for args in parsed
    )


def test_public_guide_explains_current_managed_local_jobs():
    body = customer_docs.guide(Config())
    assert 'id="client-tools"' in body and 'id="background-jobs"' in body
    for text in (
        "local Claude print-mode",
        "24 hours",
        "at most 16",
        "4 MiB",
        "never automatically restart",
        "not sandbox-contained",
        "not included in support exports",
        "computer must stay running",
    ):
        assert text in body
    assert "--transport bridge" not in body and "ccfleet remote" not in body


def test_release_guidance_requires_verified_current_updates():
    body = customer_docs.guide(Config())
    for phrase in (
        "pinned Ed25519 key",
        "immutable manifest",
        "every helper",
        "failed verification or download leaves the current client",
        "absent or expired signed channel is an error",
        "not Anthropic's original Claude executable",
    ):
        assert phrase in html.unescape(re.sub(r"<[^>]+>", "", body))
    assert "bootstrap" not in body


def test_public_privacy_covers_local_job_retention_without_hiding_root_access():
    privacy = usersite.privacy_page(Config())
    assert "prompts, options, project paths" in privacy and "retained until deliberately removed" in privacy
    assert "never automatically uploaded to the operator" in privacy
    overview = customer_docs.overview(Config())
    assert "Administrators retain root access" in overview
    assert "home directory only you can read" not in overview


def test_repository_guides_include_the_new_controls_and_preserve_privacy_boundaries():
    for path in (ROOT / "README.html", ROOT / "docs/local-relay.html"):
        source = read_document(path).text
        for command in ("ccfleet start", "ccfleet sessions", "ccfleet doctor --privacy",
                        "ccfleet status --json", "ccfleet preferences set", "ccfleet update",
                        "ccfleet update --rollback", "ccfleet jobs stop JOB_ID"):
            assert command in source, (path.name, command)
        assert "not a" in source and "sandbox" in source
        normalized = " ".join(source.split())
        if path.name == "README.html":
            assert "The gateway can read model messages and replies" in source
            assert "The gateway sees connection IP/transport metadata and model contents" in source
            assert "Gateway and slot administrators can inspect or alter requests/responses" in source
            assert "no credential pool or shared inference account" in normalized
            assert "holder's own slots" in normalized
        else:
            assert "direct HTTPS gateway can read model requests" in source
            assert "The broker sees the incoming IP and connection/routing metadata" in source
            assert "Slot root can inspect or alter relayed requests and responses" in source
            assert "does not pool accounts" in normalized
            assert "specific own slot or several own slots in priority order" in normalized
