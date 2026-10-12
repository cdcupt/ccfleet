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
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any, Optional

from . import (
    claude_versions,
    cli_access,
    client_status,
    compatibility,
    gateway_ui,
    names,
    oauth,
    payments,
    plans,
    resets,
    status,
)
from . import slots as slotstates
from .bilingual import CONTROLS, SCRIPT_TAG, bi, label
from .bilingual import CSS as BILINGUAL_CSS
from .config import Config
from .credential_health import (
    ACCESS_MARGIN_S,
    NATIVE_LOGIN_FAILURES,
    RENEWAL_WINDOW_S,
    TRANSITION_REASONS,
    access_expiry,
    fresh_observation,
    instant,
    recovery_guidance,
    renewal_report,
    renewal_warning,
    sign_in_reason,
)
from .desired import is_login_url
from .heartbeat import relay_report
from .monitor import LOGIN_MAX_AGE_S
from .render import (
    CONSOLE_PATH,
    CSS,
    FAVICON,
    LOCAL_TIMES_TAG,
    LOGIN_WORDS,
    MARK,
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
    "cli-revoked": ("ok", "That computer can no longer connect to this slot."),
    "code": ("ok", "Code sent to your slot."),
    "cancelled": ("ok", "Cancelled."),
    "not-now": ("warn", "Your slot cannot do that right now. It may still be setting up, "
                        "or being given back."),
    "updating": ("ok", "Updating Claude Code on your slot. Sessions already open keep "
                       "running; new ones start on the new version."),
    "stable": ("ok", "Back to the stable release. Your slot moves there at its next quiet "
                     "moment."),
    "renamed": ("ok", "Renamed. Your slot answers to the new name within a minute or two; "
                      "paired computers keep working."),
    "name-bad": ("warn", "A name is 2 to 30 letters, digits and inner hyphens, and not "
                         "pool- or slot- followed by a number."),
    "name-taken": ("warn", "That name is taken. Pick another."),
    "reading": ("ok", "Reading your usage now. It shows here within a minute or two."),
    "emails-on": ("ok", "Outage emails on: we email you when your slot's machine has been "
                        "down for five minutes, and again when it is back."),
    "emails-off": ("ok", "Outage emails off."),
    **gateway_ui.NOTES,
}

NOTES_ZH = {
    "claimed": (
        '槽位正在准备：先创建独立账号，再安装 Claude Code。'
        '通常需要几分钟，页面会自动更新。'
    ),
    "no-slot": "目前没有空闲槽位。请稍后再试，或联系运营方。",
    "no-allowance": "你已领取额度内的全部槽位。",
    "released": "槽位已归还，正在删除其中的所有内容。",
    "confirm": "请先勾选确认框：归还槽位会删除其中的所有内容。",
    "signin": "正在槽位上启动 Claude 登录…", "switch": "正在槽位上启动账号更换…",
    "cli-revoked": "这台电脑已无法通过原配对连接该槽位。", "code": "授权码已发往槽位。",
    "cancelled": "已取消。", "not-now": "槽位目前无法执行此操作，可能仍在准备或清空中。",
    "updating": (
        '正在更新槽位上的 Claude Code。'
        '已打开的会话继续使用原版本，新会话使用更新后的版本。'
    ),
    "stable": "已选择稳定版，槽位会在下一个空闲时机切换。",
    "renamed": "名称已修改，一两分钟内生效；已配对的电脑仍可连接。",
    "name-bad": (
        '名称需为 2–30 个英文字母、数字或中间连字符，'
        '且不能使用 pool-数字 或 slot-数字。'
    ),
    "name-taken": "名称已被使用，请换一个。", "reading": "正在读取用量，一两分钟内会显示。",
    "emails-on": (
        '已开启故障邮件：机器离线五分钟后通知，'
        '恢复时再次通知。'
    ), "emails-off": "已关闭故障邮件。",
    "gateway-updated": "接入设置已更新，新请求使用新的槽位选择。",
    "gateway-revoked": "接入密钥已撤销，无法再用它发起请求。",
    "gateway-invalid": (
        '请选择至少一个自己的槽位，使用不重复的优先级，'
        '并填写 1–60 字符的名称。'
    ),
}

STATE_ZH = {
    slotstates.CLAIMING: ("准备中", "正在创建独立账号并安装 Claude Code，通常需要几分钟。"),
    slotstates.CLAIMED: ("等待登录", "在下方登录你自己的 Claude 账号，即可开始使用。"),
    slotstates.ACTIVE: ("使用中", ""),
    slotstates.RELEASING: ("清空中", (
        '正在删除槽位中的所有内容；机器确认清空后，'
        '槽位额度才会释放。'
    )),
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
                           (
                               'Everything on it is being deleted. It stops counting '
                               'against your '
                               'allowance once the machine confirms it is gone.'
                           )),
}

#: What the two windows count, said wherever they are shown beside a slot's own tokens.
ACCOUNT_WIDE = ("These count everything this Claude account does, wherever you use it.")

SLOT_ACTIONS = ("release", "signin", "switch", "code", "cancel", "cli", "cli-revoke",
                "update", "stable", "rename", "quota")
# What a slot can do, by state. Sign-in and CLI pairing need the account to exist
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
        # Only authored menu labels are localized; account data remains escaped
        # by user_menu, and sign-out remains the same CSRF-protected POST.
        menu = user_menu(self.account, self.csrf, operator=self.operator,
                         slots_href=self.slots_href, console_href=self.console_href,
                         slots_held=self.slots_held)
        for en, zh in (("Your slots", "我的槽位"), ("Console", "管理后台"),
                       ("operator", "管理员"), ("Signed in as", "当前账号"),
                       ("Sign out", "退出登录")):
            menu = menu.replace(f">{en}</", f">{label(en, zh, inline=True)}</")
        email = str(self.account.get("email") or "")
        old_label = "Account menu for " + email
        menu = menu.replace('aria-label="' + escape(old_label) + '"',
                            'aria-label="' + escape(old_label) + '" '
                            'data-aria-label-en="' + escape(old_label) + '" '
                            'data-aria-label-zh="' + escape("账号菜单：" + email) + '"')
        return menu.replace('aria-label="Your account"',
                            'aria-label="Your account" data-aria-label-en="Your account" '
                            'data-aria-label-zh="你的账号"')


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
    body = (f'<div class="card door">{MARK}<h1>Not found</h1>'
            '<p>There is nothing here.</p>'
            '<p><a class="back" href="/account">&larr; your slots</a></p></div>')
    return Outcome(404, body=_shell("Not found", _blocks(body, [
        "页面不存在", "没有找到这个页面。", (
            '<a class="back" href="/account">&larr; 我的槽位</a>'
        ),
    ]), viewer=viewer))


# -- actions ---------------------------------------------------------------------

def act(store: Store, cfg: Config, account: Mapping[str, Any], path: str,
        form: Mapping[str, str], now: float, session_id: str = "") -> Outcome:
    """Do what a form on this page asked, for this account and nobody else."""
    viewer = viewer_for(account, session_id, cfg, store)
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[:2] == ["account", "gateway"]:
        return gateway_ui.act(store, cfg, account, path, form, now, session_id)
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
        if action == "cli":
            token = store.request_cli_pairing(slot_id, holder, now=now)
            return Outcome(200, body=cli_pairing_page(slot, token, viewer))
        if action == "cli-revoke":
            store.revoke_cli_device(slot_id, form.get("device_id", ""), held_by=holder)
            return _back("cli-revoked", anchor)
        if action == "code":
            store.submit_slot_login_code(slot_id, form.get("code", ""), now, held_by=holder)
            return _back("code", anchor)
        if action == "update":
            # The number it is going to, as the page showed it, so the row can
            # say "Updating to 2.1.281…" while the machine works.
            latest = claude_versions.channel_version(store.get_channel_versions(), "latest")
            store.request_claude_update(slot_id, now, to_version=latest or "", held_by=holder)
            return _back("updating", anchor)
        if action == "stable":
            store.choose_stable(slot_id, held_by=holder)
            return _back("stable", anchor)
        # Cancel: whichever Claude sign-in flow is in flight on this slot ends here.
        store.clear_slot_login(slot_id, held_by=holder)
        return _back("cancelled", anchor)
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
.site main [id]{scroll-margin-top:144px}
@media(max-width:760px){.site main [id]{scroll-margin-top:164px}}
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
/* A small state light used beside a short connection sentence. */
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
.ways{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;margin:12px 0 2px}
.way{padding:14px;border:1px solid var(--rule-soft);border-radius:11px;background:var(--inset)}
.way b{display:block;margin-bottom:5px}
.way p{margin:0;color:var(--muted);font-size:13px;line-height:1.45}
.cli-access .device{display:flex;align-items:center;justify-content:space-between;gap:12px;
padding:10px 0;border-bottom:1px solid var(--rule-soft)}
.cli-access .device span{min-width:0}.cli-access .device b,.cli-access .device small{display:block}
.cli-access .device small{color:var(--muted);overflow-wrap:anywhere;margin-top:2px}
/* Claude Code on the slot: the version it runs, and one press to the newest. */
.cc-say{font-size:14px;overflow-wrap:anywhere}
.cc-say b{font-family:var(--mono);font-size:13.5px;font-weight:650}
.cc-say .sep{color:var(--muted);margin:0 2px}
.cc-say.bad-text{font-weight:600}
.row-line.cc .cc-note{flex-basis:100%;margin:-2px 0 0;font-size:13px;color:var(--muted)}
button.quiet{font-size:12.5px;padding:6px 11px;font-weight:600}
.site .page>p.note{margin-top:26px;padding:14px 18px;border:1px solid var(--rule);
border-radius:12px;background:var(--panel)}

/* Two languages stay together in prose; control labels remain compact. */
.site p>.bi-inline>.copy-en,.site p>.bi-inline>.copy-zh,
.site li>.bi-inline>.copy-en,.site li>.bi-inline>.copy-zh{display:block}
.site p>.bi-inline>.copy-zh::before,.site li>.bi-inline>.copy-zh::before{content:none!important}
.site p>.bi-inline>.copy-en+.copy-zh,.site li>.bi-inline>.copy-en+.copy-zh{margin-top:7px}
.site .page.doc{max-width:900px}
.site .page.doc .card{padding:24px 28px}
.site .page.doc .card p{line-height:1.75;overflow-wrap:anywhere}
.site .page.doc .card li{margin:14px 0;line-height:1.7}
.site .allowance h2{font-size:13px;letter-spacing:0;text-transform:none}
.site .row-name{font-weight:650}
.site .row-name .bi-inline>.copy-en,.site .row-name .bi-inline>.copy-zh{display:block}
.site .row-name .bi-inline>.copy-zh::before{content:none!important}
.site .row-name .copy-zh{font-size:12px;color:var(--muted);font-weight:500}
.site .topbar-end{display:flex;align-items:center;gap:12px;min-width:0;flex-wrap:wrap}
.site .relay-usage{margin:16px 0;padding:14px 16px;background:var(--inset);
border:1px solid var(--rule-soft);border-radius:12px}
.site .relay-usage>summary{font-weight:650;cursor:pointer}
.site .relay-usage p{line-height:1.65}
.site .row-line.release{border-top:1px solid var(--rule-soft);margin-top:8px;padding-block:20px}
.site .row-line.release .check{line-height:1.65}
.site .row-line.release button{white-space:normal}
.site input[type=text],.site input[type=email]{min-width:0;max-width:100%}
@media(max-width:760px){.site .topbar-end{margin-left:auto;gap:8px}
.site .page.doc .card{padding:20px 18px}
.site .doc-nav{flex-wrap:wrap;overflow:visible;margin:0;gap:3px}
.site .doc-nav a{padding:6px 8px;white-space:normal}
.site .row-line{gap:8px;min-width:0}.site .actions{min-width:0;max-width:100%;flex-wrap:wrap}
.site .row-line form.inline{max-width:100%;flex-wrap:wrap}
.site .row-line form.inline input{flex:1 1 170px}
.site .row-line .actions .cc-say{max-width:100%}}

/* The doors: signing in, the console's, and a page that is not there. */
.door{max-width:520px;margin:24px auto 0;padding:28px 30px 24px}
.door .mark{width:44px;height:44px}
.door h1{margin:18px 0 10px}
.door .btn.big{width:100%;margin:8px 0 4px}
.door-alt{max-width:520px;margin:16px auto 0}

/* A pairing code: selected whole with one click. */
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
.ways{grid-template-columns:minmax(0,1fr)}
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
    nav_zh = {"Overview": "概览", "Guide": "使用指南", "How it works": "工作原理",
              "Privacy": "隐私", "Terms": "条款"}
    links = "".join(f'<a href="{path}"{_HERE if path == here else ""}>'
                    f'{label(name, nav_zh[name], inline=True)}</a>' for path, name in NAV)
    corner = (viewer.menu() if viewer is not None else "" if door else
              '<a class="btn primary" href="/account">'
              + label("Sign in", "登录", inline=True) + '</a>')
    title_text = {"sign in": "Sign in", "your slots": "Your slots", "privacy": "Privacy",
                  "status": "Service status", "console": "Console",
                  "connect a computer": "Connect a computer"}.get(title, title)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"{refresh}<title>CC Fleet · {escape(title_text)}</title>"
            f'<link rel="icon" href="{FAVICON}">'
            + (f'<link rel="canonical" href="{escape(canonical)}">' if canonical else "")
            + (
                f"<style>{USER_CSS}{BILINGUAL_CSS}{extra_css}</style></head"
                f">"
                '<body class="site" data-language="en"><header '
                'class="topbar"><div class="topbar-in">'
                f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
                f'<nav class="doc-nav">{links}</nav>'
                f'<div class="topbar-end">{CONTROLS}{corner}</div></div></h'
                f'eader>'
                f'<main class="page {escape(width)}">{body}</main>'
                '<footer class="sitefoot"><div class="sitefoot-in"><div>'
                f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
                "<p>"
            ) + label("Local Claude Code. Your accounts, your selected slots.",
                          (
                              'Claude Code 在本机运行，使用你选择的账号槽位。'
                          ), inline=True) + (

                                  "</p></div>"
                                  '<nav aria-label="More" data-aria-label-en="More" '
                                  'data-aria-label-zh="更多">'

                          )
            + ''.join(f'<a href="{path}">{label(en, zh, inline=True)}</a>'
                      for path, en, zh in (("/account", "Your slots", "我的槽位"),
                                           ("/docs/guide", "Guide", "使用指南"),
                                           ("/docs/how-it-works", "How it works", "工作原理"),
                                           ("/privacy", "Privacy", "隐私"),
                                           ("/docs/terms", "Terms", "条款"),
                                           ("/status", "Status", "服务状态")))
            + '<a href="https://github.com/cdcupt/ccfleet" target="_blank" '
            'rel="noopener noreferrer">' + label("Source code", "源代码", inline=True)
            + '</a></nav></div></footer>' + LOCAL_TIMES_TAG + SCRIPT_TAG + '</body></html>')


_HERE = ' class="here"'


FORM_ZH = {
    "Claim a slot": "领取槽位", "Sign in again": "重新登录", "Sign in to Claude": "登录 Claude",
    "Change account": "更换账号", "Send code": "提交授权码", "Cancel": "取消",
    "Cancel setup": "取消设置", "Back to Stable": "切回稳定版", "Refresh": "刷新用量",
    "Rename": "重命名", "Give this slot back": "归还并清空槽位", "Remove": "移除电脑",
    "Connect another computer": "连接另一台电脑", "Connect this computer": "连接电脑",
    "Turn on": "开启", "Turn off": "关闭", "Sign out": "退出登录",
    "Update to the latest release": "更新到最新版",
}


def _form(action: str, csrf: str, words: str, inner: str = "", cls: str = "") -> str:
    zh = FORM_ZH.get(words)
    if words.startswith("Update to ") and zh is None:
        zh = "更新到 " + words[len("Update to "): ]
    button = label(words, zh, inline=True) if zh is not None else escape(words)
    return (f'<form class="inline" method="post" action="{escape(action)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
            f'<button class="{cls}" type="submit">{button}</button></form>')


def _blocks(source: str, chinese: list[str]) -> str:
    """Pair authored paragraph/heading/list copy without changing its markup.

    Each caller supplies the Chinese blocks in the same order as its fixed
    template. Dynamic fields in either language are escaped by the caller.
    Forms, URLs, attributes, code blocks and timers are left untouched.
    A count mismatch is a template error, covered by renderer tests.
    """
    pattern = r"(<(h[123]|p|li|summary)\b[^>]*>)(.*?)(</\2>)"
    matches = list(re.finditer(pattern, source, flags=re.S))
    if len(matches) != len(chinese):
        raise ValueError("bilingual template has a different number of copy blocks")
    translations = iter(chinese)
    return re.sub(pattern, lambda m: m[1] + bi(m[3], next(translations), inline=True)
                  + m[4], source, flags=re.S)


def _span_zh(seconds: int) -> str:
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size} {unit}"
    return f"{seconds} 秒"


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
    cli_devices = {s["id"]: store.list_cli_devices(s["id"], held_by=account["id"])
                   for s in held}
    csrf = csrf_for(session_id, cfg.cookie_secret)
    quota = int(account.get("slot_quota") or 0)
    counted = sum(1 for s in held if s["state"] in slotstates.HELD)

    tone, words = NOTES.get(note, ("", ""))
    banner = ((
        f'<div class="note-banner '
        f'{tone}">{label(words, NOTES_ZH.get(note, words))}</div>'
    )
              if words else "")
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
    allowance = _blocks(allowance, [
        "你还没有槽位。",
        '槽位由运营方分配。获得额度后可在这里领取。<a href="/docs">了解如何购买</a>。',
    ] if quota == 0 else [(
        f"可持有 <strong>{quota}</strong> 个槽位，已领取 <strong>{counted}</s"
        f"trong> 个。"
    )])
    allowance += _paid(payments.paid_through(store.list_payments(account["id"])), now)
    # The operator's alerts about the rule, said to the holder without naming
    # the other place: it may be somebody else's.
    flagged = {s["id"]: _flags(s, store.open_alerts(s["node_id"])) for s in held}
    # Each slot's machine, in the words of the status page: a shared machine
    # reports every minute, somebody's own node every five.
    listening = status.listening_for(store)
    lines = {s["id"]: _machine_line(status.machine_state(
        latest.get(s["node_id"]), store.open_alerts(s["node_id"]),
        s.get("kind") != slotstates.OWNER_SLOT, now, listening(s["node_id"])), now)
        for s in held}
    since = store.listening_since()
    silent = {a["node_id"] for a in store.open_alerts() if a["rule"] == "no_heartbeat"}
    cards = "".join(_slot_card(s, nodes.get(s["node_id"]) or {}, latest.get(s["node_id"]),
                               logins[s["id"]], csrf, cfg, now, flagged[s["id"]],
                               updates[s["id"]], channels, cli_devices[s["id"]],
                               machine_line=lines[s["id"]],
                               listening=None if s["node_id"] in silent else since)
                    for s in held)
    body = (
        '<div class="pagehead"><h1>' + label('Your slots', '我的槽位', inline=True) + '</h1>'
        '<p class="sub">' + label('Signed in as', '当前账号', inline=True)
        + f' <strong>{escape(str(account.get("email", "")))}'
        "</strong></p></div>"
        + banner
        + (
            '<div class="card allowance"><div><h2>'
        ) + label('Your allowance', '槽位额度', inline=True)
        + f'</h2>{allowance}</div>'
        f"{claim}</div>"
        + gateway_ui.panel(store, cfg, account, csrf, held)
        + (f'<div class="slots">{cards}</div>' if cards else "")
        + (_outage_emails(account, csrf) if cfg.emails_ready else "")
        + (
            '<p class="note">Your slot is a Linux account on a '
            'machine we operate, with its own '
            'home, its own Claude Code and your own Claude sign-in. '
            "Other people's slots on the "
            "machine cannot read yours; the machine's administrators "
            'technically can. This page '
            'never shows your files or conversations, and nothing '
            'here holds your Claude '
            'credential: it is written on the machine when you sign '
            'in, and nowhere else.</p>'
        ))
    note_start = body.index('<p class="note">')
    body = body[:note_start] + _blocks(body[note_start:], [
        (
            '槽位是在我们运营的机器上的独立 Linux 账号，'
            '拥有自己的目录、Claude Code 和 Claude 登录。'
            '普通用户的其他槽位无法读取你的文件，'
            '但机器管理员在技术上可以。本页不展示文件或对话。'
            'Claude 登录凭据只写入槽位所在机器，Fleet '
            '服务器不保存它。'
        ),
    ])
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
        return '<p class="muted">' + bi(
            f'Paid through <strong>{escape(str(through))}</strong>.',
            f'服务有效至 <strong>{escape(str(through))}</strong>。', inline=True) + '</p>'
    if state == payments.LAPSED:
        return '<p class="lapsed">' + label(
            (
                f'Your paid period ended on {through}.'
            ), f'服务已于 {through} 到期。', inline=True) + '</p>'
    return ""


def _signed_out(cfg: Config) -> str:
    if not cfg.google_ready:
        body = (f'<div class="card door">{MARK}<h1>Sign in</h1>'
                '<p>Sign-in is not set up on this server.</p>'
                '<p class="muted">An operator configures '
                '<code>CCFLEET_GOOGLE_CLIENT_ID</code>, '
                '<code>CCFLEET_GOOGLE_CLIENT_SECRET</code> and '
                '<code>CCFLEET_COOKIE_SECRET</code> to turn it on.</p></div>')
        return _shell("sign in", _blocks(body, [
            "登录", "此服务器尚未配置登录。",
            (
                '运营方需设置 <code>CCFLEET_GOOGLE_CLIENT_ID</code>、'
                '<code>CCFLEET_GOOGLE_CLIENT_SECRET</code> 和 '
                '<code>CCFLEET_COOKIE_SECRET</code>。'
            ),
        ]), door=True)
    body = ((
        f'<div class="card door">{MARK}<h1>Sign in to your slots</h'
        f'1>'
        '<p>ccfleet gives you a slot on a machine we operate: '
        'your own Linux '
        'account there, with Claude Code, signed in to your own '
        'Claude account. '
        'The operator decides who gets slots.</p>'
        '<p>Sign in to see the slots you hold.</p>'
        '<div class="sign-in-action"><a class="btn primary big" '
        'href="/auth/google/start?next=/account">'
    )
            + label("Continue with Google", "使用 Google 登录", inline=True) + '</a></div>'
            '<p class="muted small">We ask Google for your email address, whether '
            'Google has verified it, and the id it gives your account, which stays the '
            'same if the address changes. From Google we keep only the address and the '
            'id. <a href="/privacy">What else we keep, and why</a>.</p></div>'
            '<div class="door-alt"><p class="muted">New to ccfleet? Start with '
            '<a href="/docs">what it is</a> and <a href="/docs/guide">how to begin</a>.'
            '</p></div>')
    source = body
    source = _blocks(source, [
        "登录管理我的槽位",
        (
            'Fleet 槽位是在我们运营的机器上的独立 Linux 账号。'
            '每个槽位安装 Claude Code，登录你自己的 Claude 账号。'
            '槽位由运营方分配。'
        ),
        "登录后可查看并管理你持有的槽位。",
        (
            'Google 登录只请求邮箱、邮箱验证状态和固定账号 ID；'
            '更改邮箱不会改变该 ID。我们只保留邮箱和 ID。'
            '<a href="/privacy">了解其他保存的信息及用途</a>。'
        ),
        '首次使用？先读 <a href="/docs">产品概览</a> 和 <a href="/docs/guide">入门指南</a>。',
    ])
    return _shell("sign in", source, door=True)


#: When the privacy page last changed in substance. Change it with the words.
PRIVACY_UPDATED = "2026-10-11"


def _span(seconds: int) -> str:
    """"14 days", "36 hours", "15 minutes": the privacy page quotes the settings in force."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def privacy_page(cfg: Config, viewer: Optional[Viewer] = None) -> str:
    """Current service privacy: local work, HTTPS model context and slot credentials."""
    contact = (
        f'<a href="mailto:{escape(cfg.contact_email)}">{escape(cfg.contact_email)}</a>'
        if cfg.contact_email
        else "the support address Google shows on ccfleet&#x27;s sign-in screen"
    )
    contact_zh = contact if cfg.contact_email else "Google 登录页上显示的支持邮箱"
    attempt = _span(LOGIN_MAX_AGE_S)
    attempt_zh = _span_zh(LOGIN_MAX_AGE_S)

    def card(en: str, zh: str, paragraphs: list[tuple[str, str]]) -> str:
        return (
            '<div class="card"><h2>'
            + label(en, zh, inline=True)
            + "</h2>"
            + "".join(_notice(english, chinese) for english, chinese in paragraphs)
            + "</div>"
        )

    body = (
        '<div class="pagehead"><h1>'
        + label("Privacy", "隐私说明", inline=True)
        + "</h1>"
        + _notice(
            f"Last updated {escape(PRIVACY_UPDATED)}", f"最近更新：{escape(PRIVACY_UPDATED)}", "sub"
        )
        + "</div>"
    )
    body += card(
        "What ccfleet is",
        "Fleet 如何工作",
        [
            (
                (
                    "ccfleet gives you a slot on a machine we operate: "
                    "your own Linux account there, "
                    "with Claude Code, signed in to your own Claude "
                    "account. The operator decides who gets "
                    f"slots. To reach the operator, write to {contact}."
                ),
                (
                    "Fleet 槽位是在我们运营的机器上的独立 Linux 账号，"
                    "安装 Claude Code 并登录你自己的 Claude 账号。"
                    f"槽位由运营方分配。联系运营方：{contact_zh}。"
                ),
            ),
            (
                (
                    "Think of Fleet as the desk that connects your "
                    "computer to your selected account. "
                    "Your computer does the file and tool work. It sends "
                    "selected model context over HTTPS "
                    "to Fleet; Fleet chooses one of your selected slots "
                    "and uses its Claude sign-in to "
                    "request a reply from Anthropic. The reply returns "
                    "to your computer."
                ),
                (
                    "可以把 Fleet 看作接待台：电脑负责文件和工具操作，"
                    "需要模型回复时，把选定的上下文通过 HTTPS 发给 "
                    "Fleet。"
                    "Fleet 选择你指定的槽位，使用它的 Claude 登录向 "
                    "Anthropic 请求回复，再把回复送回电脑。"
                ),
            ),
        ],
    )
    body += card(
        "What we get from Google",
        "Google 登录提供什么",
        [
            (
                (
                    "When you sign in with Google we ask for your email "
                    "address, whether Google has verified "
                    "it, and the id Google gives your account. We keep "
                    "the address and the id. We do not ask "
                    "for your name, your photo, your contacts, or access "
                    "to your Gmail, Drive or anything else "
                    "in your Google account."
                ),
                (
                    "Google 提供邮箱、邮箱验证状态和固定账号 ID。"
                    "我们只保留邮箱和 ID，不请求姓名、照片、联系人，"
                    "也不获取 Gmail、Drive 等其他 Google 服务的访问权限。"
                ),
            ),
        ],
    )
    saved = [
        (
            (
                "Your account: the address and id above, whether you "
                "are an operator, how many slots you "
                "may hold, when you first signed in, when you last "
                "visited, and whether you asked for outage emails."
            ),
            (
                "Fleet 账号：邮箱和 ID、是否为管理员、槽位额度、"
                "首次登录及最近访问时间，以及是否开启故障邮件。"
            ),
        ),
        (
            (
                "The slots you hold, when you claimed each one, and "
                "when you last moved it to another "
                "Claude account, which a slot may do once a week. A "
                "new slot gets a neutral name such as "
                "slot-4821, never a name derived from your email "
                "address. You or the operator can rename "
                "it. The name also becomes the machine hostname, "
                "which Anthropic can see. These slot "
                "names and dates are deleted when you give the slot "
                "back."
            ),
            (
                "持有的槽位、领取时间及最近更换 Claude 账号的时间；"
                "每个槽位每周最多更换一次账号。新名称为 "
                "slot-4821 这样的中性名称，不从邮箱生成。"
                "你或运营方可以改名；它也会成为 Anthropic "
                "可见的主机名。"
                "归还槽位时删除这些名称和日期。"
            ),
        ),
        (
            (
                "Your sign-in here: a random value in a cookie, of "
                "which we store only a hash, with when "
                f"it began and when it ends. It lasts "
                f"{_span(cfg.session_ttl_s)}, or until you sign out."
            ),
            "Fleet 登录会话：Cookie 中是随机值，服务器只保存哈希及开始、结束时间。"
            f"有效期为 {_span_zh(cfg.session_ttl_s)}，退出登录会提前结束。",
        ),
        (
            (
                "Payments the operator has recorded for you: the "
                "amount, currency, paid-through day, "
                "the operator’s note, when and by whom it was "
                "recorded, and whether it was later voided."
            ),
            ("付款记录：金额、币种、服务到期日、运营方备注、记录时间和记录人，以及是否后来作废。"),
        ),
        (
            (
                "The slot reports its version, sign-in health, usage "
                "and fixed maintenance results so "
                "your account page can show them. These are "
                "observations, not a guarantee that Anthropic "
                "will accept the next model request."
            ),
            (
                "槽位上报版本、登录健康、用量和固定维护结果，"
                "供账号页面展示。这些是观测记录，不能保证 Anthropic "
                "一定接受下一次模型请求。"
            ),
        ),
    ]
    body += (
        '<div class="card"><h2>'
        + label(("What we keep because you use ccfleet"), "保存哪些资料", inline=True)
        + "</h2><ul>"
        + "".join("<li>" + bi(en, zh, inline=True) + "</li>" for en, zh in saved)
        + "</ul><details><summary>"
        + label("Health and usage details", "健康与用量明细", inline=True)
        + "</summary>"
        + _notice(
            (
                "Reports include the email address and plan of the "
                "one Claude account signed in "
                "on the slot, and one-way account and bound-account "
                "fingerprints. They cannot be "
                "turned back into the account ID or email. The "
                "fingerprints detect an account "
                "used on two machines or a slot signed in to another "
                "account than its own. "
                "Reports also include the plan’s rate-limit tier, "
                "when that sign-in expires, "
                "quota readings and reset times, credential-renewal "
                "state and attempt timestamps "
                "with fixed reason codes, never tokens or raw native "
                "errors. Hourly token counts "
                "for the past week are calculated on the slot; only "
                "the numbers leave it."
            ),
            (
                "报告包含该槽位所用 Claude 账号的邮箱、套餐，"
                "以及账号和绑定账号的单向指纹；指纹无法还原成账号 "
                "ID 或邮箱，用来发现同一账号用于两台机器、"
                "或槽位登录了其他账号。还包含套餐限流等级、"
                "登录到期时间、"
                "用量与重置时间、凭据续期状态、尝试时间和固定原因码，"
                "不包含凭据值或原始错误。过去一周的小时 Token "
                "数在槽位上计算，只有数字上报。"
            ),
        )
        + _notice(
            (
                "Relay measurements report request counts, completed "
                "transfers and fixed error "
                "categories over seven UTC calendar days. Connection,"
                " first-body-byte and "
                "total-request timing have sample counts, means and "
                "maxima, observation time "
                "and last completed-transfer time. Validated "
                "input/output and cache-read/cache-write "
                "token totals show their completed-response sample "
                "coverage. Missing or partial "
                "usage is unknown, not zero or a bill. These metrics "
                "contain no model names, "
                "request IDs, headers, bodies, paths, email "
                "addresses or credentials. The seven-day "
                "aggregate window is distinct from how long heartbeat snapshots "
                "are retained here. We keep"
                f" these reports for "
                f"{_span(cfg.retention_days * 86400)}."
            ),
            (
                "转发统计按七个 UTC 日汇总请求数、"
                "完成传输数和固定错误分类；连接、"
                "首个正文响应字节和总时长有样本数、"
                "均值、最大值及观测时间、最近完成时间。输入、"
                "输出及缓存 Token 总数注明完整响应样本覆盖率；"
                "缺失或部分 "
                "数据表示未知，不是零或账单。统计不含模型名、请求 "
                "ID、请求头、正文、路径、邮箱或凭据。七日汇总窗口与 "
                f"健康快照的保存期限不同；服务器保留这些报告 "
                f"{_span_zh(cfg.retention_days * 86400)}。"
            ),
        )
        + "</details>"
        + _notice(
            (
                "Your slot’s heartbeat reports do not include "
                "prompts, conversations, file "
                "contents, your Claude credential, or the name on "
                "your Claude account. This is "
                "separate from model traffic: the HTTPS gateway on "
                "BWH receives and processes "
                "the contents of model requests and responses."
            ),
            (
                "健康报告不包含提示词、对话、文件内容、Claude 凭据或 "
                "Claude 账号显示名。模型流量是另一回事："
                "BWH 上的 HTTPS 网关会接收并处理模型请求及回复内容。"
            ),
        )
        + "</div>"
    )
    body += card(
        "Your Claude account and access keys",
        "Claude 登录与访问密钥",
        [
            (
                (
                    "You sign in to Claude yourself, through Anthropic. "
                    "The credential is written only "
                    "on the machine in your slot; this server never "
                    "stores it. While a sign-in is in progress, "
                    f"its link, the code you paste and its progress are he"
                    f"ld here for at most {attempt}. "
                    "If it fails, its reason is retained for "
                    "your page, "
                    f"for at most {attempt}. Account-change results follow"
                    f" the same limit."
                ),
                (
                    "你通过 Anthropic 自行登录 Claude，"
                    "凭据只写入槽位所在机器，Fleet 服务器不保存。"
                    "登录期间，授权链接、"
                    f"你粘贴的授权码和进度最多暂存 {attempt_zh}。失败原因保"
                    f"留在你的页面中，最多 "
                    f"{attempt_zh}；更换账号的结果同样如此。"
                ),
            ),
            (
                (
                    "When you connect the Fleet CLI, its single-use "
                    "pairing code is stored only as a hash "
                    "for ten minutes. Each connected computer has its "
                    "own public SSH key and random Fleet "
                    "access token; the server stores only the token hash."
                    " A Fleet key grants Fleet access; "
                    "it is not your Claude credential."
                ),
                (
                    "Fleet CLI 的一次性配对码只保存哈希，十分钟后过期。"
                    "每台已连接电脑有自己的 SSH 公钥和随机 Fleet "
                    "访问令牌，服务器只保存令牌哈希。Fleet 密钥用于访问 "
                    "Fleet，不是 Claude 登录凭据。"
                ),
            ),
            (
                (
                    "HTTPS model access uses a separate revocable "
                    "endpoint key. The server stores its hash, "
                    "name, selected own slot IDs and current rental "
                    "bindings in priority order, routing mode, "
                    "configuration generation, creation/last-use times "
                    "and revocation time. A key selects "
                    "only slots held by its account, with one bound "
                    "Claude account per slot. Short-lived "
                    "routing hashes keep conversation affinity and "
                    "failure cooldowns separately from usage statistics."
                ),
                (
                    "HTTPS 模型访问使用另一把可撤销的接入密钥。"
                    "服务器保存哈希、名称、按优先级排列的自有槽位 ID "
                    "和当前租用绑定、"
                    "路由模式、配置版本、创建/最近使用时间及撤销时间。"
                    "只能选用所属用户持有的槽位，每个槽位绑定一个 Claude "
                    "账号。"
                    "短期路由哈希用于会话保持和失败冷却，与用量统计分开。"
                ),
            ),
            (
                (
                    "The selected slot sends supported model requests to "
                    "Anthropic using its bound account. "
                    "Native Claude on the slot owns credential writing "
                    "and renewal. The slot credential is "
                    "never returned to your computer or BWH. Anthropic’s "
                    "own terms and privacy policy apply."
                ),
                (
                    "选中的槽位只使用其绑定账号，把受支持的模型请求发给 "
                    "Anthropic。槽位上的原版 Claude 负责写入和续期凭据，"
                    "凭据不会返回电脑或 BWH。Anthropic "
                    "自身的条款和隐私政策同时适用。"
                ),
            ),
        ],
    )
    body += card(
        "Your local work and model context",
        "本地工作与模型上下文",
        [
            (
                (
                    "<code>ccfleet local</code> runs the original Claude "
                    "CLI, tools, files, settings and "
                    "conversation history on your computer. It does not "
                    "upload your whole project. File "
                    "excerpts, prompts and tool results selected as "
                    "model context are sent with model requests."
                ),
                (
                    "<code>ccfleet local</code> 在本机运行原版 Claude "
                    "CLI，文件、工具、设置和会话历史留在电脑上。"
                    "不会上传整个项目，但选作模型上下文的文件片段、"
                    "提示词和工具结果会随请求发送。"
                ),
            ),
            (
                (
                    "HTTPS encrypts traffic in transit. TLS ends at BWH, "
                    "so <strong>BWH can read model "
                    "requests and responses</strong>, including sent "
                    "prompts, tool results and file contents. "
                    "The selected slot and Anthropic also process that "
                    "model context. Your account credential "
                    "stays on the slot. Read the <a "
                    'href="/docs/library/docs/public-gateway.'
                    'html">connection guide</a> '
                    "for setup and account selection."
                ),
                (
                    "HTTPS 加密传输过程，TLS 在 BWH 终止，因此 "
                    "<strong>BWH 能读取模型请求和回复</strong>，"
                    "包括发送的提示词、工具结果和文件内容。选中的槽位和 "
                    "Anthropic 也会处理这些上下文；账号凭据仍在槽位上。"
                    '设置与账号选择见 <a href="/docs/library/docs/public-'
                    'gateway.html">连接指南</a>。'
                ),
            ),
            (
                (
                    "Native permissions govern file and command access; "
                    "the working directory is not a "
                    "sandbox. Bypass mode can modify, delete or transmit "
                    "local data without individual "
                    "approval prompts. MCP servers, hooks, plugins, "
                    "shell tools, updates and other native "
                    "services may connect directly from your computer. "
                    "Optional telemetry controls are "
                    "not an all-traffic firewall."
                ),
                (
                    "文件和命令权限由原版 Claude 管理，工作目录不是沙箱。"
                    "跳过权限确认的模式可以直接修改、删除或传输本地数据。"
                    "MCP、hooks、插件、Shell 工具、"
                    "更新及其他原版服务可能从电脑直接联网；"
                    "可选遥测设置不是覆盖所有流量的防火墙。"
                ),
            ),
            (
                (
                    "The gateway and slot relay remove selected request "
                    "headers and the top-level "
                    "<code>metadata</code> field. This filtering does "
                    "not redact arbitrary prompts, tool "
                    "results or file contents. Native prompts may "
                    "include local OS, working directory "
                    "and environment details. BWH can also see your "
                    "connection IP and timing. This is not anonymity."
                ),
                (
                    "网关和槽位转发器移除部分请求头及顶层 "
                    "<code>metadata</code> 字段，不会自动删除提示词、"
                    "工具结果或文件内容中的 "
                    "敏感信息。原版提示词可能包含操作系统、"
                    "工作目录和环境信息。BWH 还能看到连接 IP 和时间；"
                    "这不是匿名服务。"
                ),
            ),
            (
                (
                    "Native local history/settings remain in their "
                    "existing location. Setup does not "
                    "automatically erase existing files or saved history."
                    " Disconnecting or revoking a "
                    "computer does not undo writes or erase content "
                    "already received or saved in native history."
                ),
                (
                    "原版历史和设置保留在现有位置。"
                    "设置程序不会自动清除已有文件或保存的历史。"
                    "断开或撤销电脑访问，不会撤销已写入的 "
                    "内容，也不会删除已经收到或保存到原版历史中的数据。"
                ),
            ),
            (
                (
                    "Project preferences are private local settings "
                    "indexed by a directory hash. Background "
                    "jobs keep prompts, options, project paths and "
                    "bounded stdout/stderr logs in private "
                    "files on your computer; they may contain sensitive content and are "
                    "retained until deliberately removed. "
                    "Job listings hide prompt and path contents. "
                    "Doctor/status exports contain only "
                    "allowlisted health facts and fixed recovery "
                    "guidance, not job logs, credentials or "
                    "project contents. Exports are saved only to your "
                    "requested local file and never automatically uploaded to the operator."
                ),
                (
                    "项目偏好是以目录哈希索引的本地私有设置。"
                    "后台任务的提示词、选项、"
                    "项目路径及有大小限制的输出/错误日志保存在 "
                    "电脑的私有文件中，可能包含敏感内容，直到你主动删除；"
                    "任务列表不展示提示词或路径。doctor/status "
                    "导出只包含 "
                    "允许的健康字段和固定恢复建议，不包含任务日志、"
                    "凭据或项目内容；只写入你指定的本地文件，"
                    "不会自动上传。"
                ),
            ),
        ],
    )
    body += card(
        "What the operator can see",
        "运营方能看到什么",
        [
            (
                (
                    "The machines are ours, and their administrators "
                    "have root. They can technically read "
                    "any slot&#x27;s files, and its Claude credential. "
                    "The operator console does not expose "
                    "these contents, but root access can inspect or "
                    "alter model requests and responses. "
                    "Altered replies may influence local tool actions. "
                    "Use the service only if you trust "
                    "the operator and slot host."
                ),
                (
                    "机器由我们运营，管理员有 root 权限，"
                    "技术上可以读取槽位文件及 Claude 凭据。"
                    "管理后台不直接展示这些内容，"
                    "但 root 权限能够查看或修改模型请求与回复；"
                    "被修改的回复可能影响本地工具操作。使用服务前，"
                    "需要信任运营方及槽位主机。"
                ),
            ),
            (
                (
                    "The console shows your email address, allowance, "
                    "held slots, last visit and recorded payments."
                ),
                "管理后台展示邮箱、槽位额度、持有的槽位、最近访问时间及付款记录。",
            ),
        ],
    )
    body += card(
        "Cookies and language",
        "Cookie 与页面语言",
        [
            (
                (
                    "Two necessary cookies support sign-in: your session "
                    "cookie and the one that ties "
                    "Google’s reply to the browser that requested it, "
                    f"which lasts {_span(oauth.FLOW_TTL_S)}; the server ke"
                    f"eps a matching record for the same time. "
                    "There is no analytics, advertising or third-party "
                    "script. Page language is selected "
                    "by the English or Chinese URL."
                ),
                (
                    "登录使用两类必要 Cookie：Fleet 会话，以及把 Google "
                    "授权回复绑定到发起登录的浏览器的 Cookie。"
                    f"后者有效期为 {_span_zh(oauth.FLOW_TTL_S)}，服务器的对"
                    f"应记录保留相同时间。没有分析追踪、广告或第三方脚本。"
                    "英语或中文由页面地址选择。"
                ),
            ),
        ],
    )
    body += card(
        "Sharing, keeping and deleting",
        "共享、保存与删除",
        [
            (
                (
                    "We do not sell what we keep. If you choose outage "
                    "emails, your address and slot name "
                    "go to Resend for delivery only when there is an "
                    "outage or recovery to report. Servers "
                    "run at hosting companies we rent from. Google and "
                    "Anthropic handle your use of their "
                    "own services: Google sign-in and Claude."
                ),
                (
                    "我们不出售这些资料。开启故障邮件后，"
                    "需要通知故障或恢复时，邮箱和槽位名称会发送给投递服务"
                    " Resend。"
                    "服务器由我们租用的托管公司提供；Google 和 Anthropic "
                    "分别处理你在其自身服务中的登录和 Claude 使用。"
                ),
            ),
            (
                (
                    "Giving a slot back deletes its Linux account and "
                    "every file in it, not your local "
                    "files or Claude account. Your Fleet account and "
                    "recorded payments remain while that "
                    "account exists. To delete it, contact the operator: "
                    "deletion is done by hand after "
                    "all held slots have been returned and wiped. If "
                    "these practices change, we update this page and its "
                    "date."
                ),
                (
                    "归还槽位会删除其 Linux 账号和所有文件，"
                    "不删除本地文件或 Claude 账号。Fleet 账号存在期间，"
                    "账号信息和付款记录 "
                    "继续保留。要删除 Fleet 账号，请联系运营方；"
                    "待全部槽位归还并清空后，由运营方手动处理。"
                    "处理方式变化时会更新本页及日期。"
                ),
            ),
        ],
    )
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
NOT_DONE = {"token": "The connection setup did not finish",
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


def _machine_line(state: status.State, now: float) -> str:
    source = status.slot_line(state, now)
    words = {status.GREEN: "正常", status.YELLOW: "异常", status.RED: "离线"}
    since = '，始于 ' + status.since_html(state.since, now) if state.since is not None else ''
    return _blocks(source, [
        f'<span class="st-dot {state.level}" aria-hidden="true"></span>'
        f'<span>机器：{words[state.level]}{since}</span> <a href="/status">服务状态</a>',
    ])


def _slot_card(
    slot: Mapping[str, Any],
    node: Mapping[str, Any],
    heartbeat: Optional[Mapping[str, Any]],
    login: Mapping[str, Any],
    csrf: str,
    cfg: Config,
    now: float,
    flagged: frozenset[str] = frozenset(),
    update: Optional[Mapping[str, Any]] = None,
    channels: Optional[Mapping[str, Any]] = None,
    cli_devices: Optional[list[Mapping[str, Any]]] = None,
    *,
    machine_line: str = "",
    listening: Optional[float] = None,
) -> str:
    own = slot.get("kind") == slotstates.OWNER_SLOT
    report = _own_report(heartbeat) if own else _report_for(slot, heartbeat)
    # A sign-in that failed, or a change of account that finished, is
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
        machine_zh = '<span class="bad-text">尚未收到报告</span>'
    elif now - max(heard, listening or 0.0) > cfg.heartbeat_max_age_s:
        # Quiet only while the server was down is not unreachable (see
        # status.machine_state); `listening` is None for a machine silent before.
        machine = (
            f'<span class="bad-text">not heard from in {escape(_age(now, heard))}'
            "</span> &mdash; your slot may be unreachable"
        )
        machine_zh = (

                f'<span class="bad-text">已 {_age(now, heard)} 未收到报'
                f'告</span>；槽位可能无法连接'

        )
    else:
        machine = f"last heard {escape(_age(now, heard))} ago"
        machine_zh = f"最近报告：{escape(_age(now, heard))} 前"
    region = f" · {escape(node['region'])}" if node.get("region") else ""
    claimed = (
        (
            f" · claimed {escape(_age(now, slot['claimed_at']))} a"
            f"go"
        ) if slot.get("claimed_at") else ""
    )
    # The holder sees the slot by this name on every paired computer.
    name = names.display(slot)
    about = f"{region}{claimed}".lstrip(" ·")
    claimed_zh = (
        (
            f" · 领取于 {escape(_age(now, slot['claimed_at']))} 前"
        ) if slot.get("claimed_at") else ""
    )
    about_zh = f"{region}{claimed_zh}".lstrip(" ·")
    # Somebody's own node, said beside its name rather than lost in the small
    # print: it is theirs outright, not one of ours they hold.
    kind, mine = (
        (
            ' data-kind="own"',
            ' <span class="tag">'
            + label("your own machine", "你自己的机器", inline=True)
            + "</span>",
        )
        if own
        else ("", "")
    )
    signed_in_title = title == "In use"
    parts = [
        f'<div class="card slot" id="slot-{escape(slot["id"])}" '
        f'data-state="{escape(str(slot["state"]))}"{kind}><div class="slot-head">'
        f'<h2>{escape(name)} <span class="pill {tone}">'
        + label(
            title,
            ("使用中" if signed_in_title else "等待登录")
            if own
            else STATE_ZH.get(slot["state"], (title, ""))[0],
            inline=True,
        )
        + f"</span>{mine}</h2>",
        '<p class="slot-meta">'
        + bi(
            (about + " · " if about else "") + "machine " + machine,
            (about_zh + " · " if about_zh else "") + "机器：" + machine_zh,
            inline=True,
        )
        + "</p>"
        '</div><div class="slot-body">',
    ]
    # Its machine's state, as the status page says it (status.slot_line).
    if machine_line:
        parts.append(machine_line)
    if detail:
        detail_zh = (
            "在下方登录你自己的 Claude 账号。"
            if own
            else STATE_ZH.get(slot["state"], ("", detail))[1]
        )
        parts.append("<p>" + label(detail, detail_zh, inline=True) + "</p>")
    if not own and slot["state"] in CAN_SIGN_IN:
        parts.append(_account_health(slot, report, login, heard, cfg, now, listening))
    if slot["state"] in (slotstates.CLAIMING, slotstates.RELEASING):
        # The machine is working on it. The sentence above says what; this
        # says it is still moving, which a still page otherwise cannot.
        parts.append('<div class="progress" aria-hidden="true"><i></i></div>')
    signed_in = (report.get("credentials") or {}).get("logged_in") is True
    if signed_in if own else slot["state"] == slotstates.ACTIVE:
        parts.append(
            _in_use(
                report,
                now,
                "" if own else _quota_refresh(slot, report, csrf, now),
                fresh=fresh_observation(
                    report.get("credentials"), heard, now, cfg.heartbeat_max_age_s, listening
                ),
            )
        )
        if not own:
            since = max(slot.get("claimed_at") or 0, slot.get("account_switched_at") or 0)
            parts.append(
                _relay_usage(report.get("relay"), heard, now, cfg.heartbeat_max_age_s, since=since)
            )
            parts.append(_ways_to_use(report))
    if slot["state"] in CAN_SIGN_IN:
        for rule, words in (("account_elsewhere", ELSEWHERE), ("account_changed", CHANGED)):
            if rule in flagged:
                chinese = (
                    (
                        "此 Claude 账号还登录在 Fleet 的另一台机器上。"
                        "同一账号只能用于一台机器，请退出其中一台。"
                    )
                    if rule == "account_elsewhere"
                    else (
                        "槽位当前登录的账号与原绑定不一致。请重新登录原账号，"
                        "或用“更换账号”更新绑定。"
                    )
                )
                parts.append('<p class="lapsed">' + label(words, chinese, inline=True) + "</p>")
    if ended and slot["state"] in CAN_SIGN_IN:
        parts.append(_ended(ended))
    if slot["state"] in CAN_SIGN_IN:
        parts.append(_sign_in(slot, report, login, csrf, now))
        parts.append(_claude_row(slot, node, report, update or {}, channels or {}, csrf))
        if not own:
            parts.append(_cli_access(slot, node, cli_devices or [], csrf, now))
    if slot["state"] in CAN_SIGN_IN and not own:
        parts.append(_rename(slot, csrf))
    # Never on somebody's own node: giving back means wiping, and nothing
    # there is ours to wipe.
    if slot["state"] in slotstates.RELEASABLE and not own:
        parts.append(_release(slot, csrf))
    parts.append("</div></div>")
    return "".join(parts)


def _guidance_zh(credentials: Mapping[str, Any], now: float) -> str:
    translations = {
        "A fresh Claude sign-in is required; automatic renewal cannot recover this login.":
            "需要重新登录 Claude，自动续期无法恢复此登录。",
        (
            'Native maintenance configuration needs operator review. '
            'Your computer pairing and local history are kept.'
        ):
            "原版客户端的维护配置需要运营方检查；电脑配对和本地历史保留。",
        "A temporary native renewal failure is being retried automatically.":
            "原版凭据续期暂时失败，正在自动重试。",
        "Native renewal is not yet confirmed; maintenance will retry automatically.":
            "原版凭据续期尚未确认，维护程序会自动重试。",
        (
            'Access renewal has not been verified. Access expiry '
            'alone does not mean a revoked login.'
        ):
            "尚未确认访问凭据续期；访问凭据过期本身不代表登录已被撤销。",
    }
    words = recovery_guidance(credentials, now)
    return translations.get(words, words)


COMPATIBILITY_ZH = {
    "passed": "更新检查通过", "pending": "等待更新检查",
    "failed": "更新检查需要处理", "blocked": "更新检查等待中",
}
COMPATIBILITY_DETAILS_ZH = {
    "native_unavailable": "原版客户端不可用，请联系运营方。",
    "native_version_unknown": "尚未确认已安装的客户端版本。",
    "native_version_changed": "安装版本已变化，将重新验证。",
    "native_timeout": "客户端检查超时，维护程序会重试。",
    "native_interface_changed": "新版客户端接口需要运营方检查。",
    "native_auth_source": "原版认证设置与槽位登录不符，请联系运营方。",
    "native_extensions": "托管的原版扩展需要运营方检查，才能继续维护验证。",
    "usage_pending": "等待新的原版用量检查。", "usage_unavailable": (
        '用量验证未确认，维护程序会重试。'
    ),
    "relay_unavailable": "已安装的转发器需要运营方检查。",
    "relay_protocol_mismatch": "转发协议验证失败，请联系运营方。",
    "relay_tls_policy": "加密传输验证失败，请联系运营方。",
    "account_unbound": "等待槽位账号绑定。", "account_transition": "等待账号维护完成。",
    "sign_in_pending": "等待登录完成。", "credential_unavailable": "等待可用的登录凭据。",
    "probe_busy": (
        '其他维护检查正在执行，稍后会重试。'
    ), "check_interrupted": "验证被中断，维护程序会重试。",
}


def _notice(english: str, chinese: str, cls: str = "") -> str:
    opening = '<p' + (f' class="{cls}"' if cls else '') + '>'
    return opening + bi(english, chinese, inline=True) + '</p>'


def _account_health(
    slot: Mapping[str, Any],
    report: Mapping[str, Any],
    login: Mapping[str, Any],
    heard: Any,
    cfg: Config,
    now: float,
    listening: Optional[float] = None,
) -> str:
    """Only fixed health codes become copy; never echo a diagnostic payload."""
    observation = client_status.health(
        slot,
        report,
        login,
        heard=heard,
        now=now,
        max_age=cfg.heartbeat_max_age_s,
        listening_since=listening,
    )
    words = {
        "ready": ("ok", "Reported ready", (
            'The latest slot report shows current credentials.'
        )),
        "renewal_pending": (
            "warn",
            "Renewal pending",
            recovery_guidance(report.get("credentials") or {}, now),
        ),
        "sign_in_required": (
            "warn",
            "Sign-in required",
            "Use the Claude sign-in controls below. Your computer pairing "
            "and saved local conversations are kept.",
        ),
        "switching": (
            "busy",
            "Account maintenance",
            ("Wait for the current sign-in or account change to finish before retrying."),
        ),
        "degraded": (
            "warn",
            "Readiness unverified",
            "Fresh account readiness is not established. Run ccfleet doctor or "
            "contact your operator if this persists.",
        ),
    }
    code = observation.get("health")
    if (
        not isinstance(code, str)
        or code not in words
        or (code == "ready" and slot.get("state") != slotstates.ACTIVE)
    ):
        code = "degraded"
    tone, title, detail = words[code]
    warning = observation.get("renewal_warning")
    if code == "ready" and warning == "login_expiring":
        detail += (
            " Your longer-lived sign-in expires within 3 days. Sign in again before "
            "then to keep automatic renewal available."
        )
    elif code == "ready" and sign_in_reason(report.get("credentials") or {}, now):
        detail += (
            " The reported access credential has not expired; live "
            "model access is "
            "unverified. Sign in again before it expires to restore "
            "automatic renewal."
        )
    elif code == "ready" and warning:
        detail += " " + recovery_guidance(report.get("credentials") or {}, now)
    validation = observation.get("compatibility") or {}
    checked = validation.get("state")
    if checked in compatibility.LABELS:
        check_words = compatibility.LABELS[checked]
        explanation = compatibility.description(validation)
        if code == "ready" and (
            checked == "failed" or validation.get("reason") in compatibility.OPERATOR_REVIEW_REASONS
        ):
            tone, title = "warn", compatibility.LABELS["failed"]
        detail += " " + check_words + ". " + explanation
    health_zh = {
        "ready": ("报告显示就绪", "最近报告显示访问凭据仍有效。"),
        "renewal_pending": ("等待凭据续期", _guidance_zh(report.get("credentials") or {}, now)),
        "sign_in_required": ("需要登录", "请用下方按钮登录 Claude，电脑配对和本地对话保留。"),
        "switching": ("账号维护中", "请等待登录或账号更换完成，再重试模型请求。"),
        "degraded": (
            "可用性尚未确认",
            ("尚未获得新的账号可用性报告；持续如此时，请运行 ccfleet doctor 或联系运营方。"),
        ),
    }
    title_zh, detail_zh = health_zh[code]
    if code == "ready" and warning == "login_expiring":
        detail_zh += " 长期登录会在三天内到期，请提前重新登录，以保持自动续期。"
    elif code == "ready" and sign_in_reason(report.get("credentials") or {}, now):
        detail_zh += (

                ' 报告中的访问凭据尚未过期，但实时模型访问未经验证；'
                '请在到期前重新登录，恢复自动续期。'

        )
    elif code == "ready" and warning:
        detail_zh += " " + _guidance_zh(report.get("credentials") or {}, now)
    if checked in compatibility.LABELS:
        if code == "ready" and (
            checked == "failed" or validation.get("reason") in compatibility.OPERATOR_REVIEW_REASONS
        ):
            title_zh = COMPATIBILITY_ZH["failed"]
        explanation_zh = (
            "已报告的客户端版本与账号通过检查。"
            if checked == "passed"
            else COMPATIBILITY_DETAILS_ZH.get(validation.get("reason"), "等待更新验证确认。")
        )
        detail_zh += " " + COMPATIBILITY_ZH[checked] + "。" + explanation_zh
    source = (

            f'<div class="account-health" role="status" '
            f'data-health="{code}" '
            'aria-label="Reported account health" '
            'data-aria-label-en="Reported account health" '
            'data-aria-label-zh="账号健康报告"><p>'
            f'<span class="pill {tone}">{title}</span> {detail}</p'
            f'>'
            '<p class="small muted">A heartbeat observation does '
            'not '
            "prove "
            'that Anthropic will accept a model request. Use '
            '<code>ccfleet status --json</code> '
            'or <code>ccfleet doctor --privacy</code> for '
            'connection '
            "checks.</p></div>"

    )
    return _blocks(
        source,
        [
            f'<span class="pill {tone}">{escape(title_zh)}</span> {escape(detail_zh)}',
            "健康报告不能保证 Anthropic 会接受下一次模型请求。连接检查可用 "
            "<code>ccfleet status --json</code> 或 <code>ccfleet doctor --privacy</code>。",
        ],
    )


def _relay_usage(raw: Any, heard: Any, now: float, max_age: float, *, since: float = 0) -> str:
    """Content-free observations are optional detail, distinct from account quota."""
    report = relay_report(raw, now)
    if report is None or report["observed_at"] < since:
        return ""
    observed = report["observed_at"]
    current = (
        type(heard) in (int, float)
        and now - max_age <= heard <= now + 60
        and observed >= now - max_age
    )
    freshness = "Fresh report" if current else "Stale report — not current performance"
    freshness_zh = "最新报告" if current else "旧报告，不能代表当前性能"
    errors = report["requests"] - report["success"]
    success = report["last_success_at"]
    last_transfer = (
        "Last completed transfer " + escape(_age(now, min(success, now))) + " ago."
        if success is not None
        else ("No completed transfer observed in this window.")
    )
    transfer_zh = (
        "最近完成传输：" + escape(_age(now, min(success, now))) + " 前。"
        if success is not None
        else "此统计窗口尚未观测到完成的传输。"
    )
    lines = [
        (
            '<details class="relay-usage" aria-label="Inference '
            'relay measurements · 转发统计"><summary>'
        )
        + label("Relay measurements", "转发统计", inline=True)
        + "</summary><div>",
        _notice(
            (
                f"{freshness}; observed "
                f"{escape(_age(now, min(observed, now)))} ago. Seven UTC ca"
                f"lendar days."
            ),
            (
                f"{freshness_zh}；{escape(_age(now, min(observed, now)))} 前观"
                f"测。统计过去七个 UTC 日。"
            ),
            "small muted",
        ),
        _notice(
            f"<strong>{report['requests']}</strong> requests observed · "
            f"<strong>{report['success']}</strong> completed transfers · "
            f"<strong>{errors}</strong> other outcomes.",
            (
                f"<strong>{report['requests']}</strong> 次请求 · "
                f"<strong>{report['success']}</strong> 次完成传输 · "
                f"<strong>{errors}</strong> 次其他结果。"
            ),
        ),
        _notice(last_transfer, transfer_zh, "small muted"),
        _notice(
            (
                "Separate from the subscription quota bars and native "
                "transcript token counts. "
                "A completed HTTP transfer does not prove model-task "
                "success or future account readiness."
            ),
            (
                "这些转发统计与套餐用量条、原版对话记录中的 Token "
                "数分开。HTTP 传输完成不代表模型任务成功，"
                "也不保证账号下次仍可用。"
            ),
            "small",
        ),
        '<ul class="small">',
    ]
    for key, en, zh in (
        ("connect_ms", "Upstream connection", "上游连接"),
        ("first_byte_ms", "First upstream body byte", "上游正文首字节"),
        ("total_ms", "Total relay request", "转发请求总时长"),
    ):
        value = report["latencies"][key]
        samples = "sample" if value["count"] == 1 else "samples"
        measured = (
            (
                f"{value['mean']:.1f} ms mean · {value['max']:.1f} ms "
                f"max · {value['count']} {samples}"
            )
            if value["count"]
            else "No timing samples"
        )
        measured_zh = (
            ((
                f"均值 {value['mean']:.1f} ms · 最大 "
                f"{value['max']:.1f} ms · {value['count']} 个样本"
            ))
            if value["count"]
            else "没有时长样本"
        )
        lines.append(
            "<li>" + label(en + ": " + measured, zh + "：" + measured_zh, inline=True) + "</li>"
        )
    lines.append("</ul>")
    usage = report.get("token_usage")
    if usage is None:
        lines.append(
            _notice(
                "Response token usage: not measured by this report.",
                "此报告尚未统计响应 Token 用量。",
                "small",
            )
        )
    else:
        lines.append(
            '<div class="small relay-tokens"><b>'
            + label("Observed response token usage", "响应 Token 观测", inline=True)
            + "</b>"
            + _notice(
                (
                    f"Complete input/output samples: {usage['samples']} of "
                    f"{usage['eligible']} completed Messages responses tracked "
                    f"for usage."
                ),
                (
                    f"有完整输入/输出用量的样本：{usage['samples']}；可统计"
                    f"的完整 Messages 响应："
                    f"{usage['eligible']}。"
                ),
            )
        )
        if usage["samples"]:
            lines.append(
                _notice(
                    (
                        f"{usage['input_tokens']:,} input tokens · "
                        f"{usage['output_tokens']:,} output tokens."
                    ),
                    (
                        f"输入 {usage['input_tokens']:,} Token · 输出 "
                        f"{usage['output_tokens']:,} Token。"
                    ),
                )
            )
        else:
            lines.append(
                _notice(
                    "Input/output totals unknown: no complete usage samples.",
                    "没有完整用量样本，输入/输出总数未知。",
                )
            )
        for prefix, en, zh in (
            ("cache_read", "Cache-read input", "缓存读取输入"),
            ("cache_creation", "Cache-write input", "缓存写入输入"),
        ):
            samples = usage[prefix + "_samples"]
            measured = (
                f"{usage[prefix + '_input_tokens']:,} tokens from {samples} samples"
                if samples
                else "unknown (no explicit cache samples)"
            )
            measured_zh = (
                f"{samples} 个样本中的 {usage[prefix + '_input_tokens']:,} Token"
                if samples
                else "未知（没有明确的缓存样本）"
            )
            lines.append(_notice(en + ": " + measured + ".", zh + "：" + measured_zh + "。"))
        lines.append(
            _notice(
                (
                    "Only validated usage in fully completed successful "
                    "responses contributes. "
                    "Missing, oversized or partial usage is not zero; older "
                    "traffic was not backfilled. "
                    "These observed sums are not a bill, subscription quota, "
                    "or a complete account total. "
                    "No model/session IDs or conversation content are stored "
                    "with them."
                ),
                (
                    "只统计完整成功响应中验证过的用量。缺失、"
                    "超限或不完整的用量不是零，历史流量也未补录。"
                    "这些观测总数不是账单、套餐限额或整个账号的总用量；"
                    "不保存模型/会话 ID 或对话内容。"
                ),
                "muted",
            )
            + "</div>"
        )
    lines.append(
        '<details class="small"><summary>'
        + label("Other outcome categories", "其他结果分类", inline=True)
        + "</summary><ul>"
    )
    for key, en, zh in (
        ("auth_errors", "Authentication errors", "认证失败"),
        ("permission_errors", "Permission errors", "权限不足"),
        ("rate_limits", "Rate limits", "触发限流"),
        ("upstream_errors", "Upstream errors", "上游错误"),
        ("connection_errors", "Connection errors", "连接错误"),
        ("cancelled", "Cancelled", "已取消"),
        ("input_errors", "Input errors", "请求格式错误"),
    ):
        lines.append(
            "<li>" + label(f"{en}: {report[key]}", f"{zh}：{report[key]}", inline=True) + "</li>"
        )
    lines.append(
        "</ul></details>"
        + _notice(
            (
                "Timing is measured on the slot, not end-to-end from "
                "your computer. First-byte and total timing begin after "
                "the relay decodes a model-request envelope. First-byte "
                "samples require a forwarded upstream response body."
            ),
            (
                "时长在槽位上测量，不是从你的电脑开始的端到端耗时。"
                "首字节和总时长从转发器解码模型请求后开始计时；"
                "只有转发了上游响应正文才记录首字节样本。"
            ),
            "small muted",
        )
        + "</div></details>"
    )
    return "".join(lines)


def _quota_meter(used: Any, en: str, zh: str, reset: Any, read: Any, now: float, exact: Any) -> str:
    """Keep native quota/reset calculations; translate only their authored labels."""
    source = _meter(used, en, reset, read, now, exact)
    source = source.replace(
        f"<span>{escape(en)}</span>", "<span>" + label(en, zh, inline=True) + "</span>"
    )
    return source.replace(
        '<div class="meter-foot">resets ',
        '<div class="meter-foot">' + label("resets", "重置时间", inline=True) + " ",
    )


def _in_use(report: Mapping[str, Any], now: float, refresh: str = "", *, fresh: bool = True) -> str:
    """The bound account and observed usage, with the same readiness conditions."""
    if not fresh:
        return _notice(
            "Waiting for fresh credential status from the machine. Current model "
            "readiness has not been verified; the last report may be out of date.",
            "等待机器上报新的凭据状态。当前模型可用性尚未验证，旧报告可能已过时。",
        )
    creds = report.get("credentials") or {}
    renewal = renewal_report(creds.get("renewal"))
    expires = access_expiry(creds)
    terminal = sign_in_reason(creds, now)
    if (
        renewal.get("reason") in TRANSITION_REASONS
        or creds.get("account_fp")
        and creds.get("bound_fp")
        and creds["account_fp"] != creds["bound_fp"]
    ):
        return _notice(
            (
                "Claude account maintenance is in progress. Wait for it "
                "to finish before retrying model requests."
            ),
            "Claude 账号正在维护，请等待完成后再重试模型请求。",
        )
    if terminal in NATIVE_LOGIN_FAILURES:
        return _notice(
            (
                "Native Claude requires a fresh sign-in. Use Sign in "
                "again below. Your pairing, files and history are kept."
            ),
            ("原版 Claude 需要重新登录，请使用下方“重新登录”。配对、文件和历史保留。"),
            "lapsed",
        )
    if expires is not None and expires <= now + ACCESS_MARGIN_S:
        return _notice(
            "Model access needs credential renewal. "
            + recovery_guidance(creds, now)
            + (
                " Use Sign in again below. "
                if terminal
                else " Contact your operator if access does not recover. "
            )
            + "Your pairing, files and history are kept.",
            "模型访问需要续期凭据。"
            + _guidance_zh(creds, now)
            + ("请使用下方“重新登录”。" if terminal else "如果无法恢复，请联系运营方。")
            + "配对、文件和历史保留。",
            "lapsed",
        )
    if creds.get("logged_in") is False or creds.get("present") is False:
        return _notice(
            "Not signed in to Claude right now. Sign in below to use your slot.",
            "当前尚未登录 Claude，请在下方登录后使用槽位。",
        )
    if terminal and expires is None:
        return _notice(
            recovery_guidance(creds, now)
            + (" Use Sign in again below. Your pairing, files and history are kept."),
            _guidance_zh(creds, now) + ("请使用下方“重新登录”；配对、文件和历史保留。"),
            "lapsed",
        )
    if renewal.get("reason") in {"expiry_unknown", "credential_unavailable"}:
        return _notice(
            (
                "Claude credential expiry could not be verified. Contact "
                "your operator before assuming model access is ready."
            ),
            ("尚未确认 Claude 凭据到期时间，请联系运营方，不要据此假设模型访问已就绪。"),
            "lapsed",
        )
    if creds.get("logged_in") is not True:
        return _notice(
            (
                "Claude sign-in could not be verified from the current "
                "report. Contact your operator before assuming model "
                "access is ready."
            ),
            ("当前报告无法确认 Claude 登录，请联系运营方，不要据此假设模型访问已就绪。"),
            "lapsed",
        )
    plan = plans.label(creds.get("subscription_type"), creds.get("plan"))
    who = f" as {escape(str(creds['email']))}" if creds.get("email") else ""
    who_zh = "：" + escape(str(creds["email"])) if creds.get("email") else ""
    left = _sign_in_left(instant(creds.get("refresh_expires_at")), now)
    remaining = instant(creds.get("refresh_expires_at"))
    left_zh = ""
    if remaining is not None and remaining > now:
        days = int((remaining - now) // 86400)
        left_zh = f"还可使用 {days} 天" if days else "一天内到期"
    lines = [
        _notice(
            f"Signed in{who}{(' · ' + escape(str(plan)) + ' plan') if plan else ''}"
            f"{' · sign-in ' + left if left else ''}.",
            f"已登录{who_zh}{(' · ' + escape(str(plan)) + ' 套餐') if plan else ''}"
            f"{' · 长期登录' + left_zh if left_zh else ''}。",
            "signed",
        )
    ]
    warning = renewal_warning(creds, now)
    if terminal:
        lines.append(
            _notice(
                (
                    "The reported access credential has not expired; live "
                    "model access is unverified. "
                    "Sign in again before expiry to restore automatic "
                    "renewal. Your pairing, files and history are kept."
                ),
                (
                    "报告中的访问凭据尚未过期，但实时模型访问未经验证。"
                    "请在到期前重新登录以恢复自动续期；配对、"
                    "文件和历史保留。"
                ),
                "lapsed",
            )
        )
    elif warning == "login_expiring":
        lines.append(
            _notice(
                (
                    "Your longer-lived Claude sign-in expires within 3 days. "
                    "Sign in again before then to keep automatic renewal "
                    "available."
                ),
                "长期 Claude 登录会在三天内到期，请提前重新登录，以保持自动续期。",
                "lapsed",
            )
        )
    elif warning and renewal.get("reason") != "native_refresh_unconfirmed":
        lines.append(
            _notice(recovery_guidance(creds, now), _guidance_zh(creds, now), "small muted")
        )
    elif renewal.get("state") == "retrying":
        lines.append(
            _notice(
                (
                    'Native credential renewal is not yet confirmed; a '
                    'bounded retry is scheduled.'
                ),
                "原版凭据续期尚未确认，已安排有限次数的重试。",
                "small muted",
            )
        )
    elif expires is not None and expires <= now + RENEWAL_WINDOW_S and renewal:
        lines.append(
            _notice(((
                'Automatic credential renewal is due.'
            )), "已到自动续期时间。", "small muted")
        )
    elif renewal.get("last_success_at"):
        age = _age(now, renewal["last_success_at"])
        lines.append(
            _notice(
                ("Last observed native credential renewal: ") + escape(age) + " ago.",
                "最近观测到的原版凭据续期：" + escape(age) + " 前。",
                "small muted",
            )
        )
    quota = report.get("quota") or {}
    session, week = quota.get("session") or {}, quota.get("week") or {}
    read = quota.get("checked_at")
    bars = _quota_meter(
        session.get("used_pct"),
        "5-hour session",
        "五小时窗口",
        session.get("resets"),
        read,
        now,
        session.get("resets_at"),
    ) + _quota_meter(
        week.get("used_pct"),
        "This week",
        "本周窗口",
        week.get("resets"),
        read,
        now,
        week.get("resets_at"),
    )
    usage = report.get("usage") or {}
    spent = ""
    if usage:
        chart, caption = _usage_chart(usage)
        span = _usage_span(usage)
        span_zh = span.replace("days", "天").replace("day", "天")
        spent = _notice(
            f"<b>{escape(_human_tokens(usage.get('total_tokens') or 0))}</b>"
            f" tokens run on this slot itself, last {escape(span)}",
            f"<b>{escape(_human_tokens(usage.get('total_tokens') or 0))}</b>"
            f" Token · 此槽位过去 {escape(span_zh)} 的原版记录",
            "small",
        )
        if "<svg" not in chart:
            chinese_chart = "暂无足够数据绘制趋势" if chart else ""
            spent += bi(chart, escape(chinese_chart), inline=True) if chart else ""
        else:
            spent += chart
        if caption:
            unit_zh = (
                "每小时"
                if isinstance(usage.get("by_hour"), Mapping) and usage["by_hour"].get("tokens")
                else "每日"
            )
            spent += bi(
                caption,
                (
                    f'<div class="usage-nums muted">{unit_zh} Token · 过去'
                    f' {escape(span_zh)}</div>'
                ),
            )
    if bars:
        bars = (
            '<div class="usage-nums muted">'
            + label(
                ("your Claude account · every device"),
                ("Claude 账号用量 · 所有设备合计"),
                inline=True,
            )
            + "</div>"
            + bars
            + _notice(
                ACCOUNT_WIDE,
                ("这两个窗口包含这个 Claude 账号在所有设备上的使用，不仅是当前槽位。"),
                "small muted",
            )
            + refresh
        )
    elif refresh:
        bars = (
            _notice(("No reading of your limits yet."), "尚未读取用量限制。", "small muted")
            + refresh
        )
    halves = [
        f'<div class="usage-{name}">{html}</div>'
        for name, html in (("bars", bars), ("trend", spent))
        if html
    ]
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


def _ways_to_use(_report: Mapping[str, Any]) -> str:
    """Explain the current local CLI and HTTPS model connection."""
    return ('<div class="ways" aria-label="How to use this slot" '
            'data-aria-label-en="How to use this slot" data-aria-label-zh="使用方法">'
            '<div class="way"><b>Claude Code CLI</b>'
            + _notice(
                (
                    'Run <code>ccfleet local</code> on your computer. '
                    'Claude Code runs its tools '
                    'and reads your files locally. When it needs a model '
                    'reply, it sends the '
                    'selected context over HTTPS to Fleet. Fleet chooses '
                    'one of your selected '
                    'slots, uses that slot’s Claude sign-in, and returns '
                    'the reply to your computer.'
                ),
                (
                    '在电脑上运行 <code>ccfleet local</code>。Claude '
                    'Code 在本机运行工具、读取文件；'
                    '需要模型回复时，把选定的上下文通过 HTTPS 发给 '
                    'Fleet。Fleet 选择你指定的槽位，'
                    '使用该槽位的 Claude 登录，再把回复送回电脑。'
                ))
            + _notice(
                (
                    'Your files, settings and conversation history stay '
                    'on your computer, but '
                    'file excerpts and tool results selected as model '
                    'context are sent with the request. '
                    '<code>ccfleet start</code> offers a '
                    'new/continue/resume menu; '
                    '<code>ccfleet sessions</code> opens native history. '
                    '<a href="/docs/guide#client-tools">Client tools and '
                    'diagnostics</a> are in the guide.'
                ),
                (
                    '文件、设置和会话历史保留在电脑上；'
                    '被选作模型上下文的文件片段、工具结果会随请求发送。'
                    '<code>ccfleet start</code> 提供新建/继续/恢复菜单；'
                    '<code>ccfleet sessions</code> 打开原版历史列表。'
                    '<a href="/docs/guide#client-tools">客户端工具与诊断<'
                    '/a> 见使用指南。'
                ))
            + '</div></div>')


def _ended(login: Mapping[str, Any]) -> str:
    """How a flow that is over ended, said once on the card: a change of
    account by the machine's fixed word, anything that failed with its reason."""
    if login.get("state") == "done":
        cls, words = SWITCH_ENDED.get(str(login.get("detail")), ("", ""))
        chinese = {slotstates.SWITCHED: "槽位已改用新的 Claude 账号。",
                   slotstates.SAME_ACCOUNT: "这就是槽位原来的账号，没有发生变更。"}
        return ('<p class="' + cls + '">' + label(
            words, chinese.get(str(login.get("detail")), words), inline=True) + '</p>'
                if words else "")
    what = NOT_DONE.get(str(login.get("kind")), "The sign-in was not kept")
    chinese = {"token": "上次本地设置未完成", "switch": "账号更换未完成"}.get(
        str(login.get("kind")), "登录未完成")
    reason = str(login.get("detail") or "no reason given")
    return '<p class="lapsed">' + label(what + ': ' + reason + '.', chinese + '：' + reason + '。',
                                        inline=True) + '</p>'


def _sign_in(slot: Mapping[str, Any], report: Mapping[str, Any],
             login: Mapping[str, Any], csrf: str, now: float) -> str:
    base = f"/account/slots/{escape(slot['id'])}"
    if login.get("kind") == "token" and login.get("state"):
        return ('<div class="row-line stacked flow"><div class="row-name">Claude</div>'
                '<p class="muted small">' + label(
                    (
                        'A connection setup is still pending. Cancel it '
                        'before signing in again.'
                    ),
                    "连接设置尚未结束，请先取消，再重新登录。", inline=True) + '</p>'
                + _form(f"{base}/cancel", csrf, "Cancel setup", cls="danger") + "</div>")
    state = login.get("state") if login.get("kind") != "token" else None
    if not state:
        signed_in = (report.get("credentials") or {}).get("logged_in") is True
        field = ("" if signed_in else
                 '<input type="email" name="email" placeholder="your Claude email (optional)" '
                 'data-placeholder-en="your Claude email (optional)" '
                 'data-placeholder-zh="Claude 邮箱（可选）" aria-label="Claude email" '
                 'data-aria-label-en="Claude email" data-aria-label-zh="Claude 邮箱">')
        return ('<div class="row-line"><div class="row-name">Claude</div>'
                '<div class="actions">'
                + _form(f"{base}/signin", csrf, "Sign in again" if signed_in else
                        "Sign in to Claude", field, "" if signed_in else "primary")
                + _change_account(slot, csrf, now) + "</div></div>")
    flow_zh = {"requested": "正在请求槽位启动登录，可能需要几分钟。请保持页面打开。",
               "url_ready": "打开链接完成授权，再把授权码粘贴到下方。",
               "code_sent": "授权码已提交，等待槽位完成登录…"}
    body = '<span class="login-say">' + label(
        LOGIN_WORDS.get(state, state), flow_zh.get(state, state), inline=True) + '</span>'
    if login.get("kind") == "switch":
        body = '<p class="flow-why">' + label(
            SWITCH_FLOW, (
                '请登录槽位以后要使用的 Claude 账号。文件保留，'
                '在流程完成前仍使用原账号。'
            ),
            inline=True) + '</p>' + body
    url = login.get("url") or ""
    # Checked again here, not only when it was stored: a link on this page must
    # never be anything but a sign-in on Anthropic's own hosts.
    if is_login_url(url) and state in ("url_ready", "code_sent"):
        body += (f'<a class="login-url" href="{escape(url)}" target="_blank" '
                 f'rel="noopener noreferrer">{escape(url)}</a>')
    if state == "url_ready":
        body += _form(f"{base}/code", csrf, "Send code",
                      (

                              '<input type="text" name="code" placeholder="paste '
                              'the '
                              'code / 粘贴授权码" '
                              'data-placeholder-en="paste the code" '
                              'data-placeholder-zh="粘贴授权码" '
                              'aria-label="Authorization code" '
                              'data-aria-label-en="Authorization code" '
                              'data-aria-label-zh="授权码" '
                              'autocomplete="off" required>'

                      ), "primary")
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
    return (' <span class="muted small">'
            + label("You can change account again", "下次可更换账号：", inline=True)
            + f' <time datetime="{escape(resets.iso(wait))}" data-local>'
            f"{escape(resets.until(now, wait))}</time></span>")


#: A channel as the page names it.
CHANNEL_WORDS = {"stable": "Stable", "latest": "Latest"}
#: Under the button that moves a slot off Stable: what pressing it commits to.
UPDATE_NOTE = (
    'Switches this slot to the latest release; it keeps '
    'itself current from then on.'
)
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
    line = bi(_claude_line(said), _claude_line_zh(said), inline=True)
    if kind in ("available", "failed"):
        update_words = f"Update to {latest}" if latest else "Update to the latest release"
        buttons.append(_form(f"{base}/update", csrf, update_words, cls="primary"))
        if channel != "latest":
            note = '<p class="cc-note">' + label(
                UPDATE_NOTE, (
                    '改用最新版通道，以后会自动保持该通道的最新版本。'
                ), inline=True) + '</p>'
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
        return f"Updated to <b>{escape(said['to'])}</b> {SEP} new sessions use it"
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


def _claude_line_zh(said: Mapping[str, Any]) -> str:
    kind, version, latest = said["status"], escape(said["installed"]), said["latest"]
    if kind == "held":
        return f"<b>{version}</b> {SEP} 运营方暂停更新"
    if kind == "updating":
        return (f"正在更新到 <b>{escape(said['to'])}</b>…" if said.get("to")
                else "正在更新到最新版…")
    if kind == "updated":
        return f"已更新到 <b>{escape(said['to'])}</b> {SEP} 新会话使用此版本"
    if kind == "failed":
        return "更新失败：" + escape(said.get("detail") or "未提供原因")
    if said["channel"] == "latest":
        told = f" {SEP} 已是最新" if kind == "current" and latest else ""
        newer = f" {SEP} 最新版 <b>{escape(latest)}</b>" if kind == "available" else ""
        return f"<b>{version}</b>{told} {SEP} 跟随最新版通道{newer}"
    where = "稳定版" if said["channel"] == "stable" else "固定版本" if said["pinned"] else ""
    line = f"<b>{version}</b>" + (f" {SEP} {where}" if where else "")
    if kind == "available":
        return line + f" {SEP} 最新版 <b>{escape(latest)}</b>"
    return line + (f" {SEP} 已是最新" if latest else "")


def _cli_access(slot: Mapping[str, Any], node: Mapping[str, Any],
                devices: list[Mapping[str, Any]], csrf: str, now: float) -> str:
    """Pair and revoke local ccfleet installations. No Claude token is involved."""
    base = f"/account/slots/{escape(slot['id'])}"
    configured = bool(node.get("access_host") and node.get("ssh_host_key"))
    ready = configured and slot.get("state") == slotstates.ACTIVE
    rows = []
    for device in devices:
        seen = device.get("last_seen_at") or 0
        when = label(f"used {_age(now, seen)} ago" if seen else "not used yet",
                     f"最近使用：{_age(now, seen)} 前" if seen else "尚未使用", inline=True)
        rows.append('<div class="device"><span><b>' + escape(str(device["name"]))
                    + f'</b><small>{when} · {escape(str(device["fingerprint"]))}</small>'
                    + '</span>' + _form(f"{base}/cli-revoke", csrf, "Remove",
                                        '<input type="hidden" name="device_id" value="'
                                        + escape(str(device["id"])) + '">', "quiet") + '</div>')
    if ready:
        button = _form(f"{base}/cli", csrf,
                       "Connect another computer" if devices else "Connect this computer",
                       cls="primary")
    elif configured:
        button = '<span class="muted small">' + label(
            "Finish signing in to Claude on this slot, then connect this computer.",
            "请先在槽位上完成 Claude 登录，再连接电脑。", inline=True) + '</span>'
    else:
        button = '<span class="muted small">' + label(
            (
                'CLI access is being prepared for this slot.'
            ), "正在为槽位准备电脑接入。", inline=True) + '</span>'
    introduction = _blocks(
        '<p class="small muted"><code>ccfleet local</code> runs Claude Code on your '
        'computer and sends supported model requests through this slot. The device '
        'access key is not your Claude credential.</p>', [
            (
                '<code>ccfleet local</code> 在本机运行 Claude Code，'
                '受支持的模型请求使用这个槽位。'
                '电脑访问密钥不是你的 Claude 凭据。'
            ),
        ])
    return ((
        '<div class="row-line stacked cli-access"><div '
        'class="row-name">CC Fleet CLI</div>'
    )
            + introduction + ''.join(rows) + f'<div class="actions">{button}</div></div>')


def _quota_refresh(slot: Mapping[str, Any], report: Mapping[str, Any], csrf: str,
                   now: float) -> str:
    """Read account quota now, keeping the same protected action."""
    read = (report.get("quota") or {}).get("checked_at")
    known = isinstance(read, (int, float)) and not isinstance(read, bool)
    when = f"read {_age(now, read)} ago · " if known else ""
    when_zh = f"最近读取：{_age(now, read)} 前 · " if known else ""
    if quota_reading(slot.get("quota_wanted_at"), read, now):
        return '<p class="small muted">' + label(
            when + "reading them again now…", when_zh + "正在重新读取…", inline=True) + '</p>'
    return ('<div class="refresh"><span class="small muted">'
            + label(when + "every five minutes", when_zh + "每五分钟读取一次", inline=True)
            + '</span>' + _form(f"/account/slots/{escape(slot['id'])}/quota", csrf, "Refresh")
            + '</div>')


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
    copy = _blocks((
        '<div><h2>Outage emails</h2>'
        f"<p>{escape(said)}</p>"
        '<p class="small muted">Sent through Resend, which '
        'delivers them: your address and '
        "your slot's name go to it, and only when there is an "
        'outage to tell you about. '
        'The status page says the same for everybody.</p></div>'
    ), [
                "故障邮件", "已开启：机器离线五分钟后通知，恢复时再次通知。" if on else
                "已关闭。开启后，机器离线五分钟和恢复时会发送邮件。",
                (
                    '邮件由 Resend 投递，仅在需要通知故障时发送你的邮箱和'
                    '槽位名称。公共状态页提供相同的机器状态。'
                ),
            ])
    return '<div class="card allowance" id="outage-emails">' + copy + button + '</div>'


def _rename(slot: Mapping[str, Any], csrf: str) -> str:
    """The slot's name is its holder's to choose (Erik, 2026-09-24): it is what
    claude.ai shows, so it is what Anthropic sees, and it starts neutral."""
    return ('<div class="row-line stacked"><div class="row-name">'
            + label('Name', '槽位名称', inline=True) + '</div>'
            + _form(f"/account/slots/{escape(slot['id'])}/rename", csrf, "Rename",
                    (
                        '<input type="text" name="name" maxlength="30" '
                        'autocomplete="off" '
                        'required pattern="[A-Za-z0-9][A-Za-z0-9-]{0,'
                        '28}[A-Za-z0-9]" '
                        f'placeholder="{escape(names.display(slot))}" '
                        'aria-label="A new name for this slot" '
                        'data-aria-label-en="A new name for this slot" '
                        'data-aria-label-zh="槽位的新名称">'
                    ))
            + '<p class="small muted">' + label(
                (
                    'What claude.ai shows for this machine, so Anthropic '
                    'sees it too: pick anything but your email address.'
                ),
                "这个名称会显示在 claude.ai，也会被 Anthropic 看到。请不要用邮箱命名。",
                inline=True) + '</p></div>')


def _release(slot: Mapping[str, Any], csrf: str) -> str:
    return ('<div class="row-line release"><div class="row-name">'
            + label('Give it back', '归还槽位', inline=True) + '</div>'
            + _form((
                f"/account/slots/{escape(slot['id'])}/release"
            ), csrf, "Give this slot back",
                    '<label class="check"><input type="checkbox" name="confirm" value="wipe" '
                    'required> ' + label(
                        (
                            'Delete everything on it: files, sessions and the Claude '
                            'sign-in. Your Claude account itself is untouched.'
                        ),
                        (
                            '删除槽位中的文件、会话和 Claude 登录信息；'
                            '不会删除你的 Claude 账号。'
                        ),
                        inline=True) + '</label>', "danger")
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
    body = _blocks(body, [
        "管理员也通过 Google 登录。", (
            '<a class="btn primary big" '
            'href="/auth/google/start?next=/admin">使用 Google 登录</a>'
        ),
    ] if account is None else [
        (
            f"当前登录："
            f"<strong>{escape(str(account.get('email', '')))}</str"
            f"ong>。这个账号不是管理员。"
        ),
        (
            '运营方可在服务器执行 <code>ccfleetd account role '
            '&lt;email&gt; admin</code> 设置管理员。'
        ),
    ])
    # Customers land here too, by typing the bare address: point them home.
    elsewhere = (
        '<div class="card door-alt"><p><strong>Looking for your '
        'slots?</strong> They '
        'are on <a href="/account">your page</a>. New to '
        'ccfleet? Start with '
        '<a href="/docs">what it is</a> and <a '
        'href="/docs/guide">how to begin</a>.'
        "</p></div>"
    )
    elsewhere = _blocks(elsewhere, [
        (
            '<strong>要找自己的槽位？</strong> 请到 <a '
            'href="/account">我的槽位</a>。首次使用可阅读 '
            '<a href="/docs">产品概览</a> 和 <a '
            'href="/docs/guide">入门指南</a>。'
        ),
    ])
    fallback = _blocks('<p class="note">Or <a href="/auth/basic">use the admin token</a> '
                       '&mdash; the way in when Google sign-in is unavailable.</p>', [
                           (
                               'Google 登录不可用时，可 <a '
                               'href="/auth/basic">使用管理员令牌</a>。'
                           ),
                       ])
    return _shell("console", f'<div class="card door">{MARK}<h1>'
                  + label('ccfleet console', 'Fleet 管理后台', inline=True) + '</h1>' + body
                  + fallback + '</div>' + elsewhere,
                  viewer=viewer_for(account, session_id, cfg, store, on_console=True),
                  door=True)


def cli_pairing_page(slot: Mapping[str, Any], token: str, viewer: Optional[Viewer] = None) -> str:
    """A single-use device pairing code, followed by current HTTPS setup."""
    name = escape(str(slot.get("name") or slot["id"]))
    body = (
        '<div class="pagehead"><h1>'
        + label("Connect a computer", "连接电脑", inline=True)
        + "</h1>"
        + _notice(
            (f"This pairing code connects your computer to <strong>{name}</strong>."),
            (f"此配对码会把电脑连接到槽位 <strong>{name}</strong>。"),
            "sub",
        )
        + "</div>"
        + '<div class="card"><h2>'
        + label("1. Install or update Fleet", "1. 安装或更新 Fleet", inline=True)
        + "</h2><pre>curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/"
        "laptop/install.sh | bash -s -- --setup</pre>"
        + _notice(
            (
                "The same command sets up a new computer or updates "
                "your Fleet client. "
                "It checks the connection and configures your shell "
                "PATH. Existing native "
                "Claude is kept; if missing, setup installs the "
                "original local CLI from the "
                "fixed vendor URL. No project is uploaded during "
                "setup."
            ),
            (
                "新电脑或更新 Fleet 客户端都使用这个命令。"
                "设置程序检查连接并配置 PATH；已有原版 Claude 保留，"
                "未安装时会从固定官方地址安装。"
                "设置过程中不会上传项目。"
            ),
        )
        + "</div>"
        + '<div class="card"><h2>'
        + label(("2. Enter this code only if asked"), "2. 仅在提示时输入配对码", inline=True)
        + "</h2>"
        + _notice(
            (
                f"A new computer needs this pairing code. It works onc"
                f"e and expires in {_span(cli_access.PAIRING_TTL_S)}."
            ),
            (f"新电脑需要这个一次性配对码，有效期为 {_span_zh(cli_access.PAIRING_TTL_S)}。"),
        )
        + f'<pre class="token">{escape(token)}</pre>'
        + _notice(
            (
                "Already paired? Setup keeps your existing "
                "connection; you do not need to "
                "use this code. To deliberately pair another slot, "
                "use <code>ccfleet login</code> "
                "and enter its code."
            ),
            (
                "已经配对的电脑会继续使用现有连接，不必再次输入。"
                "要主动配对另一槽位，请运行 "
                "<code>ccfleet login</code>，输入其配对码。"
            ),
        )
        + "</div>"
        + '<div class="card"><h2>'
        + label(
            ("3. Connect Claude and open your project"), "3. 连接 Claude 并打开项目", inline=True
        )
        + "</h2><pre>ccfleet gateway setup\ncd /path/to/your-project\nccfleet local</pre>"
        + _notice(
            (
                "Open a new terminal first so PATH changes take "
                "effect. Gateway setup "
                "creates or reuses a private Fleet connection key. "
                "Claude Code does file and "
                "tool work on your computer; its model requests "
                "travel over HTTPS to Fleet, "
                "use your selected slot’s Claude account, and return "
                "the reply to your computer."
            ),
            (
                "先打开新终端，使 PATH 生效。"
                "网关设置会创建或复用私密的 Fleet 连接密钥。Claude "
                "Code "
                "在本机处理文件和工具操作；模型请求通过 HTTPS 发给 "
                "Fleet，使用你选定槽位的 Claude 账号，"
                "再把回复送回电脑。"
            ),
        )
        + _notice(
            (
                "Your saved history and files stay local. Selected "
                "prompts, file excerpts "
                "and tool results are sent as model context. Fleet’s "
                "gateway can read those "
                "requests and replies while forwarding them. Setup "
                "does not automatically "
                "erase existing files or history."
            ),
            (
                "保存的历史和文件留在本机；选定的提示词、文件片段、"
                "工具结果会作为模型上下文发送。Fleet "
                "网关转发时能读取这些请求与回复。"
                "设置程序不会自动清除已有文件或历史。"
            ),
        )
        + _notice(
            (
                "Use <code>ccfleet start</code> for a "
                "new/continue/resume menu, or native "
                "<code>--resume</code> and <code>--continue</code> "
                "to reopen local history. "
                "New local conversations default to bypassing "
                "permission prompts. Choose "
                "<code>--mode manual</code> for approval prompts. "
                "See the "
                '<a href="/docs/guide">connection and usage '
                "guide</a>."
            ),
            (
                "<code>ccfleet start</code> 提供新建/继续/恢复菜单，"
                "也可用原版 <code>--resume</code> "
                "或 <code>--continue</code> 打开本地历史。"
                "新建本地对话默认跳过权限确认，需要逐次确认时使用 "
                "<code>--mode manual</code>。详见 <a "
                'href="/docs/guide">连接与使用指南</a>。'
            ),
            "muted",
        )
        + '</div><p><a class="back" href="/account">'
        + label("Back to your slots", "返回我的槽位", inline=True)
        + "</a></p>"
    )
    return _shell("connect a computer", body, viewer=viewer)
