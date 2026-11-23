"""Publish a frozen client branch to the repository approved on this computer."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import select
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode

GIT = "/usr/bin/git"
CURL = "/usr/bin/curl"
API_LIMIT = 2 * 1024 * 1024
TIMEOUT = 120
OID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
REVIEW_FIELDS = {"title": 256, "body": 32768, "base": 255}
UNCERTAIN = " The remote operation may have completed. Check the branch or review before retrying."


class PublishError(ValueError):
    """Only fixed diagnostics or validated repository and ref information reach the editor."""


def _target(remote):
    if not isinstance(remote, str) or len(remote) > 512:
        raise PublishError("Configure the publishing repository on this computer.")
    m = re.fullmatch(r"https://(github\.com|gitlab\.com)/([A-Za-z0-9._/-]+)", remote)
    if not m:
        raise PublishError("The pinned publishing repository is invalid.")
    host, project = m.groups()
    parts = project.split("/")
    if (
        len(parts) < 2
        or (host == "github.com" and len(parts) != 2)
        or project.endswith(".git")
        or any(p in ("", ".", "..", "-") or p.endswith(".git") for p in parts)
    ):
        raise PublishError("The pinned publishing repository is invalid.")
    return host, project


def _request(value):
    if not isinstance(value, dict):
        raise PublishError("Invalid publish request.")
    if value == {"action": "push"}:
        return "push"
    if value.get("action") != "create_review" or value.keys() - {"action", *REVIEW_FIELDS}:
        raise PublishError("Unsupported publish action or fields.")
    for key in value.keys() & REVIEW_FIELDS.keys():
        field = value[key]
        if (
            not isinstance(field, str)
            or "\0" in field
            or len(field.encode("utf-8")) > REVIEW_FIELDS[key]
        ):
            raise PublishError("Invalid review field.")
    if "title" in value and not value["title"].strip():
        raise PublishError("A review title cannot be empty.")
    return "create_review"


def _helper(host):
    name = "gh" if host == "github.com" else "glab"
    for prefix in (Path("/opt/homebrew"), Path("/usr/local"), Path("/usr")):
        binary = prefix / "bin" / name
        if binary.is_file() and os.access(binary, os.X_OK):
            real = binary.resolve()
            # Only a fixed installer location may provide the credential helper.
            if (
                real.is_relative_to(prefix)
                and real.stat().st_uid in (0, os.getuid())
                and not real.stat().st_mode & 0o002
            ):
                return str(binary)
    raise PublishError(f"Install and sign in to {name} on the connecting Mac to publish.")


def _environment(scratch, host):
    # Git and curl never consult the user's checkout, configuration or .netrc.
    return {
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "PATH": "/usr/bin:/bin",
        "XDG_CONFIG_HOME": str(scratch),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _helper_environment(scratch, host):
    # OS credential stores may depend on the account's real HOME. Only the fixed
    # provider CLI receives it, with an explicit host config and no ambient tokens.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    env = _environment(scratch, host)
    env.update(HOME=str(home), DO_NOT_TRACK="1")
    if host == "github.com":
        env.update(
            GH_CONFIG_DIR=str(home / ".config/gh"),
            GH_PROMPT_DISABLED="1",
            GH_NO_UPDATE_NOTIFIER="1",
        )
    else:
        env.update(
            GLAB_CONFIG_DIR=str(home / ".config/glab-cli"),
            GLAB_PROMPT_DISABLED="true",
            NO_PROMPT="true",
        )
    return env


def _run(args, cwd, env, check, *, data=b"", limit=32768, mutation=False):
    """Bound subprocess lifetime and output; never echo provider stderr or raw credentials."""
    check()
    with subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as proc:
        complete = False
        try:
            deadline = time.monotonic() + TIMEOUT
            if data:
                os.set_blocking(proc.stdin.fileno(), False)
            else:
                proc.stdin.close()
            result = bytearray()
            while not proc.stdout.closed or proc.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise PublishError("The publishing operation timed out.")
                reads, writes, _ = select.select(
                    [] if proc.stdout.closed else [proc.stdout],
                    [proc.stdin] if data else [],
                    [],
                    0.12,
                )
                if writes:
                    try:
                        data = data[os.write(proc.stdin.fileno(), data) :]
                    except BrokenPipeError:
                        data = b""
                    if not data:
                        proc.stdin.close()
                if reads:
                    part = os.read(proc.stdout.fileno(), 65536)
                    if not part:
                        proc.stdout.close()
                    elif len(result) + len(part) > limit:
                        raise PublishError("The publishing response exceeded its size limit.")
                    else:
                        result.extend(part)
            returncode = proc.wait()
            complete = True
            return returncode, bytes(result)
        except (OSError, PublishError) as exc:
            if mutation:
                raise PublishError("Publishing was interrupted." + UNCERTAIN) from exc
            raise
        finally:
            if not complete:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


class _Publisher:
    def __init__(self, remote, scratch, check):
        self.remote = remote
        self.host, self.project = _target(remote)
        self.scratch = scratch
        self.gitdir = scratch / "repo.git"
        self.env = _environment(scratch, self.host)
        self.check = check
        self.helper = _helper(self.host)
        self.helper_env = _helper_environment(scratch, self.host)
        self.commit = None
        self.branch = None

    def git(self, *args, protocol="file", mutation=False):
        cmd = [
            GIT,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.unpackLimit=1",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.proxy=",
            "-c",
            "http.sslVerify=true",
            "-c",
            "credential.helper=",
        ]
        if protocol == "https":
            # Git appends the helper action (get/store/erase). Replace its environment
            # before invoking the one absolute helper binary chosen by the client.
            helper = shlex.join(
                [
                    "/usr/bin/env",
                    "-i",
                    *(key + "=" + val for key, val in self.helper_env.items()),
                    self.helper,
                    "auth",
                    "git-credential",
                ]
            )
            cmd.extend(
                [
                    "-c",
                    f"credential.https://{self.host}.helper=!{helper}",
                    "-c",
                    "credential.useHttpPath=true",
                ]
            )
        cmd.extend(["--git-dir", str(self.gitdir), *args])
        return _run(
            cmd,
            self.scratch,
            {**self.env, "GIT_ALLOW_PROTOCOL": protocol},
            self.check,
            mutation=mutation,
        )

    def ref(self, branch):
        if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
            raise PublishError("Select a valid named branch to publish.")
        ref = "refs/heads/" + branch
        if self.git("check-ref-format", ref)[0]:
            raise PublishError("Select a valid named branch to publish.")
        return ref

    def import_branch(self, branch, bundle):
        ref = self.ref(branch)
        path = Path(bundle)
        info = path.lstat()
        if (
            not path.is_absolute()
            or path != path.resolve()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or not 0 < info.st_size <= 64 << 20
        ):
            raise PublishError("The exported branch bundle is unavailable.")
        # list-heads reads the bundle without importing local repo configuration or hooks.
        code, listing = self.git("bundle", "list-heads", str(path))
        fields = listing.decode("ascii", errors="replace").split()
        if code or len(fields) != 2 or not OID.fullmatch(fields[0]) or fields[1] != ref:
            raise PublishError("The bundle must contain exactly the selected branch.")
        oid = fields[0]
        fmt = "sha1" if len(oid) == 40 else "sha256"
        if (
            self.git("init", "--bare", "--template=", f"--object-format={fmt}")[0]
            or self.git("bundle", "verify", str(path))[0]
            or self.git("fetch", "--no-tags", "--no-recurse-submodules", str(path), f"{ref}:{ref}")[
                0
            ]
            or self.git("fsck", "--strict", "--no-reflogs")[0]
        ):
            raise PublishError("Git could not verify the exported branch history.")
        code, commit = self.git("rev-parse", "--verify", ref + "^{commit}")
        if code or commit.strip() != oid.encode():
            raise PublishError("The verified branch does not point to its exported commit.")
        self.branch, self.commit = branch, oid
        return ref

    def token(self):
        request = f"protocol=https\nhost={self.host}\npath={self.project}.git\n\n".encode()
        code, raw = _run(
            [self.helper, "auth", "git-credential", "get"],
            self.scratch,
            self.helper_env,
            self.check,
            data=request,
            limit=8192,
        )
        fields = {}
        for line in raw.splitlines():
            if not line:
                continue
            k, sep, v = line.partition(b"=")
            if k == b"capability[]" and sep and re.fullmatch(rb"[A-Za-z0-9_.-]{1,64}", v):
                continue
            if (
                not sep
                or k in fields
                or k
                not in {
                    b"protocol",
                    b"host",
                    b"path",
                    b"username",
                    b"password",
                    b"password_expiry_utc",
                    b"oauth_refresh_token",
                }
                or len(v) > 4096
            ):
                raise PublishError("The local credential helper returned an invalid response.")
            fields[k] = v
        token = fields.get(b"password", b"")
        # Git helpers may respond with only username/password. The request already
        # pins HTTPS, host and path. Reject contradictory coordinates when echoed.
        if (
            code
            or fields.get(b"protocol", b"https") != b"https"
            or fields.get(b"host", self.host.encode()) != self.host.encode()
            or fields.get(b"path", (self.project + ".git").encode())
            != (self.project + ".git").encode()
            or not re.fullmatch(rb"[!-~]{1,256}", fields.get(b"username", b""))
            or not re.fullmatch(rb"[!-~]{1,4096}", token)
        ):
            raise PublishError("Sign in to the repository host's CLI on the connecting Mac.")
        return token

    def api(self, method, path, token, payload=None):
        origin = (
            "https://api.github.com" if self.host == "github.com" else "https://gitlab.com/api/v4"
        )
        command = [
            CURL,
            "--disable",
            "--silent",
            "--proto",
            "=https",
            "--noproxy",
            "*",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "10",
            "--max-time",
            str(TIMEOUT),
            "--header",
            "@-",
            "--header",
            "Accept: application/json",
            "--request",
            method,
            "--write-out",
            "\\n%{http_code}",
            "--url",
            origin + path,
        ]
        if payload is not None:
            body = self.scratch / "review.json"
            body.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            command.extend(
                ["--header", "Content-Type: application/json", "--data-binary", "@" + str(body)]
            )
        rc, raw = _run(
            command,
            self.scratch,
            self.env,
            self.check,
            data=b"Authorization: Bearer " + token + b"\n",
            limit=API_LIMIT,
            mutation=method == "POST",
        )
        content, _, status = raw.rpartition(b"\n")
        if rc or not status.isdigit():
            raise PublishError(
                "The repository API could not be reached." + (UNCERTAIN if method == "POST" else "")
            )
        code = int(status)
        if code in (409, 422) and method == "POST":
            return None
        if code not in (200, 201):
            raise PublishError(
                f"The repository API returned HTTP {code}."
                + (UNCERTAIN if method == "POST" and (code == 408 or code >= 500) else "")
            )
        try:
            return json.loads(content)
        except (ValueError, RecursionError):
            raise PublishError(
                "The repository API returned an invalid response."
                + (UNCERTAIN if method == "POST" else "")
            ) from None

    def push(self, ref):
        rc, _ = self.git(
            "push",
            "--no-verify",
            "--no-follow-tags",
            "--recurse-submodules=no",
            self.remote,
            ref + ":" + ref,
            protocol="https",
            mutation=True,
        )
        if rc:
            raise PublishError(
                "Git did not confirm the push. Check authentication and branch protection."
                + UNCERTAIN
            )
        return f"Pushed {self.branch} at {self.commit[:12]} to {self.remote}."

    def review(self, ref, request):
        rc, output = self.git(
            "ls-remote", "--exit-code", "--heads", self.remote, ref, protocol="https"
        )
        if rc or output.strip().split() != [self.commit.encode(), ref.encode()]:
            raise PublishError(
                "Push the current branch and commit to the approved repository first."
            )
        github = self.host == "github.com"
        root = "/repos/" + self.project if github else "/projects/" + quote(self.project, safe="")
        token = self.token()
        repo = self.api("GET", root, token)
        if (
            not isinstance(repo, dict)
            or type(repo.get("id")) is not int
            or str(repo.get("full_name" if github else "path_with_namespace")).lower()
            != self.project.lower()
        ):
            raise PublishError("The repository API did not confirm the approved repository.")
        base = request.get("base", repo.get("default_branch"))
        self.ref(base)
        if base == self.branch:
            raise PublishError("Choose a review base different from the current branch.")
        route = root + ("/pulls" if github else "/merge_requests")
        query = (
            {
                "per_page": "100",
                "state": "open",
                "head": self.project.split("/")[0] + ":" + self.branch,
                "base": base,
            }
            if github
            else {
                "per_page": "100",
                "state": "opened",
                "scope": "all",
                "source_branch": self.branch,
                "target_branch": base,
            }
        )

        def number(item):
            if not isinstance(item, dict):
                return None
            if github:
                head, target = item.get("head"), item.get("base")
                if not isinstance(head, dict) or not isinstance(target, dict):
                    return None
                for side, branch in [(head, self.branch), (target, base)]:
                    if (
                        side.get("ref") != branch
                        or not isinstance(side.get("repo"), dict)
                        or side["repo"].get("id") != repo["id"]
                    ):
                        return None
                n = item.get("number")
            else:
                if (
                    item.get("source_project_id") != repo["id"]
                    or item.get("target_project_id") != repo["id"]
                    or item.get("source_branch") != self.branch
                    or item.get("target_branch") != base
                ):
                    return None
                n = item.get("iid")
            return n if type(n) is int and n > 0 else None

        def existing():
            rows = self.api("GET", route + "?" + urlencode(query), token)
            if not isinstance(rows, list):
                raise PublishError("The repository API did not confirm existing reviews.")
            return next((n for row in rows if (n := number(row))), None)

        review = existing()
        if not review:
            title = request.get("title", self.branch)
            body = request.get("body", "")
            payload = (
                {"head": self.branch, "base": base, "title": title, "body": body}
                if github
                else {
                    "source_branch": self.branch,
                    "target_branch": base,
                    "title": title,
                    "description": body,
                }
            )
            created = self.api("POST", route, token, payload)
            # A concurrent request may already have opened the same review. Never repeat POST.
            review = existing() if created is None else number(created)
            if not review:
                raise PublishError(
                    "The repository API did not confirm the review. Check the host before retrying."
                )
        suffix = "/pull/" if github else "/-/merge_requests/"
        return f"Review: {self.remote}{suffix}{review}"


def publish_project(access, request, *, cancelled=None, export_bundle):
    """Expose only a pinned push or a same-repository review through the connecting Mac."""
    try:
        action = _request(request)
        grant = access.snapshot()
        if not grant:
            raise PublishError("Enable local project access before publishing.")
        remote = grant[1]
        _target(remote)

        def check():
            if (cancelled and cancelled()) or access.snapshot() != grant:
                raise PublishError("Local publishing authorization changed or this turn ended.")

        check()
        with export_bundle(access, cancelled=cancelled) as (branch, bundle):
            with tempfile.TemporaryDirectory(prefix="ccfleet-git-publish-") as scratch:
                publisher = _Publisher(remote, Path(scratch).resolve(), check)
                ref = publisher.import_branch(branch, bundle)
                text = publisher.push(ref) if action == "push" else publisher.review(ref, request)
        return {"exit": 0, "output": text}
    except PublishError as exc:
        return {"exit": 1, "output": str(exc)}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {"exit": 1, "output": "The local publisher could not complete this operation."}
