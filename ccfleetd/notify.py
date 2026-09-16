"""Alert delivery: log always, Telegram when configured."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Callable, Optional

from .config import Config

log = logging.getLogger("ccfleetd.notify")

Opener = Callable[..., Any]
TELEGRAM_API = "https://api.telegram.org"
LEVEL_ICON = {"critical": "\U0001f534", "warn": "\U0001f7e0", "ok": "✅"}


class Notifier:
    def send(self, text: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError


class LogNotifier(Notifier):
    def send(self, text: str) -> bool:
        log.info("ALERT %s", text)
        return True


class TelegramNotifier(Notifier):
    """Send a plain-text message through the Bot API. Failures are logged, never raised."""

    def __init__(self, bot_token: str, chat_id: str, opener: Optional[Opener] = None,
                 timeout: float = 10.0) -> None:
        self._url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    def send(self, text: str) -> bool:
        body = json.dumps({"chat_id": self._chat_id, "text": text,
                           "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            log.warning("telegram send failed: HTTP %s", exc.code)
            return False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("telegram send failed: %s", exc.__class__.__name__)
            return False
        if status != 200:
            log.warning("telegram send failed: HTTP %s", status)
            return False
        return True


class MultiNotifier(Notifier):
    def __init__(self, targets: list[Notifier]) -> None:
        self._targets = list(targets)

    def send(self, text: str) -> bool:
        results = [t.send(text) for t in self._targets]
        return all(results)


def build_notifier(cfg: Config, opener: Optional[Opener] = None) -> Notifier:
    targets: list[Notifier] = [LogNotifier()]
    if cfg.telegram_enabled:
        targets.append(TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, opener))
    return MultiNotifier(targets)


def format_event(event: str, alert: Mapping[str, Any], node: Mapping[str, Any],
                 public_url: str = "") -> str:
    """One-line human message for an alert transition ("opened" or "closed")."""
    label = f"{node.get('id')} ({node.get('owner')})"
    if event == "closed":
        text = f"{LEVEL_ICON['ok']} resolved on {label}: {alert['rule']}"
    else:
        icon = LEVEL_ICON.get(alert["level"], LEVEL_ICON["warn"])
        text = f"{icon} {alert['level'].upper()} on {label}: {alert['rule']} - {alert['message']}"
    if public_url:
        text += f"\n{public_url}/"
    return text
