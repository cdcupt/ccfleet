"""Finder/Ghostty client for a private Antigravity workspace session on macOS."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import runpy
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from types import SimpleNamespace

RESOURCES = Path(__file__).resolve().parent
PACKAGE = RESOURCES if (RESOURCES / "ccfleet_agent").is_dir() else RESOURCES.parents[1]
sys.path.insert(0, str(PACKAGE))
from ccfleet_agent import workspace_io as wire  # noqa: E402
from ccfleet_agent.workspace_tools import COMMANDS, Workspace  # noqa: E402

FLEET = RESOURCES / "ccfleet" if PACKAGE == RESOURCES else RESOURCES.parent / "ccfleet"
ORIGIN = "https://ccfleet.io"
PAGE = ORIGIN + "/account/gemini#subscription"
APP_VERSION = "1"


def dialog(script, *args):
    proc = subprocess.run(
        ["/usr/bin/osascript", "-", *args],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def approve(folder, action, detail):
    script = """on run argv
set info to (item 1 of argv) & return & (item 2 of argv) & return & return & (item 3 of argv)
set resultButton to display dialog info with title "CCFleet · Local workspace" ¬
    buttons {"Cancel", "Allow once"} default button "Cancel" cancel button "Cancel"
return button returned of resultButton
end run"""
    return dialog(script, action, str(folder), detail) == "Allow once"


def tool_worker(path, test):
    def cancelled(*_):
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, cancelled)
    ws = Workspace(path, lambda action, detail: approve(path, action, detail), test)
    try:
        while True:
            raw = sys.stdin.buffer.readline(wire.MAX_FRAME + 1)
            if not raw:
                return 0
            if not raw.endswith(b"\n") or len(raw) > wire.MAX_FRAME:
                return 2
            result = ws.rpc(json.loads(raw))
            if result is not None:
                sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                sys.stdout.buffer.flush()
    finally:
        ws.close()


def pair(fleet, code=None):
    if code is None:
        subprocess.run(["/usr/bin/open", PAGE], check=True)
        code = getpass.getpass(
            "Paste the Antigravity connection code from your slot page: "
        ).strip()
    if not code.startswith("agy1-ccf_pair_"):
        raise ValueError(
            "Use the Antigravity subscription pairing code from your private Mac slot."
        )
    fleet["cmd_login"](
        SimpleNamespace(
            server=ORIGIN, code=code[5:], name="Antigravity workspace", workspace_pairing=True
        )
    )


def wait_for_slot(fleet, device):
    command = [*fleet["ssh_transport_command"](device, tty=False), "ccfleet-antigravity-v1"]
    deadline = time.monotonic() + 110
    print("Connecting to your Antigravity slot through ccfleet.io…", flush=True)
    while True:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            pending = bytearray()
            expires = min(deadline, time.monotonic() + 14)
            while time.monotonic() < expires:
                if not select.select([child.stdout], [], [], max(0, expires - time.monotonic()))[0]:
                    break
                raw = os.read(child.stdout.fileno(), 65536)
                if not raw:
                    break
                pending.extend(raw)
                # Preserve coalesced terminal output following the hello packet.
                if len(pending) >= 5:
                    size = struct.unpack_from(">I", pending, 1)[0]
                    if size > 256:
                        raise ValueError("Unexpected slot response")
                    if len(pending) >= size + 5:
                        if pending[0:1] == b"E":
                            raise ValueError("The Antigravity slot is already in use.")
                        if bytes(pending[: size + 5]) != wire.frame(b"H", b"ccfleet-antigravity-1"):
                            raise ValueError("The slot does not support this workspace protocol.")
                        return child, pending[size + 5 :]
        except BaseException:
            stop(child)
            raise
        stop(child)
        if time.monotonic() >= deadline:
            raise ValueError(
                "The private Mac has not accepted this device. Check the connection "
                "on your slot page and keep the Mac awake."
            )
        time.sleep(2)


def stop(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for stream in (process.stdin, process.stdout):
        if stream:
            stream.close()


def interact(connection, pending, project, test):
    output = connection.stdin.fileno()
    terminal = sys.stdin.fileno()
    saved = termios.tcgetattr(terminal)
    previous_size = None
    tools = None
    try:
        tty.setraw(terminal)
        while True:
            size = os.get_terminal_size(terminal)
            if size != previous_size:
                wire.write(output, wire.frame(b"R", struct.pack(">HH", size.lines, size.columns)))
                previous_size = size
            for tag, payload in wire.unpack(pending):
                if tag == b"O":
                    wire.write(1, payload)
                elif tag == b"I":
                    stop(tools)
                    cmd = [
                        sys.executable,
                        "-I",
                        "-B",
                        str(Path(__file__).resolve()),
                        "--tools",
                        str(project),
                    ]
                    if test:
                        cmd += ["--test", test]
                    tools = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                elif tag == b"M" and tools:
                    wire.write(tools.stdin.fileno(), payload)
                else:
                    raise ValueError("Unexpected workspace record.")
            inputs = [terminal, connection.stdout] + ([tools.stdout] if tools else [])
            ready, _, _ = select.select(inputs, [], [], 0.4)
            for src in ready:
                data = os.read(src if src == terminal else src.fileno(), 65536)
                if not data:
                    if tools and src == tools.stdout:
                        raise ValueError("Local project tools stopped. Reopen the workspace.")
                    return
                if src == connection.stdout:
                    pending.extend(data)
                else:
                    wire.write(output, wire.frame(b"K" if src == terminal else b"M", data))
    finally:
        termios.tcsetattr(terminal, termios.TCSADRAIN, saved)
        stop(tools)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--tools", action="store_true")
    cli.add_argument("--test", choices=COMMANDS)
    cli.add_argument("--pair", action="store_true")
    cli.add_argument("project", type=Path)
    args = cli.parse_args()
    if args.tools:
        return tool_worker(args.project, args.test)
    if not os.isatty(0):
        raise ValueError("Open this workspace in Ghostty.")
    # A subscription pairing must never replace the user's regular Fleet Claude pairing.
    os.environ["CCFLEET_HOME"] = str(Path.home() / ".config/ccfleet/antigravity-workspace")
    fleet = runpy.run_path(str(FLEET))
    project = fleet["selected_project"](str(args.project))
    if args.pair or not fleet["load_config"]()["devices"]:
        pair(fleet)
    device = fleet["choose_device"](fleet["load_config"]())
    if device["server"] != ORIGIN or device["endpoint"] != "wss://ccfleet.io/api/cli/connect":
        raise ValueError("This workspace requires a ccfleet.io pairing.")
    print(
        f"Local project: {project}\nGoogle sign-in: your private Mac slot\n"
        "Ask Antigravity to use the local project tools. Saving a file or running the chosen "
        "test opens an approval on this computer.",
        flush=True,
    )
    remote, pending = wait_for_slot(fleet, device)
    try:
        interact(remote, pending, project, args.test)
    finally:
        stop(remote)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    except Exception as error:
        print(f"\nCCFleet: {error}", file=sys.stderr)
        sys.exit(1)
