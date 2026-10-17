"""The pages a customer is sent: what ccfleet is, how to start, how it works, the terms.

Public, on the product site, beside /privacy. Written for somebody deciding
whether to buy a slot and then using one, not for an operator. The front page
also shows a masked public usage summary; no machine address or private account
identity is disclosed. Every button it quotes is the label
the user site really shows.

The one fact that must never soften is the plan requirement: ccfleet sells a
machine, not Claude. Every person brings their own Claude plan and signs in to
it themselves; no page may suggest otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any, Callable, Optional

from . import pricing, statuspage
from .bilingual import bi, label
from .config import Config
from .render import _meter
from .usersite import NAV, Viewer, _shell, public_usage_panel

#: When these pages last changed in substance. Change it with the words.
DOCS_UPDATED = "2026-10-16"

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


def _rental_pricing_details() -> str:
    """Current catalogue policy, shared by the guide, explanation and terms."""
    return bi(
        '<p><strong>Alpha pricing.</strong> Each machine has its own monthly USD rate in the '
        '<a href="/#slot-pricing">public slot list</a>. Alpha pricing passes through recorded '
        'VPS and exit-IP costs, plus a monthly service fee of 10% of resource cost, with a '
        '$2 minimum per slot. Your Claude subscription or Anthropic API usage is billed '
        'separately.</p><p>Recorded costs are normalized to a calendar month. '
        'A 30-day cost is multiplied by 365/360; an annual cost is divided by 12. '
        'For a customer quote, round the normalized USD resource cost to cents, half up; '
        'calculate 10% of that amount, round the service fee half up to cents with a $2 '
        'minimum, then add the two displayed amounts. Resource costs of $8.68, $10.83, '
        '$22.28 and $27.44 therefore give totals of $10.68, $12.83, $24.51 and $30.18 '
        'per month. The fee remains $2 through $20 of resource cost.</p>',
        '<p><strong>Alpha 定价。</strong>每台主机的美元月费分别列于'
        '<a href="/#slot-pricing">公开槽位列表</a>。按已记录的 VPS 与出口 IP 成本计价，'
        '另收资源成本 10% 的月度服务费，每槽位最低 $2。你自己的 Claude 订阅或 '
        'Anthropic API 用量另行付费。</p><p>已记录成本统一折算为日历月：30 天成本乘以 '
        '365/360，年付成本除以 12。报价先将折算后的美元资源成本四舍五入到美分，再取其 '
        '10% 并将服务费四舍五入到美分，服务费最低 $2，最后将两项显示金额相加。'
        '资源成本 $8.68、$10.83、$22.28 和 $27.44，分别对应月费 $10.68、$12.83、'
        '$24.51 和 $30.18。资源成本不超过 $20 时，服务费为 $2。</p>',
    ) + bi(
        '<p>The public breakdown shows VPS cost, separately recorded exit-IP cost, service '
        'fee and total. When hosting includes the exit IP, it shows the recorded bundled '
        'resource cost without inventing a split. A normalization rounding line explains '
        'any cent-level difference between individually rounded components and the '
        'rounded combined resource cost. Missing costs or costs not converted '
        'to USD make pricing unavailable. The host and IP-provider brands remain visible. '
        'ISP and Residential describe network types, with the same pricing formula and '
        'no separate residential premium; neither guarantees quality, speed or Claude '
        'compatibility.</p>',
        '<p>公开明细展示 VPS 成本、单独记录的出口 IP 成本、服务费与总价。'
        '主机包含出口 IP 时，展示已记录的打包资源成本，不虚构拆分金额。若各项单独舍入后'
        '与合计资源成本存在美分差异，会以折算舍入行解释。成本缺失或尚未'
        '转换为美元时，价格暂不可用。主机与 IP 服务商品牌保持可见。ISP 与住宅只描述'
        '网络类型，两者采用同一公式，不另加住宅溢价；这些类型不保证质量、速度或 '
        'Claude 兼容性。</p>',
    ) + _alpha_stage_details() + bi(
        '<p>Agree the price and paid period with the operator; the website does not charge '
        'automatically, renew automatically or rewrite past payments.</p>',
        '<p>价格及服务有效期直接与运营者约定；网站不会自动扣款、自动续费，'
        '也不会改写过去的付款记录。</p>',
    ) + bi(
        '<p>Retired machines are removed from the rental catalogue. A temporarily '
        'offline machine that remains in service still appears on '
        '<a href="/status#slot-machines">Status</a>. Retirement does not release an '
        'existing holder or delete their account and payment history.</p>',
        '<p>已退役主机会从出租目录移除。仍在运营、只是暂时离线的主机继续显示在'
        '<a href="/status#slot-machines">服务状态</a>中。退役不会自动归还已有持有者的'
        '槽位，也不会删除其账号或付款历史。</p>',
    )


def _alpha_stage_details() -> str:
    """Describe the current stage without turning a personal trial into a guarantee."""
    return bi(
        '<p><strong>Alpha — Early Access.</strong> The service is still being tested and '
        'has not launched commercially. The operator has personally used one residential '
        'slot with encouraging results; reliability across providers, configurations and '
        'multiple users is still being evaluated. Alpha pricing is introductory and may '
        'change for future billing periods. You will receive an updated quote before '
        'renewal if pricing changes; an already agreed billing period retains its agreed '
        'price.</p>',
        '<p><strong>Alpha — 早期体验。</strong>服务仍在测试，尚未正式商业发布。运营者'
        '亲自使用过一个住宅槽位，结果令人鼓舞，但不同服务商、配置及多用户场景的可靠性'
        '仍待评估。Alpha 价格为初期价格，未来计费周期可能调整。如有变更，续期前会提供'
        '更新报价；已约定计费周期仍执行原约定价格。</p>',
    )


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
        '<p class="sub"><a href="/docs/library">Documentation</a> &mdash; '
        "the gateway explained simply, account choices and platform comparison.</p>",
        '<p class="sub"><a href="/docs/library">使用文档</a>：'
        "网关通俗说明、账号选择与平台对比。</p>",
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
    ranking: Optional[Mapping[str, Any]] = None,
    catalogue: str = "",
    per_slot_pricing: bool = False,
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
    # A legacy global selling price is not evidence of current resource costs.
    paying = ("Pricing is unavailable until the recorded resource costs support a quote. "
              "Agree the quote and payment directly with the operator.")
    paying_zh = ("已记录的资源成本支持报价前，价格暂不可用。报价与付款方式直接和运营者约定。")
    if per_slot_pricing:
        paying = ('The <a href="#slot-pricing">slot list</a> shows each provider, IP type '
                  'and monthly USD rate. Alpha pricing adds a service fee of 10% of '
                  'recorded monthly resource cost, with a $2 minimum per slot. Your Claude '
                  'subscription or Anthropic API usage is billed separately. Choose a '
                  'machine at its displayed rate and pay the operator directly; this site '
                  'does not charge automatically or renew automatically.')
        paying_zh = ('<a href="#slot-pricing">槽位列表</a>展示各服务商、IP 类型及月费，'
                     'Alpha 定价在已记录的美元月度资源成本上，收取 10% 服务费，'
                     '每槽位最低 $2。Claude 订阅或 Anthropic API 用量另计。'
                     '按所示价格选择主机，直接支付给运营者；本站不会自动扣款或自动续费。')
    body = (
        '<section class="hero"><div class="hero-copy"><div class="hero-top">'
        + bi(
            '<p class="eyebrow">Bring your own Claude plan</p>',
            '<p class="eyebrow">使用你自己的 Claude 订阅</p>',
        )
        + bi('<p class="eyebrow">Alpha — Early Access</p>',
             '<p class="eyebrow">Alpha — 早期体验</p>')
        + statuspage.pill(health)
        + "</div>"
        + bi(
            "<h1>Local Claude Code, your own account on your slot</h1>",
            "<h1>Claude Code 在本地运行，账号留在你的槽位</h1>",
        )
        + bi(
            '<p class="lead">ccfleet gives you a <strong>slot</strong>: a dedicated VPS '
            "and dedicated exit IP, with a private Linux account signed in to "
            "<em>your own</em> Claude account. "
            "Run <code>ccfleet local</code> for original Claude Code, files, tools and "
            "history on your computer. Supported model requests go over HTTPS to our "
            "gateway, then through a slot you selected.</p>",
            '<p class="lead">CC Fleet 提供一个<strong>槽位</strong>：专属 VPS 与专属出口 '
            "IP，以及登录<em>你自己的</em> Claude 账号的独立 Linux 用户环境。运行 <code>ccfleet "
            "local</code>，原版 Claude Code、文件、工具和历史在你的电脑上；支持的模型请求通"
            "过 HTTPS 到网关，再经过你选中的槽位。</p>",
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
        + '</ol></div></section>'
        + '<section class="band">'
        + bi('<h2>Alpha — Early Access</h2>', '<h2>Alpha — 早期体验</h2>')
        + _alpha_stage_details()
        + '</section>'
        + catalogue
        + public_usage_panel(ranking)
        + '<section class="band">'
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
            '<p><strong>在网页上：</strong>打开<a href="/account#gateway">我的槽位 → 连接 '
            "Claude</a>。只用 A 时，仅给 A 填 <code>1</code>；需要 B 备选时，再给 B 填 "
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
            "request, selects your slot, and that slot connects to Anthropic. The "
            "connection key authenticates you to CC Fleet; it is not a Claude credential "
            "or an Anthropic paid API key.</p>",
            "<p>Base URL 是客户端保存的服务地址。保存地址不会创建本地服务器，也不表示消息存"
            "放在这里。<code>/v1/messages</code> 是我们服务上的 Messages API "
            "兼容路径；路径名称相同，不代表这个域名归 Anthropic 所有。HTTPS 请求先到我们的"
            "网关，由网关选槽位，再由槽位连接 Anthropic。连接密钥用于向 CC Fleet "
            "证明访问权限，不是 Claude 登录凭证，也不是 Anthropic 付费 API key。</p>",
        )
        + bi(
            "<p><strong>Privacy:</strong> your laptop encrypts the connection to the gateway, but "
            "the gateway decrypts it to route the request and can read model messages and replies. "
            "The slot keeps its native Claude credential. Local tools may include file "
            "contents in model context; keeping files local does not mean that no file "
            "content can leave the computer.</p>",
            "<p><strong>隐私边界：</strong>电脑到网关的连接有加密，但网关"
            "为了路由会解密，因此可以读取模型消息和回复。原生 Claude "
            "凭证仍保存在槽位。工具可能把文件内容放入模型上下文；文件留在本地，不等于任何文"
            "件内容都不会离开电脑。</p>",
        )
        + bi(
            "<p>Machine administrators have root access and can inspect your slot. "
            "They may also alter relayed replies, which can influence local tool actions. "
            "Choose local permissions accordingly and keep only data you trust them to handle.</p>",
            "<p>机器管理员拥有 root 权限，能检查你的槽位，也可能修改转发的回复，"
            "进而影响本地工具行为。请选择合适的本地权限，"
            "并只放入你愿意信任服务方处理的数据。</p>",
        )
        + bi(
            '<p>For diagrams and the comparison with CC Host, read the <a href="/docs/libra'
            'ry/docs/platform-comparison.html">comparison guide</a>. The recorded '
            "verification on October 11, 2026 used one actual live account; multi-slot "
            "routing and faults used controlled peers, not two live accounts. See <a "
            'href="/docs/library/docs/public-gateway.html#verification">verification '
            "scope</a> for the evidence.</p>",
            '<p>路线图与 CC Host 对比见<a href="/docs/library/docs/platform-comparison.html'
            '">对比指南</a>。2026 年 10 月 11 日记录的真实线上验证使用了一个实际账号；多槽'
            "位路由和故障使用受控测试，并非两个真实账号的线上实测。证据范围见<a "
            'href="/docs/library/docs/public-gateway.html#verification">验证说明</a>。</p>',
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
            "has no update button. New Claude processes use the new version; existing ones "
            "are not interrupted. Updating the slot does not change your local Claude "
            "Code.</p>",
            '<p><strong>槽位上的 Claude Code：</strong>打开<a href="/account">我的槽位</a>'
            "，在 Claude Code 一栏点击<strong>更新至</strong>提供的新版本，同时会切换为 "
            "Latest 渠道。<strong>返回稳定版</strong>则选择 Stable "
            "渠道。运营者固定的版本没有更新按钮。新 Claude 进程使用新版，已有进程不被打断；"
            "更新槽位不会改变你电脑上的 Claude Code。</p>",
        )
        + "</div>"
    )


def _prompt_example(commands: str, prompt_en: str, prompt_zh: str) -> str:
    """Localize sample prompt prose while keeping one shared command example."""
    code = escape(commands, quote=False).replace(
        escape(prompt_en, quote=False), label(prompt_en, prompt_zh, inline=True), 1
    )
    return "<pre><code>" + code + "</code></pre>"


def _client_tools() -> str:
    """Current local controls; commands are shared between the language pages."""
    return (
        '<div class="card" id="client-tools">'
        + bi("<h2>Everyday commands</h2>", "<h2>日常操作</h2>")
        + bi(
            "<p>Use the menu to start a new conversation, continue your last one, or "
            "resume a saved conversation. Your native Claude history stays private on this "
            "computer; CC Fleet does not import or synchronize conversations.</p>",
            "<p>通过菜单可以新建会话、继续上一次工作，或恢复保存的会话。原生 Claude "
            "历史留在这台电脑上，CC Fleet 不会导入或同步会话。</p>",
        )
        + "<pre><code>ccfleet start\nccfleet sessions\nccfleet preferences set --model "
        "opus --effort high --mode plan\nccfleet preferences show\nccfleet preferences "
        "clear</code></pre>"
        + bi(
            "<p>Preferences apply to new local sessions in this project, or use "
            "<code>--project PATH</code>. Explicit launch choices win. Resuming keeps the "
            "saved model and effort unless changed deliberately. Clearing preferences does "
            "not clear history.</p>",
            "<p>偏好用于当前项目的新会话，也可用 <code>--project PATH</code> "
            "指定项目。启动时明确选择的选项优先。恢复时保留原有模型和推理强度，除非你主动修"
            "改；清除偏好不会删除历史。</p>",
        )
        + '<h3 id="simple-verification">'
        + label("Check the connection first", "先检查连接", inline=True)
        + "</h3>"
        + bi(
            "<p>With the current native-local client, run this in your normal terminal, "
            "not inside Claude. It checks installation and slot readiness. It does not "
            "scan projects, start a Claude session or make a model request.</p>",
            "<p>使用当前本地客户端时，在普通终端中运行下面的命令，不要在 Claude "
            "会话中输入。它检查安装和槽位是否就绪，不扫描项目、不启动 Claude "
            "会话，也不调用模型。</p>",
        )
        + "<pre><code>ccfleet local --check\nccfleet status --json\nccfleet doctor "
        "--privacy --json --export ./ccfleet-support.json</code></pre>"
        + bi(
            "<p>Doctor separates installation, pairing, connection and account-health "
            "problems. The support report is a new local file, not an upload. It omits "
            "prompts, file contents, local paths, tokens and account email; review it "
            "before sharing. Reported readiness is an observation, not proof that "
            "Anthropic will accept the next request. Keep an existing working pairing "
            "while you investigate an error.</p>",
            "<p>Doctor 区分安装、配对、连接和账号状态问题。诊断报告只写入新的本地文件，不会"
            "上传，也不包含提示、文件内容、本地路径、token 或账号邮箱；请先检查再分享。显示"
            "“就绪”是一次状态观察，不保证 Anthropic 会接受下一条消息。排查问题时，请保留仍"
            "然有效的配对。</p>",
        )
        + bi(
            "<p>For an optional basic response check, open <code>ccfleet local</code> and "
            "paste this prompt. It makes a model request and may use your plan "
            "allowance:</p>",
            "<p>如需额外检查基本回复，打开 <code>ccfleet local</code>，粘贴下面的提示。它会"
            "调用模型，并可能消耗订阅额度：</p>",
        )
        + bi(
            "<blockquote><p>Do not use tools, read files, or inspect my system or "
            "network.<br>Reply only: <code>CCFLEET_OK</code></p></blockquote>",
            "<blockquote><p>不要使用工具、读取文件，或检查我的系统与网络。<br>"
            "只回复：<code>CCFLEET_OK</code></p></blockquote>",
        )
        + bi(
            "<p>A matching reply checks basic responsiveness only. The no-tools "
            "instruction is not an enforced privacy safeguard: native context or hooks can "
            "still exist. Do not ask Claude to print real IPs, locations or environment "
            "values just to check privacy. The gateway can read model requests and replies, and "
            "Anthropic sees the selected slot&#x27;s outbound IP for those requests. Other "
            "tool or native network connections can go directly from the laptop.</p>",
            "<p>收到相同回复，只说明基本响应正常。“不要使用工具”是提示要求，不是强制隔离，"
            "原生上下文或 hooks 仍可能存在。不要为了检查隐私，让 Claude 打印真实 "
            "IP、位置或环境变量。网关可以读取模型消息与回复；对于这些请求，Anthropic "
            "看到的是选中槽位的出口 IP。其他工具或原生服务仍可能从电脑直接联网。</p>",
        )
        + '<h3 id="management-mcp">'
        + label("Optional read-only status tools", "可选的只读状态工具", inline=True)
        + "</h3>"
        + bi(
            "<p>You can explicitly add the local management MCP to Claude. Nothing is "
            "registered automatically. Its tools report cached slot health, quota "
            "percentages/reset times and numeric relay usage with observation age and "
            "sample coverage. Missing observations are unknown, not zero.</p>",
            "<p>可以主动把本地管理 MCP 添加到 Claude；不会自动注册。它提供缓存的槽位状态、"
            "配额百分比与重置时间，以及转发用量数值，并标明观察时间和样本覆盖范围。没有记录"
            "表示未知，不表示零。</p>",
        )
        + "<pre><code>claude mcp add --transport stdio ccfleet -- ccfleet mcp "
        "serve\nclaude mcp remove ccfleet</code></pre>"
        + bi(
            "<p>These fixed health codes and numeric results become available to the "
            "calling AI. They omit account/device identifiers, names, emails, IP "
            "addresses, local paths, prompts, logs and model/session identifiers. The "
            "tools cannot invoke Claude, refresh quota, scan projects, run commands or "
            "change account state. Pairing/account changes require a fresh MCP process. "
            "Usage covers seven UTC calendar days, not billing or just today.</p>",
            "<p>启用后，调用它的 AI 会看到固定状态码与数值。结果不含账号或设备标识、名称、"
            "邮箱、IP、本地路径、提示、日志，以及模型或会话标识。工具不能调用 "
            "Claude、刷新配额、扫描项目、执行命令或修改账号。更换配对或账号后，需要重启 "
            "MCP。用量覆盖七个 UTC 自然日，不是账单，也不只是今天的用量。</p>",
        )
        + "<h3>"
        + label("Update the Fleet client", "更新 Fleet 客户端", inline=True)
        + "</h3>"
        + "<pre><code>ccfleet version --json\nccfleet update\nccfleet update "
        "--rollback</code></pre>"
        + bi(
            "<p>The updater verifies the signed release channel with its pinned Ed25519 "
            "key, then the immutable manifest and every helper before activation. A failed "
            "verification or download leaves the current client in place. Rollback selects "
            "the preserved previous client without changing pairing, local files or native "
            "history. An absent or expired signed channel is an error. These commands "
            "update CC Fleet, not Anthropic&#x27;s original Claude executable.</p>",
            "<p>更新程序先用内置的 Ed25519 公钥验证发布渠道签名，再验证不可变清单和每个辅助"
            "文件，通过后才切换版本。验证或下载失败时保留当前客户端。回退使用保留的上一版，"
            "不改变配对、本地文件或原生历史。签名渠道缺失或过期时会报错。这里更新的是 CC "
            "Fleet，不是 Anthropic 的原版 Claude Code。</p>",
        )
        + '</div><div class="card" id="background-jobs">'
        + bi("<h2>Local background tasks</h2>", "<h2>本地后台任务</h2>")
        + bi(
            "<p>A managed task keeps local Claude print-mode work running after you close "
            "the starting terminal. Your computer must stay running and connected. Copy "
            "the job ID returned by start, then use it to view output or stop the task.</p>",
            "<p>受管理的任务可以在关闭启动终端后继续运行本地 Claude 的 print-mode "
            "工作。你的电脑必须保持开机并联网。保存 start 返回的任务 "
            "ID，用它查看输出或停止任务。</p>",
        )
        + _prompt_example(
            'ccfleet jobs start --prompt "Review this project and summarize findings"\n'
            "ccfleet jobs list\n"
            "ccfleet jobs status JOB_ID\n"
            "ccfleet jobs logs JOB_ID\n"
            "ccfleet jobs stop JOB_ID\n"
            "ccfleet jobs archive JOB_ID",
            "Review this project and summarize findings",
            "检查这个项目并总结发现",
        )
        + bi(
            "<p>For existing context, choose <code>--resume NAME_OR_ID</code> or "
            "<code>--continue</code>. The default timeout is one hour, with a maximum of "
            "24 hours; the default concurrency is four, with at most 16. Prompts and "
            "output stay in private local files. Each log is capped at 4 MiB, can contain "
            "sensitive content, and is not included in support exports. Jobs never "
            "automatically restart or replay failed inference.</p>",
            "<p>要使用已有上下文，明确选择 <code>--resume NAME_OR_ID</code> 或 "
            "<code>--continue</code>。默认超时一小时，最长 24 小时；默认同时运行四个任务，"
            "最多 16 个。提示和输出保存在私有本地文件中，每份日志最多 4 "
            "MiB，可能含敏感内容，不会进入诊断导出。失败后不会自动重启或重发模型请求。</p>",
        )
        + bi(
            "<p>Stop, timeout, revocation or loss of the supervisor ends the owned process "
            "group. Authorization is checked periodically. Only verified cleanup is "
            "reported as confirmed; an unresponsive record is not proof it stopped. Tools "
            "that deliberately detach are not sandbox-contained. Native permissions still "
            "govern local files. Archive an inactive record to preserve its private files "
            "while freeing capacity; archiving never stops an active job.</p>",
            "<p>停止、超时、撤销授权或管理进程丢失时，会结束所管理的进程组；授权按周期检查"
            "。只有清理经过验证才会报告“已确认”，无响应的记录不能证明任务已停止。工具主动脱"
            "离管理时不会因此受到沙箱限制，本地文件仍由原生权限决定。归档已结束的记录可以保"
            "留私有文件并释放名额，不会停止活动任务。</p>",
        )
        + "</div>"
    )


def guide(
    cfg: Config, viewer: Optional[Viewer] = None, *, price: Optional[pricing.Price] = None,
    per_slot_pricing: bool = False,
) -> str:
    price_en = ("Pricing is unavailable until the recorded resource costs support a quote. "
                "Agree the quote and payment directly with the operator.")
    price_zh = "已记录的资源成本支持报价前，价格暂不可用。报价与付款方式直接和运营者约定。"
    if per_slot_pricing:
        price_en = ('Each slot has its own provider, IP type and monthly price in the '
                    '<a href="/#slot-pricing">slot list</a>. Alpha pricing passes through '
                    'recorded monthly VPS and exit-IP costs, plus a 10% service fee with '
                    'a $2 minimum per slot, in USD. Your Claude subscription or Anthropic '
                    'API usage is billed separately. Choose the machine at its displayed '
                    'rate on your account page and pay the operator directly; this site '
                    'does not charge automatically or renew automatically.')
        price_zh = ('每个槽位的服务商、IP 类型及月费见<a href="/#slot-pricing">槽位列表</a>，'
                    'Alpha 定价按已记录的 VPS 与出口 IP 美元月成本，加 10% 服务费，'
                    '每槽位最低 $2。Claude 订阅或 Anthropic API 用量另计。'
                    '在账号页按所示价格选择主机，并直接向运营者付款；'
                    '本站不会自动扣款或自动续费。')
    body = (
        '<div class="dochead">'
        + bi("<h1>Getting started</h1>", "<h1>开始使用</h1>")
        + bi(
            '<p class="lead">Your computer does the work; your own Claude account on a '
            "slot handles model access. Follow these steps to get both ready.</p>",
            '<p class="lead">你的电脑负责工作，槽位上你自己的 Claude '
            "账号负责模型访问。按下面几步，把两边准备好。</p>",
        )
        + '</div><ol class="steps">'
        + "<li>"
        + bi(
            '<h3>Sign in</h3><p>Open <a href="/account">Your slots</a> and choose <span '
            'class="btnlabel">Continue with Google</span>. This creates your Fleet '
            "account. Seeing no slots yet is expected.</p>",
            '<h3>登录本站</h3><p>打开<a href="/account">我的槽位</a>，选择 <span '
            'class="btnlabel">使用 Google 继续</span>。这会创建 Fleet '
            "用户账号；此时没有槽位是正常的。</p>",
        )
        + "</li>"
        + "<li>"
        + bi(
            f"<h3>Get a slot</h3><p>Ask {contact(cfg)} for a slot using the Google "
            f"address you signed in with. {price_en} The operator enables access "
            "for that account.</p>",
            f"<h3>开通槽位</h3><p>联系 {_contact_zh(cfg)}，提供登录本站所用的 Google "
            f"邮箱。{price_zh} 运营者会为这个用户账号开通领取权限。</p>",
        )
        + "</li>"
        + "<li>"
        + bi(
            '<h3>Claim it</h3><p>Choose <span class="btnlabel">Claim a slot</span>. The '
            'card shows <span class="pill busy">Setting up</span> while the machine '
            "creates your separate Linux account and installs Claude Code. This takes a "
            "few minutes; the page updates itself.</p>",
            '<h3>领取槽位</h3><p>点击 <span class="btnlabel">领取槽位</span>。机器会建立独'
            '立的 Linux 用户环境并安装 Claude Code，此时显示 <span class="pill '
            'busy">正在准备</span>。通常需要几分钟，页面会自动更新。</p>',
        )
        + "</li>"
        + "<li>"
        + bi(
            '<h3>Sign in to Claude</h3><p>At <span class="pill warn">Ready to sign '
            'in</span>, choose <span class="btnlabel">Sign in to Claude</span>. Open the '
            "link, sign in with your own paid Claude account, copy its code and paste it "
            'back. Choose <span class="btnlabel">Send code</span>. A completed sign-in '
            'shows <span class="pill ok">In use</span>.</p>',
            '<h3>登录你的 Claude 账号</h3><p>显示 <span class="pill warn">可以登录</span> '
            '后，点击 <span class="btnlabel">登录 Claude</span>。打开链接，用你自己的付费 '
            'Claude 账号登录，把提供的代码复制回来，再点击 <span class="btnlabel">提交代码<'
            '/span>。成功后显示 <span class="pill ok">使用中</span>。</p>',
        )
        + "</li>"
        + "<li>"
        + bi(
            "<h3>Install on your computer</h3><p>Run the command below in your normal "
            "terminal. It keeps an existing pairing and local Claude installation; if "
            "Claude is missing, it installs the original local CLI from the fixed vendor "
            'URL. Only if asked, choose <span class="btnlabel">Connect this computer</span>'
            " on your slot and paste a fresh pairing code.</p>",
            "<h3>在电脑上安装</h3><p>在普通终端运行下面的命令。已有配对和本地 Claude "
            "安装会保留；尚未安装 Claude 时，会从固定的官方地址安装原版客户端。只有终端要求"
            '输入配对码时，才点击槽位上的 <span class="btnlabel">连接这台电脑</span>，输入'
            "新生成的配对码。</p>",
        )
        + "</li></ol>"
        + '<div class="card" id="setup">'
        + bi("<h2>Install, check, then start</h2>", "<h2>安装、检查，然后开始</h2>")
        + "<pre><code>curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/la"
        "ptop/install.sh | bash -s -- --setup\n\ncd ~/code/my-project\nccfleet local "
        "--check\nccfleet local</code></pre>"
        + bi(
            "<p>Open a new terminal after installation if the command is not found. Setup "
            "preserves pairing, configuration and slot sign-in, verifies its digest-pinned "
            "helper, and keeps a backup when it adjusts PATH. Setup and the readiness "
            "check make no model request and upload no project files. The check may wait "
            "for the selected slot to become ready.</p>",
            "<p>安装后若找不到命令，请打开新终端。安装会保留配对、配置和槽位登录，验证固定"
            "摘要的辅助程序；调整 PATH 时会保留备份。安装和就绪检查都不调用模型、不上传项目"
            "文件。检查可能需要等待所选槽位准备好。</p>",
        )
        + bi(
            "<p>When gateway access is enabled, setup saves a separate private connection "
            "key and the HTTPS address. <code>ccfleet local</code> launches original "
            "Claude Code on your computer. Files, tools, settings and history run locally; "
            "supported model requests go through the gateway and your selected own slot. "
            "If this computer is already paired but has no gateway configuration, run "
            "<code>ccfleet gateway setup</code>. Installing the client alone does not "
            "enable a slot that the operator has not activated.</p>",
            "<p>网关开放时，安装会保存独立的私有连接密钥和 HTTPS 地址。<code>ccfleet "
            "local</code> 在你的电脑上启动原版 Claude Code，文件、工具、设置和历史在本地；"
            "支持的模型请求经过网关和你选中的自己的槽位。已配对但还没有网关配置的电脑，运行"
            " <code>ccfleet gateway setup</code> 即可。单独安装客户端不会自动开通运营者尚未"
            "启用的槽位。</p>",
        )
        + "</div>"
        + _model_connections(cfg)
        + '<div class="card" id="local-work">'
        + bi("<h2>Your local files and conversations</h2>", "<h2>本地文件与会话</h2>")
        + bi(
            "<p><code>ccfleet local</code> launches the original local Claude CLI. It does "
            "not upload a project, mount your filesystem on the slot, or require "
            "push/pull. There are no CC Fleet filesystem count/size caps or Git-ignore "
            "filters. Use <code>cd</code> or <code>--project PATH</code> to choose where "
            "you work.</p>",
            "<p><code>ccfleet local</code> 启动原版本地 Claude Code，不上传整个项目、不把文"
            "件系统挂载到槽位，也不需要 push/pull。CC Fleet 不对本地文件数量、大小或 "
            "Git-ignore 另设限制。先 <code>cd</code> 进入项目，或用 <code>--project "
            "PATH</code> 指定目录。</p>",
        )
        + _prompt_example(
            "ccfleet local --new --name work\n"
            "ccfleet local --new --name research --mode plan --model opus --effort high\n"
            "ccfleet local --resume\n"
            "ccfleet local --resume work\n"
            "ccfleet local --continue\n"
            "ccfleet local --resume work --fork-session\n"
            'ccfleet local --print "Summarize this project"',
            "Summarize this project",
            "总结这个项目",
        )
        + bi(
            "<p>Running <code>ccfleet local</code> again starts a new conversation; "
            "<code>--new</code> makes that choice explicit. Existing history is kept. "
            "<code>--resume</code> uses Claude&#x27;s native history picker or an ID/name; "
            "<code>--continue</code> selects the last local conversation for this "
            "directory. Quit with <code>/exit</code> and resume later. Native Claude "
            "settings/history, including an existing <code>CLAUDE_CONFIG_DIR</code>, stay "
            "on this computer.</p>",
            "<p>再次运行 <code>ccfleet local</code> 会新建会话，<code>--new</code> "
            "明确表示新建；已有历史保留。<code>--resume</code> 打开原生历史选择器，或按 "
            "ID/名称恢复；<code>--continue</code> 继续当前目录最近的本地会话。使用 "
            "<code>/exit</code> 退出，以后再恢复。原生 Claude 设置与历史，包括已有的 "
            "<code>CLAUDE_CONFIG_DIR</code>，仍保存在这台电脑上。</p>",
        )
        + bi(
            "<p>The working directory is not a sandbox. Native Claude permissions govern "
            "file and tool access, including outside that directory. New conversations "
            "default to <code>bypassPermissions</code>, Opus and max effort, so tools can "
            "modify, delete or transmit data without individual approval prompts. Choose "
            "<code>--mode plan</code> or another supported mode when appropriate. Inside "
            "Claude, <code>/model</code> and <code>/effort</code> change your choices. "
            "Higher effort usually uses more of your plan allowance.</p>",
            "<p>工作目录不是沙箱。原生 Claude 权限决定文件与工具访问，也可能涉及目录外的文"
            "件。新会话默认使用 <code>bypassPermissions</code>、Opus 和 max "
            "effort，工具可能不逐次询问就修改、删除或传输数据。需要时，选择 <code>--mode "
            "plan</code> 或其他受支持的模式。会话内可用 <code>/model</code> 和 "
            "<code>/effort</code> 调整；更高推理强度通常消耗更多额度。</p>",
        )
        + bi(
            "<p>A temporary private settings overlay fixes the gateway route and "
            "authentication, clears conflicting provider overrides and disables supported "
            "optional telemetry. Ordinary customizations remain; native settings files are "
            "not permanently rewritten. Only supported model endpoints use the gateway. "
            "MCP servers, hooks, plugins, shell tools, updates and other native services "
            "can connect directly from the laptop. Optional telemetry controls are not an "
            "all-traffic firewall.</p>",
            "<p>启动时使用临时的私有设置层，固定网关地址和认证信息，清除冲突的供应商设置，"
            "并关闭受支持的可选遥测。常规自定义设置保留，原生设置文件不会被永久改写。只有受"
            "支持的模型接口走网关；MCP、hooks、插件、shell 工具、更新和其他原生服务仍可能从"
            "电脑直接联网。可选遥测控制不是拦截所有流量的防火墙。</p>",
        )
        + "</div>"
        + _version_choices()
        + _client_tools()
        + '<div class="card" id="slot-care">'
        + bi("<h2>Look after your slot</h2>", "<h2>管理你的槽位</h2>")
        + bi(
            '<p>A slot is signed in to one Claude account, your own. <span class="btnlabel"'
            ">Sign in again</span> uses that account only. To bind another Claude account "
            'of yours, choose <span class="btnlabel">Change account</span> (once a week). '
            "To use two accounts at once, hold two slots. Keep each account on one "
            "machine; a duplicate machine sign-in is flagged to you and the operator.</p>",
            '<p>每个槽位绑定你自己的一个 Claude 账号，<span class="btnlabel">重新登录</span'
            '> 只用于这个账号。要换成另一个自己的账号，点击 <span class="btnlabel">更换账号'
            "</span>，每周一次。需要同时保留两个账号，就持有两个槽位。同一账号应保持在一台"
            "机器上；同时在两台机器登录时，会向你和运营者提示。</p>",
        )
        + bi(
            "<p>Account changes keep files and settings but invalidate requests using the "
            "previous binding. CC Fleet does not automatically close your local Claude "
            "process or delete its history. It cancels an in-flight request if that "
            'account changes. Wait for the slot to show <span class="pill ok">In '
            "use</span> before retrying deliberately; new requests then use its current "
            "bound account. An account change is visible to the operator.</p>",
            "<p>更换账号保留文件与设置，但旧绑定的请求会失效。CC Fleet 不会自动关闭本地 "
            "Claude 进程，也不会删除历史；账号改变时，会取消正在进行的请求。等槽位重新显示 "
            '<span class="pill ok">使用中</span>，再主动重试；新的请求使用当前绑定账号。运'
            "营者能看到账号更换。</p>",
        )
        + bi(
            "<p>Your slot name is also its hostname, which Anthropic can see: pick "
            "anything but your email address. Other slots on the same machine use separate "
            "Linux accounts. There is no administrator access (sudo) in a slot.</p>",
            "<p>槽位名称也用作 hostname，Anthropic 可能看到它，请不要使用邮箱。同一台机器上"
            "的不同槽位使用独立的 Linux 用户，槽位内没有管理员权限（sudo）。</p>",
        )
        + bi(
            "<p>The 5-hour and weekly bars cover the whole Claude account. The token count "
            "covers what Claude Code used on this slot, not complete local-relay usage. "
            'Quota is read every five minutes; <span class="btnlabel">Refresh</span> reads '
            'it now. Pair more devices with <span class="btnlabel">Connect another '
            "computer</span>; each device is independently revocable.</p>",
            "<p>五小时和每周进度条表示整个 Claude 账号的用量；token 数表示这个槽位上的 "
            "Claude Code 使用量，不是完整的本地转发统计。配额每五分钟读取一次，点击 <span "
            'class="btnlabel">刷新</span> 可立即读取。通过 <span class="btnlabel">连接另一'
            "台电脑</span> 添加设备，之后可单独撤销。</p>",
        )
        + bi(
            "<p>Your slot is <strong>not backed up</strong>. Before choosing <span "
            'class="btnlabel">Give this slot back</span>, save any slot files in Git or '
            "storage you own. Giving it back deletes the Linux account and every file in "
            "it; it does not delete your Claude account or local history. For a connection "
            "problem, keep your working pairing, check status and ask your operator.</p>",
            '<p>槽位<strong>没有备份</strong>。点击 <span class="btnlabel">归还这个槽位</sp'
            "an> 之前，请把槽位文件保存在 Git 或自己的存储中。归还会删除 Linux "
            "用户及其中所有文件，不会删除 Claude 账号或本地历史。遇到连接问题，请保留有效配"
            "对、检查状态，再联系运营者。</p>",
        )
        + bi(
            f'<p>Questions: ask {contact(cfg)}. <a href="/privacy">The privacy page</a> '
            "explains what the service processes and stores.</p>",
            f'<p>有问题请联系 {_contact_zh(cfg)}。<a href="/privacy">隐私说明</a>'
            "列出服务处理与保存的信息。</p>",
        )
        + "</div>"
    )
    if per_slot_pricing:
        body += ('<div class="card" id="rental-pricing">'
                 + bi('<h2>Choosing a slot and its rate</h2>', '<h2>选择槽位与价格</h2>')
                 + _rental_pricing_details() + '</div>')
    return _page("/docs/guide", "getting started", body, viewer=viewer)


# -- how it works -----------------------------------------------------------------------


GATEWAY_PICTURE = """<svg class="diag" viewBox="0 0 360 520" role="img"
aria-labelledby="gateway-diag-t gateway-diag-d">
<title id="gateway-diag-t">HTTPS gateway model request path</title>
<desc id="gateway-diag-d">Original Claude Code sends supported model requests over HTTPS
to our gateway. The gateway reads the request and chooses one of your selected own slots.
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
<text x="180" y="160" text-anchor="middle">Our HTTPS gateway</text>
<text class="sub" x="180" y="182" text-anchor="middle">Can read model contents</text>
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

    Each authored variant has its own IDs and references. The HTTP response
    includes only the selected language's diagram.
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


GATEWAY_PICTURE_ZH = _diagram_zh(
    GATEWAY_PICTURE,
    "gateway",
    {
        **_DIAGRAM_COMMON,
        "HTTPS gateway model request path": "HTTPS 网关模型请求路线",
        "Original Claude Code sends supported model requests over HTTPS\nto our "
        "gateway. The gateway reads the request and chooses one of your selected own "
        "slots.\nA restricted SSH service connection reaches that slot, which uses its "
        "native Claude\ncredential over HTTPS to Anthropic. Files, tools and history "
        "remain local.": "原版 Claude Code 通过 HTTPS 将支持的模型请求发给我们的网关。网关"
        "读取请求，在你选中的自己的槽位里选择一个。权限受限的 SSH "
        "服务连接到该槽位，再由槽位用原生 Claude 凭证通过 HTTPS 访问 "
        "Anthropic。文件、工具和历史仍在电脑上。",
        "HTTPS + endpoint key": "HTTPS + 连接密钥",
        "Our HTTPS gateway": "我们的 HTTPS 网关",
        "Can read model contents": "可读取模型消息",
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
    body = (
        '<div class="dochead">'
        + bi("<h1>How it works</h1>", "<h1>CC Fleet 如何工作</h1>")
        + bi(
            '<p class="lead">Think of the gateway as a reception desk and your slot as a '
            "seat reserved for your own Claude account. Your computer sends the question; "
            "the desk chooses one of your selected seats; Anthropic runs the model.</p>",
            '<p class="lead">把网关想成收件前台，把槽位想成留给你自己的 Claude '
            "账号的座位。电脑送出问题，前台从你选中的座位里挑一个，Anthropic "
            "运行模型并回答。</p>",
        )
        + '</div><div class="how"><figure class="card diagram">'
        + bi(GATEWAY_PICTURE, GATEWAY_PICTURE_ZH)
        + "<figcaption>"
        + bi(
            "Model requests go over HTTPS. Your files, tools and history stay local; model "
            "context can contain their contents.",
            "模型请求通过 HTTPS 发送。文件、工具和历史留在本地；模型上下文仍可能包含它们的内容。",
            inline=True,
        )
        + '</figcaption></figure><div class="how-text">'
        + '<div class="card">'
        + bi("<h2>One question, step by step</h2>", "<h2>跟着一个问题走一遍</h2>")
        + bi(
            "<p><code>ccfleet local</code> runs original Claude Code on your computer. "
            "Local tools may read a file and include relevant text in the model request. "
            "The request goes over HTTPS to our gateway with a separate Fleet "
            "connection key.</p>",
            "<p><code>ccfleet local</code> 在你的电脑上运行原版 Claude "
            "Code。本地工具可能读取文件，把相关文字放进模型请求，再带上独立的 Fleet "
            "连接密钥，通过 HTTPS 发给我们的网关。</p>",
        )
        + bi(
            "<p>The gateway checks your key and ownership, then chooses one of your "
            "selected own slots. The gateway uses a restricted inference-only SSH connection to "
            "that slot. The slot uses its bound native Claude credential over HTTPS to "
            "Anthropic. The answer streams back along this route; permitted tools execute "
            "on your computer. Credentials are not copied to your laptop or the gateway.</p>",
            "<p>网关验证密钥与槽位归属，从你选中的自己的槽位中选择一个。网关"
            "通过只允许模型转发的受限 SSH 连接到槽位。槽位使用绑定的原生 Claude 凭证，通过 "
            "HTTPS 请求 Anthropic。回答沿原路分段返回，获准的工具仍在你的电脑上执行；Claude"
            " 凭证不会复制到电脑或网关。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Your seat and your account</h2>", "<h2>你的槽位与你的账号</h2>")
        + bi(
            "<p>Each customer receives a dedicated VPS and dedicated exit IP. The slot "
            "has a private Linux home directory and one bound Claude account. You sign "
            "in yourself through "
            "Anthropic; native Claude owns authentication and renewal. Renewal can fail or "
            "be revoked, so the page may ask you to <strong>Sign in again</strong>.</p>",
            "<p>每位客户获得专属 VPS 与专属出口 IP。槽位有独立的 Linux 主目录，并绑定一个 "
            "Claude 账号。你通过 Anthropic "
            "亲自登录，认证与续期由原版 Claude 管理。续期可能失败，登录也可能被撤销，因此页"
            "面仍可能要求<strong>重新登录</strong>。</p>",
        )
        + bi(
            "<p>Your slot card distinguishes reported ready, renewal pending, sign-in "
            "required and account maintenance. Stale observations do not claim readiness. "
            "A heartbeat does not prove provider acceptance. Read-only diagnostics check "
            "connections without a model request or project scan. Machines keep their "
            "clocks on UTC; slot cards display reset times in your own time zone.</p>",
            "<p>卡片区分已报告就绪、等待续期、需要登录和账号维护。过期状态不会被当作当前就"
            "绪。机器状态报告不保证服务商会接受下一条消息；只读诊断不调用模型，也不扫描项目"
            "。机器时钟使用 UTC；槽位卡片上的额度重置时间按你所在的时区显示。</p>",
        )
        + "</div>"
        + _model_connections(cfg)
        + '<div class="card">'
        + bi("<h2>Who can read what</h2>", "<h2>谁能看到什么</h2>")
        + bi(
            "<p><strong>The gateway can read model requests and responses.</strong> HTTPS protects "
            "each connection, but the gateway decrypts messages to route them. The gateway, the "
            "selected slot and Anthropic process model context, including any prompts, "
            "file contents and tool results it contains. Local files stay local as files; "
            "relevant contents can still be sent to the model.</p>",
            "<p><strong>网关可以读取模型请求和回复。</strong>HTTPS "
            "保护每段连接，但网关为路由会解密消息。网关、选中的槽位和 Anthropic "
            "都会处理模型上下文，包括其中的提示、文件内容和工具结果。文件本身仍在本地，但相"
            "关内容可能发给模型。</p>",
        )
        + bi(
            "<p>The Fleet server receives slot sign-in state, the bound Claude email and "
            "plan, quota observations, hourly token counts and paired-device facts. Those "
            "heartbeat reports do not contain prompts, conversations, file contents or the "
            "Claude credential. Separately, the direct HTTPS gateway receives and "
            "processes model requests. The server stores only a hash of your connection "
            "key; the native Claude credential remains on the slot.</p>",
            "<p>Fleet 服务收到槽位登录状态、绑定的 Claude 邮箱与订阅、配额观察、按小时的 "
            "token 数和设备配对信息。这样的状态报告不包含提示、会话、文件内容或 Claude "
            "凭证；我们的 HTTPS 网关则另外接收和处理模型消息。服务端只保存连接密钥的哈希"
            "，原生 Claude 凭证留在槽位。</p>",
        )
        + bi(
            "<p>The relay removes selected headers and top-level structured metadata. It "
            "does not redact arbitrary prompts or tool results. Native system prompts may "
            "include local OS, working directory or environment details. This is not "
            "anonymity or a zero-metadata guarantee. MCP servers, hooks, plugins, tools "
            "and updates may connect directly from the laptop.</p>",
            "<p>转发时会移除部分请求头和顶层结构化 metadata，但不会自动删去提示或工具结果中"
            "的任意个人信息。原生系统提示可能包含本地系统、工作目录或环境信息。这不是匿名或"
            "零元数据保证。MCP、hooks、插件、工具和更新仍可能从电脑直接联网。</p>",
        )
        + bi(
            "<p>Machine administrators have root and can technically inspect any slot. The "
            "operator console does not display native credentials or full file contents, "
            'but that does not remove root access. Read <a href="/privacy">Privacy</a> for '
            "the full data scope and retention.</p>",
            "<p>机器管理员拥有 root 权限，技术上能检查任意槽位。管理界面不显示原生凭证或完"
            '整文件，但这不消除 root 访问能力。完整数据范围与保存时长见<a href="/privacy">'
            "隐私说明</a>。</p>",
        )
        + '</div><div class="card">'
        + bi("<h2>Updates, outages and returning a slot</h2>", "<h2>更新、故障与归还</h2>")
        + bi(
            "<p>Local Claude updates separately from the slot and the Fleet client. "
            "Automatic updates are disabled during a CC Fleet launch; update your local "
            "Claude installation separately, outside that session. Machine security "
            "updates install daily; necessary reboots are scheduled by the operator. Check "
            '<a href="/status">Status</a> for website and machine availability, and '
            'the <a href="/status#performance">fleet performance panel</a> for combined '
            "relay measurements. It shows average upstream timing and transfer completion "
            "when enough users contribute data, without listing individual accounts.</p>",
            "<p>电脑上的 Claude、槽位上的 Claude 和 Fleet 客户端分别更新。CC Fleet "
            "启动期间关闭 Claude 自动更新，请在会话外单独更新本地安装。机器每天安装安全更新"
            '，需要重启时由运营者安排。网站和机器是否在线，可以查看<a href="/status">服务状'
            '态</a>；<a href="/status#performance">整体性能面板</a>汇总转发统计。'
            "达到参与用户和样本数量要求后，展示上游平均时长及传输完成率，不列出单个账号。</p>",
        )
        + bi(
            '<p>The <a href="/status#gateway-machine">gateway machine resources</a> panel '
            "shows the Fleet gateway VPS and Fleet service resource use. A busy CPU, low available "
            "memory or a nearly full data filesystem can indicate pressure. These readings "
            "alone do not prove where a slow request spends its time.</p>",
            '<p><a href="/status#gateway-machine">网关主机资源</a>面板展示 Fleet 网关 VPS 和 Fleet '
            "服务的资源占用。CPU 繁忙、可用内存少或数据所在文件系统快满，都可能提示资源压力；"
            "这些数值本身不能证明一次慢请求把时间花在了哪里。</p>",
        )
        + bi(
            '<p>The <a href="/status#slot-machines">slot-machine details</a> list each '
            "monitored machine as Operational, Degraded or Down, with the machine-level "
            "reason and last report. Stable labels such as Machine 1 protect holder names; "
            "your slot card links to the matching label. A completed slot release clears "
            "your chosen name and returns the machine under its reserved pool-N name.</p>",
            '<p><a href="/status#slot-machines">槽位主机详情</a>逐台展示已监测主机的正常、异常'
            "或离线状态，以及主机层面的原因和最近报告时间。“主机 1”等固定标签保护持有者"
            "的名称；你的槽位卡片链接到对应标签。槽位归还且删除确认完成后，会清除你选择的"
            "名称，并恢复该槽位保留的 pool-N 名称。</p>",
        )
        + bi(
            "<p>Model requests need the configured gateway and an eligible slot. A gateway "
            "outage reports an error. Giving a slot back stops its processes and deletes "
            "the Linux account and all slot files. It is offered again only after the "
            "machine confirms deletion; local files and history are not deleted.</p>",
            "<p>模型请求需要网关在线，并有可用的所选槽位。网关故障会报错。归还槽位会停止其"
            "中的进程，删除 Linux 用户与所有槽位文件；只有机器确认删除完成，槽位才会再次开"
            "放领取。本地文件和历史不会因此删除。</p>",
        )
        + "</div></div></div>"
    )
    body += '<div class="card" id="rental-pricing">' + bi(
        '<h2>Slot providers, IP types and rates</h2>',
        '<h2>槽位服务商、IP 类型与价格</h2>',
    ) + _rental_pricing_details() + bi(
        '<p>An ISP slot can use a '
        'separate approved exit proxy; provider HTTPS remains encrypted through that '
        'tunnel. Your account page shows the same labels before you choose a slot.</p>',
        '<p>ISP 槽位可以使用独立获准的出口代理，'
        '与上游的 HTTPS 仍通过隧道保持加密。账号页在选择槽位前展示相同的信息。</p>',
    ) + '</div>'
    return _page("/docs/how-it-works", "how it works", body, width="", viewer=viewer)


# -- terms ------------------------------------------------------------------------------


def terms(
    cfg: Config, viewer: Optional[Viewer] = None, *, price: Optional[pricing.Price] = None,
    per_slot_pricing: bool = False,
) -> str:
    paying = ("Pricing is unavailable until the recorded resource costs support a quote. "
              "Agree the quote and paid period directly with the operator, who enables "
              "access after payment; this site does not charge or renew automatically.")
    paying_zh = ("已记录的资源成本支持报价前，价格暂不可用。报价及服务有效期直接和运营者约定，"
                 "付款后开通；本站不会自动扣款或自动续费。")
    if per_slot_pricing:
        paying = ('Monthly rates are listed separately for each slot in the '
                  '<a href="/#slot-pricing">slot list</a>. Alpha pricing adds a service '
                  'fee of 10% of recorded monthly VPS and exit-IP resource cost, with a '
                  '$2 minimum per slot, in USD. Your Claude subscription or Anthropic API '
                  'usage is billed separately. Agree the price and paid period directly '
                  'with the operator; claiming a slot does not charge you automatically '
                  'or renew automatically.')
        paying_zh = ('各槽位月费分别列于<a href="/#slot-pricing">槽位列表</a>：'
                     'Alpha 定价在已记录的 VPS 与出口 IP 美元月度资源成本上，加 10% 服务费，'
                     '每槽位最低 $2。Claude 订阅或 Anthropic API 用量另计。'
                     '价格及服务有效期直接与运营者约定，领取槽位不会自动扣款或自动续费。')
    body = (
        '<div class="dochead">'
        + bi("<h1>Terms</h1>", "<h1>服务条款</h1>")
        + bi(
            f'<p class="sub">Last updated {DOCS_UPDATED}</p>',
            f'<p class="sub">最后更新：{DOCS_UPDATED}</p>'
        )
        + '</div><div class="card">'
        + bi("<h2>The service</h2>", "<h2>服务内容</h2>")
        + bi(
            "<p>A slot provides one person with a dedicated VPS and dedicated exit IP, "
            "with a private Linux account and Claude Code installed. You bring your own "
            "Claude plan and sign in to it "
            "yourself; ccfleet does not provide access to Claude. CC Fleet is independent "
            "and is not affiliated with or endorsed by Anthropic.</p>",
            "<p>槽位为一人提供专属 VPS 与专属出口 IP，包含独立 Linux 用户环境，并安装了 Claude "
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
    if per_slot_pricing:
        body += ('<div class="card" id="rental-pricing">'
                 + bi('<h2>Per-machine rental rates</h2>', '<h2>逐台主机的出租价格</h2>')
                 + _rental_pricing_details() + '</div>')
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
