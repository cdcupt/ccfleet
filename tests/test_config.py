import pytest

from ccfleetd.config import Config, ConfigError


def test_defaults_when_env_empty():
    cfg = Config.from_env({})
    assert (cfg.bind_host, cfg.bind_port) == ("127.0.0.1", 8110)
    assert cfg.heartbeat_max_age_s == 900
    assert cfg.telegram_enabled is False


def test_env_overrides_and_bind_parsing():
    cfg = Config.from_env({"CCFLEET_BIND": "0.0.0.0:9000", "CCFLEET_DISK_WARN_PCT": "70",
                           "CCFLEET_PUBLIC_URL": "https://fleet.example/",
                           "CCFLEET_TELEGRAM_BOT_TOKEN": "t", "CCFLEET_TELEGRAM_CHAT_ID": "1"})
    assert (cfg.bind_host, cfg.bind_port) == ("0.0.0.0", 9000)
    assert cfg.disk_warn_pct == 70
    assert cfg.public_url == "https://fleet.example"
    assert cfg.telegram_enabled is True


@pytest.mark.parametrize("env", [
    {"CCFLEET_DISK_WARN_PCT": "abc"},
    {"CCFLEET_CHECK_INTERVAL_S": "1"},
    {"CCFLEET_BIND": "nocolon"},
    {"CCFLEET_BIND": "127.0.0.1:70000"},
    {"CCFLEET_DISK_WARN_PCT": "96", "CCFLEET_DISK_CRIT_PCT": "95"},
])
def test_invalid_values_raise(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_require_admin_token():
    with pytest.raises(ConfigError):
        Config.from_env({"CCFLEET_ADMIN_TOKEN": "short"}).require_admin_token()
    Config.from_env({"CCFLEET_ADMIN_TOKEN": "a" * 16}).require_admin_token()
