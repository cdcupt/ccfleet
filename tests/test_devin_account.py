"""Native account metadata only: synthetic files and fake processes, no native calls."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import types
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ccfleet_agent import devin_account as account
from ccfleet_agent import devin_native as native
from tests.test_node_egress_review import protected as protected

ORIGIN = "https://server.codeium.com"
SUBJECT = "synthetic_user_subject_01234567890123456"
SECRET = b"synthetic-token-never-public"
CREDENTIAL = (b'windsurf_api_key = "' + SECRET + b'"\n'
              b'api_server_url = "https://server.codeium.com"\n'
              b'devin_webapp_host = "app.devin.ai"\n'
              b'devin_api_url = "https://api.devin.ai"\n')
REPORT_FIELDS = {"account_fp", "identity_verified", "bridge_contract", "capabilities",
                 "native_version", "adapter_revision", "model_map_digest", "observed_at"}


def test_pinned_credential_subset_is_validated_without_returning_secret_material():
    assert account.validate_native_credentials(CREDENTIAL, expected_api_origin=ORIGIN) is None
    assert account.validate_native_credentials(CREDENTIAL.replace(b"\n", b"\r\n"),
                                               expected_api_origin=ORIGIN) is None
    reordered = b"\n".join(reversed(CREDENTIAL.rstrip(b"\n").split(b"\n"))) + b"\n"
    assert account.validate_native_credentials(reordered, expected_api_origin=ORIGIN) is None


@pytest.mark.parametrize("raw", [
    b"", b"x" * (65536 + 1), b"\xef\xbb\xbf" + CREDENTIAL,
    CREDENTIAL.replace(SECRET, b""), CREDENTIAL.replace(SECRET, b"x" * 16385),
    CREDENTIAL + b'windsurf_api_key = "another-token"\n',
    CREDENTIAL + b'api_server_url = "https://server.codeium.com"\n',
    CREDENTIAL + b'unknown_option = "value"\n',
    CREDENTIAL + b'[extra]\n', CREDENTIAL + b'[[extra]]\n',
    CREDENTIAL + b'# comment\n', CREDENTIAL.replace(b'"\n', b'" # comment\n', 1),
    CREDENTIAL.replace(b'windsurf_api_key', b'"windsurf_api_key"', 1),
    CREDENTIAL.replace(b'windsurf_api_key', b'account.windsurf_api_key', 1),
    CREDENTIAL.replace(SECRET, b'has\\escape'),
    CREDENTIAL.replace(SECRET, b'has\\u0061escape'),
    CREDENTIAL.replace(SECRET, b'has\\U00000061escape'),
    CREDENTIAL.replace(SECRET, b'has\\"quote'),
    CREDENTIAL.replace(SECRET, b'has\tcontrol'),
    CREDENTIAL.replace(SECRET, b'has\x00control'),
    CREDENTIAL.replace(SECRET, b'nonascii-\xff'),
    CREDENTIAL.replace(b'"' + SECRET + b'"', b'42'),
    CREDENTIAL.replace(b'"' + SECRET + b'"', b"'literal-token'"),
    CREDENTIAL.replace(b'"' + SECRET + b'"', b'"""multiline"""'),
    CREDENTIAL.replace(b'devin_api_url = "https://api.devin.ai"\n', b''),
    b"\n" * 17 + CREDENTIAL,
])
def test_ambiguous_unrecognized_or_escaped_native_toml_is_rejected(raw):
    with pytest.raises(account.AccountError) as captured:
        account.validate_native_credentials(raw, expected_api_origin=ORIGIN)
    assert SECRET.decode() not in str(captured.value)


@pytest.mark.parametrize("endpoint", [
    b'https://server.codeium.com/', b'http://server.codeium.com', b'https://localhost',
    b'https://127.0.0.1', b'https://169.254.169.254', b'https://server.codeium.com.evil.test',
    b'https://server.codeium.com@evil.test', b'https://evil.test/server.codeium.com',
    b'https://server.codeium.com:444', b'https://SERVER.codeium.com',
])
def test_api_server_requires_exact_official_url_not_just_a_host_like_substring(endpoint):
    raw = CREDENTIAL.replace(b'https://server.codeium.com', endpoint)
    with pytest.raises(account.AccountError, match="native_credential_endpoints"):
        account.validate_native_credentials(raw, expected_api_origin=ORIGIN)


@pytest.mark.parametrize("before,after", [
    (b'app.devin.ai', b'evil.test'),
    (b'app.devin.ai', b'https://app.devin.ai'),
    (b'https://api.devin.ai', b'https://evil.test'),
    (b'https://api.devin.ai', b'https://api.devin.ai/'),
])
def test_devin_identity_endpoints_are_pinned_as_well_as_displayed_api_server(before, after):
    raw = CREDENTIAL.replace(before, after)
    with pytest.raises(account.AccountError, match="native_credential_endpoints"):
        account.validate_native_credentials(raw, expected_api_origin=ORIGIN)


def test_unapproved_root_api_origin_also_fails_closed():
    with pytest.raises(account.AccountError, match="native_credential_api_origin"):
        account.validate_native_credentials(CREDENTIAL, expected_api_origin="https://other.test")


def status(**changes):
    values = {"Credentials": "", "File": "/private/native/credentials.toml",
              "API server": ORIGIN, "Devin webapp": "https://app.devin.ai",
              "User": "", "Name": "Private Person", "Email": "private@example.test",
              "User ID": SUBJECT, "Account": "", "Tier": "Pro", "Plan": "Pro",
              "Team ID": "team-private (annotation)", "Team settings": "",
              "Allowed models": "all", "Fetched at": "cached team settings"}
    values.update(changes)
    return ("Logged in (via Devin).\n" +
            "\n".join(key + ": " + value for key, value in values.items()) + "\n").encode()


def test_subject_fingerprint_uses_global_user_and_trusted_origin_only():
    parsed = account.parse_auth_status(status(), expected_api_origin=ORIGIN)
    changed = account.parse_auth_status(status(**{"Email": "other@example.test", "Team ID": "other"}),
                                        expected_api_origin=ORIGIN)
    assert parsed.fingerprint() == changed.fingerprint()
    assert len(parsed.fingerprint()) == 64
    other = account.parse_auth_status(status(**{"User ID": SUBJECT + "x"}), expected_api_origin=ORIGIN)
    assert other.fingerprint() != parsed.fingerprint()
    other_origin = "https://other.example.test"
    other = account.parse_auth_status(status(**{"API server": other_origin}), expected_api_origin=other_origin)
    assert other.fingerprint() != parsed.fingerprint()
    assert SUBJECT not in repr(parsed)


@pytest.mark.parametrize("optional_fields", [
    {"Default model": "claude-opus-5-5-max"},
    {"Disabled tools": "tool-one, tool-two"},
    {"Primary org": "private-org-display"},
    {"Default model": "claude-opus-5-5-max", "Disabled tools": "tool-one, tool-two",
     "Primary org": "private-org-display"},
])
def test_pinned_native_optional_display_labels_do_not_change_verified_subject(optional_fields):
    # The pinned 3000.11.92 auth-status formatter includes these optional
    # account/team fields. Their display values never establish identity.
    expected = account.parse_auth_status(status(), expected_api_origin=ORIGIN)
    actual = account.parse_auth_status(status(**optional_fields), expected_api_origin=ORIGIN)
    changed = account.parse_auth_status(
        status(**dict.fromkeys(optional_fields, "different display value")),
        expected_api_origin=ORIGIN,
    )
    assert actual == changed == expected
    assert actual.fingerprint() == expected.fingerprint()


@pytest.mark.parametrize("label", ["Default model", "Disabled tools", "Primary org"])
def test_optional_native_display_labels_remain_exact_and_unique(label):
    raw = status(**{label: "display value"})
    with pytest.raises(account.AccountError, match="native_status_duplicate_label"):
        account.parse_auth_status(raw + (label + ": second value\n").encode(),
                                  expected_api_origin=ORIGIN)
    for near_spelling in (label.lower(), label + "s", label.replace(" ", "_")):
        with pytest.raises(account.AccountError, match="native_status_unknown_label"):
            account.parse_auth_status(status(**{near_spelling: "display value"}),
                                      expected_api_origin=ORIGIN)


@pytest.mark.parametrize("identity_field", ["User ID", "API server"])
def test_optional_display_fields_never_relax_required_identity_labels(identity_field):
    raw = status(**{"Default model": "optional", "Disabled tools": "none", "Primary org": "org"})
    prefix = (identity_field + ":").encode()
    missing = b"\n".join(line for line in raw.splitlines() if not line.startswith(prefix)) + b"\n"
    with pytest.raises(account.AccountError, match="native_status_subject_unverified"):
        account.parse_auth_status(missing, expected_api_origin=ORIGIN)
    with pytest.raises(account.AccountError, match="native_status_duplicate_label"):
        account.parse_auth_status(raw + (identity_field + ": duplicate\n").encode(),
                                  expected_api_origin=ORIGIN)


@pytest.mark.parametrize("raw,reason", [
    (b"", "native_status_size"),
    (b"x" * (account.MAX_STATUS_BYTES + 1), "native_status_size"),
    (b"\xff", "native_status_encoding"),
    (status() + b"\x1b[0m", "native_status_controls"),
    (status() + b"\t", "native_status_controls"),
    (b"\n" * 129, "native_status_line_bound"),
    (status() + b"User ID: another_subject\n", "native_status_duplicate_label"),
    (status() + b"New field: unexpected\n", "native_status_unknown_label"),
    (status() + b"unstructured value\n", "native_status_unknown_line"),
    (status(**{"User ID": "private@example.test"}), "native_status_subject_invalid"),
    (status(**{"API server": "https://other.example.test"}), "native_status_origin_mismatch"),
    (b"API server: https://server.codeium.com\n", "native_status_subject_unverified"),
])
def test_status_rejects_ambiguous_or_unbounded_identity(raw, reason):
    with pytest.raises(account.AccountError, match=reason):
        account.parse_auth_status(raw, expected_api_origin=ORIGIN)


@pytest.mark.parametrize("value", ["http://server.codeium.com", "https://u:p@server.codeium.com",
    "https://server.codeium.com/path", "https://server.codeium.com?secret=x",
    "https://server.codeium.com#fragment", "https://server.codeium.com:0", "https://bad\n.test"])
def test_origin_is_a_trusted_namespace_not_a_secret_url(value):
    with pytest.raises(account.AccountError, match="invalid_native_api_origin"):
        account.canonical_origin(value)


def test_plain_sections_crlf_and_default_port_are_canonical():
    raw = status(**{"API server": "https://SERVER.codeium.com:443/"})
    raw = raw.replace(b"Credentials: ", b"Credentials").replace(b"\n", b"\r\n")
    assert account.parse_auth_status(raw, expected_api_origin=ORIGIN).api_origin == ORIGIN


def test_exact_native_header_is_optional_and_only_accepted_once_before_labels():
    raw = status()
    without_header = raw.removeprefix(b"Logged in (via Devin).\n")
    assert account.parse_auth_status(raw, expected_api_origin=ORIGIN) == account.parse_auth_status(
        without_header, expected_api_origin=ORIGIN)
    for bad in (raw + b"Logged in (via Devin).\n", b"Logged in (via Devin).\n" + raw,
                without_header + b"Logged in (via Devin).\n", raw.replace(b"via Devin", b"via Other")):
        with pytest.raises(account.AccountError, match="native_status_unknown_line"):
            account.parse_auth_status(bad, expected_api_origin=ORIGIN)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    credentials = home / ".local/share/devin/credentials.toml"
    credentials.parent.mkdir(parents=True)
    credentials.write_bytes(CREDENTIAL)
    credentials.chmod(0o600)
    (home / "never-copy-cache").write_text("private cached identity")
    binding = native.OwnershipBinding("owner", "key", 1, "slot", "a" * 32, 2, 3)
    data = {"binding": asdict(binding), "model_uid": "claude-opus-5-5-max",
            "request_models": ["opus", "claude-opus-5-5"],
            "verified_models": ["claude-opus-5-5-max"], "enabled": False,
            "files": {"adapter": {"path": "/fixed/adapter", "sha256": "b" * 64},
                      "executable": {"path": "/fixed/devin", "sha256": "c" * 64}}}
    config = native.NativeConfig(tmp_path / "unused-policy", "d" * 64, json.dumps(data))
    flags = {"authorized": True, "cleanup": True, "returncode": 0, "mutate": False,
             "revoke": False, "source_verified": True, "expected_proxy_environment": {}}
    monkeypatch.setattr(native.NativeConfig, "validate", lambda self: (home, state))
    runtime = {"bridge_contract": "native-agent-v1", "native_version": "3000.11.92",
               "adapter_revision": "b" * 64, "model_map_digest": account.model_map_digest(config)}
    source = {"version": 1, "metadata_profile": "native-auth-status-fresh-home-v1",
              "native_version": runtime["native_version"], "native_sha256": "c" * 64,
              "api_origin": ORIGIN, "collector_sha256": "e" * 64,
              "adapter_revision": runtime["adapter_revision"],
              "model_map_digest": runtime["model_map_digest"],
              "capabilities": dict.fromkeys(account.CAPABILITIES, True),
              "live_subject_source_verified": True, "proof_sha256": "f" * 64}
    profile = account.SubjectSourceProfile(tmp_path / "unused-source", "a" * 64, json.dumps(source))

    def validate_source(self):
        return {**source, "live_subject_source_verified": flags["source_verified"]}

    monkeypatch.setattr(account.SubjectSourceProfile, "validate", validate_source)
    catalog = json.dumps({"families": [{"variants": [{"model_uid": data["model_uid"]}]}]}).encode()
    outputs, calls, stopped = [status(), catalog], [], []

    def popen(argv, **kwargs):
        assert argv[0] == "/fixed/devin"
        fresh = Path(kwargs["env"]["HOME"])
        assert fresh != home and fresh.parent == state
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
        assert (fresh / ".local/share/devin/credentials.toml").read_bytes() == CREDENTIAL
        assert stat.S_IMODE((fresh / ".local/share/devin/credentials.toml").stat().st_mode) == 0o600
        assert not (fresh / "never-copy-cache").exists()
        assert set(kwargs["env"]) == {"HOME", "PATH", "LANG", "TERM", "NO_COLOR",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"} | set(
                flags["expected_proxy_environment"])
        assert all(kwargs["env"].get(key) == value
                   for key, value in flags["expected_proxy_environment"].items())
        policy = json.loads((fresh / ".config/devin/config.json").read_text())
        assert policy["auto_update"] is False and not any(policy["read_config_from"].values())
        assert kwargs["start_new_session"] is True
        process = types.SimpleNamespace(pid=12345, stdout=io.BytesIO(outputs[len(calls)]),
                                        stderr=io.BytesIO(), returncode=None)
        calls.append((argv, kwargs, process))
        return process

    def stop(process):
        assert Path(calls[-1][1]["env"]["HOME"]).exists()
        process.returncode = flags["returncode"]
        stopped.append(process)
        if flags["mutate"]:
            credentials.write_bytes(b"changed-synthetic-credential")
        if flags["revoke"]:
            flags["authorized"] = False
        return flags["cleanup"]

    monkeypatch.setattr(account, "_stop_group", stop)

    def run(**kwargs):
        return account.collect_readiness(config, binding, profile, runtime=runtime,
            authorize=lambda actual: actual == binding and flags["authorized"],
            now=lambda: 1800000000.0, **kwargs)

    return types.SimpleNamespace(**locals())


def test_default_collection_never_reads_credential_or_executes(scenario, monkeypatch):
    monkeypatch.setattr(account, "_credential", lambda _: pytest.fail("default read credentials"))
    result = scenario.run()
    assert not result.report["identity_verified"] and result.report["account_fp"] is None
    assert result.reason == "metadata_execution_disabled" and not scenario.calls
    assert result.credential_sha256 is None


def test_verified_report_uses_live_metadata_and_private_change_guard(scenario, monkeypatch):
    monkeypatch.setenv("WINDSURF_API_KEY", "ambient-synthetic-secret")
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "verified" and result.cleanup_confirmed
    assert set(result.report) == REPORT_FIELDS
    assert result.report["identity_verified"] is True
    assert result.report["observed_at"] == result.checked_at == 1800000000.0
    expected = account.parse_auth_status(status(), expected_api_origin=ORIGIN).fingerprint()
    assert result.report["account_fp"] == expected
    assert result.credential_sha256 == hashlib.sha256(CREDENTIAL).hexdigest()
    public = json.dumps(result.report) + repr(result)
    for private in (SUBJECT, SECRET.decode(), "private@example.test", result.credential_sha256):
        assert private not in public
    assert [call[0][1:] for call in scenario.calls] == [["auth", "status"], ["models", "list", "--format", "json"]]
    assert len(scenario.stopped) == 2
    assert not list(scenario.state.iterdir())
    detached = result.report
    detached["identity_verified"] = False
    assert result.report["identity_verified"] is True


def test_metadata_commands_share_sixty_seconds_and_two_twenty_second_reads_pass(
        scenario, monkeypatch):
    now, deadlines = [0.0], []
    capture = account._capture

    def slow_read(process, **kwargs):
        deadlines.append(kwargs["deadline"])
        now[0] += 20
        return capture(process, **kwargs)

    monkeypatch.setattr(account, "_capture", slow_read)
    result = scenario.run(popen_factory=scenario.popen, clock=lambda: now[0])
    assert result.reason == "verified" and result.report["identity_verified"]
    assert deadlines == [60, 60] and now[0] == 40
    assert len(scenario.calls) == len(scenario.stopped) == 2
    assert not list(scenario.state.iterdir())


@pytest.mark.parametrize("cleanup", [False, True])
def test_metadata_exhausted_shared_budget_cleans_or_retains_quarantine(
        scenario, monkeypatch, cleanup):
    now, deadlines = [0.0], []
    capture = account._capture

    def slow_read(process, **kwargs):
        deadlines.append(kwargs["deadline"])
        now[0] += 30
        # Only the final cleanup is uncertain in the quarantine case.
        if len(deadlines) == 2:
            scenario.flags["cleanup"] = cleanup
        return capture(process, **kwargs)

    monkeypatch.setattr(account, "_capture", slow_read)
    result = scenario.run(popen_factory=scenario.popen, clock=lambda: now[0])
    assert deadlines == [60, 60] and len(scenario.calls) == len(scenario.stopped) == 2
    assert not result.report["identity_verified"] and result.credential_sha256 is None
    assert result.cleanup_confirmed is cleanup
    assert result.reason == ("metadata_authorization_or_timeout" if cleanup
                             else "native_metadata_cleanup_unconfirmed")
    assert len(list(scenario.state.iterdir())) == (0 if cleanup else 1)


def test_metadata_setup_time_counts_against_shared_budget_before_launch(scenario, monkeypatch):
    now = [0.0]
    fresh_home = account._fresh_home

    def slow_setup(*args):
        home = fresh_home(*args)
        now[0] = 60
        return home

    monkeypatch.setattr(account, "_fresh_home", slow_setup)
    result = scenario.run(popen_factory=scenario.popen, clock=lambda: now[0])
    assert result.reason == "native_metadata_timeout" and result.cleanup_confirmed
    assert not result.report["identity_verified"] and result.credential_sha256 is None
    assert not scenario.calls and not list(scenario.state.iterdir())


@pytest.mark.parametrize("change", ["revoke", "cancel"])
def test_extended_metadata_budget_still_checks_revocation_and_cancellation(
        scenario, monkeypatch, change):
    now, reads = [0.0], []
    cancelled = threading.Event()
    capture = account._capture

    def slow_read(process, **kwargs):
        now[0] += 20
        reads.append(process)
        if len(reads) == 2:
            if change == "revoke":
                scenario.flags["authorized"] = False
            else:
                cancelled.set()
        return capture(process, **kwargs)

    monkeypatch.setattr(account, "_capture", slow_read)
    result = scenario.run(popen_factory=scenario.popen, clock=lambda: now[0],
                          cancel_event=cancelled)
    assert now[0] == 40 and len(scenario.calls) == len(scenario.stopped) == 2
    assert not result.report["identity_verified"] and result.credential_sha256 is None
    assert result.cleanup_confirmed and not list(scenario.state.iterdir())


def test_metadata_verifier_uses_same_protected_proxy_for_both_commands(
        scenario, protected, monkeypatch):
    expected = native.egress.native_environment()
    scenario.flags["expected_proxy_environment"] = expected
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-secret@untrusted.invalid")
    monkeypatch.setenv("NO_PROXY", "*")
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "verified" and result.cleanup_confirmed
    assert len(scenario.calls) == 2
    for _, kwargs, _ in scenario.calls:
        environment = kwargs["env"]
        assert environment["HTTPS_PROXY"] == native.egress.PROXY_URL
        assert all(environment.get(key) == value for key, value in expected.items())
        assert protected.config["password"] not in json.dumps(environment)
        assert "ambient-secret" not in json.dumps(environment)


@pytest.mark.parametrize("broken", ["orphan_upstream", "missing_upstream", "invalid_policy"])
def test_metadata_incomplete_egress_fails_before_any_native_command(scenario, protected, broken):
    if broken == "orphan_upstream":
        protected.policy.unlink()
    elif broken == "missing_upstream":
        protected.upstream.unlink()
    else:
        protected.policy.write_text('{"version":1,"enabled":false}')
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "native_egress_unavailable" and not result.report["identity_verified"]
    assert result.credential_sha256 is None and scenario.calls == []
    assert result.cleanup_confirmed and list(scenario.state.iterdir()) == []


def test_unproved_acquisition_profile_cannot_launch(scenario):
    scenario.flags["source_verified"] = False
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "native_subject_source_unverified"
    assert not scenario.calls and not result.report["identity_verified"]


@pytest.mark.parametrize("raw", [
    CREDENTIAL.replace(b'https://api.devin.ai', b'https://evil.test'),
    CREDENTIAL.replace(b'app.devin.ai', b'127.0.0.1'),
    CREDENTIAL.replace(b'https://server.codeium.com', b'https://169.254.169.254'),
    CREDENTIAL + b'additional_option = "unsafe"\n',
    CREDENTIAL.replace(SECRET, b'escaped\\nvalue'),
])
def test_untrusted_credential_endpoints_or_toml_reject_before_any_home_or_native_launch(scenario, raw):
    scenario.credentials.write_bytes(raw)
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason in {"native_credential_format", "native_credential_endpoints"}
    assert not scenario.calls and not list(scenario.state.iterdir())
    assert result.report["account_fp"] is None and result.credential_sha256 is None
    assert result.cleanup_confirmed


@pytest.mark.parametrize("field", ["native_version", "adapter_revision", "model_map_digest"])
def test_runtime_profile_mismatch_rejects_before_credential_read(scenario, field):
    scenario.source[field] = "9.9.9" if field == "native_version" else "0" * 64
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "account_profile_or_authorization_changed"
    assert not scenario.calls and result.credential_sha256 is None


@pytest.mark.parametrize("case,reason", [
    ("catalog", "native_model_catalog_unverified"),
    ("nonzero", "native_auth_status_rejected"),
    ("mutation", "native_account_changed_during_verification"),
    ("revocation", "metadata_authorization_revoked"),
    ("bound", "native_metadata_output_bound"),
])
def test_failure_never_produces_account_or_private_attestation(scenario, case, reason):
    if case == "catalog":
        scenario.outputs[1] = b'{"families":[]}'
    elif case == "nonzero":
        scenario.flags["returncode"] = 1
    elif case == "mutation":
        scenario.flags["mutate"] = True
    elif case == "revocation":
        scenario.flags["revoke"] = True
    else:
        scenario.outputs[0] = b"x" * (account.MAX_STATUS_BYTES + 1)
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == reason
    assert result.report["account_fp"] is None and result.credential_sha256 is None
    assert result.cleanup_confirmed and not list(scenario.state.iterdir())
    if case in {"nonzero", "revocation", "bound"}:
        assert len(scenario.calls) == 1


@pytest.mark.parametrize("phase,reason", [
    (0, "native_auth_status_rejected"), (1, "native_models_list_rejected"),
])
@pytest.mark.parametrize("exit_code", [1, 42, -15])
def test_metadata_rejection_names_only_the_failed_command(scenario, phase, reason, exit_code):
    before = scenario.credentials.read_bytes()

    def popen(*args, **kwargs):
        if len(scenario.calls) == phase:
            scenario.flags["returncode"] = exit_code
        return scenario.popen(*args, **kwargs)

    result = scenario.run(popen_factory=popen)
    assert result.reason == reason and len(scenario.calls) == phase + 1
    assert not result.report["identity_verified"] and result.report["account_fp"] is None
    assert result.credential_sha256 is None and result.cleanup_confirmed
    assert not list(scenario.state.iterdir()) and scenario.credentials.read_bytes() == before
    assert str(exit_code) not in result.reason
    assert SECRET.decode() not in result.reason and SUBJECT not in result.reason


@pytest.mark.parametrize("extra,reason", [
    (b"private-label@example.test: private-value\n", "native_status_unknown_label_sha256_"
     + hashlib.sha256(b"private-label@example.test").hexdigest()),
    (b"User ID: private-subject\n", "native_status_duplicate_label_user_id"),
])
def test_status_shape_failure_keeps_identity_closed_and_private(scenario, extra, reason):
    scenario.outputs[0] += extra
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == reason and len(scenario.calls) == 2
    assert not result.report["identity_verified"] and result.report["account_fp"] is None
    assert result.credential_sha256 is None and result.cleanup_confirmed
    assert not list(scenario.state.iterdir())
    assert "private" not in result.reason and SUBJECT not in result.reason


def test_unknown_label_diagnostic_hashes_only_the_label_and_keeps_error_static():
    label = "private-label@example.test"
    diagnostics = []
    for value in ("private-value-one", "private-value-two"):
        with pytest.raises(account.AccountError) as captured:
            account.parse_auth_status(status() + f"{label}: {value}\n".encode(),
                                      expected_api_origin=ORIGIN)
        error = captured.value
        assert error.code == str(error) == "native_status_unknown_label"
        assert label not in repr(vars(error)) and value not in repr(vars(error))
        diagnostics.append(error.diagnostic_code)
    assert diagnostics == ["native_status_unknown_label_sha256_"
                           + hashlib.sha256(label.encode()).hexdigest()] * 2


@pytest.mark.parametrize("label,suffix", [
    ("User ID", "user_id"), ("API server", "api_server"), ("Default model", "default_model"),
])
def test_duplicate_known_label_diagnostic_is_a_closed_enum(label, suffix):
    first_value = ORIGIN if label == "API server" else "private-value"
    with pytest.raises(account.AccountError) as captured:
        account.parse_auth_status(status(**{label: first_value})
                                  + f"{label}: private-second-value\n".encode(),
                                  expected_api_origin=ORIGIN)
    error = captured.value
    assert error.code == str(error) == "native_status_duplicate_label"
    assert error.diagnostic_code == "native_status_duplicate_label_" + suffix
    assert "private" not in repr(vars(error))


@pytest.mark.parametrize("diagnostic", [
    None, True, 12, b"native_status_duplicate_label_user_id", "private@example.test",
    "native_status_unknown_label_sha256_" + "a" * 63,
    "native_status_unknown_label_sha256_" + "A" * 64,
    "native_status_unknown_label_sha256_" + "a" * 64 + "\n",
    "native_status_duplicate_label_private_secret",
])
def test_account_error_sanitizes_optional_diagnostic_at_construction(diagnostic):
    error = account.AccountError("native_status_unknown_label", diagnostic_code=diagnostic)
    assert error.diagnostic_code is None
    assert error.code == str(error) == "native_status_unknown_label"


def test_collector_revalidates_diagnostic_before_emission(scenario, monkeypatch):
    error = account.AccountError("native_status_unknown_label")
    error.diagnostic_code = "private-native-output@example.test"

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(account, "_capture", reject)
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "native_status_unknown_label"
    assert not result.report["identity_verified"] and result.credential_sha256 is None
    assert result.cleanup_confirmed and not list(scenario.state.iterdir())


def test_failed_cleanup_retains_quarantined_private_directory_and_no_identity(scenario):
    scenario.flags["cleanup"] = False
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "native_metadata_cleanup_unconfirmed"
    assert not result.cleanup_confirmed and result.report["account_fp"] is None
    assert len(list(scenario.state.iterdir())) == 1
    assert len(scenario.calls) == 1


def test_cancelled_before_start_never_launches(scenario):
    event = threading.Event()
    event.set()
    result = scenario.run(popen_factory=scenario.popen, cancel_event=event)
    assert result.reason == "account_profile_or_authorization_changed"
    assert not scenario.calls


def test_model_map_digest_is_canonical_and_comes_from_config(scenario):
    original = account.model_map_digest(scenario.config)
    data = scenario.config.data
    data["request_models"].reverse()
    same = replace(scenario.config, _json=json.dumps(data))
    assert account.model_map_digest(same) == original
    data["request_models"].append("another-alias")
    assert account.model_map_digest(replace(same, _json=json.dumps(data))) != original
    with pytest.raises(account.AccountError, match="root_native_config_required"):
        account.model_map_digest(data)


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "A" * 64])
def test_source_profile_requires_explicit_pin(digest):
    with pytest.raises(account.AccountError, match="source_profile_pin_required"):
        account.SubjectSourceProfile.load("/unused", expected_sha256=digest)


def test_real_pipe_capture_is_bounded_without_launching_process():
    pipes = [os.pipe(), os.pipe()]
    try:
        os.write(pipes[0][1], b"synthetic-status")
        os.write(pipes[1][1], b"ignored-error")
        for _, writer in pipes:
            os.close(writer)
        process = types.SimpleNamespace(stdout=os.fdopen(pipes[0][0], "rb"), stderr=os.fdopen(pipes[1][0], "rb"))
        raw = account._capture(process, maximum=100, deadline=10, permitted=lambda: True,
                               cancelled=threading.Event(), clock=lambda: 1)
        assert raw == b"synthetic-status"
        process.stdout.close()
        process.stderr.close()
    finally:
        for pair in pipes:
            for descriptor in pair:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def test_group_cleanup_signals_before_reaping_and_closes_private_output(monkeypatch):
    events = []
    process = types.SimpleNamespace(pid=777, stdout=io.BytesIO(b"private"), stderr=io.BytesIO(), returncode=None)

    def wait(**kwargs):
        events.append("wait")
        process.returncode = 0

    process.wait = wait
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: events.append(sig))
    monkeypatch.setattr(account.time, "sleep", lambda seconds: None)
    assert account._stop_group(process)
    assert events == [account.signal.SIGTERM, account.signal.SIGKILL, "wait"]
    assert process.stdout.closed and process.stderr.closed


def test_foreign_process_group_never_claims_cleanup(monkeypatch):
    process = types.SimpleNamespace(pid=777, stdout=io.BytesIO(), stderr=io.BytesIO())
    monkeypatch.setattr(os, "getpgid", lambda pid: 778)
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("foreign group signalled"))
    assert not account._stop_group(process)


def source_data():
    return {"version": 1, "metadata_profile": "native-auth-status-fresh-home-v1",
            "native_version": "3000.11.92", "native_sha256": "a" * 64,
            "api_origin": ORIGIN, "collector_sha256": "b" * 64,
            "adapter_revision": "c" * 64, "model_map_digest": "d" * 64,
            "capabilities": dict.fromkeys(account.CAPABILITIES, True),
            "live_subject_source_verified": True, "proof_sha256": "e" * 64}


@pytest.mark.parametrize("change", [None, {"extra": True}, {"version": True},
    {"live_subject_source_verified": 1}, {"native_version": "latest"},
    {"api_origin": "https://SERVER.codeium.com:443"}, {"proof_sha256": ""},
    {"capabilities": {"client_tools": 1}}, {"capabilities": {"raw_model_parity": True}}])
def test_source_profile_root_and_collector_pins_are_validated(monkeypatch, change):
    value = {**source_data(), **(change or {})}
    raw = json.dumps(value).encode()
    path = Path("/root-pinned/source.json")
    guards = []
    monkeypatch.setattr(native, "_plain", lambda value: value)
    monkeypatch.setattr(native, "_root_ancestry", lambda path: guards.append(path))
    monkeypatch.setattr(native, "_read", lambda path, **kwargs: raw)
    monkeypatch.setattr(native, "_pinned", lambda record, **kwargs: guards.append(record))
    if change:
        with pytest.raises(account.AccountError):
            account.SubjectSourceProfile.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    else:
        profile = account.SubjectSourceProfile.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())
        assert profile.data == value
        assert guards[:2] == [path, path]
        assert guards[2] == {"path": str(Path(account.__file__).resolve()), "sha256": "b" * 64}


def test_profile_file_hash_change_is_rejected(monkeypatch):
    monkeypatch.setattr(native, "_plain", lambda value: value)
    monkeypatch.setattr(native, "_root_ancestry", lambda path: None)
    monkeypatch.setattr(native, "_read", lambda path, **kwargs: b"{}")
    with pytest.raises(account.AccountError, match="source_profile_changed"):
        account.SubjectSourceProfile.load(Path("/root-pinned/source.json"), expected_sha256="a" * 64)


def test_cli_default_does_not_read_stdin_or_collect(monkeypatch):
    monkeypatch.setattr(account, "_read_request", lambda *_: pytest.fail("plan read stdin"))
    monkeypatch.setattr(account, "collect_readiness", lambda *_: pytest.fail("plan collected"))
    output = io.StringIO()
    assert account.main([], stdout=output) == 0
    assert json.loads(output.getvalue())["execute"] is False


def test_direct_isolated_script_plan_is_model_free():
    result = subprocess.run([sys.executable, "-I", account.__file__, "--plan"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
        check=False)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)["native_model_calls"] is False


def registration(scenario):
    return {"config_path": "/root-pinned/config.json", "config_sha256": "a" * 64,
            "source_profile_path": "/root-pinned/source.json", "source_profile_sha256": "b" * 64,
            "runtime": scenario.runtime, "binding": asdict(scenario.binding)}


def test_private_cli_loads_pins_and_returns_attestation_without_raw_metadata(scenario, monkeypatch):
    pins = []

    def config_load(cls, path, *, expected_sha256):
        pins.append((path, expected_sha256))
        return scenario.config

    def source_load(cls, path, *, expected_sha256):
        pins.append((path, expected_sha256))
        return scenario.profile

    monkeypatch.setattr(native.NativeConfig, "load", classmethod(config_load))
    monkeypatch.setattr(account.SubjectSourceProfile, "load", classmethod(source_load))
    output = io.StringIO()
    assert account.main(["--collect-profile"],
        stdin=io.BytesIO(json.dumps(registration(scenario)).encode()), stdout=output,
        popen_factory=scenario.popen) == 0
    value = json.loads(output.getvalue())
    assert len(pins) == 2 and pins[0][1] == "a" * 64 and pins[1][1] == "b" * 64
    assert value["credential_sha256"] == hashlib.sha256(CREDENTIAL).hexdigest()
    assert set(value["report"]) == REPORT_FIELDS and value["cleanup_confirmed"]
    assert SUBJECT not in output.getvalue() and SECRET.decode() not in output.getvalue()


@pytest.mark.parametrize("payload", [b"{}", b"not JSON", b"", b"x" * (65536 + 1)])
def test_private_cli_malformed_registration_never_executes(payload, monkeypatch):
    monkeypatch.setattr(account, "collect_readiness", lambda *_: pytest.fail("invalid collected"))
    output = io.StringIO()
    assert account.main(["--collect-profile"], stdin=io.BytesIO(payload), stdout=output) == 2
    result = json.loads(output.getvalue())
    assert result["report"] is None and result["credential_sha256"] is None
    assert result["reason"] == "metadata_registration_rejected"
