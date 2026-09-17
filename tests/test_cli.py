import re

import pytest

from ccfleetd import cli


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("CCFLEET_ADMIN_TOKEN", raising=False)
    return str(tmp_path / "fleet.db")


def test_node_lifecycle(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik", "--region", "us"]) == 0
    out = capsys.readouterr().out
    token = re.search(r"CCFLEET_NODE_TOKEN=([0-9a-f]{64})", out).group(1)
    assert "CCFLEET_NODE_ID=node-a" in out and len(token) == 64
    assert cli.main(["--db", db, "node", "pin", "node-a", "2.1.92"]) == 0
    assert cli.main(["--db", db, "node", "list"]) == 0
    listing = capsys.readouterr().out
    assert "node-a" in listing and "2.1.92" in listing and "never" in listing
    assert cli.main(["--db", db, "node", "rotate-token", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "disable", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "enable", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "remove", "node-a"]) == 0
    assert cli.main(["--db", db, "node", "remove", "node-a"]) == 2


def test_check_and_serve_guard(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik"]) == 0
    assert cli.main(["--db", db, "check"]) == 0
    assert cli.main(["--db", db, "serve"]) == 2  # no admin token configured
    assert "CCFLEET_ADMIN_TOKEN" in capsys.readouterr().err


def test_alert_test_uses_log_notifier(db):
    assert cli.main(["--db", db, "alert-test"]) == 0


def test_bad_config_exits_2(db, monkeypatch, capsys):
    monkeypatch.setenv("CCFLEET_BIND", "nonsense")
    assert cli.main(["--db", db, "node", "list"]) == 2
    assert "CCFLEET_BIND" in capsys.readouterr().err


def test_rc_expected_toggle_and_listing(db, capsys):
    assert cli.main(["--db", db, "node", "add", "node-a", "--owner", "erik"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "node", "list"]) == 0
    assert " off " in capsys.readouterr().out  # default: no Remote Control alerting

    assert cli.main(["--db", db, "node", "rc-expected", "node-a", "on"]) == 0
    assert "will alert" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "list"]) == 0
    assert " on " in capsys.readouterr().out

    assert cli.main(["--db", db, "node", "rc-expected", "node-a", "off"]) == 0
    assert "will not alert" in capsys.readouterr().out
    assert cli.main(["--db", db, "node", "rc-expected", "ghost", "on"]) == 2


def test_rc_expected_rejects_bad_state(db):
    with pytest.raises(SystemExit):
        cli.main(["--db", db, "node", "rc-expected", "node-a", "maybe"])
