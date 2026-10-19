"""Read-only Devin guidance for a holder's hosted slot.

This is connection guidance, not a native login endpoint. Readiness comes only
from the same current root-verified binding used by gateway admission. No slot
facts, browser flag, credential presence or Claude login can establish it.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Optional

from . import gateway_ui
from . import slots as slotstates
from .bilingual import bi, label
from .store import StoreError

GUIDE = "/docs/library/docs/devin.html#connect"
_HELD = {slotstates.CLAIMING, slotstates.CLAIMED, slotstates.ACTIVE}


def native_state(store: Any, cfg: Any, account: Mapping[str, Any],
                 slot: Mapping[str, Any], *, now: Optional[float] = None) -> Optional[str]:
    """Return a public-safe state; foreign, owner-node and released slots hide it."""
    owner = account.get("id")
    if (not isinstance(owner, str) or not owner or slot.get("held_by") != owner
            or slot.get("kind") != slotstates.MACHINE_SLOT or slot.get("state") not in _HELD
            or not isinstance(slot.get("id"), str) or not slot["id"]):
        return None
    moment = time.time() if now is None else now
    if slot["id"] in gateway_ui.ready_provider_slots(store, cfg, owner, [slot], now=moment):
        return "ready"
    try:
        binding = store.gateway_provider_binding(slot["id"], owner, "devin")
    except (StoreError, KeyError, TypeError, ValueError):
        binding = None
    if (binding and binding.get("owner_account_id") == owner
            and binding.get("slot_id") == slot["id"]
            and binding.get("claim_id") == slot.get("gateway_claim_id")):
        return "verification_required"
    return "setup_required"


def native_details(store: Any, cfg: Any, account: Mapping[str, Any],
                   slot: Mapping[str, Any], *, now: Optional[float] = None) -> str:
    """Render an honest next step without forms, native commands or mutation."""
    state = native_state(store, cfg, account, slot, now=now)
    if state is None:
        return ""
    titles = {
        "ready": ("Devin ready", "Devin 已就绪"),
        "verification_required": ("Verification required", "需要验证"),
        "setup_required": ("Setup required", "需要设置"),
    }
    tone = "ok" if state == "ready" else "warn"
    opening = ('<section class="devin-connection" data-provider="devin" data-native-state="'
               + state + '"><h3>Devin <span class="pill ' + tone + '">'
               + label(*titles[state], inline=True) + '</span></h3>')
    common = bi(
        '<p>Use your own Devin account on this slot. Original Claude Code and its tools '
        'run on your computer and connect through the same Fleet endpoint.</p>',
        '<p>此槽位使用你自己的 Devin 账号。原版 Claude Code 及其工具在你的电脑上运行，'
        '仍连接同一个 Fleet 地址。</p>')
    if state == "ready":
        body = bi(
            '<p>Your Devin account is verified for this slot. Open connection '
            'settings, choose Devin and this slot for a connection key, then use that '
            'key in Claude Code. This pilot uses Opus 5.5 at max effort. '
            'You do not need to install Devin CLI locally.</p>',
            '<p>当前槽位的 Devin 绑定已通过最新验证。打开连接设置，为连接密钥选择 Devin '
            '和此槽位，再在 Claude Code 中使用该密钥。试点使用 Opus 5.5，推理强度为 max。'
            '无需在本机安装 Devin CLI。</p>')
        action = ('<a class="btn" href="/account#gateway">'
                  + label("Connection settings", "连接设置", inline=True) + '</a>')
    else:
        pending = (bi(
            '<p>Your Devin login needs to be checked again. Verification is required '
            'before it can receive requests.</p>',
            '<p>当前 Devin 绑定尚未就绪，需要重新通过最新验证后才能接收请求。</p>')
            if state == "verification_required" else "")
        body = pending + bi(
            '<p>Devin setup is assisted during this pilot. The operator prepares your slot; '
            'you sign in through the official Devin CLI, then Fleet checks the account. '
            'Browser-only self-service login is not available yet. Signing in '
            'to Claude does not sign you in to Devin.</p>',
            '<p>Devin 是由运营人员启用的试点。运营人员需要先为你的槽位准备受支持的运行环境'
            '和原生登录，再由 Fleet 验证账号。目前尚不提供纯网页自助登录。登录 Claude '
            '不会同时登录 Devin。</p>')
        action = ('<a class="btn" href="' + GUIDE + '">'
                  + label("How to connect Devin", "如何连接 Devin", inline=True) + '</a>')
    return opening + common + body + '<div class="actions">' + action + '</div></section>'
