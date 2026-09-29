#!/usr/bin/env python3
"""Real OpenSSH reuse acceptance check in an explicitly marked disposable container.

Run only in the dedicated fixture image, with --network none and no host mounts
or published ports. Creates one synthetic user, keys and loopback sshd, then
removes that fixture. No Claude credentials, projects or model calls are used.
The reported timing is controlled loopback latency, not an internet benchmark.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import pwd
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = Path("/etc/ccfleet-transport-fixture")
MARKER_TEXT = "isolated transport fixture\n"
USER = "ccfleet-transport-fixture"
UID = 19991
ALIAS = "ccfleet-transport-loopback"

FORCED = r'''import json,os,struct,sys
from pathlib import Path
events = Path(sys.argv[1])
with events.open("a") as stream:
    stream.write(json.dumps({"connection":os.environ["SSH_CONNECTION"]})+"\n")
if os.environ.get("SSH_ORIGINAL_COMMAND") != "ccfleet-inference-v1":
    raise SystemExit(2)
raw = sys.stdin.buffer.read(4)
if len(raw) != 4: raise SystemExit(2)
size = struct.unpack("!I",raw)[0]
if not 0 < size < 1024: raise SystemExit(2)
request = json.loads(sys.stdin.buffer.read(size))
if request != {"version":2,"operation":"status"}: raise SystemExit(2)
def frame(value):
    raw=json.dumps(value,separators=(",",":")).encode()
    sys.stdout.buffer.write(struct.pack("!I",len(raw))+raw)
frame({"version":2,"status":200,"headers":{"content-type":"application/json"}})
frame({"ready":True,"protocol":2})
sys.stdout.buffer.write(struct.pack("!I",0))
sys.stdout.buffer.flush()
'''

PROXY = r'''import json,os,select,signal,socket,sys
from pathlib import Path
events=Path(sys.argv[2])
def record(kind):
    own=Path("/proc/self/stat").read_text().rpartition(")")[2].split()
    with events.open("a") as stream:
        stream.write(json.dumps({"kind":kind,"pid":os.getpid(),"identity":own[19]})+"\n")
def terminate(*_): raise SystemExit(0)
signal.signal(signal.SIGTERM,terminate)
signal.signal(signal.SIGINT,terminate)
peer=None
record("started")
try:
    peer=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=5)
    peer.settimeout(None)
    while True:
        ready,_,_=select.select([0,peer],[],[],10)
        if not ready: continue
        if 0 in ready:
            chunk=os.read(0,65536)
            if not chunk: break
            peer.sendall(chunk)
        if peer in ready:
            chunk=peer.recv(65536)
            if not chunk: break
            data=memoryview(chunk)
            while data:
                count=os.write(1,data)
                if count<=0: raise OSError("pipe closed")
                data=data[count:]
finally:
    if peer is not None: peer.close()
    record("ended")
'''


class CheckError(RuntimeError):
    pass


def available() -> bool:
    return (os.geteuid() == 0 and Path("/.dockerenv").is_file() and MARKER.is_file()
            and MARKER.read_text() == MARKER_TEXT and shutil.which("sshd") is not None)


def command(args, *, timeout=15, **options):
    result = subprocess.run(args, capture_output=True,
                            timeout=timeout, check=False, **options)
    if result.returncode:
        raise CheckError("synthetic fixture command failed: " + Path(args[0]).name)
    return result.stdout


def write(path: Path, data: str, mode: int = 0o600) -> None:
    path.write_text(data)
    path.chmod(mode)


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def eventually(check, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.025)
    raise CheckError("bounded synthetic fixture condition was not confirmed")


def check_transport(*, samples: int = 5, expected_sha256: str = "") -> dict:
    if not available():
        raise CheckError("requires the explicitly marked disposable root container with sshd")
    if type(samples) is not int or not 2 <= samples <= 10:
        raise CheckError("samples must be between 2 and 10")
    for lookup, value in ((pwd.getpwnam, USER), (pwd.getpwuid, UID)):
        try:
            lookup(value)
        except KeyError:
            continue
        raise CheckError("synthetic fixture account already exists; use a fresh container")
    source = Path(__file__).resolve().parents[1] / "ccfleet_agent" / "inference_client.py"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise CheckError("transport source changed after its verification snapshot")
    spec = importlib.util.spec_from_file_location("fixture_inference_client", source)
    if spec is None or spec.loader is None:
        raise CheckError("transport module is unavailable")
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    ssh = shutil.which("ssh")
    sshd = shutil.which("sshd")
    keygen = shutil.which("ssh-keygen")
    if not ssh or not sshd or not keygen:
        raise CheckError("fixture requires OpenSSH client, server and key generator")
    user_created = False
    daemon = None
    with tempfile.TemporaryDirectory(prefix="ccf-ssh-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o755)  # The unprivileged forced command must traverse this parent.
        private = root / "private"
        private.mkdir(mode=0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        forced_events = home / "forced.jsonl"
        proxy_events = private / "proxy.jsonl"
        forced, proxy = root / "forced.py", private / "proxy.py"
        write(forced, FORCED, 0o644)
        write(proxy, PROXY)
        log_path = private / "sshd.log"
        try:
            command(["useradd", "--no-create-home", "--home-dir", str(home), "--uid", str(UID),
                     "--shell", "/bin/sh", USER])
            user_created = True
            # Unlock only this synthetic account; its password remains invalid,
            # and sshd disables every password/interactive authentication method.
            command(["usermod", "--password", "x", USER])
            os.chown(home, UID, UID)
            for name in ("client", "host", "wrong-host"):
                command([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private / name)])
            public = (private / "client.pub").read_text().split()
            ssh_home = home / ".ssh"
            ssh_home.mkdir(mode=0o700)
            os.chown(ssh_home, UID, UID)
            authorized = ssh_home / "authorized_keys"
            write(authorized, "restrict " + " ".join(public[:2]) + "\n")
            os.chown(authorized, UID, UID)
            pinned = private / "known_hosts"
            wrong = private / "wrong_known_hosts"
            for key, target in (("host.pub", pinned), ("wrong-host.pub", wrong)):
                public_host = " ".join((private / key).read_text().split()[:2])
                write(target, ALIAS + " " + public_host + "\n")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            config = private / "sshd_config"
            force = shlex.join([sys.executable, "-I", str(forced), str(forced_events)])
            write(config, f"""Port {port}
ListenAddress 127.0.0.1
HostKey {private / 'host'}
PidFile {runtime / 'sshd.pid'}
AuthorizedKeysFile {authorized}
AllowUsers {USER}
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
AuthenticationMethods publickey
UsePAM no
StrictModes yes
AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
PermitTunnel no
PermitTTY no
PermitUserRC no
PermitUserEnvironment no
ForceCommand {force}
LogLevel VERBOSE
MaxSessions 10
""")
            Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
            command([sshd, "-t", "-f", str(config)])
            with log_path.open("wb") as log:
                daemon = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                          start_new_session=True)

            def listening():
                if daemon.poll() is not None:
                    raise CheckError("isolated sshd ended unexpectedly")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        return True
                except OSError:
                    return False

            eventually(listening)
            environment = {"PATH": os.defpath, "HOME": str(private), "LANG": "C"}

            def base(pin=pinned):
                return [ssh, "-T", "-F", "/dev/null", "-o", "Port=" + str(port),
                        "-o", "ProxyCommand=" + shlex.join(
                            [sys.executable, "-I", str(proxy), str(port), str(proxy_events)]),
                        "-o", "HostKeyAlias=" + ALIAS, "-o", "UserKnownHostsFile=" + str(pin),
                        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
                        "-o", "UpdateHostKeys=no", "-o", "IdentitiesOnly=yes",
                        "-o", "IdentityFile=" + str(private / "client"),
                        "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                        "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                        "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ConnectTimeout=5",
                        USER + "@" + ALIAS]

            def accepted():
                return sum("Accepted publickey for " + USER in line
                           for line in log_path.read_text().splitlines())

            def probe(factory):
                began = time.perf_counter()
                result = transport.check_status(factory, lambda: environment, timeout=8)
                if result != {"ready": True, "protocol": 2}:
                    raise CheckError("real SSH status framing was incorrect")
                return (time.perf_counter() - began) * 1000

            session = transport.TransportSession(base, lambda: environment, private, timeout=8)
            began = time.perf_counter()
            try:
                try:
                    session.start()
                except transport.RelayError as exc:
                    # Only this isolated synthetic sshd's log, never a host or
                    # customer log, is available to the acceptance fixture.
                    detail = " | ".join(log_path.read_text().splitlines()[-6:])
                    raise CheckError("synthetic SSH authentication failed: " + detail) from exc
                startup = (time.perf_counter() - began) * 1000
                if events(forced_events):
                    raise CheckError("the no-command SSH master invoked the forced command")
                first_connections = accepted()
                if first_connections != 1:
                    raise CheckError("master startup did not produce one authenticated connection")
                def factory():
                    return session.command(["ccfleet-inference-v1"])
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda _: probe(factory), range(2)))
                initial = events(forced_events)
                if (len(initial) != 2 or len({item["connection"] for item in initial}) != 1
                        or accepted() != first_connections):
                    raise CheckError("two SSH channels did not share one authenticated connection")
                reused = [probe(factory) for _ in range(samples)]
                stale_command = session.command(["ccfleet-inference-v1"])
                master = session._process
            finally:
                session.close()
            if master is None or master.poll() is None:
                raise CheckError("owned SSH master did not exit")
            before = accepted()
            proxy_before = len([item for item in events(proxy_events) if item["kind"] == "started"])
            refused = subprocess.run(stale_command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=environment, timeout=5, check=False)
            if refused.returncode == 0 or accepted() != before:
                raise CheckError("closed-master channel opened an unexpected connection")
            proxy_after = len([item for item in events(proxy_events) if item["kind"] == "started"])
            if proxy_after != proxy_before:
                raise CheckError("closed-master channel fell back to the original proxy")
            try:
                session.command(["ccfleet-inference-v1"])
            except transport.RelayError:
                pass
            else:
                raise CheckError("a closed session produced a new channel command")
            wrong_session = transport.TransportSession(lambda: base(wrong), lambda: environment,
                                                        private, timeout=5)
            before = accepted()
            try:
                wrong_session.start()
            except transport.RelayError:
                pass
            else:
                raise CheckError("an incorrect pinned SSH host key was accepted")
            finally:
                wrong_session.close()
            if accepted() != before:
                raise CheckError("wrong host pin reached authenticated session access")
            cold = [probe(lambda: [*base(), "ccfleet-inference-v1"]) for _ in range(samples)]
            if accepted() != 1 + samples:
                raise CheckError("cold/reused authenticated connection counts did not match")

            def proxies_ended():
                observed = events(proxy_events)
                for item in observed:
                    if item["kind"] != "started":
                        continue
                    try:
                        current = Path(f"/proc/{item['pid']}/stat").read_text()
                    except FileNotFoundError:
                        continue
                    fields = current.rpartition(")")[2].split()
                    # PIDs only identify this fixture together with the Linux
                    # process start identity. Never signal a recorded PID. A
                    # zombie has exited and cannot relay bytes; its container
                    # init reaps it. OpenSSH may kill a proxy before finally.
                    if fields[19] == item["identity"] and fields[0] != "Z":
                        return False
                return True

            try:
                eventually(proxies_ended)
            except CheckError as exc:
                raise CheckError("synthetic proxy remained alive after its SSH owner exited") \
                    from exc
            if any(private.glob("m*/s")) or any(path.is_dir() for path in private.glob("m*")):
                raise CheckError("an owned SSH control directory was left behind")
            return {"schema": 1, "scope": "isolated_loopback_only", "helper_sha256": digest,
                    "master_did_not_invoke_forced_command": True,
                    "two_channels_one_connection": True, "host_pin_failure_refused": True,
                    "closed_master_no_fallback": True, "owned_master_and_proxy_cleanup": True,
                    "authenticated_connections": accepted(), "model_requests": 0,
                    "latency_ms": {"samples_each": samples, "master_startup": round(startup, 2),
                                   "cold_median": round(statistics.median(cold), 2),
                                   "reused_median": round(statistics.median(reused), 2)},
                    "not_an_internet_benchmark": True}
        finally:
            if daemon is not None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=3)
            if user_created:
                command(["userdel", USER])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args(argv)
    try:
        report = check_transport(samples=args.samples, expected_sha256=args.expected_sha256)
    except (CheckError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("transport check failed: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
