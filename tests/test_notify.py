import io
import json
import urllib.error

from ccfleetd.config import Config
from ccfleetd.notify import (
    LogNotifier,
    MultiNotifier,
    TelegramNotifier,
    build_notifier,
    format_event,
)


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_posts_json_to_bot_endpoint():
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return FakeResponse(b"{}")

    ok = TelegramNotifier("123:abc", "42", opener=opener).send("hello")
    assert ok is True
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert seen["body"] == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True}
    assert seen["timeout"] == 10.0


def test_telegram_failures_are_swallowed():
    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

    def url_error(req, timeout):
        raise urllib.error.URLError("dns")

    assert TelegramNotifier("t", "c", opener=http_error).send("x") is False
    assert TelegramNotifier("t", "c", opener=url_error).send("x") is False


def test_multi_and_build_notifier():
    assert MultiNotifier([LogNotifier(), LogNotifier()]).send("x") is True
    plain = build_notifier(Config.from_env({}))
    assert len(plain._targets) == 1
    with_tg = build_notifier(Config.from_env({"CCFLEET_TELEGRAM_BOT_TOKEN": "t",
                                              "CCFLEET_TELEGRAM_CHAT_ID": "c"}))
    assert len(with_tg._targets) == 2


def test_format_event():
    node = {"id": "node-a", "owner": "erik"}
    alert = {"rule": "disk_high", "level": "critical", "message": "disk 97% used"}
    opened = format_event("opened", alert, node, "https://fleet.example")
    assert "CRITICAL on node-a (erik): disk_high - disk 97% used" in opened
    assert opened.endswith("https://fleet.example/")
    closed = format_event("closed", alert, node)
    assert closed.startswith("✅ resolved on node-a (erik): disk_high")
