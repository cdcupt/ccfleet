"""Sending an email, over Resend's API and the standard library.

Erik, 2026-09-24: outage emails for the holders who ask for them, sent from a
distinct address on the domain the operator's Resend account already has
verified (its free tier is one domain) with one send-only key. Cloudflare in
front of Resend refuses urllib's own User-Agent (error 1010), so every request
names itself; with that, a send from the server's container was accepted and
landed in an inbox.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
SEND_TIMEOUT_S = 20
USER_AGENT = f"ccfleet/{__version__} (+https://github.com/cdcupt/ccfleet)"


@dataclass(frozen=True)
class Email:
    """One message to one person: plain text, and the same as HTML."""

    to: str
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    def send(self, email: Email) -> bool:  # pragma: no cover - interface
        ...


class NoMailer:
    """Emails off: no key, or no sender, configured."""

    def send(self, email: Email) -> bool:
        return False


def masked(address: str) -> str:
    """An address as a log line may say it: "a…@example.com"."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}…@{domain}" if domain else "…"


class ResendMailer:
    def __init__(self, key: str, sender: str,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self._key, self._sender, self._open = key, sender, opener

    def send(self, email: Email) -> bool:
        """True once Resend has accepted it; False, logged, when not."""
        body = json.dumps({"from": self._sender, "to": [email.to], "subject": email.subject,
                           "text": email.text, "html": email.html}).encode("utf-8")
        request = urllib.request.Request(
            RESEND_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with self._open(request, timeout=SEND_TIMEOUT_S) as reply:
                accepted = 200 <= reply.status < 300
        except urllib.error.HTTPError as exc:
            log.warning("email to %s refused: HTTP %s", masked(email.to), exc.code)
            return False
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("email to %s not sent: %s", masked(email.to), exc.__class__.__name__)
            return False
        if not accepted:
            log.warning("email to %s not accepted", masked(email.to))
        return accepted


def build_mailer(cfg: Config) -> Mailer:
    """Resend when the operator configured it; otherwise nothing is sent."""
    if cfg.resend_api_key and cfg.email_from:
        return ResendMailer(cfg.resend_api_key, cfg.email_from)
    return NoMailer()
