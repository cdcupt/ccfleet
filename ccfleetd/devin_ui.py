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
    optional = state == "setup_required" and slot.get("state") == slotstates.ACTIVE
    section_class = "devin-connection devin-optional" if optional else "devin-connection"
    opening = ('<section class="' + section_class + '" data-provider="devin" data-native-state="'
               + state + '">')
    guide = ('<a href="' + GUIDE + '">'
             + label("Devin setup guide", "Devin 设置指南", inline=True) + '</a>')
    privacy = ('<a href="/privacy">'
               + label("Privacy details", "隐私说明", inline=True) + '</a>')
    details = ('<details class="provider-details"><summary>'
               + label("Details and privacy", "详情与隐私", inline=True) + '</summary>'
               + bi(
                   '<p>Opus 5.5 · max effort. No local Devin CLI installation is needed. '
                   'Claude Code and its tools run on your computer.</p>'
                   '<p>Sent context and replies may be saved on the slot and read by '
                   'its administrators. They may remain until the slot is cleared.</p>',
                   '<p>Opus 5.5 · max 推理强度。无需在本机安装 Devin CLI。'
                   'Claude Code 及其工具在你的电脑上运行。</p>'
                   '<p>发送的上下文和回复可能保存在槽位上，主机管理员可以读取。'
                   '这些记录可能保留到槽位清理时。</p>')
               + '<p>' + guide + ' · ' + privacy + '</p></details>')
    if state == "ready":
        body = ('<h3><span class="pill ok">'
                + label("Devin ready", "Devin 已就绪", inline=True) + '</span></h3>'
                + bi(
                    '<p>Devin is connected. Use this slot from Claude Code on your computer.</p>',
                    '<p>Devin 已连接。在你的电脑上打开 Claude Code，即可使用此槽位。</p>')
                + details)
    elif state == "verification_required":
        body = ('<h3><span class="pill warn">'
                + label("Devin needs a check", "Devin 需要检查", inline=True) + '</span></h3>'
                + bi(
                    '<p>Devin requests are paused while the operator checks this slot.</p>',
                    '<p>Devin 请求已暂停，等待运营人员检查此槽位。</p>')
                + details)
    else:
        setup = (bi(
            '<p>Devin setup is assisted during this pilot. Ask the operator to enable '
            'it for this slot. Website sign-in is not available yet.</p>',
            '<p>Devin 试点需要运营人员协助启用。请联系运营人员设置此槽位，'
            '目前尚不支持在网页上登录 Devin。</p>')
            + '<p>' + guide + ' · ' + privacy + '</p>')
        if optional:
            body = ('<details class="provider-optional"><summary>'
                    + '<span class="provider-add-icon" aria-hidden="true">+</span>'
                    + '<span class="provider-add-label">'
                    + label("Add Devin (optional)", "添加 Devin（可选）", inline=True)
                    + '</span><span class="provider-add-arrow" aria-hidden="true">›</span>'
                    + '</summary><div class="provider-add-body">' + setup + '</div></details>')
        else:
            body = ('<h3>' + label("Set up Devin", "设置 Devin", inline=True)
                    + '</h3>' + setup)
    return opening + body + '</section>'
