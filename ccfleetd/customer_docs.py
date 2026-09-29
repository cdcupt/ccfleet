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
from .render import _meter
from .usersite import NAV, Viewer, _shell

#: When these pages last changed in substance. Change it with the words.
DOCS_UPDATED = "2026-09-29"

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
        '<p class="rc on">CC Fleet CLI is ready: run <strong>ccfleet local</strong>.</p>'
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
        "<h1>Local Claude Code, your own account on your slot</h1>"
        '<p class="lead">ccfleet gives you a <strong>slot</strong>: your own Linux account on a '
        "machine we run, with Claude Code installed and signed in to <em>your own</em> Claude "
        "account. Run <code>ccfleet local</code> for original Claude Code, files, tools and "
        "history on your computer, with supported model requests relayed through that slot. "
        "Plain <code>ccfleet</code> keeps the remote terminal available for earlier work.</p>"
        + way_in + "</div>" + _demo() + "</section>"
        '<section class="band"><h2>What you get</h2>'
        '<p class="band-lead">For anybody with a Claude plan that includes Claude Code who '
        "wants a dedicated account-bound slot for model access.</p>"
        '<div class="features">'
        f'<div class="feature">{ICONS["account"]}<h3>Your own Linux account</h3>'
        "<p>A slot home isolated from other slot users, with room for your tools. "
        "Administrators retain root access.</p></div>"
        f'<div class="feature">{ICONS["machine"]}<h3>Your native local workflow</h3>'
        "<p>Original Claude Code, local tools and native history; no project upload or "
        "filesystem mount.</p></div>"
        f'<div class="feature">{ICONS["usage"]}<h3>One command anywhere</h3>'
        "<p>Run <code>ccfleet local</code> from your terminal. No manual SSH or proxy "
        "configuration, and no slot Claude credential copied to your computer.</p></div>"
        "</div></section>"
        '<section class="band two">'
        '<div class="card"><h2>What you need</h2><ul>'
        "<li>Your own paid Claude plan that includes Claude Code: <strong>Pro, Max, Team or "
        "Enterprise</strong>. You sign that account in to your slot once; API keys do not "
        "replace that subscription sign-in.</li>"
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

def _client_tools() -> str:
    """Concrete local controls, distinct from the unchanged remote commands."""
    return (
        '<div class="card" id="client-tools"><h2>Your everyday terminal controls</h2>'
        '<h3>Choose a session and save defaults</h3>'
        '<pre><code>ccfleet start\nccfleet sessions\n'
        'ccfleet preferences set --model opus --effort high --mode plan\n'
        'ccfleet preferences show\nccfleet preferences clear</code></pre>'
        '<p><code>ccfleet start</code> (also <code>ccfleet menu</code>) offers local new, '
        'continue and resume choices. <code>ccfleet sessions</code> opens the original '
        'Claude history picker; CC Fleet does not import or synchronize conversations. '
        'Preferences apply to new local sessions in this project, or use '
        '<code>--project PATH</code>. Explicit launch choices win; resuming keeps the '
        'saved model and effort unless changed deliberately. Clearing preferences does '
        'not clear history. Bare <code>ccfleet</code> still means the remote terminal; '
        '<code>ccfleet remote</code> selects it explicitly.</p>'
        '<h3>Check readiness and get local help</h3>'
        '<pre><code>ccfleet status --json\n'
        'ccfleet doctor --privacy --json --export ./ccfleet-support.json</code></pre>'
        '<p>These checks make no model request and do not scan a project. Doctor separates '
        'installation, pairing, broker/relay and account-health problems. The support '
        'report is written only to the new local file you request; it is not uploaded and '
        'does not include prompts, file contents, local paths, tokens or account email. '
        'Review it before sharing it yourself. A reported-ready heartbeat is an '
        'observation, not proof that Anthropic will accept the next request. Renewal '
        'pending, sign-in required, account maintenance and stale observations remain '
        'distinct; keep your pairing and use the existing sign-in controls when needed.</p>'
        '<h3>Update or return to the previous client</h3>'
        '<pre><code>ccfleet version --json\nccfleet update\nccfleet update --rollback</code></pre>'
        '<p>The updater verifies a signed release channel against the client&#x27;s pinned '
        'Ed25519 key, then verifies an immutable manifest and every helper before atomic '
        'activation. A failed verification or download leaves the current client in place. '
        'Rollback selects the preserved previous client without changing pairing, local '
        'files or native history. A restored legacy bootstrap is not labelled signature-verified. '
        'The version report distinguishes bootstrap and verified releases; an absent or '
        'expired signed channel is an error, not permission to install unsigned code. '
        'These commands update CC Fleet, not Anthropic&#x27;s original Claude executable.</p></div>'
        '<div class="card" id="background-jobs"><h2>Managed local background jobs</h2>'
        '<pre><code>ccfleet jobs start --prompt "Review this project and summarize findings"\n'
        'ccfleet jobs list\nccfleet jobs status JOB_ID\nccfleet jobs logs JOB_ID\n'
        'ccfleet jobs logs JOB_ID --stream stderr\nccfleet jobs stop JOB_ID\n'
        'ccfleet jobs archive JOB_ID</code></pre>'
        '<p>Copy the job ID returned by start. An equivalent launch form is '
        '<code class="code-wrap">ccfleet local --background --print '
        '"Summarize this project"</code>. '
        'This runs local Claude print-mode work under a supervisor and keeps its local '
        'bridge alive after the starting terminal closes. Your computer must stay running '
        'and connected. It is not a remote job, native detached <code>--bg</code>, or an '
        'interactive session you can attach to. For saved context choose an explicit '
        '<code>--resume NAME_OR_ID</code> or <code>--continue</code>, not a history picker.</p>'
        '<p>The default timeout is one hour; <code>--timeout SECONDS</code> allows at most '
        '24 hours. The default concurrency limit is four, with <code>--max-jobs</code> '
        'allowing at most 16. Prompts, job specifications and output stay in private local '
        'files; each stdout/stderr log is capped at 4 MiB and can contain sensitive content. '
        'Logs are shown only when requested, not included in support exports. Jobs never '
        'automatically restart or replay failed inference.</p>'
        '<p>Stop, timeout, device revocation or supervisor loss ends the owned process '
        'group and bridge. Authorization is checked periodically, not instantaneously. '
        'A sign-in/account transition or changed computer pairing stops old jobs; '
        'they do not silently continue under a different account. '
        'Shutdown is reported as confirmed only after cleanup is '
        'verified; an interrupted or unresponsive record is not proof it stopped. Tools '
        'that deliberately detach into another process group are not sandbox-contained. '
        'Native permissions still govern local data, including bypass mode. Job records '
        'are retained, not silently deleted. Archive an inactive, confirmed record with '
        '<code>ccfleet jobs archive JOB_ID</code> to keep its private files while freeing '
        'record capacity. An unconfirmed record requires '
        '<code>--acknowledge-unconfirmed</code>; archiving never claims its cleanup succeeded '
        'and never stops an active job.</p></div>'
    )


def guide(cfg: Config, viewer: Optional[Viewer] = None, *,
          price: Optional[pricing.Price] = None) -> str:
    paying = (f"and pay them: {_cost(price)}, paid directly to them; this site takes no "
              "card. They switch" if price is not None else "and pay them. They switch")
    body = (
        '<div class="dochead"><h1>Getting started</h1>'
        '<p class="lead">From buying a slot to your first Claude Code session. It takes a few '
        "minutes, most of which is the machine setting your slot up.</p>"
        '<p>Already using CC Fleet? Start with the <a href="#migration">migration guide</a>. '
        'For the native local workflow, see '
        '<a href="#project-workspaces">local files and history</a>.</p></div>'
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
        "<li><h3>Connect this computer</h3>"
        '<p>Run the <a href="#migration">one-command setup below</a>. It reuses an existing '
        'pairing. Only if it asks for a code, press <span class="btnlabel">Connect this '
        'computer</span> on your slot and paste the fresh single-use pairing code.</p></li>'
        "</ol>"
        '<div class="card" id="migration"><h2>One-command setup and migration</h2>'
        '<p>New computer, already paired, or still using <code>ccfleet-connect</code>: '
        'use the same command. You do not need to choose a migration mode.</p>'
        '<ol><li><strong>Finish old work first.</strong> An old live-folder connector may '
        'still be running after its terminal closes. Finish that work before approving '
        'migration; do not assume a closed terminal stopped folder access.</li>'
        '<li><strong>Run setup:</strong></li></ol>'
        '<pre><code>curl -fsSL https://raw.githubusercontent.com/cdcupt/'
        'ccfleet/main/laptop/install.sh | bash -s -- --setup</code></pre>'
        '<ol start="3"><li><strong>Follow the pairing prompt.</strong> An already-paired '
        'computer reuses its existing pairing; do not pair again. Only if this computer '
        'is not paired, open <a href="/account">your slot page</a>, choose '
        '<span class="btnlabel">Connect this computer</span>, and paste one fresh pairing '
        'code when asked. No SSH command or slot Claude credential is needed.</li>'
        '<li><strong>Approve old access cleanup only when ready.</strong> Setup asks in '
        'the controlling terminal before stopping old live-folder connectors and their '
        'associated remote live-folder sessions. This cancels pending work and invalidates '
        'open mount handles. Files and history are kept; ordinary remote tmux sessions '
        'are untouched. If you decline or cleanup fails, follow the reported instructions '
        'and retry. Adding <code>--yes</code> explicitly authorizes this cancellation.</li>'
        '<li><strong>Wait for readiness.</strong> Existing native Claude is preserved; '
        'if missing, setup installs the original local CLI from the fixed vendor URL. '
        'Setup waits for a new device key to reach the slot and checks access without '
        'uploading project files or making a model request. Old '
        '<code>ccfleet-connect</code> cleanup runs only after readiness succeeds.</li>'
        '<li><strong>Open a new terminal, then choose your project:</strong></li></ol>'
        '<pre><code>cd ~/code/my-project\nccfleet start</code></pre>'
        '<p>Original Claude Code now runs on your computer with local files, tools, '
        'settings and history. Supported model requests use your assigned slot. Setup '
        'itself starts no Claude conversation. Choose new, continue or resume from the '
        'menu, or run <code>ccfleet local</code> directly.</p>'
        '<p>The installer verifies its digest-pinned helper. Pairing, configuration, '
        'slot sign-in and existing remote files stay in place. PATH changes preserve '
        'existing shell settings and keep a backup. Existing terminals keep their old '
        'environment; custom token exports outside the managed setup are not removed.</p>'
        '<p>Optional: add <code>--name "Personal Mac"</code> after <code>--setup</code> to '
        'label a new device, or <code>--slot SLOT</code> to choose an existing pairing. '
        'Install-only, manual <code>ccfleet login</code>, and legacy <code>--migrate</code> '
        'remain compatibility options. Setup does not automatically revoke an Anthropic '
        'credential. Revoke an old setup-token yourself only if nothing else uses it.</p>'
        '<p><strong>Availability:</strong> inference relay access requires an upgraded, '
        'operator-enabled assigned slot. Installing the client or running the check '
        'does not activate access. <code>ccfleet local --check</code> sends no project '
        'files and makes no model request. A working old terminal or mount does not '
        'prove the relay is enabled; follow any readiness error before starting work.</p></div>'
        '<div class="card" id="project-workspaces"><h2>Local Claude, local files and history</h2>'
        '<p><code>ccfleet local</code> launches the original local Claude CLI. It does not '
        'upload a project, mount your filesystem on the slot, or require push/pull. There '
        'are no CC Fleet filesystem count/size caps or Git-ignore filters. Use '
        '<code>--project PATH</code> or <code>cd</code>; home works too:</p>'
        '<pre><code>cd ~\nccfleet local</code></pre>'
        '<p>The working directory is not a sandbox. Native Claude permissions govern '
        'local file and tool access, including outside that directory. New conversations '
        'default to <code>bypassPermissions</code>, Opus and max effort, so local tools can '
        'modify, delete or transmit data without individual approval prompts. Choose '
        'another mode when appropriate:</p>'
        '<pre><code>ccfleet local --new --name work\n'
        'ccfleet local --new --name research --mode plan --model opus --effort high\n'
        'ccfleet local --resume\nccfleet local --resume work\nccfleet local --continue\n'
        'ccfleet local --resume work --fork-session\n'
        'ccfleet local --print "Summarize this project"</code></pre>'
        '<p>These are native local conversations, not remote tmux sessions. '
        'Running <code>ccfleet local</code> again starts a new conversation; '
        '<code>--new</code> makes that choice explicit. Existing history is kept. '
        '<code>--resume</code> uses Claude&#x27;s native history picker or an ID/name; '
        '<code>--continue</code> selects the last local conversation for this directory. '
        'Use <code>/model</code> and <code>/effort</code> inside Claude. Resuming preserves '
        'the saved model and effort unless you request changes. Additional supported '
        'native arguments go after <code>--</code>; routing/authentication overrides '
        'are not alternate CC Fleet modes.</p>'
        '<p>Foreground interactive sessions, <code>--print</code>, native resume and '
        'multiple normal terminal sessions are supported. Native <code>--bg</code> / '
        '<code>--background</code> is explicitly rejected when forwarded after '
        '<code>--</code>: it would bypass supervision. Use the '
        '<a href="#background-jobs">managed local background jobs</a> below, or another '
        'regular terminal for parallel interactive work.</p>'
        '<h3>Keep your native settings and history</h3>'
        '<p>The default uses native Claude settings/history, including an existing '
        '<code>CLAUDE_CONFIG_DIR</code>. Nothing is imported or deleted. To reopen an '
        'earlier per-slot relay-preview profile, use '
        '<code class="code-wrap">ccfleet local --legacy-history --resume</code>. This selects '
        'that old local profile; it does not merge it into native history. Old remote '
        'history remains on the slot.</p>'
        '<p>A temporary private settings overlay pins the loopback model route and nonce, '
        'clears conflicting provider/authentication overrides, and disables supported '
        'optional telemetry. Ordinary customizations remain; native settings files '
        'are not permanently rewritten.</p>'
        '<h3>Stopping and recovering older work</h3>'
        '<p>Quit a new local session normally with <code>/exit</code>, then use native '
        'resume later. A closed terminal does not create a persistent remote version of '
        'this local agent. Interrupted inference is not silently replayed by CC Fleet.</p>'
        '<p><code>ccfleet local --disconnect</code> is only for explicitly cleaning up an '
        'old live-folder grant and its associated remote sessions. It can cancel pending '
        'work. <code>--reset-link</code> is retired and provides migration guidance. '
        'Old <code>ccfleet project</code> commands remain for deliberate snapshot recovery, '
        'not for the current local workflow.</p>'
        '<h3>Routing and privacy limits</h3>'
        '<p>The relay removes selected headers and the top-level structured '
        '<code>metadata</code> field before forwarding. It does not redact arbitrary '
        'prompts or tool results. Native system prompts may include local OS, working '
        'directory and environment details; files and paths may identify you and reach '
        'the slot and Anthropic. This is not a fingerprint-free or zero-metadata guarantee.</p>'
        '<p>Only supported model endpoints use the relay. MCP servers, hooks, plugins, '
        'shell tools, updates and other native CLI services can connect directly from '
        'the laptop. Optional telemetry controls are not an all-traffic firewall.</p>'
        '<p>BWH sees your incoming IP address and connection metadata but cannot decrypt '
        'the inner SSH model stream. SSH exposes client version; remote-terminal sessions '
        'also send terminal dimensions. '
        'Slot administrators have root and can inspect or alter relayed requests and '
        'responses, which can influence local tool actions. Trust and native local '
        'permissions remain important. Each paired device remains separately revocable.</p></div>'
        + _client_tools()
        + '<div class="card"><h2>Remote-terminal compatibility</h2>'
        '<p>The commands below run on the slot, not in a local Claude conversation.</p><ul>'
        "<li><strong>The command.</strong> Run <code>ccfleet</code> in a normal terminal. It "
        "opens the original Claude Code interface running in your slot. You never type an "
        "SSH command and your computer receives no Claude credential.</li>"
        "<li><strong>Where work runs.</strong> Claude Code, its shell tools and model requests "
        "run on the slot for this compatibility command. Files live in its remote workspace; "
        "use <code>ccfleet local</code> for local Claude and local files.</li>"
        "<li><strong>Permission mode.</strong> Hosted sessions start with permission prompts "
        "off. Claude can run tools without asking as your slot user. The slot has no sudo, "
        "but Claude can read, change, delete or send anything that user can access. Start a "
        "named session with another mode using <code>ccfleet new research --mode plan</code>; "
        "later use <code>ccfleet attach --session research</code>. To change a running "
        "session deliberately, use <code class=\"code-wrap\">"
        "ccfleet restart --session research --mode "
        "auto</code>.</li>"
        "<li><strong>Model and effort.</strong> The default is Opus at max effort. For a "
        "new named session, choose both at launch: <code class=\"code-wrap\">"
        "ccfleet new research --model "
        "fable --effort xhigh</code>. In a running Claude Code session, use "
        "<code>/model</code> or <code>/effort</code> to change it without losing the "
        "conversation.</li>"
        "<li><strong>Projects.</strong> Keep projects in <code>~/workspace</code> on the slot. "
        "Clone with Git or fetch them from another service from inside Claude Code. CC Fleet "
        "does not silently upload or mount files from the computer where you run the client. "
        'For local Claude instead, see '
        '<a href="#project-workspaces">local files and history</a>.</li>'
        "<li><strong>Reconnect.</strong> Claude Code runs inside a persistent session. If "
        "Wi-Fi changes, the laptop sleeps or the terminal closes, run <code>ccfleet</code> "
        "again and it reattaches to the same session.</li>"
        "<li><strong>More computers.</strong> Press <span class=\"btnlabel\">Connect another "
        "computer</span> for each device. Each receives its own key and can be removed from "
        "your slot page without changing your Claude sign-in.</li>"
        "<li><strong>Your usage.</strong> The 5-hour and weekly bars cover the whole Claude "
        "account. Beside them, the token count covers what Claude Code used on this slot. "
        "That slot token count is not a complete local-relay usage meter. "
        "The limits are read every five minutes; <span class=\"btnlabel\">Refresh</span> "
        "reads them now.</li>"
        '<li><strong>Giving it back.</strong> Tick the box and press '
        '<span class="btnlabel">Give this slot back</span>. Your Linux account and every file '
        "in it are deleted; your Claude account is not touched. Push your work somewhere "
        "first.</li></ul></div>"
        '<div class="card"><h2>One Claude account per slot</h2>'
        "<p>A slot is signed in to one Claude account, your own, and keeps it: "
        '<span class="btnlabel">Sign in again</span> works with that account only. To move '
        "your slot to another Claude account of yours, press "
        '<span class="btnlabel">Change account</span> (once a week). To use two accounts at '
        "once, hold two slots. Each local CC Fleet profile is bound to exactly one slot.</p>"
        "<p>One account also stays on one machine: signed in on two at once, it is flagged "
        "to you and to the operator.</p>"
        "<p>Your slot is a Linux account with a name of its own: a neutral one like "
        "slot-4821 when you claim it, never anything from your address, and whatever you "
        "rename it to on your page. CC Fleet shows that name on every paired computer, and "
        "the slot uses it as its hostname, so Anthropic can see it: pick anything but your "
        "email address. When you give it back, the name goes with it.</p></div>"
        '<div class="card"><h2>Changing to another Claude account</h2>'
        '<p>On a slot in use, <span class="btnlabel">Change account</span> moves it to '
        "another Claude account of yours. Open the link it shows, sign in with the account "
        "the slot should use from now on, and paste the code as you did the first time. "
        "Your files and settings stay; only the Claude sign-in changes.</p>"
        "<p>Until that sign-in finishes, the slot keeps its current account; if it does not "
        "go through, or you sign in with the account the slot already has, the account does "
        "not change. Once it finishes, the slot&#x27;s Claude sessions end so they no longer "
        "keep the old account; plain <code>ccfleet</code> opens a new remote session. "
        "Connected computers stay paired because their CC Fleet keys are separate from "
        "the Claude credential.</p>"
        '<p>For <code>ccfleet local</code>, CC Fleet does not automatically close your '
        'local Claude process or delete its history. The relay checks the slot&#x27;s '
        'account for every request and cancels an in-flight request if that account '
        'changes. Wait until the slot is <span class="pill ok">In use</span> again '
        'before retrying deliberately; new requests then use its current bound account. '
        'CC Fleet does not silently replay interrupted requests.</p>'
        "<p>A slot can change account once a week; after a change, your slot card says when "
        "it can change again. An account change is visible to the operator; "
        '<a href="/privacy">the privacy page</a> explains which account information '
        'is reported to CC Fleet.</p></div>'
        '<div class="card"><h2>Keeping Claude Code up to date</h2>'
        "<p>Your slot card has a <strong>Claude Code</strong> row: the version your slot "
        "runs and, when Anthropic has published a newer one, its number. The button beside "
        "it, <strong>Update to</strong> and that number, installs it now. Your slot then "
        "follows Anthropic&#x27;s latest release and keeps itself current from then on.</p>"
        "<p>A session that is open keeps running on the version it started with; new "
        "sessions start on the new one.</p>"
        '<p>To go back to Anthropic&#x27;s stable release, press <span class="btnlabel">'
        "Back to Stable</span>. A version <strong>held by the operator</strong> has been "
        "fixed on purpose, for example while a release misbehaves, and there is nothing to "
        "press.</p></div>"
        '<div class="card"><h2>Model and effort</h2>'
        "<p>Claude Code on your slot starts on <strong>Opus</strong> at <strong>max "
        "effort</strong>, so it thinks as hard as it can about everything you ask. That also "
        "uses your Claude plan&#x27;s limits fastest; the bars on your slot card show how "
        "fast.</p>"
        "<p>Inside a running session, <code>/model</code> changes models and "
        "<code>/effort</code> changes effort without ending the conversation. At launch, "
        "use <code class=\"code-wrap\">ccfleet new NAME --model fable --effort "
        "xhigh</code>. Model choices "
        "include Opus, Fable and Sonnet when your Claude account offers them. Effort "
        "choices are low, medium, high, xhigh (extreme high), max and ultracode. "
        "Ultracode combines xhigh with Claude Code workflow orchestration; it is not an "
        "\"extreme max\" level.</p>"
        "<p><code>ccfleet attach --session NAME</code> resumes that named session. Use "
        "<code class=\"code-wrap\">ccfleet restart --session NAME --model opus --effort "
        "max</code> only when "
        "you deliberately want to end its current Claude process and replace it.</p></div>"
        '<div class="card"><h2>Good to know</h2><ul>'
        "<li>Your slot is <strong>not backed up</strong>. Keep your work in git, or anywhere "
        "else that is yours.</li>"
        "<li>You can install tools in your home directory; there is no administrator access "
        "(sudo) in a slot.</li>"
        "<li>If Claude asks you to sign in again, your slot card offers "
        '<span class="btnlabel">Sign in again</span>.</li>'
        "<li><code>ccfleet</code> cannot connect? Check that the slot says "
        '<span class="pill ok">In use</span>, then rerun the setup command. If it still '
        'fails, contact your operator before removing a working pairing. '
        "A disconnected session continues on the slot.</li>"
        f"<li>Anything else: ask {contact(cfg)}.</li></ul></div>")
    return _page("/docs/guide", "getting started", body, viewer=viewer)


# -- how it works -----------------------------------------------------------------------

#: Portrait, so it stays legible at phone width. The broker is in the network
#: path, but the SSH session inside its WebSocket tunnel is encrypted through
#: to the slot.
PICTURE = """<svg class="diag" viewBox="0 0 360 520" role="img" aria-labelledby="diag-t diag-d">
<title id="diag-t">Where your work runs</title>
<desc id="diag-d">Original Claude Code runs locally. Supported model requests travel through
encrypted SSH and the CC Fleet broker to the assigned slot relay, then Anthropic.
Other native CLI services can connect directly from the laptop.</desc>
<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="head" d="M0,0 L10,5 L0,10 z"/></marker>
<marker id="af" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
orient="auto-start-reverse"><path class="headfaint" d="M0,0 L10,5 L0,10 z"/></marker></defs>
<rect class="box you" x="20" y="14" width="320" height="70" rx="12"/>
<text x="180" y="44" text-anchor="middle">Local original Claude Code</text>
<text class="sub" x="180" y="66" text-anchor="middle">local files, tools and history</text>
<path class="arrow" d="M180,88 L180,126" marker-start="url(#ah)" marker-end="url(#ah)"/>
<text class="sub" x="190" y="111">TLS + encrypted SSH</text>
<rect class="side" x="20" y="130" width="320" height="70" rx="12"/>
<text x="180" y="160" text-anchor="middle">CC Fleet broker</text>
<text class="sub" x="180" y="182" text-anchor="middle">authenticates device; relays bytes</text>
<path class="arrow" d="M180,204 L180,242" marker-start="url(#ah)" marker-end="url(#ah)"/>
<text class="sub" x="190" y="227">SSH to the slot</text>
<rect class="slot" x="20" y="246" width="320" height="114" rx="12"/>
<text x="180" y="276" text-anchor="middle">Your slot, on our machine</text>
<text class="sub" x="180" y="300" text-anchor="middle">fixed upstream model relay</text>
<text class="sub" x="180" y="322" text-anchor="middle">only your bound Claude account</text>
<text class="sub" x="180" y="344" text-anchor="middle">credential stays on the slot</text>
<path class="arrow" d="M180,364 L180,408" marker-start="url(#ah)" marker-end="url(#ah)"/>
<text class="sub" x="190" y="392">Claude traffic from the slot</text>
<rect class="box" x="20" y="412" width="320" height="82" rx="12"/>
<text x="180" y="444" text-anchor="middle">Anthropic</text>
<text class="sub" x="180" y="468" text-anchor="middle">relevant prompts and tool context</text>
</svg>"""


def how_it_works(cfg: Config, viewer: Optional[Viewer] = None, *,
                 price: Optional[pricing.Price] = None) -> str:
    """Nothing here is about paying; `price` is taken so every page is called
    the same way."""
    body = (
        '<div class="dochead"><h1>How it works</h1>'
        '<p class="lead"><code>ccfleet local</code> runs original Claude Code on your computer. '
        'Supported model requests travel through encrypted SSH to your assigned slot relay. '
        'Plain <code>ccfleet</code> remains a remote-terminal compatibility command.</p></div>'
        f'<div class="how"><figure class="card diagram">{PICTURE}</figure>'
        '<div class="how-text">'
        '<div class="card"><h2>Your slot</h2>'
        "<p>Your slot is a Linux account under the name you give it, with a private home "
        "directory and the original Claude Code. The plain <code>ccfleet</code> "
        "remote-terminal command uses a persistent session that keeps running when your "
        "computer disconnects; <code>ccfleet local</code> instead runs on your computer. "
        "There is no administrator access inside a slot. Other slots on the same machine use "
        "different operating-system accounts and Claude sign-ins.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>You sign in to Claude yourself, through Anthropic&#x27;s own sign-in. The "
        "credential that creates is written on the machine, in your slot, and nowhere else: "
        "ccfleet&#x27;s server passes along the sign-in link and the code you paste, and "
        "never receives or keeps the resulting credential. Native Claude on the slot owns "
        "authentication and renewal. The slot relay uses that bound credential upstream, "
        "not a credential sent to your laptop. No account rotation or pooling: one slot "
        "keeps one account for one holder.</p></div>"
        '<div class="card"><h2>Credential maintenance</h2>'
        '<p>Before access credentials expire, the machine attempts bounded renewal '
        'through native Claude in an isolated maintenance session. It checks that expiry '
        'actually advanced, preserves the bound account, and backs off on failure. '
        'The relay never writes or independently refreshes credentials. Your page reports '
        'unconfirmed renewal or stale health information instead of claiming readiness. '
        'Revocation or an expired sign-in can still require <strong>Sign in again</strong>; '
        'this is not a guarantee of permanent account access.</p></div>'
        '<div class="card"><h2>Reported health and local diagnostics</h2>'
        '<p>Your slot card distinguishes reported ready, renewal pending, sign-in required '
        'and account maintenance. Stale observations do not claim readiness. A heartbeat '
        'does not prove provider acceptance; <code>ccfleet doctor --privacy</code> and '
        '<code>ccfleet status --json</code> perform read-only connection checks without a '
        'model request or project scan. Support export is an explicit local-file operation, '
        'not an upload of your project or conversation.</p></div>'
        '<div class="card"><h2>Use it from anywhere</h2>'
        "<p>Install <code>ccfleet</code> on each computer you use. It creates a device key, "
        "pairs once with your held slot, and starts the local workflow without an SSH command. "
        "The broker accepts an authenticated WebSocket and passes the already encrypted SSH "
        "stream to that slot. It cannot read model request contents. A forced entrypoint "
        "permits only the fixed relay and compatibility protocols, with forwarding disabled. "
        "The model destination is not caller-selected.</p></div>"
        '<div class="card"><h2>Native local files and history</h2>'
        '<p>Claude, its tools, settings and conversations run locally. There is no folder '
        'upload, mount or push/pull step, and no CC Fleet filesystem cap. Native permissions '
        'apply; bypass mode can read, modify, delete or transmit local data without prompts. '
        '<a href="/docs/guide#migration">The migration guide</a> explains explicit cleanup '
        'of retired live-folder grants and the optional <code>--legacy-history</code> '
        'profile.</p></div>'
        '<div class="card"><h2>What we can and cannot see</h2>'
        "<p>ccfleet&#x27;s server receives facts about your slot: whether Claude Code is "
        "signed in, the email address and plan of the Claude account signed in on it, how "
        "much of your usage limits is used, token counts per hour, and which CC Fleet devices "
        "are paired. It does not receive your prompts, conversations, files or Claude "
        "credential. "
        '<a href="/privacy">The privacy page</a> lists everything, and for how long.</p>'
        "<p>Claude Code on your slot sends Anthropic less than it would by default: its "
        "error reports, bug reports and feedback surveys are switched off. The machines "
        "keep their clocks on UTC; your page shows times in your own time zone. That does "
        "not make the service anonymous: BWH sees your connection IP, SSH has transport "
        "metadata, and native prompts, tool results and file contents can identify you. "
        "Selected headers and top-level structured metadata are removed, but native system "
        "prompts can still include local OS, working directory or environment details. "
        "MCP servers, hooks, plugins, tools, updates and other native services can connect "
        "directly from the laptop. Optional telemetry controls are not an all-traffic firewall.</p>"
        "<p>One limit is worth saying plainly: the machines are ours, and their "
        "administrators have root, so they can technically read any slot. No feature does "
        "this and we do not look, but nothing can make it impossible. Keep nothing in a slot "
        "that you could not accept an administrator being able to read.</p></div>"
        '<div class="card"><h2>Kept up to date</h2><ul>'
        '<li><strong>Local Claude Code</strong>: CC Fleet setup preserves an existing '
        'local installation and installs it if missing. Automatic updates are disabled '
        'during a CC Fleet launch; update your local Claude installation separately, '
        'outside that session.</li>'
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
        "is what sends them. Existing slot processes may keep running, but local model "
        "requests require the broker to be reachable; a broker outage interrupts that "
        "path.</li></ul></div>"
        '<div class="card"><h2>Giving a slot back</h2>'
        "<p>When you give a slot back, the machine stops everything running in it and deletes "
        "your Linux account and every file in it. The slot is offered to anybody else only "
        "after the machine itself confirms your account is gone, so nobody is ever handed "
        "your files.</p></div>"
        '<div class="card"><h2>Where</h2>'
        "<p>The hosted machines are in California. A slot is a separate Linux account; "
        "slots may share a physical machine and its internet address. "
        "ccfleet&#x27;s code is open source: "
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
        "ccfleet does not provide access to Claude. CC Fleet is independent and is not "
        "affiliated with or endorsed by Anthropic.</p></div>"
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
