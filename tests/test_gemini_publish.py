"""Only a frozen local branch, its exact pinned repository and typed reviews may be published."""

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ccfleet_agent import gemini_publish as publisher


class Access:
    def __init__(self, remote):
        self.grant = ("generation", remote, "project-identity")

    def snapshot(self):
        return self.grant


def git(directory, *args):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(directory),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    return subprocess.check_output(
        ["/usr/bin/git", *args], cwd=directory, env=env, stderr=subprocess.DEVNULL, text=True
    ).strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q", "-b", "feature/fix")
    (root / "worked.txt").write_text("locally created")
    git(root, "add", "worked.txt")
    git(
        root,
        "-c",
        "user.name=QA",
        "-c",
        "user.email=qa@example.invalid",
        "commit",
        "-qm",
        "A reviewed change",
    )
    bare = tmp_path / "delivery.git"
    git(tmp_path, "init", "--bare", "-q", str(bare))
    bundle = tmp_path / "frozen.bundle"
    git(root, "bundle", "create", str(bundle), "refs/heads/feature/fix")

    @contextlib.contextmanager
    def export(access, *, cancelled):
        yield "feature/fix", bundle

    return root, bare, bundle, export


class FakeHost:
    """Network boundary double. Final Git ref updates use real Git on a disposable bare origin."""

    def __init__(self, monkeypatch, checkout, host):
        self.source, self.bare, self.bundle, self.export = checkout
        self.host = host
        self.project = "org/demo" if host == "github.com" else "group/nested/demo"
        self.url = f"https://{host}/{self.project}"
        self.access = Access(self.url)
        self.api_calls = []
        self.git_calls = []
        self.credential_calls = 0
        self.review = None
        self.repo_id = 123
        self.credential_reply = (
            f"protocol=https\nhost={host}\nusername=user\npassword=test-private\n".encode()
        )
        self.conflict = False
        self.status = None
        self.before_call = None
        self.run = publisher._run
        self.helper = "/usr/local/bin/" + ("gh" if host == "github.com" else "glab")
        monkeypatch.setattr(publisher, "_helper", lambda h: self.helper)
        monkeypatch.setattr(publisher, "_run", self.handle)

    def handle(self, args, cwd, env, check, *, data=b"", limit=32768, mutation=False):
        assert cwd.resolve() != self.source.resolve() and not cwd.is_relative_to(self.source)
        assert env["HOME"] == str(cwd) and env["PATH"] == "/usr/bin:/bin"
        assert "GH_TOKEN" not in env and "HTTPS_PROXY" not in env
        assert "GIT_CONFIG_PARAMETERS" not in env and "GIT_CONFIG_COUNT" not in env
        check()
        if self.before_call:
            self.before_call(args, check)
        if args[0] == publisher.GIT and env["GIT_ALLOW_PROTOCOL"] == "file":
            return self.run(args, cwd, env, check, data=data, limit=limit, mutation=mutation)
        if args[0] == publisher.GIT:
            assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert "protocol.allow=never" in args and "http.followRedirects=false" in args
            assert (
                "http.proxy=" in args
                and "http.sslVerify=true" in args
                and "credential.helper=" in args
            )
            assert (
                "credential.https://"
                + self.host
                + ".helper=!"
                + self.helper
                + " auth git-credential"
                in args
            )
            assert "--force" not in args and self.url in args
            op = "push" if "push" in args else "ls-remote"
            self.git_calls.append((op, mutation))
            translated = list(args)
            translated[translated.index(self.url)] = str(self.bare)
            return self.run(
                translated, cwd, {**env, "GIT_ALLOW_PROTOCOL": "file"}, check, limit=limit
            )
        if args[0] == self.helper:
            assert list(args[1:]) == ["auth", "git-credential", "get"]
            assert data == f"protocol=https\nhost={self.host}\npath={self.project}.git\n\n".encode()
            self.credential_calls += 1
            return 0, self.credential_reply
        assert args[0] == publisher.CURL and args[1] == "--disable"
        assert "--location" not in args and "--insecure" not in args
        assert (
            args[args.index("--noproxy") + 1] == "*" and args[args.index("--proto") + 1] == "=https"
        )
        assert args[args.index("--max-redirs") + 1] == "0" and not any(
            "test-private" in x for x in args
        )
        assert all("test-private" not in value for value in env.values())
        assert data == b"Authorization: Bearer test-private\n"
        method = args[args.index("--request") + 1]
        endpoint = urlsplit(args[args.index("--url") + 1])
        assert endpoint.scheme == "https"
        assert endpoint.netloc == ("api.github.com" if self.host == "github.com" else "gitlab.com")
        self.api_calls.append((method, endpoint.path, endpoint.query))
        assert mutation == (method == "POST")

        def result(obj, status=200):
            return 0, json.dumps(obj).encode() + b"\n" + str(status).encode()

        if self.status:
            return result({"error": "test-private"}, self.status)
        if self.host == "github.com":
            path = "/repos/" + self.project
            reviews = path + "/pulls"
            detail = {"full_name": self.project, "id": self.repo_id, "default_branch": "main"}
        else:
            path = "/api/v4/projects/group%2Fnested%2Fdemo"
            reviews = path + "/merge_requests"
            detail = {
                "path_with_namespace": self.project,
                "id": self.repo_id,
                "default_branch": "main",
            }
        if endpoint.path == path:
            assert method == "GET"
            return result(detail)
        assert endpoint.path == reviews
        if method == "GET":
            q = parse_qs(endpoint.query)
            assert q.get("head", q.get("source_branch")) == (
                ["org:feature/fix"] if self.host == "github.com" else ["feature/fix"]
            )
            assert q.get("base", q.get("target_branch")) == ["main"]
            return result([self.review] if self.review else [])
        assert not endpoint.query
        body = Path(args[args.index("--data-binary") + 1][1:])
        assert body.parent == cwd
        obj = json.loads(body.read_bytes())
        assert obj["title"] == "Feature title"
        if self.host == "github.com":
            assert obj == {
                "head": "feature/fix",
                "base": "main",
                "title": "Feature title",
                "body": "Details",
            }
            self.review = {
                "number": 9,
                "head": {"repo": {"id": self.repo_id}, "ref": "feature/fix"},
                "base": {"repo": {"id": self.repo_id}, "ref": "main"},
            }
        else:
            assert obj == {
                "source_branch": "feature/fix",
                "target_branch": "main",
                "title": "Feature title",
                "description": "Details",
            }
            self.review = {
                "iid": 9,
                "source_project_id": self.repo_id,
                "target_project_id": self.repo_id,
                "source_branch": "feature/fix",
                "target_branch": "main",
            }
        return result(self.review, 409 if self.conflict else 201)

    def publish(self, request, **kw):
        return publisher.publish_project(self.access, request, export_bundle=self.export, **kw)


@pytest.mark.parametrize("host", ["github.com", "gitlab.com"])
def test_publish_verified_commit_then_same_repo_review(monkeypatch, checkout, host):
    monkeypatch.setenv("HTTPS_PROXY", "https://ignored.invalid")
    monkeypatch.setenv("GH_TOKEN", "unused-test-value")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "project-controlled-value")
    mock = FakeHost(monkeypatch, checkout, host)
    root, origin, _, _ = checkout
    req = {"action": "create_review", "title": "Feature title", "body": "Details"}
    assert "Push the current branch" in mock.publish(req)["output"]
    assert not mock.api_calls and not mock.credential_calls
    assert mock.publish({"action": "push"})["exit"] == 0
    assert git(origin, "rev-parse", "refs/heads/feature/fix") == git(root, "rev-parse", "HEAD")
    for _ in range(2):
        out = mock.publish(req)
        tail = "/pull/9" if host == "github.com" else "/-/merge_requests/9"
        assert out == {"exit": 0, "output": "Review: " + mock.url + tail}
    assert sum(m == "POST" for m, _, _ in mock.api_calls) == 1
    assert mock.git_calls == [
        ("ls-remote", False),
        ("push", True),
        ("ls-remote", False),
        ("ls-remote", False),
    ]


def test_existing_review_race_is_reconciled_without_repeating_post(monkeypatch, checkout):
    mock = FakeHost(monkeypatch, checkout, "github.com")
    mock.conflict = True
    assert mock.publish({"action": "push"})["exit"] == 0
    r = mock.publish({"action": "create_review", "title": "Feature title", "body": "Details"})
    assert r["exit"] == 0 and sum(method == "POST" for method, _, _ in mock.api_calls) == 1


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/../another",
        "https://github.com/owner/project/extra",
        "https://github.com@other.invalid/o/r",
        "http://gitlab.com/g/r",
        "ssh://github.com/o/r",
        "https://gitlab.com/group/project?x=1",
        "https://github.com/org/repo.git",
        None,
    ],
)
def test_malformed_remote_never_enters_export(remote):
    access = Access(remote)
    out = publisher.publish_project(
        access, {"action": "push"}, export_bundle=lambda *_: pytest.fail("Unexpected export")
    )
    assert out["exit"] == 1


@pytest.mark.parametrize("changes", [{"remote": "other"}, {"force": True}, {"title": "unexpected"}])
def test_push_does_not_accept_remotes_or_options(changes):
    out = publisher.publish_project(
        Access("https://github.com/o/r"),
        {"action": "push", **changes},
        export_bundle=lambda *_: pytest.fail("Unexpected export"),
    )
    assert out["exit"] == 1


def test_revocation_before_remote_mutation_stops_delivery(monkeypatch, checkout):
    fake = FakeHost(monkeypatch, checkout, "github.com")

    def revoke(args, check):
        if "push" in args:
            fake.access.grant = ("regranted", fake.url, "project-identity")
            check()

    fake.before_call = revoke
    result = fake.publish({"action": "push"})
    assert result["exit"] == 1 and "authorization" in result["output"]
    assert git(checkout[1], "for-each-ref") == ""


def test_invalid_bundle_or_additional_ref_never_sends_network_command(monkeypatch, checkout):
    root, _, bundle, _ = checkout
    git(root, "branch", "unwanted")
    bundle.unlink()
    git(root, "bundle", "create", str(bundle), "--branches")
    fake = FakeHost(monkeypatch, checkout, "github.com")
    assert fake.publish({"action": "push"})["exit"] == 1
    assert not fake.git_calls
    bundle.write_bytes(b"broken")
    assert fake.publish({"action": "push"})["exit"] == 1 and not fake.git_calls


def test_unrelated_fork_review_is_not_an_existing_same_repo_review(monkeypatch, checkout):
    fake = FakeHost(monkeypatch, checkout, "github.com")
    fake.review = {
        "number": 44,
        "head": {"repo": {"id": 42}, "ref": "feature/fix"},
        "base": {"repo": {"id": 123}, "ref": "main"},
    }
    assert fake.publish({"action": "push"})["exit"] == 0
    assert fake.publish({"action": "create_review", "title": "Feature title", "body": "Details"})[
        "output"
    ].endswith("/pull/9")


def test_refusal_and_untrusted_helper_output_do_not_expose_credentials(monkeypatch, checkout):
    fake = FakeHost(monkeypatch, checkout, "gitlab.com")
    assert fake.publish({"action": "push"})["exit"] == 0
    fake.credential_reply = b"protocol=https\nhost=other.invalid\npassword=test-private\n"
    r = fake.publish({"action": "create_review"})
    assert r["exit"] == 1
    assert "test-private" not in r["output"] and not fake.api_calls
    fake.credential_reply = b"protocol=https\nhost=gitlab.com\npassword=test-private\n"
    fake.status = 301
    r = fake.publish({"action": "create_review"})
    assert r["exit"] == 1 and "301" in r["output"] and "test-private" not in r["output"]
    assert [i[0] for i in fake.api_calls] == ["GET"]


def test_runner_cancels_subprocess_tree_and_suppresses_sensitive_output(tmp_path):
    # Both the parent and inherited stdout-holding descendant belong to the operation.
    marker = tmp_path / "started"
    code = (
        "import os,sys,subprocess,time; p=subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(90)']); open(sys.argv[1],'w').write(str(p.pid));"
        "print('test-private',flush=True); time.sleep(90)"
    )

    def stop():
        if marker.exists():
            raise publisher.PublishError("cancelled")

    with pytest.raises(publisher.PublishError, match="may have completed") as caught:
        publisher._run([sys.executable, "-c", code, str(marker)], tmp_path, {}, stop, mutation=True)
    assert "test-private" not in str(caught.value)
    pid = int(marker.read_text())
    # macOS or Linux init may briefly retain a terminated child as a zombie.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z")


def test_push_never_replaces_a_diverged_remote_ref(monkeypatch, checkout):
    fake = FakeHost(monkeypatch, checkout, "github.com")
    assert fake.publish({"action": "push"})["exit"] == 0
    root, origin, _, _ = checkout
    # Simulate another developer pushing a new commit after the local frozen snapshot.
    git(
        root,
        "-c",
        "user.name=QA",
        "-c",
        "user.email=qa@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "newer commit",
    )
    git(root, "push", str(origin), "refs/heads/feature/fix")
    newer = git(origin, "rev-parse", "refs/heads/feature/fix")
    refused = fake.publish({"action": "push"})
    assert refused["exit"] == 1 and "did not confirm" in refused["output"]
    assert git(origin, "rev-parse", "refs/heads/feature/fix") == newer


def test_incomplete_pack_is_not_published(monkeypatch, checkout):
    fake = FakeHost(monkeypatch, checkout, "github.com")
    damaged = checkout[2].read_bytes()
    checkout[2].write_bytes(damaged[:-40])
    result = fake.publish({"action": "push"})
    assert result["exit"] == 1 and not fake.git_calls
