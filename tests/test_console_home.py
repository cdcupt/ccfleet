"""Where the console's own links go.

The mark at the top left goes home, to the product's front page, as it does on
every other page; it used to reload the console. And "back to the fleet" is the
console, not the bare address, which became the front page.
"""

from __future__ import annotations

import re

import pytest

from ccfleetd.config import Config
from ccfleetd.render import CONSOLE_PATH, render_add_result, render_dashboard, render_token_result

ONE_SITE = Config(admin_token="x" * 32, public_url="https://fleet.example.com")
TWO_SITES = Config(admin_token="x" * 32, public_url="https://fleet.example.com",
                   admin_host="admin.fleet.example.com")


def pages(cfg):
    return {"dashboard": render_dashboard([], [], 3_000_000.0, cfg),
            "node added": render_add_result("node-a", "f" * 64, cfg, owner="erik"),
            "device token": render_token_result("node-a", "sk-ant-oat01-" + "x" * 20, cfg),
            "nothing to show": render_token_result("node-a", "", cfg)}


def brand(page):
    return re.findall(r'<a class="brand" href="([^"]*)"', page)


@pytest.mark.parametrize("cfg,home", [(ONE_SITE, "/"), (TWO_SITES, "https://fleet.example.com/")])
def test_the_mark_goes_to_the_front_page(cfg, home):
    for name, page in pages(cfg).items():
        assert brand(page) == [home], name


@pytest.mark.parametrize("cfg", [ONE_SITE, TWO_SITES])
def test_back_to_the_fleet_is_the_console(cfg):
    for name, page in pages(cfg).items():
        if name == "dashboard":
            continue
        backs = re.findall(r'<a class="back" href="([^"]*)">', page)
        assert backs == [CONSOLE_PATH], name
