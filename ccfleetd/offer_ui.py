"""Public slot offers: explicit labels and prices, never machine access details."""
from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any, Optional

from . import offers, pricing, slots, status
from .bilingual import label

CSS = """
.slot-offer{display:flex;flex-wrap:wrap;gap:8px 20px;margin:12px 0 16px;
padding:12px 14px;border:1px solid var(--rule-soft);border-radius:10px;background:var(--inset)}
.slot-offer>span{min-width:0;overflow-wrap:anywhere;font-size:13px}
.slot-offer b{display:block;font-size:11px;color:var(--muted);font-weight:550;margin-bottom:3px}
.offer-sheet{overflow:auto;border:1px solid var(--rule-soft);border-radius:12px}
.offer-table{width:100%;min-width:660px;border-collapse:collapse;font-size:13px}
.offer-table th,.offer-table td{text-align:left;padding:12px 14px;
border-bottom:1px solid var(--rule-soft);
vertical-align:middle;overflow-wrap:anywhere}
.offer-table thead th{font-size:11px;color:var(--muted);font-weight:600}
.offer-table tbody th{text-transform:none;letter-spacing:normal;font-weight:600}
.ip-category{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
font-weight:600;white-space:nowrap;background:var(--inset);border:1px solid var(--rule-soft)}
.ip-category.residential{color:var(--ok);background:var(--ok-bg)}
.ip-category.isp{color:var(--acc);background:var(--acc-soft)}
.offer-table tbody tr:last-child>*{border-bottom:0}.offer-table td:last-child{white-space:nowrap}
.offer-note{font-size:13px;color:var(--muted);margin:12px 0}
.offer-compact{display:block;font-size:11px;font-weight:400;color:var(--muted);margin-top:4px;
line-height:1.6;overflow-wrap:anywhere}.offer-choices{margin:14px 0;display:grid;gap:10px}
.offer-choice{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;
justify-content:space-between;
padding:12px 14px;background:var(--inset);border:1px solid var(--rule-soft);border-radius:10px}
.offer-choice p{margin:0;font-size:13px}.offer-choice .offer-compact{margin-top:2px}
@media(max-width:560px){.slot-offer{gap:10px 16px}.slot-offer>span{flex:1 1 40%}}
"""


def ip_label(kind: str) -> str:
    en, zh = {"residential": ("Residential", "住宅 IP"),
              "isp": ("ISP", "ISP IP")}.get(kind, ("Not verified", "尚未核实"))
    style = kind if kind in ("residential", "isp") else "unknown"
    return f'<span class="ip-category {style}">' + label(en, zh, inline=True) + '</span>'


def rate(node: Mapping[str, Any], fallback: Optional[pricing.Price] = None) -> str:
    configured = node.get("offer") is not None
    price = offers.public(node.get("offer"))["price"] if configured else fallback
    if price is None:
        return label("Price pending", "价格待确认", inline=True)
    shown = escape(pricing.display(price))
    return label(shown + " / month", shown + "／月", inline=True)


def _brand(value: str) -> str:
    return escape(value) if value else label("Not provided", "尚未提供", inline=True)


def details(node: Mapping[str, Any]) -> str:
    """A holder's own card, including honest unknowns for older unconfigured nodes."""
    offer = offers.public(node.get("offer"))
    cells = ((label("Host provider", "主机服务商", inline=True), _brand(offer["host_brand"])),
             (label("IP provider", "IP 服务商", inline=True), _brand(offer["ip_brand"])),
             (label("IP type", "IP 类型", inline=True), ip_label(offer["ip_type"])),
             (label("Monthly price", "月费", inline=True), rate(node)))
    return '<div class="slot-offer">' + ''.join(
        '<span><b>' + title + '</b>' + value + '</span>' for title, value in cells) + '</div>'


def compact(node: Mapping[str, Any]) -> str:
    if node.get("offer") is None:
        return ""
    offer = offers.public(node.get("offer"))
    return ('<small class="offer-compact">' + _brand(offer["host_brand"]) + ' / '
            + _brand(offer["ip_brand"]) + ' · ' + ip_label(offer["ip_type"]) + ' · '
            + rate(node) + '</small>')


def available(store: Any, account: Mapping[str, Any], now: float,
              max_age: float) -> list[Mapping[str, Any]]:
    """Only likely eligible choices; the store rechecks claim eligibility atomically."""
    free = {s["node_id"] for s in store.list_slots(kind=slots.MACHINE_SLOT)
            if s["state"] == slots.FREE and s.get("present") == 0}
    latest = store.latest_heartbeats()
    result = []
    for node in store.list_nodes():
        heartbeat = latest.get(node["id"])
        if (node["id"] in free and node.get("enabled") is not False
                and node.get("capacity", 0) > 0
                and node.get("reserved_for") in (None, account["id"])
                and status.public_machine_number(node) is not None
                and heartbeat is not None and heartbeat["ts"] >= now - max_age
                and offers.public(node.get("offer"))["price"] is not None):
            result.append(node)
    return result


def choices(store: Any, account: Mapping[str, Any], csrf: str, now: float,
            max_age: float) -> str:
    rows = []
    for node in available(store, account, now, max_age):
        number = status.public_machine_number(node)
        rows.append('<div class="offer-choice"><div><p><strong>'
                    + label(f"Machine {number}", f"主机 {number}", inline=True)
                    + '</strong></p>' + compact(node) + '</div>'
                    '<form method="post" action="/account/claim">'
                    f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
                    f'<input type="hidden" name="machine_number" value="{number}">'
                    '<button class="primary" type="submit">'
                    + label("Claim this slot", "领取此槽位", inline=True)
                    + '</button></form></div>')
    if not rows:
        return '<p class="offer-note">' + label(
            "No priced slot is ready for your account yet. Check back or contact the operator.",
            "暂时没有已定价且可供你领取的槽位，请稍后查看或联系运营者。", inline=True) + '</p>'
    return '<div class="offer-choices">' + ''.join(rows) + '</div>'


def catalogue(store: Any, now: float, max_age: float) -> str:
    """Anonymous machine numbers only; no holders, nicknames, IPs or endpoints."""
    nodes = store.list_nodes()
    if not any(node.get("offer") is not None for node in nodes):
        return ""
    latest = store.latest_heartbeats()
    declared = {slot["node_id"]: slot for slot in store.list_slots()}
    rows = []
    for node in sorted(nodes, key=lambda n: status.public_machine_number(n) or 0):
        number = status.public_machine_number(node)
        slot = declared.get(node["id"])
        if number is None or slot is None:
            continue
        offer = offers.public(node.get("offer"))
        beat = latest.get(node["id"])
        if node.get("enabled") is False or not beat or beat["ts"] < now - max_age:
            state = label("Unavailable", "暂不可用", inline=True)
        elif slot["state"] == slots.FREE and slot.get("present") == 0:
            state = label("Free", "空闲", inline=True)
        elif slot["state"] in slots.HELD:
            state = label("In use", "已领取", inline=True)
        else:
            state = label("Preparing", "准备中", inline=True)
        rows.append('<tr><th scope="row"><a href="/status#machine-' + str(number) + '">'
                    + label(f"Machine {number}", f"主机 {number}", inline=True) + '</a></th>'
                    '<td>' + _brand(offer["host_brand"]) + '</td><td>'
                    + _brand(offer["ip_brand"]) + '</td><td>' + ip_label(offer["ip_type"])
                    + '</td><td>' + state + '</td><td>' + rate(node) + '</td></tr>')
    heads = (("Slot machine", "槽位主机"), ("Host provider", "主机服务商"),
             ("IP provider", "IP 服务商"), ("IP type", "IP 类型"),
             ("Availability", "可用情况"), ("Monthly price", "月费"))
    return ('<section class="band" id="slot-pricing"><h2>'
            + label("Slots & pricing", "槽位与价格", inline=True) + '</h2>'
            '<p class="offer-note">' + label(
                "Residential is our premium network category; ISP is our standard category. "
                "The label describes the supplied IP service, not a guaranteed quality score. "
                "Rates are monthly hosting plus IP cost, with a 10% markup. Prices stay in "
                "their stated currencies; your Claude subscription is separate.",
                "住宅 IP 为高级网络类别，ISP 为标准类别。标签描述供应的 IP 服务类型，"
                "并非保证性的质量评分。月费按主机和 IP 成本加 10% 计算，并保留所标币种；"
                "Claude 订阅另计。", inline=True) + '</p>'
            '<div class="offer-sheet" role="region" aria-label="Slot providers and prices" '
            'data-aria-label-en="Slot providers and prices" '
            'data-aria-label-zh="槽位服务商与价格" '
            'tabindex="0"><table class="offer-table"><thead><tr>'
            + ''.join('<th scope="col">' + label(en, zh, inline=True) + '</th>'
                      for en, zh in heads) + '</tr></thead><tbody>' + ''.join(rows)
            + '</tbody></table></div><p class="offer-note">' + label(
                "Free means empty, not a promise of eligibility. Sign in to choose an "
                "available priced slot within your allowance. Payment is agreed directly "
                "with the operator; this site does not charge automatically.",
                "空闲表示槽位为空，不保证你有领取权限。登录后可在额度内选择可领取的已定价"
                "槽位。付款直接与运营者约定，本站不会自动扣款。", inline=True)
            + '</p></section>')
