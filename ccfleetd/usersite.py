"""The user site: one person, the slots they hold, and what they can do with them.

Everything here is scoped to the signed-in account, and the scoping is the
point. A slot that is not theirs answers exactly as a slot that does not exist
would — they never see other people, other slots or anybody's alerts — and
every action checks the slot's holder again rather than trusting the page it
came from.

Signing in to *us* is Google's (see oauth.py and sessions.py). Signing in to
*Claude* happens on the slot, through Claude Code's own login: this page
carries a URL out and a code back, and the credential is written on the
machine, in that slot's home. Nothing here ever holds it.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any, Optional

from . import claude_versions, names, oauth, payments, plans, resets, status
from . import slots as slotstates
from .config import Config
from .desired import is_login_url
from .monitor import LOGIN_MAX_AGE_S
from .render import (
    CONSOLE_PATH,
    CSS,
    FAVICON,
    LOCAL_TIMES_TAG,
    LOGIN_WORDS,
    MARK,
    SIGN_IN_LINK,
    TOKEN_WORDS,
    _age,
    _human_tokens,
    _meter,
    _usage_chart,
    _usage_span,
    console_href,
    product_href,
    quota_reading,
    user_menu,
)
from .store import (
    BadName,
    NameTaken,
    NoSlotAvailable,
    NotYours,
    QuotaExceeded,
    Store,
    StoreError,
)

# What a page may say after an action. Chosen by a fixed code, never text taken
# from the request: a message a link could write is a message a stranger could
# put on this page, on our domain, in front of somebody signed in.
NOTES = {
    "claimed": ("ok", "Your slot is being set up: an account on the machine, then Claude "
                      "Code. It takes a few minutes, and this page keeps up."),
    "no-slot": ("warn", "No slot is free right now. Try again later, or ask your operator."),
    "no-allowance": ("warn", "You already hold every slot your allowance covers."),
    "released": ("ok", "Given back. Everything on it is being deleted."),
    "confirm": ("warn", "Tick the box first: giving a slot back deletes everything on it."),
    "signin": ("ok", "Starting the sign-in on your slot…"),
    "switch": ("ok", "Starting the change of account on your slot…"),
    "token": ("ok", "Starting a device token on your slot…"),
    "code": ("ok", "Code sent to your slot."),
    "cancelled": ("ok", "Cancelled."),
    "done": ("ok", "Done. The token is no longer kept here."),
    "not-now": ("warn", "Your slot cannot do that right now. It may still be setting up, "
                        "or being given back."),
    "updating": ("ok", "Updating Claude Code on your slot. Sessions already open keep "
                       "running; new ones start on the new version."),
    "stable": ("ok", "Back to the stable release. Your slot moves there at its next quiet "
                     "moment."),
    "renamed": ("ok", "Renamed. Your machine answers to the new name within a minute or two, "
                      "and Remote Control restarts under it, which ends a session open in it."),
    "name-bad": ("warn", "A name is 2 to 30 letters, digits and inner hyphens, and not "
                         "pool- or slot- followed by a number."),
    "name-taken": ("warn", "That name is taken. Pick another."),
    "reading": ("ok", "Reading your usage now. It shows here within a minute or two."),
    "emails-on": ("ok", "Outage emails on: we email you when your slot's machine has been "
                        "down for five minutes, and again when it is back."),
    "emails-off": ("ok", "Outage emails off."),
}

# The pill says the state the way every page says a state: green running,
# amber waiting on you, the accent while the machine is working on it, grey on
# its way out.
STATE_WORDS = {
    slotstates.CLAIMING: ("busy", "Setting up",
                          "Creating your account on the machine and installing Claude Code. "
                          "A few minutes."),
    slotstates.CLAIMED: ("warn", "Ready to sign in",
                         "Sign in to your own Claude account below to start using it."),
    slotstates.ACTIVE: ("ok", "In use", ""),
    slotstates.RELEASING: ("disabled", "Being wiped",
                           "Everything on it is being deleted. It stops counting against your "
                           "allowance once the machine confirms it is gone."),
}

#: What the two windows count, said wherever they are shown beside a slot's own tokens.
ACCOUNT_WIDE = ("These count everything this Claude account does: claude.ai, the Claude "
                "app, and Claude Code on any computer, device tokens included.")

SLOT_ACTIONS = ("release", "signin", "switch", "code", "cancel", "token", "token-show",
                "token-done", "update", "stable", "rename", "quota")
# What a slot can do, by state. Sign-in and tokens need the account to exist
# on the machine and the slot not to be on its way out.
CAN_SIGN_IN = (slotstates.CLAIMED, slotstates.ACTIVE)
IDLE_REFRESH_S = 60
ACTIVE_REFRESH_S = 4


@dataclass(frozen=True)
class Viewer:
    """Somebody signed in to us, as the corner of every page shows them.

    Built only from a session this request carried, so the page shows the
    person looking at it and nobody else. The token is that session's, which
    is all the menu's sign-out form needs.
    """

    account: Mapping[str, Any]
    csrf: str
    slots_href: str = "/account"
    console_href: str = CONSOLE_PATH
    slots_held: int = 0

    @property
    def operator(self) -> bool:
        """Whether the menu offers the console. Operators are made only from
        the server's command line; the console checks the role again itself."""
        return self.account.get("role") == "admin"

    def menu(self) -> str:
        return user_menu(self.account, self.csrf, operator=self.operator,
                         slots_href=self.slots_href, console_href=self.console_href,
                         slots_held=self.slots_held)


def viewer_for(account: Optional[Mapping[str, Any]], session_id: str, cfg: Config,
               store: Optional[Store] = None, *, on_console: bool = False
               ) -> Optional[Viewer]:
    """The viewer for a page, or None when nobody is signed in.

    The count on Your slots is read here, for this account and no other, so no
    page can show one person another's. On the console the menu's links point
    back at the product: with two hostnames the console's host has no slots
    page and the product's has no console, so each side names the other in full.
    """
    if account is None or not session_id:
        return None
    csrf = csrf_for(session_id, cfg.cookie_secret)
    held = store.held_slot_count(account["id"]) if store is not None else 0
    if on_console:
        return Viewer(account, csrf, slots_href=product_href(cfg, "/account"), slots_held=held)
    return Viewer(account, csrf, console_href=console_href(cfg), slots_held=held)


@dataclass(frozen=True)
class Outcome:
    """What the handler should send: a redirect when `location` is set."""

    status: int
    location: str = ""
    body: str = ""


def csrf_for(session_id: str, secret: str) -> str:
    """This session's form token.

    Derived from the session, so it is worth nothing to anybody else and dies
    with the session. The cookie is SameSite=Lax, which already keeps it off a
    cross-site POST; this is the second lock, for the browsers where the first
    does not hold.
    """
    return hmac.new(secret.encode("utf-8"), b"ccfleet-user-csrf:" + session_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _back(note: str, anchor: str = "") -> Outcome:
    return Outcome(303, location=f"/account?note={note}" + (f"#{anchor}" if anchor else ""))


def not_found(viewer: Optional[Viewer] = None) -> Outcome:
    return Outcome(404, body=_shell("Not found", f'<div class="card door">{MARK}'
                                    "<h1>Not found</h1>"
                                    "<p>There is nothing here.</p>"
                                    "<p><a class=\"back\" href=\"/account\">&larr; your slots</a>"
                                    "</p></div>", viewer=viewer))


# -- actions ---------------------------------------------------------------------

def act(store: Store, cfg: Config, account: Mapping[str, Any], path: str,
        form: Mapping[str, str], now: float, session_id: str = "") -> Outcome:
    """Do what a form on this page asked, for this account and nobody else."""
    viewer = viewer_for(account, session_id, cfg, store)
    parts = path.strip("/").split("/")
    if parts == ["account", "claim"]:
        return _claim(store, cfg, account, now)
    if parts == ["account", "outage-emails"] and cfg.emails_ready:
        on = form.get("on") == "1"
        store.set_outage_emails(account["id"], on)
        return _back("emails-on" if on else "emails-off", "outage-emails")
    if len(parts) == 4 and parts[:2] == ["account", "slots"] and parts[3] in SLOT_ACTIONS:
        slot = store.get_slot(parts[2])
        if slot is None:
            return not_found(viewer)
        try:
            return _on_slot(store, slot, account["id"], parts[3], form, now, viewer)
        except NotYours:
            # Somebody else's slot answers exactly as a missing one: which ids
            # are held, and by whom, is not this person's to learn. The store
            # decides it, in the same transaction as the action itself.
            return not_found(viewer)
    return not_found(viewer)


def _claim(store: Store, cfg: Config, account: Mapping[str, Any], now: float) -> Outcome:
    try:
        # Only a machine heard from recently: handing out a slot on one that
        # has gone quiet leaves somebody watching "setting up" for half an hour.
        slot = store.claim_slot(account["id"], now=now,
                                heard_since=now - cfg.heartbeat_max_age_s)
    except QuotaExceeded:
        return _back("no-allowance")
    except NoSlotAvailable:
        return _back("no-slot")
    return _back("claimed", f"slot-{slot['id']}")


def _on_slot(store: Store, slot: Mapping[str, Any], holder: str, action: str,
             form: Mapping[str, str], now: float,
             viewer: Optional[Viewer] = None) -> Outcome:
    """Every store call carries the holder, and the store checks it in the
    same transaction as the change: a slot given back and claimed by somebody
    else between loading and acting is refused, not acted on."""
    slot_id, anchor = slot["id"], f"slot-{slot['id']}"
    try:
        if action == "release":
            # The box, not merely the button: this deletes somebody's work, and
            # a form resubmitted from history must not do it by accident. Said
            # only to the slot's own holder — to anybody else this slot answers
            # as a missing one, whatever shape of request they send.
            if form.get("confirm") != "wipe":
                if slot.get("held_by") != holder:
                    raise NotYours(f"{slot_id} is not held by this account")
                return _back("confirm", anchor)
            store.begin_release(slot_id, held_by=holder)
            return _back("released", anchor)
        if action == "signin":
            store.request_slot_login(slot_id, form.get("email", ""), now, held_by=holder)
            return _back("signin", anchor)
        if action == "rename":
            # Lowercase, as a hostname is; the store checks the rest, the
            # holder included, in one transaction.
            try:
                store.name_slot(slot_id, (form.get("name") or "").strip().lower(),
                                held_by=holder)
            except BadName:
                return _back("name-bad", anchor)
            except NameTaken:
                return _back("name-taken", anchor)
            return _back("renamed", anchor)
        if action == "quota":
            # Once a minute at most, and only a slot in use: the store checks
            # both, and the holder, in one transaction.
            store.request_quota_read(slot_id, now, held_by=holder)
            return _back("reading", anchor)
        if action == "switch":
            # The store holds it to a slot in use and to once a week, in the
            # same transaction as the holder check.
            store.request_slot_login(slot_id, "", now, kind="switch", held_by=holder)
            return _back("switch", anchor)
        if action == "token":
            store.request_slot_login(slot_id, "", now, kind="token", held_by=holder)
            return _back("token", anchor)
        if action == "code":
            store.submit_slot_login_code(slot_id, form.get("code", ""), now, held_by=holder)
            return _back("code", anchor)
        if action == "token-show":
            return Outcome(200, body=token_page(
                slot, store.read_slot_secret(slot_id, now, held_by=holder), viewer))
        if action == "update":
            # The number it is going to, as the page showed it, so the row can
            # say "Updating to 2.1.281…" while the machine works.
            latest = claude_versions.channel_version(store.get_channel_versions(), "latest")
            store.request_claude_update(slot_id, now, to_version=latest or "", held_by=holder)
            return _back("updating", anchor)
        if action == "stable":
            store.choose_stable(slot_id, held_by=holder)
            return _back("stable", anchor)
        # cancel, token-done: whichever flow is in flight on this slot ends here.
        store.clear_slot_login(slot_id, held_by=holder)
        return _back("done" if action == "token-done" else "cancelled", anchor)
    except NotYours:
        raise
    except (StoreError, slotstates.TransitionError):
        return _back("not-now", anchor)


# -- the page --------------------------------------------------------------------

# What the user site adds to the shared styles: its own frame (a bar on top and a
# footer, the same on every page people are sent to), the account page, the
# doors, and the progress a slot shows while the machine works on it.
USER_CSS = CSS + """
body.site{font-size:15.5px;display:flex;flex-direction:column;min-height:100vh}
.site main{flex:1 0 auto;width:100%}
.site .page{max-width:1120px;padding-block:34px 64px}
.site .page.narrow{max-width:880px}
.site .page.doc{max-width:840px}

/* The bar's links to the pages anybody can read. */
.doc-nav{display:flex;align-items:center;gap:2px;font-size:14px;min-width:0;
overflow-x:auto;scrollbar-width:none}
.doc-nav::-webkit-scrollbar{display:none}
.doc-nav a{color:var(--muted);text-decoration:none;padding:7px 11px;border-radius:9px;
white-space:nowrap;font-weight:560}
.doc-nav a:hover{color:var(--ink);background:var(--inset)}
.doc-nav a.here{color:var(--acc);background:var(--acc-soft)}

/* The footer: the same ways out, from every page. */
.sitefoot{border-top:1px solid var(--rule);background:var(--panel)}
.sitefoot-in{max-width:1120px;margin:0 auto;padding:26px 20px 34px;display:flex;
flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:14px 40px;
font-size:13.5px;color:var(--muted)}
.sitefoot .brand{font-size:15px}
.sitefoot .brand .mark{width:22px;height:22px}
.sitefoot p{margin:10px 0 0;max-width:46ch}
.sitefoot nav{display:flex;flex-wrap:wrap;gap:8px 20px;padding-top:3px}
.sitefoot nav a{color:var(--muted);text-decoration:none}
.sitefoot nav a:hover{color:var(--ink)}

/* A page's title, and the line under it. */
.pagehead{margin:0 0 22px}
.pagehead .sub{margin-top:8px;font-size:15px}
.note-banner{border:1px solid var(--rule);border-radius:12px;padding:12px 16px;
margin:0 0 18px;font-weight:550;background:var(--panel)}
.note-banner.ok{border-color:var(--ok-line);background:var(--ok-bg);color:var(--ok)}
.note-banner.warn{border-color:var(--warn-line);background:var(--warn-bg);color:var(--warn)}
.lapsed{color:var(--warn);font-weight:650}
.switched{color:var(--ok);font-weight:650}
.card+.card{margin-top:14px}
.card ul{margin:10px 0;padding-left:20px}.card li{margin:7px 0;line-height:1.55}
.card li::marker{color:var(--acc)}

/* The allowance: how many you may hold, and the one button that spends it. */
.card.allowance{display:flex;align-items:center;justify-content:space-between;gap:14px 24px;
flex-wrap:wrap;padding:18px 22px;margin:0 0 24px}
.allowance h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
margin:0 0 4px;font-weight:680}
.allowance p{margin:2px 0}

/* A slot: its name and state on top, then what you can do with it. */
.slots{display:grid;gap:18px}
.card.slot{padding:0;overflow:hidden;scroll-margin-top:96px}
.slot-head{padding:16px 22px 14px;border-bottom:1px solid var(--rule-soft)}
.slot-head h2{margin:0;display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;
font-family:var(--mono);font-size:18px;font-weight:700;letter-spacing:-.01em}
.slot-head h2 .tag{font-family:var(--sans);font-size:12px;font-weight:650;letter-spacing:.01em}
.slot-meta{margin:6px 0 0;font-size:13px;color:var(--muted)}
.slot-body{padding:4px 22px 6px}
.slot-body>p{margin:14px 0}
.card.slot[data-state="releasing"] .slot-head{background:var(--off-bg)}
.card.slot[data-state="releasing"] .slot-body{color:var(--muted)}
/* The machine is working on it: said by motion, and stated in words beside it. */
.progress{height:4px;border-radius:99px;background:var(--acc-soft);overflow:hidden;
margin:14px 0 4px;max-width:320px}
.progress i{display:block;height:100%;width:36%;border-radius:99px;background:var(--acc);
animation:ccfleet-slide 1.6s ease-in-out infinite}
[data-state="releasing"] .progress{background:var(--off-bg)}
[data-state="releasing"] .progress i{background:var(--off)}
@keyframes ccfleet-slide{from{transform:translateX(-100%)}to{transform:translateX(280%)}}
@media (prefers-reduced-motion:reduce){.progress i{animation:none;width:100%;opacity:.4}}
.signed{margin:14px 0 4px;font-size:15px}
/* Remote Control's state as a light beside the sentence; the sentence itself
   stays ordinary running text, link and all. */
.rc{position:relative;padding-left:18px;margin:4px 0 14px}
.rc::before{content:"";position:absolute;left:0;top:.55em;width:8px;height:8px;
border-radius:50%;background:var(--off)}
.rc.on::before{background:var(--led);box-shadow:0 0 0 3px var(--ok-bg)}
.usage{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:18px 28px;
align-items:start;margin:4px 0 14px;padding:16px 18px;border-radius:12px;
background:var(--inset);border:1px solid var(--rule-soft)}
.usage.one{grid-template-columns:minmax(0,1fr)}
.usage .usage-nums{margin:0 0 10px}
.refresh{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:10px}
.refresh form{margin:0}
.usage-trend p{margin:0 0 8px}
.usage-trend svg.spark{height:52px}
.slot-body .row-line{padding:14px 0}
.row-line.release .row-name{color:var(--muted)}
.row-line.release .check{color:var(--muted);max-width:56ch}
/* A sign-in or a token under way: the one thing on the card that is waiting on you. */
.row-line.flow{margin:10px 0 14px;padding:16px 18px;border:1px solid var(--acc-line);
background:var(--acc-soft);border-radius:12px}
.row-line.flow .login-url{background:var(--panel)}
.row-line.flow input[type=text]{max-width:280px}
.row-line.flow .flow-why{margin:0 0 10px;font-size:14px;max-width:64ch}
/* Claude Code on the slot: the version it runs, and one press to the newest. */
.cc-say{font-size:14px;overflow-wrap:anywhere}
.cc-say b{font-family:var(--mono);font-size:13.5px;font-weight:650}
.cc-say .sep{color:var(--muted);margin:0 2px}
.cc-say.bad-text{font-weight:600}
.row-line.cc .cc-note{flex-basis:100%;margin:-2px 0 0;font-size:13px;color:var(--muted)}
button.quiet{font-size:12.5px;padding:6px 11px;font-weight:600}
.site .page>p.note{margin-top:26px;padding:14px 18px;border:1px solid var(--rule);
border-radius:12px;background:var(--panel)}

/* The doors: signing in, the console's, and a page that is not there. */
.door{max-width:520px;margin:24px auto 0;padding:28px 30px 24px}
.door .mark{width:44px;height:44px}
.door h1{margin:18px 0 10px}
.door .btn.big{width:100%;margin:8px 0 4px}
.door-alt{max-width:520px;margin:16px auto 0}

/* A device token: the one string on its page, selected whole with one click. */
pre.token{white-space:pre-wrap;word-break:break-all;font-size:14px;user-select:all;
-webkit-user-select:all;background:var(--panel);border-color:var(--acc-line)}

@media (max-width:760px){
.topbar-in{padding:10px 16px;gap:6px 12px}
.doc-nav{order:3;flex:1 0 100%;margin:0 -8px}
.doc-nav a{padding:6px 8px;font-size:13.5px}
.site .page{padding-block:24px 48px;padding-left:16px;padding-right:16px}
.card.slot{scroll-margin-top:124px}
.slot-head,.slot-body{padding-left:16px;padding-right:16px}
.usage{grid-template-columns:minmax(0,1fr);padding:14px}
.door{padding:22px 20px 20px}
.sitefoot-in{padding-left:16px;padding-right:16px}}
""" + status.LINE_CSS

#: The pages anybody can read, in the order the bar on top shows them.
NAV = (("/", "Overview"), ("/docs/guide", "Guide"),
       ("/docs/how-it-works", "How it works"), ("/privacy", "Privacy"),
       ("/docs/terms", "Terms"))
def _shell(title: str, body: str, refresh: str = "", extra_css: str = "", *,
           here: str = "", viewer: Optional[Viewer] = None, door: bool = False,
           width: str = "narrow", canonical: str = "") -> str:
    """A page of the user site, in its frame: the bar on top, the page, the footer.

    ``here`` marks the bar's link to this page, and nothing else is marked, so
    somebody can always tell where they are. The bar's corner says who is
    looking: their menu when they are signed in, a way to sign in when not,
    and nothing on a page that is itself the way in (``door``). A page served
    at more than one address names the one to keep in ``canonical``.
    """
    links = "".join(f'<a href="{path}"{_HERE if path == here else ""}>{escape(name)}</a>'
                    for path, name in NAV)
    corner = viewer.menu() if viewer is not None else "" if door else SIGN_IN_LINK
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"{refresh}<title>ccfleet · {escape(title)}</title>"
            f'<link rel="icon" href="{FAVICON}">'
            + (f'<link rel="canonical" href="{escape(canonical)}">' if canonical else "")
            + f"<style>{USER_CSS}{extra_css}</style></head>"
            '<body class="site"><header class="topbar"><div class="topbar-in">'
            f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
            f'<nav class="doc-nav">{links}</nav>'
            f'<div class="topbar-end">{corner}</div></div></header>'
            f'<main class="page {escape(width)}">{body}</main>'
            '<footer class="sitefoot"><div class="sitefoot-in"><div>'
            f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
            "<p>Claude Code on a machine that is always on. You bring your own Claude "
            "plan.</p></div>"
            '<nav aria-label="More"><a href="/account">Your slots</a>'
            '<a href="/docs/guide">Guide</a><a href="/docs/how-it-works">How it works</a>'
            '<a href="/privacy">Privacy</a><a href="/docs/terms">Terms</a>'
            '<a href="/status">Status</a>'
            '<a href="https://github.com/cdcupt/ccfleet" target="_blank" '
            'rel="noopener noreferrer">Source code</a></nav>'
            "</div></footer>" + LOCAL_TIMES_TAG + "</body></html>")


_HERE = ' class="here"'


def _form(action: str, csrf: str, label: str, inner: str = "", cls: str = "") -> str:
    return (f'<form class="inline" method="post" action="{escape(action)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
            f'<button class="{cls}" type="submit">{escape(label)}</button></form>')


def page(store: Store, cfg: Config, account: Optional[Mapping[str, Any]],
         session_id: str, now: float, note: str = "") -> str:
    """The whole page, for one person. Signed out, it offers sign-in and nothing else."""
    if account is None:
        return _signed_out(cfg)
    held = store.list_slots(held_by=account["id"])
    latest = store.latest_heartbeats()
    nodes = {n["id"]: n for n in store.list_nodes()}
    # From wherever each slot keeps it: an owner's node counted as their slot
    # signs in through the node's own row.
    logins = {s["id"]: store.login_for_slot(s) or {} for s in held}
    # The numbers Anthropic's channels stood at when last read, and any update
    # asked for on this page, for each slot's Claude Code row.
    channels = store.get_channel_versions()
    updates = {s["id"]: store.get_claude_update(s["id"]) or {} for s in held}
    csrf = csrf_for(session_id, cfg.cookie_secret)
    quota = int(account.get("slot_quota") or 0)
    counted = sum(1 for s in held if s["state"] in slotstates.HELD)

    tone, words = NOTES.get(note, ("", ""))
    banner = f'<div class="note-banner {tone}">{escape(words)}</div>' if words else ""
    claim = ""
    if quota == 0:
        allowance = ("<p><strong>You have no slots yet.</strong></p>"
                     "<p class=\"muted\">Slots are assigned by the operator. Once you have "
                     "an allowance, you can claim one here. "
                     '<a href="/docs">How to buy a slot</a>.</p>')
    else:
        allowance = (f"<p>You may hold <strong>{quota}</strong> "
                     f"{'slot' if quota == 1 else 'slots'}, and hold "
                     f"<strong>{counted}</strong>.</p>")
        if counted < quota:
            claim = _form("/account/claim", csrf, "Claim a slot", cls="primary")
    allowance += _paid(payments.paid_through(store.list_payments(account["id"])), now)
    # The operator's alerts about the rule, said to the holder without naming
    # the other place: it may be somebody else's.
    flagged = {s["id"]: _flags(s, store.open_alerts(s["node_id"])) for s in held}
    # Each slot's machine, in the words of the status page: a shared machine
    # reports every minute, somebody's own node every five.
    lines = {s["id"]: status.slot_line(status.machine_state(
        latest.get(s["node_id"]), store.open_alerts(s["node_id"]),
        s.get("kind") != slotstates.OWNER_SLOT, now), now) for s in held}
    cards = "".join(_slot_card(s, nodes.get(s["node_id"]) or {}, latest.get(s["node_id"]),
                               logins[s["id"]], csrf, cfg, now, flagged[s["id"]],
                               updates[s["id"]], channels, machine_line=lines[s["id"]])
                    for s in held)
    body = (
        '<div class="pagehead"><h1>Your slots</h1>'
        f'<p class="sub">Signed in as <strong>{escape(str(account.get("email", "")))}'
        "</strong></p></div>"
        + banner
        + f'<div class="card allowance"><div><h2>Your allowance</h2>{allowance}</div>'
        f"{claim}</div>"
        + (f'<div class="slots">{cards}</div>' if cards else "")
        + (_outage_emails(account, csrf) if cfg.emails_ready else "")
        + '<p class="note">Your slot is a Linux account on a machine we operate, with its own '
        "home, its own Claude Code and your own Claude sign-in. Other people's slots on the "
        "machine cannot read yours; the machine's administrators technically can. This page "
        "never shows your files or conversations, and nothing here holds your Claude "
        "credential: it is written on the machine when you sign in, and nowhere else.</p>")
    updating = any(u.get("state") == "pending" for u in updates.values())
    # A usage read under way: come back soon, so the new numbers show.
    reading = any(quota_reading(s.get("quota_wanted_at"),
                                (_report_for(s, latest.get(s["node_id"])).get("quota") or {})
                                .get("checked_at"), now) for s in held)
    return _shell("your slots", body, _refresh(held, logins, updating or reading),
                  viewer=viewer_for(account, session_id, cfg, store))


def _paid(through: Optional[str], now: float) -> str:
    """Their own paid-through day, and only the day: amounts and the operator's
    notes stay on the console. Nothing at all when nothing is recorded, since
    plenty of allowances are arranged without one and "no payment" would read
    as a debt."""
    state = payments.standing(through, now)
    if state == payments.PAID:
        return f'<p class="muted">Paid through <strong>{escape(str(through))}</strong>.</p>'
    if state == payments.LAPSED:
        return f'<p class="lapsed">Your paid period ended on {escape(str(through))}.</p>'
    return ""


def _signed_out(cfg: Config) -> str:
    if not cfg.google_ready:
        # Said plainly rather than showing a button that cannot work.
        return _shell("sign in", f'<div class="card door">{MARK}<h1>Sign in</h1>'
                      "<p>Sign-in is not set up on this server.</p>"
                      "<p class=\"muted\">An operator configures "
                      "<code>CCFLEET_GOOGLE_CLIENT_ID</code>, "
                      "<code>CCFLEET_GOOGLE_CLIENT_SECRET</code> and "
                      "<code>CCFLEET_COOKIE_SECRET</code> to turn it on.</p></div>", door=True)
    return _shell("sign in", f'<div class="card door">{MARK}<h1>Sign in to your slots</h1>'
                  "<p>ccfleet gives you a slot on a machine we operate: your own Linux "
                  "account there, with Claude Code, signed in to your own Claude account. "
                  "The operator decides who gets slots.</p>"
                  "<p>Sign in to see the slots you hold.</p>"
                  "<p><a class=\"btn primary big\" href=\"/auth/google/start?next=/account\">"
                  "Continue with Google</a></p>"
                  # A promise about oauth.SCOPES; a test keeps the two together.
                  "<p class=\"muted small\">We ask Google for your email address, whether "
                  "Google has verified it, and the id it gives your account, which stays the "
                  "same if the address changes. From Google we keep only the address and the "
                  "id. <a href=\"/privacy\">What else we keep, and why</a>.</p></div>"
                  '<div class="door-alt"><p class="muted">New to ccfleet? Start with '
                  '<a href="/docs">what it is</a> and <a href="/docs/guide">how to begin</a>.'
                  "</p></div>", door=True)


#: When the privacy page last changed in substance. Change it with the words.
PRIVACY_UPDATED = "2026-09-24"


def _span(seconds: int) -> str:
    """"14 days", "36 hours", "15 minutes": the privacy page quotes the settings in force."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def privacy_page(cfg: Config, viewer: Optional[Viewer] = None) -> str:
    """What ccfleet keeps about the people who use it, in plain words.

    Every length of time on it is read from the settings in force, so the page
    cannot drift from what the server does. Public: Google links to it from the
    sign-in screen, and nobody should need an account to read it.
    """
    contact = (f'<a href="mailto:{escape(cfg.contact_email)}">{escape(cfg.contact_email)}</a>'
               if cfg.contact_email else
               "the support address Google shows on ccfleet&#x27;s sign-in screen")
    attempt = _span(LOGIN_MAX_AGE_S)
    body = (
        '<div class="pagehead"><h1>Privacy</h1>'
        f'<p class="sub">Last updated {escape(PRIVACY_UPDATED)}</p></div>'
        '<div class="card"><h2>What ccfleet is</h2>'
        "<p>ccfleet gives you a slot on a machine we operate: your own Linux account there, "
        "with Claude Code, signed in to your own Claude account. The operator decides who "
        f"gets slots. To reach the operator, write to {contact}.</p></div>"
        '<div class="card"><h2>What we get from Google</h2>'
        "<p>When you sign in with Google we ask for your email address, whether Google has "
        "verified it, and the id Google gives your account. We keep the address and the id. "
        "We do not ask for your name, your photo, your contacts, or access to your Gmail, "
        "Drive or anything else in your Google account.</p></div>"
        '<div class="card"><h2>What we keep because you use ccfleet</h2><ul>'
        "<li>Your account: the address and id above, whether you are an operator, how many "
        "slots you may hold, when you first signed in, when you last visited, and whether "
        "you asked for outage emails.</li>"
        "<li>The slots you hold, when you claimed each one, when a device token was last "
        "handed out for it, and when you last moved it to another Claude account, which a "
        "slot may do once a week. Each slot you hold has a name: a neutral one like "
        "slot-4821 when you claim it, never anything from your address, or the one you give "
        "it on your page (or, if you asked, a name the operator set for you). That name is "
        "also the one its machine answers to in claude.ai/code, so Anthropic sees it too. "
        "The name and the date go when you give the slot back.</li>"
        "<li>Your sign-in here: a random value in a cookie, of which we store only a hash, "
        "with when it began and when it ends. "
        f"It lasts {_span(cfg.session_ttl_s)}, or until you sign out.</li>"
        "<li>Payments the operator has recorded for you: the amount, the currency, the day "
        "it covers you to, the operator&#x27;s own note, when and by whom it was recorded, "
        "and whether it was later voided.</li>"
        "<li>What your slot reports about itself: which version of Claude Code is installed, "
        "whether it is signed in, the email address and plan of the one Claude account "
        "signed in on it, so your page can show which of your accounts it is, a "
        "fingerprint of that account (a one-way digest of Anthropic&#x27;s id for it, which "
        "cannot be turned back into the id or your address) and of the account the slot "
        "keeps, so we can tell when one account is signed in on two machines, or a slot on "
        "another account than its own, which ccfleet does not allow, that "
        "plan&#x27;s rate-limit tier, when that sign-in expires, whether Remote Control is "
        "running, how much of your Claude usage limits is used and when they reset, and how "
        "many tokens were used each hour over the last week. The token counts are worked "
        "out on the machine, from Claude Code&#x27;s own records in your slot; only the "
        "numbers leave it. We keep these reports for "
        f"{_span(cfg.retention_days * 86400)}.</li></ul>"
        "<p>Your slot never reports your prompts, your conversations, your files, your Claude "
        "credential, or the name on your Claude account.</p></div>"
        '<div class="card"><h2>Your Claude account</h2>'
        "<p>You sign in to Claude yourself, through Anthropic. The credential that creates is "
        "written on the machine, in your slot, and nowhere else: this server never stores "
        f"it. While a sign-in is in progress, its link, the code you paste and its progress "
        f"are held here for at most {attempt}. If one does not go through, why is kept for "
        f"your page, and nothing else of it, for at most {attempt}, and so is how a change "
        f"of account ended. A device token you ask for is held here until you say you are "
        f"done with it, and for at most {attempt}.</p>"
        "<p>Claude Code on your slot talks to Anthropic directly, under your own account and "
        "Anthropic&#x27;s own terms and privacy policy.</p></div>"
        '<div class="card"><h2>What the operator can see</h2>'
        "<p>The machines are ours, and their administrators have root. That means they can "
        "technically read any slot&#x27;s files, and its Claude credential. No feature of "
        "ccfleet does this and we do not look, but no setting can make it impossible, so "
        "please keep nothing on a slot that you could not accept an administrator being "
        "able to read.</p>"
        "<p>In the console, the operator sees your email address, your allowance, the slots "
        "you hold, when you last visited, and the payments recorded for you.</p></div>"
        '<div class="card"><h2>Cookies</h2>'
        "<p>Two, both needed to sign you in: your session cookie, and one that ties "
        "Google&#x27;s answer to the browser that asked for it, which lasts "
        f"{_span(oauth.FLOW_TTL_S)}; the server keeps a matching record of that sign-in "
        "for the same time. There is no analytics, no advertising and no third-party "
        "script on any page.</p></div>"
        '<div class="card"><h2>Sharing, keeping and deleting</h2>'
        "<p>We do not sell what we keep, and we do not give it to anyone, but for one thing "
        "you choose: if you turn on outage emails, your address and your slot&#x27;s name go "
        "to Resend, the service that delivers them, each time there is an outage to tell "
        "you about. The servers run at hosting companies we rent them from, and Google and "
        "Anthropic see what you do with their own services: signing in, and using "
        "Claude.</p>"
        "<p>Giving a slot back deletes its Linux account and every file in it. Your account "
        "and the payments recorded for you stay while your account exists. To have your "
        "account deleted, write to the operator: it is done by hand, once any slot you hold "
        "has been given back and wiped.</p>"
        "<p>If any of this changes, this page changes, and the date at the top says when.</p>"
        "</div>")
    return _shell("privacy", body, here="/privacy", width="doc", viewer=viewer)


def _refresh(held: list[Mapping[str, Any]], logins: Mapping[str, Mapping[str, Any]],
             updating: bool = False) -> str:
    """Come back soon while something is moving; never while a code is being typed.

    Always to the bare page, never the address the page was opened at. After an
    action that address is /account?note=…#slot-…, and a refresh naming no
    address on a page whose address has a fragment is a fragment navigation:
    the browser scrolls and reloads nothing. "Starting the sign-in on your
    slot…" stood for minutes that way, the link it was waiting for already
    there for anybody who reloaded by hand. The bare page also leaves the note
    behind: said once, right after the action, and from then on the cards say
    how things stand. The target never carries a fragment, for the same reason.
    """
    states = {(login or {}).get("state") for login in logins.values()}
    if "url_ready" in states:
        return ""
    moving = updating or any(s["state"] in (slotstates.CLAIMING, slotstates.RELEASING)
                             for s in held)
    soon = moving or bool(states & {"requested", "code_sent"})
    seconds = ACTIVE_REFRESH_S if soon else IDLE_REFRESH_S
    return f'<meta http-equiv="refresh" content="{seconds};url=/account">'


def _report_for(slot: Mapping[str, Any], heartbeat: Optional[Mapping[str, Any]]
                ) -> Mapping[str, Any]:
    """What the slot's machine last said about this slot, or nothing."""
    payload = (heartbeat or {}).get("payload") or {}
    for entry in payload.get("slots") or []:
        if isinstance(entry, Mapping) and entry.get("unix_user") == slot["unix_user"]:
            return entry
    return {}


#: Said on the card of a slot whose Claude account is live on another node too.
ELSEWHERE = ("The Claude account on this slot is also signed in on another machine in "
             "this fleet. ccfleet keeps one account on one machine: sign it out of one of "
             "them.")
#: Said on the card of a slot signed in to another account than the one it keeps.
CHANGED = ("This slot is signed in to another Claude account than the one it keeps. Sign "
           "that one in again, or move the slot to the other with Change account.")
#: Over a change of account while it runs: which account to sign in with, and
#: that nothing is lost or changed until it finishes.
SWITCH_FLOW = ("Sign in with the Claude account this slot should use from now on. Your "
               "files stay; until this finishes the slot keeps its current account.")
#: How a change of account ended, from the machine's fixed word: the class the
#: sentence is said in, and the sentence.
SWITCH_ENDED = {
    slotstates.SWITCHED: ("switched", "Your slot now uses the new Claude account."),
    slotstates.SAME_ACCOUNT: ("lapsed", "That is the account this slot already had; "
                                        "nothing changed."),
}
#: What a flow that did not go through was, by its kind.
NOT_DONE = {"token": "The device token was not made",
            "switch": "The change of account did not go through"}


def _flags(slot: Mapping[str, Any], alerts: list[Mapping[str, Any]]) -> frozenset[str]:
    """Which of the one-account rules the operator's alerts say this slot breaks.

    A machine names each slot's alert by its Linux user; an owner's own node,
    counted as their slot, has the node's own alert, with nothing after it —
    and no binding to break, which only a machine's slots keep.
    """
    rules = {a["rule"] for a in alerts}
    if slot.get("kind") == slotstates.OWNER_SLOT:
        return frozenset(r for r in ("account_elsewhere",) if r in rules)
    return frozenset(r for r in ("account_elsewhere", "account_changed")
                     if f"{r}:{slot['unix_user']}" in rules)


def _own_report(heartbeat: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """What an owner's own node says about itself, in a slot report's shape:
    it reports these at the top of its heartbeat rather than per slot."""
    payload = (heartbeat or {}).get("payload") or {}
    return {key: payload.get(key) or {}
            for key in ("claude", "credentials", "remote_control", "quota", "usage")}


def _own_state(report: Mapping[str, Any]) -> tuple[str, str, str]:
    """Signed in or not, from the node's own word: its slot never goes
    through the lifecycle, so its state says nothing about that."""
    if (report.get("credentials") or {}).get("logged_in") is True:
        return STATE_WORDS[slotstates.ACTIVE]
    return ("warn", "Not signed in", "Sign in to your own Claude account below.")


def _slot_card(slot: Mapping[str, Any], node: Mapping[str, Any],
               heartbeat: Optional[Mapping[str, Any]], login: Mapping[str, Any],
               csrf: str, cfg: Config, now: float,
               flagged: frozenset[str] = frozenset(),
               update: Optional[Mapping[str, Any]] = None,
               channels: Optional[Mapping[str, Any]] = None, *,
               machine_line: str = "") -> str:
    own = slot.get("kind") == slotstates.OWNER_SLOT
    report = _own_report(heartbeat) if own else _report_for(slot, heartbeat)
    # A sign-in or token that failed, or a change of account that finished, is
    # over: it is said once, below, and the buttons come back as if nothing
    # were in flight.
    ended = login if login.get("state") in ("failed", "done") else {}
    login = {} if ended else login
    if own:
        tone, title, detail = _own_state(report)
        # A token handed over for their own node is noted on the node.
        slot = {**slot, "device_token_at": node.get("device_token_at") or 0}
    else:
        tone, title, detail = STATE_WORDS.get(slot["state"], ("disabled", slot["state"], ""))
    heard = (heartbeat or {}).get("ts")
    if heard is None:
        machine = '<span class="bad-text">not heard from yet</span>'
    elif now - heard > cfg.heartbeat_max_age_s:
        machine = (f'<span class="bad-text">not heard from in {escape(_age(now, heard))}'
                   "</span> &mdash; your slot may be unreachable")
    else:
        machine = f"last heard {escape(_age(now, heard))} ago"
    region = f" · {escape(node['region'])}" if node.get("region") else ""
    claimed = (f" · claimed {escape(_age(now, slot['claimed_at']))} ago"
               if slot.get("claimed_at") else "")
    # The name claude.ai/code shows them, and nothing about the machine it is
    # on: while they hold it, the name is theirs (Erik, 2026-09-24).
    name = names.display(slot)
    about = f"{region}{claimed}".lstrip(" ·")
    # Somebody's own node, said beside its name rather than lost in the small
    # print: it is theirs outright, not one of ours they hold.
    kind, mine = (' data-kind="own"', ' <span class="tag">your own machine</span>') if own \
        else ("", "")
    parts = [
        f'<div class="card slot" id="slot-{escape(slot["id"])}" '
        f'data-state="{escape(str(slot["state"]))}"{kind}><div class="slot-head">'
        f'<h2>{escape(name)} <span class="pill {tone}">{escape(title)}</span>{mine}</h2>',
        f'<p class="slot-meta">{about + " · " if about else ""}machine {machine}</p>'
        '</div><div class="slot-body">',
    ]
    # Its machine's state, as the status page says it (status.slot_line).
    if machine_line:
        parts.append(machine_line)
    if detail:
        parts.append(f"<p>{escape(detail)}</p>")
    if slot["state"] in (slotstates.CLAIMING, slotstates.RELEASING):
        # The machine is working on it. The sentence above says what; this
        # says it is still moving, which a still page otherwise cannot.
        parts.append('<div class="progress" aria-hidden="true"><i></i></div>')
    signed_in = (report.get("credentials") or {}).get("logged_in") is True
    if (signed_in if own else slot["state"] == slotstates.ACTIVE):
        parts.append(_in_use(report, now, "" if own else _quota_refresh(slot, report, csrf,
                                                                          now)))
    if slot["state"] in CAN_SIGN_IN:
        for rule, words in (("account_elsewhere", ELSEWHERE), ("account_changed", CHANGED)):
            if rule in flagged:
                parts.append(f'<p class="lapsed">{escape(words)}</p>')
    if ended and slot["state"] in CAN_SIGN_IN:
        parts.append(_ended(ended))
    if slot["state"] in CAN_SIGN_IN:
        parts.append(_sign_in(slot, report, login, csrf, now))
        parts.append(_claude_row(slot, node, report, update or {}, channels or {}, csrf))
        parts.append(_tokens(slot, login, csrf, now))
    if slot["state"] in CAN_SIGN_IN and not own:
        parts.append(_rename(slot, csrf))
    # Never on somebody's own node: giving back means wiping, and nothing
    # there is ours to wipe.
    if slot["state"] in slotstates.RELEASABLE and not own:
        parts.append(_release(slot, csrf))
    parts.append("</div></div>")
    return "".join(parts)


def _in_use(report: Mapping[str, Any], now: float, refresh: str = "") -> str:
    """Signed in: as whom, the plan, a way in, and how much of each window is left.

    One Claude account per slot, so this names it: the holder can see which of
    their accounts this slot is, and how long before Anthropic asks for a
    fresh sign-in. A slot stays in use when that sign-in is gone, so this says
    so plainly rather than going on about one that is not there.
    """
    creds = report.get("credentials") or {}
    if creds.get("logged_in") is False:
        return "<p>Not signed in to Claude right now. Sign in below to use your slot.</p>"
    remote = (report.get("remote_control") or {}).get("state")
    plan = plans.label(creds.get("subscription_type"), creds.get("plan"))
    who = f" as {escape(str(creds['email']))}" if creds.get("email") else ""
    left = _sign_in_left(creds.get("refresh_expires_at"), now)
    lines = [f'<p class="signed">Signed in{who}'
             f"{(' · ' + escape(str(plan)) + ' plan') if plan else ''}"
             f"{' · sign-in ' + left if left else ''}.</p>"]
    if remote == "active":
        lines.append('<p class="rc on">Remote Control is on: open <a href="https://claude.ai/code" '
                     'target="_blank" rel="noopener noreferrer">claude.ai/code</a> or the '
                     "Claude app, signed in as the same account, and pick this machine.</p>")
    else:
        lines.append('<p class="rc">Remote Control is starting; it comes on within a '
                     "minute of signing in.</p>")
    quota = report.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    read = quota.get("checked_at")
    bars = (_meter(session.get("used_pct"), "5-hour session", session.get("resets"), read, now,
                   session.get("resets_at"))
            + _meter(week.get("used_pct"), "This week", week.get("resets"), read, now,
                     week.get("resets_at")))
    usage = report.get("usage") or {}
    spent = ""
    if usage:
        # The same week, by the hour, that the console draws — and the same
        # care to say that these windows are the account's, and the tokens
        # only what ran on this slot.
        chart, caption = _usage_chart(usage)
        spent = (f'<p class="small"><b>{escape(_human_tokens(usage.get("total_tokens") or 0))}'
                 f"</b> tokens run on this slot itself, last {escape(_usage_span(usage))}</p>"
                 f"{chart}{caption}")
    if bars:
        # Said under the bars, because beside "tokens run on this slot" they
        # read as the same thing: a week at 26% beside 78k tokens looked wrong
        # when the rest of the week had run on the holder's own laptop.
        bars = ('<div class="usage-nums muted">your Claude account &middot; every device'
                "</div>" + bars + f'<p class="small muted">{ACCOUNT_WIDE}</p>' + refresh)
    elif refresh:
        # Signed in and not read yet: the first reading can be asked for too.
        bars = '<p class="small muted">No reading of your limits yet.</p>' + refresh
    # The windows beside the trend: what is left now, and how it got there.
    halves = [f'<div class="usage-{name}">{html}</div>'
              for name, html in (("bars", bars), ("trend", spent)) if html]
    if halves:
        cls = "usage" if len(halves) == 2 else "usage one"
        lines.append(f'<div class="{cls}">{"".join(halves)}</div>')
    return "".join(lines)


def _sign_in_left(expires: Any, now: float) -> str:
    """How long a sign-in has before Anthropic asks for a fresh one, or ""."""
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= now:
        return ""
    days = int((expires - now) // 86400)
    if days >= 2:
        return f"good for {days} more days"
    return "good for 1 more day" if days == 1 else "ends within a day"


def _ended(login: Mapping[str, Any]) -> str:
    """How a flow that is over ended, said once on the card: a change of
    account by the machine's fixed word, anything that failed with its reason."""
    if login.get("state") == "done":
        cls, words = SWITCH_ENDED.get(str(login.get("detail")), ("", ""))
        return f'<p class="{cls}">{escape(words)}</p>' if words else ""
    what = NOT_DONE.get(str(login.get("kind")), "The sign-in was not kept")
    return (f'<p class="lapsed">{what}: '
            f'{escape(str(login.get("detail") or "no reason given"))}.</p>')


def _sign_in(slot: Mapping[str, Any], report: Mapping[str, Any],
             login: Mapping[str, Any], csrf: str, now: float) -> str:
    base = f"/account/slots/{escape(slot['id'])}"
    state = login.get("state") if login.get("kind") != "token" else None
    if not state:
        signed_in = (report.get("credentials") or {}).get("logged_in") is True
        if login.get("state"):
            return ('<p class="muted small">Finish or cancel the device token below before '
                    "signing in again.</p>")
        field = ("" if signed_in else
                 '<input type="email" name="email" placeholder="your Claude email (optional)">')
        return ('<div class="row-line"><div class="row-name">Claude</div>'
                '<div class="actions">'
                + _form(f"{base}/signin", csrf, "Sign in again" if signed_in else
                        "Sign in to Claude", field, "" if signed_in else "primary")
                + _change_account(slot, csrf, now) + "</div></div>")
    body = f'<span class="login-say">{escape(LOGIN_WORDS.get(state, state))}</span>'
    if login.get("kind") == "switch":
        body = f'<p class="flow-why">{escape(SWITCH_FLOW)}</p>' + body
    url = login.get("url") or ""
    # Checked again here, not only when it was stored: a link on this page must
    # never be anything but a sign-in on Anthropic's own hosts.
    if is_login_url(url) and state in ("url_ready", "code_sent"):
        body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                 f'rel="noopener noreferrer">{escape(url)}</a>')
    if state == "url_ready":
        body += _form(f"{base}/code", csrf, "Send code",
                      '<input type="text" name="code" placeholder="paste the code" '
                      'autocomplete="off" required>', "primary")
    body += " " + _form(f"{base}/cancel", csrf, "Cancel", cls="danger")
    return (f'<div class="row-line stacked flow"><div class="row-name">Claude</div>'
            f"{body}</div>")


def _change_account(slot: Mapping[str, Any], csrf: str, now: float) -> str:
    """Moving a machine's slot in use to another Claude account: the button,
    or — for a week after the last move — when it comes back, in the viewer's
    own time where the page can say it (see LOCAL_TIMES_JS)."""
    if slot.get("kind") == slotstates.OWNER_SLOT or slot["state"] != slotstates.ACTIVE:
        return ""
    wait = slotstates.switch_wait_until(slot.get("account_switched_at"), now)
    if wait is None:
        return " " + _form(f"/account/slots/{escape(slot['id'])}/switch", csrf,
                           "Change account")
    return (' <span class="muted small">You can change account again '
            f'<time datetime="{escape(resets.iso(wait))}" data-local>'
            f"{escape(resets.until(now, wait))}</time></span>")


#: A channel as the page names it.
CHANNEL_WORDS = {"stable": "Stable", "latest": "Latest"}
#: Under the button that moves a slot off Stable: what pressing it commits to.
UPDATE_NOTE = "Switches this slot to the latest release; it keeps itself current from then on."
SEP = '<span class="sep">&middot;</span>'


def _claude_row(slot: Mapping[str, Any], node: Mapping[str, Any], report: Mapping[str, Any],
                update: Mapping[str, Any], channels: Mapping[str, Any], csrf: str) -> str:
    """Which Claude Code the slot runs, whether a newer one is out, and one
    press to move there. Nothing until the machine has said what it runs.

    Every number and reason here came from a machine or a download, so every
    one is escaped. The operator's hold offers nothing: it is a safety valve.
    """
    restart = (report.get("upgrade") or {}).get("restart") == "waiting"
    said = claude_versions.status((report.get("claude") or {}).get("version"),
                                  claude_versions.slot_target(slot, node), channels,
                                  update, restart)
    if said is None:
        return ""
    base = f"/account/slots/{escape(slot['id'])}"
    kind, latest, channel = said["status"], said["latest"], said["channel"]
    buttons, note = [], ""
    line = _claude_line(said)
    if kind in ("available", "failed"):
        label = f"Update to {latest}" if latest else "Update to the latest release"
        buttons.append(_form(f"{base}/update", csrf, label, cls="primary"))
        if channel != "latest":
            note = f'<p class="cc-note">{escape(UPDATE_NOTE)}</p>'
    if channel == "latest" and kind not in ("held", "updating"):
        buttons.append(_form(f"{base}/stable", csrf, "Back to Stable", cls="quiet"))
    tone = " bad-text" if kind == "failed" else ""
    return ('<div class="row-line cc"><div class="row-name">Claude Code</div>'
            f'<div class="actions"><span class="cc-say{tone}">{line}</span>'
            f'{" ".join(buttons)}</div>{note}</div>')


def _claude_line(said: Mapping[str, Any]) -> str:
    """The row's one sentence, for each thing it can say."""
    kind, version, latest = said["status"], escape(said["installed"]), said["latest"]
    if kind == "held":
        return f"<b>{version}</b> {SEP} held by the operator"
    if kind == "updating":
        to = said.get("to")
        return (f"Updating to <b>{escape(to)}</b>&hellip;" if to
                else "Updating to the latest release&hellip;")
    if kind == "updated":
        return (f"Updated to <b>{escape(said['to'])}</b> {SEP} Remote Control switches over "
                "once no session is open")
    if kind == "failed":
        return f"Update failed: {escape(said.get('detail') or 'no reason given')}"
    if said["channel"] == "latest":
        told = f" {SEP} up to date" if kind == "current" and latest else ""
        newer = f" {SEP} latest is <b>{escape(latest)}</b>" if kind == "available" else ""
        return f"<b>{version}</b>{told} {SEP} follows the latest release{newer}"
    where = (CHANNEL_WORDS["stable"] if said["channel"] == "stable"
             else "pinned" if said["pinned"] else "")
    line = f"<b>{version}</b>" + (f" {SEP} {where}" if where else "")
    if kind == "available":
        return line + f" {SEP} latest is <b>{escape(latest)}</b>"
    return line + (f" {SEP} up to date" if latest else "")


def _tokens(slot: Mapping[str, Any], login: Mapping[str, Any], csrf: str, now: float) -> str:
    base = f"/account/slots/{escape(slot['id'])}"
    state = login.get("state") if login.get("kind") == "token" else None
    if not state:
        if login.get("state"):
            return ""                     # a sign-in is in flight; one thing at a time
        issued = slot.get("device_token_at") or 0
        said = (f'<span class="pill ok">last issued {escape(_age(now, issued))} ago</span> '
                if issued else
                '<span class="muted small">None yet &middot; optional, for using this '
                'account from your own computer</span> ')
        return ('<div class="row-line"><div class="row-name">Device token</div>'
                f'<div class="actions">{said}'
                + _form(f"{base}/token", csrf, "Get another" if issued else "Get a device token")
                + "</div></div>")
    if state == "ready":
        body = (f'<span class="pill ok">{escape(TOKEN_WORDS["ready"])}</span> '
                + _form(f"{base}/token-show", csrf, "Show it", cls="primary") + " "
                + _form(f"{base}/token-done", csrf, "Done with it"))
        return (f'<div class="row-line"><div class="row-name">Device token</div>'
                f'<div class="actions">{body}</div></div>')
    body = f'<span class="login-say">{escape(TOKEN_WORDS.get(state, state))}</span>'
    url = login.get("url") or ""
    if is_login_url(url) and state in ("url_ready", "code_sent"):
        body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                 f'rel="noopener noreferrer">{escape(url)}</a>')
    if state == "url_ready":
        body += _form(f"{base}/code", csrf, "Send code",
                      '<input type="text" name="code" placeholder="paste the code" '
                      'autocomplete="off" required>', "primary")
    body += " " + _form(f"{base}/cancel", csrf, "Cancel", cls="danger")
    return (f'<div class="row-line stacked flow"><div class="row-name">Device token</div>'
            f"{body}</div>")


def _quota_refresh(slot: Mapping[str, Any], report: Mapping[str, Any], csrf: str,
                   now: float) -> str:
    """Read the windows again now, rather than at the next five minutes (Erik,
    2026-09-24), with when they were last read; or say a read is under way."""
    read = (report.get("quota") or {}).get("checked_at")
    when = (f"read {escape(_age(now, read))} ago · "
            if isinstance(read, (int, float)) and not isinstance(read, bool) else "")
    if quota_reading(slot.get("quota_wanted_at"), read, now):
        return f'<p class="small muted">{when}reading them again now&hellip;</p>'
    return (f'<div class="refresh"><span class="small muted">{when}every five minutes</span>'
            + _form(f"/account/slots/{escape(slot['id'])}/quota", csrf, "Refresh") + "</div>")


def _outage_emails(account: Mapping[str, Any], csrf: str) -> str:
    """Emails about outages, for those who ask (Erik, 2026-09-24)."""
    on = bool(account.get("outage_emails"))
    said = ("On: we email you when your slot's machine has been down for five minutes, "
            "and again when it is back." if on else
            "Off. Turned on, we email you when your slot's machine has been down for five "
            "minutes, and again when it is back.")
    button = _form("/account/outage-emails", csrf, "Turn off" if on else "Turn on",
                   f'<input type="hidden" name="on" value="{"0" if on else "1"}">',
                   "" if on else "primary")
    return ('<div class="card allowance" id="outage-emails"><div><h2>Outage emails</h2>'
            f"<p>{escape(said)}</p>"
            '<p class="small muted">Sent through Resend, which delivers them: your address and '
            "your slot's name go to it, and only when there is an outage to tell you about. "
            "The status page says the same for everybody.</p></div>"
            f"{button}</div>")


def _rename(slot: Mapping[str, Any], csrf: str) -> str:
    """The slot's name is its holder's to choose (Erik, 2026-09-24): it is what
    claude.ai shows, so it is what Anthropic sees, and it starts neutral."""
    return ('<div class="row-line stacked"><div class="row-name">Name</div>'
            + _form(f"/account/slots/{escape(slot['id'])}/rename", csrf, "Rename",
                    '<input type="text" name="name" maxlength="30" autocomplete="off" '
                    'required pattern="[A-Za-z0-9][A-Za-z0-9-]{0,28}[A-Za-z0-9]" '
                    f'placeholder="{escape(names.display(slot))}" '
                    'aria-label="A new name for this slot">')
            + '<p class="small muted">What claude.ai shows for this machine, so Anthropic '
            "sees it too: pick anything but your email address.</p></div>")


def _release(slot: Mapping[str, Any], csrf: str) -> str:
    return ('<div class="row-line release"><div class="row-name">Give it back</div>'
            + _form(f"/account/slots/{escape(slot['id'])}/release", csrf, "Give this slot back",
                    '<label class="check"><input type="checkbox" name="confirm" value="wipe" '
                    'required> Delete everything on it: files, sessions and the Claude '
                    "sign-in. Your Claude account itself is untouched.</label>", "danger")
            + "</div>")


def console_door(account: Optional[Mapping[str, Any]], session_id: str, cfg: Config,
                 store: Optional[Store] = None) -> str:
    """The console's front door for somebody not signed in as an operator.

    Operators sign in the way everybody else does, with Google; an account
    only reaches the console once the operator has made it an admin from the
    server's own command line. The admin token stays one link away, for when
    Google is the thing that is down.
    """
    if account is None:
        body = ("<p>Operators sign in with their Google account.</p>"
                "<p><a class=\"btn primary big\" href=\"/auth/google/start?next=/admin\">"
                "Continue with Google</a></p>")
    else:
        body = (f"<p>Signed in as <strong>{escape(str(account.get('email', '')))}</strong>, "
                "which is not an operator account.</p>"
                "<p class=\"muted\">An operator makes one on the server with "
                "<code>ccfleetd account role &lt;email&gt; admin</code>.</p>"
                + _form("/auth/signout", csrf_for(session_id, cfg.cookie_secret), "Sign out"))
    # Customers land here too, by typing the bare address: point them home.
    elsewhere = ('<div class="card door-alt"><p><strong>Looking for your slots?</strong> They '
                 'are on <a href="/account">your page</a>. New to ccfleet? Start with '
                 '<a href="/docs">what it is</a> and <a href="/docs/guide">how to begin</a>.'
                 "</p></div>")
    return _shell("console", f'<div class="card door">{MARK}<h1>ccfleet console</h1>' + body
                  + "<p class=\"note\">Or <a href=\"/auth/basic\">use the admin token</a> "
                  "&mdash; the way in when Google sign-in is unavailable.</p></div>" + elsewhere,
                  viewer=viewer_for(account, session_id, cfg, store, on_console=True),
                  door=True)


def token_page(slot: Mapping[str, Any], token: str, viewer: Optional[Viewer] = None) -> str:
    """The minted token, for as long as its request lasts."""
    if not token:
        return _shell("device token", f'<div class="card door">{MARK}<h1>Nothing to show</h1>'
                      "<p>No token is waiting on this slot. Either you were done with it, or "
                      "the request expired. Start a new one from your slots.</p>"
                      "<p><a class=\"back\" href=\"/account\">&larr; your slots</a></p></div>",
                      viewer=viewer)
    return _shell("device token", (
        "<div class=\"pagehead\"><h1>Device token</h1><p class=\"sub\">Minted on <strong>"
        f"{escape(slot['id'])}</strong>, for the Claude account you approved. Good for one "
        "year.</p></div>"
        "<div class=\"ok-banner\">You can come back and show this again while the request "
        "lasts. Press <strong>Done with it</strong> on your slots page when you have "
        "finished, or leave it and it expires on its own.</div>"
        f'<pre class="token">{escape(token)}</pre>'
        "<div class=\"card\"><h2>Put it on a machine</h2>"
        "<pre>bash -c \"$(curl -fsSL https://raw.githubusercontent.com/cdcupt/"
        "ccfleet/main/laptop/ccfleet-connect.sh)\"</pre>"
        "<p class=\"muted\">It asks for the token and hides what you paste. Then "
        "<code>claude</code> runs there with no login. Scope is inference only, which is "
        "Anthropic's limit on long-lived tokens; revoke it from your Claude account.</p>"
        "<p class=\"muted\">The computer keeps its own Claude login too, if it has one: "
        "<code>ccfleet-connect --off</code> switches it to that, your own subscription, "
        "and <code>ccfleet-connect --on</code> back to this token. Running it again with "
        "another token replaces this one.</p></div>"
        "<p><a class=\"back\" href=\"/account\">&larr; your slots</a></p>"), viewer=viewer)
