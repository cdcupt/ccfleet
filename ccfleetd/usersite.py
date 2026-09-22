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

from . import slots as slotstates
from .config import Config
from .desired import is_login_url
from .render import (
    CSS,
    LOGIN_WORDS,
    TOKEN_WORDS,
    _age,
    _human_tokens,
    _meter,
    _usage_chart,
    _usage_span,
)
from .store import (
    NoSlotAvailable,
    NotYours,
    QuotaExceeded,
    Store,
    StoreError,
    slot_login_key,
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
    "token": ("ok", "Starting a device token on your slot…"),
    "code": ("ok", "Code sent to your slot."),
    "cancelled": ("ok", "Cancelled."),
    "done": ("ok", "Done. The token is no longer kept here."),
    "not-now": ("warn", "Your slot cannot do that right now. It may still be setting up, "
                        "or being given back."),
}

STATE_WORDS = {
    slotstates.CLAIMING: ("warn", "Setting up",
                          "Creating your account on the machine and installing Claude Code. "
                          "A few minutes."),
    slotstates.CLAIMED: ("warn", "Ready to sign in",
                         "Sign in to your own Claude account below to start using it."),
    slotstates.ACTIVE: ("ok", "In use", ""),
    slotstates.RELEASING: ("disabled", "Being wiped",
                           "Everything on it is being deleted. It stops counting against your "
                           "allowance once the machine confirms it is gone."),
}

SLOT_ACTIONS = ("release", "signin", "code", "cancel", "token", "token-show", "token-done")
# What a slot can do, by state. Sign-in and tokens need the account to exist
# on the machine and the slot not to be on its way out.
CAN_SIGN_IN = (slotstates.CLAIMED, slotstates.ACTIVE)
IDLE_REFRESH_S = 60
ACTIVE_REFRESH_S = 4


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


def not_found() -> Outcome:
    return Outcome(404, body=_shell("Not found", "<h1>Not found</h1><div class=\"card\">"
                                    "<p>There is nothing here.</p>"
                                    "<p><a class=\"back\" href=\"/account\">&larr; your slots</a>"
                                    "</p></div>"))


# -- actions ---------------------------------------------------------------------

def act(store: Store, cfg: Config, account: Mapping[str, Any], path: str,
        form: Mapping[str, str], now: float) -> Outcome:
    """Do what a form on this page asked, for this account and nobody else."""
    parts = path.strip("/").split("/")
    if parts == ["account", "claim"]:
        return _claim(store, cfg, account, now)
    if len(parts) == 4 and parts[:2] == ["account", "slots"] and parts[3] in SLOT_ACTIONS:
        slot = store.get_slot(parts[2])
        if slot is None:
            return not_found()
        try:
            return _on_slot(store, slot, account["id"], parts[3], form, now)
        except NotYours:
            # Somebody else's slot answers exactly as a missing one: which ids
            # are held, and by whom, is not this person's to learn. The store
            # decides it, in the same transaction as the action itself.
            return not_found()
    return not_found()


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
             form: Mapping[str, str], now: float) -> Outcome:
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
        if action == "token":
            store.request_slot_login(slot_id, "", now, kind="token", held_by=holder)
            return _back("token", anchor)
        if action == "code":
            store.submit_slot_login_code(slot_id, form.get("code", ""), now, held_by=holder)
            return _back("code", anchor)
        if action == "token-show":
            return Outcome(200, body=token_page(
                slot, store.read_slot_secret(slot_id, now, held_by=holder)))
        # cancel, token-done: whichever flow is in flight on this slot ends here.
        store.clear_slot_login(slot_id, held_by=holder)
        return _back("done" if action == "token-done" else "cancelled", anchor)
    except NotYours:
        raise
    except (StoreError, slotstates.TransitionError):
        return _back("not-now", anchor)


# -- the page --------------------------------------------------------------------

# What this page adds to the console's styles: the note after an action, and
# a little air between one slot and the next.
USER_CSS = CSS + """
.note-banner{border:1px solid var(--rule);border-radius:12px;padding:12px 16px;
margin:0 0 18px;font-weight:500}
.note-banner.ok{border-color:var(--ok);background:var(--ok-bg);color:var(--ok)}
.note-banner.warn{border-color:var(--warn);background:var(--warn-bg);color:var(--warn)}
.card.slot{margin:0 0 16px}
.card.slot h2{text-transform:none;letter-spacing:0;font-family:var(--mono);font-size:15px;
color:var(--ink)}
.card.slot h2 .pill{margin-left:6px;vertical-align:1px;font-family:var(--sans)}
.card .usage{margin:8px 0 4px}
.card .usage svg.spark{max-width:520px}
label.check{font-weight:400;color:var(--muted);align-items:flex-start;margin-top:0}
"""


def _shell(title: str, body: str, refresh: str = "") -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"{refresh}<title>ccfleet · {escape(title)}</title><style>{USER_CSS}</style></head>"
            f"<body><div class=\"page\">{body}</div></body></html>")


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
    logins = {s["id"]: store.get_login(slot_login_key(s["id"])) or {} for s in held}
    csrf = csrf_for(session_id, cfg.cookie_secret)
    quota = int(account.get("slot_quota") or 0)
    counted = sum(1 for s in held if s["state"] in slotstates.HELD)

    tone, words = NOTES.get(note, ("", ""))
    banner = f'<div class="note-banner {tone}">{escape(words)}</div>' if words else ""
    if quota == 0:
        allowance = ("<p><strong>You have no slots yet.</strong></p>"
                     "<p class=\"muted\">Slots are assigned by the operator. Once you have "
                     "an allowance, you can claim one here.</p>")
    else:
        allowance = (f"<p>You may hold <strong>{quota}</strong> "
                     f"{'slot' if quota == 1 else 'slots'}, and hold "
                     f"<strong>{counted}</strong>.</p>")
        if counted < quota:
            allowance += _form("/account/claim", csrf, "Claim a slot", cls="primary")
    cards = "".join(_slot_card(s, nodes.get(s["node_id"]) or {}, latest.get(s["node_id"]),
                               logins[s["id"]], csrf, cfg, now) for s in held)
    body = (
        '<header class="mast"><div><h1>ccfleet<span class="dot">.</span></h1>'
        f'<p class="sub">Signed in as <strong>{escape(str(account.get("email", "")))}'
        "</strong></p></div>"
        + _form("/auth/signout", csrf, "Sign out") + "</header>"
        + banner
        + f'<div class="card"><h2>Your allowance</h2>{allowance}</div>'
        + (f"<h2>Your slots</h2>{cards}" if cards else "")
        + '<p class="note">Your slot is a Linux account on a machine we operate, with its own '
        "home, its own Claude Code and your own Claude sign-in. Other people's slots on the "
        "machine cannot read yours; the machine's administrators technically can. This page "
        "never shows your files or conversations, and nothing here holds your Claude "
        "credential: it is written on the machine when you sign in, and nowhere else.</p>")
    return _shell("your slots", body, _refresh(held, logins))


def _signed_out(cfg: Config) -> str:
    if not cfg.google_ready:
        # Said plainly rather than showing a button that cannot work.
        return _shell("sign in", "<h1>ccfleet</h1><div class=\"card\">"
                      "<p>Sign-in is not set up on this server.</p>"
                      "<p class=\"muted\">An operator configures "
                      "<code>CCFLEET_GOOGLE_CLIENT_ID</code>, "
                      "<code>CCFLEET_GOOGLE_CLIENT_SECRET</code> and "
                      "<code>CCFLEET_COOKIE_SECRET</code> to turn it on.</p></div>")
    return _shell("sign in", "<h1>ccfleet</h1><div class=\"card\">"
                  "<p>Sign in to see the slots you hold.</p>"
                  "<p><a class=\"btn\" href=\"/auth/google/start?next=/account\">"
                  "Continue with Google</a></p>"
                  "<p class=\"muted\">We ask Google for your email address and "
                  "nothing else.</p></div>")


def _refresh(held: list[Mapping[str, Any]], logins: Mapping[str, Mapping[str, Any]]) -> str:
    """Come back soon while something is moving; never while a code is being typed."""
    states = {(login or {}).get("state") for login in logins.values()}
    if "url_ready" in states:
        return ""
    moving = any(s["state"] in (slotstates.CLAIMING, slotstates.RELEASING) for s in held)
    if moving or states & {"requested", "code_sent"}:
        return f'<meta http-equiv="refresh" content="{ACTIVE_REFRESH_S}">'
    return f'<meta http-equiv="refresh" content="{IDLE_REFRESH_S}">'


def _report_for(slot: Mapping[str, Any], heartbeat: Optional[Mapping[str, Any]]
                ) -> Mapping[str, Any]:
    """What the slot's machine last said about this slot, or nothing."""
    payload = (heartbeat or {}).get("payload") or {}
    for entry in payload.get("slots") or []:
        if isinstance(entry, Mapping) and entry.get("unix_user") == slot["unix_user"]:
            return entry
    return {}


def _slot_card(slot: Mapping[str, Any], node: Mapping[str, Any],
               heartbeat: Optional[Mapping[str, Any]], login: Mapping[str, Any],
               csrf: str, cfg: Config, now: float) -> str:
    report = _report_for(slot, heartbeat)
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
    parts = [
        f'<div class="card slot" id="slot-{escape(slot["id"])}">'
        f'<h2>{escape(slot["id"])} <span class="pill {tone}">{escape(title)}</span></h2>',
        f'<p class="muted small">on {escape(slot["node_id"])}{region}{claimed} · machine '
        f"{machine}</p>",
    ]
    if detail:
        parts.append(f"<p>{escape(detail)}</p>")
    if slot["state"] == slotstates.ACTIVE:
        parts.append(_in_use(report))
    if slot["state"] in CAN_SIGN_IN:
        parts.append(_sign_in(slot, report, login, csrf))
        parts.append(_tokens(slot, login, csrf, now))
    if slot["state"] in slotstates.RELEASABLE:
        parts.append(_release(slot, csrf))
    parts.append("</div>")
    return "".join(parts)


def _in_use(report: Mapping[str, Any]) -> str:
    """Signed in: the plan, a way in, and how much of each window is left."""
    creds = report.get("credentials") or {}
    remote = (report.get("remote_control") or {}).get("state")
    plan = creds.get("subscription_type")
    lines = [f"<p>Signed in{(' · ' + escape(str(plan)) + ' plan') if plan else ''}.</p>"]
    if remote == "active":
        lines.append('<p>Remote Control is on: open <a href="https://claude.ai/code" '
                     'target="_blank" rel="noopener noreferrer">claude.ai/code</a> or the '
                     "Claude app, signed in as the same account, and pick this machine.</p>")
    else:
        lines.append('<p class="muted">Remote Control is starting; it comes on within a '
                     "minute of signing in.</p>")
    quota = report.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    bars = (_meter(session.get("used_pct"), "5-hour session", session.get("resets"))
            + _meter(week.get("used_pct"), "This week", week.get("resets")))
    usage = report.get("usage") or {}
    spent = ""
    if usage:
        # The same week, by the hour, that the console draws — and the same
        # care to say that these windows are the account's, and the tokens
        # only what ran on this slot.
        chart, caption = _usage_chart(usage)
        spent = (f'<p class="small"><b>{escape(_human_tokens(usage.get("total_tokens") or 0))}'
                 f"</b> tokens on this slot, last {escape(_usage_span(usage))}</p>"
                 f"{chart}{caption}")
    if bars:
        bars = ('<div class="usage-nums muted">your Claude account &middot; every device'
                "</div>" + bars)
    if bars or spent:
        lines.append(f'<div class="usage">{bars}{spent}</div>')
    return "".join(lines)


def _sign_in(slot: Mapping[str, Any], report: Mapping[str, Any],
             login: Mapping[str, Any], csrf: str) -> str:
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
                + "</div></div>")
    body = f'<span class="login-say">{escape(LOGIN_WORDS.get(state, state))}</span>'
    url = login.get("url") or ""
    # Checked again here, not only when it was stored: a link on this page must
    # never be anything but a sign-in on Anthropic's own hosts.
    if is_login_url(url) and state in ("url_ready", "code_sent"):
        body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                 f'rel="noopener noreferrer">{escape(url)}</a>')
    if state == "url_ready":
        body += _form(f"{base}/code", csrf, "Send code",
                      '<input type="text" name="code" placeholder="paste the code" '
                      'autocomplete="off" required>')
    body += " " + _form(f"{base}/cancel", csrf, "Cancel", cls="danger")
    return f'<div class="row-line stacked"><div class="row-name">Claude</div>{body}</div>'


def _tokens(slot: Mapping[str, Any], login: Mapping[str, Any], csrf: str, now: float) -> str:
    base = f"/account/slots/{escape(slot['id'])}"
    state = login.get("state") if login.get("kind") == "token" else None
    if not state:
        if login.get("state"):
            return ""                     # a sign-in is in flight; one thing at a time
        issued = slot.get("device_token_at") or 0
        said = (f'<span class="pill ok">last issued {escape(_age(now, issued))} ago</span> '
                if issued else '<span class="muted small">for your own laptop or desktop</span> ')
        return ('<div class="row-line"><div class="row-name">Device token</div>'
                f'<div class="actions">{said}'
                + _form(f"{base}/token", csrf, "Get another" if issued else "Get a device token")
                + "</div></div>")
    if state == "ready":
        body = (f'<span class="pill ok">{escape(TOKEN_WORDS["ready"])}</span> '
                + _form(f"{base}/token-show", csrf, "Show it", cls="primary") + " "
                + _form(f"{base}/token-done", csrf, "Done with it"))
        return f'<div class="row-line"><div class="row-name">Device token</div>{body}</div>'
    body = f'<span class="login-say">{escape(TOKEN_WORDS.get(state, state))}</span>'
    url = login.get("url") or ""
    if is_login_url(url) and state in ("url_ready", "code_sent"):
        body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                 f'rel="noopener noreferrer">{escape(url)}</a>')
    if state == "url_ready":
        body += _form(f"{base}/code", csrf, "Send code",
                      '<input type="text" name="code" placeholder="paste the code" '
                      'autocomplete="off" required>')
    body += " " + _form(f"{base}/cancel", csrf, "Cancel", cls="danger")
    return f'<div class="row-line stacked"><div class="row-name">Device token</div>{body}</div>'


def _release(slot: Mapping[str, Any], csrf: str) -> str:
    return ('<div class="row-line"><div class="row-name">Give it back</div>'
            + _form(f"/account/slots/{escape(slot['id'])}/release", csrf, "Give this slot back",
                    '<label class="check"><input type="checkbox" name="confirm" value="wipe" '
                    'required> Delete everything on it: files, sessions and the Claude '
                    "sign-in. Your Claude account itself is untouched.</label>", "danger")
            + "</div>")


def console_door(account: Optional[Mapping[str, Any]], session_id: str, cfg: Config) -> str:
    """The console's front door for somebody not signed in as an operator.

    Operators sign in the way everybody else does, with Google; an account
    only reaches the console once the operator has made it an admin from the
    server's own command line. The admin token stays one link away, for when
    Google is the thing that is down.
    """
    if account is None:
        body = ("<p>Operators sign in with their Google account.</p>"
                "<p><a class=\"btn\" href=\"/auth/google/start?next=/\">Continue with "
                "Google</a></p>")
    else:
        body = (f"<p>Signed in as <strong>{escape(str(account.get('email', '')))}</strong>, "
                "which is not an operator account.</p>"
                "<p class=\"muted\">An operator makes one on the server with "
                "<code>ccfleetd account role &lt;email&gt; admin</code>.</p>"
                + _form("/auth/signout", csrf_for(session_id, cfg.cookie_secret), "Sign out"))
    return _shell("console", "<h1>ccfleet console</h1><div class=\"card\">" + body
                  + "<p class=\"note\">Or <a href=\"/auth/basic\">use the admin token</a> "
                  "&mdash; the way in when Google sign-in is unavailable.</p></div>")


def token_page(slot: Mapping[str, Any], token: str) -> str:
    """The minted token, for as long as its request lasts."""
    if not token:
        return _shell("device token", "<h1>Nothing to show</h1><div class=\"card\">"
                      "<p>No token is waiting on this slot. Either you were done with it, or "
                      "the request expired. Start a new one from your slots.</p>"
                      "<p><a class=\"back\" href=\"/account\">&larr; your slots</a></p></div>")
    return _shell("device token", (
        f"<h1>Device token</h1><p class=\"sub\">Minted on <strong>{escape(slot['id'])}"
        "</strong>, for the Claude account you approved. Good for one year.</p>"
        "<div class=\"ok-banner\">You can come back and show this again while the request "
        "lasts. Press <strong>Done with it</strong> on your slots page when you have "
        "finished, or leave it and it expires on its own.</div>"
        f"<pre>{escape(token)}</pre>"
        "<div class=\"card\"><h2>Put it on a machine</h2>"
        "<pre>bash -c \"$(curl -fsSL https://raw.githubusercontent.com/cdcupt/"
        "ccfleet/main/laptop/ccfleet-connect.sh)\"</pre>"
        "<p class=\"muted\">It asks for the token and hides what you paste. Then "
        "<code>claude</code> runs there with no login. Scope is inference only, which is "
        "Anthropic's limit on long-lived tokens; revoke it from your Claude account.</p></div>"
        "<p><a class=\"back\" href=\"/account\">&larr; your slots</a></p>"))
