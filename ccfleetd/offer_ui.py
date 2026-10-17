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
.slot-offer>div{min-width:0;overflow-wrap:anywhere;font-size:13px}
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
.ip-category.residential,.ip-category.isp{color:var(--ink);background:var(--inset)}
.offer-table tbody tr:last-child>*{border-bottom:0}.offer-table td:last-child{white-space:nowrap}
.offer-note{font-size:13px;color:var(--muted);margin:12px 0}
.alpha-badge{display:inline-flex;align-items:center;border:1px solid var(--acc-line);
border-radius:999px;padding:3px 8px;background:var(--acc-soft);color:var(--acc);
font-size:11px;font-weight:650;line-height:1.4;white-space:nowrap}
.offer-rate{font-size:16px;font-weight:650;color:var(--ink)}
.offer-breakdown{font-size:12px;min-width:180px;margin-top:6px;white-space:normal}
.offer-breakdown summary{cursor:pointer;color:var(--acc);font-weight:500}
.offer-breakdown dl{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px 14px;
margin:10px 0 4px;min-width:210px;max-width:360px;line-height:1.5}
.offer-breakdown dt,.offer-breakdown dd{margin:0}.offer-breakdown dd{text-align:right}
.offer-breakdown .offer-total{border-top:1px solid var(--rule-soft);padding-top:7px;font-weight:650}
.offer-choice .offer-breakdown{margin-top:8px}
.offer-compact{display:block;font-size:11px;font-weight:400;color:var(--muted);margin-top:4px;
line-height:1.6;overflow-wrap:anywhere}.offer-choices{margin:14px 0;display:grid;gap:10px}
.offer-choice{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;
justify-content:space-between;
padding:12px 14px;background:var(--inset);border:1px solid var(--rule-soft);border-radius:10px}
.offer-choice p{margin:0;font-size:13px}.offer-choice .offer-compact{margin-top:2px}
@media(max-width:560px){.slot-offer{gap:10px 16px}.slot-offer>div{flex:1 1 40%}}
"""


def ip_label(kind: str) -> str:
    en, zh = {"residential": ("Residential", "住宅 IP"),
              "isp": ("ISP", "ISP IP")}.get(kind, ("Not verified", "尚未核实"))
    style = kind if kind in ("residential", "isp") else "unknown"
    return f'<span class="ip-category {style}">' + label(en, zh, inline=True) + '</span>'


def alpha_label() -> str:
    return '<span class="alpha-badge">' + label("Alpha", "Alpha 内测", inline=True) + '</span>'


def rate(node: Mapping[str, Any], fallback: Optional[pricing.Price] = None) -> str:
    # A legacy selling price is never evidence of a machine's resource costs.
    price = offers.public(node.get("offer"))["price"]
    if price is None:
        return label("Pricing unavailable", "价格暂不可用", inline=True)
    shown = escape(pricing.display(price))
    return label(shown + " / month", shown + "／月", inline=True)


def breakdown(node: Mapping[str, Any]) -> str:
    """Publish only validated monthly USD quote items, never supplier/access records."""
    offer = node.get("offer")
    quote = offers.quote(offer) if isinstance(offer, offers.Offer) else None
    if quote is None:
        return ""
    rows = []

    def item(en: str, zh: str, value: pricing.Price, *, total: bool = False) -> None:
        style = ' class="offer-total"' if total else ''
        rows.append('<dt' + style + '>' + label(en, zh, inline=True) + '</dt><dd'
                    + style + '>' + escape(pricing.display(value)) + '</dd>')

    if quote.bundled:
        item("VPS + exit IP (bundled)", "VPS 与出口 IP（打包）", quote.hosting)
    else:
        item("VPS cost", "VPS 成本", quote.hosting)
        item("Exit-IP cost", "出口 IP 成本", quote.ip)
    if quote.rounding_adjustment:
        adjustment = quote.rounding_adjustment
        shown = pricing.Price(format(abs(adjustment), ".2f"), "USD")
        rows.append('<dt>' + label("Monthly rounding adjustment", "月成本舍入调整", inline=True)
                    + '</dt><dd>' + ('−' if adjustment < 0 else '+')
                    + escape(pricing.display(shown)) + '</dd>')
    item("Monthly resource cost", "每月资源成本", quote.resource)
    item("Service fee", "服务费", quote.service_fee)
    item("Total monthly price", "每月总价", quote.total, total=True)
    return ('<details class="offer-breakdown"><summary>'
            + label("Monthly price breakdown", "月费明细", inline=True)
            + '</summary><dl>' + ''.join(rows) + '</dl></details>')


def quoted_rate(node: Mapping[str, Any]) -> str:
    return '<strong class="offer-rate">' + rate(node) + '</strong>' + breakdown(node)


def _brand(value: str) -> str:
    return escape(value) if value else label("Not provided", "尚未提供", inline=True)


def details(node: Mapping[str, Any]) -> str:
    """A holder's own card, including honest unknowns for older unconfigured nodes."""
    offer = offers.public(node.get("offer"))
    cells = ((label("Host provider", "主机服务商", inline=True), _brand(offer["host_brand"])),
             (label("IP provider", "IP 服务商", inline=True), _brand(offer["ip_brand"])),
             (label("IP type", "IP 类型", inline=True), ip_label(offer["ip_type"])),
             (label("Current Alpha quote", "当前 Alpha 报价", inline=True), quoted_rate(node)))
    return '<div class="slot-offer">' + ''.join(
        '<div><b>' + title + '</b>' + value + '</div>' for title, value in cells) + '</div>' \
        + '<p class="offer-note">' + label(
            "An already agreed billing period keeps its agreed price. If the price changes, "
            "you will receive an updated quote before renewal.",
            "已经约定的账期继续按原价执行。如价格变更，续费前会提供新报价。", inline=True) + '</p>'


def compact(node: Mapping[str, Any]) -> str:
    if node.get("offer") is None:
        return ""
    offer = offers.public(node.get("offer"))
    return ('<small class="offer-compact">' + _brand(offer["host_brand"]) + ' / '
            + _brand(offer["ip_brand"]) + ' · ' + ip_label(offer["ip_type"]) + ' · '
            + label('Current Alpha quote: ', '当前 Alpha 报价：', inline=True)
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
                    + '</strong></p>' + compact(node) + breakdown(node) + '</div>'
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
    # Disabled machines are retained for operators and historical records, but
    # are no longer offered. Keep enabled machines with stale reports visible
    # so a temporary outage cannot silently disappear from the catalogue.
    nodes = [node for node in store.list_nodes() if node.get("enabled")]
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
        if not beat or beat["ts"] < now - max_age:
            state = label("Unavailable", "暂不可用", inline=True)
        elif slot["state"] == slots.FREE and slot.get("present") == 0:
            state = label("Available", "可领取", inline=True)
        elif slot["state"] in slots.HELD:
            state = label("In use", "已领取", inline=True)
        else:
            state = label("Preparing", "准备中", inline=True)
        machine = label(f"Machine {number}", f"主机 {number}", inline=True)
        if beat is not None:
            machine = f'<a href="/status#machine-{number}">' + machine + '</a>'
        rows.append('<tr><th scope="row">' + machine + '</th>'
                    '<td>' + _brand(offer["host_brand"]) + '</td><td>'
                    + _brand(offer["ip_brand"]) + '</td><td>' + ip_label(offer["ip_type"])
                    + '</td><td>' + state + '</td><td>' + quoted_rate(node) + '</td></tr>')
    heads = (("Slot machine", "槽位主机"), ("Host provider", "主机服务商"),
             ("IP provider", "IP 服务商"), ("IP type", "IP 类型"),
             ("Availability", "可用情况"), ("Monthly price", "月费"))
    return ('<section class="band" id="slot-pricing"><h2>'
            + label("Slots & pricing", "槽位与价格", inline=True) + ' ' + alpha_label() + '</h2>'
            '<p class="offer-note">' + label(
                "Alpha pricing passes through recorded VPS and exit-IP costs, plus a monthly "
                "service fee of 10% of resource cost, with a $2 minimum per slot. "
                "Costs are normalized to an average calendar month and converted to USD "
                "by the operator. Resource cost and service fee are each rounded to cents, "
                "then added. Your Claude subscription or Anthropic API usage is billed separately.",
                "Alpha 定价按已记录的 VPS 与出口 IP 成本，加收资源成本 10% 的月服务费，"
                "每个槽位的服务费最低为 2 美元。成本折算为平均自然月并由运营者换算为美元；"
                "资源成本和服务费分别舍入到美分后相加。Claude 订阅或 Anthropic API 用量另行计费。",
                inline=True) + '</p><p class="offer-note">' + label(
                "ISP and residential describe network types, not a quality ranking or a "
                "guarantee of Claude compatibility. Each customer receives a dedicated VPS "
                "and dedicated exit IP. Bundled supplier costs are shown without an "
                "invented split.",
                "ISP 与住宅表示网络类型，并非质量排名，也不保证 Claude 兼容性。每位客户使用独立 "
                "VPS 和独立出口 IP。供应商打包成本按记录展示，不虚构拆分。", inline=True) + '</p>'
            '<div class="offer-sheet" role="region" aria-label="Slot providers and prices" '
            'data-aria-label-en="Slot providers and prices" '
            'data-aria-label-zh="槽位服务商与价格" '
            'tabindex="0"><table class="offer-table"><thead><tr>'
            + ''.join('<th scope="col">' + label(en, zh, inline=True) + '</th>'
                      for en, zh in heads) + '</tr></thead><tbody>' + ''.join(rows)
            + '</tbody></table></div><p class="offer-note">' + label(
                "Available means unallocated, not a promise of eligibility. Sign in to choose an "
                "available priced slot within your allowance. Payment is agreed directly "
                "with the operator; this site does not charge or renew automatically.",
                "可领取表示槽位尚未分配，不保证你有领取权限。登录后可在额度内选择可领取的已定价"
                "槽位。付款直接与运营者约定，本站不会自动扣款或续费。", inline=True)
            + '</p><p class="offer-note">' + label(
                "Alpha is still being tested. The operator has had encouraging results with one "
                "residential slot; reliability across providers, configurations and multiple users "
                "is still being evaluated. Alpha pricing is introductory and may change for future "
                "billing periods. If pricing changes, you will receive an updated quote before "
                "renewal; an already agreed billing period retains its agreed price.",
                "Alpha 仍在测试中。运营者自用一个住宅槽位的结果令人鼓舞，但不同服务商、配置及多用户"
                "情况下的可靠性仍在评估。Alpha 价格为入门阶段定价，未来账期可能调整；如有变更，"
                "续费前会提供新报价，已经约定的账期继续按原价执行。", inline=True)
            + '</p></section>')
