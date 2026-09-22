"""Command line entry point: ``ccfleetd serve`` and node administration."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from typing import Optional

from . import __version__
from .api import Context, serve
from .config import Config, ConfigError
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
    pin = node.add_parser("pin", help="set the pinned Claude Code version")
    pin.add_argument("node_id")
    pin.add_argument("version")
    rc = node.add_parser("rc-expected",
                         help="turn the remote_control_down alert on or off for a node")
    rc.add_argument("node_id")
    rc.add_argument("state", choices=("on", "off"))

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
        slot = store.add_slot(args.slot_id, args.machine, args.unix_user,
                              now=time.time())
        print(f"slot {slot['id']} declared on {slot['node_id']} "
              f"as {slot['unix_user']}, state {slot['state']}")
        print(f"Create it on the machine with: sudo node/slot-add.sh "
              f"--slot {slot['unix_user']}")
    elif args.slot_command == "list":
        rows = store.list_slots(node_id=args.machine)
        if not rows:
            print("no slots declared")
            return EXIT_OK
        print(f"{'slot':<16} {'machine':<14} {'unix user':<12} {'state':<10} held by")
        for r in rows:
            print(f"{r['id']:<16} {r['node_id']:<14} {r['unix_user']:<12} "
                  f"{r['state']:<10} {r['held_by'] or '-'}")
    elif args.slot_command == "release":
        # Only ever starts the wipe. The slot does not become free here — it
        # becomes free when the machine reports the wipe finished, because only
        # that proves the Linux user and its files are gone. A missing slot is
        # already a StoreError, which main() prints; a second check would be
        # unreachable rather than defensive.
        store.begin_release(args.slot_id)
        print(f"{args.slot_id} is releasing. It stays held until the wipe "
              f"finishes on the machine.")
        print(f"Run there: sudo node/slot-remove.sh --slot "
              f"{store.get_slot(args.slot_id)['unix_user']}")
    elif args.slot_command == "remove":
        store.remove_slot(args.slot_id)
        print(f"{args.slot_id} is no longer declared on this fleet")
    elif args.slot_command == "capacity":
        if not store.set_machine_capacity(args.machine, args.count):
            print(f"error: no such machine {args.machine!r}", file=sys.stderr)
            return EXIT_USAGE
        print(f"{args.machine} may hold {args.count} slots")
    return EXIT_OK


def _account_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.account_command == "list":
        rows = store.list_accounts()
        if not rows:
            print("nobody has registered yet")
            return EXIT_OK
        print(f"{'email':<32} {'role':<6} {'allowance':<10} holding")
        for a in rows:
            held = store.held_slot_count(a["id"])
            print(f"{a['email']:<32} {a['role']:<6} {a['slot_quota']:<10} {held}")
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
        print(f"{'id':<20} {'owner':<14} {'region':<12} {'pinned':<10} {'enabled':<8} "
              f"{'rc':<4} last seen")
        for node in store.list_nodes():
            hb = latest.get(node["id"])
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(hb["ts"])) if hb else "never"
            print(f"{node['id']:<20} {node['owner']:<14} {node['region'] or '-':<12} "
                  f"{node['pinned_version'] or '-':<10} {'yes' if node['enabled'] else 'no':<8} "
                  f"{'on' if node['rc_expected'] else 'off':<4} {seen}")
    elif args.node_command == "remove":
        store.remove_node(args.node_id)
        print(f"removed {args.node_id}")
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
            monitor = Monitor(store, cfg, build_notifier(cfg))
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
