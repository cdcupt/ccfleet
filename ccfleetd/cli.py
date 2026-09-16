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
    return parser


def _print_token(node_id: str, token: str, cfg: Config) -> None:
    url = cfg.public_url or f"http://{cfg.bind_host}:{cfg.bind_port}"
    print(f"node {node_id} registered. Token (shown once):\n")
    print(f"  {token}\n")
    print("Put this in ~/.config/ccfleet/agent.env on the node:\n")
    print(f"  CCFLEET_URL={url}")
    print(f"  CCFLEET_NODE_ID={node_id}")
    print(f"  CCFLEET_NODE_TOKEN={token}")


def _node_command(args: argparse.Namespace, store: Store, cfg: Config) -> int:
    if args.node_command == "add":
        token = store.add_node(args.node_id, args.owner, args.region, args.pinned_version,
                               args.rc_expected, now=time.time())
        _print_token(args.node_id, token, cfg)
    elif args.node_command == "list":
        latest = store.latest_heartbeats()
        print(f"{'id':<20} {'owner':<14} {'region':<12} {'pinned':<10} {'enabled':<8} last seen")
        for node in store.list_nodes():
            hb = latest.get(node["id"])
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(hb["ts"])) if hb else "never"
            print(f"{node['id']:<20} {node['owner']:<14} {node['region'] or '-':<12} "
                  f"{node['pinned_version'] or '-':<10} {'yes' if node['enabled'] else 'no':<8} "
                  f"{seen}")
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
