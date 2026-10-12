"""The pages a customer is sent: what ccfleet is, how to start, how it works, the terms.

Public, on the product site, beside /privacy. Written for somebody deciding
whether to buy a slot and then using one, not for an operator: nothing here
names a machine's address or a person, and every button it quotes is the label
the user site really shows.

The one fact that must never soften is the plan requirement: ccfleet sells a
machine, not Claude. Every person brings their own Claude plan and signs in to
it themselves; no page may suggest otherwise.
"""

from __future__ import annotations

from html import escape
from typing import Callable, Optional

from . import pricing, statuspage
from .bilingual import bi, label
from .config import Config
from .render import _meter
from .usersite import NAV, Viewer, _shell

#: When these pages last changed in substance. Change it with the words.
DOCS_UPDATED = "2026-10-11"

DOCS_CSS = """
/* A docs page's title block. */
.dochead{margin:4px 0 26px}
.dochead .lead{margin-bottom:0}
.lead{font-size:18px;line-height:1.6;margin:14px 0 26px;max-width:64ch}

/* The landing page: what it is and a way in, then what you get, then how to buy. */
.hero{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,.9fr);gap:48px;
align-items:center;padding:18px 0 8px}
.eyebrow{display:inline-flex;font-size:13px;font-weight:650;color:var(--acc);
background:var(--acc-soft);border:1px solid var(--acc-line);border-radius:999px;
padding:4px 12px;margin:0 0 18px}
/* Above the title: the eyebrow, and whether the service is up (statuspage.pill),
   side by side while they fit. */
.hero-top{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:0 0 18px}
.hero-top .eyebrow{margin:0}
.st-pill{font-size:13px;padding:4px 12px 4px 10px;text-decoration:none;white-space:nowrap}
.st-pill:hover{border-color:currentColor}
.hero h1{font-size:clamp(34px,4.6vw,54px);line-height:1.05;letter-spacing:-.034em;
font-weight:780}
.hero .lead{font-size:18.5px;margin:20px 0 28px}
.cta-row{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px}
.hero-demo{margin:0}
.demo-card{background:var(--panel);border:1px solid var(--rule);border-radius:18px;
overflow:hidden;box-shadow:0 1px 2px rgba(12,17,28,.05),0 30px 60px -30px rgba(12,17,28,.35)}
.demo-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
padding:16px 20px;border-bottom:1px solid var(--rule-soft)}
.demo-head b{font-family:var(--mono);font-size:17px}
.demo-body{padding:4px 20px 18px}
.demo-meta{font-size:13px;color:var(--muted);margin:12px 0 4px}
.demo-body .signed{margin:10px 0 2px}
.demo-body .rc{margin:4px 0 14px}
.demo-usage{padding:14px 16px 12px;border-radius:12px;background:var(--inset);
border:1px solid var(--rule-soft)}
.hero-demo figcaption{font-size:13px;color:var(--muted);margin:12px 4px 0;text-align:center}
.band{margin:56px 0 0}
.band>h2{font-size:26px;letter-spacing:-.024em;margin:0 0 8px}
.band-lead{font-size:16px;color:var(--muted);margin:0 0 22px;max-width:64ch}
.features{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.feature{background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
padding:20px;box-shadow:var(--shadow)}
.feature h3{margin:14px 0 6px}
.feature p{margin:0;color:var(--muted);font-size:14.5px;line-height:1.55}
svg.icon{display:block;width:42px;height:42px;padding:9px;border-radius:12px;
background:var(--acc-soft);color:var(--acc);fill:none;stroke:currentColor;stroke-width:1.7;
stroke-linecap:round;stroke-linejoin:round}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.two>.card{padding:6px 24px 20px}
.two>.card+.card{margin-top:0}
.two>.card>h2:first-child{margin-top:20px}
.callout{margin:16px 0 0;padding:12px 14px;border-radius:10px;background:var(--inset);
border:1px solid var(--rule-soft);color:var(--muted);font-size:14.5px}
.buy{list-style:none;counter-reset:buy;padding:0;margin:14px 0 0}
.buy>li{counter-increment:buy;position:relative;padding:2px 0 0 42px;margin:0 0 14px}
.buy>li::before{content:counter(buy);position:absolute;left:0;top:0;width:28px;height:28px;
border-radius:50%;background:var(--acc-soft);color:var(--acc);border:1px solid var(--acc-line);
font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center}
.next{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.next a{display:block;padding:18px 20px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--panel);text-decoration:none;color:var(--ink);box-shadow:var(--shadow)}
.next a:hover{border-color:var(--acc)}
.next b{display:block;color:var(--acc);margin-bottom:4px;font-size:15.5px}
.next span{color:var(--muted);font-size:14px;line-height:1.5}

/* The guide's steps, in order. */
.steps{list-style:none;counter-reset:step;padding:0;margin:0 0 26px}
.steps>li{counter-increment:step;position:relative;padding:18px 22px 16px 70px;
background:var(--panel);border:1px solid var(--rule);border-radius:var(--radius);
margin:0 0 12px;box-shadow:var(--shadow)}
.steps>li::before{content:counter(step);position:absolute;left:22px;top:18px;width:30px;
height:30px;border-radius:50%;background:var(--acc);color:var(--on-acc);font-weight:700;
font-size:14px;display:flex;align-items:center;justify-content:center}
.steps h3{margin:3px 0 4px;font-size:16.5px}
.steps p{margin:6px 0}

/* A button's label, quoted in the text, looks like the button it names. */
.btnlabel{display:inline-block;border:1px solid var(--rule);border-radius:7px;padding:0 7px;
font-size:.88em;font-weight:600;line-height:1.55;background:var(--panel);
box-shadow:0 1px 0 var(--rule);white-space:nowrap}

/* Document pages: each section a card, the text at a readable width. */
.doc .card,.how .card{padding:6px 24px 20px}
.doc .card>h2:first-child,.how .card>h2:first-child{margin-top:20px}
.card h3{margin:16px 0 6px}
.card[id]{scroll-margin-top:110px}
.connection-choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;
margin:18px 0}
.connection-choice{border:1px solid var(--rule);border-radius:12px;padding:4px 16px 12px;
background:var(--inset);min-width:0}
.connection-choice p{font-size:14px;line-height:1.65}
.doc .card .bi h2:first-child,.how .card .bi h2:first-child{margin-top:20px}
.doc .bi p,.how .bi p{overflow-wrap:anywhere}
.hero .bi>.copy-en+.copy-zh h1{font-size:clamp(28px,4vw,42px);line-height:1.2}
.how .diagram>.bi>.copy-en+.copy-zh{border-top:1px solid var(--rule);padding-top:12px}

/* How it works: the picture beside the words, where there is room for both. */
.how{display:grid;grid-template-columns:minmax(0,400px) minmax(0,1fr);gap:18px;
align-items:start}
.how .diagram{position:sticky;top:88px;margin:0}
.how-text .card+.card{margin-top:14px}
.diag{display:block;width:100%;max-width:420px;height:auto;margin:14px auto 10px}
.diag .box{fill:var(--panel);stroke:var(--rule);stroke-width:1.5}
.diag .you{stroke:var(--acc);stroke-width:2}
.diag .slot{fill:var(--acc-soft);stroke:var(--acc);stroke-width:2}
.diag .side{fill:var(--inset);stroke:var(--muted);stroke-width:1.2;stroke-dasharray:5 4}
.diag text{fill:var(--ink);font:650 15px var(--sans)}
.diag text.sub{fill:var(--muted);font:500 12.5px var(--sans)}
.diag .arrow{stroke:var(--acc);stroke-width:2;fill:none}
.diag .faint{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:4 4;fill:none}
.diag .head{fill:var(--acc)}
.diag .headfaint{fill:var(--muted)}

@media (max-width:980px){.hero{grid-template-columns:minmax(0,1fr);gap:30px}
.how{grid-template-columns:minmax(0,1fr)}.how .diagram{position:static}}
@media (max-width:640px){.features,.two,.next{grid-template-columns:minmax(0,1fr)}
.connection-choices{grid-template-columns:minmax(0,1fr)}
.band{margin-top:40px}.band>h2{font-size:22px}
.steps>li{padding:16px 16px 14px 58px}
.steps>li::before{left:16px;top:16px;width:28px;height:28px}
.lead{font-size:16.5px}.hero .lead{font-size:17px}}
"""

#: The pages, in the order the bar on top links them. The bar lives in the
#: user site's frame, so every page carries the same one.
PAGES_TITLES = NAV


def _cost(price: pricing.Price) -> str:
    """The price in a sentence, bold, escaped."""
    return f"<strong>{escape(pricing.display(price))}</strong> a {pricing.PERIOD}"


def _cost_zh(price: pricing.Price) -> str:
    return f"<strong>{escape(pricing.display(price))}</strong>／月"


def _contact_zh(cfg: Config) -> str:
    return contact(cfg) if cfg.contact_email else "向你提供此页面的运营者"


def contact(cfg: Config) -> str:
    """How to reach the operator, in the words a sentence needs.

    Slots are sold person to person, so without a published address the right
    answer is the person who sent the link; an address is shown only once the
    operator has chosen to publish one.
    """
    if cfg.contact_email:
        address = escape(cfg.contact_email)
        return f'<a href="mailto:{address}">{address}</a>'
    return "the person who sent you this page"


def _page(
    here: str,
    title: str,
    body: str,
    width: str = "doc",
    viewer: Optional[Viewer] = None,
    canonical: str = "",
) -> str:
    """A docs page with its current navigation link marked."""
    body += bi(
        '<p class="sub"><a href="/docs/library">HTML reference library</a> &mdash; '
        "architecture, operations and verification records.</p>",
        '<p class="sub"><a href="/docs/library">HTML 参考资料库</a>：架构、运维与按日期'
        "记录的验证结果。</p>",
    )
    return _shell(
        title, body, extra_css=DOCS_CSS, here=here, width=width, viewer=viewer, canonical=canonical
    )


# -- the overview -------------------------------------------------------------------


def _icon(paths: str) -> str:
    """A line icon in the text's own colour, so it follows the theme."""
    return (
        f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{paths}</svg>'
    )


ICONS = {
    "account": _icon(
        '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7.5 9.5l3 2.5-3 '
        '2.5M12.5 15h4"/>'
    ),
    "machine": _icon(
        '<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" '
        'width="17" height="6.5" rx="2"/><path d="M7 7.25h3M7 16.75h3"/><circle '
        'cx="16.5" cy="7.25" r=".9"/><circle cx="16.5" cy="16.75" r=".9"/>'
    ),
    "usage": _icon('<path d="M4 19v-8M9.33 19V6M14.67 19v-5M20 19V9"/>'),
}


def _demo() -> str:
    """The landing page's picture: a slot, as its holder's own page shows it.

    Built from the page's own parts rather than drawn, so it cannot drift from
    what somebody sees once they have a slot. Hidden from screen readers; the
    caption says what it shows.
    """
    return (
        '<figure class="hero-demo"><div class="demo-card" aria-hidden="true"><div '
        'class="demo-head"><b>slot-4821</b><span class="pill ok">'
        + label("In use", "使用中", inline=True)
        + "</span></div>"
        '<div class="demo-body">'
        + bi(
            '<p class="demo-meta">machine last heard 12s ago</p>',
            '<p class="demo-meta">机器在 12 秒前报告了状态</p>',
        )
        + bi(
            '<p class="signed">Signed in as <strong>alice@example.com</strong> &middot; '
            "Max 20x plan.</p>",
            '<p class="signed">已登录 <strong>alice@example.com</strong> &middot; Max 20x '
            "订阅。</p>",
        )
        + bi(
            '<p class="rc on">CC Fleet CLI is ready: run <strong>ccfleet local</strong>.</p>',
            '<p class="rc on">CC Fleet CLI 已就绪，运行 <strong>ccfleet local</strong> '
            "即可开始。</p>",
        )
        + '<div class="demo-usage">'
        + bi(
            _meter(34, "5-hour session", "at 11:40pm"),
            _meter(34, "五小时用量", "at 11:40pm").replace("resets at 11:40pm", "23:40 重置"),
        )
        + bi(
            _meter(61, "This week", "on Friday"),
            _meter(61, "本周用量", "on Friday").replace("resets on Friday", "周五重置"),
        )
        + "</div></div></div>"
        "<figcaption>"
        + bi(
            "An example of your page: the slot, your Claude account on it, and how much of "
            "your usage limits is left.",
            "槽位页面示例：查看登录的 Claude 账号，以及账号的用量。",
            inline=True,
        )
        + "</figcaption></figure>"
    )


def overview(
    cfg: Config,
    viewer: Optional[Viewer] = None,
    *,
    price: Optional[pricing.Price] = None,
    health: Optional[str] = None,
) -> str:
    """The site's front page: the bare address and /docs serve it alike, and
    it names the bare address as the one to keep. At its top, whether the
    service is up: ``health`` is the status page's banner level
    (statuspage.health), or None to say only "Status"."""
    # Straight into Google's sign-in when it is set up; otherwise the page that
    # says it is not, rather than a button that goes nowhere. Somebody already
    # signed in is offered their slots instead of a sign-in they have done, and
    # an operator the console too, which checks the role again itself.
    how = '<a class="btn big" href="/docs/how-it-works">How it works</a>'
    if viewer is not None:
        console = (
            f'<a class="btn big" href="{escape(viewer.console_href)}">Console</a>'
            if viewer.operator
            else ""
        )
        way_in = (
            '<div class="cta-row"><a class="btn primary big" '
            f'href="{escape(viewer.slots_href)}">Your slots</a>{console}{how}</di'
            f"v>"
        )
    else:
        sign_in = "/auth/google/start?next=/account" if cfg.google_ready else "/account"
        way_in = (
            f'<div class="cta-row"><a class="btn primary big" href="{sign_in}">'
            f"Sign in "
            f"with Google</a>{how}</div>"
            '<p class="muted small">Already have a slot? '
            '<a href="/account">Go to your slots</a>.</p>'
        )
    how_zh = '<a class="btn big" href="/docs/how-it-works">了解工作方式</a>'
    if viewer is not None:
        console_zh = (
            f'<a class="btn big" href="{escape(viewer.console_href)}">管理控制台</a>'
            if viewer.operator
            else ""
        )
        way_in_zh = (
            '<div class="cta-row"><a class="btn primary big" '
            f'href="{escape(viewer.slots_href)}">我的槽位</a>'
            f"{console_zh}{how_zh}</div>"
        )
    else:
        way_in_zh = (
            f'<div class="cta-row"><a class="btn primary big" href="{sign_in}">'
            f"使用 Google 登录</a>{how_zh}</div>"
            '<p class="muted small">已经有槽位？<a href="/account">'
            "查看我的槽位</a>。</p>"
        )
    way_in = bi(way_in, way_in_zh)
    # With a price the operator set, say it; without one, say it is agreed with them.
    paying = (
        f"A slot costs {_cost(price)}, paid directly to the operator; this "
        f"site takes "
        f"no "
        "card and no payment."
        if price is not None
        else "Slots are sold directly by the operator; price and payment are agreed with them."
    )
    paying_zh = (
        f"槽位费用为{_cost_zh(price)}，直接支付给运营者，本站不收集银行卡，也不代收款项。"
        if price is not None
        else "价格与付款方式直接和运营者约定。"
    )
    body = (
        '<section class="hero"><div class="hero-copy"><div class="hero-top">'
        + bi(
            '<p class="eyebrow">Bring your own Claude plan</p>',
            '<p class="eyebrow">使用你自己的 Claude 订阅</p>',
        )
        + statuspage.pill(health)
        + "</div>"
        + bi(
            "<h1>Local Claude Code, your own account on your slot</h1>",
            "<h1>Claude Code 在本地运行，账号留在你的槽位</h1>",
        )
        + bi(
            '<p class="lead">ccfleet gives you a <strong>slot</strong>: your own Linux '
            "account on a machine we run, with Claude Code installed and signed in to "
            "<em>your own</em> Claude account. Run <code>ccfleet local</code> for original "
            "Claude Code, files, tools and history on your computer, with supported model "
            "requests relayed through that slot. Plain <code>ccfleet</code> keeps the "
            "remote terminal available for earlier work.</p>",
            '<p class="lead">CC Fleet 为你提供一个<strong>槽位</strong>：我们托管的机器上的'
            "独立 Linux 用户环境，安装原版 Claude Code，并登录<em>你自己的</em> Claude "
            "账号。运行 <code>ccfleet local</code>，代码、工具、设置和会话记录都留在你的电"
            "脑；支持的模型请求经过你选择的槽位。需要继续以前的远程工作时，仍可使用 "
            "<code>ccfleet</code> 打开远程终端。</p>",
        )
        + way_in
        + "</div>"
        + _demo()
        + '</section><section class="band">'
        + bi("<h2>What you get</h2>", "<h2>你会获得什么</h2>")
        + bi(
            '<p class="band-lead">For anybody with a Claude plan that includes Claude Code '
            "who wants a dedicated account-bound slot for model access.</p>",
            '<p class="band-lead">适合已有 Claude Code 订阅，希望通过自己的专属账号环境使用'
            "模型的人。</p>",
        )
        + '<div class="features"><div class="feature">'
        + ICONS["account"]
        + bi("<h3>Your own Linux account</h3>", "<h3>独立的 Linux 用户环境</h3>")
        + bi(
            "<p>A slot home isolated from other slot users, with room for your tools. "
            "Administrators retain root access.</p>",
            "<p>槽位有自己的主目录，与其他槽位用户隔离，你可以安装自己的工具。机器管理员仍"
            "拥有 root 权限。</p>",
        )
        + '</div><div class="feature">'
        + ICONS["machine"]
        + bi("<h3>Your native local workflow</h3>", "<h3>熟悉的本地工作方式</h3>")
        + bi(
            "<p>Original Claude Code, local tools and native history; no project upload or "
            "filesystem mount.</p>",
            "<p>使用原版 Claude Code、本地工具和原生会话记录；无需上传整个项目，也不需要挂"
            "载文件夹。</p>",
        )
        + '</div><div class="feature">'
        + ICONS["usage"]
        + bi("<h3>One command anywhere</h3>", "<h3>一条命令开始工作</h3>")
        + bi(
            "<p>Run <code>ccfleet local</code> from your terminal. No manual SSH or proxy "
            "configuration, and no slot Claude credential copied to your computer.</p>",
            "<p>在终端运行 <code>ccfleet local</code>。无需手动设置 SSH "
            "或代理，也不会把槽位上的 Claude 登录凭证复制到你的电脑。</p>",
        )
        + '</div></div></section><section class="band two"><div class="card">'
        + bi("<h2>What you need</h2>", "<h2>开始前需要什么</h2>")
        + "<ul>"
        + "<li>"
        + bi(
            "Your own paid Claude plan that includes Claude Code: <strong>Pro, Max, Team "
            "or Enterprise</strong>. You sign that account in to your slot once; API keys "
            "do not replace that subscription sign-in.",
            "你自己的 Claude 付费订阅，并且包含 Claude Code：<strong>Pro、Max、Team 或 "
            "Enterprise</strong>。你需要亲自在槽位上登录该账号，普通 API key "
            "不能代替这一步。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi("A Google account, to sign in here.", "一个 Google 账号，用于登录本站。", inline=True)
        + "</li>"
        + "</ul>"
        + bi(
            '<p class="callout">ccfleet sells the machine, not Claude. Nobody else&#x27;s '
            "Claude account is ever shared with you, and yours is never shared with "
            "anybody.</p>",
            '<p class="callout">CC Fleet 提供托管的槽位环境，不出售 Claude '
            "订阅。你只能使用自己的 Claude 账号；我们不会把其他用户的账号分给你，也不会把你"
            "的账号分给其他用户。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>How to buy</h2>", "<h2>如何开通</h2>")
        + '<ol class="buy">'
        + "<li>"
        + bi(
            'First <a href="/account">sign in once</a> with Google, so there is an account '
            "to switch on.",
            '先用 Google <a href="/account">登录一次</a>，创建一个可以开通槽位的用户账号。',
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            f"Then ask {contact(cfg)} for a slot, and pay them. {paying}",
            f"然后联系 {_contact_zh(cfg)} 购买槽位。{paying_zh}",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            'Once they have switched it on, a <span class="btnlabel">Claim a slot</span> '
            "button is waiting on your page.",
            '开通后，你的页面会出现 <span class="btnlabel">领取槽位</span> 按钮。',
            inline=True,
        )
        + "</li>"
        + '</ol></div></section><section class="band">'
        + bi("<h2>Read next</h2>", "<h2>继续了解</h2>")
        + '<div class="next">'
        '<a href="/docs/guide"><b>'
        + label("Getting started", "开始使用", inline=True)
        + "</b><span>"
        + bi(
            "From buying to your first session, step by step.",
            "从开通槽位到第一次会话，按步骤开始。",
            inline=True,
        )
        + "</span></a>"
        '<a href="/docs/how-it-works"><b>'
        + label("How it works", "工作方式", inline=True)
        + "</b><span>"
        + bi(
            "Where your work runs, who can see what, and how it is kept up to date.",
            "工作在哪里运行、谁能看到内容，以及如何更新。",
            inline=True,
        )
        + '</span></a><a href="/privacy"><b>'
        + label("Privacy", "隐私说明", inline=True)
        + "</b><span>"
        + bi(
            "What we keep about you, why, and for how long.",
            "我们保存哪些信息、为什么保存，以及保存多久。",
            inline=True,
        )
        + '</span></a><a href="/status"><b>'
        + label("Status", "服务状态", inline=True)
        + "</b><span>"
        + bi(
            "Whether this site and the machines are up, now and over the last 90 days.",
            "查看网站和机器的当前状态，以及过去 90 天记录。",
            inline=True,
        )
        + "</span></a></div></section>"
    )
    return _page("/", "about", body, width="", viewer=viewer, canonical="/")


# -- getting started ----------------------------------------------------------------


def _model_connections(cfg: Config) -> str:
    """Account choices, shared examples and stable anchors."""
    base_url = escape((cfg.public_url or "https://ccfleet.daichenlab.com").rstrip("/"))
    choices = (
        (
            "Only one account",
            "只使用指定账号",
            "Select A only",
            "只选 A",
            "Every request uses A. Owning B does not add it automatically; if A is "
            "unavailable, this connection reports the error.",
            "每条请求只使用 A。即使你还持有 B，也不会自动使用它；A 不可用时，这个连接会报错。",
        ),
        (
            "Priority",
            "按优先级选择",
            "A first, B second",
            "A 为第一选择，B 为备选",
            "Each request prefers the highest-priority eligible slot. A safe failure can "
            "use B. After A recovers and its brief cooldown ends, a later request may use "
            "A again.",
            "每条请求优先选可用、排序最前的槽位。遇到可以安全切换的失败时才用 B；A "
            "恢复且短暂等待结束后，后续请求可能重新使用 A。",
        ),
        (
            "Sticky",
            "保持会话账号",
            "A first, B second; keep each conversation on one account",
            "A 优先、B 备选；同一会话尽量保持同一账号",
            "Once a normal response starts on B, later requests in that conversation "
            "prefer B while the assignment is remembered and B remains eligible. A new "
            "conversation starts from your priority order.",
            "如果安全切换后 B 开始正常回复，这个会话后续会优先使用 "
            "B，只要服务仍记得这个选择且 B 仍可用。新会话从你设置的优先顺序开始。",
        ),
    )
    cards = []
    for title, title_zh, selected, selected_zh, description, description_zh in choices:
        cards.append(
            '<div class="connection-choice"><h3>'
            + label(title, title_zh, inline=True)
            + '</h3><p class="muted">'
            + label(selected, selected_zh, inline=True)
            + "</p>"
            + bi(f"<p>{description}</p>", f"<p>{description_zh}</p>")
            + "</div>"
        )
    return (
        '<div class="card" id="model-connections">'
        + bi(
            "<h2>Choose the accounts your model connection may use</h2>",
            "<h2>选择模型连接可以使用的账号</h2>",
        )
        + bi(
            "<p>Think of a connection as a saved delivery address plus a key, and a slot "
            "as one signed-in account environment. The website manages these choices; the "
            "CLI is an alternative way to save them. You do not need to configure both.</p>",
            "<p>可以把模型连接理解为“保存的收件地址，加上一把钥匙”，槽位则是已经登录一个账"
            "号的环境。网页用于管理这些选择，CLI 也能完成配置；不需要把两种方式都操作一遍。"
            "</p>",
        )
        + '<div class="connection-choices">'
        + "".join(cards)
        + "</div>"
        + bi(
            '<p><strong>On the website:</strong> open <a href="/account#gateway">Your '
            "slots → Connect Claude</a>. Give A priority <code>1</code> and B priority "
            "<code>2</code>; leave any excluded slot blank. Priorities must be unique. "
            "Choose your routing mode, then save. Only your own held slots are selectable, "
            "and each needs a usable Claude sign-in before it can receive requests.</p>",
            '<p><strong>在网页上：</strong>打开<a href="/account#gateway">我的槽位 → '
            "连接 Claude</a>。只用 A 时，仅给 A 填 <code>1</code>；需要 B 备选时，再给 B 填 "
            "<code>2</code>。不想使用的槽位留空，排序数字不能重复。选择路由方式后保存。只能"
            "选择你自己持有的槽位，而且各槽位需要有效的 Claude 登录才能接收请求。</p>",
        )
        + bi(
            "<p><strong>In the CLI:</strong> after pairing, choose one of these examples. "
            "Replace A and B with your actual slot names. <code>--replace</code> is an "
            "explicit replacement of an existing saved connection; it creates a new "
            "key.</p>",
            "<p><strong>使用 CLI：</strong>完成配对后，按需求选择下面的一条命令，把 A、B "
            "换成真实槽位名称。<code>--replace</code> 表示主动替换已经保存的连接，并生成新"
            "密钥。</p>",
        )
        + "<pre><code>ccfleet gateway setup --slots A --replace\nccfleet gateway setup "
        "--slots A B --routing priority --replace\nccfleet gateway setup --slots A B "
        "--routing sticky --replace</code></pre>"
        + bi(
            "<p>For one launch, <code>ccfleet local --slot A</code> pins a slot already "
            "selected for that connection and disables backup switching for that launch. "
            "It cannot add an unselected slot or someone else&#x27;s account.</p>",
            "<p>只想让这次启动固定使用 A，可以运行 <code>ccfleet local --slot A</code>。A "
            "必须已经在这个连接的选择范围内；固定后，这次启动不会切到备选槽位，也不能借此使"
            "用未选中的槽位或其他用户的账号。</p>",
        )
        + bi(
            "<p><strong>Safe switching has limits.</strong> A known failure before "
            "sending, or a supported explicit upstream rejection marked safe to retry, can "
            "try the next selected slot. If sending has begun and the result is uncertain, "
            "the gateway stops instead of guessing. Once public response headers are sent, "
            "a broken stream ends without switching or replay, even before any text "
            "appears. An invalid connection key or malformed request needs correction, not "
            "another account.</p>",
            "<p><strong>切换只发生在确定安全时。</strong>请求发出前已确认的连接失败，或上游"
            "明确返回、且被标为可以重试的特定拒绝，可以尝试下一个已选槽位。如果已经开始发送"
            "，却不知道对方是否收到，网关会停止，不会猜测重发。一旦已向客户端发送回复头，即"
            "使屏幕还没出现文字，流中断时也不会换账号重发。连接密钥错误或请求格式错误，需要"
            "先修正问题，换账号不能解决。</p>",
        )
        + bi(
            "<p>Sticky selection needs a usable conversation identifier. Native Claude "
            "Code supplies supported identifiers; compatible clients can send "
            "<code>X-CCFleet-Session</code>. Without one, priority order applies. The "
            "gateway remembers an assignment in bounded runtime memory for up to 24 hours "
            "after a normal response starts; restarts, configuration changes or eviction "
            "can reset it. Switching does not move files, merge credentials or combine "
            "plan limits. Your conversation history stays local.</p>",
            "<p>保持会话账号需要可识别的会话标识。原版 Claude Code "
            "提供受支持的标识；兼容客户端可发送 <code>X-CCFleet-Session</code>。没有标识时"
            "按优先级选择。网关只在有限的运行时内存中保存选择，从正常回复开始起最多保留 24 "
            "小时；重启、配置变更或缓存淘汰可能重置它。切换不会搬动文件、合并登录凭证或叠加"
            "订阅配额，会话记录仍留在本地。</p>",
        )
        + "<h3>"
        + label("Understand the address", "理解连接地址", inline=True)
        + "</h3>"
        + f"<pre><code>Base URL: {base_url}\nPOST {base_url}/v1/messages</code>"
        f"</pre>"
        + bi(
            "<p>The Base URL is the service address saved by your client; saving it does "
            "not create a local server or store messages there. <code>/v1/messages</code> "
            "is the Messages API-compatible path on our service. The path name does not "
            "mean this hostname is owned by Anthropic. Our gateway receives the HTTPS "
            "request on BWH, selects your slot, and that slot connects to Anthropic. The "
            "connection key authenticates you to CC Fleet; it is not a Claude credential "
            "or an Anthropic paid API key.</p>",
            "<p>Base URL 是客户端保存的服务地址。保存地址不会创建本地服务器，也不表示消息存"
            "放在这里。<code>/v1/messages</code> 是我们服务上的 Messages API "
            "兼容路径；路径名称相同，不代表这个域名归 Anthropic 所有。HTTPS 请求先到 BWH "
            "的网关，由网关选槽位，再由槽位连接 Anthropic。连接密钥用于向 CC Fleet "
            "证明访问权限，不是 Claude 登录凭证，也不是 Anthropic 付费 API key。</p>",
        )
        + bi(
            "<p><strong>Privacy:</strong> your laptop encrypts the connection to BWH, but "
            "BWH decrypts it to route the request and can read model messages and replies. "
            "The slot keeps its native Claude credential. Local tools may include file "
            "contents in model context; keeping files local does not mean that no file "
            "content can leave the computer.</p>",
            "<p><strong>隐私边界：</strong>电脑到 BWH 的连接有加密，但 BWH "
            "为了路由会解密，因此可以读取模型消息和回复。原生 Claude "
            "凭证仍保存在槽位。工具可能把文件内容放入模型上下文；文件留在本地，不等于任何文"
            "件内容都不会离开电脑。</p>",
        )
        + bi(
            '<p>For diagrams and the comparison with CC Host, read the <a href="https://ccf'
            'leet-security-report-2026-10-09.cdcupt.chatgpt.site/?lang=en#host-comparison" '
            'target="_blank" rel="noopener noreferrer">current comparison report</a>. The '
            "original October 9 audit is historical; the current gateway update is dated "
            "October 11. The live verification used one actual account; multi-slot routing "
            "and failures were tested with controlled peers, not two live accounts. "
            'Technical details are in the <a href="/docs/library/docs/public-gateway.html">'
            "gateway reference</a>.</p>",
            '<p>路线图与 CC Host 对比见<a href="https://ccfleet-security-report-2026-10-09.'
            'cdcupt.chatgpt.site/?lang=zh#host-comparison" target="_blank" rel="noopener '
            'noreferrer">最新对比报告</a>。10 月 9 日的原始审计是历史记录，当前网关更新标注'
            "为 10 月 11 日。真实线上验证使用了一个实际账号；多槽位路由和故障切换使用受控测"
            '试，并非两个真实账号的线上实测。技术细节见<a href="/docs/library/docs/public-g'
            'ateway.html">网关参考指南</a>。</p>',
        )
        + "</div>"
    )


def _version_choices() -> str:
    return (
        '<div class="card" id="versions">'
        + bi("<h2>Three versions, three different updates</h2>", "<h2>三个版本，分别更新</h2>")
        + bi(
            "<p><strong>CC Fleet on your computer:</strong> run <code>ccfleet "
            "update</code> to update the Fleet launcher and connection helpers. This does "
            "not update either copy of Claude Code.</p>",
            "<p><strong>电脑上的 CC Fleet：</strong>运行 <code>ccfleet update</code>，更新"
            "启动器和连接辅助程序。这不会更新电脑或槽位上的 Claude Code。</p>",
        )
        + bi(
            "<p><strong>Claude Code on your computer:</strong> update your original local "
            "installation separately, outside a CC Fleet session. Existing sessions keep "
            "the process they started; open a new local session to use an updated "
            "executable.</p>",
            "<p><strong>电脑上的 Claude Code：</strong>在 CC Fleet "
            "会话之外，单独更新原版的本地安装。已经打开的会话继续使用原进程，更新后打开新的"
            "本地会话才使用新版。</p>",
        )
        + bi(
            '<p><strong>Claude Code on your slot:</strong> open <a href="/account">Your '
            "slots</a>, find the Claude Code row and choose <strong>Update to</strong> the "
            "offered version. This also selects the Latest channel. <strong>Back to "
            "Stable</strong> selects the stable channel instead. An operator-held version "
            "has no update button. New remote sessions use the new version; existing ones "
            "are not interrupted. Updating the slot does not change your local Claude "
            "Code.</p>",
            '<p><strong>槽位上的 Claude Code：</strong>打开<a href="/account">我的槽位</a>'
            "，在 Claude Code 一栏点击<strong>更新至</strong>提供的新版本，同时会切换为 "
            "Latest 渠道。<strong>返回稳定版</strong>则选择 Stable "
            "渠道。运营者固定的版本没有更新按钮。新远程会话使用新版，已有会话不被打断；更新"
            "槽位不会改变你电脑上的 Claude Code。</p>",
        )
        + "</div>"
    )


def _client_tools() -> str:
    """Local controls, distinct from remote commands."""
    body = (
        '<div class="card" id="client-tools">'
        + bi("<h2>Your everyday terminal controls</h2>", "<h2>日常终端操作</h2>")
        + bi("<h3>Choose a session and save defaults</h3>", "<h3>选择会话与保存偏好</h3>")
        + "<pre><code>ccfleet start\nccfleet sessions\nccfleet preferences set --model "
        "opus --effort high --mode plan\nccfleet preferences show\nccfleet preferences "
        "clear</code></pre>"
        + bi(
            "<p><code>ccfleet start</code> (also <code>ccfleet menu</code>) offers local "
            "new, continue and resume choices. <code>ccfleet sessions</code> opens the "
            "original Claude history picker; CC Fleet does not import or synchronize "
            "conversations. Preferences apply to new local sessions in this project, or "
            "use <code>--project PATH</code>. Explicit launch choices win; resuming keeps "
            "the saved model and effort unless changed deliberately. Clearing preferences "
            "does not clear history. Bare <code>ccfleet</code> still means the remote "
            "terminal; <code>ccfleet remote</code> selects it explicitly.</p>",
            "<p><code>ccfleet start</code>（也可写作 <code>ccfleet menu</code>）提供新建、"
            "继续和恢复本地会话的菜单。<code>ccfleet sessions</code> 打开 Claude "
            "原生历史选择器，CC Fleet 不会导入或同步会话。偏好用于当前项目的新本地会话，也"
            "可用 <code>--project PATH</code> 指定项目。启动时明确指定的选项优先；恢复会保"
            "留原有模型和推理强度，除非主动更改。清除偏好不会清除历史。单独运行 "
            "<code>ccfleet</code> 仍打开远程终端；<code>ccfleet remote</code> "
            "可以明确选择它。</p>",
        )
        + bi("<h3>Check readiness and get local help</h3>", "<h3>检查连接与导出诊断</h3>")
        + "<pre><code>ccfleet status --json\nccfleet doctor --privacy --json --export "
        "./ccfleet-support.json</code></pre>"
        + bi(
            "<p>These checks make no model request and do not scan a project. Doctor "
            "separates installation, pairing, broker/relay and account-health problems. "
            "The support report is written only to the new local file you request; it is "
            "not uploaded and does not include prompts, file contents, local paths, tokens "
            "or account email. Review it before sharing it yourself. A reported-ready "
            "heartbeat is an observation, not proof that Anthropic will accept the next "
            "request. Renewal pending, sign-in required, account maintenance and stale "
            "observations remain distinct; keep your pairing and use the existing sign-in "
            "controls when needed.</p>",
            "<p>这些检查不会调用模型，也不会扫描项目。Doctor 会区分安装、配对、转发连接和账"
            "号状态问题。诊断报告只写入你指定的新本地文件，不会上传，也不包含提示词、文件内"
            "容、本地路径、token 或账号邮箱。请先自行检查，再决定是否分享。机器报告“就绪”只"
            "是一次状态观察，不保证 Anthropic 会接受下一条请求。待续期、需要重新登录、账号"
            "维护和过期的状态信息会分别显示；请保留已有配对，必要时使用现有登录按钮。</p>",
        )
        + '<h3 id="simple-verification">'
        + bi("Simple readiness and response checks", "简单检查：连接就绪与基本回复", inline=True)
        + "</h3>"
        + bi(
            "<p>With the current native-local client, first run this in your normal "
            "terminal, not inside Claude:</p>",
            "<p>使用当前原生本地客户端时，先在普通终端中运行下面的命令，不要在 Claude "
            "会话中输入：</p>",
        )
        + "<pre><code>ccfleet local --check</code></pre>"
        + bi(
            "<p>This checks local Claude executable availability and the configured "
            "gateway or legacy bridge&#x27;s slot readiness. It does not scan projects, "
            "start a Claude session or make a model request. Readiness is not proof that "
            "Anthropic will accept the next request.</p>",
            "<p>它检查本地 Claude 是否可用，以及当前网关或旧桥接方式所需的槽位是否就绪。不"
            "扫描项目、不启动 Claude 会话，也不调用模型。“就绪”不等于 Anthropic "
            "一定会接受下一条请求。</p>",
        )
        + bi(
            "<p>For an optional basic response check, open <code>ccfleet local</code> and "
            "paste this prompt:</p>",
            "<p>如需额外检查基本回复，可以打开 <code>ccfleet local</code>，粘贴下面的提示：</p>",
        )
        + "<blockquote>"
        + bi(
            "<p>Do not use tools, read files, or inspect my system or network.<br>Reply "
            "only: <code>CCFLEET_OK</code></p>",
            "<p>不要使用工具、读取文件，或检查我的系统和网络。<br>只回复：<code>CCFLEET_OK<"
            "/code></p>",
        )
        + "</blockquote>"
        + bi(
            "<p>The optional prompt makes a model request and may use your plan allowance. "
            "A matching reply checks only basic responsiveness; it does not independently "
            "prove routing, anonymity or zero metadata disclosure. The no-tools "
            "instruction is not an enforced privacy safeguard. Native context or hooks can "
            "still exist, and context may include local OS or directory information. Do "
            "not ask Claude to print real IPs, locations or environment values just to "
            "check privacy: those outputs would enter the conversation. BWH sees "
            "connection metadata; the direct HTTPS gateway also reads model request and "
            "response contents. The explicit <code>--transport bridge</code> route keeps "
            "its inner SSH stream encrypted through the BWH byte broker. For relayed model "
            "requests, Anthropic sees the slot&#x27;s outbound IP. Other native/tool "
            "network connections are outside that statement.</p>",
            "<p>这条可选提示会调用模型，并可能消耗订阅额度。收到相同回复，只说明基本响应正"
            "常，不能单独证明路由、匿名性或没有元数据披露。“不要使用工具”是提示要求，不是强"
            "制的隐私隔离；原生上下文或 hooks 仍可能存在，上下文也可能包含本地系统和目录信"
            "息。不要为了验证隐私，让 Claude 打印真实 IP、位置或环境变量，否则这些内容会进"
            "入会话。BWH 能看到连接元数据，HTTPS 网关还能读取模型请求和回复；明确使用 "
            "<code>--transport bridge</code> 时，内部 SSH 模型流经过 BWH "
            "仍保持加密。对于转发的模型请求，Anthropic 看到的是槽位的出口 "
            "IP；这不涵盖其他原生服务或工具的联网。</p>",
        )
        + '<h3 id="management-mcp">'
        + bi(
            "Optional read-only management MCP (client 0.2.2)",
            "可选：只读管理 MCP（客户端 0.2.2 起）",
            inline=True,
        )
        + "</h3>"
        + bi(
            "<p>Run <code>ccfleet mcp serve</code> as a local stdio MCP server, or "
            'explicitly add <code class="code-wrap">claude mcp add --transport stdio '
            "ccfleet -- ccfleet mcp serve</code> to your original local Claude client. Use "
            "<code>/mcp</code> to review the tools. Nothing is registered automatically. "
            "The three tools report cached slot health, native quota percentages/reset "
            "times and numeric relay usage with observation age and sample coverage. "
            "Missing observations are unknown, not zero. Usage covers seven UTC calendar "
            "days, not billing or just today.</p>",
            "<p>可以将 <code>ccfleet mcp serve</code> 作为本地 stdio MCP 服务运行，或用 "
            '<code class="code-wrap">claude mcp add --transport stdio ccfleet -- ccfleet '
            "mcp serve</code> 主动添加到原版本地 Claude；然后用 <code>/mcp</code> "
            "查看工具。系统不会自动注册它。三个工具提供缓存的槽位状态、原生配额百分比与重置"
            "时间，以及数字化的转发用量，并标明观察时间和样本覆盖范围。没有记录表示未知，不"
            "表示零。用量覆盖七个 UTC 自然日，不是账单，也不只是今天的用量。</p>",
        )
        + bi(
            "<p>Enabling these tools intentionally makes their fixed health codes and "
            "numeric results available to the calling AI. They omit account/device "
            "identifiers, names, emails, IP addresses, local paths, prompts, logs and "
            "model/session identifiers. They only read status for one existing pairing; "
            "they cannot invoke Claude, refresh quota, scan projects, run commands or "
            "change account state. Pairing/account changes require a fresh MCP process. "
            "Remove it with <code>claude mcp remove ccfleet</code>.</p>",
            "<p>启用后，调用它的 AI 会看到固定状态码与数值结果。结果不含账号或设备标识、名"
            "称、邮箱、IP、本地路径、提示词、日志，以及模型或会话标识。工具只读取现有单个配"
            "对的状态，不能调用 Claude、刷新配额、扫描项目、执行命令或修改账号。更换配对或"
            "账号后需要重新启动 MCP。删除方式：<code>claude mcp remove ccfleet</code>。</p>",
        )
        + bi("<h3>Update or return to the previous client</h3>", "<h3>更新客户端或回退上一版</h3>")
        + "<pre><code>ccfleet version --json\nccfleet update\nccfleet update "
        "--rollback</code></pre>"
        + bi(
            "<p>The updater verifies a signed release channel against the client&#x27;s "
            "pinned Ed25519 key, then verifies an immutable manifest and every helper "
            "before atomic activation. A failed verification or download leaves the "
            "current client in place. Rollback selects the preserved previous client "
            "without changing pairing, local files or native history. A restored legacy "
            "bootstrap is not labelled signature-verified. The version report "
            "distinguishes bootstrap and verified releases; an absent or expired signed "
            "channel is an error, not permission to install unsigned code. These commands "
            "update CC Fleet, not Anthropic&#x27;s original Claude executable.</p>",
            "<p>更新程序会用客户端内置的 Ed25519 公钥验证发布渠道签名，再验证不可变的版本清"
            "单和每个辅助文件，全部通过后才切换版本。验证或下载失败时会保留当前客户端。回退"
            "使用保留的上一版，不改变配对、本地文件或原生历史。恢复旧版引导程序不会被标为“"
            "签名已验证”；版本报告会区分引导程序与已验证发布版。签名渠道缺失或过期时会报错"
            "，不会退而安装未签名代码。这里更新的是 CC Fleet 客户端，不是 Anthropic 的原版 "
            "Claude Code。</p>",
        )
        + '</div><div class="card" id="background-jobs">'
        + bi("<h2>Managed local background jobs</h2>", "<h2>受管理的本地后台任务</h2>")
        + '<pre><code>ccfleet jobs start --prompt "Review this project and summarize '
        'findings"\nccfleet jobs list\nccfleet jobs status JOB_ID\nccfleet jobs logs '
        "JOB_ID\nccfleet jobs logs JOB_ID --stream stderr\nccfleet jobs stop "
        "JOB_ID\nccfleet jobs archive JOB_ID</code></pre>"
        + bi(
            "<p>Copy the job ID returned by start. An equivalent launch form is <code "
            'class="code-wrap">ccfleet local --background --print "Summarize this '
            'project"</code>. This runs local Claude print-mode work under a supervisor '
            "and keeps its configured model connection alive after the starting terminal "
            "closes. Your computer must stay running and connected. It is not a remote "
            "job, native detached <code>--bg</code>, or an interactive session you can "
            "attach to. For saved context choose an explicit <code>--resume NAME_OR_ID</cod"
            "e> or <code>--continue</code>, not a history picker.</p>",
            '<p>保存 start 返回的任务 ID。也可以用 <code class="code-wrap">ccfleet local '
            '--background --print "Summarize this project"</code> 启动。程序会管理本地 '
            "Claude 的 print-mode 工作；关闭启动终端后，所需的连接仍会维持。你的电脑必须继"
            "续开机并联网。这不是远程任务，也不是原生脱离管理的 <code>--bg</code>，不能像交"
            "互会话那样重新 attach。要使用已有上下文，请明确指定 <code>--resume "
            "NAME_OR_ID</code> 或 <code>--continue</code>，不要使用历史选择器。</p>",
        )
        + bi(
            "<p>The default timeout is one hour; <code>--timeout SECONDS</code> allows at "
            "most 24 hours. The default concurrency limit is four, with <code>--max-jobs</c"
            "ode> allowing at most 16. Prompts, job specifications and output stay in "
            "private local files; each stdout/stderr log is capped at 4 MiB and can "
            "contain sensitive content. Logs are shown only when requested, not included "
            "in support exports. Jobs never automatically restart or replay failed "
            "inference.</p>",
            "<p>默认超时一小时；<code>--timeout SECONDS</code> 最长可设 24 "
            "小时。默认同时运行四个任务，<code>--max-jobs</code> 最多允许 16 "
            "个。提示、任务配置和输出保存在私有的本地文件中，每份 stdout/stderr 日志最多 4 "
            "MiB，可能含敏感内容。日志只有主动请求时才会显示，不会进入诊断导出。任务失败后"
            "不会自动重启或重新发送推理请求。</p>",
        )
        + bi(
            "<p>Stop, timeout, device revocation or supervisor loss ends the owned process "
            "group and connection. Authorization is checked periodically, not "
            "instantaneously. A sign-in/account transition or changed computer pairing "
            "stops old jobs; they do not silently continue under a different account. "
            "Shutdown is reported as confirmed only after cleanup is verified; an "
            "interrupted or unresponsive record is not proof it stopped. Tools that "
            "deliberately detach into another process group are not sandbox-contained. "
            "Native permissions still govern local data, including bypass mode. Job "
            "records are retained, not silently deleted. Archive an inactive, confirmed "
            "record with <code>ccfleet jobs archive JOB_ID</code> to keep its private "
            "files while freeing record capacity. An unconfirmed record requires "
            "<code>--acknowledge-unconfirmed</code>; archiving never claims its cleanup "
            "succeeded and never stops an active job.</p>",
            "<p>停止、超时、设备撤销或管理进程丢失，会结束所管理的进程组及连接。授权按周期"
            "检查，不是瞬时生效。登录或账号状态改变、电脑配对改变时，旧任务会停止，不会悄悄"
            "换账号继续。只有清理经过验证才会报告“已确认停止”；中断或无响应的记录不能证明任"
            "务已结束。工具若主动脱离进程组，不会因此受到沙箱限制；本地数据仍由原生权限决定"
            "，包括跳过确认的模式。任务记录不会自动删除。对已确认结束的任务运行 "
            "<code>ccfleet jobs archive JOB_ID</code>，可保留私有文件并释放记录名额。未确认"
            "结束的记录还需要 <code>--acknowledge-unconfirmed</code>；归档不代表清理成功，"
            "也不会停止活动任务。</p>",
        )
        + "</div>"
    )
    return body


def guide(
    cfg: Config, viewer: Optional[Viewer] = None, *, price: Optional[pricing.Price] = None
) -> str:
    paying = (
        f"and pay them: {_cost(price)}, paid directly to them; this site takes no card. They switch"
        if price is not None
        else "and pay them. They switch"
    )
    paying_zh = (
        f"槽位费用为{_cost_zh(price)}，直接支付给运营者，本站不收集银行卡。"
        if price is not None
        else "价格与付款方式直接和运营者约定。"
    )
    body = (
        '<div class="dochead">'
        + bi("<h1>Getting started</h1>", "<h1>开始使用</h1>")
        + bi(
            '<p class="lead">From buying a slot to your first Claude Code session. It '
            "takes a few minutes, most of which is the machine setting your slot up.</p>",
            '<p class="lead">从开通槽位到第一次使用 Claude Code，按下面几步操作即可。准备槽'
            "位通常需要几分钟。</p>",
        )
        + bi(
            '<p>Already using CC Fleet? Start with the <a href="#migration">migration '
            'guide</a>. For the native local workflow, see <a href="#project-workspaces">lo'
            "cal files and history</a>.</p>",
            '<p>已经在使用 CC Fleet？先看<a href="#migration">安装与迁移</a>。想了解本地文'
            '件和会话记录，请看<a href="#project-workspaces">本地工作方式</a>。</p>',
        )
        + '</div><ol class="steps">'
        + "<li>"
        + bi(
            '<h3>Sign in</h3><p>Open <a href="/account">your page</a> and choose <span '
            'class="btnlabel">Continue with Google</span>. That makes your account; it '
            "says you have no slots yet, which is expected.</p>",
            '<h3>登录本站</h3><p>打开<a href="/account">账号页面</a>，选择 <span '
            'class="btnlabel">使用 Google 继续</span>。首次登录会创建你的用户账号，此时没有'
            "槽位是正常的。</p>",
        )
        + "</li>"
        + "<li>"
        + bi(
            f"<h3>Get a slot</h3><p>Ask {contact(cfg)} for a slot, tell them the "
            f"Google "
            f"address you signed in with, {paying} your slot on for that "
            f"account.</p>",
            f"<h3>开通槽位</h3><p>联系 {_contact_zh(cfg)}，"
            f"告诉对方你登录本站所用的 Google 邮箱。{paying_zh} 开通后，"
            f"这个用户账号就可以领取槽位。</p>",
        )
        + "</li>"
        + "<li>"
        + bi(
            '<h3>Claim it</h3><p>Press <span class="btnlabel">Claim a slot</span>. The '
            'card shows <span class="pill busy">Setting up</span> while the machine '
            "creates your Linux account and installs Claude Code, which takes a few "
            "minutes. The page updates itself.</p>",
            '<h3>领取槽位</h3><p>点击 <span class="btnlabel">领取槽位</span>。机器会创建独'
            '立的 Linux 用户并安装 Claude Code；此时卡片显示 <span class="pill '
            'busy">正在准备</span>，页面会自动更新。</p>',
        )
        + "</li>"
        + "<li>"
        + bi(
            '<h3>Sign in to Claude</h3><p>When the card says <span class="pill warn">Ready '
            'to sign in</span>, press <span class="btnlabel">Sign in to Claude</span> and '
            "open the link it shows. Sign in with your own Claude account, copy the code "
            'Claude gives you, paste it into the box and press <span class="btnlabel">Send '
            'code</span>. The card turns <span class="pill ok">In use</span>.</p>',
            '<h3>登录你的 Claude 账号</h3><p>卡片显示 <span class="pill warn">可以登录</spa'
            'n> 后，点击 <span class="btnlabel">登录 Claude</span> '
            "并打开登录链接。用你自己的 Claude 账号完成登录，把 Claude "
            '提供的代码复制到输入框，再点击 <span class="btnlabel">提交代码</span>。完成后'
            '会显示 <span class="pill ok">使用中</span>。</p>',
        )
        + "</li>"
        + "<li>"
        + bi(
            '<h3>Connect this computer</h3><p>Run the <a href="#migration">one-command '
            "setup below</a>. It reuses an existing pairing. Only if it asks for a code, "
            'press <span class="btnlabel">Connect this computer</span> on your slot and '
            "paste the fresh single-use pairing code.</p>",
            '<h3>连接这台电脑</h3><p>运行下面的<a href="#migration">安装命令</a>。已配对的'
            "电脑会沿用原有连接；只有命令提示输入配对码时，才点击槽位上的 <span "
            'class="btnlabel">连接这台电脑</span>，粘贴新生成的一次性配对码。</p>',
        )
        + "</li>"
        + '</ol><div class="card" id="migration">'
        + bi("<h2>One-command setup and migration</h2>", "<h2>安装与迁移：使用同一条命令</h2>")
        + bi(
            "<p>New computer, already paired, or still using <code>ccfleet-connect</code>: "
            "use the same command. You do not need to choose a migration mode.</p>",
            "<p>新电脑、已经配对的电脑，或仍在使用 <code>ccfleet-connect</code> "
            "的电脑，都使用同一条命令，无需自己选择迁移模式。</p>",
        )
        + "<ol>"
        + "<li>"
        + bi(
            "<strong>Finish old work first.</strong> An old live-folder connector may "
            "still be running after its terminal closes. Finish that work before approving "
            "migration; do not assume a closed terminal stopped folder access.",
            "<strong>先结束旧工作。</strong>旧的文件夹连接器可能在终端关闭后仍继续运行。确"
            "认相关工作已经结束，再批准迁移；关闭终端不代表文件夹访问已经停止。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi("<strong>Run setup:</strong>", "<strong>运行安装命令：</strong>", inline=True)
        + "</li>"
        + "</ol><pre><code>curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/ma"
        'in/laptop/install.sh | bash -s -- --setup</code></pre><ol start="3">'
        + "<li>"
        + bi(
            "<strong>Follow the pairing prompt.</strong> An already-paired computer reuses "
            "its existing pairing; do not pair again. Only if this computer is not paired, "
            'open <a href="/account">your slot page</a>, choose <span class="btnlabel">Conn'
            "ect this computer</span>, and paste one fresh pairing code when asked. No SSH "
            "command or slot Claude credential is needed.",
            "<strong>按提示完成配对。</strong>已配对的电脑会沿用原有连接，不要重新配对。只"
            '有尚未配对时，才打开<a href="/account">槽位页面</a>，点击 <span class="btnlabe'
            'l">连接这台电脑</span>，输入新生成的配对码。无需自己运行 '
            "SSH，也无需复制槽位上的 Claude 凭证。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Approve old access cleanup only when ready.</strong> Setup asks in "
            "the controlling terminal before stopping old live-folder connectors and their "
            "associated remote live-folder sessions. This cancels pending work and "
            "invalidates open mount handles. Files and history are kept; ordinary remote "
            "tmux sessions are untouched. If you decline or cleanup fails, follow the "
            "reported instructions and retry. Adding <code>--yes</code> explicitly "
            "authorizes this cancellation.",
            "<strong>准备好后再清理旧的文件夹访问。</strong>安装程序会在终端询问你，确认后"
            "才停止旧连接器及其对应的远程文件夹会话。这会取消未完成的工作，并让已打开的挂载"
            "连接失效；文件、历史记录和普通远程 tmux 会话会保留。拒绝或清理失败时，按提示处"
            "理后重试。添加 <code>--yes</code> 表示你明确同意取消这些旧工作。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Wait for readiness.</strong> Existing native Claude is preserved; if "
            "missing, setup installs the original local CLI from the fixed vendor URL. "
            "Setup checks the configured route and waits for required slot access without "
            "uploading project files or making a model request. Old <code>ccfleet-connect</"
            "code> cleanup runs only after readiness succeeds.",
            "<strong>等待检查完成。</strong>现有的原版 Claude Code "
            "会保留；尚未安装时，程序会从固定的官方地址安装。本次检查只确认连接方式和所需的"
            "槽位访问，不上传项目，也不向模型发送消息。只有检查通过后，才清理旧的 "
            "<code>ccfleet-connect</code>。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Open a new terminal, then choose your project:</strong>",
            "<strong>打开新终端，再进入你的项目：</strong>",
            inline=True,
        )
        + "</li>"
        + "</ol><pre><code>cd ~/code/my-project\nccfleet start</code></pre>"
        + bi(
            "<p>Original Claude Code now runs on your computer with local files, tools, "
            "settings and history. When gateway access is available, a fresh pairing "
            "creates a separate endpoint key and model requests use HTTPS to our domain on "
            "BWH. Setup itself starts no Claude conversation. Choose new, continue or "
            "resume from the menu, or run <code>ccfleet local</code> directly.</p>",
            "<p>现在，原版 Claude Code 在你的电脑上运行，使用本地文件、工具、设置和会话记录"
            "。如果已开通网关，新配对会生成独立的连接密钥，模型请求通过 HTTPS 发往我们在 "
            "BWH 上的域名。安装本身不会发起 Claude 会话；在菜单中选择新会话、继续或恢复，也"
            "可直接运行 <code>ccfleet local</code>。</p>",
        )
        + bi(
            "<p>If a previously paired computer is still using the bridge, enroll the "
            "gateway with <code>ccfleet gateway setup</code>. Then use <code>ccfleet "
            'local</code> normally. On <a href="/account#gateway">Connect Claude</a>, '
            "select only one own slot for a fixed account, or several own slots in "
            "priority order with conversation affinity or safe failover. The endpoint key "
            "is separate from your Claude credential. The gateway needs no local HTTP "
            "listener or laptop-side SSH for model requests. Use <code>ccfleet local "
            "--transport bridge</code> to choose the legacy bridge explicitly. See the <a "
            'href="/docs/library/docs/public-gateway.html">public gateway guide</a> for '
            "routing and privacy.</p>",
            "<p>以前已配对、仍使用桥接方式的电脑，先运行 <code>ccfleet gateway "
            "setup</code> 配置 HTTPS 网关，然后照常运行 <code>ccfleet local</code>。在<a "
            'href="/account#gateway">连接 Claude</a>中，可以只选自己的一个槽位，也可以按顺序选'
            "择多个自己的槽位。连接密钥与 Claude 登录凭证是两回事。网关模式下，模型请求不需"
            "要本地 HTTP 服务，也不需要电脑端 SSH；如果要明确使用旧桥接方式，运行 "
            "<code>ccfleet local --transport bridge</code>。详细的路由与隐私说明见<a "
            'href="/docs/library/docs/public-gateway.html">网关参考指南</a>。</p>',
        )
        + bi(
            "<p>The installer verifies its digest-pinned helper. Pairing, configuration, "
            "slot sign-in and existing remote files stay in place. PATH changes preserve "
            "existing shell settings and keep a backup. Existing terminals keep their old "
            "environment; custom token exports outside the managed setup are not "
            "removed.</p>",
            "<p>安装程序会验证固定摘要的辅助程序。现有配对、配置、槽位登录和远程文件会保留"
            "；修改 PATH 时也会保留原有 shell 设置并创建备份。已打开的终端仍使用旧环境；安"
            "装程序不会清理你在管理范围之外自行导出的 token。</p>",
        )
        + bi(
            '<p>Optional: add <code>--name "Personal Mac"</code> after <code>--setup</code>'
            " to label a new device, or <code>--slot SLOT</code> to choose an existing "
            "pairing. Install-only, manual <code>ccfleet login</code>, and legacy "
            "<code>--migrate</code> remain compatibility options. Setup does not "
            "automatically revoke an Anthropic credential. Revoke an old setup-token "
            "yourself only if nothing else uses it.</p>",
            '<p>可选：在 <code>--setup</code> 后添加 <code>--name "Personal '
            'Mac"</code>，为新电脑设置名称；或使用 <code>--slot SLOT</code> '
            "选择已有配对。仅安装、手动 <code>ccfleet login</code> 和旧版 <code>--migrate</"
            "code> 仍可使用。安装不会自动撤销 Anthropic 凭证；只有确认其他地方已不再使用旧 "
            "token，才自行撤销它。</p>",
        )
        + bi(
            "<p><strong>Availability:</strong> inference relay access requires an "
            "upgraded, operator-enabled assigned slot. Installing the client or running "
            "the check does not activate access. <code>ccfleet local --check</code> sends "
            "no project files and makes no model request. A working old terminal or mount "
            "does not prove the relay is enabled; follow any readiness error before "
            "starting work.</p>",
            "<p><strong>访问条件：</strong>槽位必须已经升级，并由运营者开通模型转发。安装客"
            "户端或运行检查，不会自动开通访问。<code>ccfleet local --check</code> "
            "不发送项目文件，也不调用模型。旧远程终端或挂载能工作，并不代表模型转发已启用；"
            "请先处理检查中显示的问题。</p>",
        )
        + "</div>"
        + _model_connections(cfg)
        + _version_choices()
        + '<div class="card" id="project-workspaces">'
        + bi(
            "<h2>Local Claude, local files and history</h2>", "<h2>Claude、本地文件和会话记录</h2>"
        )
        + bi(
            "<p><code>ccfleet local</code> launches the original local Claude CLI. It does "
            "not upload a project, mount your filesystem on the slot, or require "
            "push/pull. There are no CC Fleet filesystem count/size caps or Git-ignore "
            "filters. Use <code>--project PATH</code> or <code>cd</code>; home works "
            "too:</p>",
            "<p><code>ccfleet local</code> 启动原版的本地 Claude Code，不会上传整个项目、把"
            "你的文件系统挂载到槽位，也不需要 push/pull。CC Fleet 不对本地文件数量、大小或 "
            "Git-ignore 规则另设限制。使用 <code>--project PATH</code> 或先 <code>cd</code>"
            " 进入目录；也可以在主目录开始：</p>",
        )
        + "<pre><code>cd ~\nccfleet local</code></pre>"
        + bi(
            "<p>The working directory is not a sandbox. Native Claude permissions govern "
            "local file and tool access, including outside that directory. New "
            "conversations default to <code>bypassPermissions</code>, Opus and max effort, "
            "so local tools can modify, delete or transmit data without individual "
            "approval prompts. Choose another mode when appropriate:</p>",
            "<p>工作目录不是安全沙箱。文件和工具权限由原版 Claude Code "
            "决定，也可能涉及该目录之外的文件。新会话默认使用 <code>bypassPermissions</code"
            ">、Opus 和 max effort，工具可能不逐次询问就修改、删除或传输数据。需要逐步确认"
            "时，请选择合适的权限模式：</p>",
        )
        + "<pre><code>ccfleet local --new --name work\nccfleet local --new --name "
        "research --mode plan --model opus --effort high\nccfleet local --resume\nccfle"
        "et local --resume work\nccfleet local --continue\nccfleet local --resume work "
        '--fork-session\nccfleet local --print "Summarize this project"</code></pre>'
        + bi(
            "<p>These are native local conversations, not remote tmux sessions. Running "
            "<code>ccfleet local</code> again starts a new conversation; <code>--new</code>"
            " makes that choice explicit. Existing history is kept. <code>--resume</code> "
            "uses Claude&#x27;s native history picker or an ID/name; <code>--continue</code"
            "> selects the last local conversation for this directory. Use <code>/model</co"
            "de> and <code>/effort</code> inside Claude. Resuming preserves the saved "
            "model and effort unless you request changes. Additional supported native "
            "arguments go after <code>--</code>; routing/authentication overrides are not "
            "alternate CC Fleet modes.</p>",
            "<p>这些是本地原生会话，不是远程 tmux 会话。再次运行 <code>ccfleet "
            "local</code> 会新建会话；<code>--new</code> 明确表示新建，原有历史不会删除。<c"
            "ode>--resume</code> 打开 Claude 的历史选择器，或按 ID/名称恢复；<code>--contin"
            "ue</code> 继续当前目录最近的本地会话。会话内可使用 <code>/model</code> 和 "
            "<code>/effort</code>。恢复时会保留原有模型和推理强度，除非你主动修改。其他受支"
            "持的原生参数放在 <code>--</code> 后；覆盖路由或登录参数并不是 CC Fleet "
            "的另一种模式。</p>",
        )
        + bi(
            "<p>Foreground interactive sessions, <code>--print</code>, native resume and "
            "multiple normal terminal sessions are supported. Native <code>--bg</code> / "
            "<code>--background</code> is explicitly rejected when forwarded after "
            '<code>--</code>: it would bypass supervision. Use the <a href="#background-job'
            's">managed local background jobs</a> below, or another regular terminal for '
            "parallel interactive work.</p>",
            "<p>支持前台交互、<code>--print</code>、原生恢复，以及在多个普通终端中同时工作"
            "。通过 <code>--</code> 转发原生 <code>--bg</code> 或 <code>--background</code>"
            ' 会被拒绝，因为这样会绕过进程管理。请使用下方的<a href="#background-jobs">受管'
            "理的本地后台任务</a>，或另开普通终端进行交互工作。</p>",
        )
        + bi("<h3>Keep your native settings and history</h3>", "<h3>保留原生设置与历史</h3>")
        + bi(
            "<p>The default uses native Claude settings/history, including an existing "
            "<code>CLAUDE_CONFIG_DIR</code>. Nothing is imported or deleted. To reopen an "
            'earlier per-slot relay-preview profile, use <code class="code-wrap">ccfleet '
            "local --legacy-history --resume</code>. This selects that old local profile; "
            "it does not merge it into native history. Old remote history remains on the "
            "slot.</p>",
            "<p>默认沿用 Claude 原生设置与会话记录，包括已有的 <code>CLAUDE_CONFIG_DIR</cod"
            "e>，不会导入或删除历史。如果要打开以前按槽位保存的预览版配置，使用 <code "
            'class="code-wrap">ccfleet local --legacy-history --resume</code>。这只是选择旧'
            "配置，不会把旧记录合并到原生历史中；远程会话记录仍在槽位上。</p>",
        )
        + bi(
            "<p>A temporary private settings overlay pins the selected model route and "
            "authentication, using the remote HTTPS gateway or the legacy loopback bridge, "
            "clears conflicting provider/authentication overrides, and disables supported "
            "optional telemetry. Ordinary customizations remain; native settings files are "
            "not permanently rewritten.</p>",
            "<p>启动时会创建临时的私有设置层，固定当前模型连接地址和认证信息，清除冲突的供"
            "应商或登录覆盖项，并关闭受支持的可选遥测。HTTPS 模式使用远程网关地址；旧桥接模"
            "式使用本地回环地址。常规自定义设置会保留，原生设置文件不会被永久改写。</p>",
        )
        + bi("<h3>Stopping and recovering older work</h3>", "<h3>结束会话与恢复旧工作</h3>")
        + bi(
            "<p>Quit a new local session normally with <code>/exit</code>, then use native "
            "resume later. A closed terminal does not create a persistent remote version "
            "of this local agent. Interrupted inference is not silently replayed by CC "
            "Fleet.</p>",
            "<p>用 <code>/exit</code> 正常退出本地会话，以后通过原生恢复功能继续。关闭终端"
            "不会把本地会话变成持续运行的远程任务。CC Fleet 不会悄悄重新发送已经中断的模型"
            "请求。</p>",
        )
        + bi(
            "<p><code>ccfleet local --disconnect</code> is only for explicitly cleaning up "
            "an old live-folder grant and its associated remote sessions. It can cancel "
            "pending work. <code>--reset-link</code> is retired and provides migration "
            "guidance. Old <code>ccfleet project</code> commands remain for deliberate "
            "snapshot recovery, not for the current local workflow.</p>",
            "<p><code>ccfleet local --disconnect</code> 只用于主动清理旧的文件夹访问授权及"
            "关联的远程会话，可能取消未完成的工作。<code>--reset-link</code> "
            "已停用，会显示迁移指引。旧的 <code>ccfleet project</code> "
            "命令仅用于主动恢复历史快照，不属于当前本地工作方式。</p>",
        )
        + bi("<h3>Routing and privacy limits</h3>", "<h3>哪些内容会经过网关</h3>")
        + bi(
            "<p>The relay removes selected headers and the top-level structured "
            "<code>metadata</code> field before forwarding. It does not redact arbitrary "
            "prompts or tool results. Native system prompts may include local OS, working "
            "directory and environment details; files and paths may identify you and reach "
            "the slot and Anthropic. This is not a fingerprint-free or zero-metadata "
            "guarantee.</p>",
            "<p>转发时会移除部分请求头，以及顶层结构化的 <code>metadata</code> "
            "字段；不会自动删去提示词或工具结果中的任意个人信息。原生系统提示可能包含本地操"
            "作系统、工作目录和环境信息；文件内容与路径也可能识别你的身份，并到达槽位和 "
            "Anthropic。这里没有“零元数据”或“没有指纹”的保证。</p>",
        )
        + bi(
            "<p>Only supported model endpoints use the relay. MCP servers, hooks, plugins, "
            "shell tools, updates and other native CLI services can connect directly from "
            "the laptop. Optional telemetry controls are not an all-traffic firewall.</p>",
            "<p>只有受支持的模型接口走转发路径。MCP 服务、hooks、插件、shell "
            "工具、更新和其他原生服务，仍可能从你的电脑直接联网。可选遥测设置不是拦截所有网"
            "络访问的防火墙。</p>",
        )
        + bi(
            "<p>The direct HTTPS gateway terminates TLS on BWH, so <strong>BWH can read "
            "model requests and responses</strong> before forwarding them to your selected "
            "slot. Relevant prompts, tool results and file contents reach BWH, the slot "
            "and Anthropic. Only the explicit <code>--transport bridge</code> route keeps "
            "the model stream inside laptop-to-slot SSH: its BWH byte broker sees your "
            "incoming IP address and connection metadata but cannot decrypt the inner SSH "
            "model stream. SSH exposes client version; remote-terminal sessions also send "
            "terminal dimensions. Slot administrators have root and can inspect or alter "
            "relayed requests and responses, which can influence local tool actions. Trust "
            "and native local permissions remain important. Each paired device remains "
            "separately revocable.</p>",
            "<p>HTTPS 网关在 BWH 上结束 TLS 连接，因此<strong>BWH 可以读取模型请求和回复</s"
            "trong>，再转发给你选择的槽位。消息中包含的提示、工具结果和文件内容会到达 "
            "BWH、槽位和 Anthropic。只有明确选择 <code>--transport bridge</code>，消息才始"
            "终位于电脑到槽位的 SSH 加密流中；这时 BWH 的字节转发服务能看到连接 IP "
            "与元数据，但无法解密内部的 SSH 模型消息。SSH 会暴露客户端版本，远程终端还会发"
            "送终端尺寸。槽位管理员拥有 root 权限，能查看或修改请求和回复，进而影响本地工具"
            "行为。你仍需要信任服务方，并选择合适的本地权限。每台已配对的电脑都可以单独撤销"
            "。</p>",
        )
        + "</div>"
        + _client_tools()
        + '<div class="card">'
        + bi("<h2>Remote-terminal compatibility</h2>", "<h2>继续使用远程终端</h2>")
        + bi(
            "<p>The commands below run on the slot, not in a local Claude conversation.</p>",
            "<p>下面的操作在槽位上执行，不是本地 Claude 会话。</p>",
        )
        + "<ul>"
        + "<li>"
        + bi(
            "<strong>The command.</strong> Run <code>ccfleet</code> in a normal terminal. "
            "It opens the original Claude Code interface running in your slot. You never "
            "type an SSH command and your computer receives no Claude credential.",
            "<strong>打开终端。</strong>在普通终端运行 <code>ccfleet</code>，就会打开槽位上"
            "的原版 Claude Code。无需手动输入 SSH，电脑也不会收到 Claude 凭证。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Where work runs.</strong> Claude Code, its shell tools and model "
            "requests run on the slot for this compatibility command. Files live in its "
            "remote workspace; use <code>ccfleet local</code> for local Claude and local "
            "files.",
            "<strong>工作在哪里运行。</strong>这种兼容方式的 Claude Code、shell "
            "工具和模型请求都在槽位运行，文件在远程工作目录中。要使用本地 Claude "
            "与本地文件，请运行 <code>ccfleet local</code>。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Permission mode.</strong> Hosted sessions start with permission "
            "prompts off. Claude can run tools without asking as your slot user. The slot "
            "has no sudo, but Claude can read, change, delete or send anything that user "
            "can access. Start a named session with another mode using <code>ccfleet new "
            "research --mode plan</code>; later use <code>ccfleet attach --session "
            "research</code>. To change a running session deliberately, use <code "
            'class="code-wrap">ccfleet restart --session research --mode auto</code>.',
            "<strong>权限模式。</strong>托管会话默认跳过权限确认，Claude "
            "会以槽位用户身份执行工具。槽位没有 sudo，但 Claude "
            "可以读取、修改、删除或发送这个用户能访问的内容。用 <code>ccfleet new research "
            "--mode plan</code> 新建其他模式的会话，之后用 <code>ccfleet attach --session "
            'research</code> 恢复。需要主动替换正在运行的进程时，使用 <code class="code-wra'
            'p">ccfleet restart --session research --mode auto</code>。',
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Model and effort.</strong> The default is Opus at max effort. For a "
            'new named session, choose both at launch: <code class="code-wrap">ccfleet new '
            "research --model fable --effort xhigh</code>. In a running Claude Code "
            "session, use <code>/model</code> or <code>/effort</code> to change it without "
            "losing the conversation.",
            "<strong>模型与推理强度。</strong>默认为 Opus、max effort。新建命名会话时可指定"
            ' <code class="code-wrap">ccfleet new research --model fable --effort '
            "xhigh</code>。会话中用 <code>/model</code> 或 <code>/effort</code> "
            "调整，不必丢弃对话。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Projects.</strong> Keep projects in <code>~/workspace</code> on the "
            "slot. Clone with Git or fetch them from another service from inside Claude "
            "Code. CC Fleet does not silently upload or mount files from the computer "
            'where you run the client. For local Claude instead, see <a href="#project-work'
            'spaces">local files and history</a>.',
            "<strong>项目文件。</strong>项目放在槽位的 <code>~/workspace</code> 中，可通过 "
            "Git 克隆，或在 Claude Code 中从其他服务获取。CC Fleet "
            '不会悄悄上传或挂载你运行客户端的电脑文件。需要本地方式，请看<a href="#project-'
            'workspaces">本地文件与历史</a>。',
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Reconnect.</strong> Claude Code runs inside a persistent session. If "
            "Wi-Fi changes, the laptop sleeps or the terminal closes, run <code>ccfleet</co"
            "de> again and it reattaches to the same session.",
            "<strong>重新连接。</strong>远程 Claude Code 在持久会话中运行。Wi-Fi "
            "改变、电脑休眠或终端关闭后，再运行 <code>ccfleet</code> 会连接回同一会话。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            '<strong>More computers.</strong> Press <span class="btnlabel">Connect another '
            "computer</span> for each device. Each receives its own key and can be removed "
            "from your slot page without changing your Claude sign-in.",
            '<strong>连接其他电脑。</strong>每台设备分别点击 <span class="btnlabel">连接另'
            "一台电脑</span>。每台电脑有自己的密钥，之后可以单独在槽位页面撤销，不必更改 "
            "Claude 登录。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Your usage.</strong> The 5-hour and weekly bars cover the whole "
            "Claude account. Beside them, the token count covers what Claude Code used on "
            "this slot. That slot token count is not a complete local-relay usage meter. "
            'The limits are read every five minutes; <span class="btnlabel">Refresh</span> '
            "reads them now.",
            "<strong>查看用量。</strong>五小时和每周进度条表示整个 Claude "
            "账号的用量；旁边的 token 数表示这个槽位上的 Claude Code "
            "使用量，不是完整的本地转发用量统计。配额每五分钟读取一次，点击 <span "
            'class="btnlabel">刷新</span> 可立即读取。',
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            '<strong>Giving it back.</strong> Tick the box and press <span class="btnlabel"'
            ">Give this slot back</span>. Your Linux account and every file in it are "
            "deleted; your Claude account is not touched. Push your work somewhere first.",
            '<strong>归还槽位。</strong>勾选确认框，再点击 <span class="btnlabel">归还这个'
            "槽位</span>。这会删除 Linux 用户及其所有文件，不会删除你的 Claude "
            "账号。请先推送或备份远程工作。",
            inline=True,
        )
        + "</li>"
        + '</ul></div><div class="card">'
        + bi("<h2>One Claude account per slot</h2>", "<h2>每个槽位对应一个 Claude 账号</h2>")
        + bi(
            "<p>A slot is signed in to one Claude account, your own, and keeps it: <span "
            'class="btnlabel">Sign in again</span> works with that account only. To move '
            'your slot to another Claude account of yours, press <span class="btnlabel">Cha'
            "nge account</span> (once a week). To use two accounts at once, hold two "
            "slots. For the legacy bridge: Each local CC Fleet profile is bound to exactly "
            "one slot. A current HTTPS endpoint may select your own slot pool, while each "
            "request uses just one bound account.</p>",
            '<p>槽位只绑定你自己的一个 Claude 账号；<span class="btnlabel">重新登录</span> '
            '只能登录原账号。要换成另一个自己的账号，使用 <span class="btnlabel">更换账号</'
            "span>，每周一次。需要同时保留两个账号，就持有两个槽位。旧桥接配置仍固定单个槽"
            "位；HTTPS 模型连接则可以选择自己的一个或多个槽位，每次请求仍只使用一个槽位的账"
            "号。</p>",
        )
        + bi(
            "<p>One account also stays on one machine: signed in on two at once, it is "
            "flagged to you and to the operator.</p>",
            "<p>同一个账号也应保持在同一台机器上；同时在两台机器登录时，会向你和运营者显示"
            "提示。</p>",
        )
        + bi(
            "<p>Your slot is a Linux account with a name of its own: a neutral one like "
            "slot-4821 when you claim it, never anything from your address, and whatever "
            "you rename it to on your page. CC Fleet shows that name on every paired "
            "computer, and the slot uses it as its hostname, so Anthropic can see it: pick "
            "anything but your email address. When you give it back, the name goes with "
            "it.</p>",
            "<p>领取时，槽位会使用类似 slot-4821 的中性 Linux 用户名，不从你的邮箱生成。之"
            "后可在页面重命名。配对的电脑会显示这个名称，槽位也把它用作 hostname，因此 "
            "Anthropic 可能看到它，请不要用邮箱当名称。归还槽位时，这个名称也随之删除。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Changing to another Claude account</h2>", "<h2>更换槽位绑定的 Claude 账号</h2>")
        + bi(
            '<p>On a slot in use, <span class="btnlabel">Change account</span> moves it to '
            "another Claude account of yours. Open the link it shows, sign in with the "
            "account the slot should use from now on, and paste the code as you did the "
            "first time. Your files and settings stay; only the Claude sign-in changes.</p>",
            '<p>在使用中的槽位点击 <span class="btnlabel">更换账号</span>，可以换成另一个你'
            "自己的 Claude 账号。打开链接，用以后要使用的账号登录，再像第一次一样粘贴代码。"
            "文件与设置保留，只更换 Claude 登录。</p>",
        )
        + bi(
            "<p>Until that sign-in finishes, the slot keeps its current account; if it "
            "does not go through, or you sign in with the account the slot already has, "
            "the account does not change. Once it finishes, the slot&#x27;s Claude "
            "sessions end so they no longer keep the old account; plain <code>ccfleet</code"
            "> opens a new remote session. Connected computers stay paired because their "
            "CC Fleet keys are separate from the Claude credential.</p>",
            "<p>新登录完成前，槽位继续绑定原账号；登录失败或仍登录同一账号时，不会改变绑定"
            "。完成后，槽位上的 Claude 会话会结束，防止继续使用旧账号；运行 <code>ccfleet</"
            "code> 会打开新的远程会话。电脑配对仍然保留，因为 CC Fleet 密钥与 Claude "
            "凭证是独立的。</p>",
        )
        + bi(
            "<p>For <code>ccfleet local</code>, CC Fleet does not automatically close your "
            "local Claude process or delete its history. The relay checks the slot&#x27;s "
            "account for every request and cancels an in-flight request if that account "
            'changes. Wait until the slot is <span class="pill ok">In use</span> again '
            "before retrying deliberately; new requests then use its current bound "
            "account. CC Fleet does not silently replay interrupted requests.</p>",
            "<p>对于 <code>ccfleet local</code>，CC Fleet 不会自动关闭本地 Claude "
            "进程，也不会删除历史。转发服务每次请求都会检查槽位账号；账号改变时，会取消正在"
            '进行的请求。等槽位重新显示 <span class="pill ok">使用中</span>，再主动重试；新'
            "的请求使用当前绑定账号。CC Fleet 不会悄悄重发被中断的请求。</p>",
        )
        + bi(
            "<p>A slot can change account once a week; after a change, your slot card says "
            "when it can change again. An account change is visible to the operator; <a "
            'href="/privacy">the privacy page</a> explains which account information is '
            "reported to CC Fleet.</p>",
            "<p>每周只能更换一次账号，卡片会显示下次可更换的时间。运营者能看到账号更换；哪"
            '些账号信息会报告给 CC Fleet，见<a href="/privacy">隐私说明</a>。</p>',
        )
        + '</div><div class="card">'
        + bi("<h2>Keeping Claude Code up to date</h2>", "<h2>更新槽位上的 Claude Code</h2>")
        + bi(
            "<p>Your slot card has a <strong>Claude Code</strong> row: the version your "
            "slot runs and, when Anthropic has published a newer one, its number. The "
            "button beside it, <strong>Update to</strong> and that number, installs it "
            "now. Your slot then follows Anthropic&#x27;s latest release and keeps itself "
            "current from then on.</p>",
            "<p>槽位卡片的 <strong>Claude Code</strong> 一栏会显示当前版本和可用的新版本。"
            "点击 <strong>更新至</strong>及版本号，会立即安装新版，并切换为 Latest "
            "渠道，之后自动跟随 Anthropic 的最新发布。</p>",
        )
        + bi(
            "<p>A session that is open keeps running on the version it started with; new "
            "sessions start on the new one.</p>",
            "<p>已经打开的远程会话继续使用启动时的版本；新会话才使用新版。</p>",
        )
        + bi(
            '<p>To go back to Anthropic&#x27;s stable release, press <span class="btnlabel"'
            ">Back to Stable</span>. A version <strong>held by the operator</strong> has "
            "been fixed on purpose, for example while a release misbehaves, and there is "
            "nothing to press.</p>",
            '<p>要切回 Anthropic 的稳定渠道，点击 <span class="btnlabel">返回稳定版</span>'
            "。如果显示<strong>由运营者固定版本</strong>，表示版本被主动锁定，例如避开有问"
            "题的发布，此时没有更新按钮。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Model and effort</h2>", "<h2>模型与推理强度</h2>")
        + bi(
            "<p>Claude Code on your slot starts on <strong>Opus</strong> at <strong>max "
            "effort</strong>, so it thinks as hard as it can about everything you ask. "
            "That also uses your Claude plan&#x27;s limits fastest; the bars on your slot "
            "card show how fast.</p>",
            "<p>槽位上的 Claude Code 默认使用 <strong>Opus</strong> 和 <strong>max "
            "effort</strong>。更高的推理强度通常消耗更多订阅额度；可以在卡片上观察用量。</p"
            ">",
        )
        + bi(
            "<p>Inside a running session, <code>/model</code> changes models and "
            "<code>/effort</code> changes effort without ending the conversation. At "
            'launch, use <code class="code-wrap">ccfleet new NAME --model fable --effort '
            "xhigh</code>. Model choices include Opus, Fable and Sonnet when your Claude "
            "account offers them. Effort choices are low, medium, high, xhigh (extreme "
            "high), max and ultracode. Ultracode combines xhigh with Claude Code workflow "
            'orchestration; it is not an "extreme max" level.</p>',
            "<p>会话内使用 <code>/model</code> 或 <code>/effort</code> "
            '修改，无需结束对话。远程启动时可使用 <code class="code-wrap">ccfleet new NAME '
            "--model fable --effort xhigh</code>。账号支持时可选择 Opus、Fable 或 "
            "Sonnet；推理强度包括 low、medium、high、xhigh、max 和 ultracode。Ultracode "
            "组合 xhigh 与 Claude Code 工作流编排，不是比 max 更高的单一推理等级。</p>",
        )
        + bi(
            "<p><code>ccfleet attach --session NAME</code> resumes that named session. Use "
            '<code class="code-wrap">ccfleet restart --session NAME --model opus --effort '
            "max</code> only when you deliberately want to end its current Claude process "
            "and replace it.</p>",
            "<p><code>ccfleet attach --session NAME</code> 恢复指定的远程会话。只有要主动结"
            '束原进程并替换它时，才使用 <code class="code-wrap">ccfleet restart --session '
            "NAME --model opus --effort max</code>。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Good to know</h2>", "<h2>使用小贴士</h2>")
        + "<ul>"
        + "<li>"
        + bi(
            "Your slot is <strong>not backed up</strong>. Keep your work in git, or "
            "anywhere else that is yours.",
            "槽位<strong>没有备份</strong>，请把远程工作保存在 Git 或你自己的存储中。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "You can install tools in your home directory; there is no administrator "
            "access (sudo) in a slot.",
            "可以在主目录安装工具，但槽位没有管理员权限（sudo）。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "If Claude asks you to sign in again, your slot card offers <span "
            'class="btnlabel">Sign in again</span>.',
            '需要重新登录 Claude 时，卡片会提供 <span class="btnlabel">重新登录</span>。',
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<code>ccfleet</code> cannot connect? Check that the slot says <span "
            'class="pill ok">In use</span>, then rerun the setup command. If it still '
            "fails, contact your operator before removing a working pairing. A "
            "disconnected session continues on the slot.",
            '<code>ccfleet</code> 无法连接时，先确认槽位显示 <span class="pill '
            'ok">使用中</span>，再运行安装命令。仍失败时，先联系运营者，不要急着删除有效配'
            "对。远程会话在断开连接后仍继续运行。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            f"Anything else: ask {contact(cfg)}.",
            f"其他问题，请联系 {_contact_zh(cfg)}。",
            inline=True,
        )
        + "</li>"
        + "</ul></div>"
    )
    return _page("/docs/guide", "getting started", body, viewer=viewer)


# -- how it works -----------------------------------------------------------------------

#: The explicit legacy bridge, distinct from the current public HTTPS gateway.
PICTURE = """<svg class="diag" viewBox="0 0 360 520" role="img"
aria-labelledby="legacy-diag-t legacy-diag-d">
<title id="legacy-diag-t">Legacy bridge transport</title>
<desc id="legacy-diag-d">With --transport bridge, original Claude Code runs locally.
A local HTTP bridge carries supported model requests through encrypted SSH and
the BWH byte broker to the assigned slot relay, then Anthropic.
Other native CLI services can connect directly from the laptop.</desc>
<defs><marker id="legacy-ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="head" d="M0,0 L10,5 L0,10 z"/></marker>
<marker id="legacy-af" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="headfaint" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="box you" x="20" y="14" width="320" height="70" rx="12"/>
<text x="180" y="44" text-anchor="middle">Local original Claude Code</text>
<text class="sub" x="180" y="66" text-anchor="middle">local files, tools and history</text>
<path class="arrow" d="M180,88 L180,126" marker-start="url(#legacy-ah)"
marker-end="url(#legacy-ah)"/>
<text class="sub" x="190" y="111">TLS + encrypted SSH</text>
<rect class="side" x="20" y="130" width="320" height="70" rx="12"/>
<text x="180" y="160" text-anchor="middle">CC Fleet broker</text>
<text class="sub" x="180" y="182" text-anchor="middle">authenticates device; relays bytes</text>
<path class="arrow" d="M180,204 L180,242" marker-start="url(#legacy-ah)"
marker-end="url(#legacy-ah)"/>
<text class="sub" x="190" y="227">SSH to the slot</text>
<rect class="slot" x="20" y="246" width="320" height="114" rx="12"/>
<text x="180" y="276" text-anchor="middle">Your slot, on our machine</text>
<text class="sub" x="180" y="300" text-anchor="middle">fixed upstream model relay</text>
<text class="sub" x="180" y="322" text-anchor="middle">only your bound Claude account</text>
<text class="sub" x="180" y="344" text-anchor="middle">credential stays on the slot</text>
<path class="arrow" d="M180,364 L180,408" marker-start="url(#legacy-ah)"
marker-end="url(#legacy-ah)"/>
<text class="sub" x="190" y="392">Claude traffic from the slot</text>
<rect class="box" x="20" y="412" width="320" height="82" rx="12"/>
<text x="180" y="444" text-anchor="middle">Anthropic</text>
<text class="sub" x="180" y="468" text-anchor="middle">relevant prompts and tool context</text>
</svg>"""

GATEWAY_PICTURE = """<svg class="diag" viewBox="0 0 360 520" role="img"
aria-labelledby="gateway-diag-t gateway-diag-d">
<title id="gateway-diag-t">HTTPS gateway model request path</title>
<desc id="gateway-diag-d">Original Claude Code sends supported model requests over HTTPS
to our gateway on BWH. BWH reads the request and chooses one of your selected own slots.
A restricted SSH service connection reaches that slot, which uses its native Claude
credential over HTTPS to Anthropic. Files, tools and history remain local.</desc>
<defs><marker id="gateway-ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="head" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="box you" x="20" y="14" width="320" height="70" rx="12"/>
<text x="180" y="44" text-anchor="middle">Local original Claude Code</text>
<text class="sub" x="180" y="66" text-anchor="middle">local files, tools and history</text>
<path class="arrow" d="M180,88 L180,126" marker-start="url(#gateway-ah)"
marker-end="url(#gateway-ah)"/>
<text class="sub" x="190" y="111">HTTPS + endpoint key</text>
<rect class="side" x="20" y="130" width="320" height="70" rx="12"/>
<text x="180" y="160" text-anchor="middle">Our HTTPS gateway on BWH</text>
<text class="sub" x="180" y="182" text-anchor="middle">BWH can read model contents</text>
<path class="arrow" d="M180,204 L180,242" marker-start="url(#gateway-ah)"
marker-end="url(#gateway-ah)"/>
<text class="sub" x="190" y="227">Restricted service SSH</text>
<rect class="slot" x="20" y="246" width="320" height="114" rx="12"/>
<text x="180" y="276" text-anchor="middle">Your selected own slot</text>
<text class="sub" x="180" y="300" text-anchor="middle">one bound Claude account</text>
<text class="sub" x="180" y="322" text-anchor="middle">native authentication and renewal</text>
<text class="sub" x="180" y="344" text-anchor="middle">credential stays on the slot</text>
<path class="arrow" d="M180,364 L180,408" marker-start="url(#gateway-ah)"
marker-end="url(#gateway-ah)"/>
<text class="sub" x="190" y="392">HTTPS to Anthropic</text>
<rect class="box" x="20" y="412" width="320" height="82" rx="12"/>
<text x="180" y="444" text-anchor="middle">Anthropic</text>
<text class="sub" x="180" y="468" text-anchor="middle">runs the model; streams the answer</text>
</svg>"""


def _diagram_zh(svg: str, namespace: str, text: dict[str, str]) -> str:
    """Translate a trusted static SVG, including its accessible description.

    Both diagrams can appear in bilingual mode, so every translated ID and
    corresponding reference has its own namespace.
    """
    translated = svg.replace(namespace + "-", namespace + "-zh-")
    for english, chinese in sorted(text.items(), key=lambda item: len(item[0]), reverse=True):
        translated = translated.replace(english, chinese)
    return translated


_DIAGRAM_COMMON = {
    "Local original Claude Code": "电脑上的原版 Claude Code",
    "local files, tools and history": "本地文件、工具与会话记录",
    "credential stays on the slot": "登录凭证留在槽位",
}

PICTURE_ZH = _diagram_zh(
    PICTURE,
    "legacy",
    {
        **_DIAGRAM_COMMON,
        "Legacy bridge transport": "旧版桥接方式",
        "With --transport bridge, original Claude Code runs locally.\nA local HTTP "
        "bridge carries supported model requests through encrypted SSH and\nthe BWH "
        "byte broker to the assigned slot relay, then Anthropic.\nOther native CLI "
        "services can connect directly from the laptop.": (
            "选择 --transport bridge 时，原版 Claude Code 在电脑上运行。本地 HTTP "
            "服务把支持的模型请求送进 SSH 加密流，经 BWH 字节转发到指定槽位，再到 "
            "Anthropic。其他原生服务仍可能直接从电脑联网。"
        ),
        "TLS + encrypted SSH": "TLS + SSH 加密",
        "CC Fleet broker": "CC Fleet 字节转发服务",
        "authenticates device; relays bytes": "验证设备；转发加密字节",
        "SSH to the slot": "SSH 到槽位",
        "Your slot, on our machine": "托管机器上的你的槽位",
        "fixed upstream model relay": "固定上游的模型转发",
        "only your bound Claude account": "只使用你绑定的 Claude 账号",
        "Claude traffic from the slot": "从槽位发出的模型请求",
        "relevant prompts and tool context": "包含相关提示与工具上下文",
    },
)

GATEWAY_PICTURE_ZH = _diagram_zh(
    GATEWAY_PICTURE,
    "gateway",
    {
        **_DIAGRAM_COMMON,
        "HTTPS gateway model request path": "HTTPS 网关模型请求路线",
        "Original Claude Code sends supported model requests over HTTPS\nto our "
        "gateway on BWH. BWH reads the request and chooses one of your selected own "
        "slots.\nA restricted SSH service connection reaches that slot, which uses its "
        "native Claude\ncredential over HTTPS to Anthropic. Files, tools and history "
        "remain local.": "原版 Claude Code 通过 HTTPS 将支持的模型请求发给 BWH 上的网关。BWH "
        "读取请求，在你选中的自己的槽位里选择一个。权限受限的 SSH "
        "服务连接到该槽位，再由槽位用原生 Claude 凭证通过 HTTPS 访问 "
        "Anthropic。文件、工具和历史仍在电脑上。",
        "HTTPS + endpoint key": "HTTPS + 连接密钥",
        "Our HTTPS gateway on BWH": "BWH 上的 HTTPS 网关",
        "BWH can read model contents": "BWH 可以读取模型消息",
        "Restricted service SSH": "权限受限的服务 SSH",
        "Your selected own slot": "你选中的自己的槽位",
        "one bound Claude account": "绑定一个 Claude 账号",
        "native authentication and renewal": "原生 Claude 管理登录与续期",
        "HTTPS to Anthropic": "HTTPS 到 Anthropic",
        "runs the model; streams the answer": "运行模型；流式返回回复",
    },
)


def how_it_works(
    cfg: Config, viewer: Optional[Viewer] = None, *, price: Optional[pricing.Price] = None
) -> str:
    """Nothing here is about paying; `price` is taken so every page is called
    the same way."""
    body = (
        '<div class="dochead">'
        + bi("<h1>How it works</h1>", "<h1>CC Fleet 如何工作</h1>")
        + bi(
            '<p class="lead"><code>ccfleet local</code> runs original Claude Code on your '
            "computer. With a configured gateway, supported model requests go over HTTPS "
            "to our domain on BWH and then to one of your selected own slots. BWH can read "
            "those model contents. Plain <code>ccfleet</code> remains a remote-terminal "
            "compatibility command.</p>",
            '<p class="lead"><code>ccfleet local</code> 在你的电脑上运行原版 Claude '
            "Code。配置网关后，支持的模型请求通过 HTTPS 到达我们在 BWH "
            "上的域名，再使用你选中的某个自己的槽位。BWH 可以读取这些模型消息。单独运行 "
            "<code>ccfleet</code> 仍打开兼容的远程终端。</p>",
        )
        + '</div><div class="how"><figure class="card diagram">'
        + bi(GATEWAY_PICTURE, GATEWAY_PICTURE_ZH)
        + "<figcaption>"
        + bi(
            "Current HTTPS gateway: no local HTTP listener or laptop-side SSH for model requests.",
            "当前 HTTPS 方式：模型请求无需本地 HTTP 服务，也无需电脑端 SSH。",
            inline=True,
        )
        + "</figcaption>"
        + "<details>"
        + "<summary>"
        + bi("Legacy bridge transport", "旧版桥接方式", inline=True)
        + "</summary>"
        + bi(PICTURE, PICTURE_ZH)
        + bi(
            "<p><code>ccfleet local --transport bridge</code> uses a local HTTP bridge and "
            "laptop-to-slot SSH through the BWH byte broker.</p>",
            "<p><code>ccfleet local --transport bridge</code> 会启动本地 HTTP "
            "桥接服务，通过 BWH 的字节转发服务建立电脑到槽位的 SSH 加密连接。</p>",
        )
        + "</details>"
        + bi(
            '<p><a href="/docs/library/docs/public-gateway.html">Public gateway '
            "architecture and privacy</a></p>",
            '<p><a href="/docs/library/docs/public-gateway.html">网关架构与隐私参考</a></p>',
        )
        + '</figure><div class="how-text">'
        + _model_connections(cfg)
        + '<div class="card">'
        + bi("<h2>Your slot</h2>", "<h2>槽位是什么</h2>")
        + bi(
            "<p>Your slot is a Linux account under the name you give it, with a private "
            "home directory and the original Claude Code. The plain <code>ccfleet</code> "
            "remote-terminal command uses a persistent session that keeps running when "
            "your computer disconnects; <code>ccfleet local</code> instead runs on your "
            "computer. There is no administrator access inside a slot. Other slots on the "
            "same machine use different operating-system accounts and Claude sign-ins.</p>",
            "<p>槽位是以你选定名称运行的 Linux 用户环境，拥有自己的主目录与原版 Claude "
            "Code。单独运行 <code>ccfleet</code> 会连接持久的远程会话，电脑断开后远程会话仍"
            "运行；<code>ccfleet local</code> 则在你的电脑上运行。槽位内没有管理员权限。同"
            "一台机器上的不同槽位使用不同的系统用户和 Claude 登录。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Your Claude account</h2>", "<h2>Claude 账号与登录凭证</h2>")
        + bi(
            "<p>You sign in to Claude yourself, through Anthropic&#x27;s own sign-in. The "
            "credential that creates is written on the machine, in your slot, and nowhere "
            "else: ccfleet&#x27;s server passes along the sign-in link and the code you "
            "paste, and never receives or keeps the resulting credential. Native Claude on "
            "the slot owns authentication and renewal. The slot relay uses that bound "
            "credential upstream, not a credential sent to your laptop or BWH. One slot "
            "keeps one bound account for one holder. A gateway endpoint may select one or "
            "several of that holder's own slots; it does not combine credentials within a "
            "slot or use another user's account.</p>",
            "<p>你通过 Anthropic 的登录流程亲自登录 Claude。产生的凭证只写在机器上的槽位中"
            "；CC Fleet 服务转发登录链接与粘贴的代码，不接收或保存最终的 Claude "
            "凭证。认证与续期由槽位上的原版 Claude 管理；槽位转发程序使用该槽位绑定的凭证访"
            "问上游，不把凭证传到电脑或 BWH。每个槽位绑定持有者的一个账号。一个 HTTPS "
            "模型连接可以选择同一用户自己的一个或多个槽位，不会在一个槽位中合并凭证，也不会"
            "使用其他用户的账号。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Credential maintenance</h2>", "<h2>登录状态怎样维护</h2>")
        + bi(
            "<p>Before access credentials expire, the machine attempts bounded renewal "
            "through native Claude in an isolated maintenance session. It checks that "
            "expiry actually advanced, preserves the bound account, and backs off on "
            "failure. The relay never writes or independently refreshes credentials. Your "
            "page reports unconfirmed renewal or stale health information instead of "
            "claiming readiness. Revocation or an expired sign-in can still require "
            "<strong>Sign in again</strong>; this is not a guarantee of permanent account "
            "access.</p>",
            "<p>访问凭证到期前，机器会通过原版 Claude 在隔离的维护会话中尝试续期。它确认有"
            "效期确实延长、账号绑定未改变；失败时限制重试频率。模型转发程序不会自行写入或刷"
            "新凭证。页面会明确显示续期尚未确认或状态已过期，而不是冒充就绪。凭证被撤销或登"
            "录过期时，仍可能需要<strong>重新登录</strong>，没有“永久可用”的保证。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Reported health and local diagnostics</h2>", "<h2>状态观察与本地诊断</h2>")
        + bi(
            "<p>Your slot card distinguishes reported ready, renewal pending, sign-in "
            "required and account maintenance. Stale observations do not claim readiness. "
            "A heartbeat does not prove provider acceptance; <code>ccfleet doctor "
            "--privacy</code> and <code>ccfleet status --json</code> perform read-only "
            "connection checks without a model request or project scan. Support export is "
            "an explicit local-file operation, not an upload of your project or "
            "conversation.</p>",
            "<p>卡片会区分已报告就绪、等待续期、需要登录和账号维护；过期的观察不会被当作当"
            "前就绪。状态报告不能证明供应商会接受下一条消息。<code>ccfleet doctor "
            "--privacy</code> 与 <code>ccfleet status --json</code> "
            "只做连接检查，不调用模型、不扫描项目。诊断导出是主动写入本地文件的操作，不上传"
            "项目或会话。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Use it from anywhere</h2>", "<h2>在不同电脑上使用</h2>")
        + bi(
            "<p>Install <code>ccfleet</code> on each computer you use. A fresh setup pairs "
            "the device and enrolls an endpoint key when gateway access is available. "
            "Previously paired computers can run <code>ccfleet gateway setup</code> "
            "without pairing again. Local Claude sends model requests directly over HTTPS "
            "using that separate key. Choose one own slot, or an ordered own-slot pool "
            "with conversation affinity or safe failover. BWH uses a restricted "
            "inference-only SSH key to the selected slot.</p>",
            "<p>每台电脑分别安装 <code>ccfleet</code>。首次安装会配对设备，网关开放时也会创"
            "建独立的连接密钥。以前已配对的电脑可运行 <code>ccfleet gateway "
            "setup</code>，无需重新配对。本地 Claude 使用这个独立密钥直接通过 HTTPS "
            "发送模型请求。你可以只选自己的一个槽位，也可以配置自己的槽位顺序、会话保持和安"
            "全切换。BWH 用权限受限、仅用于模型转发的 SSH 密钥连接选中的槽位。</p>",
        )
        + bi(
            "<p>The explicit <code>--transport bridge</code> alternative starts a local "
            "HTTP listener and sends laptop-to-slot SSH through an authenticated "
            "WebSocket. Its BWH byte broker cannot read that encrypted model stream. The "
            "HTTPS gateway terminates TLS on BWH and can read the stream's contents. Both "
            "routes keep the model destination fixed; it is not caller-selected.</p>",
            "<p>明确选择 <code>--transport bridge</code> 时，会启动本地 HTTP "
            "服务，通过已认证的 WebSocket 承载电脑到槽位的 SSH。BWH "
            "字节转发服务无法读取内部加密的模型流。HTTPS 网关则在 BWH 解密 TLS，因此 BWH "
            "能读取模型消息。两种方式的模型上游都由服务固定，不允许调用方指定任意上游地址。"
            "</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Native local files and history</h2>", "<h2>文件、工具与历史在本地</h2>")
        + bi(
            "<p>Claude, its tools, settings and conversations run locally. There is no "
            "folder upload, mount or push/pull step, and no CC Fleet filesystem cap. "
            "Native permissions apply; bypass mode can read, modify, delete or transmit "
            'local data without prompts. <a href="/docs/guide#migration">The migration '
            "guide</a> explains explicit cleanup of retired live-folder grants and the "
            "optional <code>--legacy-history</code> profile.</p>",
            "<p>本地方式的 Claude、工具、设置和会话都在你的电脑上运行，不需要上传文件夹、挂"
            "载或 push/pull，也没有 CC Fleet 另设的文件系统上限。原生权限仍然生效；跳过权限"
            '确认时，工具可以不逐次询问就读取、修改、删除或发送本地数据。<a href="/docs/gui'
            'de#migration">迁移指南</a>说明如何主动清理已退役的文件夹授权，以及如何选择旧的'
            " <code>--legacy-history</code> 配置。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>What we can and cannot see</h2>", "<h2>服务方能看到什么</h2>")
        + bi(
            "<p>ccfleet&#x27;s server receives facts about your slot: whether Claude Code "
            "is signed in, the email address and plan of the Claude account signed in on "
            "it, how much of your usage limits is used, token counts per hour, and which "
            "CC Fleet devices are paired. Those heartbeat reports do not contain prompts, "
            "conversations, file contents or the Claude credential. Separately, the direct "
            "HTTPS gateway on BWH receives and processes model requests and responses, "
            "including any prompt, file or tool-result context they contain. Its endpoint "
            "key is stored only as a hash on the server; the native Claude credential "
            'remains on the slot. <a href="/privacy">The privacy page</a> lists '
            "everything, and for how long.</p>",
            "<p>CC Fleet 会收到槽位状态：Claude 是否登录、登录账号的邮箱与订阅、已用配额、"
            "按小时的 token 数，以及已配对的设备。这样的状态报告不包含提示、会话、文件内容"
            "或 Claude 凭证。另一方面，BWH 上的 HTTPS 网关会接收和处理模型请求与回复，也能"
            "读取其中的提示、文件或工具结果。服务端只保存连接密钥的哈希；原生 Claude "
            '凭证仍在槽位上。完整数据范围与保存时长见<a href="/privacy">隐私说明</a>。</p>',
        )
        + bi(
            "<p>Claude Code on your slot sends Anthropic less than it would by default: "
            "its error reports, bug reports and feedback surveys are switched off. The "
            "machines keep their clocks on UTC; your page shows times in your own time "
            "zone. That does not make the service anonymous: BWH sees your connection IP "
            "and the gateway's model contents; SSH has transport metadata, and native "
            "prompts, tool results and file contents can identify you. Selected headers "
            "and top-level structured metadata are removed, but native system prompts can "
            "still include local OS, working directory or environment details. MCP "
            "servers, hooks, plugins, tools, updates and other native services can connect "
            "directly from the laptop. Optional telemetry controls are not an all-traffic "
            "firewall.</p>",
            "<p>槽位上的 Claude Code 关闭了错误报告、bug 报告和反馈问卷。机器使用 "
            "UTC，网页按你的时区显示时间。这不代表匿名：BWH 看得到连接 IP 和网关消息，SSH "
            "也有传输元数据；提示、工具结果和文件内容可能识别你。虽然会移除部分请求头和顶层"
            "结构化 metadata，原生系统提示仍可能包含本地系统、工作目录和环境信息。MCP、hook"
            "s、插件、工具、更新和其他原生服务可能从电脑直接联网；可选遥测控制不是拦截所有"
            "流量的防火墙。</p>",
        )
        + bi(
            "<p>One limit is worth saying plainly: the machines are ours, and their "
            "administrators have root, so they can technically read any slot. The operator "
            "console does not display native credentials or full file contents, but that "
            "does not prevent an administrator from inspecting them. Keep nothing in a "
            "slot that you could not accept an administrator being able to read.</p>",
            "<p>机器管理员拥有 root 权限，技术上能读取任意槽位。管理界面不会显示原生凭证或"
            "完整文件内容，但这不阻止管理员直接检查。请只在槽位保存你能接受管理员有能力读取"
            "的内容。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Kept up to date</h2>", "<h2>更新与服务状态</h2>")
        + "<ul>"
        + "<li>"
        + bi(
            "<strong>Local Claude Code</strong>: CC Fleet setup preserves an existing "
            "local installation and installs it if missing. Automatic updates are disabled "
            "during a CC Fleet launch; update your local Claude installation separately, "
            "outside that session.",
            "<strong>电脑上的 Claude Code：</strong>安装 CC Fleet "
            "时保留现有版本，缺少时才安装。CC Fleet 启动期间关闭自动更新；需要更新时，在会"
            "话外单独更新电脑上的 Claude Code。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Claude Code</strong> in your slot is updated automatically to "
            "Anthropic&#x27;s latest release, or to its stable release if you choose that "
            "on your page. A session that is running is never interrupted; the new version "
            "is used from the next one.",
            "<strong>槽位上的 Claude Code：</strong>自动跟随 Anthropic "
            "最新发布，也可在页面选择稳定渠道。正在运行的会话不会因更新被中断，下次新建会话"
            "使用新版。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Security updates</strong> for the machine install every day.",
            "<strong>系统安全更新：</strong>机器每天自动安装。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Reboots</strong>: when an update needs one, the machine says so and "
            "the operator reboots it at a quiet time. Your slot and its services come back "
            "by themselves.",
            "<strong>重启：</strong>更新需要重启时，机器会报告，运营者选择较空闲的时间执行"
            "。槽位及其服务会自动恢复。",
            inline=True,
        )
        + "</li>"
        + "<li>"
        + bi(
            "<strong>Status</strong>: whether this site and the machines are up, now and "
            'over the last 90 days, is on <a href="/status">the status page</a>; your own '
            "page says how your slot&#x27;s machine is. Turn on <strong>outage "
            "emails</strong> there, and we email you when your slot&#x27;s machine has "
            "been down for five minutes, and again when it is back. An outage of this "
            "website is told afterwards, since the website is what sends them. Existing "
            "slot processes may keep running, but local model requests require their "
            "configured gateway or bridge broker to be reachable. A configured gateway "
            "failure does not silently start a bridge fallback.",
            '<strong>服务状态：</strong><a href="/status">状态页</a>显示网站和机器的当前状'
            "态及过去 90 天记录，槽位页面显示对应机器状态。开启<strong>故障邮件</strong>后"
            "，机器断线五分钟及恢复时会发邮件；网站故障只能恢复后通知，因为邮件由网站发送。"
            "槽位已有进程可能继续运行，但本地模型请求仍需要已配置的网关或桥接服务在线。网关"
            "失败时，不会悄悄切换到旧桥接方式。",
            inline=True,
        )
        + "</li>"
        + '</ul></div><div class="card">'
        + bi("<h2>Giving a slot back</h2>", "<h2>归还槽位时发生什么</h2>")
        + bi(
            "<p>When you give a slot back, the machine stops everything running in it and "
            "deletes your Linux account and every file in it. The slot is offered to "
            "anybody else only after the machine itself confirms your account is gone, so "
            "nobody is ever handed your files.</p>",
            "<p>归还槽位后，机器会停止其中运行的程序，删除 Linux "
            "用户及所有文件。只有机器确认删除完成，槽位才会再次开放领取，避免把原用户的文件"
            "交给下一位。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Where</h2>", "<h2>托管位置与源代码</h2>")
        + bi(
            "<p>The hosted machines are in California. A slot is a separate Linux account; "
            "slots may share a physical machine and its internet address. ccfleet&#x27;s "
            'code is open source: <a href="https://github.com/cdcupt/ccfleet" '
            'target="_blank" rel="noopener noreferrer">github.com/cdcupt/ccfleet</a>.</p>',
            "<p>托管机器位于加利福尼亚。槽位是独立的 Linux 用户环境，多个槽位可能共用同一台"
            '物理机器与公网地址。CC Fleet 代码开源：<a href="https://github.com/cdcupt/ccfl'
            'eet" target="_blank" rel="noopener noreferrer">github.com/cdcupt/ccfleet</a>。'
            "</p>",
        )
        + "</div></div></div>"
    )
    return _page("/docs/how-it-works", "how it works", body, width="", viewer=viewer)


# -- terms ------------------------------------------------------------------------------


def terms(
    cfg: Config, viewer: Optional[Viewer] = None, *, price: Optional[pricing.Price] = None
) -> str:
    paying = (
        f"A slot costs {_cost(price)}, paid directly to the operator, who "
        f"switches "
        f"your "
        "slot on when you pay; this site takes no card and no payment."
        if price is not None
        else "Price and payment are agreed directly with the operator, who switches your "
        "slot on when you pay."
    )
    paying_zh = (
        f"槽位费用为{_cost_zh(price)}，直接支付给运营者，付款后开通；"
        f"本站不收集银行卡，也不代收款项。"
        if price is not None
        else "价格与付款方式直接和运营者约定，付款后开通。"
    )
    body = (
        '<div class="dochead">'
        + bi("<h1>Terms</h1>", "<h1>服务条款</h1>")
        + bi(
            '<p class="sub">Last updated 2026-10-11</p>', '<p class="sub">最后更新：2026-10-11</p>'
        )
        + '</div><div class="card">'
        + bi("<h2>The service</h2>", "<h2>服务内容</h2>")
        + bi(
            "<p>A slot is a Linux account on a machine the operator runs, for one person, "
            "with Claude Code installed. You bring your own Claude plan and sign in to it "
            "yourself; ccfleet does not provide access to Claude. CC Fleet is independent "
            "and is not affiliated with or endorsed by Anthropic.</p>",
            "<p>槽位是运营者托管机器上的一个 Linux 用户环境，供一人使用，安装了 Claude "
            "Code。你自行准备 Claude 订阅并亲自登录；CC Fleet 不提供 Claude 订阅或账号。CC "
            "Fleet 独立运营，与 Anthropic 没有隶属关系，也未获其背书。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Your Claude account</h2>", "<h2>你的 Claude 账号</h2>")
        + bi(
            "<p>Use your own Claude account in your slot, and only yours. You are "
            "responsible for following Anthropic&#x27;s terms and usage policy, as you "
            "would on your own computer. Do not share your slot, or your sign-in, with "
            "anybody else.</p>",
            "<p>槽位只能使用你自己的 Claude 账号。你需要遵守 Anthropic "
            "的条款与使用政策，和在自己电脑上使用时一样。不要向他人共享槽位或登录。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Paying</h2>", "<h2>费用与付款</h2>")
        + bi(
            f"<p>{paying} If a payment lapses, the operator may take a slot "
            f"back, and "
            f"taking a slot back deletes everything in it.</p>",
            f"<p>{paying_zh} 逾期未付款时，运营者可能收回槽位；收回会删除槽位内的所有内容。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Your files</h2>", "<h2>文件与备份</h2>")
        + bi(
            "<p>Slots are <strong>not backed up</strong>. Giving a slot back, or having it "
            "taken back, deletes everything in it for good. Keep your work in git, or "
            "anywhere else that is yours.</p>",
            "<p>槽位<strong>没有备份</strong>。归还或被收回时，槽位内所有内容都会永久删除。"
            "请把远程工作保存在 Git 或你自己的存储中。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Fair use</h2>", "<h2>合理使用</h2>")
        + bi(
            "<p>Use your slot for your own work with Claude Code. Do not use it to attack "
            "or scan other systems, mine cryptocurrency, send spam, break the law, or "
            "reach other people&#x27;s slots. The operator may take back a slot that is "
            "used this way.</p>",
            "<p>槽位用于你自己的 Claude Code 工作。不得用来攻击或扫描其他系统、挖矿、发垃圾"
            "消息、违法，或访问他人的槽位。出现这些行为时，运营者可能收回槽位。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Availability</h2>", "<h2>可用性</h2>")
        + bi(
            "<p>The service is run with care but without a guarantee of uptime. Machines "
            "are updated and occasionally rebooted, and a machine can fail.</p>",
            "<p>我们会认真维护服务，但不保证无中断运行。机器需要更新、偶尔重启，也可能发生"
            "故障。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Changes and contact</h2>", "<h2>条款变更与联系</h2>")
        + bi(
            f"<p>If these terms change, this page changes and the date at the "
            f"top says "
            f"when. Questions go to {contact(cfg)}. How your information is "
            f"handled is on "
            f'the <a href="/privacy">privacy page</a>.</p>',
            f"<p>条款变更时会更新本页，并修改顶部日期。有问题请联系 "
            f'{_contact_zh(cfg)}。个人信息的处理方式见<a href="/privacy">'
            f"隐私说明</a>。</p>",
        )
        + "</div>"
    )
    return _page("/docs/terms", "terms", body, viewer=viewer)


PAGES: dict[str, Callable[..., str]] = {
    "/docs": overview,
    "/docs/guide": guide,
    "/docs/how-it-works": how_it_works,
    "/docs/terms": terms,
}


def page_for(path: str) -> Optional[Callable[..., str]]:
    """The page at this path; None when there is none."""
    return PAGES.get(path.rstrip("/") or "/")
