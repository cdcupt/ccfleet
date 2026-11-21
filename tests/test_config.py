import pytest

from ccfleetd.config import Config, ConfigError


def test_defaults_when_env_empty():
    cfg = Config.from_env({})
    assert (cfg.bind_host, cfg.bind_port) == ("127.0.0.1", 8110)
    assert cfg.heartbeat_max_age_s == 900
    assert cfg.telegram_enabled is False
    assert (cfg.gateway_max_queued, cfg.gateway_max_queued_per_key,
            cfg.gateway_queue_timeout_s) == (16, 8, 30)


def test_gateway_queue_settings_are_configurable_and_can_be_disabled():
    cfg = Config.from_env({"CCFLEET_GATEWAY_MAX_QUEUED": "4",
                           "CCFLEET_GATEWAY_MAX_QUEUED_PER_KEY": "2",
                           "CCFLEET_GATEWAY_QUEUE_TIMEOUT_S": "0"})
    assert (cfg.gateway_max_queued, cfg.gateway_max_queued_per_key,
            cfg.gateway_queue_timeout_s) == (4, 2, 0)


def test_low_connection_limit_keeps_existing_configuration_valid():
    assert Config.from_env({"CCFLEET_GATEWAY_MAX_CONNECTIONS": "8"}).gateway_max_connections == 8


@pytest.mark.parametrize("settings", [
    {"CCFLEET_GATEWAY_MAX_QUEUED": "0"},
    {"CCFLEET_GATEWAY_MAX_QUEUED": "65"},
    {"CCFLEET_GATEWAY_MAX_QUEUED_PER_KEY": "0"},
    {"CCFLEET_GATEWAY_MAX_QUEUED_PER_KEY": "17"},
    {"CCFLEET_GATEWAY_QUEUE_TIMEOUT_S": "-1"},
    {"CCFLEET_GATEWAY_QUEUE_TIMEOUT_S": "61"},
])
def test_gateway_queue_settings_are_bounded(settings):
    with pytest.raises(ConfigError):
        Config.from_env(settings)


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


def test_bypass_by_default_is_off_unless_asked():
    from ccfleetd.config import Config
    cfg = Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32})
    assert cfg.bypass_by_default is False


def test_bypass_by_default_accepts_the_usual_spellings():
    from ccfleetd.config import Config
    base = {"CCFLEET_ADMIN_TOKEN": "x" * 32}
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env({**base, "CCFLEET_BYPASS_BY_DEFAULT": raw}).bypass_by_default is True
    for raw in ("0", "false", "no", "off", ""):
        assert Config.from_env({**base, "CCFLEET_BYPASS_BY_DEFAULT": raw}).bypass_by_default is False


def test_bypass_by_default_rejects_a_value_it_cannot_read():
    import pytest

    from ccfleetd.config import Config, ConfigError
    # Silently treating "maybe" as false would turn prompts back on without a word.
    with pytest.raises(ConfigError, match="BYPASS_BY_DEFAULT"):
        Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_BYPASS_BY_DEFAULT": "maybe"})


def test_every_config_option_is_documented_where_operators_look():
    """A setting absent from the env example is a setting nobody finds.

    This caught CCFLEET_BYPASS_BY_DEFAULT shipping undocumented; it exists so the
    next option cannot do the same.
    """
    import pathlib
    import re

    from ccfleetd.config import ENV_PREFIX, Config

    root = pathlib.Path(__file__).resolve().parent.parent
    example = (root / "deploy" / "ccfleetd.env.example").read_text()
    guidebook = (root / "docs" / "guidebook.html").read_text()

    # Derived, not hand-listed, so a new field is covered the moment it is added.
    fields = {f.name for f in Config.__dataclass_fields__.values()}
    # bind_host/bind_port arrive together as CCFLEET_BIND; db_path is CCFLEET_DB.
    special = {"bind_host": "BIND", "bind_port": "BIND", "db_path": "DB",
               "admin_user": "ADMIN_USER", "admin_token": "ADMIN_TOKEN"}
    expected = {ENV_PREFIX + special.get(f, f.upper()) for f in fields}

    missing = sorted(v for v in expected if v not in example)
    assert not missing, f"not in deploy/ccfleetd.env.example: {missing}"

    # The security-relevant one must also reach the guidebook's reference table.
    assert ENV_PREFIX + "BYPASS_BY_DEFAULT" in guidebook
    assert re.search(r"passwordless sudo", guidebook)


def test_a_contact_address_is_optional_and_kept_as_given():
    assert Config.from_env({}).contact_email == ""
    assert Config.from_env({"CCFLEET_CONTACT_EMAIL": " help@example.com "}).contact_email \
        == "help@example.com"


@pytest.mark.parametrize("raw", [
    "not-an-address", "a@b", "a b@example.com", 'x"y@example.com', "x<y@example.com",
    "a@example.com, b@example.com", "a@b@example.com",
    "a@" + "b" * 250 + ".com",            # the shape is right, the length is not
])
def test_a_contact_address_that_is_not_one_address_is_refused(raw):
    """It goes on a public page, so it is checked rather than trusted."""
    with pytest.raises(ConfigError, match="CONTACT_EMAIL"):
        Config.from_env({"CCFLEET_CONTACT_EMAIL": raw})


@pytest.mark.parametrize("url", [
    "http://fleet.example.com", "https://name:password@fleet.example.com",
    "https://fleet.example.com/v1/messages", "https://fleet.example.com?key=secret",
    "https://fleet.example.com#fragment", "file:///tmp/service", "https://",
])
def test_gateway_refuses_insecure_or_ambiguous_public_origin(url):
    from tests.test_gateway_end_to_end import public_key

    with pytest.raises(ConfigError, match="GATEWAY"):
        Config.from_env({"CCFLEET_GATEWAY_ENABLED": "true",
                         "CCFLEET_GATEWAY_SSH_KEY_PATH": "/run/ccfleet/key",
                         "CCFLEET_GATEWAY_SSH_PUBLIC_KEY": public_key(),
                         "CCFLEET_PUBLIC_URL": url})


@pytest.mark.parametrize("url", ["https://fleet.example.com", "http://127.0.0.1:8110"])
def test_gateway_accepts_https_or_local_test_origin(url):
    from tests.test_gateway_end_to_end import public_key

    assert Config.from_env({"CCFLEET_GATEWAY_ENABLED": "true",
                            "CCFLEET_GATEWAY_SSH_KEY_PATH": "/run/ccfleet/key",
                            "CCFLEET_GATEWAY_SSH_PUBLIC_KEY": public_key(),
                            "CCFLEET_PUBLIC_URL": url}).gateway_ready
