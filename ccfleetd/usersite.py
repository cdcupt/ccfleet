"""The user site: one person, the slots they hold, and what they can do with them.

Private slot management is scoped to the signed-in account, and the scoping is
the point. A slot that is not theirs answers exactly as a slot that does not exist
would — they never see other people, other slots or anybody's alerts — and
every action checks the slot's holder again rather than trusting the page it
came from.

The public homepage ranking renders a separate allowlisted numeric summary with
masked email fragments or chosen nicknames. It never reads another account's private page.

Signing in to *us* is Google's (see oauth.py and sessions.py). Signing in to
*Claude* happens on the slot, through Claude Code's own login: this page
carries a URL out and a code back, and the credential is written on the
machine, in that slot's home. Nothing here ever holds it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import shlex
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any, Optional

from ccfleet_agent.model_usage import LABELS, MODELS_URL, PRICING_URL

from . import (
    claude_versions,
    cli_access,
    client_status,
    compatibility,
    devin_ui,
    gateway_ui,
    names,
    oauth,
    offer_ui,
    payments,
    plans,
    resets,
    status,
    wallet,
    wallet_access,
    wallet_routes,
    wallet_ui,
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
    PANEL_ACTION_TAG,
    _age,
    _human_tokens,
    _meter,
    _usage_chart,
    _usage_span,
    console_href,
    product_href,
    quota_reading,
    quota_refresh_completed,
    quota_refresh_failed,
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
    SubscriptionReleaseBlocked,
)
from .usage import REFERENCE_MODEL, REFERENCE_RATES

# Read only this signed-in account's rendered quota area; never replace its
# connection instructions, inputs or disclosures. One five-minute bounded run.
QUOTA_POLL_JS = r'''(function(){
var attempts=0,deadline=0,running=false;
function pending(){return Array.from(document.querySelectorAll('[data-quota-slot]'));}
function stop(nodes){nodes.forEach(function(n){n.removeAttribute('data-quota-slot');
var zh=document.documentElement.lang.toLowerCase().startsWith('zh');
n.textContent=zh?
'暂未收到新的额度读数。请稍后重试；旧读数不代表还有剩余额度。':
'No new quota reading arrived. Try again later; old readings do not confirm remaining quota.';
var link=document.createElement('a');link.href=location.pathname;link.dataset.fleetStatus='account';
link.textContent=zh?'查看状态':'Check status';n.append(' ',link);});}
function poll(){var nodes=pending();if(!nodes.length){running=false;return;}
if(++attempts>30||Date.now()>=deadline){stop(nodes);running=false;return;}
var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},8000);
fetch(location.pathname,{credentials:'same-origin',cache:'no-store',redirect:'error',
signal:controller.signal}).then(function(r){if(!r.ok)throw Error('status');return r.text();})
.then(function(text){if(text.length>2097152)throw Error('size');
var page=new DOMParser().parseFromString(text,'text/html');
nodes.forEach(function(n){var card=page.getElementById('slot-'+n.dataset.quotaSlot);
var fresh=card&&card.querySelector('.usage-bars'),current=n.closest('.usage-bars');
if(!fresh||!current)throw Error('missing');current.replaceChildren(
...Array.from(fresh.childNodes).map(function(x){return document.importNode(x,true);}));});
}).catch(function(){stop(pending());}).finally(function(){clearTimeout(timer);
if(pending().length)setTimeout(poll,10000);else running=false;});}
function start(){if(!running&&pending().length){running=true;attempts=0;
deadline=Date.now()+300000;setTimeout(poll,10000);}}
document.addEventListener('ccfleet:quota',start);start();
})();'''
QUOTA_POLL_CSP = "'sha256-" + base64.b64encode(
    hashlib.sha256(QUOTA_POLL_JS.encode()).digest()).decode() + "'"
QUOTA_POLL_TAG = '<script>' + QUOTA_POLL_JS + '</script>'

# What a page may say after an action. Chosen by a fixed code, never text taken
# from the request: a message a link could write is a message a stranger could
# put on this page, on our domain, in front of somebody signed in.
NOTES = {
    "checkout-returned": ("warn", "Checkout closed. Your balance shows confirmed credit; "
                         "a payment may still be processing."),
    "wallet-purchased": ("ok", "First month purchased. Your slot is being prepared."),
    "wallet-renewed": ("ok", "Your slot has been renewed for one calendar month."),
    "wallet-purchase-required": ("warn", "Choose a slot and review its USD price before buying."),
    "wallet-expired": ("warn", "Renew this slot before starting a new connection."),
    "claimed": ("ok", "Your slot is being set up: an account on the machine, then Claude "
                      "Code. It takes a few minutes, and this page keeps up."),
    "no-slot": ("warn", "No slot is available right now. Try again later, or ask your operator."),
    "no-allowance": ("warn", "You already hold every slot your allowance covers."),
    "released": ("ok", "Given back. Everything on it is being deleted."),
    "monthly-release-blocked": ("warn", "Cancel monthly billing first, then keep the slot "
                                 "until its monthly plan and paid time have both ended. "
                                 "Manage it under Monthly payments."),
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
    "reading": ("ok", "A new quota reading was requested. This panel checks for the result."),
    "emails-on": ("ok", "Outage emails on: we email you when your slot's machine has been "
                        "down for five minutes, and again when it is back."),
    "emails-off": ("ok", "Outage emails off."),
    **gateway_ui.NOTES,
}

NOTES_ZH = {
    "checkout-returned": "付款页面已关闭。余额只显示已确认的入账，付款可能仍在处理中。",
    "wallet-purchased": "已购买首月，正在准备槽位。",
    "wallet-renewed": "已按 UTC 日历续费一个月。",
    "wallet-purchase-required": "请选择槽位，确认美元月费后再购买。",
    "wallet-expired": "请先续费此槽位，再建立新连接。",
    "claimed": (
        '槽位正在准备：先创建独立账号，再安装 Claude Code。'
        '通常需要几分钟，页面会自动更新。'
    ),
    "no-slot": "目前没有可领取槽位。请稍后再试，或联系运营方。",
    "no-allowance": "你已领取额度内的全部槽位。",
    "released": "槽位已归还，正在删除其中的所有内容。",
    "monthly-release-blocked": ("请先停止续费，等月付计划与已付费服务时间都结束后再归还。"
                                "可在“月付管理”操作。"),
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
    "name-taken": "名称已被使用，请换一个。", "reading": "已请求新的额度读数，此面板会检查结果。",
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
    if parts in (["account", "purchase"], ["account", "renew"]):
        return wallet_routes.act(store, cfg, account, session_id, now, parts[1], form)
    if len(parts) == 3 and parts[:2] == ["account", "gateway"]:
        return gateway_ui.act(store, cfg, account, path, form, now, session_id)
    if parts == ["account", "claim"]:
        return _claim(store, cfg, account, now, form)
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


def _claim(store: Store, cfg: Config, account: Mapping[str, Any], now: float,
           form: Optional[Mapping[str, str]] = None) -> Outcome:
    if wallet_routes.enabled(cfg):
        return _back("wallet-purchase-required", "wallet")
    node_id = None
    number = (form or {}).get("machine_number", "")
    configured = any(node.get("offer") is not None for node in store.list_nodes())
    if number or configured:
        if not re.fullmatch(r"[1-9][0-9]{0,9}", number):
            return _back("no-slot")
        candidates = offer_ui.available(store, account, now, cfg.heartbeat_max_age_s)
        node_id = next((node["id"] for node in candidates
                        if status.public_machine_number(node) == int(number)), None)
        if node_id is None:
            return _back("no-slot")
    try:
        # Only a machine heard from recently: handing out a slot on one that
        # has gone quiet leaves somebody watching "setting up" for half an hour.
        slot = store.claim_slot(account["id"], now=now, node_id=node_id,
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
            if not wallet_access.allowed(store, slot, now=now):
                return _back("wallet-expired", anchor)
            token = store.request_cli_pairing(slot_id, holder, now=now,
                admit=lambda row: wallet_access.allowed(store, row, now=time.time()))
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
    except SubscriptionReleaseBlocked:
        return _back("monthly-release-blocked", anchor)
    except (StoreError, slotstates.TransitionError):
        return _back("not-now", anchor)


# -- the page --------------------------------------------------------------------

# What the user site adds to the shared styles: its own frame (a bar on top and a
# footer, the same on every page people are sent to), the account page, the
# doors, and the progress a slot shows while the machine works on it.
USER_CSS = CSS + """
.devin-connection{border:1px solid var(--rule);border-radius:12px;padding:0 16px 16px;margin:16px 0}
.devin-connection p{max-width:66ch}
.provider-other{margin:16px 0;border-top:1px solid var(--rule);padding-top:12px}
.provider-other>summary{cursor:pointer;font-weight:650}
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
.sitefoot-identity{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
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
.site .usage-ranking{margin-top:44px;padding:24px}
.usage-ranking h2{margin:0;font-size:23px;letter-spacing:-.02em}
.usage-ranking p{font-size:13px;line-height:1.65;color:var(--muted);margin:10px 0}
.usage-ranking .usage-ranking-lead{margin:6px 0 0}
.usage-ranking-sheet{max-width:100%;overflow-x:auto;border:1px solid var(--rule-soft);
border-radius:14px;margin:18px 0 12px}
.usage-ranking table{width:100%;min-width:440px;border-collapse:collapse;font-size:14px}
.usage-ranking th,.usage-ranking td{padding:15px 16px;text-align:left;vertical-align:top;
border-bottom:1px solid var(--rule-soft);font-variant-numeric:tabular-nums}
.usage-ranking thead th{background:var(--inset);font-size:11px;color:var(--muted);
letter-spacing:.04em;white-space:nowrap}
.usage-ranking tbody th{background:none;text-transform:none}
.usage-ranking .usage-ranking-row th,.usage-ranking .usage-ranking-row td{border-bottom:0}
.usage-ranking .usage-rank{width:72px;color:var(--muted);font-size:12px}
.usage-ranking .usage-user{min-width:72px;padding-top:18px}
.usage-ranking .usage-observed{min-width:115px}
.usage-ranking .usage-observed>strong,.usage-estimate-value{font-size:19px;font-weight:700;
line-height:1.3;color:var(--ink);white-space:nowrap}
.usage-ranking small{display:block;margin-top:5px;font-size:11.5px;line-height:1.5;
color:var(--muted)}
.usage-ranking .usage-estimate{min-width:180px}
.usage-estimate-value{display:flex;align-items:center;gap:8px}
.usage-ranking .usage-badge{display:inline-flex;font-size:10px;font-weight:650;line-height:1.4;
padding:2px 6px;border-radius:999px;color:var(--acc);background:var(--acc-soft)}
.usage-ranking .usage-ranking-detail>td{padding:0 16px 12px}
.usage-ranking details{text-transform:none;font-weight:400}
.usage-ranking summary{cursor:pointer;color:var(--acc);font-size:12px;line-height:1.6}
.usage-ranking .usage-models{display:grid;gap:10px;margin:12px 0 0}
.usage-model{padding:14px 16px;border:1px solid var(--rule-soft);border-radius:10px;
background:var(--inset)}
.usage-model-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px}
.usage-model-name{font-size:13px;font-weight:650;overflow-wrap:anywhere}
.usage-model-value{font-size:14px;font-weight:650;white-space:nowrap}
.usage-token-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:12px 0}
.usage-token-grid dt{font-size:10.5px;color:var(--muted);text-transform:none;letter-spacing:0}
.usage-token-grid dd{margin:3px 0 0;font-size:14px;color:var(--ink);font-weight:600}
.usage-ranking .usage-method{border-top:1px solid var(--rule-soft);padding-top:10px;margin-top:12px}
.usage-ranking .usage-units{font-size:11px;margin:0}
.usage-ranking .usage-scope{color:var(--ink);font-size:12px}
.usage-ranking .usage-token{font-variant-numeric:tabular-nums}
.usage-ranking .usage-ranking-empty{padding:20px;color:var(--muted)}
@media(max-width:640px){.site .usage-ranking{padding:18px 14px}
.usage-ranking th,.usage-ranking td{padding:13px 10px}
.usage-ranking .usage-ranking-detail>td{padding:0 10px 10px}
.usage-ranking h2{font-size:21px}.usage-model{padding:12px}
.usage-token-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.usage-ranking .usage-observed>strong,.usage-estimate-value{font-size:17px}}
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
.site .topbar .doc-nav{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
text-align:center}
.site .page{padding-block:24px 48px;padding-left:16px;padding-right:16px}
.card.slot{scroll-margin-top:124px}
.slot-head,.slot-body{padding-left:16px;padding-right:16px}
.usage{grid-template-columns:minmax(0,1fr);padding:14px}
.ways{grid-template-columns:minmax(0,1fr)}
.door{padding:22px 20px 20px}
.sitefoot-in{padding-left:16px;padding-right:16px}}
""" + status.LINE_CSS + """
.account-nav{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 20px}
.account-nav [aria-current=page]{background:var(--acc-soft);color:var(--acc);
border-color:var(--acc-line)}
.slot-browse-link{min-width:0}.slot-browse-link p{margin:0 0 8px}

/* Account setup: one clear action per slot; details stay available on demand. */
.account-dashboard{min-width:0}
.account-dashboard .account-heading{display:flex;justify-content:space-between;
align-items:center;gap:20px;margin-bottom:24px}
.account-dashboard .account-heading>div{min-width:0}
.account-dashboard .account-eyebrow{margin:0 0 8px;color:var(--acc);font-size:11px;
font-weight:750;letter-spacing:.14em}
.account-dashboard .account-heading h1{font-size:32px;letter-spacing:-.035em}
.account-dashboard .account-heading .sub{margin-top:10px;line-height:1.6}
.account-dashboard .account-heading>.btn{flex-shrink:0;font-size:13px}
.account-dashboard .account-nav{margin-bottom:18px}
.account-dashboard .card.allowance{padding:14px 18px;margin-bottom:22px;box-shadow:none;
border-color:var(--rule-soft);gap:12px 20px}
.account-dashboard .allowance h2{font-size:12px;font-weight:650;color:var(--muted)}
.account-dashboard .allowance p{font-size:13px;line-height:1.6}
.account-dashboard .allowance .btn{font-size:13px;padding:9px 14px}
.account-dashboard .slot-browse-link{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.account-dashboard .slot-browse-link p{margin:0;font-size:12px}
.account-dashboard .slots{grid-template-columns:repeat(2,minmax(0,1fr));
align-items:start;gap:20px}
.account-dashboard .slots>.card.slot{min-width:0;margin:0;box-shadow:var(--shadow)}
.account-dashboard .slot-head{padding:20px 20px 17px;background:var(--panel)}
.account-dashboard .slot-head h2{font-family:var(--sans);font-size:20px;
letter-spacing:-.025em;overflow-wrap:anywhere}
.account-dashboard .slot-head .pill{font-size:11px;letter-spacing:0;padding:5px 8px}
.account-dashboard .slot-body{padding:4px 20px 12px;min-width:0}
.account-dashboard .slot-body p{font-size:13px;line-height:1.65;overflow-wrap:anywhere}
.account-dashboard .slot-connect{margin:14px 0;border:1px solid var(--acc-line);
border-radius:12px;overflow:hidden;background:var(--panel)}
.account-dashboard .slot-connect>summary{display:flex;align-items:center;
justify-content:space-between;gap:14px;list-style:none;padding:14px 16px;cursor:pointer;
background:var(--acc);color:var(--on-acc);font-size:14px;font-weight:650;line-height:1.5}
.account-dashboard .slot-connect>summary:hover{background:var(--acc-strong)}
.account-dashboard .slot-connect>summary:focus-visible{outline:2px solid var(--on-acc);
outline-offset:-5px}
.account-dashboard .slot-connect>summary::-webkit-details-marker{display:none}
.account-dashboard .slot-connect>summary::marker{content:""}
.account-dashboard .connect-arrow{font-size:20px;line-height:1;flex-shrink:0}
.account-dashboard .slot-connect[open]>summary .connect-arrow{transform:rotate(90deg)}
.account-dashboard .connect-body{padding:4px 16px 16px;min-width:0}
.account-dashboard .connect-body>p{margin:12px 0}
.account-dashboard .connect-steps{list-style:decimal;
margin:18px 0 0;padding:0 0 0 23px}
.account-dashboard .connect-steps>li{display:list-item;
margin:0 0 18px;padding-left:5px}
.account-dashboard .connect-steps>li:last-child{margin-bottom:10px}
.account-dashboard .connect-steps>li::marker{color:var(--acc);font-size:13px;font-weight:750}
.account-dashboard .connect-steps>li>div{min-width:0}
.account-dashboard .connect-steps b{display:block;font-size:13px;line-height:25px}
.account-dashboard pre{max-width:100%;min-width:0;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-word;font-size:12px;line-height:1.7;
padding:12px 13px;border-radius:9px;background:var(--inset)}
.account-dashboard .connect-steps pre{margin:8px 0 0}
.account-dashboard code{white-space:normal;overflow-wrap:anywhere;word-break:break-word}
.account-dashboard pre code{white-space:pre-wrap}
.account-dashboard .connect-hint{color:var(--muted);padding-top:12px;
border-top:1px solid var(--rule-soft)}
.account-dashboard .connect-first-time{margin:14px 0;padding:10px 12px;
border:1px solid var(--rule-soft);border-radius:9px;background:var(--inset)}
.account-dashboard .connect-first-time>summary{cursor:pointer;font-size:12px;font-weight:600}
.account-dashboard .devin-optional{margin:12px 0;padding:0;border:0}
.account-dashboard .provider-optional{border:1px solid var(--acc-line);
border-radius:12px;background:var(--panel);overflow:hidden}
.account-dashboard .provider-optional>summary{display:flex;align-items:center;gap:12px;
min-height:52px;padding:12px 14px;list-style:none;cursor:pointer;color:var(--acc);
font-size:13px;font-weight:650;line-height:1.5;transition:background .15s,border-color .15s}
.account-dashboard .provider-optional>summary:hover{background:var(--acc-soft)}
.account-dashboard .provider-optional>summary:focus-visible{outline:2px solid var(--acc);
outline-offset:-4px}
.account-dashboard .provider-optional>summary::-webkit-details-marker{display:none}
.account-dashboard .provider-optional>summary::marker{content:""}
.account-dashboard .provider-add-icon{display:grid;place-items:center;flex:0 0 26px;
width:26px;height:26px;border-radius:8px;background:var(--acc-soft);font-size:20px;
font-weight:400;line-height:1}
.account-dashboard .provider-add-label{flex:1;min-width:0;overflow-wrap:anywhere}
.account-dashboard .provider-add-arrow{flex-shrink:0;font-size:24px;font-weight:400;line-height:1}
.account-dashboard .provider-optional[open]>summary{background:var(--acc-soft)}
.account-dashboard .provider-optional[open]>summary .provider-add-arrow{transform:rotate(90deg)}
.account-dashboard .provider-add-body{padding:0 14px 10px;border-top:1px solid var(--acc-line)}
.account-dashboard .slot-disclosure,.account-dashboard .provider-other{margin:0;
padding:0;border:0;border-top:1px solid var(--rule-soft)}
.account-dashboard .slot-disclosure>summary,.account-dashboard .provider-other>summary{
display:flex;align-items:center;justify-content:space-between;gap:12px;list-style:none;
padding:14px 0;cursor:pointer;font-size:12px;font-weight:600;color:var(--muted)}
.account-dashboard .slot-disclosure>summary::-webkit-details-marker,
.account-dashboard .provider-other>summary::-webkit-details-marker{display:none}
.account-dashboard .slot-disclosure>summary::marker,
.account-dashboard .provider-other>summary::marker{content:""}
.account-dashboard .slot-disclosure>summary::after,
.account-dashboard .provider-other>summary::after{content:"+";font-size:17px;font-weight:400}
.account-dashboard .slot-disclosure[open]>summary,
.account-dashboard .provider-other[open]>summary{color:var(--ink)}
.account-dashboard .slot-disclosure[open]>summary::after,
.account-dashboard .provider-other[open]>summary::after{content:"−"}
.account-dashboard .disclosure-body{padding:0 0 12px;min-width:0}
.account-dashboard .slot-return>summary{font-size:11px}
.account-dashboard .slot-return>summary:hover{color:var(--bad)}
.account-dashboard .slot-return[open]>summary{color:var(--bad)}
.account-dashboard .slot-return .row-line.release{margin:0;padding:4px 0 8px;border:0}
.account-dashboard .row-line,.account-dashboard .actions{min-width:0;max-width:100%;
flex-wrap:wrap;gap:9px}
.account-dashboard .row-line form.inline{min-width:0;max-width:100%;flex-wrap:wrap}
.account-dashboard .row-line form.inline input{flex:1 1 160px}
.account-dashboard input,.account-dashboard select,.account-dashboard textarea{
min-width:0;max-width:100%}
.account-dashboard .btn,.account-dashboard button{white-space:normal;overflow-wrap:anywhere}
.account-dashboard .usage{grid-template-columns:minmax(0,1fr);padding:12px}
.account-dashboard .account-advanced{margin:20px 0 0;border:1px solid var(--rule-soft);
border-radius:12px;background:var(--panel);min-width:0}
.account-dashboard .account-advanced>summary{padding:16px 20px;cursor:pointer;
font-size:13px;font-weight:600;color:var(--muted)}
.account-dashboard .account-advanced[open]>summary{color:var(--ink)}
.account-dashboard .account-advanced>p,
.account-dashboard .account-advanced>div:not(.gateway-advanced-body){
margin:0 20px 18px;font-size:13px;line-height:1.7}
@media(max-width:860px){
.account-dashboard .slots{grid-template-columns:minmax(0,1fr);gap:18px}}
@media(max-width:760px){
.site:has(.account-dashboard) .topbar .doc-nav{display:flex;flex-wrap:nowrap;
overflow-x:auto;justify-content:flex-start}
.site:has(.account-dashboard) .topbar .doc-nav a{white-space:nowrap}}
@media(max-width:520px){
.account-dashboard .account-heading{align-items:flex-start;gap:12px}
.account-dashboard .account-heading h1{font-size:28px}
.account-dashboard .account-heading .sub{font-size:13px}
.account-dashboard .account-heading>.btn{font-size:12px;padding:7px 10px}
.account-dashboard .card.allowance{padding:13px 16px}
.account-dashboard .slot-head{padding:17px 16px 15px}
.account-dashboard .slot-body{padding:3px 16px 10px}
.account-dashboard .slot-connect>summary{padding:13px;font-size:13px}
.account-dashboard .connect-body{padding:3px 12px 12px}
.account-dashboard .connect-steps b{line-height:23px}
.account-dashboard .connect-steps pre{padding:10px;font-size:12.5px}
.account-dashboard .account-advanced>summary{padding:15px 16px}}
"""

#: The pages anybody can read, in the order the bar on top shows them.
NAV = (("/", "Overview"), ("/slots", "Slots & pricing"), ("/docs/guide", "Guide"),
       ("/docs/how-it-works", "How it works"), ("/status", "Status"),
       ("/privacy", "Privacy"),
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
    nav_zh = {"Overview": "概览", "Slots & pricing": "槽位与价格",
              "Guide": "使用指南", "How it works": "工作原理",
              "Status": "服务状态", "Privacy": "隐私", "Terms": "条款"}
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
                f"<style>{USER_CSS}{BILINGUAL_CSS}{offer_ui.CSS}{extra_css}</style></head"
                f">"
                '<body class="site" data-language="en"><header '
                'class="topbar"><div class="topbar-in">'
                f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
                + offer_ui.alpha_label() +
                f'<nav class="doc-nav">{links}</nav>'
                f'<div class="topbar-end">{CONTROLS}{corner}</div></div></h'
                f'eader>'
                f'<main class="page {escape(width)}">{body}</main>'
                '<footer class="sitefoot"><div class="sitefoot-in"><div>'
                '<div class="sitefoot-identity">'
                f'<a class="brand" href="/">{MARK}<span>ccfleet</span></a>'
                + offer_ui.alpha_label() +
                "</div><p>"
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
                                           ("/slots", "Slots & pricing", "槽位与价格"),
                                           ("/docs/guide", "Guide", "使用指南"),
                                           ("/docs/how-it-works", "How it works", "工作原理"),
                                           ("/privacy", "Privacy", "隐私"),
                                           ("/docs/terms", "Terms", "条款"),
                                           ("/status", "Status", "服务状态")))
            + '<a href="https://github.com/cdcupt/ccfleet" target="_blank" '
            'rel="noopener noreferrer">' + label("Source code", "源代码", inline=True)
            + '</a></nav></div></footer>' + LOCAL_TIMES_TAG + SCRIPT_TAG
            + (QUOTA_POLL_TAG + PANEL_ACTION_TAG if title == "your slots" else "")
            + '</body></html>')


_HERE = ' class="here"'


def _public_usage_count(value: Any) -> Optional[int]:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _public_usage_number(value: Any) -> str:
    count = _public_usage_count(value)
    return f"{count:,}" if count is not None else "—"


def _public_usage_token(value: Any) -> str:
    """Compact decimal units with exact, accessible counts and integer rounding."""
    count = _public_usage_count(value)
    if count is None:
        return label("—", "—", inline=True)
    shown = str(count)
    if count >= 1000:
        units = ((1000, "K"), (1_000_000, "M"), (1_000_000_000, "B"))
        index = max(index for index, (scale, _) in enumerate(units) if count >= scale)
        scale, suffix = units[index]
        rounded = (count * 100 + scale // 2) // scale
        if rounded >= 100_000 and index < len(units) - 1:
            scale, suffix = units[index + 1]
            rounded = (count * 100 + scale // 2) // scale
        whole, fraction = divmod(rounded, 100)
        shown = str(whole) + ("." + f"{fraction:02}".rstrip("0") if fraction else "") + suffix
    english, chinese = f"{count:,} tokens", f"{count:,} Token"
    return (f'<span class="usage-token" title="{english}" aria-label="{english}" '
            f'data-title-en="{english}" data-title-zh="{chinese}" '
            f'data-aria-label-en="{english}" data-aria-label-zh="{chinese}">{shown}</span>')


def _public_usage_price_known(reading: Mapping[str, Any]) -> bool:
    value = reading.get("estimated_usd")
    try:
        return (reading.get("cost_state") in ("complete", "partial")
                and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**15)
    except (OverflowError, ValueError):
        return False


def _public_usage_value(reading: Mapping[str, Any]) -> str:
    value = reading.get("estimated_usd")
    if not _public_usage_price_known(reading):
        return label("—", "—", inline=True)
    return "&lt;$0.01" if 0 < value < 0.01 else f"${value:,.2f}"


def _public_usage_badge(reading: Mapping[str, Any]) -> str:
    return ('<span class="usage-badge">' + label("Partial", "部分覆盖", inline=True) + '</span>'
            if reading.get("cost_state") == "partial" else '')


def _public_usage_reference(reading: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """A common conversion must cover exactly the displayed observed categories."""
    tokens = reading.get("reference_tokens")
    if (reading.get("reference_model") != REFERENCE_MODEL or not isinstance(tokens, Mapping)
            or tokens != reading.get("tokens")
            or not all(field in tokens for field in REFERENCE_RATES)
            or _public_usage_count(tokens.get("total_tokens")) is None
            or not _public_usage_count(reading.get("usage_samples"))
            or reading.get("reference_cost_state") != reading.get("token_state")):
        return None
    for field in REFERENCE_RATES:
        count = tokens.get(field)
        if count is None and field in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            continue
        if _public_usage_count(count) is None:
            return None
    if tokens["total_tokens"] != sum(tokens[field] or 0 for field in REFERENCE_RATES):
        return None
    quote = {"estimated_usd": reading.get("reference_estimated_usd"),
             "cost_state": reading.get("reference_cost_state")}
    if (not _public_usage_price_known(quote)
            or quote["cost_state"] == "complete"
            and any(tokens[field] is None for field in REFERENCE_RATES)):
        return None
    expected = math.fsum((tokens[field] or 0) * rate
                         for field, rate in REFERENCE_RATES.items()) / 1_000_000
    return (quote if math.isclose(quote["estimated_usd"], expected,
                                  rel_tol=1e-12, abs_tol=1e-12) else None)


def _public_usage_coverage(reading: Mapping[str, Any]) -> str:
    measured = _public_usage_number(reading.get("usage_samples"))
    eligible = _public_usage_number(reading.get("usage_eligible_messages"))
    priced = _public_usage_number(reading.get("priced_samples"))
    return label(f"Measured: {measured} of {eligible} replies · "
                 f"Reported-model price matched: {priced} of {measured}",
                 f"已观测回复：{measured}／{eligible} · 上报模型计价已匹配：{priced}／{measured}",
                 inline=True)


def _public_usage_models(models: Any, reading: Mapping[str, Any]) -> str:
    priced_rows, unpriced_rows = [], []
    for model in models[:16] if isinstance(models, list) else []:
        if not isinstance(model, Mapping):
            continue
        key = model.get("model")
        title = (escape(LABELS[key]) if isinstance(key, str) and key in LABELS
                 and key != "other" else label("Other / unknown", "其他／未知", inline=True))
        tokens = model.get("tokens")
        tokens = tokens if isinstance(tokens, Mapping) else {}
        sample_text = _public_usage_number(model.get("samples"))
        read_text = _public_usage_number(model.get("cache_read_samples"))
        write_text = _public_usage_number(model.get("cache_creation_samples"))
        grid = ''.join('<div><dt>' + label(en, zh, inline=True) + '</dt><dd>'
                       + _public_usage_token(tokens.get(field)) + '</dd></div>'
                       for field, en, zh in (
                           ("input_tokens", "Input", "输入"),
                           ("output_tokens", "Output", "输出"),
                           ("cache_read_input_tokens", "Cache read", "缓存读取"),
                           ("cache_creation_input_tokens", "Cache write", "缓存写入")))
        known = (_public_usage_price_known(model)
                 and _public_usage_count(tokens.get("total_tokens")) is not None)
        value = _public_usage_value(model) if known else label("—", "—", inline=True)
        paired = ('<small>' + label("API estimate for ", "API 估值对应 ", inline=True)
                  + _public_usage_token(tokens.get("total_tokens"))
                  + label(" tokens", " Token", inline=True) + '</small>' if known else '')
        (priced_rows if known else unpriced_rows).append(
                    '<div class="usage-model"><div class="usage-model-head">'
                    '<span class="usage-model-name">' + title + '</span>'
                    '<span class="usage-model-value">' + value + '</span></div>' + paired
                    + '<dl class="usage-token-grid">' + grid + '</dl><small>'
                    + label(f"{sample_text} replies · Cache field coverage: read "
                            f"{read_text}/{sample_text} · write {write_text}/{sample_text}",
                            f"{sample_text} 个回复 · 缓存字段覆盖："
                            f"读取 {read_text}／{sample_text} · "
                            f"写入 {write_text}／{sample_text}", inline=True)
                    + '</small></div>')
    priced = reading.get("priced_tokens")
    priced = priced if isinstance(priced, Mapping) else {}
    matched = (_public_usage_price_known(reading)
               and _public_usage_count(priced.get("total_tokens")) is not None)
    detail = ('<p><strong>' + label("Reported-model API estimate: ", "上报模型 API 估值：",
                                   inline=True) + '</strong>' + _public_usage_value(reading)
              + _public_usage_badge(reading) + ' · ' + label("for ", "对应 ", inline=True)
              + _public_usage_token(priced.get("total_tokens"))
              + label(" priced tokens", " 已计价 Token", inline=True) + '</p>' if matched else '')
    detail += ('<p>' + label("Priced model breakdown", "已计价模型明细", inline=True)
              + '</p><div class="usage-models">' + ''.join(priced_rows) + '</div>'
              if priced_rows else '<p>' + label(
                  "Reported-model pricing has no matched observations yet. The ranking still "
                  "converts recorded tokens at the common Opus 5.5 reference rates.",
                  "上报模型计价暂无匹配观测；排行仍按统一的 Opus 5.5 参考价格折算已记录 Token。",
                  inline=True) + '</p>')
    outside = ('<p>' + label("Model observations without an reported-model price",
                            "暂无上报模型价格的观测", inline=True)
               + '</p><div class="usage-models">' + ''.join(unpriced_rows) + '</div>'
               if unpriced_rows else '')
    totals = reading.get("tokens")
    totals = totals if isinstance(totals, Mapping) else {}
    unpriced = reading.get("unpriced_tokens")
    unpriced = unpriced if isinstance(unpriced, Mapping) else {}
    reported = ''.join('<div><dt>' + label(en, zh, inline=True) + '</dt><dd>'
                       + _public_usage_token(totals.get(field)) + '</dd></div>'
                       for field, en, zh in (
                           ("input_tokens", "Input", "输入"), ("output_tokens", "Output", "输出"),
                           ("cache_read_input_tokens", "Cache read", "缓存读取"),
                           ("cache_creation_input_tokens", "Cache write", "缓存写入")))
    aggregate = '<div class="usage-model"><span class="usage-model-name">' + label(
        "Recorded usage used for the common reference",
        "统一参考折算使用的已记录用量",
        inline=True)
    aggregate += '</span><p>' + label("Reported total ", "上报总量 ", inline=True)
    aggregate += _public_usage_token(totals.get("total_tokens")) + ' · ' + label(
        "Without matched model price ", "未匹配上报模型价格 ", inline=True)
    aggregate += _public_usage_token(unpriced.get("total_tokens"))
    aggregate += '</p><small>' + label(
        "The ranking converts these recorded categories at one reference price. Reported-model "
        "estimates above cover only matched model observations.",
        "排行按同一参考价格折算这些已记录分类；上方上报模型估值只覆盖已匹配的模型观测。",
        inline=True) + '</small>'
    aggregate += '<dl class="usage-token-grid">' + reported + '</dl></div>'
    return ('<details class="usage-model-detail"><summary>'
            + label("Token & model details", "Token 与模型明细", inline=True) + '</summary>'
            + detail + aggregate + outside + '<p>' + _public_usage_coverage(reading)
            + '</p></details>')


def _public_usage_method(report: Mapping[str, Any]) -> str:
    source = '<details class="usage-method"><summary>' + label(
        "Method, coverage & prices", "统计范围与价格", inline=True) + '</summary>'
    source += '<p>' + _public_usage_coverage(report) + '</p><p>' + label(
        "Think of one price list applied to everyone's recorded tokens. The ranking uses "
        "Opus 5.5 standard global API rates per million tokens: input $4, output $20, "
        "cache write $5 (5 minutes), cache read $0.20. Each category is counted once. "
        "This common conversion also works when the original model is missing; it does not "
        "claim that Opus served those replies. Missing replies or cache fields make the "
        "observed subtotal partial; missing counts are not filled with zero. No measurement "
        "means no numeric rank. Names prefer a masked email local-part fragment, such as "
        "cd•••pt, without its domain; unsafe or short addresses use a partly masked nickname "
        "or member code. These fragments are public; this is not anonymity.",
        "可以理解为：给每个人已记录的 Token 套用同一张价目表。排行统一采用 Opus 5.5 标准全球 "
        "API 参考价格，每百万 Token：输入 $4、输出 $20、缓存写入 $5（5 分钟）、缓存读取 "
        "$0.20，每类只计一次。即使缺少原模型信息，也能统一折算；这不代表这些回复由 Opus "
        "生成。缺少回复或缓存字段时，显示已观测到的部分小计，不把缺失数量补成零。没有用量"
        "观测时不显示数字排名。名称优先展示邮箱用户名的局部遮盖片段，例如 cd•••pt，"
        "不展示邮箱域名；不适合安全遮盖的短邮箱等情况使用遮盖昵称或用户编号。"
        "这些片段会公开，这不等于匿名。",
        inline=True) + '</p><p>' + label(
        "The reference value is not a Claude Pro/Max bill, an amount paid, a quota balance, "
        "unlimited usage or guaranteed savings. Reported-model API estimates stay in the "
        "details and use only matched model observations. Model-specific estimates use the "
        "model names reported by the agent. "
        "Haiku 5.5 has no reported-model "
        "estimate because its per-request prompt tier is absent. Actual cache TTL, context "
        "and service tiers can change API pricing. These statistics retain no raw replies "
        "and do not reconstruct older model identities. Reading this page makes no model call. ",
        "参考折算值不是 Claude Pro/Max 账单、实际付款、剩余额度、无限用量，也不保证节省了这些"
        "费用。按上报模型计价的 API 估值保留在明细中，只使用已匹配模型的观测；模型名称由代理"
        "上报。由于缺少每次请求的"
        "提示长度计价档位，Haiku 5.5 暂无上报模型估值。实际缓存有效期、上下文和服务档位"
        "可能影响 API 价格。统计不保留原始回复，也不重建旧模型身份。阅读本页不会调用模型。",
        inline=True)
    source += '<a href="/privacy">' + label("Public ranking privacy", "公开排行隐私说明",
                                          inline=True) + '</a>.</p>'
    pricing_ = report.get("reference_pricing")
    if isinstance(pricing_, Mapping):
        reference_day = pricing_.get("date")
        if (isinstance(reference_day, str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", reference_day)):
            source += '<p>' + label("Price reference: " + reference_day,
                                   "价格参考日期：" + reference_day, inline=True) + '</p>'
        links = []
        for key, expected, en, zh in (("url", PRICING_URL, "Official API prices", "官方 API 价格"),
                                      ("models_url", MODELS_URL, "Model catalogue", "模型目录")):
            if pricing_.get(key) == expected:
                links.append('<a href="' + escape(expected, quote=True) + '">'
                             + label(en, zh, inline=True) + '</a>')
        if links:
            source += '<p>' + ' · '.join(links) + '</p>'
    return source + '</details>'


def public_usage_panel(ranking: Any) -> str:
    """Pair a common reference value with its full matching observed token scope."""
    report = ranking if isinstance(ranking, Mapping) else {}
    entries = report.get("entries") if report.get("state") in ("partial", "ready") else []
    rows = []
    for position, entry in enumerate(entries[:50] if isinstance(entries, list) else [], 1):
        if not isinstance(entry, Mapping):
            continue
        handle = entry.get("masked_handle")
        email = entry.get("masked_email")
        code = entry.get("member_code")
        code = code if isinstance(code, str) and re.fullmatch(r"[0-9]{8}", code) else None
        member = label("Member ", "用户 ", inline=True) + "••" + (code or f"{position:02d}")
        masked = (escape(handle) if isinstance(handle, str) and len(handle) <= 40
                  and handle.strip("*")
                  and re.fullmatch(r"[a-z0-9-]{0,2}\*{1,32}[a-z0-9-]{0,2}", handle)
                  else member)
        if (type(email) is str and re.fullmatch(
                r"(?:[A-Za-z0-9._%+-]{2}•••[A-Za-z0-9._%+-]{2}|"
                r"[A-Za-z0-9._%+-]•••[A-Za-z0-9._%+-])", email)):
            masked = escape(email)
        rank = _public_usage_count(entry.get("rank"))
        reference = _public_usage_reference(entry)
        known = reference is not None
        rank_text = (str(rank) if known and rank is not None and 1 <= rank <= 50
                     else label("Not ranked yet", "暂未排名", inline=True))
        recorded = entry.get("tokens")
        recorded = recorded if isinstance(recorded, Mapping) else {}
        recorded_known = _public_usage_count(recorded.get("total_tokens")) is not None
        value = (_public_usage_value(reference) if known else
                 label("Reference unavailable", "暂无参考折算", inline=True) if recorded_known else
                 label("No measurement yet", "暂无观测", inline=True))
        if known:
            scope = label("Opus 5.5 reference · same rates for everyone",
                          "Opus 5.5 参考折算 · 所有人采用同一价格", inline=True)
            token_scope = label("Recorded tokens", "已记录 Token", inline=True)
        elif recorded_known:
            scope = label("A matching reference conversion is not available yet.",
                          "暂缺与这些用量匹配的参考折算。", inline=True)
            token_scope = label("Recorded tokens", "已记录 Token", inline=True)
        else:
            scope = label("No token measurement is available yet.",
                          "暂缺 Token 用量观测。", inline=True)
            token_scope = label("No measurement yet", "暂无观测", inline=True)
        badge = _public_usage_badge(reference) if known else ''
        total = recorded.get("total_tokens")
        amount = (_public_usage_token(total) if _public_usage_count(total) is not None else
                  label("Usage unavailable", "暂无用量信息", inline=True))
        rows.append('<tr class="usage-ranking-row"><td class="usage-rank">' + rank_text
                    + '</td><th class="usage-user" scope="row">' + masked
                    + '</th><td class="usage-observed"><strong>'
                    + amount + '</strong><small>' + token_scope + '</small></td>'
                    '<td class="usage-estimate"><div class="usage-estimate-value">'
                    + value + badge + '</div><small class="usage-estimate-scope">' + scope
                    + '</small></td></tr><tr class="usage-ranking-detail"><td colspan="4">'
                    + _public_usage_models(entry.get("models"), entry) + '</td></tr>')
    empty = ('<tr><td colspan="4" class="usage-ranking-empty">' + label(
        "No publishable usage observations yet. New valid reports are needed; unknown is not zero.",
        "暂无可公开的用量观测。需要新的有效报告；未知不是零。", inline=True) + '</td></tr>')
    source = ('<section class="card usage-ranking" id="usage-ranking" '
              'aria-labelledby="usage-ranking-title"><h2 id="usage-ranking-title">')
    source += label("Measured usage ranking", "观测用量排行", inline=True) + '</h2>'
    source += '<p class="usage-ranking-lead">' + label(
        "Last 7 UTC days · ranked by one common Opus 5.5 API reference",
        "最近 7 个 UTC 日期 · 统一按 Opus 5.5 API 参考折算排名",
        inline=True) + '</p><p class="usage-scope">' + label(
        "Each row is a real user with recorded activity. We apply the same reference prices "
        "to everyone's recorded input, output and cache tokens, even when the original model "
        "was not recorded. Dollar values cover the tokens shown in the same row.",
        "每一行都来自有实际活动的真实用户。我们用同一参考价格折算每个人已记录的输入、输出"
        "和缓存 Token，即使没有记录原模型，也能参与排行。美元折算值对应同一行展示的用量。",
        inline=True) + '</p>'
    source += '<p>' + label(
        "Reported processed tokens include cached context counted again on each request; "
        "this is not generated or unique text.",
        "已报告的处理 Token 包含每次请求重复计数的缓存上下文，不等于生成或独有的文本。",
        inline=True) + '</p>'
    source += ('<div class="usage-ranking-sheet" role="region" '
               'aria-labelledby="usage-ranking-title" tabindex="0"><table><thead><tr>'
               + ''.join('<th scope="col">' + label(en, zh, inline=True) + '</th>' for en, zh in (
                   ("Rank", "排名"), ("User", "用户"), ("Tokens", "Token 用量"),
                   ("Opus 5.5 equivalent · USD", "Opus 5.5 折算 · 美元")))
               + '</tr></thead><tbody>' + (''.join(rows) if rows else empty)
               + '</tbody></table></div><p class="usage-units">' + label(
                   "K = thousand · M = million · B = billion. Exact counts on hover.",
                   "K＝千 · M＝百万 · B＝十亿。悬停可查看精确数量。", inline=True) + '</p>')
    omitted = _public_usage_count(report.get("omitted_entries"))
    if rows and omitted:
        source += '<p>' + label(
            f"Showing the first 50 entries; {omitted:,} further entries omitted.",
            f"展示前 50 项；另有 {omitted:,} 项未列出。", inline=True) + '</p>'
    return source + _public_usage_method(report) + '</section>'

FORM_ZH = {
    "Claim a slot": "领取槽位", "Sign in again": "重新登录", "Sign in to Claude": "登录 Claude",
    "Change account": "更换账号", "Send code": "提交授权码", "Cancel": "取消",
    "Cancel setup": "取消设置", "Back to Stable": "切回稳定版", "Refresh": "刷新用量",
    "Rename": "重命名", "Give this slot back": "归还并清空槽位", "Remove": "移除电脑",
    "Connect another computer": "连接另一台电脑", "Connect this computer": "连接电脑",
    "Turn on": "开启", "Turn off": "关闭", "Sign out": "退出登录",
    "Update to the latest release": "更新到最新版",
}


def _form(action: str, csrf: str, words: str, inner: str = "", cls: str = "", *,
          partial: str = "") -> str:
    zh = FORM_ZH.get(words)
    if words.startswith("Update to ") and zh is None:
        zh = "更新到 " + words[len("Update to "): ]
    button = label(words, zh, inline=True) if zh is not None else escape(words)
    opt_in = f' data-fleet-action="{escape(partial)}"' if partial else ""
    return (f'<form class="inline" method="post" action="{escape(action)}"{opt_in}>'
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


def _wallet_panel(store, account, *, topup_ready: bool = False, csrf: str = "",
                  compact: bool = False) -> str:
    try:
        held = store.wallet_spending_blocked(account["id"])
        balance = wallet_ui.balance_card(store.wallet_balance(account["id"]),
            topup_ready=topup_ready and not held, compact=compact)
        return (balance + ('<p class="note-banner warn">'
                + label(*wallet_routes.ERRORS["hold"], inline=True) + '</p>' if held else ''))
    except Exception:
        # A failed financial read must never look like a zero/usable balance.
        return ('<section class="wallet-ui"><h2>' + label("Fleet credit", "Fleet 余额", inline=True)
                + '</h2><p>' + label("Credit is unavailable. Buying and renewing are paused.",
                "余额暂不可用，已暂停购买与续费。", inline=True) + '</p></section>')


def _wallet_history(store, account) -> str:
    try:
        body = wallet_ui.owner_ledger(store.list_wallet_entries(account["id"]))
    except Exception:
        body = ('<p>' + label("Credit history is unavailable.", "余额记录暂不可用。",
                             inline=True) + '</p>')
    return ('<details class="account-advanced wallet-history" id="credit-history"><summary>'
            + label("Credit history", "余额记录", inline=True) + '</summary>' + body + '</details>')


def page(store: Store, cfg: Config, account: Optional[Mapping[str, Any]],
         session_id: str, now: float, note: str = "", *, topup_ready: bool = False) -> str:
    """The whole page, for one person. Signed out, it offers sign-in and nothing else."""
    if account is None:
        return _signed_out(cfg)
    held = store.list_slots(held_by=account["id"])
    recurring_slots = set()
    if held:
        try:
            recurring_slots = {row["slot_id"] for row in store.list_slot_subscriptions(
                account["id"], limit=100) if row.get("status") != "closed"}
        except Exception:
            # Rendering never changes a contract. The Store's transactional
            # release guard remains authoritative if this read is unavailable.
            pass
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
    if any(node.get("offer") is not None for node in nodes.values()):
        available_count = len(offer_ui.available(
            store, account, now, cfg.heartbeat_max_age_s))
        claim = ('<div class="slot-browse-link"><p class="muted">'
                 + label(f"{available_count} available slots", f"{available_count} 个可用槽位",
                         inline=True)
                 + '</p><a class="btn primary" href="/account/available">'
                 + label("Browse available slots", "浏览可用槽位", inline=True) + '</a></div>')
    elif quota > counted:
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
    native_states = {s["id"]: devin_ui.native_state(store, cfg, account, s, now=now) for s in held}
    financial = {}
    for slot in held:
        try:
            recurring = (store.slot_subscription_access(account["id"], slot["id"],
                claim_id=slot["gateway_claim_id"], now=now)
                if slot.get("kind") != slotstates.OWNER_SLOT else None)
            if recurring is not None:
                end = wallet.instant(recurring["paid_through"])
                financial[slot["id"]] = (end if end > 0 else None,
                                          recurring["allowed"] is True)
                continue
            end = wallet_access.paid_through(store, slot)
            financial[slot["id"]] = (end, end is None or now < end)
        except Exception:
            financial[slot["id"]] = (None, False)
    pairing_slots = [s for s in held if s.get("kind") == slotstates.MACHINE_SLOT
                     and s["state"] == slotstates.ACTIVE and financial[s["id"]][1]
                     and nodes.get(s["node_id"], {}).get("access_host")
                     and nodes.get(s["node_id"], {}).get("ssh_host_key")]
    authorizer = next((s for s in pairing_slots if cli_devices[s["id"]]),
                      pairing_slots[0] if pairing_slots else None)
    cards = "".join(_slot_card(s, nodes.get(s["node_id"]) or {}, latest.get(s["node_id"]),
                               logins[s["id"]], csrf, cfg, now, flagged[s["id"]],
                               updates[s["id"]], channels, cli_devices[s["id"]],
                               machine_line=lines[s["id"]],
                               devin_state=native_states[s["id"]],
                               devin_html=devin_ui.native_details(store, cfg, account, s, now=now),
                               authorizer=authorizer,
                               payment_end=financial[s["id"]][0], paid_access=financial[s["id"]][1],
                               recurring=s["id"] in recurring_slots,
                               listening=None if s["node_id"] in silent else since)
                    for s in held)
    body = (
        '<section class="account-dashboard" data-account-layout="setup-first">'
        '<div class="pagehead account-heading"><div><p class="account-eyebrow">CC FLEET</p>'
        '<h1>' + label('Your slots', '我的槽位', inline=True) + '</h1>'
        '<p class="sub">' + label('Choose a slot. Connect your computer. Start coding.',
                                  '选一个槽位，连接电脑，开始写代码。', inline=True)
        + ' <a href="/account" data-fleet-status="account">'
        + label('Refresh status', '刷新状态', inline=True)
        + '</a></p></div><a class="btn quiet" href="/docs/guide">'
        + label('Setup help', '设置帮助', inline=True) + '</a></div>'
        + _account_nav("/account", monthly=_monthly_management(store, cfg, account)) + banner
        + (_wallet_panel(store, account, topup_ready=topup_ready, csrf=csrf, compact=bool(held))
           if wallet_routes.enabled(cfg) else
           '<div class="card allowance"><div><h2>'
           + label('Your allowance', '槽位额度', inline=True)
           + f'</h2>{allowance}</div>{claim}</div>')
        + (f'<div class="slots">{cards}</div>' if cards else "")
        + (_wallet_history(store, account) if wallet_routes.enabled(cfg) else "")
        + gateway_ui.panel(store, cfg, account, csrf, held)
        + ('<details class="account-advanced"><summary>'
           + label('Email notifications', '邮件通知', inline=True) + '</summary>'
           + _outage_emails(account, csrf) + '</details>' if cfg.emails_ready else "")
        + '<details class="account-advanced account-privacy"><summary>'
        + label('Where your data goes', '数据会发到哪里？', inline=True) + '</summary>'
        + (
            '<p class="note">Your slot is a Linux account on a '
            'machine we operate, with its own '
            'home and your own provider sign-in. Claude Code runs on your computer; '
            'a verified Devin slot uses native Devin CLI for model replies. '
            "Other people's slots on the "
            "machine cannot read yours; the machine's administrators "
            'technically can. This page '
            'never shows your files or conversations, and nothing '
            'here holds your provider '
            'credential: it is written on the machine when you sign '
            'in, and nowhere else.</p>'
        ))
    note_start = body.index('<p class="note">')
    body = body[:note_start] + _blocks(body[note_start:], [
        (
            '槽位是在我们运营的机器上的独立 Linux 账号，'
            '拥有自己的目录和服务商登录。Claude Code 在本机运行；通过验证的 Devin 槽位'
            '使用原生 Devin CLI 获取模型回复。'
            '普通用户的其他槽位无法读取你的文件，'
            '但机器管理员在技术上可以。本页不展示文件或对话。'
            '服务商登录凭据只写入槽位所在机器，Fleet '
            '服务器不保存它。'
        ),
    ]) + '</details></section>'
    # A quota request must not repeatedly erase open instructions or forms.
    # Native reads run independently; the user can check their status explicitly.
    return _shell("your slots", body,
                  _refresh(held, logins, idle_refresh=False),
                  viewer=viewer_for(account, session_id, cfg, store), width="wide",
                  extra_css=wallet_ui.CSS if wallet_routes.enabled(cfg) else "")


def slots_page(store: Store, cfg: Config, now: float, query: Mapping[str, str], *,
               viewer: Optional[Viewer] = None) -> str:
    """Public inventory without customer identities or claim authority."""
    body = ('<div class="pagehead"><h1>'
            + label("Slots & pricing", "槽位与价格", inline=True) + ' '
            + offer_ui.alpha_label() + '</h1><p class="sub">'
            + label("Compare providers, network types and current Alpha quotes.",
                    "比较服务商、网络类型与当前 Alpha 报价。", inline=True) + '</p><p>'
            + '<a href="/docs/library/docs/devin.html#prices">'
            + label('Compare Claude and Devin subscription prices',
                    '比较 Claude 与 Devin 订阅价格', inline=True)
            + '</a></p></div>'
            + offer_ui.public_browse(store, now, cfg.heartbeat_max_age_s, query))
    return _shell("Slots & pricing", body, here="/slots", viewer=viewer, width="wide")


def _monthly_management(store: Store, cfg: Config, account: Mapping[str, Any]) -> bool:
    """Enrollment is optional; existing own contracts keep a way to manage them."""
    if getattr(cfg, "stripe_subscriptions_enabled", False) is True:
        return True
    try:
        return bool(store.list_slot_subscriptions(account["id"], limit=1))
    except Exception:
        return False


def _account_nav(current: str, *, monthly: bool = False) -> str:
    pages = [("/account", "Your slots", "我的槽位"),
             ("/account/available", "Available slots", "可用槽位")]
    if monthly:
        pages.append(("/account/subscriptions", "Monthly payments", "月付管理"))
    return ('<nav class="account-nav" aria-label="Slot pages" '
            'data-aria-label-en="Slot pages" data-aria-label-zh="槽位页面">'
            + ''.join('<a class="btn" href="' + path + '"'
                      + (' aria-current="page"' if path == current else '') + '>'
                      + label(en, zh, inline=True) + '</a>'
                      for path, en, zh in pages)
            + '</nav>')


def available_page(store: Store, cfg: Config, account: Optional[Mapping[str, Any]],
                   session_id: str, now: float, query: Mapping[str, str]) -> str:
    """Only this customer's eligible pool; browsing never claims or starts a model."""
    if account is None:
        return _signed_out(cfg)
    held = store.list_slots(held_by=account["id"])
    quota = int(account.get("slot_quota") or 0)
    counted = sum(1 for slot in held if slot["state"] in slotstates.HELD)
    can_claim = wallet_routes.enabled(cfg) or counted < quota
    monthly = getattr(cfg, "stripe_subscriptions_enabled", False) is True
    note = ('' if can_claim or monthly else '<p class="offer-note">' + label(
        "You have no unused slot allowance. You can browse prices; contact the operator "
        "before claiming another slot.",
        "当前没有剩余槽位额度。你仍可浏览价格；领取其他槽位前请联系运营者。", inline=True) + '</p>')
    body = ('<div class="pagehead"><h1>'
            + label("Available slots", "可用槽位", inline=True) + '</h1><p class="sub">'
            + label("Search providers and network types, then choose a machine.",
                    "搜索服务商与网络类型，再选择主机。", inline=True) + '</p></div>'
            + _account_nav("/account/available",
                           monthly=_monthly_management(store, cfg, account)) + note
            + offer_ui.browse(store, account, csrf_for(session_id, cfg.cookie_secret), now,
                              cfg.heartbeat_max_age_s, query, can_claim=can_claim,
                              purchase=wallet_routes.enabled(cfg), monthly=monthly))
    return _shell("Available slots", body, viewer=viewer_for(account, session_id, cfg, store),
                  width="wide")


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
        'account there for your own provider sign-in. Use original Claude Code on your '
        'computer; choose Anthropic or a verified Devin pilot slot on this website. '
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
            '槽位保存你自己的服务商登录。原版 Claude Code 在本机运行；网页可以选择 '
            'Anthropic，或通过验证的 Devin 试点槽位。'
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
PRIVACY_UPDATED = "2026-10-20"


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
                    "for your own provider sign-in. Original Claude Code runs on your "
                    'computer. Anthropic is the default; verified own-slot Devin is a '
                    'pilot. The operator decides who gets '
                    f"slots. To reach the operator, write to {contact}."
                ),
                (
                    "Fleet 槽位是在我们运营的机器上的独立 Linux 账号，"
                    "保存你自己的服务商登录，原版 Claude Code 在你的电脑上运行。默认使用 "
                    "Anthropic；通过验证的自有槽位也能使用 Devin 试点。"
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
                    "and uses its selected provider account to "
                    "request a reply from Anthropic or native Devin. The reply returns "
                    "to your computer."
                ),
                (
                    "可以把 Fleet 看作接待台：电脑负责文件和工具操作，"
                    "需要模型回复时，把选定的上下文通过 HTTPS 发给 "
                    "Fleet。"
                    "Fleet 选择你指定的槽位，按设置使用自己的 Anthropic 或原生 Devin "
                    "账号请求回复，再把回复送回电脑。"
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
    body += card(
        "When you choose Devin", "选择 Devin 时", [
            (
                "Devin runs its own native agent on your slot. It receives the selected "
                "model context and returns replies and tool requests. The gateway and "
                "machine administrators can read that context. Read, Edit and Bash still "
                "run on your computer with Claude Code’s permissions.",
                "Devin 在你的槽位上运行自己的原生智能体，接收选定的模型上下文，返回回复和工具请求。"
                '网关和机器管理员能够读取这些上下文。Read、Edit、Bash 仍按 Claude Code '
                '的权限在本机执行。',
            ),
            (
                "Native Devin may keep session data and tool-handoff records on the slot, "
                "including selected context and replies. These are separate from your "
                "local Claude Code history and may remain until the slot is cleared. "
                "The provider credential stays on the slot; the Fleet website does not "
                "offer a password or provider-token collection form.",
                "原生 Devin 可能在槽位保存会话数据与工具交接记录，其中可能包含选定的上下文和回复。"
                "它们与本机 Claude Code 历史分开，可能一直保留到槽位被清理。服务商凭证保存在槽位；"
                "Fleet 网页目前不提供收集服务商密码或令牌的表单。",
            ),
            (
                "Fleet stores your provider choice and account-verification fingerprint, "
                "runtime version, supported features and latest verification time. This "
                "lets it check that the account still belongs to the current slot holder. "
                '<a href="/docs/library/docs/devin.html#connect">Devin setup and limits</a>.',
                "Fleet 保存你选择的服务商、账号验证指纹、运行版本、受支持功能和最近验证时间，"
                "用于检查账号是否仍属于当前槽位持有者。"
                '<a href="/docs/library/docs/devin.html#connect">Devin 设置与限制</a>。',
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
                "separate from model traffic: the HTTPS gateway "
                "receives and processes "
                "the contents of model requests and responses."
            ),
            (
                "健康报告不包含提示词、对话、文件内容、Claude 凭据或 "
                "Claude 账号显示名。模型流量是另一回事："
                "我们的 HTTPS 网关会接收并处理模型请求及回复内容。"
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
                    "never returned to your computer or the gateway. Anthropic’s "
                    "own terms and privacy policy apply."
                ),
                (
                    "选中的槽位只使用其绑定账号，把受支持的模型请求发给 "
                    "Anthropic。槽位上的原版 Claude 负责写入和续期凭据，"
                    "凭据不会返回电脑或网关。Anthropic "
                    "自身的条款和隐私政策同时适用。"
                ),
            ),
        ],
    )
    body += card(
        "Slot providers and network exits",
        "槽位服务商与网络出口",
        [("The public slot list names the host provider and IP/exit provider separately, "
          "with the operator-declared Residential, ISP or Datacenter category and monthly rate. "
          "It uses neutral machine numbers and does not publish holder identities, "
          "slot nicknames, machine addresses or proxy credentials.",
          "公开槽位列表分别展示主机及 IP／出口服务商、运营者标注的住宅、ISP 或机房类别和月费。"
          "列表使用中立主机编号，不公开持有人身份、槽位昵称、主机地址或代理凭据。"),
         ("A proxy-routed slot keeps provider TLS verification enabled. The exit provider "
          "can observe connection destinations, timing and encrypted traffic volume; "
          "the proxy tunnel does not terminate the HTTPS connection to Anthropic. "
          "The slot host's administrators retain access to the slot and its credentials.",
          "代理出口槽位保留对上游的 TLS 校验。出口服务商可观察连接目标、时间和加密流量"
          "大小；代理隧道不会终止与 Anthropic 的 HTTPS 连接。槽位主机管理员仍能访问槽位"
          "及其中的凭据。")],
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
                    "HTTPS encrypts traffic in transit. TLS ends at the gateway. "
                    "<strong>The gateway can read model "
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
                    "HTTPS 加密传输过程，TLS 在网关终止，因此 "
                    "<strong>网关能读取模型请求和回复</strong>，"
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
                    "and environment details. The gateway can also see your "
                    "connection IP and timing. This is not anonymity."
                ),
                (
                    "网关和槽位转发器移除部分请求头及顶层 "
                    "<code>metadata</code> 字段，不会自动删除提示词、"
                    "工具结果或文件内容中的 "
                    "敏感信息。原版提示词可能包含操作系统、"
                    "工作目录和环境信息。网关还能看到连接 IP 和时间；"
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
        "Public usage ranking",
        "公开的用量排行",
        [
            (
                'The <a href="/#usage-ranking">homepage ranking</a> is visible to everyone. '
                "It publishes each contributing user's measured token totals, a compact "
                "model breakdown and a common Opus 5.5 API-equivalent USD value over "
                "seven UTC calendar days. Names prefer a public masked fragment of the email "
                "local part, for example cd•••pt. Email domains and complete local parts are "
                "not included. Unsupported or short addresses fall back to a partly masked "
                "nickname or member code. The code is a purpose-specific pseudonym; "
                "rotating the website's signing secret changes it. Full email addresses and "
                "account IDs are never published as names. "
                "Masking is not anonymity: email fragments, a nickname or activity pattern "
                "may still identify someone. The ranking can include one contributing user.",
                '<a href="/#usage-ranking">首页用量排行</a>对所有人公开，展示每位参与用户在七个 '
                "UTC "
                "日期内观测到的 Token 总量、简要模型明细，以及统一按 Opus 5.5 API 参考价格折算的"
                "美元价值。"
                "名称优先公开邮箱用户名的局部遮盖片段，例如 cd•••pt；不展示域名或完整用户名。"
                "短邮箱或不适合安全遮盖的地址，使用遮盖昵称或用户编号。"
                "编号是仅用于此排行的化名，网站签名密钥轮换后会变化。"
                "公开名称绝不展示完整邮箱或账号 ID。遮盖不等于匿名：邮箱片段、昵称或活动模式"
                "仍可能让人识别出某位用户。排行可以包含仅一位参与用户。",
            ),
            (
                "The ranking uses validated usage totals from current, fresh, account-bound "
                "slot reports. All measured token categories use the same reference rates, "
                "including existing aggregates without a known model. This conversion does "
                "not identify the model that served a reply. Reported-model details and their "
                "separate estimates use only matched model observations. Model names are reported "
                "by the agent. "
                "Eligible native, bridge and gateway "
                "model calls are counted at the node once. It is not a complete record of "
                "all Claude activity. Missing, partial, oversized or older observations "
                "are not reconstructed or treated as zero. Measurement and pricing "
                "coverage are shown. Missing reported-model prices stay unknown. No "
                "prompts, response content, raw errors, hostnames, IP addresses, login "
                "accounts or individual request timestamps appear in the ranking.",
                "排行使用当前槽位最新报告中，与账号绑定且经验证的用量汇总。所有已观测 Token "
                "分类均采用同一参考价格，包括缺少模型信息的已有汇总；折算不代表确定了生成回复"
                "的模型。上报模型明细与单独估值只使用已匹配的模型观测，模型名称由代理上报。符合条件的"
                "原生、桥接和网关模型调用在节点上只计一次，不代表全部 Claude 活动的完整记录。"
                "缺失、不完整、过大或较旧的观测不重建，也不当作零。页面注明观测与计价覆盖范围；"
                "缺失的上报模型价格仍表示未知。排行不展示提示词、回复内容、原始错误、主机名、"
                "IP 地址、登录账号或单条请求时间戳。",
            ),
            (
                "The ranking uses Opus 5.5 standard global reference rates per million "
                "tokens: input $4, output $20, five-minute cache writes $5 and cache reads "
                "$0.20. It is not a Claude Pro/Max subscription bill, an amount paid, a "
                "quota balance, unlimited usage or a promise of savings. The primary token "
                "total and reference value cover the same measured categories. Missing "
                "replies or cache fields make the subtotal partial, never fabricated zero. "
                "Reported-model estimates appear separately in expanded details and cover "
                "only their matching priced tokens. Output and reply counts remain in the "
                "detailed breakdowns. "
                "Processed totals include cached context counted again per request; "
                "they are not generated or unique text. No older model identity is inferred.",
                "排行统一采用 Opus 5.5 标准全球参考价格，每百万 Token：输入 $4、输出 $20、"
                "五分钟缓存写入 $5、缓存读取 $0.20。这不是 Claude Pro/Max 订阅账单、实际付款、"
                "剩余额度、无限用量，也不保证节省了这些费用。排行主表的 Token 总量与参考折算值"
                "覆盖相同的已观测分类；缺少回复或缓存字段时，标注部分小计，不伪造为零。实际"
                "模型估值在展开明细中单独展示，只覆盖对应的已计价 Token。"
                "输出与回复数量保留在明细中。处理总量包含每次请求重复计数的"
                "缓存上下文，不等于生成或独有的文本。不推断旧模型身份。",
            ),
        ],
    )
    body += card(
        "Public performance measurements",
        "公开的性能统计",
        [
            (
                "The public Status performance panel combines existing numeric relay measurements "
                "from current, fresh slot reports. It shows transfer completion and "
                "average slot-side timing over seven UTC calendar days, including today. "
                "A separate streamed output rate combines matched native/bridge node streams "
                "and HTTPS gateway observations over the same window, without counting "
                "gateway traffic twice. This performance panel "
                "does not split measurements by user or slot or publish names, emails, "
                "prompts, credentials or response contents.",
                "公开的服务状态性能面板汇总当前槽位最新报告中已有的数值统计，展示包括今天在内的 "
                "七个 UTC 日的传输完成率和槽位端平均响应时长。流式输出速率在相同窗口内，合并"
                "符合条件的节点原生／桥接流和 HTTPS 网关观测，不重复计算网关流量。这个性能面板"
                "不按用户或槽位分别展示统计，"
                "也不公开名称、邮箱、提示词、凭据或回复内容。",
            ),
            (
                "Machine availability is shown separately in a compact table under stable "
                "neutral labels such as Machine 1, with status and the last report. "
                "Your own slot card links to its machine's public label. Operators can "
                "also see actual machine names beside those labels after signing in; "
                "those names remain hidden from other public viewers. Status describes "
                "machine health only; the table has no detailed reasons or per-machine "
                "uptime cards.",
                "主机可用状态在紧凑表格中单独展示，使用“主机 1”等固定中性标签，显示状态和"
                "最近报告时间。你的槽位卡片会链接到对应的公开标签。管理员登录后还可以在"
                "标签旁看到实际主机名称；其他公开访问者看不到这些名称。状态只表示主机健康，"
                "表格不展示详细异常原因或逐台主机的可用率卡片。",
            ),
            (
                "Traffic figures appear after at least twenty observed attempts. "
                "Published aggregates can include only one contributing Fleet user, "
                "so a total or average can reflect that user's traffic. Each timing "
                "separately needs twenty samples, which can all come from one user. "
                "Until enough fresh measurements are available, the panel "
                "shows that more aggregate data is needed. A completed transfer requires "
                "a successful HTTP reply (2xx) delivered in full; error replies, "
                "cancellations and interrupted transfers are not successes. "
                "These sampled relay attempts "
                "can include a backup attempt for one request; they do not cover gateway "
                "failures before a slot is reached or measure answer quality. Viewing "
                "Status makes no extra model request.",
                "观测到至少二十次转发尝试后，才公开流量数值。公开汇总可能只包含一个 Fleet "
                "用户的观测，因此总量或平均值可能反映这个用户的流量。每项计时分别需要二十个"
                "样本，所有样本可以来自同一个用户。没有足够的新观测时，页面会提示"
                "需要更多汇总数据。成功传输要求 HTTP 回复状态为 2xx，且内容完整传完；错误"
                "回复、取消或中断的传输不算成功。一条请求可能因使用备用"
                "账号而产生多次转发尝试。这些统计不包含请求到达槽位前的网关失败，也不评价"
                "答案质量。查看状态页不会额外调用模型。",
            ),
            (
                "Streamed output rate is provider-reported output tokens divided by "
                "first-to-last response-body chunk time, summed across successful, fully "
                "delivered /v1/messages streaming replies observed at a node for native/bridge "
                "traffic or at the HTTPS gateway. A sample needs valid usage, "
                "at least two chunks and at least 100 ms of delivery time. Single-chunk "
                "or too-short replies are excluded; buffering can affect the observed rate. "
                "This observed delivery rate "
                "excludes the initial wait and includes stream pauses, network delivery "
                "and any thinking or tool output included in the provider's output count. "
                "Compare only similar models and workloads; it is not a pure model benchmark. "
                "At least twenty eligible replies are needed independently of slot-relay "
                "measurements. Node observations stay in a private account-bound seven-day "
                "relay file and allowlisted numeric heartbeats. Gateway storage contains only "
                "anonymous daily sample counts, output-token totals and delivery-time totals "
                "for seven UTC calendar days. The public output-rate measure shows no model "
                "breakdown, input/cache token totals or individual request timestamps. "
                "Older traffic is not backfilled.",
                "流式输出速率用服务商报告的输出 Token 总量，除以从首个到最后一个响应正文分块"
                "的时间总和，只统计成功且完整传回的 /v1/messages 流式回复；原生／桥接流量在节点"
                "观测，HTTPS 流量在网关观测。样本需有有效用量、"
                "至少两个分块，且传输过程至少持续 100 毫秒。单个分块或过短的回复不计入；"
                "缓冲可能影响观测到的速率。这是观测到的交付速率，不含初始等待，"
                "但包含流中的暂停、网络传输，"
                "以及服务商输出计数中包含的思考或工具输出。只宜比较相近的模型和工作负载，"
                "不是模型本身的基准测试。它单独要求至少二十个符合条件的回复，不依赖槽位端"
                "统计是否就绪。节点观测保存在私有、与账号绑定的七日转发统计文件及允许的数值心跳"
                "字段中。网关只在七个 UTC 日期内保留不含身份信息的每日样本数、输出 Token 总量"
                "和交付时间总和。公开输出速率不展示模型明细、输入或缓存 Token 总量，也不展示"
                "单条请求时间戳。不补填历史流量。",
            ),
        ],
    )
    body += card(
        "Public machine resources",
        "公开的主机资源",
        [
            (
                "When the operator enables it, Status also shows Fleet's dedicated gateway-VPS "
                "CPU, memory and data-filesystem capacity, plus Fleet's own container "
                "CPU and memory consumption against its limits. Host figures cover "
                "the whole VPS, including system services. They do not list processes, "
                "users, IP addresses, individual requests or their contents.",
                "运营方开启后，服务状态页还会展示 Fleet 专用网关 VPS 的整体 CPU、内存、数据所在文件"
                "系统容量，以及 Fleet 容器的 CPU 和内存占用与配额。主机数值覆盖整台 VPS，"
                "包括系统服务，不列出进程、用户、IP 地址、单条请求或内容。",
            ),
            (
                "The service samples these numeric kernel counters every fifteen "
                "seconds and keeps the latest observation in memory. Values older "
                "than one minute are hidden; restarting resets the observations. "
                "These infrastructure figures are separate from user traffic and do "
                "not require relay attempts or timing samples. Viewing them makes "
                "no model request.",
                "服务每十五秒采样一次系统的数值计数器，只在内存中保留最新观测。超过一分钟"
                "的数值不再显示，重启会重置观测。基础设施数值与用户流量分开，不需要转发尝试或"
                "计时样本，查看它们不会调用模型。",
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
                    "We do not use browser-tracking analytics, advertising or "
                    "third-party scripts. Page language is selected "
                    "by the English or Chinese URL."
                ),
                (
                    "登录使用两类必要 Cookie：Fleet 会话，以及把 Google "
                    "授权回复绑定到发起登录的浏览器的 Cookie。"
                    f"后者有效期为 {_span_zh(oauth.FLOW_TTL_S)}，服务器的对"
                    f"应记录保留相同时间。不使用浏览器追踪分析、广告或第三方脚本。"
                    "英语或中文由页面地址选择。"
                ),
            ),
        ],
    )
    if cfg.usd_slot_credit_enabled:
        body += card(
            "Service credit and payments",
            "服务余额与付款",
            [
                (
                    "We keep credit amounts, slot purchases and renewals, paid dates, "
                    "staff actions and private billing notes to check your balance and "
                    "billing history. These records are append-only. Returning a slot "
                    "does not remove them, and they do not expire automatically.",
                    "我们保存余额变动、槽位购买与续费、有效期、工作人员操作及内部账目备注，"
                    "用于核对余额和账单。记录以追加方式保存，归还槽位不会删除，也不会自动过期。",
                ),
                (
                    'When online top-up is available, Stripe hosts the payment page. '
                    'We send Stripe your Fleet account reference, top-up order reference, '
                    'USD amount and currency. You enter payment details directly on Stripe, '
                    'under <a href="https://stripe.com/privacy">its privacy policy</a>. '
                    'Our billing history keeps payment references and payment-review status; '
                    'it does not store card numbers or card security codes.',
                    '在线充值开通后，付款页面由 Stripe 提供。我们向 Stripe 发送 Fleet 账号标识、'
                    '充值订单标识、美元金额及币种。付款资料由你直接在 Stripe 页面填写，并适用'
                    '<a href="https://stripe.com/privacy">Stripe 隐私政策</a>。我们的账目只保存'
                    '付款关联标识和审核状态，不保存银行卡号或卡片安全码。',
                ),
                (
                    "For account deletion, contact the operator to review the slots and "
                    "billing records, including any financial-record retention requirements.",
                    "删除账号时，请联系运营方，一并核对槽位、账目及需要保留财务记录的情况。",
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
             updating: bool = False, *, idle_refresh: bool = True) -> str:
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
    if not soon and not idle_refresh:
        # Do not close setup instructions or erase an unfinished connection form.
        return ""
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


def _slot_disclosure(english: str, chinese: str, content: str, cls: str,
                     *, expanded: bool = False) -> str:
    opened = ' open' if expanded else ''
    return ('<details class="slot-disclosure ' + cls + '"' + opened + '><summary>'
            + label(english, chinese, inline=True) + '</summary><div class="disclosure-body">'
            + content + '</div></details>')


def _slot_connect(slot: Mapping[str, Any], report: Mapping[str, Any], csrf: str,
                  cfg: Config, devin_state: Optional[str],
                  authorizer: Optional[Mapping[str, Any]]) -> str:
    """Show complete, secret-free instructions for an existing supported route.

    CLI enrollment still requires a ready hosted Claude slot. A verified Devin
    slot alone cannot mint that pairing; never present the CLI recipe without
    an eligible authorizer, or imply that website key creation saves a CLI key.
    """
    if (slot.get('kind') == slotstates.OWNER_SLOT or not gateway_ui.available(cfg)
            or slot.get('state') not in CAN_SIGN_IN):
        return ''
    providers = []
    if devin_state == 'ready':
        providers.append(('devin', 'Devin'))
    if (slot.get('state') == slotstates.ACTIVE
            and (report.get('credentials') or {}).get('logged_in') is True):
        providers.append(('anthropic', 'Claude'))
    result = []
    for provider, name in providers:
        title = label('Use ' + name + ' on my computer', '在电脑上使用 ' + name, inline=True)
        start = ('<details class="slot-connect" data-slot-connect="' + provider
                 + '"><summary><span>' + title
                 + '</span><span class="connect-arrow" aria-hidden="true">→</span></summary>')
        if authorizer is None:
            result.append(start + '<div class="connect-body">' + bi(
                '<p>First computer? This pilot needs assisted Fleet CLI setup. '
                'You can also create a connection key for Claude Code in advanced connections.</p>',
                '<p>首次连接电脑？当前试点需要协助设置 Fleet CLI。'
                '也可以在高级连接设置中创建密钥，手动连接 Claude Code。</p>')
                + '<a class="btn" href="/account#gateway">'
                + label('Open connection settings', '打开连接设置', inline=True)
                + '</a></div></details>')
            continue
        source = shlex.quote(names.display(authorizer))
        target = shlex.quote(names.display(slot))
        setup = ('ccfleet gateway setup --slot ' + source + ' --slots ' + target
                 + ' --provider ' + provider)
        launch = ('ccfleet local --transport gateway --slot ' + target
                  + (' --model claude-opus-5-5 --effort max' if provider == 'devin' else '')
                  + ' --new' + (' -- --safe-mode' if provider == 'devin' else ''))
        pair = ('<details class="connect-first-time"><summary>'
                + label('First time on this computer?', '第一次在这台电脑上使用？', inline=True)
                + '</summary>' + bi(
                    '<p>Connect this computer once, then return here. '
                    'Already used Fleet on this computer? Skip this step.</p>',
                    '<p>先连接这台电脑，再返回这里。已经在这台电脑上用过 Fleet？跳过此步。</p>')
                + _form('/account/slots/' + escape(str(authorizer['id'])) + '/cli', csrf,
                        'Connect this computer', cls='primary') + '</details>')
        steps = ''.join('<li><div><b>' + label(en, zh, inline=True)
                        + '</b><pre><code>' + escape(command) + '</code></pre></div></li>'
                        for en, zh, command in (
                            ('Update Fleet', '更新 Fleet', 'ccfleet update'),
                            ('Save this connection', '保存这个连接', setup),
                            ('Start coding', '开始写代码', launch)))
        pilot = (bi(
            '<p class="connect-hint">The Devin pilot starts with Claude Code’s built-in tools. '
            'Custom plugins and MCP tools are off for this session.</p>',
            '<p class="connect-hint">Devin 试点使用 Claude Code 内置工具，'
            '本次会话不加载自定义插件和 MCP 工具。</p>') if provider == 'devin' else '')
        result.append(start + '<div class="connect-body">' + bi(
            '<p>Open Terminal in your project folder. Run these commands in order.</p>',
            '<p>在项目文件夹中打开终端，按顺序运行下面的命令。</p>')
            + pair + '<ol class="connect-steps">' + steps + '</ol>' + bi(
                '<p class="connect-hint">Keep the setup command: it saves the connection '
                'that the start command needs. Your tools and files stay on your computer; '
                'selected context is sent for model replies.</p>',
                '<p class="connect-hint">请先运行“保存这个连接”，再启动。工具与文件在电脑上，'
                '选中的上下文会发送出去以获取模型回复。</p>') + pilot + '</div></details>')
    return ''.join(result)


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
    devin_state: Optional[str] = None,
    devin_html: str = "",
    authorizer: Optional[Mapping[str, Any]] = None,
    payment_end: Optional[float] = None,
    paid_access: bool = True,
    recurring: bool = False,
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
    devin_slot = (not own and devin_state in {"ready", "verification_required"}
                  and slot["state"] == slotstates.CLAIMED
                  and (report.get("credentials") or {}).get("logged_in") is not True)
    devin_only = devin_slot and devin_state == "ready"
    if devin_slot:
        tone, title, detail = (("ok", "Devin ready", "") if devin_only
                              else ("warn", "Devin needs a check", ""))
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
    title_zh = (("Devin 已就绪" if devin_only else "Devin 需要检查") if devin_slot else
                ("使用中" if signed_in_title else "等待登录") if own else
                STATE_ZH.get(slot["state"], (title, ""))[0])
    parts = [
        f'<div class="card slot" id="slot-{escape(slot["id"])}" '
        f'data-state="{escape(str(slot["state"]))}"{kind}><div class="slot-head">'
        f'<h2><span class="slot-display-name" id="slot-{escape(slot["id"])}-name" '
        f'data-fleet-readonly>{escape(name)}</span> '
        f'<span id="slot-{escape(slot["id"])}-state" data-fleet-readonly>'
        f'<span class="pill {tone}">'
        + label(
            title,
            title_zh,
            inline=True,
        )
        + f"</span></span>{mine}</h2></div><div class=\"slot-body\">",
    ]
    technical = ['<p class="slot-meta">'
        + bi(
            (about + " · " if about else "") + "machine " + machine,
            (about_zh + " · " if about_zh else "") + "机器：" + machine_zh,
            inline=True,
        )
        + "</p>"]
    if node.get("offer") is not None:
        technical.append(offer_ui.details(node))
    # Its machine's state, as the status page says it (status.slot_line).
    if machine_line:
        parts.append(machine_line)
    public_number = status.public_machine_number(node)
    if public_number is not None:
        public_label = label(f"Public status: Machine {public_number}",
                             f"公共状态：主机 {public_number}", inline=True)
        monitored = heartbeat is not None and node.get("enabled") is not False
        technical.append(
            '<p class="muted small">'
            + (f'<a href="/status#machine-{public_number}">{public_label}</a>'
               if monitored else public_label + ' · ' + label(
                   "Not publicly monitored yet", "暂未纳入公开监测", inline=True))
            + '</p>'
        )
    if detail:
        detail_zh = (
            "在下方登录你自己的 Claude 账号。"
            if own
            else STATE_ZH.get(slot["state"], ("", detail))[1]
        )
        parts.append("<p>" + label(detail, detail_zh, inline=True) + "</p>")
    if payment_end is not None:
        state = (label("Paid period active" if paid_access else "Check monthly payment",
                       "已付费服务期有效" if paid_access else "请检查月付款项", inline=True)
                 if recurring else label(
                      "Paid month active" if paid_access else "Renew to reconnect",
                      "已付费月份有效" if paid_access else "续费后可重新连接", inline=True))
        parts.append('<p class="' + ('muted' if paid_access else 'lapsed') + '">' + state
                     + ' · ' + escape(wallet_ui._date(payment_end)) + '</p>')
        if recurring:
            parts.append('<p><a class="btn" href="/account/subscriptions">'
                         + label("Manage monthly payment", "管理月付", inline=True) + '</a></p>')
        elif (wallet_routes.enabled(cfg)
              and slot["state"] in (slotstates.CLAIMED, slotstates.ACTIVE)):
            parts.append('<p><a class="btn" href="/account/renew?'
                         + urllib.parse.urlencode({"slot_id": slot["id"]}) + '">'
                         + label("Renew one month", "续费一个月", inline=True) + '</a></p>')
    elif not paid_access:
        if recurring:
            parts.append('<p class="lapsed">' + label(
                "This monthly rental is paused. Check your monthly payment before reconnecting.",
                "此月付槽位已暂停，请先查看月付款项，再重新连接。", inline=True)
                + ' <a href="/account/subscriptions">'
                + label("Monthly payments", "月付管理", inline=True) + '</a></p>')
        else:
            parts.append('<p class="lapsed">' + label(
                "Payment status is unavailable. New connections are paused.",
                "付费状态暂不可用，已暂停新连接。", inline=True) + '</p>')
    if paid_access:
        parts.append(_slot_connect(slot, report, csrf, cfg, devin_state, authorizer))
    parts.append(_slot_disclosure('About Devin', '关于 Devin', devin_html, 'slot-provider')
                 if devin_only else devin_html)
    account_parts = []
    if not own and slot["state"] in CAN_SIGN_IN:
        account_parts.append(_account_health(slot, report, login, heard, cfg, now, listening))
    if slot["state"] in (slotstates.CLAIMING, slotstates.RELEASING):
        # The machine is working on it. The sentence above says what; this
        # says it is still moving, which a still page otherwise cannot.
        parts.append('<div class="progress" aria-hidden="true"><i></i></div>')
    signed_in = (report.get("credentials") or {}).get("logged_in") is True
    if signed_in if own else slot["state"] == slotstates.ACTIVE:
        usage = [
            _in_use(
                report,
                now,
                "" if own else _quota_refresh(slot, report, csrf, now),
                fresh=fresh_observation(
                    report.get("credentials"), heard, now, cfg.heartbeat_max_age_s, listening
                ),
                slot_id=slot["id"],
            )
        ]
        if not own:
            since = max(slot.get("claimed_at") or 0, slot.get("account_switched_at") or 0)
            usage.append(
                _relay_usage(report.get("relay"), heard, now, cfg.heartbeat_max_age_s, since=since)
            )
        parts.append(_slot_disclosure('Usage & limits', '用量与额度', ''.join(usage),
                                      'slot-usage'))
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
        account_parts.append(_sign_in(slot, report, login, csrf, now))
        technical.append(_claude_row(slot, node, report, update or {}, channels or {}, csrf))
        if not own:
            # A Devin connection does not require a Claude sign-in or SSH pairing
            # on this slot. Its paired authorizer is explained in the connect guide.
            if not devin_slot:
                parts.append(_slot_disclosure('Connected computers', '已连接的电脑',
                    _cli_access(slot, node, cli_devices or [], csrf, now, paid_access=paid_access),
                    'slot-devices'))
    if account_parts:
        if devin_slot:
            parts.append('<details class="provider-other"><summary>' + label(
                'Anthropic sign-in (optional)', 'Anthropic 登录（可选）', inline=True
            ) + '</summary><p>' + label(
                'This is for a separate Claude account. '
                + ('Your Devin connection is ready.' if devin_only else
                   'The Devin check is handled separately.'),
                '这里用于连接独立的 Claude 账号。'
                + ('你的 Devin 连接已经就绪。' if devin_only else 'Devin 检查会单独处理。'),
                inline=True
            ) + '</p>' + ''.join(account_parts) + '</details>')
        else:
            needs_sign_in = not signed_in or bool(login)
            parts.append(_slot_disclosure('Claude account', 'Claude 账号',
                ''.join(account_parts), 'slot-account', expanded=needs_sign_in))
    if slot["state"] in CAN_SIGN_IN and not own:
        technical.append(_rename(slot, csrf))
    parts.append(_slot_disclosure('Slot details', '槽位详情', ''.join(technical), 'slot-technical'))
    # Never on somebody's own node: giving back means wiping, and nothing
    # there is ours to wipe.
    if slot["state"] in slotstates.RELEASABLE and not own:
        if recurring:
            parts.append('<p class="slot-monthly-return-note">' + label(
                "Cancel renewal first. Return this slot only after its monthly plan "
                "and paid time end. ",
                "请先停止续费，等月付计划与已付费服务时间都结束后再归还。", inline=True)
                + '<a href="/account/subscriptions">'
                + label("Manage monthly payments", "管理月付", inline=True) + '</a></p>')
        parts.append(_slot_disclosure('Return this slot', '归还槽位',
                                      _release(slot, csrf), 'slot-return'))
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
    if not source:
        return '<div class="meter"><div class="meter-head"><span>' + label(
            en, zh, inline=True) + '</span></div>' + _notice(
                "Current quota is unavailable. This does not mean quota remains.",
                "当前额度未知，不代表还有剩余额度。", "small muted") + '</div>'
    source = source.replace(
        f"<span>{escape(en)}</span>", "<span>" + label(en, zh, inline=True) + "</span>"
    )
    return source.replace(
        '<div class="meter-foot">resets ',
        '<div class="meter-foot">' + label("resets", "重置时间", inline=True) + " ",
    )


def _in_use(report: Mapping[str, Any], now: float, refresh: str = "", *, fresh: bool = True,
            slot_id: Optional[str] = None) -> str:
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
    quota_age = now - read if type(read) in (int, float) else None
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
                ("last reported Claude account usage · every device"),
                ("上次上报的 Claude 账号用量 · 所有设备合计"),
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
        if quota_age is None or quota_age > 10 * 60 or quota_age < 0:
            bars = _notice(
                "<strong>Historical reading · current quota unknown.</strong> "
                "A signed-in account can still have reached its limit.",
                "<strong>历史读数 · 当前额度未知。</strong>账号已登录也可能已达到用量限制。",
                "small lapsed") + bars
    elif refresh:
        bars = (
            _notice(("No reading of your limits yet."), "尚未读取用量限制。", "small muted")
            + refresh
        )
    halves = [
        f'<div class="usage-{name}"'
        + (f' id="slot-{escape(slot_id)}-quota" data-fleet-readonly'
           if name == "bars" and slot_id else "") + f'>{html}</div>'
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
        buttons.append(_form(f"{base}/update", csrf, update_words, cls="primary",
                             partial="version"))
        if channel != "latest":
            note = '<p class="cc-note">' + label(
                UPDATE_NOTE, (
                    '改用最新版通道，以后会自动保持该通道的最新版本。'
                ), inline=True) + '</p>'
    if channel == "latest" and kind not in ("held", "updating"):
        buttons.append(_form(f"{base}/stable", csrf, "Back to Stable", cls="quiet",
                             partial="version"))
    tone = " bad-text" if kind == "failed" else ""
    return (f'<div class="row-line cc" id="slot-{escape(slot["id"])}-version" '
            'data-fleet-readonly><div class="row-name">Claude Code</div>'
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
                devices: list[Mapping[str, Any]], csrf: str, now: float,
                *, paid_access: bool = True) -> str:
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
    if not paid_access:
        button = '<span class="muted small">' + label("Renew this slot to connect a computer.",
            "请先续费此槽位，再连接电脑。", inline=True) + '</span>'
    elif ready:
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
    acknowledgement = (report.get("quota") or {}).get("refresh")
    known = isinstance(read, (int, float)) and not isinstance(read, bool)
    when = f"read {_age(now, read)} ago · " if known else ""
    when_zh = f"最近读取：{_age(now, read)} 前 · " if known else ""
    if quota_reading(slot.get("quota_wanted_at"), read, now, acknowledgement):
        return '<p class="small muted" data-quota-slot="' + escape(slot['id']) + '">' + label(
            when + "A new quota reading was requested. It may take a few minutes. "
            "Only this quota panel checks for updates; your open forms are kept.",
            when_zh + "已请求新的额度读数，可能需要几分钟。"
            "仅此额度面板检查更新，你打开的表单会保留。",
            inline=True) + '</p>'
    wanted = instant(slot.get("quota_wanted_at"))
    completed = quota_refresh_completed(slot.get("quota_wanted_at"), acknowledgement, now)
    completed_failure = ((not known or wanted is not None and wanted > read)
                         and quota_refresh_failed(slot.get("quota_wanted_at"),
                                                   acknowledgement, now))
    timed_out = (not completed and wanted is not None and wanted <= now - 5 * 60
                 and (not known or wanted > read))
    failed = (_notice(
        "No new quota reading was received for your request. The machine may be busy "
        "or unable to read Claude's usage. Missing data does not mean quota remains.",
        "本次请求未收到新的额度读数，机器可能繁忙或无法读取 Claude 用量。"
        "没有读数不代表还有剩余额度。",
        "small lapsed") if timed_out or completed_failure else "")
    latest = (_notice(
        "Claude returned its latest available native reading. Its actual reading time "
        "is shown below; it can be earlier than this request.",
        "Claude 返回了原版客户端最新可用的读数。下方保留真实读取时间，可能早于本次请求。",
        "small muted") if completed and not completed_failure and known
        and wanted is not None and read < wanted else "")
    return (failed + latest + '<div class="refresh"><span class="small muted">'
            + label(when + "every five minutes", when_zh + "每五分钟读取一次", inline=True)
            + '</span>' + _form(f"/account/slots/{escape(slot['id'])}/quota", csrf, "Refresh",
                               partial="quota")
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
                   "" if on else "primary", partial="emails")
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
                    ), partial="rename")
            + '<p class="small muted">' + label(
                (
                    'This also becomes the machine hostname. '
                    'Pick anything but your email address.'
                ),
                "这个名称也会成为机器主机名。请不要用邮箱命名。",
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
                            'Delete everything on it: files, sessions and provider '
                            'sign-ins. Your provider accounts and subscriptions are untouched.'
                        ),
                        (
                            '删除槽位中的文件、会话和服务商登录信息；'
                            '不会删除你的服务商账号或取消订阅。'
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
