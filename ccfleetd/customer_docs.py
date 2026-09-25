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
from .config import Config
from .monitor import LOGIN_MAX_AGE_S
from .render import _meter
from .usersite import NAV, Viewer, _shell, _span

#: When these pages last changed in substance. Change it with the words.
DOCS_UPDATED = "2026-09-24"

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
.band{margin-top:40px}.band>h2{font-size:22px}
.steps>li{padding:16px 16px 14px 58px}
.steps>li::before{left:16px;top:16px;width:28px;height:28px}
.lead{font-size:16.5px}.hero .lead{font-size:17px}}
"""

#: The pages, in the order the bar on top links them. The bar lives in the
#: user site's frame, so every page carries the same one.
PAGES_TITLES = NAV


def _cost(price: pricing.Price) -> str:
    """The price in a sentence, bold, escaped: "<strong>$20</strong> a month"."""
    return f"<strong>{escape(pricing.display(price))}</strong> a {pricing.PERIOD}"


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


def _page(here: str, title: str, body: str, width: str = "doc",
          viewer: Optional[Viewer] = None, canonical: str = "") -> str:
    """A docs page in the site's frame, with its own link in the bar marked."""
    return _shell(title, body, extra_css=DOCS_CSS, here=here, width=width, viewer=viewer,
                  canonical=canonical)


# -- the overview -------------------------------------------------------------------

def _icon(paths: str) -> str:
    """A line icon in the text's own colour, so it follows the theme."""
    return ('<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            f"{paths}</svg>")


ICONS = {
    "account": _icon('<rect x="3" y="4" width="18" height="16" rx="2.5"/>'
                     '<path d="M7.5 9.5l3 2.5-3 2.5M12.5 15h4"/>'),
    "machine": _icon('<rect x="3.5" y="4" width="17" height="6.5" rx="2"/>'
                     '<rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/>'
                     '<path d="M7 7.25h3M7 16.75h3"/>'
                     '<circle cx="16.5" cy="7.25" r=".9"/><circle cx="16.5" cy="16.75" r=".9"/>'),
    "usage": _icon('<path d="M4 19v-8M9.33 19V6M14.67 19v-5M20 19V9"/>'),
}


def _demo() -> str:
    """The landing page's picture: a slot, as its holder's own page shows it.

    Built from the page's own parts rather than drawn, so it cannot drift from
    what somebody sees once they have a slot. Hidden from screen readers; the
    caption says what it shows.
    """
    return (
        '<figure class="hero-demo"><div class="demo-card" aria-hidden="true">'
        '<div class="demo-head"><b>slot-4821</b><span class="pill ok">In use</span></div>'
        '<div class="demo-body">'
        '<p class="demo-meta">machine last heard 12s ago</p>'
        '<p class="signed">Signed in as <strong>alice@example.com</strong> &middot; '
        'Max 20x plan.</p>'
        '<p class="rc on">Remote Control is on: pick slot-4821 in claude.ai/code.</p>'
        '<div class="demo-usage">'
        + _meter(34, "5-hour session", "at 11:40pm") + _meter(61, "This week", "on Friday")
        + "</div></div></div>"
        "<figcaption>An example of your page: the slot, your Claude account on it, and how "
        "much of your usage limits is left.</figcaption></figure>")


def overview(cfg: Config, viewer: Optional[Viewer] = None, *,
             price: Optional[pricing.Price] = None, health: Optional[str] = None) -> str:
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
        console = (f'<a class="btn big" href="{escape(viewer.console_href)}">Console</a>'
                   if viewer.operator else "")
        way_in = ('<div class="cta-row"><a class="btn primary big" '
                  f'href="{escape(viewer.slots_href)}">Your slots</a>{console}{how}</div>')
    else:
        sign_in = "/auth/google/start?next=/account" if cfg.google_ready else "/account"
        way_in = (f'<div class="cta-row"><a class="btn primary big" href="{sign_in}">Sign in '
                  f"with Google</a>{how}</div>"
                  '<p class="muted small">Already have a slot? '
                  '<a href="/account">Go to your slots</a>.</p>')
    # With a price the operator set, say it; without one, say it is agreed with them.
    paying = (f"A slot costs {_cost(price)}, paid directly to the operator; this site takes no "
              "card and no payment." if price is not None else
              "Slots are sold directly by the operator; price and payment are agreed with them.")
    body = (
        '<section class="hero"><div class="hero-copy"><div class="hero-top">'
        '<p class="eyebrow">Bring your own Claude plan</p>'
        + statuspage.pill(health) + "</div>"
        "<h1>Claude Code on a machine that is always on</h1>"
        '<p class="lead">ccfleet gives you a <strong>slot</strong>: your own Linux account on a '
        "machine we run, with Claude Code installed and signed in to <em>your own</em> Claude "
        "account. Your slot is a whole machine, under a name you choose. Open claude.ai/code "
        "or the Claude app on any device, pick it by that name, and Claude works there, on "
        "your files and with your tools, while your laptop is closed.</p>"
        + way_in + "</div>" + _demo() + "</section>"
        '<section class="band"><h2>What you get</h2>'
        '<p class="band-lead">For anybody with a Claude plan that includes Claude Code who '
        "wants it running somewhere that stays on.</p>"
        '<div class="features">'
        f'<div class="feature">{ICONS["account"]}<h3>Your own Linux account</h3>'
        "<p>A home directory only you can read, and room for your projects and tools.</p></div>"
        f'<div class="feature">{ICONS["machine"]}<h3>Claude Code, always on</h3>'
        "<p>Installed, with Remote Control on, and kept up to date for you.</p></div>"
        f'<div class="feature">{ICONS["usage"]}<h3>Your own page</h3>'
        "<p>Your slot, and how much of your Claude usage limits is used.</p></div>"
        "</div></section>"
        '<section class="band two">'
        '<div class="card"><h2>What you need</h2><ul>'
        "<li>Your own paid Claude plan that includes Claude Code: <strong>Pro, Max, Team or "
        "Enterprise</strong>. On Team and Enterprise, your organisation&#x27;s owner must "
        "turn Remote Control on. API keys don&#x27;t work.</li>"
        "<li>A Google account, to sign in here.</li></ul>"
        '<p class="callout">ccfleet sells the machine, not Claude. Nobody else&#x27;s Claude '
        "account is ever shared with you, and yours is never shared with anybody.</p></div>"
        '<div class="card"><h2>How to buy</h2><ol class="buy">'
        '<li>First <a href="/account">sign in once</a> with Google, so there is an account '
        "to switch on.</li>"
        f"<li>Then ask {contact(cfg)} for a slot, and pay them. {paying}</li>"
        "<li>Once they have switched it on, a <span class=\"btnlabel\">Claim a slot</span> "
        "button is waiting on your page.</li></ol></div>"
        "</section>"
        '<section class="band"><h2>Read next</h2><div class="next">'
        '<a href="/docs/guide"><b>Getting started</b><span>From buying to your first session, '
        "step by step.</span></a>"
        '<a href="/docs/how-it-works"><b>How it works</b><span>Where your work runs, who can '
        "see what, and how it is kept up to date.</span></a>"
        '<a href="/privacy"><b>Privacy</b><span>What we keep about you, why, and for how '
        "long.</span></a>"
        '<a href="/status"><b>Status</b><span>Whether this site and the machines are up, now '
        "and over the last 90 days.</span></a>"
        "</div></section>")
    return _page("/", "about", body, width="", viewer=viewer, canonical="/")


# -- getting started ----------------------------------------------------------------

def guide(cfg: Config, viewer: Optional[Viewer] = None, *,
          price: Optional[pricing.Price] = None) -> str:
    paying = (f"and pay them: {_cost(price)}, paid directly to them; this site takes no "
              "card. They switch" if price is not None else "and pay them. They switch")
    body = (
        '<div class="dochead"><h1>Getting started</h1>'
        '<p class="lead">From buying a slot to your first Claude Code session. It takes a few '
        "minutes, most of which is the machine setting your slot up.</p></div>"
        '<ol class="steps">'
        "<li><h3>Sign in</h3>"
        '<p>Open <a href="/account">your page</a> and choose '
        '<span class="btnlabel">Continue with Google</span>. That makes your account; it '
        "says you have no slots yet, which is expected.</p></li>"
        "<li><h3>Get a slot</h3>"
        f"<p>Ask {contact(cfg)} for a slot, tell them the Google address you signed in "
        f"with, {paying} your slot on for that account.</p></li>"
        "<li><h3>Claim it</h3>"
        '<p>Press <span class="btnlabel">Claim a slot</span>. The card shows '
        '<span class="pill busy">Setting up</span> while the machine creates your Linux '
        "account and installs Claude Code, which takes a few minutes. The page updates "
        "itself.</p></li>"
        "<li><h3>Sign in to Claude</h3>"
        '<p>When the card says <span class="pill warn">Ready to sign in</span>, press '
        '<span class="btnlabel">Sign in to Claude</span> and open the link it shows. Sign in '
        "with your own Claude account, copy the code Claude gives you, paste it into the box "
        'and press <span class="btnlabel">Send code</span>. The card turns '
        '<span class="pill ok">In use</span>.</p></li>'
        "<li><h3>Use it</h3>"
        "<p>Remote Control comes on within a minute. Open "
        '<a href="https://claude.ai/code" target="_blank" rel="noopener noreferrer">'
        "claude.ai/code</a> in any browser, or the Claude app on iOS or Android, signed in to "
        "the same Claude account. Your slot appears there under its name, the one at the "
        "top of your slot card: slot-4821 until you rename it. Start a session there. "
        "Everything runs on the machine, in your slot, with your files.</p></li>"
        "</ol>"
        '<div class="card"><h2>Also on your page</h2><ul>'
        "<li><strong>Your usage.</strong> Your Claude account&#x27;s 5-hour and weekly limits, "
        "which count everything the account does: claude.ai, the Claude app, and Claude "
        "Code on any computer, device tokens included. Beside them, the tokens Claude Code "
        "used on this slot itself over the last week; work on your own computer is not in "
        "that number. The limits are read every five minutes; "
        '<span class="btnlabel">Refresh</span> reads them now.</li>'
        '<li><strong>Device tokens.</strong> <span class="btnlabel">Get a device token</span> '
        "makes a one-year token from your own Claude account, for running Claude Code on "
        "your own computer. It is shown until you press "
        '<span class="btnlabel">Done with it</span>, for at most '
        f"{_span(LOGIN_MAX_AGE_S)}, and never kept after that. Your computer keeps its "
        "own Claude login too, with your own subscription: <code>ccfleet-connect "
        "--off</code> switches to it, and <code>ccfleet-connect --on</code> back to your "
        "slot&#x27;s account. Connecting again with another token replaces the one it "
        "had. A copy of <code>ccfleet-connect</code> installed before 25 September 2026 "
        "answers <code>--off</code> with <em>unknown option</em>; replace it with the "
        "current one, and the token stays as it is:"
        "<pre>curl -fsSL -o ~/.local/bin/ccfleet-connect \\\n  https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/ccfleet-connect.sh</pre></li>"
        '<li><strong>Giving it back.</strong> Tick the box and press '
        '<span class="btnlabel">Give this slot back</span>. Your Linux account and every file '
        "in it are deleted; your Claude account is not touched. Push your work somewhere "
        "first.</li></ul></div>"
        '<div class="card"><h2>One Claude account per slot</h2>'
        "<p>A slot is signed in to one Claude account, your own, and keeps it: "
        '<span class="btnlabel">Sign in again</span> works with that account only. To move '
        "your slot to another Claude account of yours, press "
        '<span class="btnlabel">Change account</span> (once a week). To use two accounts at '
        "once, hold two slots. In claude.ai/code each account sees only its own "
        "machine.</p>"
        "<p>One account also stays on one machine: signed in on two at once, it is flagged "
        "to you and to the operator.</p>"
        "<p>Your slot is a whole machine with a name of its own: a neutral one like "
        "slot-4821 when you claim it, never anything from your address, and whatever you "
        "rename it to on your page. claude.ai/code shows it by that name, so Anthropic sees "
        "it too: pick anything but your email address. When you give it back, the name goes "
        "with it.</p></div>"
        '<div class="card"><h2>Changing to another Claude account</h2>'
        '<p>On a slot in use, <span class="btnlabel">Change account</span> moves it to '
        "another Claude account of yours. Open the link it shows, sign in with the account "
        "the slot should use from now on, and paste the code as you did the first time. "
        "Your files and settings stay; only the Claude sign-in changes.</p>"
        "<p>Until that sign-in finishes, the slot keeps its current account; if it does not "
        "go through, or you sign in with the account the slot already has, the account does "
        "not change. Once it finishes, Remote Control restarts on the new account, which "
        "ends any session open in it, and the slot appears in claude.ai/code under the new "
        "account instead of the old one.</p>"
        "<p>A slot can change account once a week; after a change, your slot card says when "
        "it can change again. The operator sees that the account changed, never which "
        "account it is.</p></div>"
        '<div class="card"><h2>Keeping Claude Code up to date</h2>'
        "<p>Your slot card has a <strong>Claude Code</strong> row: the version your slot "
        "runs and, when Anthropic has published a newer one, its number. The button beside "
        "it, <strong>Update to</strong> and that number, installs it now. Your slot then "
        "follows Anthropic&#x27;s latest release and keeps itself current from then on.</p>"
        "<p>A session that is open keeps running on the version it started with; new "
        "sessions start on the new one. Remote Control switches over by itself once no "
        "session is open.</p>"
        '<p>To go back to Anthropic&#x27;s stable release, press <span class="btnlabel">'
        "Back to Stable</span>. A version <strong>held by the operator</strong> has been "
        "fixed on purpose, for example while a release misbehaves, and there is nothing to "
        "press.</p></div>"
        '<div class="card"><h2>Model and effort</h2>'
        "<p>Claude Code on your slot starts on <strong>Opus</strong> at <strong>max "
        "effort</strong>, so it thinks as hard as it can about everything you ask. That also "
        "uses your Claude plan&#x27;s limits fastest; the bars on your slot card show how "
        "fast.</p>"
        "<p>In a session, <code>/model</code> picks another model. The effort is set for the "
        "whole slot, so <code>/effort</code> cannot lower it. To change it, set the "
        "<code>CLAUDE_CODE_EFFORT_LEVEL</code> line in <code>~/.claude/settings.json</code> "
        "on your slot to low, medium, high or xhigh, or ask Claude to. Sessions you start "
        "after that use the new level.</p></div>"
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
    return _page("/docs/guide", "getting started", body, viewer=viewer)


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


def how_it_works(cfg: Config, viewer: Optional[Viewer] = None, *,
                 price: Optional[pricing.Price] = None) -> str:
    """Nothing here is about paying; `price` is taken so every page is called
    the same way."""
    body = (
        '<div class="dochead"><h1>How it works</h1>'
        '<p class="lead">Your work runs in your slot, on our machine. Your conversations go '
        "between you, Anthropic and your slot; ccfleet&#x27;s own server is not in that "
        "path.</p></div>"
        f'<div class="how"><figure class="card diagram">{PICTURE}</figure>'
        '<div class="how-text">'
        '<div class="card"><h2>Your slot</h2>'
        "<p>Your slot is a whole machine under the name you give it, and claude.ai/code shows it "
        "by that name. On it you have a Linux account of your own: a home directory only you can "
        "read, your own Claude Code, and two services that run while nobody is logged in: a work "
        "session, and Remote Control, which is what claude.ai/code and the Claude app connect to. "
        "There is no administrator access in a slot, and it is the only slot on its "
        "machine.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>You sign in to Claude yourself, through Anthropic&#x27;s own sign-in. The "
        "credential that creates is written on the machine, in your slot, and nowhere else: "
        "ccfleet&#x27;s server passes along the sign-in link and the code you paste, and "
        "never receives or keeps the credential. Claude Code in your slot talks to Anthropic "
        "directly. ccfleet does not relay, pool or rewrite anybody&#x27;s requests, and no "
        "Claude account is ever shared between people.</p></div>"
        '<div class="card"><h2>What we can and cannot see</h2>'
        "<p>ccfleet&#x27;s server receives facts about your slot: whether Claude Code is "
        "signed in, the email address and plan of the Claude account signed in on it, how "
        "much of your usage limits is used, and token counts per hour. Never your prompts, "
        "conversations, files or credential. "
        '<a href="/privacy">The privacy page</a> lists everything, and for how long.</p>'
        "<p>Claude Code on your slot sends Anthropic less than it would by default: its "
        "error reports, bug reports and feedback surveys are switched off. Its usage "
        "telemetry stays on, because Remote Control, which is how you reach your slot, "
        "does not work without it. The machines keep their clocks on UTC, so your slot "
        "says nothing about where you are; your page shows times in your own time "
        "zone.</p>"
        "<p>One limit is worth saying plainly: the machines are ours, and their "
        "administrators have root, so they can technically read any slot. No feature does "
        "this and we do not look, but nothing can make it impossible. Keep nothing in a slot "
        "that you could not accept an administrator being able to read.</p></div>"
        '<div class="card"><h2>Kept up to date</h2><ul>'
        "<li><strong>Claude Code</strong> in your slot is updated automatically to "
        "Anthropic&#x27;s latest release, or to its stable release if you choose that on "
        "your page. A "
        "session that is running is never interrupted; the new version is used from the "
        "next one.</li>"
        "<li><strong>Security updates</strong> for the machine install every day.</li>"
        "<li><strong>Reboots</strong>: when an update needs one, the machine says so and the "
        "operator reboots it at a quiet time. Your slot and its services come back by "
        "themselves.</li>"
        '<li><strong>Status</strong>: whether this site and the machines are up, now and over '
        'the last 90 days, is on <a href="/status">the status page</a>; your own page says '
        "how your slot&#x27;s machine is. Turn on <strong>outage emails</strong> there, and we "
        "email you when your slot&#x27;s machine has been down for five minutes, and again "
        "when it is back. An outage of this website is told afterwards, since the website "
        "is what sends them; your slot keeps working through one.</li></ul></div>"
        '<div class="card"><h2>Giving a slot back</h2>'
        "<p>When you give a slot back, the machine stops everything running in it and deletes "
        "your Linux account and every file in it. The slot is offered to anybody else only "
        "after the machine itself confirms your account is gone, so nobody is ever handed "
        "your files.</p></div>"
        '<div class="card"><h2>Where</h2>'
        "<p>The machines are in California. Each slot is a machine of its own, with its own "
        "internet address. ccfleet&#x27;s code is open source: "
        '<a href="https://github.com/cdcupt/ccfleet" target="_blank" '
        'rel="noopener noreferrer">github.com/cdcupt/ccfleet</a>.</p></div>'
        "</div></div>")
    return _page("/docs/how-it-works", "how it works", body, width="", viewer=viewer)


# -- terms ------------------------------------------------------------------------------

def terms(cfg: Config, viewer: Optional[Viewer] = None, *,
          price: Optional[pricing.Price] = None) -> str:
    paying = (f"A slot costs {_cost(price)}, paid directly to the operator, who switches your "
              "slot on when you pay; this site takes no card and no payment."
              if price is not None else
              "Price and payment are agreed directly with the operator, who switches your slot "
              "on when you pay.")
    body = (
        '<div class="dochead"><h1>Terms</h1>'
        f'<p class="sub">Last updated {escape(DOCS_UPDATED)}</p></div>'
        '<div class="card"><h2>The service</h2>'
        "<p>A slot is a Linux account on a machine the operator runs, for one person, with "
        "Claude Code installed. You bring your own Claude plan and sign in to it yourself; "
        "ccfleet does not provide access to Claude.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>Use your own Claude account in your slot, and only yours. You are responsible for "
        "following Anthropic&#x27;s terms and usage policy, as you would on your own "
        "computer. Do not share your slot, or your sign-in, with anybody else.</p></div>"
        '<div class="card"><h2>Paying</h2>'
        f"<p>{paying} If a payment lapses, the operator may take a slot back, and taking "
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
    return _page("/docs/terms", "terms", body, viewer=viewer)


PAGES: dict[str, Callable[..., str]] = {
    "/docs": overview, "/docs/guide": guide,
    "/docs/how-it-works": how_it_works, "/docs/terms": terms,
}


def page_for(path: str) -> Optional[Callable[..., str]]:
    """The page at this path, trailing slash or not; None when there is none."""
    return PAGES.get(path.rstrip("/") or "/")
