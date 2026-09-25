"""Command line entry point: ``ccfleetd serve`` and node administration."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from typing import Optional

from . import __version__, claude_versions, payments, pricing
from . import slots as slotstates
from .api import Context, serve
from .config import Config, ConfigError
from .mail import build_mailer
from .monitor import Monitor
from .notify import build_notifier
from .passwords import generate_password, hash_password
from .store import Store, StoreError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccfleetd",
                                     description="ccfleet server: dashboard and alerts.")
    parser.add_argument("--version", action="version", version=f"ccfleetd {__version__}")
    parser.add_argument("--db", help="SQLite path (overrides CCFLEET_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the HTTP server and the periodic checks")
    sub.add_parser("check", help="evaluate every node once and print transitions")
    sub.add_parser("alert-test", help="send a test message through the configured notifier")

    node = sub.add_parser("node", help="manage nodes").add_subparsers(dest="node_command",
                                                                       required=True)
    add = node.add_parser("add", help="register a node and print its token once")
    add.add_argument("node_id")
    add.add_argument("--owner", required=True)
    add.add_argument("--region", default="")
    add.add_argument("--pinned-version", default="")
    add.add_argument("--rc-expected", action="store_true",
                     help="alert when the remote-control service is not active")
    node.add_parser("list", help="list nodes")
    for name in ("remove", "rotate-token", "enable", "disable"):
        node.add_parser(name).add_argument("node_id")
    node_rename = node.add_parser(
        "rename", help="give a node a new id; its token, slots and history come along")
    node_rename.add_argument("node_id")
    node_rename.add_argument("new_id")
    pin = node.add_parser("pin", help="set the pinned Claude Code version")
    pin.add_argument("node_id")
    pin.add_argument("version")
    rc = node.add_parser("rc-expected",
                         help="turn the remote_control_down alert on or off for a node")
    rc.add_argument("node_id")
    rc.add_argument("state", choices=("on", "off"))
    reserve = node.add_parser("reserve",
                              help="keep a shared machine's free slots for one account")
    reserve.add_argument("node_id")
    keep_for = reserve.add_mutually_exclusive_group(required=True)
    keep_for.add_argument("email", nargs="?", help="the account's address, as it signed in")
    keep_for.add_argument("--none", action="store_true", help="open it to anybody again")
    hold = node.add_parser("hold", help="count an owner's own node as a slot they hold")
    hold.add_argument("node_id")
    holder = hold.add_mutually_exclusive_group(required=True)
    holder.add_argument("email", nargs="?", help="the account's address, as it signed in")
    holder.add_argument("--none", action="store_true", help="stop counting it as a slot")
    hold.add_argument("--unix-user", default=None,
                      help="their Linux login on it; defaults to the node's owner")

    slot = sub.add_parser("slot", help="manage slots on a machine").add_subparsers(
        dest="slot_command", required=True)
    slot_add = slot.add_parser("add", help="declare a slot on a machine")
    slot_add.add_argument("slot_id")
    slot_add.add_argument("--machine", required=True)
    slot_add.add_argument("--unix-user", required=True,
                          help="the Linux login on that machine, e.g. slot01")
    slot_list = slot.add_parser("list", help="list slots and who holds them")
    slot_list.add_argument("--machine", default=None)
    slot_release = slot.add_parser(
        "release", help="start the wipe that frees a slot for somebody else")
    slot_release.add_argument("slot_id")
    slot_rm = slot.add_parser(
        "remove", help="take a free slot off a machine (release it first)")
    slot_rm.add_argument("slot_id")
    slot_rename = slot.add_parser(
        "rename", help="give a slot a new id, in any state; the machine never sees it")
    slot_rename.add_argument("slot_id")
    slot_rename.add_argument("new_id")
    slot_name = slot.add_parser(
        "name", help="give a held slot a new name, its machine's hostname; none: a neutral one")
    slot_name.add_argument("slot_id")
    slot_name.add_argument("name", nargs="?", default=None)
    cap = slot.add_parser("capacity", help="how many slots a machine may hold")
    cap.add_argument("machine")
    cap.add_argument("count", type=int)

    acct = sub.add_parser(
        "account", help="manage the people who rent slots").add_subparsers(
        dest="account_command", required=True)
    acct.add_parser("list", help="list accounts and their allowance")
    quota = acct.add_parser(
        "quota", help="grant or reduce how many slots somebody may claim")
    quota.add_argument("email")
    quota.add_argument("count", type=int)
    role = acct.add_parser(
        "role", help="make somebody who has signed in an operator, or not")
    role.add_argument("email")
    role.add_argument("role", choices=("admin", "user"))
    handle = acct.add_parser(
        "handle", help="name this person's slots <handle>-<n>, only if they ask for it")
    handle.add_argument("email")
    named = handle.add_mutually_exclusive_group(required=True)
    named.add_argument("handle", nargs="?")
    named.add_argument("--none", action="store_true",
                       help="go back to neutral names like slot-4821")

    pay = sub.add_parser(
        "payment", help="the record of who paid, and through when").add_subparsers(
        dest="payment_command", required=True)
    pay_add = pay.add_parser("add", help="write down a payment somebody made")
    pay_add.add_argument("email")
    pay_add.add_argument("amount", help="e.g. 30 or 30.50")
    pay_add.add_argument("currency", help="a three-letter code, e.g. USD")
    pay_add.add_argument("through", help="the last day it covers, YYYY-MM-DD")
    pay_add.add_argument("--note", default="")
    pay_list = pay.add_parser("list", help="payments, newest first")
    pay_list.add_argument("email", nargs="?", default=None)
    pay_void = pay.add_parser(
        "void", help="mark a payment written down in error; it stays in the record")
    pay_void.add_argument("payment_id", type=int)

    price = sub.add_parser(
        "price", help="what the public pages say a slot costs; shown, never charged"
    ).add_subparsers(dest="price_command", required=True)
    price.add_parser("show", help="the price the public pages show")
    price_set = price.add_parser("set", help="set the price of a slot for a month")
    price_set.add_argument("amount", help="e.g. 20 or 20.50")
    price_set.add_argument("currency", help="one of " + ", ".join(pricing.CURRENCIES))
    price.add_parser("clear", help="show no price: the pages say it is agreed with you")

    user = sub.add_parser("user", help="manage console accounts").add_subparsers(
        dest="user_command", required=True)
    user_add = user.add_parser("add", help="create a console login and print its password once")
    user_add.add_argument("username")
    user_add.add_argument("--role", choices=("owner", "admin"), default="owner",
                          help="owner sees only their own nodes and can change nothing; "
                               "admin sees and manages the whole fleet")
    user_add.add_argument("--owner", default="",
                          help="which node owner this login maps to; defaults to the username")
    user_add.add_argument("--password", default="",
                          help="leave unset to have one generated and printed once")
    user.add_parser("list", help="list console accounts (never their passwords)")
    user_pw = user.add_parser("passwd", help="set a new password, printed once")
    user_pw.add_argument("username")
    user_pw.add_argument("--password", default="")
    user.add_parser("remove", help="delete a console account").add_argument("username")
    return parser


def _print_token(node_id: str, token: str, cfg: Config) -> None:
    url = cfg.public_url or f"http://{cfg.bind_host}:{cfg.bind_port}"
    print(f"node {node_id} registered. Token (shown once):\n")
    print(f"  {token}\n")
    print("Put this in ~/.config/ccfleet/agent.env on the node:\n")
    print(f"  CCFLEET_URL={url}")
    print(f"  CCFLEET_NODE_ID={node_id}")
    print(f"  CCFLEET_NODE_TOKEN={token}")


def _print_password(username: str, password: str, cfg: Config) -> None:
    where = cfg.public_url or "the console"
    print(f"\nconsole account {username!r} ready. Password (shown once):\n")
    print(f"  {password}\n")
    print(f"Sign in at {where} with that user name and password.")
    print("It is stored only as a PBKDF2 hash, so it cannot be recovered; use")
    print(f"'ccfleetd user passwd {username}' to set a new one.\n")


def _slot_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.slot_command == "add":
        already = store.list_slots(node_id=args.machine, kind=slotstates.MACHINE_SLOT)
        if len(already) >= slotstates.MAX_SLOTS_PER_MACHINE:
            print(f"error: {args.machine} already has its slot ({already[0]['id']}): "
                  f"{slotstates.ONE_SLOT_WHY}", file=sys.stderr)
            return EXIT_USAGE
        slot = store.add_slot(args.slot_id, args.machine, args.unix_user,
                              now=time.time())
        print(f"slot {slot['id']} declared on {slot['node_id']} "
              f"as {slot['unix_user']}, state {slot['state']}")
        # Free means the Linux user does not exist, so the account is made at
        # claim time by the machine's own agent (slot-add.sh), never ahead of
        # it by hand: a user already there reads as occupied and is held back.
        print(f"It is handed out once {slot['node_id']} reports that "
              f"{slot['unix_user']} does not exist there. The machine creates "
              f"it with slot-add.sh when somebody claims it; do not create it "
              f"by hand.")
    elif args.slot_command == "list":
        rows = store.list_slots(node_id=args.machine)
        if not rows:
            print("no slots declared")
            return EXIT_OK
        emails = {a["id"]: a["email"] for a in store.list_accounts()}
        # The name goes before the holder, so what scripts read — the id off
        # the front, the state and "on machine" after it — stays where it was.
        print(f"{'slot':<16} {'machine':<14} {'unix user':<12} {'state':<10} "
              f"{'on machine':<13} {'name':<24} held by")
        for r in rows:
            # What the machine last said, which is the half of "free" the
            # records cannot vouch for on their own. An owner's own node has
            # no machine agent to say it: it is theirs, not ours to vouch for.
            on_machine = ("own machine" if r["kind"] == slotstates.OWNER_SLOT
                          else {1: "yes", 0: "no"}.get(r["present"], "not yet seen"))
            holder = emails.get(r["held_by"], r["held_by"]) if r["held_by"] else "-"
            print(f"{r['id']:<16} {r['node_id']:<14} {r['unix_user']:<12} "
                  f"{r['state']:<10} {on_machine:<13} {r['name'] or '-':<24} {holder}")
    elif args.slot_command == "release":
        # Only ever starts the wipe. The slot does not become free here — it
        # becomes free when the machine reports the wipe finished, because only
        # that proves the Linux user and its files are gone. A missing slot is
        # already a StoreError, which main() prints; a second check would be
        # unreachable rather than defensive.
        store.begin_release(args.slot_id)
        print(f"{args.slot_id} is releasing. It stays held until the machine "
              f"reports its Linux user gone.")
        print(f"The machine's agent runs slot-remove.sh --slot "
              f"{store.get_slot(args.slot_id)['unix_user']} on its next check-in.")
    elif args.slot_command == "remove":
        store.remove_slot(args.slot_id)
        print(f"{args.slot_id} is no longer declared on this fleet")
    elif args.slot_command == "rename":
        store.rename_slot(args.slot_id, args.new_id)
        print(f"renamed {args.slot_id} to {args.new_id}; its holder, state, sign-in and "
              f"requests came along. The machine knows its slots by their Linux user, "
              f"so nothing changes there.")
    elif args.slot_command == "name":
        chosen = store.name_slot(args.slot_id, args.name)
        print(f"{args.slot_id} is now called {chosen}; its machine answers to it at its next "
              f"run, and Remote Control restarts under it, ending a session open in it.")
    elif args.slot_command == "capacity":
        if args.count > slotstates.MAX_SLOTS_PER_MACHINE:
            print(f"error: {slotstates.ONE_SLOT_WHY}; a machine's capacity is 0 or 1",
                  file=sys.stderr)
            return EXIT_USAGE
        if not store.set_machine_capacity(args.machine, args.count):
            print(f"error: no such machine {args.machine!r}", file=sys.stderr)
            return EXIT_USAGE
        print(f"{args.machine} may hold {args.count} slots")
    return EXIT_OK


def _payment_command(args: argparse.Namespace, store: Store) -> int:
    """The ledger, from the server. A record for the operator; it enforces nothing."""
    if args.payment_command == "void":
        store.void_payment(args.payment_id, now=time.time())
        print(f"payment {args.payment_id} voided; it stays in the record")
        return EXIT_OK
    account = store.account_by_email(args.email) if args.email else None
    if args.email and account is None:
        print(f"error: nobody registered as {args.email!r}", file=sys.stderr)
        return EXIT_USAGE
    if args.payment_command == "add":
        payment_id = store.record_payment(
            account["id"], amount=args.amount, currency=args.currency,
            through=args.through, note=args.note, recorded_by="server command line",
            now=time.time())
        print(f"payment {payment_id} recorded: {args.email} paid through {args.through}")
        return EXIT_OK
    rows = store.list_payments(account["id"] if account else None)
    if not rows:
        print("no payments recorded")
        return EXIT_OK
    emails = {a["id"]: a["email"] for a in store.list_accounts()}
    print(f"{'id':<6} {'recorded':<11} {'email':<32} {'amount':<14} {'through':<11} note")
    for r in rows:
        amount = payments.format_amount(r["amount_minor"], r["currency"])
        voided = "  [voided]" if r["voided_at"] is not None else ""
        print(f"{r['id']:<6} {payments.today(r['recorded_at']).isoformat():<11} "
              f"{emails.get(r['account_id'], r['account_id']):<32} {amount:<14} "
              f"{r['paid_through']:<11} {r['note']}{voided}")
    return EXIT_OK


def _price_command(args: argparse.Namespace, store: Store) -> int:
    if args.price_command == "set":
        price = store.set_price(args.amount, args.currency, by="server command line",
                                now=time.time())
        print(f"the public pages now say: {pricing.per_slot(price)}")
        return EXIT_OK
    if args.price_command == "clear":
        store.clear_price()
        print("no price shown: the pages say price and payment are agreed with you")
        return EXIT_OK
    current = store.get_price()
    if current is None:
        print("no price set: the pages say price and payment are agreed with you")
        return EXIT_OK
    print(f"{pricing.per_slot(current['price'])}, set by {current['updated_by']} on "
          f"{payments.today(current['updated_at']).isoformat()}")
    return EXIT_OK


def _account_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.account_command == "list":
        rows = store.list_accounts()
        if not rows:
            print("nobody has registered yet")
            return EXIT_OK
        print(f"{'email':<32} {'role':<6} {'allowance':<10} {'holding':<8} paid through")
        now = time.time()
        for a in rows:
            held = store.held_slot_count(a["id"])
            through = payments.paid_through(store.list_payments(a["id"]))
            state = payments.standing(through, now)
            paid = {payments.NONE: "-", payments.PAID: through}.get(state, f"{through} (lapsed)")
            print(f"{a['email']:<32} {a['role']:<6} {a['slot_quota']:<10} {held:<8} {paid}")
    elif args.account_command == "role":
        # Deliberately only here, on the server's own command line: nothing on
        # either site can make an operator, so a bug in one cannot either.
        account = store.account_by_email(args.email)
        if account is None:
            print(f"error: nobody registered as {args.email!r}; they sign in once first",
                  file=sys.stderr)
            return EXIT_USAGE
        store.set_account_role(account["id"], args.role)
        if args.role == "admin":
            print(f"{args.email} is an operator: they sign in to the console with Google")
        else:
            print(f"{args.email} is no longer an operator")
            # Whatever console sessions they had end now, not at expiry.
            ended = store.end_all_sessions(account["id"])
            if ended:
                print(f"ended {ended} session(s)")
    elif args.account_command == "handle":
        account = store.account_by_email(args.email)
        if account is None:
            print(f"error: nobody registered as {args.email!r}", file=sys.stderr)
            return EXIT_USAGE
        store.set_account_handle(account["id"], None if args.none else args.handle)
        if args.none:
            # Never anything from their address: the name is a hostname, which
            # claude.ai shows and Anthropic receives (Erik, 2026-09-24).
            print(f"{args.email}: slots they claim from now on get neutral names like "
                  f"slot-4821, which they can rename; slots already named keep their names")
        else:
            print(f"{args.email}: slots they claim from now on are named {args.handle}-1, "
                  f"{args.handle}-2 and so on; slots already named keep their names")
    elif args.account_command == "quota":
        account = store.account_by_email(args.email)
        if account is None:
            print(f"error: nobody registered as {args.email!r}", file=sys.stderr)
            return EXIT_USAGE
        store.set_slot_quota(account["id"], args.count)
        held = store.held_slot_count(account["id"])
        print(f"{args.email} may claim {args.count} slots (holding {held})")
        if held > args.count:
            # Said out loud because it is the surprising half of the rule.
            print(f"They keep the {held} they have; this only stops them "
                  f"claiming more. Taking one back is 'slot release'.")
    return EXIT_OK


def _user_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.user_command == "add":
        password = args.password or generate_password()
        store.add_user(args.username, hash_password(password), args.role,
                       args.owner, time.time())
        _print_password(args.username, password, cfg)
    elif args.user_command == "list":
        users = store.list_users()
        if not users:
            print("no console accounts; the admin token still works")
            return EXIT_OK
        print(f"{'username':<20} {'role':<6} {'sees':<20} created")
        for u in users:
            sees = "the whole fleet" if u["role"] == "admin" else f"owner {u['owner']}"
            stamp = time.strftime("%Y-%m-%d", time.localtime(u["created_at"]))
            print(f"{u['username']:<20} {u['role']:<6} {sees:<20} {stamp}")
    elif args.user_command == "passwd":
        password = args.password or generate_password()
        if not store.set_password(args.username, hash_password(password)):
            print(f"error: no such account {args.username!r}", file=sys.stderr)
            return EXIT_USAGE
        _print_password(args.username, password, cfg)
    elif args.user_command == "remove":
        if not store.remove_user(args.username):
            print(f"error: no such account {args.username!r}", file=sys.stderr)
            return EXIT_USAGE
        print(f"removed {args.username}")
    return EXIT_OK


def _node_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.node_command == "add":
        token = store.add_node(args.node_id, args.owner, args.region, args.pinned_version,
                               args.rc_expected, now=time.time())
        _print_token(args.node_id, token, cfg)
    elif args.node_command == "list":
        latest = store.latest_heartbeats()
        emails = {a["id"]: a["email"] for a in store.list_accounts()}
        # Reserved goes last: scripts read the id off the front of each row.
        print(f"{'id':<20} {'owner':<14} {'region':<12} {'pinned':<10} {'enabled':<8} "
              f"{'rc':<4} {'last seen':<16} reserved")
        for node in store.list_nodes():
            hb = latest.get(node["id"])
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(hb["ts"])) if hb else "never"
            kept = node["reserved_for"]
            kept_for = emails.get(kept, "(account gone)") if kept else "-"
            print(f"{node['id']:<20} {node['owner']:<14} {node['region'] or '-':<12} "
                  f"{node['pinned_version'] or '-':<10} {'yes' if node['enabled'] else 'no':<8} "
                  f"{'on' if node['rc_expected'] else 'off':<4} {seen:<16} {kept_for}")
    elif args.node_command == "remove":
        store.remove_node(args.node_id)
        print(f"removed {args.node_id}")
    elif args.node_command == "rename":
        store.rename_node(args.node_id, args.new_id)
        print(f"renamed {args.node_id} to {args.new_id}: its slots, history, alerts and "
              f"sign-in came along, and its token is unchanged.")
        # Said now, because until it is done the box is refused: its heartbeat
        # names the node its token belongs to, and that name just changed.
        print("Its heartbeats are refused until the box says the new name. On it, set\n")
        print(f"  CCFLEET_NODE_ID={args.new_id}\n")
        print("in /etc/ccfleet/agent.env on a shared machine, or ~/.config/ccfleet/agent.env "
              "on an owner's node,")
        print("then rename the host itself: docs/runbooks.md, 'Rename a machine'. Its slots "
              "keep their ids; 'ccfleetd slot rename' renames them.")
    elif args.node_command == "rotate-token":
        _print_token(args.node_id, store.rotate_token(args.node_id), cfg)
    elif args.node_command == "enable":
        store.set_enabled(args.node_id, True)
        print(f"enabled {args.node_id}")
    elif args.node_command == "disable":
        store.set_enabled(args.node_id, False)
        print(f"disabled {args.node_id}")
    elif args.node_command == "pin":
        store.set_pinned_version(args.node_id, args.version)
        print(f"pinned {args.node_id} to {args.version}")
    elif args.node_command == "rc-expected":
        expected = args.state == "on"
        store.set_rc_expected(args.node_id, expected)
        verb = "will alert" if expected else "will not alert"
        print(f"{args.node_id}: {verb} when the Remote Control service is not active")
    elif args.node_command == "reserve":
        if args.none:
            store.reserve_machine(args.node_id, None)
            print(f"{args.node_id}: open to anybody with an allowance")
            return EXIT_OK
        email = args.email.strip()
        keeper = store.account_by_email(email)
        if keeper is None:
            print(f"error: nobody has signed in as {email!r}; they sign in once, then the "
                  "machine can be kept for them", file=sys.stderr)
            return EXIT_USAGE
        store.reserve_machine(args.node_id, keeper["id"])
        print(f"{args.node_id}: kept for {keeper['email']}")
    elif args.node_command == "hold":
        if args.none:
            if not store.unhold_owner_node(args.node_id):
                print(f"error: {args.node_id} is not counted as anybody's slot",
                      file=sys.stderr)
                return EXIT_USAGE
            print(f"{args.node_id}: no longer counted as a slot; the node itself is untouched")
            return EXIT_OK
        email = args.email.strip()
        holder = store.account_by_email(email)
        if holder is None:
            print(f"error: nobody has signed in as {email!r}; they sign in once, then their "
                  "node can be counted as their slot", file=sys.stderr)
            return EXIT_USAGE
        slot = store.hold_owner_node(args.node_id, holder["id"], unix_user=args.unix_user,
                                     now=time.time())
        print(f"{args.node_id}: counted as {holder['email']}'s slot (their Linux login "
              f"{slot['unix_user']}). A record only: nothing on it changes, and it is "
              "never handed out or wiped.")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    try:
        cfg = Config.from_env()
        if args.db:
            cfg = Config(**{**cfg.__dict__, "db_path": args.db})
        store = Store(cfg.db_path)
        try:
            if args.command == "node":
                return _node_command(args, store, cfg)
            if args.command == "user":
                return _user_command(args, store, cfg)
            if args.command == "slot":
                return _slot_command(args, store, cfg)
            if args.command == "account":
                return _account_command(args, store, cfg)
            if args.command == "payment":
                return _payment_command(args, store)
            if args.command == "price":
                return _price_command(args, store)
            # Only the running server reads Anthropic's release channels; a
            # one-off `check` never reaches out to the network.
            fetcher = claude_versions.default_fetcher if args.command == "serve" else None
            # Outage emails go from the serving loop alone, like everything else
            # that reaches out.
            mailer = build_mailer(cfg) if args.command == "serve" else None
            monitor = Monitor(store, cfg, build_notifier(cfg), channel_fetcher=fetcher,
                              mailer=mailer)
            if args.command == "check":
                for event in monitor.check_all():
                    alert = event["alert"]
                    print(f"{event['event']:<7} {alert['node_id']:<20} {alert['rule']:<22} "
                          f"{alert['message']}")
                return EXIT_OK
            if args.command == "alert-test":
                ok = monitor._notifier.send("ccfleet test alert: notifications are wired up")
                print("sent" if ok else "delivery failed (see log)")
                return EXIT_OK if ok else EXIT_ERROR
            cfg.require_admin_token()
            serve(Context(store, cfg, monitor))
            return EXIT_OK
        finally:
            store.close()
    except (ConfigError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
