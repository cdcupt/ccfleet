"""The public status page, /status: whether ccfleet is up, now and over 90 days.

Anybody may read it, signed in or not, so it says only what anybody may know.
Never a machine's id or a slot's name: a slot somebody holds is named after
them (Erik, 2026-09-24), so the machines are one group here, counted, never
listed. Each holder sees their own slot's machine on their own page instead
(status.slot_line). The front page says the banner's words at its top, as a
pill that links here (Erik, 2026-09-25).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any, Optional

from . import performance, status
from .bilingual import bi, label
from .config import Config
from .usersite import Viewer, _shell

SITE_NAME = "Website and account pages"
MACHINES_NAME = "Slot machines"
BANNERS = {status.GREEN: "All systems operational", status.YELLOW: "Partial outage",
           status.RED: "Major outage"}
BANNERS_ZH = {status.GREEN: "监测中的机器运行正常", status.YELLOW: "部分机器异常",
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
@media (max-width:560px){.st-bar{gap:1px;height:30px}
.st-bar .d:nth-child(-n+60){display:none}.st-scale .d90{display:none}
.st-scale .d30{display:inline}.st-overall{margin-left:0;flex-basis:100%}
.st-list,.st-key{padding-left:16px;padding-right:16px}
.st-key ul{grid-template-columns:minmax(0,1fr)}
.st-performance{padding:18px 16px}
.st-perf-grid{grid-template-columns:minmax(0,1fr);gap:8px}
.st-perf-metric{padding:12px 14px}
.st-perf-metric dd{font-size:25px;margin:5px 0}}
"""


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
            f'un on, counted together; '
            'your own page says how yours is. A machine is down when '
            'it has not reported for '
            '5 minutes, or a check of the machine itself fails; '
            'degraded when it reports late or a '
            'check warns, such as a disk nearly full. Your own '
            'sign-in and usage limits are never '
            'counted against it.'
        ),
        (
            '<strong>槽位主机</strong> 汇总承载槽位的机器，'
            '不公开各用户的名称。你的机器状态见“我的槽位”。'
            '五分钟没有上报、或机器检查失败，判为离线；上报延迟、'
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


def performance_panel(report: Mapping[str, Any], now: float) -> str:
    """Only the publishable aggregate; suppressed states never disclose numbers."""
    ready = report.get("state") == "ready"
    latencies = report.get("latencies", {}) if ready else {}
    first = latencies.get("first_byte_ms", {})
    requests = report.get("requests") if ready else None
    completed = report.get("completed") if ready else None
    rate = (status.percent(completed, requests) if requests and completed is not None else "—")
    count = f"{requests:,}" if requests is not None else "—"
    cards = (
        ("Transfer completion", "传输完成率", label(rate, rate, inline=True),
         "How often a successful HTTP reply was fully delivered.",
         "记录中的尝试，有多少完整收到成功的 HTTP 回复。"),
        ("Average first response byte", "平均首字节等待时间", _latency_text(first),
         "How long a slot waited for the reply to start arriving.",
         "槽位等多久，才收到回复正文的开头。"),
        ("Observed relay attempts", "已记录的转发尝试", label(count, count, inline=True),
         "Each attempt is counted; a retry may add another.",
         "每次尝试各计一次；切换后重试可能多计一次。"),
    )
    grid = '<dl class="st-perf-grid">' + ''.join(
        '<div class="st-perf-metric"><dt>' + label(en, zh, inline=True)
        + '</dt><dd>' + value + '<p>' + label(help_en, help_zh, inline=True)
        + '</p></dd></div>' for en, zh, value, help_en, help_zh in cards
    ) + '</dl>'
    if ready:
        freshness = (
            '<p class="st-perf-note">' + label(
                "Oldest included report:", "最早的一份有效上报：", inline=True)
            + ' ' + _report_time(report["oldest_observed_at"], now) + '</p>'
            if report.get("oldest_observed_at") is not None else ''
        )
        note = '<p class="st-perf-note">' + label(
            "Only slots with recent, usable reports are included. This is part of Fleet traffic, "
            "not a measurement of every request. A dash means no publishable timing sample yet.",
            "仅统计近期上报且数据有效的槽位。这只覆盖部分 Fleet 流量，不代表每一次请求。"
            "横线表示暂时没有可公开的计时数据。",
            inline=True) + '</p>' + freshness
    else:
        message = ("No fresh transfer data yet", "暂时没有新的传输数据") if report.get(
            "state") == "no_data" else (
                "Waiting for enough aggregated data", "等待足够的汇总数据")
        note = '<p class="st-perf-note"><strong>' + label(*message, inline=True) + '</strong> '
        note += label(
            "Numbers appear only after enough recorded traffic from several different accounts. "
            "A dash means unknown, not zero.",
            "有多个不同账号参与，且记录量足够时，才展示数字。横线表示未知，不是零。",
            inline=True) + '</p>'
    timing_notes = ''
    if ready:
        for key, en, zh in (
                ("connect_ms", "Average upstream connection", "平均上游连接时间"),
                ("first_byte_ms", "Average first response body byte", "平均回复正文首字节时间"),
                ("total_ms", "Average full transfer", "平均完整传输时间")):
            sample = latencies.get(key, {})
            count = sample.get("count")
            samples = (
                label("More aggregate samples needed", "还需要更多汇总样本", inline=True)
                if count is None else label("No timing samples", "暂无计时样本", inline=True)
                if count == 0 else label(
                    f"{count:,} timing samples", f"{count:,} 个计时样本", inline=True)
            )
            timing_notes += (
                '<li>' + label(en + ': ', zh + '：', inline=True) + _latency_text(sample)
                + ' · ' + samples + '</li>'
            )
        timing_notes = '<ul>' + timing_notes + '</ul>'
    help_ = (
        '<details class="st-perf-help"><summary>'
        + label("How we measure this", "这些数字是怎么来的", inline=True) + '</summary>'
        + '<p>' + label(
            "These are slot-side relay attempts over the last seven UTC calendar days, including "
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
            "Traffic figures require at least 20 attempts from 3 different Fleet accounts. "
            "Each timing separately requires at least 20 samples from 3 different accounts, "
            "so a timing may remain unknown even when traffic totals can be shown.",
            "至少有 3 个不同 Fleet 账号、合计 20 次尝试，才公开流量数字。每项计时也必须单独达到"
            "至少 3 个不同账号、合计 20 个样本。因此，即使流量总数可以公开，某项计时仍可能未知。",
            inline=True) + '</p>'
        + '<p>' + label(
            "Only combined numbers are public. This panel shows no account names, slots, "
            "messages, or individual request records.",
            "公开页面只显示汇总数字，不展示账号名称、具体槽位、消息内容或单条请求记录。",
            inline=True) + '</p></details>'
    )
    return (
        '<section class="card st-performance" id="performance" aria-labelledby="performance-title">'
        '<h2 id="performance-title">' + label("Fleet performance", "Fleet 性能", inline=True)
        + '</h2><p class="sub">' + label(
            "Recent transfer measurements, combined across participating accounts.",
            "汇总参与统计账号的近期传输表现。", inline=True)
        + '<span class="st-perf-window">' + label(
            "Last 7 UTC calendar days, including today", "最近 7 个 UTC 日期，包含今天",
            inline=True)
        + '</span></p>' + grid + note + help_ + '</section>'
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


def page(store: object, cfg: Config, viewer: Optional[Viewer], now: float) -> str:
    """The whole page."""
    states, group = now_state(store, now)
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
        + performance_panel(performance.summary(store, cfg, now), now)
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
        + KEY
    )
    return _shell("status", body, REFRESH, STATUS_CSS, viewer=viewer)
