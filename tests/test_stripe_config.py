"""Temporary synthetic credentials only; activation never contacts a provider."""
from __future__ import annotations

import os
import stat
import urllib.request
from types import SimpleNamespace

import pytest

from ccfleetd import stripe_billing, stripe_checkout, stripe_config
from ccfleetd.config import Config, ConfigError

KEY = 'rk_test_SYNTHETIC_NOT_A_REAL_KEY'
SECRET = 'whsec_SYNTHETIC_NOT_A_REAL_SECRET'


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('configuration must never perform network I/O')
    monkeypatch.setattr(urllib.request, 'build_opener', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)


def config(**overrides):
    return SimpleNamespace(**{'stripe_topup_enabled': True, 'usd_slot_credit_enabled': True,
        'stripe_product_approved': True, 'stripe_mode': 'test',
        'stripe_expected_account_id': 'acct_SyntheticFleet',
        'stripe_api_key_file': '/not-read/synthetic-api-key',
        'stripe_webhook_secret_file': '/not-read/synthetic-webhook-secret',
        'stripe_sandbox_database': True, 'public_url': 'https://fleet-sandbox.example',
        'public_url_aliases': (), **overrides})


def credentials(tmp_path, *, key=KEY, secret=SECRET):
    key_file, secret_file = tmp_path / 'synthetic-key', tmp_path / 'synthetic-signing'
    key_file.write_text(key, encoding='ascii')
    secret_file.write_text(secret, encoding='ascii')
    key_file.chmod(0o600)
    secret_file.chmod(0o400)
    return config(stripe_api_key_file=str(key_file), stripe_webhook_secret_file=str(secret_file))


def forbid_file_open(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('disabled or invalid configuration must not open credentials')
    monkeypatch.setattr(stripe_config.os, 'open', denied)


def test_default_config_is_off_without_any_file_or_network_work(monkeypatch):
    forbid_file_open(monkeypatch)
    assert stripe_config.validate(Config()) is False
    assert stripe_config.build_billing(Config(), object()) is None
    # Off ignores unused incomplete metadata, even if a broken path was saved.
    assert stripe_config.build_billing(config(stripe_topup_enabled=False,
        stripe_api_key_file=object(), stripe_mode='unknown'), object()) is None


def test_pure_validation_does_not_open_credentials(monkeypatch):
    forbid_file_open(monkeypatch)
    assert stripe_config.validate(config()) is True


@pytest.mark.parametrize('changes', [
    {'stripe_topup_enabled': 1}, {'stripe_topup_enabled': 'true'},
    {'usd_slot_credit_enabled': False}, {'usd_slot_credit_enabled': 1},
    {'stripe_product_approved': False}, {'stripe_product_approved': 1},
    {'stripe_mode': ''}, {'stripe_mode': 'automatic'}, {'stripe_mode': []},
    {'stripe_expected_account_id': ''}, {'stripe_expected_account_id': 'acct_'},
    {'stripe_expected_account_id': 'acct_bad/path'},
    {'stripe_sandbox_database': False}, {'stripe_sandbox_database': 1},
    {'stripe_api_key_file': ''}, {'stripe_api_key_file': 'relative/path'},
    {'stripe_webhook_secret_file': '/path\x00bad'}, {'stripe_api_key_file': '/' + 'x' * 4096},
    {'public_url': ''}, {'public_url': 'http://fleet-sandbox.example'},
    {'public_url': 'http://localhost'}, {'public_url': 'https://fleet-sandbox.example:8443'},
    {'public_url': 'https://user:password@fleet-sandbox.example'},
    {'public_url': 'https://fleet-sandbox.example/account'},
    {'public_url_aliases': []}, {'public_url_aliases': ('http://localhost',)},
])
def test_invalid_activation_metadata_is_rejected_before_file_open(changes, monkeypatch):
    forbid_file_open(monkeypatch)
    with pytest.raises(ConfigError):
        stripe_config.build_billing(config(**changes), object())


@pytest.mark.parametrize('host', ['ccfleet.io', 'www.ccfleet.io', 'ccfleet.daichenlab.com',
                                 'CCFLEET.IO'])
def test_sandbox_cannot_point_canonical_or_alias_to_production(host, monkeypatch):
    forbid_file_open(monkeypatch)
    for changes in ({'public_url': 'https://' + host},
                    {'public_url_aliases': ('https://' + host,)},
                    {'public_url_aliases': ('https://safe-sandbox.example', 'https://' + host)}):
        with pytest.raises(ConfigError, match='production Fleet origins'):
            stripe_config.build_billing(config(**changes), object())


def test_enabled_factory_constructs_sandbox_billing_without_client_or_store_calls(tmp_path):
    cfg = credentials(tmp_path)

    def no_provider_call(*args, **kwargs):
        pytest.fail('construction must not call the injected provider transport')

    store = object()
    billing = stripe_config.build_billing(cfg, store, opener=no_provider_call)
    assert isinstance(billing, stripe_billing.Billing) and billing.store is store
    assert billing.settings.mode == 'test' and billing.settings.livemode is False
    assert billing.settings.public_origin == 'https://fleet-sandbox.example'
    assert billing.settings.expected_account_id == 'acct_SyntheticFleet'
    assert billing.settings.api_key == KEY and billing.settings.webhook_secret == SECRET
    assert KEY not in repr(billing.settings) and SECRET not in repr(billing.settings)


def test_live_factory_requires_approval_not_sandbox_and_matching_key_mode(tmp_path):
    cfg = credentials(tmp_path, key='rk_live_SYNTHETIC_NOT_A_REAL_KEY')
    cfg.stripe_mode, cfg.stripe_sandbox_database, cfg.public_url = 'live', False, 'https://ccfleet.io'
    billing = stripe_config.build_billing(cfg, object())
    assert billing.settings.livemode is True
    cfg.stripe_sandbox_database = True
    with pytest.raises(ConfigError, match='sandbox database'):
        stripe_config.build_billing(cfg, object())
    cfg.stripe_sandbox_database, cfg.stripe_product_approved = False, False
    with pytest.raises(ConfigError, match='explicit approval'):
        stripe_config.build_billing(cfg, object())


@pytest.mark.parametrize('key,secret', [('pk_test_SYNTHETIC', SECRET),
    ('sk_live_SYNTHETIC_NOT_A_REAL_KEY', SECRET), (KEY, 'invalid-SYNTHETIC-secret')])
def test_credential_diagnostics_never_include_key_value_or_file_path(tmp_path, key, secret):
    cfg = credentials(tmp_path, key=key, secret=secret)
    with pytest.raises(ConfigError) as caught:
        stripe_config.build_billing(cfg, object())
    assert key not in str(caught.value) and secret not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize('ending', ['', '\n', '\r\n'])
def test_private_regular_credential_may_have_one_terminal_newline(tmp_path, ending):
    path = tmp_path / 'synthetic'
    path.write_bytes((KEY + ending).encode())
    path.chmod(0o600)
    assert stripe_config._secret(str(path)) == KEY


@pytest.mark.parametrize('value', [b'', b'\n', b'secret\n\n', b' secret', b'secret ',
    b'secret\r', b'sec\x00ret', b'sec\x7fret', b'\xff', b'x' * 513])
def test_invalid_credential_file_contents_are_bounded_and_never_echoed(tmp_path, value):
    path = tmp_path / 'synthetic-sensitive-path'
    path.write_bytes(value)
    path.chmod(0o600)
    with pytest.raises(ConfigError) as caught:
        stripe_config._secret(str(path))
    assert str(path) not in str(caught.value)
    assert 'secret-sensitive' not in str(caught.value)


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o604, 0o660, 0o601, 0o700 | 0o010])
def test_group_or_world_permissions_are_never_accepted(tmp_path, mode):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(mode)
    with pytest.raises(ConfigError):
        stripe_config._secret(str(path))


def test_symlink_hardlink_directory_and_fifo_cannot_be_credentials(tmp_path):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(0o600)
    symlink, hardlink = tmp_path / 'symlink', tmp_path / 'hardlink'
    symlink.symlink_to(path)
    os.link(path, hardlink)
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo, 0o600)
    for invalid in (symlink, hardlink, path, tmp_path, fifo, tmp_path / 'missing'):
        with pytest.raises(ConfigError):
            stripe_config._secret(str(invalid))


def test_descriptor_flags_protect_leaf_and_inheritance_and_close_on_success(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(0o600)
    real_open, real_close = os.open, os.close
    descriptors, closed = [], []

    def opened(value, flags):
        assert flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC and flags & os.O_NONBLOCK
        descriptor = real_open(value, flags)
        assert os.get_inheritable(descriptor) is False
        descriptors.append(descriptor)
        return descriptor

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(stripe_config.os, 'open', opened)
    monkeypatch.setattr(stripe_config.os, 'close', close)
    assert stripe_config._secret(str(path)) == KEY
    assert len(descriptors) == 1 and closed == descriptors
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize('change', [{'st_uid': 999_999}, {'st_nlink': 2},
    {'st_mode': stat.S_IFIFO | 0o600}, {'st_size': 513}, {'st_size': 0}])
def test_untrusted_descriptor_metadata_is_refused_and_closed(tmp_path, monkeypatch, change):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(0o600)
    original, real_close = path.stat(), os.close
    info = {key: getattr(original, key) for key in ('st_mode', 'st_uid', 'st_nlink', 'st_size')}
    info.update(change)
    monkeypatch.setattr(stripe_config.os, 'fstat', lambda descriptor: SimpleNamespace(**info))
    closed = []

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(stripe_config.os, 'close', close)
    with pytest.raises(ConfigError):
        stripe_config._secret(str(path))
    assert len(closed) == 1


def test_file_growth_after_stat_is_still_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(0o600)
    observed = []

    def read(descriptor, maximum):
        observed.append(maximum)
        return b'x' * maximum

    monkeypatch.setattr(stripe_config.os, 'read', read)
    with pytest.raises(ConfigError):
        stripe_config._secret(str(path))
    assert observed == [513]


def test_unsupported_leaf_protection_does_not_fall_back_to_unsafe_open(tmp_path, monkeypatch):
    monkeypatch.delattr(stripe_config.os, 'O_NOFOLLOW')
    forbid_file_open(monkeypatch)
    with pytest.raises(ConfigError, match='unsupported'):
        stripe_config._secret(str(tmp_path / 'synthetic'))


def test_reader_accepts_root_owned_metadata_without_requiring_current_uid(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic'
    path.write_text(KEY)
    path.chmod(0o600)
    info = path.stat()
    monkeypatch.setattr(stripe_config.os, 'fstat', lambda descriptor: SimpleNamespace(
        st_mode=info.st_mode, st_uid=0, st_nlink=1, st_size=info.st_size))
    assert stripe_config._secret(str(path)) == KEY


def test_no_raw_environment_credentials_are_used(monkeypatch):
    monkeypatch.setenv('CCFLEET_STRIPE_API_KEY', KEY)
    monkeypatch.setenv('CCFLEET_STRIPE_WEBHOOK_SECRET', SECRET)
    forbid_file_open(monkeypatch)
    assert stripe_config.build_billing(config(stripe_topup_enabled=False), object()) is None
    with pytest.raises(ConfigError, match='protected file paths'):
        stripe_config.build_billing(config(stripe_api_key_file=''), object())


def test_invalid_transport_still_has_only_safe_configuration_error(tmp_path):
    cfg = credentials(tmp_path)
    with pytest.raises(ConfigError, match='could not be confirmed'):
        stripe_config.build_billing(cfg, object(), opener='not callable')
    assert isinstance(stripe_checkout.Settings('test', 'acct_Fake', KEY, SECRET),
                      stripe_checkout.Settings)
