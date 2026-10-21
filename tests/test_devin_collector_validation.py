"""Collector-only validation optimization; no native/provider execution."""
import io
import json

import pytest

from ccfleet_agent import devin_account as account
from ccfleet_agent import devin_native as native
from tests.test_devin_account import registration
from tests.test_devin_account import scenario as account_scenario

scenario = account_scenario


def test_public_model_map_digest_keeps_its_validation_contract(scenario, monkeypatch):
    calls = []

    def validate(self):
        calls.append(self)
        return scenario.home, scenario.state

    monkeypatch.setattr(native.NativeConfig, "validate", validate)
    assert account.model_map_digest(scenario.config) == scenario.runtime["model_map_digest"]
    assert calls == [scenario.config]

    def rejected(self):
        raise native.NativeError("policy_changed")

    monkeypatch.setattr(native.NativeConfig, "validate", rejected)
    with pytest.raises(native.NativeError, match="policy_changed"):
        account.model_map_digest(scenario.config)


def test_actual_collector_entrypoint_still_validates_on_every_authorization(scenario, monkeypatch):
    validations = []
    authorizations = []
    collect = account.collect_readiness

    def validate(self):
        validations.append(self)
        return scenario.home, scenario.state

    def load_config(cls, path, *, expected_sha256):
        scenario.config.validate()
        return scenario.config

    def collect_with_observed_authorization(*args, **options):
        authorize = options["authorize"]

        def authorized(binding):
            before = len(validations)
            allowed = authorize(binding)
            assert validations[before:] == [scenario.config]
            authorizations.append(binding)
            return allowed

        return collect(*args, **{**options, "authorize": authorized})

    monkeypatch.setattr(native.NativeConfig, "validate", validate)
    monkeypatch.setattr(native.NativeConfig, "load", classmethod(load_config))
    monkeypatch.setattr(account, "collect_readiness", collect_with_observed_authorization)
    monkeypatch.setattr(account.SubjectSourceProfile, "load", classmethod(
        lambda cls, path, **kwargs: scenario.profile))
    output = io.StringIO()
    assert account.main(["--collect-profile"], stdin=io.BytesIO(json.dumps(
        registration(scenario)).encode()), stdout=output, popen_factory=scenario.popen) == 0
    value = json.loads(output.getvalue())
    assert value["reason"] == "verified" and value["cleanup_confirmed"]
    assert authorizations and all(binding == scenario.binding for binding in authorizations)
    assert len(scenario.calls) == 2


def test_revocation_during_capture_still_fails_and_cleans_without_replay(scenario, monkeypatch):
    capture = account._capture

    def revoked(process, **options):
        assert options["permitted"]() is True
        scenario.flags["authorized"] = False
        return capture(process, **options)

    monkeypatch.setattr(account, "_capture", revoked)
    result = scenario.run(popen_factory=scenario.popen)
    assert result.reason == "metadata_authorization_or_timeout"
    assert result.report["account_fp"] is None and result.credential_sha256 is None
    assert len(scenario.calls) == 1 and result.cleanup_confirmed
    assert list(scenario.state.iterdir()) == []
