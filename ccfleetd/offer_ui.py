"""Public slot offers: explicit labels and prices, never machine access details."""
from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from html import escape
from typing import Any, Optional
from urllib.parse import urlencode

from . import offers, pricing, slots, status
from .bilingual import label

PAGE_SIZE = 20

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
.ip-category.residential{color:#60269c;background:#f4edff;border-color:#cdb5e8}
.ip-category.isp{color:#1253a0;background:#edf5ff;border-color:#a9c9ec}
.ip-category.datacenter{color:#374151;background:#f3f4f6;border-color:#cbd5e1}
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
.slot-filters{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:16px 0}
.slot-filters>label{display:grid;gap:6px;flex:1 1 150px;min-width:0;margin:0}
.slot-filters input,.slot-filters select{width:100%;min-width:0;min-height:42px;
padding:9px 12px;border:1px solid var(--rule);border-radius:9px;
background:var(--panel);color:var(--ink);font:inherit}
.slot-filters input:focus-visible,.slot-filters select:focus-visible{
outline:2px solid var(--acc);outline-offset:2px}
.slot-filters .filter-search{flex:2 1 240px}.slot-filter-actions{display:flex;gap:12px;
align-items:center;padding-bottom:1px}.slot-browser-pages{display:flex;flex-wrap:wrap;
align-items:center;justify-content:space-between;gap:12px;margin-top:16px}
.slot-browser-pages p{margin:0}.slot-browser-pages nav{display:flex;gap:12px}
.offer-examples{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));
gap:14px;margin:16px 0}.offer-example{padding:18px;border:1px solid var(--rule-soft);
border-radius:12px;background:var(--panel);min-width:0}.offer-example h3{font-size:16px;
margin:12px 0 6px}.offer-example p{margin:8px 0}.offer-example .offer-rate{font-size:22px}
.offer-example-brands{margin:12px 0;font-size:12px;line-height:1.6}
.offer-example-brands dt{color:var(--muted)}.offer-example-brands dd{margin:0 0 6px;
overflow-wrap:anywhere}.offer-policy{margin:18px 0;font-size:13px}
.offer-policy>summary{cursor:pointer;color:var(--acc);font-weight:600}
.slot-browser .offer-table form{margin:0}.slot-browser .offer-table td{padding-block:10px}
@media(max-width:560px){.slot-filters>label,.slot-filters .filter-search{flex-basis:100%}
.slot-offer{gap:10px 16px}.slot-offer>div{flex:1 1 40%}}
"""


def ip_label(kind: str) -> str:
    en, zh = {"residential": ("Residential", "住宅 IP"),
              "isp": ("ISP", "ISP IP"),
              "datacenter": ("Datacenter", "机房 IP")}.get(kind, ("Not verified", "尚未核实"))
    style = kind if kind in ("residential", "isp", "datacenter") else "unknown"
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


def _browse_option(value: str, en: str, zh: str, selected: bool) -> str:
    # Options need native text, not the bilingual spans used in ordinary labels.
    return ('<option value="' + escape(value) + '" data-copy-en="' + escape(en)
            + '" data-copy-zh="' + escape(zh) + '"' + (' selected' if selected else '')
            + '>' + escape(en) + '</option>')


def _browse_params(query: Mapping[str, str], brands: set[str]) -> dict[str, str]:
    def text(key: str, limit: int) -> str:
        value = query.get(key, "")
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            return ""
        value = value.strip()
        return value[:limit] if key == "q" else value if len(value) <= limit else ""

    search, brand, kind, page = text("q", 80), text("brand", 48), text("type", 16), text("page", 7)
    return {"q": search, "brand": brand if brand in brands else "",
            "type": kind if kind in ("residential", "isp", "datacenter") else "",
            "page": page if re.fullmatch(r"[0-9]{1,6}", page) else "1"}


def browse(store: Any, account: Mapping[str, Any], csrf: str, now: float,
           max_age: float, query: Mapping[str, str], can_claim: bool = True) -> str:
    """An eligible inventory page, separate from the holder's own slot controls.

    Search and pagination use approved public metadata only. These are choices,
    not reservations: the existing claim action rechecks eligibility atomically.
    """
    return _browser(available(store, account, now, max_age), query,
                    path="/account/available", csrf=csrf, can_claim=can_claim)


def _browser(nodes: list[Mapping[str, Any]], query: Mapping[str, str], *, path: str,
             csrf: str = "", can_claim: bool = False,
             availability: Optional[Mapping[str, tuple[str, str]]] = None,
             reported: Optional[set[str]] = None) -> str:
    """Shared bounded filters and rows; only the authenticated wrapper can claim."""
    public_inventory = availability is not None
    nodes = sorted(nodes, key=lambda node: status.public_machine_number(node) or 0)
    brands = {value for node in nodes for key, value in offers.public(node.get("offer")).items()
              if key in ("host_brand", "ip_brand") and value}
    selected = _browse_params(query, brands)
    search = selected["q"].casefold()
    matching = []
    for node in nodes:
        public = offers.public(node.get("offer"))
        number = status.public_machine_number(node)
        searchable = " ".join((public["host_brand"], public["ip_brand"], public["ip_type"],
                               f"Machine {number}", f"主机 {number}",
                               {"residential": "住宅", "datacenter": "机房"}
                               .get(public["ip_type"], ""))).casefold()
        if (search and search not in searchable
                or selected["brand"] and selected["brand"] not in (
                    public["host_brand"], public["ip_brand"])
                or selected["type"] and selected["type"] != public["ip_type"]):
            continue
        matching.append(node)
    total = len(matching)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(int(selected["page"]), pages))
    first = (page - 1) * PAGE_SIZE
    shown = matching[first:first + PAGE_SIZE]
    filters = ('<form class="slot-filters" method="get" action="' + path + '">'
               '<label class="filter-search">' + label("Search", "搜索", inline=True)
               + '<input type="search" name="q" maxlength="80" value="'
               + escape(selected["q"]) + '" placeholder="Provider or Machine number" '
               'data-placeholder-en="Provider or Machine number" '
               'data-placeholder-zh="服务商或主机编号"></label><label>'
               + label("Provider", "服务商", inline=True) + '<select name="brand">'
               + _browse_option("", "All providers", "全部服务商", not selected["brand"])
               + ''.join(_browse_option(brand, brand, brand, selected["brand"] == brand)
                         for brand in sorted(brands, key=lambda brand: (brand.casefold(), brand)))
               + '</select></label><label>' + label("IP type", "IP 类型", inline=True)
               + '<select name="type">'
               + ''.join(_browse_option(value, en, zh, selected["type"] == value)
                         for value, en, zh in (("", "All network types", "全部网络类型"),
                                               ("residential", "Residential", "住宅 IP"),
                                               ("isp", "ISP", "ISP IP"),
                                               ("datacenter", "Datacenter", "机房 IP")))
               + '</select></label><div class="slot-filter-actions"><button type="submit">'
               + label("Search", "搜索", inline=True) + '</button>'
               '<a href="' + path + '">' + label("Reset", "重置", inline=True)
               + '</a></div></form>')
    rows = []
    for node in shown:
        public = offers.public(node.get("offer"))
        number = status.public_machine_number(node)
        machine = label(f"Machine {number}", f"主机 {number}", inline=True)
        action = ('<td><form method="post" action="/account/claim">'
                  '<input type="hidden" name="csrf" value="' + escape(csrf) + '">'
                  f'<input type="hidden" name="machine_number" value="{number}">'
                  '<button class="primary" type="submit">'
                  + label("Claim this slot", "领取此槽位", inline=True)
                  + '</button></form></td>') if can_claim else ''
        if reported is None or node["id"] in reported:
            machine = '<a href="/status#machine-' + str(number) + '">' + machine + '</a>'
        state = ('<td>' + label(*availability[node["id"]], inline=True) + '</td>'
                 if public_inventory else '')
        rows.append('<tr><th scope="row">' + machine + '</th><td>'
                    + _brand(public["host_brand"]) + '</td><td>'
                    + _brand(public["ip_brand"]) + '</td><td>' + ip_label(public["ip_type"])
                    + '</td>' + state + '<td>' + quoted_rate(node) + '</td>' + action + '</tr>')
    heads = [("Slot machine", "槽位主机"), ("Host provider", "主机服务商"),
             ("IP provider", "IP 服务商"), ("IP type", "IP 类型"),
             ("Current Alpha quote", "当前 Alpha 报价")]
    if public_inventory:
        heads.insert(4, ("Availability", "可用情况"))
    if can_claim:
        heads.append(("Action", "操作"))
    region_en, region_zh = (("Slot catalogue", "槽位目录") if public_inventory else
                            ("Available slot machines", "可领取槽位主机"))
    table = ('<div class="offer-sheet" role="region" aria-label="' + region_en + '" '
             'data-aria-label-en="' + region_en + '" data-aria-label-zh="' + region_zh
             + '" tabindex="0"><table class="offer-table">'
             '<thead><tr>' + ''.join('<th scope="col">' + label(en, zh, inline=True)
                                     + '</th>' for en, zh in heads)
             + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else (
                 '<p class="offer-note">' + label(
                     "No machines match these filters." if public_inventory else
                     "No available machines match these filters.",
                     "没有符合筛选条件的主机。" if public_inventory else
                     "没有符合筛选条件的可领取主机。", inline=True) + '</p>')

    def page_link(target: int, en: str, zh: str) -> str:
        parameters = {key: value for key, value in selected.items() if key != "page" and value}
        parameters["page"] = str(target)
        return '<a class="btn" href="' + escape(path + '?' + urlencode(parameters)) \
            + '">' + label(en, zh, inline=True) + '</a>'

    navigation = (page_link(page - 1, "Previous", "上一页") if page > 1 else '')
    navigation += (page_link(page + 1, "Next", "下一页") if page < pages else '')
    count = label(f"Showing {first + 1 if total else 0}–{first + len(shown)} of {total} "
                  + ("matching machines." if public_inventory else "matching available machines."),
                  f"共 {total} 台" + ("主机" if public_inventory else "可领取主机")
                  + "符合条件，当前显示第 "
                  f"{first + 1 if total else 0}–{first + len(shown)} 台。", inline=True)
    pagination = ('<div class="slot-browser-pages"><p class="offer-note">'
                  + label(f"Page {page} of {pages}", f"第 {page}／{pages} 页", inline=True)
                  + '</p><nav aria-label="Inventory pages" '
                  'data-aria-label-en="Inventory pages" data-aria-label-zh="主机列表分页">'
                  + navigation + '</nav></div>')
    return ('<section class="slot-browser">' + filters + '<p class="offer-note">'
            + count + '</p>' + table + pagination + '</section>')


def _public_nodes(store: Any) -> list[Mapping[str, Any]]:
    declared = {slot["node_id"] for slot in store.list_slots()}
    return [node for node in store.list_nodes()
            if node.get("enabled") and node["id"] in declared
            and status.public_machine_number(node) is not None]


def _pricing_policy() -> str:
    """The same complete pricing and Alpha disclosures on examples and inventory."""
    paragraphs = (
        ("Alpha pricing passes through recorded VPS and exit-IP costs, plus a monthly "
         "service fee of 10% of resource cost, with a $2 minimum per slot. "
         "Costs are normalized to an average calendar month and converted to USD "
         "by the operator. Resource cost and service fee are each rounded to cents, "
         "then added. Your Claude or Devin subscription and extra usage are billed separately.",
         "Alpha 定价按已记录的 VPS 与出口 IP 成本，加收资源成本 10% 的月服务费，"
         "每个槽位的服务费最低为 2 美元。成本折算为平均自然月并由运营者换算为美元；"
         "资源成本和服务费分别舍入到美分后相加。Claude 或 Devin 订阅及额外用量另行计费。"),
        ("Annual costs divide by 12; 30-day costs multiply by 365/360. Missing or "
         "unconverted costs leave pricing unavailable. A normalization rounding line "
         "reconciles any difference between rounded components and resource cost.",
         "年付成本除以 12，30 天成本乘以 365/360。成本缺失或尚未换算时，价格暂不可用。"
         "如单项舍入与资源成本合计存在差异，折算舍入行会说明差额。"),
        ("ISP, residential and datacenter describe network types, not a quality ranking or a "
         "guarantee of Claude compatibility. Each customer receives a dedicated VPS "
         "and dedicated exit IP. All types use the same pricing formula. "
         "Bundled supplier costs are shown without an invented split.",
         "ISP、住宅与机房表示网络类型，并非质量排名，也不保证 Claude 兼容性。每位客户使用独立 "
         "VPS 和独立出口 IP。所有类型采用同一定价公式。供应商打包成本按记录展示，不虚构拆分。"),
        ("Available means unallocated, not a promise of eligibility. Sign in to choose an "
         "available priced slot within your allowance. Payment is agreed directly "
         "with the operator; this site does not charge or renew automatically.",
         "可领取表示槽位尚未分配，不保证你有领取权限。登录后可在额度内选择可领取的已定价"
         "槽位。付款直接与运营者约定，本站不会自动扣款或续费。"),
        ("Alpha is still being tested. The operator has had encouraging results with one "
         "residential slot; reliability across providers, configurations and multiple users "
         "is still being evaluated. Alpha pricing is introductory and may change for future "
         "billing periods. If pricing changes, you will receive an updated quote before "
         "renewal; an already agreed billing period retains its agreed price.",
         "Alpha 仍在测试中。运营者自用一个住宅槽位的结果令人鼓舞，但不同服务商、配置及多用户"
         "情况下的可靠性仍在评估。Alpha 价格为入门阶段定价，未来账期可能调整；如有变更，"
         "续费前会提供新报价，已经约定的账期继续按原价执行。"),
    )
    return ('<details class="offer-policy"><summary>'
            + label("Alpha pricing & service limits", "Alpha 定价与服务说明", inline=True)
            + '</summary>' + ''.join('<p class="offer-note">' + label(en, zh, inline=True)
                                    + '</p>' for en, zh in paragraphs) + '</details>')


def public_browse(store: Any, now: float, max_age: float, query: Mapping[str, str]) -> str:
    """Public inventory is metadata only; it never reads holders or accepts claims."""
    nodes = _public_nodes(store)
    latest = store.latest_heartbeats()
    declared = {slot["node_id"]: slot for slot in store.list_slots()}
    states = {}
    for node in nodes:
        slot, beat = declared[node["id"]], latest.get(node["id"])
        if not beat or beat["ts"] < now - max_age:
            state = ("Unavailable", "暂不可用")
        elif slot["state"] == slots.FREE and slot.get("present") == 0:
            state = ("Available", "可领取")
        elif slot["state"] in slots.HELD:
            state = ("In use", "已领取")
        else:
            state = ("Preparing", "准备中")
        states[node["id"]] = state
    return (_browser(nodes, query, path="/slots", availability=states, reported=set(latest))
            + '<p class="offer-note"><a class="btn primary" href="/account/available">'
            + label("Sign in to choose a slot", "登录并选择槽位", inline=True) + '</a></p>'
            + _pricing_policy())


def catalogue(store: Any, now: float, max_age: float) -> str:
    """At most three recorded example configurations; never a homepage inventory."""
    nodes = sorted(_public_nodes(store), key=lambda node: status.public_machine_number(node) or 0)
    if not nodes:
        return ""
    distinct = []
    seen = set()
    for node in nodes:
        offer = node.get("offer")
        value = offers.quote(offer)
        if value is None:
            continue
        key = (offer.host_brand, offer.ip_brand, offer.ip_type, value.hosting.amount,
               value.ip.amount, value.resource.amount, value.total.amount, value.bundled)
        if key not in seen:
            seen.add(key)
            distinct.append(node)
    def by_price(node):
        return Decimal(offers.quote(node["offer"]).total.amount), status.public_machine_number(node)

    distinct.sort(key=by_price)
    selected = []
    for kind in ("residential", "isp", "datacenter"):
        representative = next((node for node in distinct if node["offer"].ip_type == kind), None)
        if representative is not None:
            selected.append(representative)
    selected.extend(node for node in distinct if node not in selected)
    cards = []
    for node in sorted(selected[:3], key=by_price):
        offer = offers.public(node.get("offer"))
        number = status.public_machine_number(node)
        cards.append('<article class="offer-example">' + ip_label(offer["ip_type"])
                     + '<h3>' + label("Example configuration", "配置示例", inline=True) + '</h3>'
                     '<p class="offer-note">'
                     + label(f"Recorded on Machine {number}", f"记录来自主机 {number}", inline=True)
                     + '</p><dl class="offer-example-brands"><dt>'
                     + label("Host provider", "主机服务商", inline=True) + '</dt><dd>'
                     + _brand(offer["host_brand"]) + '</dd><dt>'
                     + label("IP provider", "IP 服务商", inline=True) + '</dt><dd>'
                     + _brand(offer["ip_brand"]) + '</dd></dl>' + quoted_rate(node) + '</article>')
    examples = ('<div class="offer-examples">' + ''.join(cards) + '</div>') if cards else (
        '<p class="offer-note">' + label(
            "Pricing unavailable until recorded costs support example quotes.",
            "已记录成本支持示例报价前，价格暂不可用。", inline=True) + '</p>')
    return ('<section class="band" id="slot-pricing"><h2>'
            + label("Slots & pricing", "槽位与价格", inline=True) + ' ' + alpha_label() + '</h2>'
            '<p class="offer-note">' + label(
                "A few examples from recorded configurations. These are current Alpha quotes, "
                "not a promise that a matching slot is available. Browse the catalogue for "
                "all providers, prices and availability.",
                "以下为已记录配置的部分示例，展示当前 Alpha 报价，不保证同类槽位可领取。"
                "完整服务商、价格与可用情况请查看槽位目录。", inline=True) + '</p>'
            + examples + '<p><a class="btn primary" href="/slots">'
            + label("View all slots", "查看全部槽位", inline=True) + '</a></p>'
            + _pricing_policy() + '</section>')
