"""Turns rule findings into alert transitions and drives the periodic check loop."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from . import rules
from .config import Config
from .notify import Notifier, format_event
from .store import Store

log = logging.getLogger("ccfleetd.monitor")

Clock = Callable[[], float]
PRUNE_EVERY_S = 3600


class Monitor:
    def __init__(self, store: Store, cfg: Config, notifier: Notifier,
                 clock: Optional[Clock] = None) -> None:
        self._store = store
        self._cfg = cfg
        self._notifier = notifier
        self._clock = clock or time.time
        self._last_prune = 0.0

    def check_node(self, node: dict[str, Any], now: Optional[float] = None) -> list[dict[str, Any]]:
        """Evaluate one node and reconcile its open alerts. Returns transition events."""
        now = self._clock() if now is None else now
        recent = self._store.recent_heartbeats(node["id"], limit=2)
        latest = recent[0] if recent else None
        previous = recent[1] if len(recent) > 1 else None
        findings = rules.evaluate(node, latest, previous, now, self._cfg)
        return self._reconcile(node, findings, now)

    def check_all(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = self._clock() if now is None else now
        events: list[dict[str, Any]] = []
        for node in self._store.list_nodes():
            if node["enabled"]:
                events.extend(self.check_node(node, now))
        self._maybe_prune(now)
        return events

    def _reconcile(self, node: dict[str, Any], findings: tuple[rules.Finding, ...],
                   now: float) -> list[dict[str, Any]]:
        open_by_rule = {a["rule"]: a for a in self._store.open_alerts(node["id"])}
        wanted = {f.rule: f for f in findings}
        events: list[dict[str, Any]] = []
        for rule_name, finding in wanted.items():
            current = open_by_rule.get(rule_name)
            if current is not None and current["level"] == finding.level:
                continue
            if current is not None:
                self._store.close_alert(current["id"], now)
            alert_id = self._store.open_alert(node["id"], rule_name, finding.level,
                                              finding.message, now)
            alert = {"id": alert_id, "node_id": node["id"], "rule": rule_name,
                     "level": finding.level, "message": finding.message, "opened_at": now}
            events.append({"event": "opened", "alert": alert})
        for rule_name, alert in open_by_rule.items():
            if rule_name not in wanted:
                self._store.close_alert(alert["id"], now)
                events.append({"event": "closed", "alert": alert})
        for event in events:
            self._notifier.send(format_event(event["event"], event["alert"], node,
                                             self._cfg.public_url))
        return events

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = now
        removed = self._store.prune_heartbeats(now - self._cfg.retention_days * 86400)
        if removed:
            log.info("pruned %d old heartbeats", removed)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_all()
            except Exception:  # noqa: BLE001 - keep the loop alive, but log the traceback
                log.exception("periodic check failed")
            stop.wait(self._cfg.check_interval_s)
