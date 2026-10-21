"""Private, bilingual rendering of bounded traffic DTOs; no sampling or I/O."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape

from ccfleet_agent import network_traffic as network

from .bilingual import label


def _unknown():
    return label("Unknown", "未知", inline=True)


def _number(value):
    return f"{value:,}" if network.counter(value) else _unknown()


def _bytes(value, *, rate=False):
    valid = network.number(value, network.MAX_COUNTER) if rate else network.counter(value)
    if not valid:
        return _unknown()
    unit = "B"
    amount = value
    for suffix in ("KB", "MB", "GB", "TB", "PB"):
        if amount < 1000:
            break
        amount /= 1000
        unit = suffix
    shown = f"{amount:,.2f}".rstrip("0").rstrip(".") if amount else "0"
    return shown + " " + unit + ("/s" if rate else "")


def _date(value):
    if not network.number(value):
        return _unknown()
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _seconds(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".") if network.number(value) else _unknown()


def _metric(en, zh, value):
    return ('<div class="traffic-metric"><dt>' + label(en, zh, inline=True)
            + '</dt><dd>' + value + '</dd></div>')


def _region(identifier):
    return (' id="' + escape(identifier, quote=True) + '" data-fleet-readonly'
            if isinstance(identifier, str) and identifier else '')


def network_panel(reading, *, title, attribution="machine", region_id=None):
    reading = reading if isinstance(reading, Mapping) else {}
    state = reading.get("state")
    state = state if isinstance(state, str) else "unavailable"
    valid = (state == "ready" and all(network.counter(reading.get(key)) for key in
             ("received_bytes", "sent_bytes")) and network.number(reading.get("coverage_s"))
             and reading["coverage_s"] > 0)
    source = '<section class="traffic-panel"' + _region(region_id) + '><h3>' + title + '</h3>'
    if attribution == "shared_machine":
        scope = label("Shared machine NIC traffic · includes other slots and system activity; "
                      "not traffic attributable to your slot.",
                      "共享机器网卡流量 · 包含其他槽位与系统活动，不能归属于你的槽位。",
                      inline=True)
    elif attribution == "service":
        scope = label("Service network namespace · includes gateway, control "
                      "and other service traffic.",
                      "服务网络命名空间 · 包含网关、控制与其他服务流量。", inline=True)
    else:
        scope = label("Machine NIC traffic · includes all applications and system activity, "
                      "not only model API requests.",
                      "机器网卡流量 · 包含所有应用与系统活动，并非仅模型 API 请求。", inline=True)
    source += '<p class="traffic-meta">' + scope + '</p>'
    if valid:
        source += '<dl class="traffic-grid">' + ''.join((
            _metric("Received", "接收", _bytes(reading["received_bytes"])),
            _metric("Sent", "发送", _bytes(reading["sent_bytes"])),
            _metric("Latest receive rate", "最近接收速率",
                    _bytes(reading.get("receive_bytes_per_s"), rate=True)),
            _metric("Latest send rate", "最近发送速率",
                    _bytes(reading.get("send_bytes_per_s"), rate=True)))) + '</dl>'
        source += '<p class="traffic-meta">' + label("Measured period: ", "观测时段：", inline=True)
        source += _date(reading.get("measurement_started_at")) + ' – '
        source += _date(reading.get("measurement_through_at")) + '<br>'
        source += label("Valid interval coverage: ", "有效区间覆盖：", inline=True)
        source += _seconds(reading["coverage_s"]) + label(" seconds", " 秒", inline=True)
        source += ' · ' + label("Gaps: ", "缺口：", inline=True) + _number(reading.get("gaps"))
        source += ' · ' + label("Counter resets: ", "计数重置：", inline=True)
        source += _number(reading.get("resets")) + '</p>'
    else:
        words = {"warming": ("Collecting a baseline. Transfer totals need two valid observations.",
                              "正在建立基线，需要两次有效观测才能统计传输总量。"),
                 "stale": ("Network reading is stale. Current transfer and rates are unknown.",
                           "网络观测已过期，当前传输量与速率未知。")}
        source += '<p class="traffic-status">' + label(*words.get(state, (
            "Network measurement unavailable. Unknown is not zero.",
            "暂无网络观测；未知不是零。")), inline=True) + '</p>'
        if network.number(reading.get("observed_at")):
            source += '<p class="traffic-meta">' + label("Last observation: ", "最近观测：",
                                                        inline=True)
            source += _date(reading["observed_at"]) + '</p>'
    return source + '<p class="traffic-meta">' + label(
        "Measured intervals only. Gaps and resets are excluded. No historical or monthly "
        "traffic is inferred. Decimal units: 1 KB = 1,000 bytes.",
        "仅统计已观测区间，排除缺口与重置区间，不推算历史或月流量。十进制单位：1 KB = 1,000 字节。",
        inline=True) + '</p></section>'


def api_panel(reading, *, fleet=False, reporting=None, eligible=None, region_id=None):
    reading = reading if isinstance(reading, Mapping) else {}
    source = '<section class="traffic-panel"' + _region(region_id) + '><h3>' + label(
        "Model API usage", "模型 API 用量", inline=True) + '</h3><p class="traffic-meta">'
    source += label("Last 7 UTC dates · current bound slot relay observations. "
                    "Includes direct local and gateway API use; not account-wide quota.",
                    "最近 7 个 UTC 日期 · 当前绑定槽位的中继观测，包含本地直连与网关 API 用量，"
                    "并非账号全局额度。", inline=True)
    if fleet:
        source += ' ' + label("Across bound slots; these are not DMIT-only API totals.",
                              "汇总当前绑定槽位，不是仅经 DMIT 的 API 总量。", inline=True)
        source += ' ' + label("Reporting slots: ", "报告槽位：", inline=True)
        source += _number(reporting) + ' / ' + _number(eligible)
    source += '</p>'
    if (reading.get("state") != "ready" or reading.get("window_days") != 7
            or not all(network.counter(reading.get(key)) for key in
                       ("requests", "success", "errors"))
            or reading["success"] + reading["errors"] != reading["requests"]):
        return source + '<p class="traffic-status">' + label(
            "No fresh API observation. Current usage is unknown.",
            "暂无新鲜 API 观测，当前用量未知。", inline=True) + '</p></section>'
    source += '<dl class="traffic-grid">' + ''.join(
        _metric(en, zh, _number(reading.get(key))) for key, en, zh in (
            ("requests", "Requests", "请求"), ("success", "Completed", "已完成"),
            ("errors", "Other outcomes", "其他结果"))) + '</dl>'
    tokens = reading.get("tokens")
    tokens = tokens if isinstance(tokens, Mapping) else {}
    source += '<dl class="traffic-grid">' + ''.join(
        _metric(en, zh, _number(tokens.get(key))) for key, en, zh in (
            ("input_tokens", "Input tokens", "输入 Token"),
            ("output_tokens", "Output tokens", "输出 Token"),
            ("cache_read_input_tokens", "Cache read tokens", "缓存读取 Token"),
            ("cache_creation_input_tokens", "Cache write tokens", "缓存写入 Token"))) + '</dl>'
    source += '<p class="traffic-meta">' + label("Token response coverage: ", "Token 响应覆盖：",
                                                inline=True)
    source += _number(reading.get("usage_samples")) + ' / '
    source += _number(reading.get("usage_eligible_messages")) + ' · '
    state = reading.get("token_state")
    state = state if isinstance(state, str) else "unknown"
    source += label(*{"complete": ("Complete recorded fields", "已记录字段完整"),
                     "partial": ("Partial recorded fields", "已记录字段不完整")}.get(state, (
                         "Token measurement unavailable", "暂无 Token 观测")), inline=True)
    source += '<br>' + label("Cache field coverage: read ", "缓存字段覆盖：读取 ", inline=True)
    source += _number(reading.get("cache_read_samples")) + ' / '
    source += _number(reading.get("usage_samples")) + ' · '
    source += label("write ", "写入 ", inline=True)
    source += _number(reading.get("cache_creation_samples")) + ' / '
    source += _number(reading.get("usage_samples"))
    return source + '</p><p class="traffic-meta">' + label(
        "Missing replies or cache fields remain unknown. Cached context may be counted "
        "again on each request. Requests include failures and cancellations.",
        "缺失回复或缓存字段保持未知。缓存上下文可能在每次请求中重复计数。请求数包含失败与取消。",
        inline=True) + '</p></section>'


def slot_panel(summary, *, slot_id=None):
    summary = summary if isinstance(summary, Mapping) else {}
    region_id = "slot-traffic-" + slot_id if isinstance(slot_id, str) and slot_id else None
    return ('<div class="traffic-grid"' + _region(region_id) + '>'
            + network_panel(summary.get("network"),
            title=label("Slot machine network", "槽位机器网络", inline=True),
            attribution=summary.get("network_attribution"))
            + api_panel(summary.get("api")) + '</div>')


def controller_panel(summary, slot_readings=()):
    summary = summary if isinstance(summary, Mapping) else {}
    source = '<section id="private-traffic"><h2>' + label(
        "Network & API traffic", "网络与 API 流量", inline=True) + '</h2><div class="traffic-grid">'
    source += network_panel(summary.get("host"), title=label(
        "Controller host (DMIT)", "控制器宿主机（DMIT）", inline=True),
        region_id="traffic-controller-host")
    source += network_panel(summary.get("service"), title=label(
        "CC Fleet service container", "CC Fleet 服务容器", inline=True), attribution="service",
        region_id="traffic-controller-service")
    source += '</div><p class="traffic-meta">' + label(
        "Host and container show different network views; do not add them together.",
        "宿主机与容器是不同网络视角，请勿相加。", inline=True) + '</p>'
    source += api_panel(summary.get("api"), fleet=True, reporting=summary.get("reporting_slots"),
                        eligible=summary.get("eligible_slots"), region_id="traffic-fleet-api")
    for slot_id, name, reading in slot_readings:
        source += '<details class="traffic-panel"><summary>' + escape(str(name)) + '</summary>'
        source += slot_panel(reading, slot_id=slot_id) + '</details>'
    return source + '</section>'
