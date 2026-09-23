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

from .config import Config
from .monitor import LOGIN_MAX_AGE_S
from .usersite import _shell, _span

#: When these pages last changed in substance. Change it with the words.
DOCS_UPDATED = "2026-09-23"

DOCS_CSS = """
.doc-nav{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;margin:0 0 22px;
font-size:13.5px}
.doc-nav a{color:var(--muted);text-decoration:none}
.doc-nav a.here{color:var(--ink);font-weight:700}
.doc-nav .brand{color:var(--ink);font-weight:800;font-size:16px;margin-right:6px}
.doc-nav .cta{margin-left:auto;color:var(--acc);font-weight:600}
.lead{font-size:17px;line-height:1.6;margin:0 0 22px}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;
margin:0 0 14px}
.grid3 .card{margin:0}
.steps{list-style:none;counter-reset:step;padding:0;margin:0}
.steps>li{counter-increment:step;position:relative;padding:14px 16px 14px 56px;
background:var(--panel);border:1px solid var(--rule);border-radius:12px;margin:0 0 12px;
box-shadow:var(--shadow)}
.steps>li::before{content:counter(step);position:absolute;left:16px;top:14px;width:26px;
height:26px;border-radius:50%;background:var(--acc);color:var(--on-acc);font-weight:700;
font-size:13px;display:flex;align-items:center;justify-content:center}
.steps h3{margin:0 0 4px;font-size:15.5px}
.steps p{margin:4px 0}
.btnlabel{display:inline-block;border:1px solid var(--rule);border-radius:8px;padding:0 7px;
font-size:12.5px;background:var(--inset);white-space:nowrap}
.diag{display:block;width:100%;max-width:460px;height:auto;margin:6px auto 4px}
.diag .box{fill:var(--panel);stroke:var(--rule);stroke-width:1.5}
.diag .you{stroke:var(--acc);stroke-width:2}
.diag .slot{fill:var(--acc-soft);stroke:var(--acc);stroke-width:2}
.diag .side{fill:var(--inset);stroke:var(--rule);stroke-dasharray:5 4}
.diag text{fill:var(--ink);font:600 14px var(--sans)}
.diag text.sub{fill:var(--muted);font:12.5px var(--sans)}
.diag .arrow{stroke:var(--acc);stroke-width:2;fill:none}
.diag .faint{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:4 4;fill:none}
.diag .head{fill:var(--acc)}
.diag .headfaint{fill:var(--muted)}
.card h3{margin:14px 0 6px;font-size:15px}
.card ul{margin:8px 0;padding-left:20px}.card li{margin:6px 0;line-height:1.5}
"""

_HERE = ' class="here"'
PAGES_TITLES = (("/docs", "Overview"), ("/docs/guide", "Getting started"),
                ("/docs/how-it-works", "How it works"), ("/privacy", "Privacy"),
                ("/docs/terms", "Terms"))


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


def _page(here: str, title: str, body: str) -> str:
    """A docs page in the user site's look, with the docs' own navigation."""
    links = "".join(
        f'<a href="{path}"{_HERE if path == here else ""}>{escape(name)}</a>'
        for path, name in PAGES_TITLES)
    nav = (f'<nav class="doc-nav"><a class="brand" href="/docs">ccfleet</a>{links}'
           '<a class="cta" href="/account">Sign in &rarr;</a></nav>')
    return _shell(title, nav + body, extra_css=DOCS_CSS)


# -- the overview -------------------------------------------------------------------

def overview(cfg: Config) -> str:
    body = (
        "<h1>Claude Code on a machine that is always on</h1>"
        '<p class="lead">ccfleet gives you a <strong>slot</strong>: your own Linux account on a '
        "machine we run, with Claude Code installed and signed in to <em>your own</em> Claude "
        "account. Open claude.ai/code or the Claude app on any device, pick your slot, and "
        "Claude works there, on your files and with your tools, while your laptop is closed.</p>"
        '<div class="grid3">'
        '<div class="card"><h2>What you get</h2><ul>'
        "<li>Your own Linux account: a home directory only you can read, and room for your "
        "projects and tools.</li>"
        "<li>Claude Code installed, with Remote Control on, kept up to date for you.</li>"
        "<li>Your own page, showing your slot and how much of your Claude usage limits is "
        "used.</li></ul></div>"
        '<div class="card"><h2>What you need</h2><ul>'
        "<li>Your own paid Claude plan that includes Claude Code: <strong>Pro, Max, Team or "
        "Enterprise</strong>. On Team and Enterprise, your organisation&#x27;s owner must "
        "turn Remote Control on. API keys don&#x27;t work.</li>"
        "<li>A Google account, to sign in here.</li></ul>"
        '<p class="muted">ccfleet sells the machine, not Claude. Nobody else&#x27;s Claude '
        "account is ever shared with you, and yours is never shared with anybody.</p></div>"
        '<div class="card"><h2>How to buy</h2>'
        '<p>First <a href="/account">sign in once</a> with Google, so there is an account '
        f"to switch on. Then ask {contact(cfg)} for a slot, and pay them. Slots are sold "
        "directly by the operator; price and payment are agreed with them.</p>"
        "<p>Once they have switched it on, a <span class=\"btnlabel\">Claim a slot</span> "
        "button is waiting on your page.</p></div>"
        "</div>"
        '<div class="card"><h2>Read next</h2><ul>'
        '<li><a href="/docs/guide">Getting started</a>: from buying to your first session, '
        "step by step.</li>"
        '<li><a href="/docs/how-it-works">How it works</a>: where your work runs, who can see '
        "what, and how it is kept up to date.</li>"
        '<li><a href="/privacy">Privacy</a> and <a href="/docs/terms">Terms</a>.</li>'
        "</ul></div>")
    return _page("/docs", "about", body)


# -- getting started ----------------------------------------------------------------

def guide(cfg: Config) -> str:
    body = (
        "<h1>Getting started</h1>"
        '<p class="lead">From buying a slot to your first Claude Code session. It takes a few '
        "minutes, most of which is the machine setting your slot up.</p>"
        '<ol class="steps">'
        "<li><h3>Sign in</h3>"
        '<p>Open <a href="/account">your page</a> and choose '
        '<span class="btnlabel">Continue with Google</span>. That makes your account; it '
        "says you have no slots yet, which is expected.</p></li>"
        "<li><h3>Get a slot</h3>"
        f"<p>Ask {contact(cfg)} for a slot, tell them the Google address you signed in "
        "with, and pay them. They switch your slot on for that account.</p></li>"
        "<li><h3>Claim it</h3>"
        '<p>Press <span class="btnlabel">Claim a slot</span>. The card shows '
        '<span class="btnlabel">Setting up</span> while the machine creates your Linux '
        "account and installs Claude Code, which takes a few minutes. The page updates "
        "itself.</p></li>"
        "<li><h3>Sign in to Claude</h3>"
        '<p>When the card says <span class="btnlabel">Ready to sign in</span>, press '
        '<span class="btnlabel">Sign in to Claude</span> and open the link it shows. Sign in '
        "with your own Claude account, copy the code Claude gives you, paste it into the box "
        'and press <span class="btnlabel">Send code</span>. The card turns '
        '<span class="btnlabel">In use</span>.</p></li>'
        "<li><h3>Use it</h3>"
        "<p>Remote Control comes on within a minute. Open "
        '<a href="https://claude.ai/code" target="_blank" rel="noopener noreferrer">'
        "claude.ai/code</a> in any browser, or the Claude app on iOS or Android, signed in to "
        "the same Claude account. Your slot appears under the machine&#x27;s name, the one "
        "your slot card shows; start a session there. Everything runs on the machine, in "
        "your slot, with your files.</p></li>"
        "</ol>"
        '<div class="card"><h2>Also on your page</h2><ul>'
        "<li><strong>Your usage.</strong> Your Claude account&#x27;s 5-hour and weekly limits, "
        "counted across every device you use, and the tokens used on this slot over the last "
        "week.</li>"
        '<li><strong>Device tokens.</strong> <span class="btnlabel">Get a device token</span> '
        "makes a one-year token from your own Claude account, for running Claude Code on "
        "your own computer. It is shown until you press "
        '<span class="btnlabel">Done with it</span>, for at most '
        f"{_span(LOGIN_MAX_AGE_S)}, and never kept after that.</li>"
        '<li><strong>Giving it back.</strong> Tick the box and press '
        '<span class="btnlabel">Give this slot back</span>. Your Linux account and every file '
        "in it are deleted; your Claude account is not touched. Push your work somewhere "
        "first.</li></ul></div>"
        '<div class="card"><h2>Good to know</h2><ul>'
        "<li>Your slot is <strong>not backed up</strong>. Keep your work in git, or anywhere "
        "else that is yours.</li>"
        "<li>You can install tools in your home directory; there is no administrator access "
        "(sudo) in a slot.</li>"
        "<li>If Claude asks you to sign in again, your slot card offers "
        '<span class="btnlabel">Sign in again</span>.</li>'
        "<li>Your slot does not show up in claude.ai/code? Give Remote Control a minute after "
        "signing in, check you are signed in to the same Claude account there, then "
        "reload.</li>"
        f"<li>Anything else: ask {contact(cfg)}.</li></ul></div>")
    return _page("/docs/guide", "getting started", body)


# -- how it works -----------------------------------------------------------------------

#: Portrait, so it stays legible at phone width: you at the top, Anthropic in
#: the middle, your slot at the bottom, and ccfleet's server off to the side,
#: outside the path your work takes.
PICTURE = """<svg class="diag" viewBox="0 0 360 520" role="img" aria-labelledby="diag-t diag-d">
<title id="diag-t">Where your work runs</title>
<desc id="diag-d">Your browser or the Claude app talks to Anthropic; Anthropic talks to Claude Code
in your slot on our machine. ccfleet's server only receives facts from the machine and is not in
that path.</desc>
<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="head" d="M0,0 L10,5 L0,10 z"/></marker>
<marker id="af" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="headfaint" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="box you" x="20" y="14" width="320" height="70" rx="12"/>
<text x="180" y="44" text-anchor="middle">You</text>
<text class="sub" x="180" y="66" text-anchor="middle">claude.ai/code or the Claude app</text>
<path class="arrow" d="M180,88 L180,146" marker-start="url(#ah)" marker-end="url(#ah)"/>
<text class="sub" x="190" y="122">Remote Control</text>
<rect class="box" x="20" y="150" width="320" height="70" rx="12"/>
<text x="180" y="180" text-anchor="middle">Anthropic</text>
<text class="sub" x="180" y="202" text-anchor="middle">Claude, under your own account</text>
<path class="arrow" d="M180,224 L180,282" marker-start="url(#ah)" marker-end="url(#ah)"/>
<text class="sub" x="190" y="258">direct, not through ccfleet</text>
<rect class="slot" x="20" y="286" width="320" height="104" rx="12"/>
<text x="180" y="316" text-anchor="middle">Your slot, on our machine</text>
<text class="sub" x="180" y="340" text-anchor="middle">your Linux account and files</text>
<text class="sub" x="180" y="360" text-anchor="middle">Claude Code, signed in as you</text>
<text class="sub" x="180" y="380" text-anchor="middle">your Claude sign-in stays here</text>
<path class="faint" d="M180,394 L180,442" marker-end="url(#af)"/>
<text class="sub" x="190" y="424">health facts only</text>
<rect class="side" x="60" y="446" width="240" height="60" rx="12"/>
<text x="180" y="472" text-anchor="middle">ccfleet&#x27;s server</text>
<text class="sub" x="180" y="492" text-anchor="middle">no code, prompts or credentials</text>
</svg>"""


def how_it_works(cfg: Config) -> str:
    body = (
        "<h1>How it works</h1>"
        '<p class="lead">Your work runs in your slot, on our machine. Your conversations go '
        "between you, Anthropic and your slot; ccfleet&#x27;s own server is not in that "
        "path.</p>"
        f'<div class="card">{PICTURE}</div>'
        '<div class="card"><h2>Your slot</h2>'
        "<p>A slot is a Linux account of its own on a shared machine: a home directory only "
        "you can read, your own Claude Code, and two services that run while nobody is "
        "logged in: a work session, and Remote Control, which is what claude.ai/code and "
        "the Claude app connect to. There is no administrator access in a slot, and other "
        "slots on the machine cannot read yours.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>You sign in to Claude yourself, through Anthropic&#x27;s own sign-in. The "
        "credential that creates is written on the machine, in your slot, and nowhere else: "
        "ccfleet&#x27;s server passes along the sign-in link and the code you paste, and "
        "never receives or keeps the credential. Claude Code in your slot talks to Anthropic "
        "directly. ccfleet does not relay, pool or rewrite anybody&#x27;s requests, and no "
        "Claude account is ever shared between people.</p></div>"
        '<div class="card"><h2>What we can and cannot see</h2>'
        "<p>ccfleet&#x27;s server receives facts about your slot: whether Claude Code is "
        "signed in, your plan, how much of your usage limits is used, and token counts per "
        "hour. Never your prompts, conversations, files or credential. "
        '<a href="/privacy">The privacy page</a> lists everything, and for how long.</p>'
        "<p>One limit is worth saying plainly: the machines are ours, and their "
        "administrators have root, so they can technically read any slot. No feature does "
        "this and we do not look, but nothing can make it impossible. Keep nothing in a slot "
        "that you could not accept an administrator being able to read.</p></div>"
        '<div class="card"><h2>Kept up to date</h2><ul>'
        "<li><strong>Claude Code</strong> in your slot is updated automatically, on the "
        "release channel the operator sets, normally Anthropic&#x27;s stable channel. A "
        "session that is running is never interrupted; the new version is used from the "
        "next one.</li>"
        "<li><strong>Security updates</strong> for the machine install every day.</li>"
        "<li><strong>Reboots</strong>: when an update needs one, the machine says so and the "
        "operator reboots it at a quiet time. Your slot and its services come back by "
        "themselves.</li></ul></div>"
        '<div class="card"><h2>Giving a slot back</h2>'
        "<p>When you give a slot back, the machine stops everything running in it and deletes "
        "your Linux account and every file in it. The slot is offered to anybody else only "
        "after the machine itself confirms your account is gone, so nobody is ever handed "
        "your files.</p></div>"
        '<div class="card"><h2>Where</h2>'
        "<p>The machines are in California. Several slots share a machine, and its internet "
        "address. ccfleet&#x27;s code is open source: "
        '<a href="https://github.com/cdcupt/ccfleet" target="_blank" '
        'rel="noopener noreferrer">github.com/cdcupt/ccfleet</a>.</p></div>')
    return _page("/docs/how-it-works", "how it works", body)


# -- terms ------------------------------------------------------------------------------

def terms(cfg: Config) -> str:
    body = (
        "<h1>Terms</h1>"
        f'<p class="sub">Last updated {escape(DOCS_UPDATED)}</p>'
        '<div class="card"><h2>The service</h2>'
        "<p>A slot is a Linux account on a machine the operator runs, for one person, with "
        "Claude Code installed. You bring your own Claude plan and sign in to it yourself; "
        "ccfleet does not provide access to Claude.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>Use your own Claude account in your slot, and only yours. You are responsible for "
        "following Anthropic&#x27;s terms and usage policy, as you would on your own "
        "computer. Do not share your slot, or your sign-in, with anybody else.</p></div>"
        '<div class="card"><h2>Paying</h2>'
        "<p>Price and payment are agreed directly with the operator, who switches your slot "
        "on when you pay. If a payment lapses, the operator may take a slot back, and taking "
        "a slot back deletes everything in it.</p></div>"
        '<div class="card"><h2>Your files</h2>'
        "<p>Slots are <strong>not backed up</strong>. Giving a slot back, or having it taken "
        "back, deletes everything in it for good. Keep your work in git, or anywhere else "
        "that is yours.</p></div>"
        '<div class="card"><h2>Fair use</h2>'
        "<p>Use your slot for your own work with Claude Code. Do not use it to attack or scan "
        "other systems, mine cryptocurrency, send spam, break the law, or reach other "
        "people&#x27;s slots. The operator may take back a slot that is used this way.</p>"
        "</div>"
        '<div class="card"><h2>Availability</h2>'
        "<p>The service is run with care but without a guarantee of uptime. Machines are "
        "updated and occasionally rebooted, and a machine can fail.</p></div>"
        '<div class="card"><h2>Changes and contact</h2>'
        "<p>If these terms change, this page changes and the date at the top says when. "
        f"Questions go to {contact(cfg)}. How your information is handled is on the "
        '<a href="/privacy">privacy page</a>.</p></div>')
    return _page("/docs/terms", "terms", body)


PAGES: dict[str, Callable[[Config], str]] = {
    "/docs": overview, "/docs/guide": guide,
    "/docs/how-it-works": how_it_works, "/docs/terms": terms,
}


def page_for(path: str) -> Optional[Callable[[Config], str]]:
    """The page at this path, trailing slash or not; None when there is none."""
    return PAGES.get(path.rstrip("/") or "/")
