"""Reproduce the native agy / agy-gateway client-tool check on macOS.

Requires an installed Go 1.27.3+ toolchain and Google's signed agy 1.5.14.
Pass --go and --agy to specify their paths. Setup fetches the pinned GitHub
source and verifies Go modules. Build runs offline. Runtime tests use only
loopback, disposable homes, a synthetic credential and upstream's scripted
ACP agent. Neither Google nor CCFleet production handles a model request.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COMMIT = "9f3e671ad0e4c1b48d4ac7ee4ce095816a37ee25"
SOURCE = "https://github.com/sujoshua/agy-gateway.git"
BASE = """
(version 1)
(allow default)
(deny network*)
(deny file-read* file-write* (subpath "/Users") (subpath (param "OWNER")))
(deny file-write*)
(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.secd"))
"""
LOOPBACK = """
(allow network-bind network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
"""
WRITABLE = '(allow file-write* (literal "/dev/null") (literal "/dev/tty") (subpath (param "WORK")))'


def sandbox(extra, **paths):
    args = ["/usr/bin/sandbox-exec", "-D", f"OWNER={Path.home().resolve()}"]
    for key, path in paths.items():
        args += ["-D", f"{key}={path}"]
    return [*args, "-p", BASE + extra]


def build(root, go, env):
    src = root / "src"
    fetch_env = {
        **env,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for cmd in (
        ["init", "-q", str(src)],
        ["-C", str(src), "fetch", "-q", "--depth=1", SOURCE, COMMIT],
        ["-C", str(src), "checkout", "-q", "--detach", "FETCH_HEAD"],
    ):
        subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *cmd],
            env=fetch_env,
            check=True,
            timeout=90,
        )
    revision = subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=src, text=True)
    if revision.strip() != COMMIT:
        raise RuntimeError("unexpected upstream revision")
    shutil.copyfile(
        Path(__file__).with_name("workspace_test.go"),
        src / "internal/app/ccfleet_workspace_test.go",
    )
    goenv = {
        **env,
        "GOMODCACHE": str(root / "mods"),
        "GOCACHE": str(root / "cache"),
        "GOPATH": str(root / "gopath"),
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOFLAGS": "-mod=readonly",
        "CGO_ENABLED": "0",
        "GOTELEMETRY": "off",
    }
    subprocess.run(
        [go, "mod", "download"],
        cwd=src,
        env={**goenv, "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org"},
        check=True,
        timeout=180,
    )
    goroot = Path(
        subprocess.check_output([go, "env", "GOROOT"], env=goenv, cwd=src, text=True).strip()
    ).resolve(strict=True)
    if goroot in (Path.home(), *Path.home().parents) or not (goroot / "bin/go").is_file():
        raise RuntimeError("unexpected Go installation directory")
    locked = sandbox(
        WRITABLE + '(allow file-read* (subpath (param "GO_ROOT")))', WORK=root, GO_ROOT=goroot
    )
    goenv.update(GOPROXY="off", GOSUMDB="off")
    subprocess.run([*locked, go, "mod", "verify"], cwd=src, env=goenv, check=True, timeout=120)
    binary = root / "gateway.test"
    subprocess.run(
        [*locked, go, "test", "-c", "-o", binary, "./internal/app"],
        cwd=src,
        env=goenv,
        check=True,
        timeout=240,
    )
    return binary


def stop(process):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def trial(root, binary, agent, env, compat):
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        case = Path(tmp)
        host, project, shared, client = [
            case / name for name in ("host", "project", "shared", "client")
        ]
        for folder in (host, project, shared, client, host / "tmp", client / "tmp"):
            folder.mkdir(mode=0o700)
        proof = project / "proof.txt"
        original = "INPUT_" + secrets.token_hex(16)
        proof.write_text(original)
        config = client / ".gemini/antigravity-cli"
        config.mkdir(parents=True)
        (config / "settings.json").write_text('{"model":"mock-1"}')
        # Put the two processes in independent sandboxes. The signed CLI can see the
        # test project. The gateway can learn its contents only through a tool result.
        host_policy = (
            LOOPBACK
            + WRITABLE
            + """
(allow file-write* (subpath (param "SHARED")))
(deny file-read* file-write* (subpath (param "PROJECT")) (subpath (param "CLIENT")))
"""
        )
        host_cmd = sandbox(host_policy, WORK=host, SHARED=shared, PROJECT=project, CLIENT=client)
        hostenv = {
            **env,
            "HOME": str(host),
            "TMPDIR": str(host / "tmp"),
            "FIXTURE_FILE": str(proof),
            "FIXTURE_SHARED": str(shared),
            "FIXTURE_COMPAT": "1" if compat else "0",
        }
        gateway = subprocess.Popen(
            [*host_cmd, binary, "-test.run=^TestCCFleetLocalWorkspace$", "-test.timeout=60s"],
            cwd=host,
            env=hostenv,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            ready = shared / "url"
            deadline = time.monotonic() + 10
            while not ready.exists() and gateway.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            url = ready.read_text().strip()
            if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", url):
                raise RuntimeError("gateway did not start on loopback")
            client_policy = (
                LOOPBACK
                + WRITABLE
                + """
(allow file-write* (subpath (param "PROJECT")))
(deny file-read* file-write* (subpath (param "HOST")) (subpath (param "SHARED")))
"""
            )
            client_cmd = sandbox(
                client_policy, WORK=client, PROJECT=project, HOST=host, SHARED=shared
            )
            clientenv = {
                **env,
                "HOME": str(client),
                "TMPDIR": str(client / "tmp"),
                "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
                "AGY_LLM_GATEWAY_URL": url,
                "AGY_LLM_GATEWAY_API_KEY": "test-key",
                "AGY_LLM_GATEWAY_WIRE_PROTOCOL": "openai",
                "AGY_LLM_GATEWAY_MODELS": "mock-1",
                "NO_PROXY": "127.0.0.1,localhost",
            }
            client_proc = subprocess.Popen(
                [
                    *client_cmd,
                    agent,
                    "--mode",
                    "accept-edits",
                    "-p",
                    "Read proof.txt and apply the requested edit to this fixture.",
                ],
                cwd=project,
                env=clientenv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = client_proc.communicate(timeout=35)
            finally:
                stop(client_proc)
            (shared / "done").touch()
            if gateway.wait(timeout=5) != 0:
                raise RuntimeError("gateway fixture failed")
            if compat:
                if not (
                    client_proc.returncode == 0
                    and (shared / "read").read_text() == original
                    and proof.read_text().strip() == "Edited by the local agent."
                    and b"LOCAL_EDIT_COMPLETE" in stdout
                ):
                    raise RuntimeError("native local read/edit round trip failed")
            elif not (
                client_proc.returncode != 0
                and b"400" in stderr
                and b"max_completion_tokens" in stderr
                and not (shared / "read").exists()
                and proof.read_text() == original
            ):
                raise RuntimeError("the default gateway parameter contract changed")
        finally:
            stop(gateway)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default=shutil.which("go") or "go")
    parser.add_argument("--agy", type=Path, default=Path.home() / ".local/bin/agy")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this test uses macOS process isolation")
    with tempfile.TemporaryDirectory(prefix="agy-interop-", dir="/private/tmp") as tmp:
        root = Path(tmp)
        for name in ("empty-home", "tmp"):
            (root / name).mkdir(mode=0o700)
        env = {
            "HOME": str(root / "empty-home"),
            "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/bin:/bin",
            "TERM": "dumb",
            "LANG": "C",
            "GOMAXPROCS": "4",
        }
        agent = root / "agy"
        shutil.copy2(args.agy.resolve(strict=True), agent)
        subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "-R",
                (
                    '=identifier "cli" and anchor apple generic and '
                    'certificate leaf[subject.OU] = "EQHXZ8M8AV"'
                ),
                agent,
            ],
            check=True,
        )
        version = subprocess.check_output(
            [*sandbox(WRITABLE, WORK=root), agent, "--version"],
            cwd=root,
            env=env,
            text=True,
            timeout=10,
        ).strip()
        if version != "1.5.14":
            raise RuntimeError(f"this compatibility baseline targets agy 1.5.14, found {version}")
        executable = shutil.which(args.go)
        if not executable:
            parser.error("supply --go with the path to the Go toolchain")
        binary = build(root, str(Path(executable).resolve(strict=True)), env)
        subprocess.run(
            [
                *sandbox(LOOPBACK + WRITABLE, WORK=root),
                binary,
                "-test.run=^(TestToolLoop|TestStreamingToolLoop)$",
                "-test.timeout=25s",
            ],
            cwd=root,
            env=env,
            check=True,
            timeout=30,
        )
        trial(root, binary, agent, env, False)
        for _ in range(3):
            trial(root, binary, agent, env, True)
        print(
            json.dumps(
                {
                    "gateway_commit": COMMIT,
                    "agy": version,
                    "default_status": 400,
                    "verified_local_edits": 3,
                    "upstream": "scripted ACP fixture",
                    "google_model_requests": 0,
                    "production_route_tested": False,
                }
            )
        )


if __name__ == "__main__":
    main()
