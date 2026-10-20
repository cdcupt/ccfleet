"""The public status page, /status: whether ccfleet is up, now and over 90 days.

Anybody may read it, signed in or not. Each machine has a persistent neutral
public number; node ids, rented slot names and holder information stay private.
Only an authenticated operator sees the actual machine name. A compact machine
availability sheet complements the aggregate history and transfer panels.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from html import escape
from typing import Any, Optional

from . import offer_ui, performance, status
from . import slots as slotstates
from .bilingual import bi, label
from .config import Config
from .usersite import Viewer, _shell

SITE_NAME = "Website and account pages"
MACHINES_NAME = "Slot machines"
BANNERS = {status.GREEN: "No reported machine outage", status.YELLOW: "Partial outage",
           status.RED: "Major outage"}
BANNERS_ZH = {status.GREEN: "未收到主机故障报告", status.YELLOW: "部分机器异常",
              status.RED: "全部监测机器离线"}
COMPONENT_ZH = {SITE_NAME: "网站与账号页面", MACHINES_NAME: "槽位主机"}
WORDS_ZH = {status.GREEN: "正常", status.YELLOW: "性能或容量异常", status.RED: "离线"}
#: The front page's pill, in the colours of the site's other pills (render.CSS).
PILL_CLASS = {status.GREEN: "ok", status.YELLOW: "warn", status.RED: "critical"}
#: A day's colour, as the class its square in the bar takes.
DAY_CLASS = {status.GREEN: "g", status.YELLOW: "y", status.RED: "r", status.NO_DATA: "n"}
#: The page comes back for itself as often as the minutes behind it are counted.
REFRESH = '<meta http-equiv="refresh" content="60">'

STATUS_CSS = """
/* The status page: a banner saying it in a line, then each part of the service
   with its last ninety days, a square a day. */
.st-banner{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:18px 22px;
margin:0 0 18px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--panel)}
.st-banner strong{font-size:19px;font-weight:720;letter-spacing:-.01em}
.st-banner.green{background:var(--ok-bg);border-color:var(--ok-line)}
.st-banner.yellow{background:var(--warn-bg);border-color:var(--warn-line)}
.st-banner.red{background:var(--bad-bg);border-color:var(--bad-line)}
.st-banner .st-dot{width:11px;height:11px}
.st-since{color:var(--muted);font-size:14.5px}
.st-overall{margin-left:auto;color:var(--muted);font-size:14px}
.st-list{padding:2px 22px}
.st-comp{padding:18px 0 16px;border-bottom:1px solid var(--rule-soft)}
.st-comp:last-child{border-bottom:0}
.st-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin:0 0 12px}
.st-head h2{font-size:16.5px;margin:0}
.st-word{margin-left:auto;font-size:14.5px;font-weight:620}
.st-word.green{color:var(--ok)}.st-word.yellow{color:var(--warn)}.st-word.red{color:var(--bad)}
.st-bar{display:flex;gap:2px;height:34px}
.st-bar .d{flex:1 1 0;min-width:0;border-radius:2px;background:var(--rule)}
.st-bar .g{background:var(--ok)}.st-bar .y{background:var(--warn)}.st-bar .r{background:var(--bad)}
.st-scale{display:flex;justify-content:space-between;gap:10px;margin-top:7px;
font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.st-scale .d30{display:none}
.st-key{padding:4px 22px 18px}
.st-key>h2{margin:18px 0 0}
.st-key ul{list-style:none;padding:0;margin:12px 0 16px;display:grid;
grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 22px}
.st-key li{display:flex;align-items:baseline;gap:9px;margin:0;font-size:14.5px}
.st-key .sq{display:inline-block;width:10px;height:10px;border-radius:2px;flex:none;
background:var(--rule);transform:translateY(1px)}
.st-key .sq.g{background:var(--ok)}.st-key .sq.y{background:var(--warn)}
.st-key .sq.r{background:var(--bad)}
.st-performance{padding:20px 22px;margin:0 0 18px;scroll-margin-top:100px}
.st-performance h2{margin:0;font-size:19px}
.st-performance .sub{margin:6px 0 16px}
.st-perf-window{display:block;margin-top:5px;font-size:13px;color:var(--muted)}
.st-perf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
margin:0 0 14px}
.st-perf-metric{min-width:0;padding:14px 15px;border:1px solid var(--rule-soft);
border-radius:10px;background:var(--inset)}
.st-perf-metric dt{font-size:13px;line-height:1.5;font-weight:600;color:var(--muted)}
.st-perf-metric dd{margin:8px 0 5px;font-size:27px;font-weight:700;line-height:1.25;
font-variant-numeric:tabular-nums;letter-spacing:-.015em;overflow-wrap:anywhere}
.st-perf-metric p{font-size:12.5px;line-height:1.55;margin:0;color:var(--muted)}
.st-perf-note{margin:8px 0;font-size:13px;color:var(--muted)}
.st-perf-help{border-top:1px solid var(--rule-soft);margin-top:14px;padding-top:12px}
.st-perf-help summary{cursor:pointer;color:var(--acc);font-weight:600;font-size:14px}
.st-perf-help p,.st-perf-help li{font-size:13px;line-height:1.65}
.st-perf-help ul{margin-bottom:8px}
.st-machine-service{grid-template-columns:repeat(2,minmax(0,1fr))}
.st-machine-subhead{font-size:14px;font-weight:620;margin:20px 0 10px}
.st-machines{padding:20px 22px;margin:0 0 18px;scroll-margin-top:100px}
.st-machines>h2{margin:0;font-size:19px}
.st-machine-sheet{overflow:auto}
.st-machine-table{width:100%;min-width:0;border-collapse:collapse;table-layout:fixed;font-size:14px}
.st-machine-table th,.st-machine-table td{text-align:left;vertical-align:middle;
padding:10px 12px;border-bottom:1px solid var(--rule-soft);white-space:normal;
overflow-wrap:anywhere}
.st-machine-table thead th{font-size:12px;font-weight:620;color:var(--muted)}
.st-machine-table th:first-child{width:34%}.st-machine-table th:nth-child(2){width:28%}
.st-machine-table tbody th{font-weight:620;text-transform:none;letter-spacing:normal}
.st-machine-table tbody tr:last-child th,.st-machine-table tbody tr:last-child td{border-bottom:0}
.st-machine-row{scroll-margin-top:100px}
.st-machine-state{display:inline-flex;align-items:center;gap:7px;max-width:100%;white-space:nowrap}
.st-machine-state .st-word{margin:0;font-size:13px;white-space:nowrap}
.st-machine-name{display:block;color:var(--muted);font-size:12px;font-weight:400;margin-top:2px}
.st-machine-table time{font-size:13px;color:var(--muted)}
.st-machine-empty{color:var(--muted)}
.st-diagnosis{padding:20px 22px;margin:0 0 18px}.st-diagnosis h2{margin:0 0 8px}
.st-health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));
gap:12px}.st-health-item{min-width:0;background:var(--inset);border:1px solid var(--rule-soft);
border-radius:10px;padding:12px 14px}.st-health-item h3{font-size:15px;margin:0}
.st-health-help{font-size:13px;color:var(--muted);line-height:1.6}
@media (max-width:560px){.st-bar{gap:1px;height:30px}
.st-bar .d:nth-child(-n+60){display:none}.st-scale .d90{display:none}
.st-scale .d30{display:inline}.st-overall{margin-left:0;flex-basis:100%}
.st-list,.st-key{padding-left:16px;padding-right:16px}
.st-key ul{grid-template-columns:minmax(0,1fr)}
.st-performance{padding:18px 16px}
.st-perf-grid{grid-template-columns:minmax(0,1fr);gap:8px}
.st-perf-metric{padding:12px 14px}
.st-perf-metric dd{font-size:25px;margin:5px 0}
.st-machines{padding:18px 16px}.st-machine-table{table-layout:auto}
.st-machine-table th:first-child,.st-machine-table th:nth-child(2){width:auto}
.st-machine-table thead th:first-child,.st-machine-table tbody th>.bi-inline{white-space:nowrap}
.st-machine-table th,.st-machine-table td{padding:9px 6px}}
"""


def diagnosis_panel(store: Any, report: Optional[Mapping[str, Any]], now: float) -> str:
    """Correlate existing reports without probing a provider or exposing a holder."""
    platform = status.platform_check(report, now)
    latest = store.latest_heartbeats()
    slots = store.list_slots()
    occupied = {row["node_id"] for row in slots
                if row.get("held_by")
                and row.get("state") in slotstates.HELD}
    alerts: dict[str, list[Mapping[str, Any]]] = {}
    for alert in store.open_alerts():
        alerts.setdefault(alert["node_id"], []).append(alert)
    shared = {row["node_id"] for row in slots
              if row.get("kind") == slotstates.MACHINE_SLOT}
    rows, checks = [], []
    for node in store.list_nodes():
        number = status.public_machine_number(node)
        if node["id"] not in occupied or number is None:
            continue
        check = status.machine_check(latest.get(node["id"]), alerts.get(node["id"], ()),
                                     node["id"] in shared, now)
        checks.append(check)
        rows.append('<article class="st-health-item"><h3>'
                    + label(f"Machine {number}", f"主机 {number}", inline=True) + '</h3>'
                    + status.health_html(check, now) + '</article>')
    if platform.level in {status.YELLOW, status.RED}:
        message = ("Start with the platform: resource pressure there can affect several slots. "
                   "It is a clue, not proof of a DMIT outage.",
                   "先检查平台：平台资源压力可能影响多个槽位。"
                   "这是排查线索，不代表已确认 DMIT 故障。")
    elif any(check.level in {status.YELLOW, status.RED} for check in checks):
        message = (("Platform resources look normal; check the flagged slot machines next. "
                    "This does not rule out network or model-provider errors.")
                   if platform.level == status.GREEN else
                   "Some slot machines need attention. Platform health is still unknown.",
                   "平台资源未见异常，接着检查被标记的槽位主机；仍不能排除网络或模型服务商错误。"
                   if platform.level == status.GREEN else
                   "部分槽位主机需要留意；平台状态仍然未知。")
    elif platform.level == status.UNKNOWN or any(check.level == status.UNKNOWN for check in checks):
        message = ("Some checks are missing. We cannot yet tell whether an error starts at the "
                   "platform or a slot machine.",
                   "部分检查缺失，暂时不能判断错误来自平台还是槽位主机。")
    else:
        message = ("No issue appears in these machine checks. Check your connection, sign-in, "
                   "usage limits and the model provider if a request still fails.",
                   "这些主机检查未见异常。请求仍失败时，请继续检查本地连接、登录、用量额度和模型服务商。")
    empty = '<p class="st-health-help">' + label(
        "No occupied slot machines to show yet.", "暂无正在使用的槽位主机。", inline=True) + '</p>'
    return ('<section class="card st-diagnosis" id="health-checks"><h2>'
            + label("Where is the problem?", "哪里出了问题？", inline=True) + '</h2><p>'
            + label(*message, inline=True) + '</p><div class="st-health-grid">'
            '<article class="st-health-item"><h3>'
            + label("CC Fleet platform", "CC Fleet 平台", inline=True) + '</h3>'
            + status.health_html(platform, now) + '<p class="st-health-help">'
            + label("On our DMIT deployment, this is the website and gateway server. "
                    "It does not measure DMIT\u2019s entire network or physical hardware.",
                    "在我们的 DMIT 部署中，这是网站与网关服务器；"
                    "不代表 DMIT 整个网络或物理硬件的状态。", inline=True)
            + '</p></article>' + ''.join(rows) + '</div>' + (empty if not rows else '')
            + '<p class="st-health-help">' + label(
                "Only occupied machines are shown here. Green covers the reported resources, "
                "not a successful model call. External provider availability and your computer's "
                "connection are not automatically tested. Your account page separately shows "
                "sign-in and usage. No prompts are sent by these checks.",
                "这里只展示正在使用的主机。绿色仅表示已上报资源正常，不表示模型调用成功。外部服务商和"
                "你的电脑连接没有自动测试；账号页面会单独显示登录与用量。这些检查不发送提示词。",
                inline=True) + '</p></section>')


def _day_title(day: status.Day, group: bool = False) -> str:
    """What one day was, said on hover: the square's title. For the machines,
    down is every one of them down at once, and degraded some in trouble."""
    if day.colour == status.NO_DATA:
        return f"{day.day} (UTC): no data"
    if group:
        said = [f"every machine down {day.down} min"] if day.down else []
        if day.degraded:
            said.append(f"some in trouble {day.degraded} min")
        return f"{day.day} (UTC): " + (", ".join(said) or "all up")
    said_up = f"{day.day} (UTC): {status.percent(day.up, day.up + day.down)} up"
    return said_up + (f", down {day.down} min" if day.down else "")


def _day_title_zh(day: status.Day, group: bool = False) -> str:
    if day.colour == status.NO_DATA:
        return f"{day.day}（UTC）：没有数据"
    if group:
        said = [f"全部机器离线 {day.down} 分钟"] if day.down else []
        if day.degraded:
            said.append(f"部分机器异常 {day.degraded} 分钟")
        return f"{day.day}（UTC）：" + ("，".join(said) or "全部正常")
    percent = status.percent(day.up, day.up + day.down)
    return (
        f"{day.day}（UTC）：在线率 {percent}"
    ) + (f"，离线 {day.down} 分钟" if day.down else "")


def _component(name: str, level: str, word: str, days: Sequence[status.Day],
               minutes: tuple[int, int], group: bool = False) -> str:
    """One part of the service: its state now, and its days as a bar.

    The bar is one picture to a screen reader, its label the uptime and how
    many days had trouble; each square says its own day on hover."""
    up, counted = minutes
    pct = status.percent(up, counted)
    trouble = sum(1 for d in days if d.colour in (status.YELLOW, status.RED))
    chart_label = (f"{name}: {pct} uptime over the last {len(days)} days; "
             f"{trouble} {'day' if trouble == 1 else 'days'} with trouble"
             if counted else f"{name}: no data yet")
    chart_label_zh = ((
        f"{COMPONENT_ZH[name]}：过去 {len(days)} 天在线率 "
        f"{pct}，{trouble} 天出现异常"
    )
                      if counted else COMPONENT_ZH[name] + "：尚无数据")
    squares = "".join((
        f'<span class="d {DAY_CLASS[d.colour]}" '
        f'title="{escape(_day_title(d, group))}" '
        f'data-title-en="{escape(_day_title(d, group))}" '
        f'data-title-zh="{escape(_day_title_zh(d, group))}"></'
        f'span>'
    ) for d in days)
    return (f'<div class="st-comp"><div class="st-head">'
            f'<span class="st-dot {level}" aria-hidden="true"></span>'
            '<h2>' + label(name, COMPONENT_ZH[name], inline=True) + '</h2>'
            + f'<span class="st-word {level}">'
            + label(word, _machines_zh_from_word(word) if group else WORDS_ZH[level],
                    inline=True) + (
                        '</span></div>'
                        f'<div class="st-bar" role="img" '
                        f'aria-label="{escape(chart_label)}" '
                        f'data-aria-label-en="{escape(chart_label)}" '
                        f'data-aria-label-zh="{escape(chart_label_zh)}">{squares}</div>'
                    )
            + '<div class="st-scale"><span><span class="d90">'
            + label("90 days ago", "90 天前", inline=True) + '</span><span class="d30">'
            + label("30 days ago", "30 天前", inline=True) + '</span></span><span>'
            + label(pct + ' uptime' if counted else pct,
                    pct + ' 在线率' if counted else '暂无数据', inline=True)
            + '</span><span>' + label("Today", "今天", inline=True) + '</span></div></div>')


def machines_word(states: Sequence[status.State]) -> str:
    """The machines, counted: how many of them are up, and what the rest are."""
    if not states:
        return "No slot machines yet"
    said = [f"{sum(1 for s in states if s.level == status.GREEN)} of {len(states)} "
            "operational"]
    for level, word in ((status.YELLOW, "degraded"), (status.RED, "down")):
        count = sum(1 for s in states if s.level == level)
        if count:
            said.append(f"{count} {word}")
    return " · ".join(said)


def _machines_zh_from_word(word: str) -> str:
    """The machine summary contains only aggregate counts, never identities."""
    if word == "No slot machines yet":
        return "暂无监测中的槽位主机"
    pieces = word.split(" · ")
    first = pieces[0].split()
    result = [f"{first[2]} 台中 {first[0]} 台正常"]
    for piece in pieces[1:]:
        count, state = piece.split()
        result.append(f"{count} 台{'异常' if state == 'degraded' else '离线'}")
    return " · ".join(result)


KEY = (
    '<div class="card st-key"><h2>' + label("Reading this page", "如何理解状态", inline=True)
    + '</h2><ul>'
    + ''.join('<li><span class="sq ' + cls + '"></span>'
              + label(en, zh, inline=True) + '</li>' for cls, en, zh in (
                  ("g", "Up all day", "全天正常"),
                  ("y", (
                      'Degraded, or down for less than 5 minutes'
                  ), "出现异常，或离线不足五分钟"),
                  ("r", "Down for 5 minutes or more", "离线至少五分钟"),
                  ("", "No data: before counting began", "尚未开始统计，没有数据")))
    + '</ul><p>' + bi(
        (
            f'<strong>{SITE_NAME}</strong> covers sign-in and account p'
            f'ages. The chart records '
            'whether the website was available; it does not verify '
            'model-request readiness. '
            'If the HTTPS gateway is unavailable, model requests '
            'through it may also fail. '
            'An already open remote session may keep running if its '
            'slot machine remains available.'
        ),
        '<strong>网站与账号页面</strong> 包括登录和槽位管理。图表记录网站是否可用，'
        '不代表下一次模型请求一定成功。HTTPS 网关不可用时，经它发送的模型请求也可能失败；'
        '如果槽位机器仍在线，已经打开的远程会话可能继续运行。', inline=True)
    + '</p><p>' + bi(
        (
            f'<strong>{MACHINES_NAME}</strong> are the machines slots r'
            f'un on, counted together and listed under stable public labels; '
            'your own page links to your machine. A machine is down when '
            'it has not reported within its expected reporting interval, '
            'or a check of the machine itself fails; '
            'degraded when it reports late or a '
            'check warns, such as a disk nearly full. Your own '
            'sign-in and usage limits are never '
            'counted against it.'
        ),
        (
            '<strong>槽位主机</strong> 汇总承载槽位的机器，并以固定的公共编号逐台展示，'
            '不公开各用户的名称。“我的槽位”会链接到你的主机。'
            '超过预期上报间隔仍无心跳、或机器检查失败，判为离线；上报延迟、'
            '磁盘将满等告警，判为异常。'
            '你的 Claude 登录状态和用量限制不计入机器故障。'
        ), inline=True)
    + '</p><p class="muted small">' + label(
        'The squares are UTC days; times are in your own time zone. '
        'This page refreshes itself every minute.',
        '每日方块按 UTC 统计；具体时间按你的本地时区显示。页面每分钟自动刷新。', inline=True)
    + '</p></div>')


def _latency_text(sample: Mapping[str, Any]) -> str:
    """A measured mean, never a zero substituted for an unavailable sample."""
    mean = sample.get("mean")
    if not sample.get("count") or mean is None:
        return label("—", "—", inline=True)
    if mean >= 1000:
        number = f"{mean / 1000:,.2f}"
        return label(number + " s", number + " 秒", inline=True)
    number = f"{mean:,.0f}" if mean >= 10 else f"{mean:,.1f}"
    return label(number + " ms", number + " 毫秒", inline=True)


def _timing_samples_text(sample: Mapping[str, Any]) -> str:
    """A publishable timing's sample count, never a contributor count."""
    count = sample.get("count")
    if count is None:
        return label("More aggregate samples needed", "还需要更多汇总样本", inline=True)
    if count == 0:
        return label("No timing samples", "暂无计时样本", inline=True)
    return label(f"{count:,} timing samples", f"{count:,} 个计时样本", inline=True)


def _throughput_reading(sample: Any) -> tuple[bool, str, str]:
    """Output rate is independently publishable, including a measured zero."""
    if isinstance(sample, Mapping) and sample.get("state") == "ready":
        count, rate = sample.get("samples"), sample.get("tokens_per_second")
        try:
            if (type(count) is int
                    and performance.MIN_THROUGHPUT_SAMPLES <= count
                    <= performance.MAX_THROUGHPUT_TOTAL
                    and type(rate) in (int, float) and math.isfinite(rate)
                    and 0 <= rate <= 10_000_000_000):
                number = f"{rate:,.2f}"
                return (True, label(number + " tokens/s", number + " Token/秒", inline=True),
                        label(f"{count:,} streamed reply samples", f"{count:,} 个流式回复样本",
                              inline=True))
        except (OverflowError, ValueError):
            pass
    waiting = isinstance(sample, Mapping) and sample.get("state") == "limited_data"
    return (False, label("—", "—", inline=True), label(
        "Waiting for 20 eligible streamed replies" if waiting
        else "Needs 20 eligible streamed replies",
        "等待 20 个符合条件的流式回复" if waiting else "需有 20 个符合条件的流式回复", inline=True))


def _report_time(observed: float, now: float) -> str:
    """Localize the oldest included observation with a useful no-script age."""
    minutes = max(0, int((now - observed) // 60))
    if minutes < 1:
        age_en, age_zh = "less than a minute ago", "不到一分钟前"
    elif minutes < 60:
        age_en, age_zh = f"{minutes}m ago", f"{minutes} 分钟前"
    else:
        hours = minutes // 60
        age_en, age_zh = f"{hours}h ago", f"{hours} 小时前"
    return (f'<time datetime="{escape(status.utc_iso(observed))}" data-local>'
            + label(age_en, age_zh, inline=True) + '</time>')


def machine_details_panel(details: Sequence[status.MachineDetail],
                          viewer: Optional[Viewer], now: float,
                          nodes: Optional[Mapping[str, Mapping[str, Any]]] = None) -> str:
    """One compact row per machine, with private display names only for operators."""
    rows = []
    labeled = [detail for detail in details
               if type(detail.public_number) is int and detail.public_number > 0]
    for detail in sorted(labeled, key=lambda item: item.public_number):
        number = detail.public_number
        identity = f"machine-{number}"
        level = detail.state.level
        reported = (_report_time(detail.last_report, now) if detail.last_report is not None
                    and math.isfinite(detail.last_report) else label(
                        "Not yet received", "尚未收到", inline=True))
        operator_name = ''
        if viewer is not None and viewer.operator:
            display_name = detail.operator_name or detail.node_id
            operator_name = '<small class="st-machine-name">' + escape(display_name) + '</small>'
        rows.append(
            f'<tr class="st-machine-row" id="{identity}">'
            f'<th scope="row" id="{identity}-title">'
            + label(f"Machine {number}", f"主机 {number}", inline=True)
            + operator_name + offer_ui.compact((nodes or {}).get(detail.node_id) or {})
            + '</th><td><span class="st-machine-state">'
            f'<span class="st-dot {level}" aria-hidden="true"></span>'
            + f'<span class="st-word {level}">'
            + label(status.WORDS[level], WORDS_ZH[level], inline=True)
            + '</span></span></td><td>' + reported + '</td></tr>')
    empty = ('<tr><td class="st-machine-empty" colspan="3">'
             + label("No labeled slot machines to show yet.", "暂无可展示的已编号槽位主机。",
                     inline=True) + '</td></tr>')
    return ('<section class="card st-machines" id="slot-machines" '
            'aria-labelledby="slot-machines-title"><h2 id="slot-machines-title">'
            + label("Each slot machine", "逐台槽位主机", inline=True) + '</h2><p class="sub">'
            + label("Match the public machine number on Your slots. These checks cover the "
                    "machine, not Claude sign-in, quota or acceptance of a model request.",
                    "请对照“我的槽位”中的公共主机编号。这里检查主机状态，不包括 Claude "
                    "登录、用量额度，也不保证模型请求会被接受。", inline=True)
            + '</p><div class="st-machine-sheet" role="region" '
            'aria-labelledby="slot-machines-title" tabindex="0">'
            '<table class="st-machine-table" aria-labelledby="slot-machines-title">'
            '<thead><tr><th scope="col">' + label("Machine", "主机", inline=True)
            + '</th><th scope="col">' + label("Status", "状态", inline=True)
            + '</th><th scope="col">' + label("Last report", "最近上报", inline=True)
            + '</th></tr></thead><tbody>' + (''.join(rows) if rows else empty)
            + '</tbody></table></div></section>')


def performance_panel(report: Mapping[str, Any], now: float) -> str:
    """Relay and streamed-output aggregates have independent publication gates."""
    ready = report.get("state") == "ready"
    latencies = report.get("latencies", {}) if ready else {}
    connect = latencies.get("connect_ms", {})
    first = latencies.get("first_byte_ms", {})
    total = latencies.get("total_ms", {})
    requests = report.get("requests") if ready else None
    completed = report.get("completed") if ready else None
    rate = (status.percent(completed, requests) if requests and completed is not None else "—")
    count = f"{requests:,}" if requests is not None else "—"
    completion_samples = (label(f"{completed:,} of {requests:,} transfers completed",
                                f"{requests:,} 次传输中有 {completed:,} 次完成", inline=True)
                          if completed is not None and requests is not None else '')
    throughput = report.get("throughput")
    throughput_ready, output_rate, output_samples = _throughput_reading(throughput)
    cards = (
        ("Transfer completion", "传输完成率", label(rate, rate, inline=True),
         "Higher is better for transport completion, not task success.",
         "传输完成率越高越好；不代表任务成功率。", completion_samples),
        ("Average first response byte", "平均首字节等待时间", _latency_text(first),
         "Lower means an earlier reply body; not time to first token (TTFT).",
         "越低表示越早收到回复正文；不等同于首个 Token 时间（TTFT）。",
         _timing_samples_text(first)),
        ("Observed relay attempts", "已记录的转发尝试", label(count, count, inline=True),
         "Traffic volume, not a performance score; retries may add attempts.",
         "这是流量规模，不是性能评分；重试可能增加尝试次数。", ''),
        ("Average upstream connection", "平均上游连接时间", _latency_text(connect),
         "Lower is better for connection setup.",
         "建连时间越低越好。", _timing_samples_text(connect)),
        ("Average full transfer", "平均完整传输时间", _latency_text(total),
         "Answer-length dependent; no automatic better/worse verdict.",
         "受回复长度影响；不自动判定更好或更差。", _timing_samples_text(total)),
        ("Streamed output rate", "流式输出速率", output_rate,
         "At the observing relay; higher is faster for comparable models and workloads.",
         "在转发点观测；模型和工作负载相近时，数值越高表示交付越快。", output_samples),
    )
    grid = '<dl class="st-perf-grid">' + ''.join(
        '<div class="st-perf-metric"><dt>' + label(en, zh, inline=True)
        + '</dt><dd>' + value
        + ('<p class="st-perf-samples">' + samples + '</p>' if samples else '')
        + '<p>' + label(help_en, help_zh, inline=True)
        + '</p></dd></div>' for en, zh, value, help_en, help_zh, samples in cards
    ) + '</dl>'
    if ready:
        freshness = (
            '<p class="st-perf-note">' + label(
                "Oldest included report:", "最早的一份有效上报：", inline=True)
            + ' ' + _report_time(report["oldest_observed_at"], now) + '</p>'
            if report.get("oldest_observed_at") is not None else ''
        )
        note = '<p class="st-perf-note">' + label(
            "The five slot-relay figures include only recent, usable reports. This is part "
            "of Fleet traffic, not a measurement of every request. "
            "A dash means no publishable timing sample yet.",
            "五项槽位端统计仅包含近期上报且数据有效的报告。这只覆盖部分 Fleet 流量，"
            "不代表每一次请求。"
            "横线表示暂时没有可公开的计时数据。",
            inline=True) + '</p>' + freshness
    else:
        message = ("No fresh slot-relay transfer data yet", "暂时没有新的槽位端传输数据") if (
            report.get("state") == "no_data") else (
                "Waiting for enough aggregated relay data", "等待足够的转发汇总数据")
        note = '<p class="st-perf-note"><strong>' + label(*message, inline=True) + '</strong> '
        note += label(
            "At least 20 recorded attempts are needed before traffic figures appear. "
            "Each timing needs its own 20 samples. "
            "A dash means unknown, not zero.",
            "至少有 20 次已记录的尝试，才展示流量数字。"
            "每项计时也要单独达到 20 个样本。横线表示未知，不是零。",
            inline=True) + '</p>'
    if throughput_ready:
        collected = throughput.get("collection_start_day")
        try:
            if (isinstance(collected, str) and date.fromisoformat(collected).isoformat()
                    == collected and collected <= datetime.fromtimestamp(now, timezone.utc)
                    .date().isoformat()):
                note += '<p class="st-perf-note">' + label(
                    "Earliest available output-rate samples: " + collected + " (UTC).",
                    "现有输出速率样本最早日期：" + collected + "（UTC）。", inline=True) + '</p>'
        except (ValueError, OverflowError, OSError):
            pass
    timing_notes = ''
    if ready:
        for key, en, zh in (
                ("connect_ms", "Average upstream connection", "平均上游连接时间"),
                ("first_byte_ms", "Average first response body byte", "平均回复正文首字节时间"),
                ("total_ms", "Average full transfer", "平均完整传输时间")):
            sample = latencies.get(key, {})
            timing_notes += (
                '<li>' + label(en + ': ', zh + '：', inline=True) + _latency_text(sample)
                + ' · ' + _timing_samples_text(sample) + '</li>'
            )
        timing_notes = '<ul>' + timing_notes + '</ul>'
    help_ = (
        '<details class="st-perf-help"><summary>'
        + label("How we measure this", "这些数字是怎么来的", inline=True) + '</summary>'
        + '<p>' + label(
            "The five slot-relay figures cover the last seven UTC calendar days, including "
            "today. They can include both Messages and token-count calls. One user request "
            "can produce more than one attempt when a safe fallback is tried. Requests rejected "
            "before reaching a slot relay are not counted.",
            "统计窗口为最近七个 UTC 日期，包含今天。记录来自槽位端的转发尝试，可能包括模型消息"
            "和 Token 计数调用。发生安全切换时，一次用户请求可能产生多次尝试。尚未到达槽位转发"
            "服务就被拒绝的请求，不在统计内。", inline=True) + '</p>'
        + '<p>' + label(
            "Transfer completion means a successful HTTP reply (2xx) was fully transferred. "
            "Error replies, cancellations, and interrupted transfers are not successful. "
            "It does not tell us whether the model completed your task or guarantee service "
            "availability. The availability charts below measure something different.",
            "传输完成是指成功的 HTTP 回复（2xx）已完整传回。错误回复、取消和中断都不算成功。"
            "这个比例不表示模型一定完成了你的任务，也不保证服务可用。下方的在线记录统计的是"
            "另一回事。", inline=True) + '</p>'
        + '<p>' + label(
            "The first-byte timer starts when the slot accepts the relay request and stops when "
            "the first upstream response body byte arrives. That byte may be stream metadata, "
            "not a generated word. It excludes the trip from your computer to the gateway and "
            "SSH setup. Full transfer time also grows with answer length. These are "
            "sample-weighted averages, not medians or percentiles; each timing can have a "
            "different sample count.",
            "首字节计时从槽位接收转发请求开始，到收到上游回复正文的第一个字节结束。这个字节可能"
            "只是流式回复的标记，还不是生成的文字。它不包含你的电脑到网关的网络耗时，也不包含"
            "SSH 建连耗时。完整传输时间还受回复长度影响。这里按样本数计算平均值，不是中位数"
            "或百分位数；各项计时的样本数可能不同。", inline=True) + '</p>'
        + timing_notes
        + '<p>' + label(
            "Traffic figures require at least 20 attempts. "
            "Each timing separately requires at least 20 samples, "
            "so a timing may remain unknown even when traffic totals can be shown.",
            "至少有 20 次尝试，才公开流量数字。每项计时也必须单独达到"
            "至少 20 个样本。因此，即使流量总数可以公开，某项计时仍可能未知。",
            inline=True) + '</p>'
        + '<p>' + label(
            "Streamed output rate combines native/bridge slot-relay measurements and HTTPS "
            "gateway measurements without counting a gateway reply twice. It covers successful, "
            "fully delivered /v1/messages streaming replies. It divides the sum of "
            "provider-reported output tokens by the sum of time from the first to the last "
            "successfully forwarded response-body chunk at the observing relay. "
            "A reply needs valid output usage, at least two "
            "chunks and 100 ms of delivery time. Single-chunk or too-short replies are "
            "excluded; buffering can affect the observed rate. It excludes the initial wait, "
            "but includes stream pauses and network delivery, and includes all output "
            "tokens reported by the provider, including thinking or tool output where counted. "
            "Higher is faster for comparable models and workloads; this is an observed "
            "delivery rate, not a pure model-speed benchmark.",
            "流式输出速率合并原生/桥接槽位转发和 HTTPS 网关的观测，不重复计算网关回复。"
            "只统计成功且完整传回的 /v1/messages 流式回复。计算方式为服务商报告的输出 "
            "Token 总量除以观测点从首个到最后一个成功转发的响应正文分块的"
            "时间总和。回复需有有效的输出用量、至少两个分块，且传输过程至少持续 100 毫秒。"
            "单个分块或过短的回复不计入；缓冲可能影响观测到的速率。不包含最初等待回复的"
            "时间，但包含流中的暂停和网络传输；服务商计入的全部输出 Token 都在内，包括被计入"
            "的思考或工具输出。模型和工作负载相近时，数值越高表示交付越快；这是观测到的交付"
            "速率，不是模型本身速度的基准测试。", inline=True) + '</p>'
        + '<p>' + label(
            "Output rate needs at least 20 eligible streamed replies over the same seven UTC "
            "calendar days. It can be shown while slot-relay figures remain unknown, or "
            "remain unknown when they are ready. Collection starts with new observations; "
            "older traffic is not backfilled. The gateway retains anonymous daily numeric totals; "
            "native relays keep account-bound daily numeric totals in the slot report. "
            "This output-rate panel publishes no user, slot or model breakdown or individual "
            "request timestamps. The homepage separately shows masked user usage "
            "and model details.",
            "输出速率在相同的七个 UTC 日期窗口内单独要求至少 20 个符合条件的流式回复。"
            "槽位端统计未知时，输出速率仍可能公开；槽位端统计已就绪时，输出速率也可能仍未知。"
            "只从新的观测开始收集，不补填历史流量。网关保留匿名每日数值汇总，原生转发在槽位报告"
            "中保留绑定账号的每日数值汇总。本速率面板不公开用户、槽位、模型细分或单次请求时间；"
            "首页另行展示部分隐藏昵称的用户用量和模型详情。",
            inline=True) + '</p>'
        + '<p>' + label(
            "Only combined numbers are public, even if only one account contributes. "
            "This panel shows no account names, slots, "
            "messages, or individual request records.",
            "即使只有一个账号贡献数据，公开页面也只显示汇总数字。"
            "不展示账号名称、具体槽位、消息内容或单条请求记录。",
            inline=True) + '</p></details>'
    )
    return (
        '<section class="card st-performance" id="performance" aria-labelledby="performance-title">'
        '<h2 id="performance-title">' + label("Fleet performance", "Fleet 性能", inline=True)
        + '</h2><p class="sub">' + label(
            "Recent slot-relay transfer and streamed-output delivery measurements.",
            "近期槽位转发与流式输出交付观测。", inline=True)
        + '<span class="st-perf-window">' + label(
            "Last 7 UTC calendar days, including today", "最近 7 个 UTC 日期，包含今天",
            inline=True)
        + '</span></p>' + grid + note + help_ + '</section>'
    )


def _machine_number(value: Any) -> Optional[float]:
    """The public panel formats only finite, non-negative numeric totals."""
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _machine_percent(value: Any) -> str:
    number = _machine_number(value)
    shown = f"{number:,.1f}%" if number is not None else "—"
    return label(shown, shown, inline=True)


def _machine_bytes(value: Any) -> str:
    number = _machine_number(value)
    if number is None:
        return "—"
    unit = "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if number < 1024 or unit == "PiB":
            break
        number /= 1024
    return f"{number:,.0f} {unit}" if unit == "B" else f"{number:,.1f} {unit}"


def _machine_capacity(used: Any, total: Any) -> str:
    return label(_machine_bytes(used) + " / " + _machine_bytes(total),
                 _machine_bytes(used) + " / " + _machine_bytes(total), inline=True)


def _machine_cards(cards: Sequence[tuple[str, str, str, str]], service: bool = False) -> str:
    style = 'st-perf-grid st-machine-service' if service else 'st-perf-grid'
    return '<dl class="' + style + '">' + ''.join(
        '<div class="st-perf-metric"><dt>' + label(en, zh, inline=True)
        + '</dt><dd>' + value + '<p>' + note + '</p></dd></div>'
        for en, zh, value, note in cards
    ) + '</dl>'


def machine_panel(report: Mapping[str, Any], now: float) -> str:
    """Infrastructure totals are independent of transfer-sample minima."""
    state = report.get("state")
    visible = state in ("ready", "partial")
    groups = {}
    for key in ("host", "service", "storage"):
        candidate = report.get(key) if visible else None
        groups[key] = candidate if isinstance(candidate, Mapping) else {}
    host, service, storage = groups["host"], groups["service"], groups["storage"]
    cards = (
        ("Host CPU use", "主机 CPU 占用", _machine_percent(host.get("cpu_pct")),
         label("Across the whole gateway VPS; averaged over the sample interval.",
               "整台网关 VPS 的执行占用，按采样区间计算平均值。", inline=True)),
        ("Estimated host memory used", "主机内存估算占用",
         _machine_percent(host.get("memory_pct")),
         _machine_capacity(host.get("memory_used_bytes"), host.get("memory_total_bytes"))
         + '<br>' + label("Used / total · based on memory available to apps.",
                          "已用 / 总量 · 根据系统可供应用使用的内存估算。", inline=True)),
        ("Fleet data filesystem", "Fleet 数据所在文件系统",
         _machine_percent(storage.get("used_pct")),
         _machine_capacity(storage.get("used_bytes"), storage.get("total_bytes"))
         + '<br>' + label("Used / total · storage capacity, not disk speed.",
                          "已用 / 总量 · 表示容量占用，不是磁盘速度。", inline=True)),
    )
    quota = _machine_number(service.get("cpu_quota_cores"))
    budget = label(
        f"CPU limit: {quota:,.2f} cores" if quota is not None else "CPU limit: unknown",
        f"CPU 配额：{quota:,.2f} 核" if quota is not None else "CPU 配额：未知", inline=True)
    service_cards = (
        ("Fleet CPU budget used", "Fleet CPU 配额占用",
         _machine_percent(service.get("cpu_budget_pct")), budget),
        ("Fleet memory budget used", "Fleet 内存配额占用",
         _machine_percent(service.get("memory_pct")),
         _machine_capacity(service.get("memory_used_bytes"), service.get("memory_limit_bytes"))
         + '<br>' + label("Usage / limit · includes cache and kernel memory.",
                          "统计占用 / 上限 · 包含缓存与内核内存。", inline=True)),
    )
    notes = {
        "partial": ("Some measurements are not available yet. A dash means unknown, not zero.",
                    "部分指标暂时不可用。横线表示未知，不是零。"),
        "stale": ("The machine report is out of date. Waiting for a fresh sample; old values "
                  "are hidden.", "主机上报已过期，正在等待新样本；旧数值暂不展示。"),
    }
    note = ''
    if state != "ready":
        message = notes.get(state, (
            "Waiting for a machine sample. A dash means unknown, not zero.",
            "正在等待主机采样。横线表示未知，不是零。"))
        note = '<p class="st-perf-note"><strong>' + label(*message, inline=True) + '</strong></p>'
    observed = _machine_number(report.get("observed_at"))
    if observed is not None and observed <= now + 60 and state in ("ready", "partial", "stale"):
        stamp = ("Last report:", "最近一次上报：") if state == "stale" else ("Updated:", "更新于：")
        note += '<p class="st-perf-note">' + label(*stamp, inline=True)
        note += ' ' + _report_time(observed, now) + '</p>'
    details = ''
    cores = _machine_number(host.get("logical_cpus"))
    load = _machine_number(host.get("load_1m"))
    core_text = f"{cores:,.0f}" if cores is not None else "—"
    load_text = f"{load:,.2f}" if load is not None else "—"
    details += '<li>' + label(
        f"Logical CPUs: {core_text} · one-minute load average: {load_text}",
        f"逻辑 CPU 数：{core_text} · 一分钟平均负载：{load_text}", inline=True) + '</li>'
    details += '<li>' + label("Host I/O wait: ", "主机 I/O 等待：", inline=True)
    details += _machine_percent(host.get("io_wait_pct")) + ' · '
    details += label("VPS CPU steal: ", "VPS 等待宿主机 CPU：", inline=True)
    details += _machine_percent(host.get("steal_pct")) + '</li>'
    details += '<li>' + label("Data filesystem space available: ",
                             "数据所在文件系统可用空间：", inline=True)
    available = _machine_bytes(storage.get("available_bytes"))
    details += label(available, available, inline=True) + '</li>'
    help_ = (
        '<details class="st-perf-help"><summary>'
        + label("What these measurements mean", "这些指标怎么看", inline=True) + '</summary>'
        + '<p>' + label(
            "Think of the gateway as a junction that passes requests along. These numbers show "
            "how busy that junction is. High CPU use, little available memory, long I/O waits "
            "or a nearly full disk can help explain slow replies. They do not prove that "
            "the gateway is the bottleneck: your network, the slot machine and Anthropic can "
            "also affect response time.",
            "把网关想成转交请求的路口，这些数字告诉你路口有多忙。CPU 很忙、可用内存很少、"
            "I/O 等待较多，或磁盘快满时，都可能影响回复速度。但它们不能直接证明网关是瓶颈："
            "你的网络、槽位主机和 Anthropic 也会影响响应时间。", inline=True) + '</p>'
        + '<ul>' + details + '</ul><p>' + label(
            "Host CPU counts time executing work; I/O wait and CPU time taken by the VPS "
            "provider are shown separately. Load average includes tasks running or waiting "
            "for I/O, so it is not a CPU percentage. Host memory used is total minus available "
            "memory. The storage card measures the filesystem that holds Fleet data, including "
            "other files on that filesystem; it does not measure all disks or disk throughput.",
            "主机 CPU 占用只统计执行工作所用的时间；I/O 等待、VPS 等待宿主机提供 CPU 的时间另列。"
            "平均负载包含运行中或等待 I/O 的任务，不是 CPU 百分比。内存估算占用为总量减去可用内存。"
            "磁盘卡片统计 Fleet 数据所在文件系统，也包含该文件系统上的其他文件；不代表所有磁盘，"
            "也不测量磁盘读写速度。", inline=True) + '</p><p>' + label(
            "Fleet CPU use is a percentage of the service's configured CPU limit, rather than "
            "a percentage of the whole VPS. Fleet memory is container usage counted toward "
            "the limit, including cache and kernel memory; it differs from estimated host "
            "memory. Samples update "
            "about every 15 seconds; this page refreshes every minute. Missing or stale values "
            "remain unknown. Machine resource totals are independent of transfer samples.",
            "Fleet CPU 占用按服务自身的 CPU 配额计算，不是整台 VPS 的百分比。Fleet 内存为容器"
            "计入配额的占用，包含缓存和内核内存，与主机内存估算口径不同。约每 15 秒采样一次，"
            "页面每分钟刷新。缺失或过期的数值仍显示为未知。主机资源汇总与传输样本数相互独立。",
            inline=True) + '</p></details>'
    )
    return (
        '<section class="card st-performance" id="gateway-machine" '
        'aria-labelledby="gateway-machine-title"><h2 id="gateway-machine-title">'
        + label("Gateway machine resources", "网关主机资源", inline=True) + '</h2><p class="sub">'
        + label("Gateway VPS dedicated to Fleet. Host figures cover the entire VPS.",
                "Fleet 专用网关 VPS。主机指标覆盖整台 VPS。", inline=True) + '</p>'
        + _machine_cards(cards) + '<h3 class="st-machine-subhead">'
        + label("Fleet service allocation", "Fleet 服务自身的配额", inline=True) + '</h3>'
        + _machine_cards(service_cards, service=True) + note + help_ + '</section>'
    )


def now_state(store: object, now: float) -> tuple[list[status.State], status.State]:
    """Every machine that counts, as it is now, and the service as one: what
    the banner says, on this page and in the front page's pill. The site is up
    whenever it is served, so only the machines can say otherwise now; its own
    downtime shows in its days."""
    states = list(status.snapshot(store, now).values())
    return states, status.group_state(states)


def health(store: object, now: float) -> Optional[str]:
    """The banner's level, for the front page's pill. None while no machine
    counts yet: then nothing has been measured to vouch for."""
    states, group = now_state(store, now)
    return group.level if states else None


def pill(level: Optional[str]) -> str:
    """The way here from the top of the front page: the banner's words, in its
    colour. Nothing measured yet, it says only "Status", never a green that
    nobody saw."""
    if level is None:
        return (
            '<a class="pill st-pill" href="/status">'
        ) + label("Status", "服务状态", inline=True) + '</a>'
    return (f'<a class="pill st-pill {PILL_CLASS[level]}" href="/status" '
            f'aria-label="Status: {BANNERS[level]}" '
            f'data-aria-label-en="Status: {BANNERS[level]}" '
            f'data-aria-label-zh="服务状态：{BANNERS_ZH[level]}">'
            + label(BANNERS[level], BANNERS_ZH[level], inline=True) + '</a>')


def page(store: object, cfg: Config, viewer: Optional[Viewer], now: float,
         machine_report: Optional[Mapping[str, Any]] = None) -> str:
    """The whole page."""
    details = status.machine_details(store, now)
    states = [detail.state for detail in details]
    group = status.group_state(states)
    history = status.history(store, now)
    since = (
        '<span class="st-since">'
        + label("since", "始于", inline=True)
        + " "
        + status.since_html(group.since, now)
        + "</span>"
        if group.since is not None
        else ""
    )
    up, counted = history.overall_minutes
    overall = (
        f"{status.percent(up, counted)} uptime over the last {status.HISTORY_DAYS} days"
        if counted
        else "No uptime counted yet"
    )
    body = (
        '<div class="pagehead"><h1>' + label("Status", "服务状态", inline=True) + "</h1>"
        '<p class="sub">'
        + label(
            "Current availability and recent Fleet transfer performance.",
            "查看当前可用状态，以及近期 Fleet 传输表现。",
            inline=True,
        )
        + (
            "</p></div>"
            f'<div class="st-banner {group.level}">'
            f'<span class="st-dot {group.level}" aria-hidden="true'
            f'"></span>'
            "<strong>"
        )
        + label(BANNERS[group.level], BANNERS_ZH[group.level], inline=True)
        + f'</strong>{since}<span class="st-overall">'
        + label(
            overall,
            f"过去 {status.HISTORY_DAYS} 天在线率 {status.percent(up, counted)}"
            if counted
            else "尚无在线率数据",
            inline=True,
        )
        + "</span></div>"
        + diagnosis_panel(store, machine_report, now)
        + performance_panel(performance.summary(store, cfg, now), now)
        + (machine_panel(machine_report, now) if machine_report is not None else '')
        + '<div class="card st-list">'
        + _component(
            SITE_NAME, status.GREEN, status.WORDS[status.GREEN], history.site, history.site_minutes
        )
        + _component(
            MACHINES_NAME,
            group.level,
            machines_word(states),
            history.machines,
            history.machines_minutes,
            group=True,
        )
        + "</div>"
        + machine_details_panel(details, viewer, now,
                                {node["id"]: node for node in store.list_nodes()})
        + KEY
    )
    return _shell("status", body, REFRESH, STATUS_CSS, viewer=viewer, here="/status")
