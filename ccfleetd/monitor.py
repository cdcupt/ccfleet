"""Turns rule findings into alert transitions and drives the periodic check loop."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from . import claude_versions, rules
from . import slots as slotstates
from .config import Config
from .notify import Notifier, format_event
from .store import Store

log = logging.getLogger("ccfleetd.monitor")

Clock = Callable[[], float]
PRUNE_EVERY_S = 3600
# A sign-in is a person at a keyboard: minutes, not hours. Past this the row is
# removed, which also stops a stale verification code sitting in the database.
LOGIN_MAX_AGE_S = 15 * 60


class Monitor:
    def __init__(self, store: Store, cfg: Config, notifier: Notifier,
                 clock: Optional[Clock] = None,
                 channel_fetcher: Optional[claude_versions.Fetcher] = None) -> None:
        self._store = store
        # None reads no release channel: the tests, and every command but
        # `serve`, never touch the network.
        self._channel_fetcher = channel_fetcher
        self._cfg = cfg
        self._notifier = notifier
        self._clock = clock or time.time
        self._last_prune = 0.0
        # One lock serialises "read heartbeats -> evaluate -> reconcile alerts" so the
        # periodic loop and concurrent heartbeat handlers never interleave on a node.
        self._lock = threading.RLock()

    def record_heartbeat(self, node: dict[str, Any], payload: dict[str, Any],
                         now: Optional[float] = None) -> list[dict[str, Any]]:
        """Store a heartbeat and evaluate the node atomically with respect to other checks."""
        now = self._clock() if now is None else now
        with self._lock:
            self._store.insert_heartbeat(node["id"], now, payload)
            return self._check_node_locked(node, now)

    def check_node(self, node: dict[str, Any], now: Optional[float] = None) -> list[dict[str, Any]]:
        """Evaluate one node and reconcile its open alerts. Returns transition events."""
        now = self._clock() if now is None else now
        with self._lock:
            return self._check_node_locked(node, now)

    def _check_node_locked(self, node: dict[str, Any], now: float) -> list[dict[str, Any]]:
        recent = self._store.recent_heartbeats(node["id"], limit=2)
        latest = recent[0] if recent else None
        previous = recent[1] if len(recent) > 1 else None
        # One account, one node is a fleet-wide rule, so each node is judged
        # against where every account is live: its own alert opens as soon as
        # it reports, and the other place's at that place's next check.
        every_slot = self._store.list_slots()
        places = rules.account_places(self._store.list_nodes(), self._store.latest_heartbeats(),
                                      every_slot, now, self._cfg)
        # A machine's own slots: an owner slot is a record, with nothing on the
        # machine's side to judge.
        findings = rules.evaluate(node, latest, previous, now, self._cfg,
                                  self._store.list_slots(node_id=node["id"],
                                                         kind=slotstates.MACHINE_SLOT), places,
                                  rules.place_names(every_slot))
        return self._reconcile(node, findings, now)

    def check_all(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = self._clock() if now is None else now
        events: list[dict[str, Any]] = []
        self._expire_claims(now)
        for node in self._store.list_nodes():
            if node["enabled"]:
                events.extend(self.check_node(node, now))
        self._expire_logins(now)
        self._expire_updates(now)
        self._refresh_channels(now)
        self._maybe_prune(now)
        return events

    def _expire_claims(self, now: float) -> None:
        """Give up on provisioning that never finished.

        A machine that went quiet mid-claim would otherwise hold that slot, and
        the claimant's allowance, in `claiming` forever. The slot goes on to be
        wiped rather than freed: whatever was half-made on the machine has to be
        cleared before anybody else is handed it.
        """
        stalled = self._store.expire_claims(older_than=now - slotstates.CLAIM_TIMEOUT_S)
        if stalled:
            log.warning("gave up on %d claim(s) still provisioning after %d min: %s",
                        len(stalled), slotstates.CLAIM_TIMEOUT_S // 60, ", ".join(stalled))

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

    def _expire_logins(self, now: float) -> None:
        """Drop sign-ins nobody finished.

        A node that dies mid-login, or an owner who closes the tab, would
        otherwise leave a row that keeps telling every agent run there is a login
        to drive — and keeps a verification code in the database.
        """
        dropped = self._store.expire_logins(now - LOGIN_MAX_AGE_S)
        if dropped:
            log.info("expired %d unfinished sign-in(s)", dropped)

    def _expire_updates(self, now: float) -> None:
        """An update nobody answered is said to have failed, so the page stops
        saying "updating"; an answered one leaves the page in time."""
        changed = self._store.expire_claude_updates(now, LOGIN_MAX_AGE_S)
        if changed:
            log.info("aged %d Claude Code update(s)", changed)

    def _refresh_channels(self, now: float) -> None:
        """Read Anthropic's release channels, about once an hour. Here in the
        periodic loop and never on a heartbeat: a slow download site must not
        slow a node's reply."""
        if self._channel_fetcher is None:
            return
        record = claude_versions.refresh(self._store.get_channel_versions(), now,
                                         self._channel_fetcher)
        if record is not None:
            self._store.set_channel_versions(record, now=now)

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
