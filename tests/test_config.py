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
