"""Interactive operator enrollment on the serving Mac. Only receipts go to Fleet."""
from __future__ import annotations

import argparse
import datetime
import getpass
import importlib.util
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("private_mac_api", Path(__file__).with_name(
    "mac_api_model.py"))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def controller(host, args, receipt=None):
    # Run only the fixed controller CLI. The API key is never an SSH argument or payload.
    cmd = ["docker", "exec", "-i", "ccfleet", "ccfleetd", "api-model", *args]
    data = json.dumps(receipt).encode() if receipt else b""
    last = None
    home = Path(api.policy.authenticated_owner().pw_dir)
    connector = api.policy.directory(home) / "transport" / "ccfleet_tunnel_connect.py"
    routes = [[], ["-B", "en0"]]
    if connector.is_file():
        # The operator-installed reverse tunnel already uses this failover connector.
        proxy = shlex.join(["/usr/bin/python3", "-I", "-B", str(connector)])
        proxy += " --host %h --port %p"
        routes.insert(0, ["-o", "ProxyUseFdpass=yes", "-o", "ProxyCommand=" + proxy])
    for options in routes:
        ssh = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", *options]
        last = subprocess.run([*ssh, host, shlex.join(cmd)], input=data, capture_output=True,
                              timeout=25)
        if last.returncode == 0:
            return last.stdout
        if last.returncode != 255:
            break
    raise ValueError(last.stderr.decode(errors="replace").strip()[:300] or "controller unavailable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--check", action="store_true", help="check service and list endpoints")
    args = parser.parse_args()
    if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", args.controller):
        parser.error("this command needs the serving Mac and its configured SSH host")
    home = Path(api.policy.authenticated_owner().pw_dir)
    api.authorize(home)
    node = api.policy.read_json(home, "agent.json")["node_id"]
    entries = json.loads(controller(args.controller, ["endpoints", node]))
    if not entries:
        raise ValueError("pair the MacBook and create a single-slot Fleet gateway for this Mac")
    print("Choose the connection used by your MacBook.")
    entries.sort(key=lambda e: e["last_seen_at"], reverse=True)
    for idx, entry in enumerate(entries, 1):
        stamp = datetime.datetime.fromtimestamp(entry["last_seen_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"{idx}. {entry['name']}   {entry['id']}   last used {stamp}")
    if args.check:
        return 0
    if not sys.stdin.isatty():
        parser.error("enter the API key at the Mac mini terminal")
    index = int(input("Connection number: ").strip()) if len(entries) > 1 else 1
    if not 1 <= index <= len(entries):
        raise ValueError("invalid connection")
    key_id = entries[index-1]["id"]
    project = input("Google Cloud project number that owns the Gemini API key: ").strip()
    model = input("Gemini API model ID from Google AI Studio: ").strip()
    api.contract.account_fingerprint("gemini", project)
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]{1,88}", model):
        raise ValueError("invalid Gemini model ID")
    reservation = json.loads(controller(args.controller, [
        "reserve", key_id, node, "gemini", project, model]))
    api.contract.validate(reservation, credential=False)
    if (reservation["provider"] != "gemini" or reservation["model"] != model
            or reservation["account_fp"] != api.contract.account_fingerprint("gemini", project)):
        raise ValueError("controller returned a different reservation")
    store = api.credential_store()
    existing = store.get_password(api.SERVICE, reservation["binding_id"])
    secret = json.loads(existing)["api_key"] if existing else getpass.getpass("Gemini API key: ")
    receipt = api.save_profile(reservation, secret, store)
    controller(args.controller, ["grant", key_id, node, "-"], receipt)
    print("Gemini is enrolled. On the MacBook, start ccfleet local and select Gemini in /model.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(str(exc) if type(exc) is ValueError else "Enrollment did not complete.",
              file=sys.stderr)
        sys.exit(1)
