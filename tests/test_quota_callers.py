"""Who asks for the usage windows, and with what: an owner's node reads its own
Claude Code's config, and a slot passes on the read its holder asked for."""
from __future__ import annotations

from ccfleet_agent import agent

from . import test_agent, test_agent_account_binding
from .test_agent import _owner_cfg
from .test_agent_account_binding import MINE, NOW, Slot, signed_in_as

claude_dir = test_agent.claude_dir                  # fixtures, by name
home = test_agent_account_binding.home


def test_an_owner_node_reads_its_own_config_dir(claude_dir, tmp_path, monkeypatch):
    cfg = _owner_cfg(tmp_path, claude_dir)
    seen = {}

    def summary(state, *args, **kwargs):
        seen.update(kwargs)
        return None, None
    monkeypatch.setattr(agent, "quota_summary", summary)
    monkeypatch.setattr(agent, "build_payload", lambda *a, **k: {"node_id": "node-a"})
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (503, "down"))
    agent.run_cycle(cfg, {})
    assert seen["config_dir"] == cfg.claude_config_dir


def test_a_slot_passes_on_the_read_its_holder_asked_for(home, monkeypatch):
    signed_in_as(home, MINE)
    seen = {}

    def summary(state, *args, **kwargs):
        seen.update(kwargs)
        return None, None
    monkeypatch.setattr(agent, "quota_summary", summary)
    agent.slot_facts({"refresh_quota": True, "quota_wanted_at": NOW - 30}, Slot(home), now=NOW)
    assert seen["wanted_at"] == NOW - 30
    assert seen["config_dir"] == home / ".claude"
