#!/usr/bin/python3
"""First-party Mac registration client. The downloadable copy uses this same source."""

import base64
import fcntl
import hashlib
import io
import json
import os
import plistlib
import pwd
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ORIGIN = "https://ccfleet.io"
BUNDLE = ""
LABEL = "io.ccfleet.home-mac"
INTERVAL = 45


class Rejected(Exception):
    def __init__(self, status):
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(origin, action, token, fields=None):
    data = urllib.parse.urlencode(fields or {}).encode()
    req = urllib.request.Request(
        origin + "/api/home-macs/" + action,
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    # Do not forward the machine credential on a redirect.
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(8193)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise Rejected(exc.code) from None
    if len(raw) > 8192:
        raise ValueError("invalid controller reply")
    return json.loads(raw)


def write(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fp:
        tmp = Path(fp.name)
        try:
            os.fchmod(fp.fileno(), mode)
            fp.write(data)
            fp.flush()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def private_root(home):
    folder = home / "Library/Application Support/CCFleet/HomeMac"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The connector requires a private user-owned installation folder.")
    return folder


def identity(folder):
    token_file = folder / "credential"
    if not token_file.exists():
        write(token_file, secrets.token_hex(32).encode())
    token = token_file.read_text()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("Invalid local registration credential.")
    public = []
    for name in ("identity", "slot_host"):
        key = folder / name
        if not key.exists():
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)],
                check=True,
            )
        raw = subprocess.check_output(["/usr/bin/ssh-keygen", "-y", "-f", str(key)], timeout=5)
        public.append(" ".join(raw.decode("ascii").split()[:2]))
    return token, public


def enroll(folder, origin, token, keys, user):
    device = hashlib.sha256(token.encode()).hexdigest()
    message = f"ccfleet-home-enroll-v1\n{device}\n{keys[0]}\n{keys[1]}\n{user}\n".encode()
    proof = subprocess.run(
        [
            "/usr/bin/ssh-keygen",
            "-Y",
            "sign",
            "-n",
            "ccfleet-home-enroll-v1",
            "-f",
            str(folder / "identity"),
        ],
        input=message,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout.decode("ascii")
    return post(
        origin,
        "begin",
        token,
        {"public_key": keys[0], "host_key": keys[1], "unix_user": user, "proof": proof},
    )


def runtime_dir(home):
    return home / ".config/ccfleet/home-mac-runtime"


def install_runtime(home):
    if not BUNDLE:
        raise ValueError("Download the complete Mac connector from My machines.")
    runtime = runtime_dir(home)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe Mac runtime directory.")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(BUNDLE, validate=True))) as archive:
        for name in archive.namelist():
            if Path(name).name != name or not name.endswith(".py"):
                raise ValueError("Invalid runtime bundle.")
            write(runtime / name, archive.read(name))


def install_agent(home, folder):
    install_runtime(home)
    program = folder / "connector.py"
    if Path(__file__).resolve() != program.resolve():
        write(program, Path(__file__).read_bytes(), 0o700)
    agents = home / "Library/LaunchAgents"
    agents.mkdir(exist_ok=True, parents=True)
    plist = agents / (LABEL + ".plist")
    content = plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/python3", "-I", "-B", str(program), "--check-in"],
            "RunAtLoad": True,
            "StartInterval": INTERVAL,
        }
    )
    write(plist, content)
    domain = f"gui/{os.getuid()}"
    if subprocess.run(
        ["/bin/launchctl", "print", domain + "/" + LABEL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode:
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)


def check_in(folder, origin, token):
    result = post(origin, "check-in", token)
    if result.get("state") == "registered":
        write(folder / "registration", result["node_id"].encode("ascii"))
    return result


def main(args):
    if sys.platform != "darwin" or os.getuid() == 0:
        raise ValueError("Run the connector as the Mac user who owns the slot.")
    if args not in ([], ["--check-in"], ["--remove"]):
        raise ValueError("Usage: ccfleet-home-mac.command [--remove]")
    os.umask(0o077)
    user = pwd.getpwuid(os.getuid())
    home = Path(user.pw_dir)
    folder = private_root(home)
    background = args == ["--check-in"]
    if background:
        if (folder / "revoked").exists() or not (folder / "registration").is_file():
            return
        with (folder / "service.lock").open("a") as running:
            try:
                fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            sys.path.insert(0, str(runtime_dir(home)))
            import home_mac_runtime

            home_mac_runtime.run(
                home,
                folder,
                user.pw_name,
                (folder / "credential").read_text(),
                ORIGIN,
                post,
                write,
                Rejected,
            )
        return
    # Serialize repeat launches, including a user's double-click during setup.
    with (folder / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not background:
                print("The connector is already running. Check its Terminal window.")
            return
        if args == ["--remove"]:
            registration = folder / "registration"
            node = registration.read_text() if registration.is_file() else None
            local = folder / "credential"
            revoked = True
            if local.exists():
                try:
                    post(ORIGIN, "disconnect", local.read_text())
                except Rejected as exc:
                    revoked = exc.status == 403
                except OSError:
                    revoked = False
            plist = home / "Library/LaunchAgents" / (LABEL + ".plist")
            subprocess.run(
                ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            plist.unlink(missing_ok=True)
            private = home / ".config/ccfleet/private-owner"
            marker = private / "home-registration"
            if node and marker.is_file() and marker.read_text() == node:
                for name in (
                    "policy.json",
                    "agent.json",
                    "grants.json",
                    "maintenance.json",
                    "relay-quarantine.json",
                ):
                    (private / name).unlink(missing_ok=True)
                write(marker, b"removed")
                # A retained marker lets a new enrollment reuse this owned state directory.
            for name in (
                "identity",
                "identity.pub",
                "slot_host",
                "slot_host.pub",
                "credential",
                "registration",
                "revoked",
                "connector.py",
                "authorized_keys",
                "sshd_config",
            ):
                (folder / name).unlink(missing_ok=True)
            print(
                "Mac disconnected."
                if revoked
                else "Local connector removed. Use My machines to revoke it on Fleet."
            )
            return
        if (folder / "revoked").exists():
            if not background:
                print("Registration removed. Run with --remove before registering again.")
            return
        existing = home / ".config/ccfleet/private-owner"
        if existing.exists() and not (existing / "home-registration").is_file():
            raise ValueError("This Mac already has an operator-managed Fleet slot.")
        token, keys = identity(folder)
        try:
            reply = enroll(folder, ORIGIN, token, keys, user.pw_name)
        except Rejected as exc:
            if exc.status == 403:
                print("Registration unavailable. To reset this Mac, run with --remove.")
                return
            raise
        if reply["state"] == "pending":
            code = reply["code"]
            print(
                f"On your Fleet account, approve this Mac using {code[:5]}-{code[5:]}.", flush=True
            )
            print(ORIGIN + "/account/machines", flush=True)
            handoff = ORIGIN + "/account/machines/confirm#" + urllib.parse.urlencode({"code": code})
            subprocess.run(["/usr/bin/open", handoff], check=False)
            while time.time() < reply["expires_at"]:
                time.sleep(3)
                result = check_in(folder, ORIGIN, token)
                if result["state"] == "registered":
                    break
            else:
                raise ValueError("Code expired. Open the connector again for a new code.")
        result = check_in(folder, ORIGIN, token)
        if result["state"] != "registered":
            raise ValueError("Mac approval is still pending.")
        install_agent(home, folder)
        print("Registered. The connector will check in while you are logged in.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, Rejected, subprocess.SubprocessError, KeyError) as error:
        if isinstance(error, Rejected):
            message = f"Fleet returned HTTP {error.status}."
        elif isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            message = str(error)
        else:
            message = "The connector could not complete this step. Check the network and reopen it."
        print(message, file=sys.stderr)
        sys.exit(1)
