"""Gemini maintenance stays inside the authorized Fleet boundary, including cold starts."""

import hashlib
import io
import json
import runpy
import sys
from pathlib import Path

import pytest

from ccfleet_agent import client_experience as ux
from ccfleet_agent import gemini_distribution as dist

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = b"public-release-fixture"
SUM = hashlib.sha512(ARCHIVE).hexdigest()
MANIFEST = {
    "version": "1.5.14",
    "url": "https://storage.googleapis.com/antigravity-public/"
    "antigravity-cli/1.5.14/darwin-arm/cli.tar.gz",
    "sha512": SUM,
}


def invoke(home, fields, authorize=lambda: True):
    data = io.BytesIO(json.dumps({"v": 1, **fields}).encode() + b"\n")
    out = io.BytesIO()
    return dist.serve(data, out, home, authorize), out.getvalue()


def test_slot_is_fixed_release_source_and_caches_verified_archives(tmp_path, monkeypatch):
    (tmp_path / ".config/ccfleet/private-owner").mkdir(parents=True)
    urls = []

    def fetch(url, dest, limit, timeout):
        urls.append(url)
        dest.write_bytes(ARCHIVE if url == MANIFEST["url"] else json.dumps(MANIFEST).encode())

    monkeypatch.setattr(dist, "_fetch", fetch)
    status, data = invoke(tmp_path, {"op": "helper"})
    assert status == 0 and data == (ROOT / "ccfleet_agent/gemini_preview.py").read_bytes()
    assert not urls
    status, data = invoke(tmp_path, {"op": "manifest", "arch": "arm64"})
    assert json.loads(data) == {"arch": "arm64", "version": "1.5.14", "sha512": SUM}
    assert urls == [dist.BASE + "darwin_arm64.json"]
    q = {"op": "archive", "arch": "arm64", "sha512": SUM}
    assert invoke(tmp_path, q) == (0, ARCHIVE)
    assert invoke(tmp_path, q) == (0, ARCHIVE)
    assert urls.count(MANIFEST["url"]) == 1
    assert invoke(tmp_path, {**q, "sha512": "a" * 128}) == (2, b"")


@pytest.mark.parametrize(
    "q",
    [
        {"op": "manifest", "arch": "arm64", "url": "https://other.invalid"},
        {"op": "helper", "token": "synthetic"},
        {"op": "editor-client", "path": "/tmp/other"},
        {"op": "workspace-tools", "arch": "arm64"},
        {"op": "../../unknown"},
        {"op": []},
        {"op": "manifest", "arch": "linux"},
        {"op": "archive", "arch": "amd64", "sha512": "bad"},
    ],
)
def test_rejects_unrecognized_input_before_any_fetch(q, tmp_path, monkeypatch):
    monkeypatch.setattr(dist, "_fetch", lambda *_: pytest.fail("unexpected network"))
    assert invoke(tmp_path, q) == (2, b"")
    assert invoke(tmp_path, {"op": "helper"}, lambda: False) == (2, b"")


def test_slot_refuses_changed_origin_bad_payload_and_revoked_grant(tmp_path, monkeypatch):
    (tmp_path / ".config/ccfleet/private-owner").mkdir(parents=True)
    manifest = {**MANIFEST, "url": "https://unrelated.invalid/archive.tar.gz"}

    def fetch(url, dest, *rest):
        dest.write_bytes(b"wrong" if url == MANIFEST["url"] else json.dumps(manifest).encode())

    monkeypatch.setattr(dist, "_fetch", fetch)
    with pytest.raises(ValueError, match="unrecognized"):
        invoke(tmp_path, {"op": "manifest", "arch": "arm64"})
    manifest.update(MANIFEST)
    with pytest.raises(ValueError, match="verification"):
        invoke(tmp_path, {"op": "archive", "arch": "arm64", "sha512": SUM})
    lease = iter((True, False))
    assert invoke(tmp_path, {"op": "manifest", "arch": "amd64"}, lambda: next(lease)) == (2, b"")


def test_bounded_slot_transport(tmp_path):
    dest = tmp_path / "release"
    code = "import json,sys; q=json.load(sys.stdin); print(q['op'])"
    ux.slot_artifact([sys.executable, "-I", "-c", code], {"op": "manifest"}, dest, 32, 5)
    assert dest.read_text().strip() == "manifest"
    with pytest.raises(ValueError, match="oversized"):
        ux.slot_artifact([sys.executable, "-I", "-c", code], {"op": "manifest"}, dest, 2, 5)


def test_source_and_pairing_fail_closed(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["preview_helper"].__globals__
    from ccfleet_agent import client_release

    def absent(*_):
        raise cli["CliError"]("missing")

    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path.resolve()))
    monkeypatch.setitem(scope, "packaged_helper", absent)
    monkeypatch.setitem(scope, "client_release", lambda: client_release)
    monkeypatch.setitem(scope, "client_experience", lambda: ux)
    monkeypatch.setattr(client_release, "download", lambda *_: pytest.fail("external download"))

    def wrong(command, request, dest, *rest):
        dest.write_bytes(b"wrong")

    monkeypatch.setattr(ux, "slot_artifact", wrong)
    with pytest.raises(cli["CliError"], match="checksum"):
        cli["preview_helper"](["paired-slot"])
    calls = []

    def good(command, request, dest, *rest):
        calls.append(request)
        dest.write_bytes((ROOT / "ccfleet_agent/gemini_preview.py").read_bytes())

    monkeypatch.setattr(ux, "slot_artifact", good)
    for _ in range(2):
        assert callable(cli["preview_helper"](["paired-slot"]).run)
    assert calls == [{"op": "helper"}]
    with pytest.raises(cli["CliError"], match="ccfleet.io"):
        cli["api_json"]("https://other.invalid/api/cli/register", "x", {}, fleet_only=True)
    monkeypatch.setitem(scope, "open_websocket", lambda *_: pytest.fail("outside Fleet"))
    with pytest.raises(cli["CliError"], match="ccfleet.io"):
        cli["proxy"](
            {"server": "https://ccfleet.io", "endpoint": "wss://other.invalid"}, fleet_only=True
        )


@pytest.mark.parametrize("installed", [True, False])
def test_client_always_checks_slot_and_never_runs_vendor_updater(tmp_path, monkeypatch, installed):
    home = tmp_path / "home"
    home.mkdir()
    state = home / "config"
    target = home / ".local/bin/agy"
    target.parent.mkdir(parents=True)
    calls = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ux.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(ux, "_vendor_download", lambda *_: pytest.fail("direct download"))
    monkeypatch.setattr(ux, "_installed_agent_version", lambda *_: pytest.fail("unsandboxed probe"))
    monkeypatch.setattr(ux, "_google_version", lambda *_: (1, 5, 14))

    def transfer(command, q, dest, *rest):
        assert command == ["paired-slot"]
        calls.append(q["op"])
        dest.write_bytes(
            json.dumps({"version": "1.5.14", "arch": "arm64", "sha512": SUM}).encode()
            if q["op"] == "manifest"
            else ARCHIVE
        )

    def install(archive, digest, path, version, stage, replaced):
        assert digest == SUM and archive.read_bytes() == ARCHIVE
        path.write_bytes(ARCHIVE)

    monkeypatch.setattr(ux, "slot_artifact", transfer)
    monkeypatch.setattr(ux, "_install_google", install)
    if installed:
        target.write_bytes(ARCHIVE)
    for _ in range(2):
        path = ux.prepare_agent(
            "agy",
            state,
            lambda: str(target) if target.exists() else None,
            slot_command=["paired-slot"],
        )
        assert Path(path) == target
    assert calls == (["manifest", "manifest"] if installed else ["manifest", "archive", "manifest"])
    with pytest.raises(ux.ExperienceError, match="paired"):
        ux.prepare_agent("agy", state, lambda: None)


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple sandbox")
def test_offline_version_probe_blocks_network_and_uses_temporary_home(tmp_path, monkeypatch):
    # A local fixture executable observes the same sandbox and environment as the real version probe.
    stage = tmp_path / "stage"
    stage.mkdir()
    spy = tmp_path / "spy"
    spy.write_text(
        "#!/usr/bin/python3\nimport os,socket\ns=socket.socket()\n"
        "try: s.bind(('127.0.0.1',0))\nexcept PermissionError: pass\n"
        "else: raise SystemExit(9)\nassert os.environ['HOME']==os.getcwd()\nprint('1.2.3')\n"
    )
    spy.chmod(0o700)
    monkeypatch.setattr(ux, "_verify_google_binary", lambda _: None)
    assert ux._google_version(spy, stage) == (1, 2, 3)

@pytest.mark.parametrize("op", ["workspace-tools", "editor-client"])
def test_approved_slot_distributes_all_signed_editor_generations(tmp_path, monkeypatch, op):
    generations = (b"first public component", b"second public component", b"current component")
    pins = [hashlib.sha256(component).hexdigest() for component in generations]
    name = dist.EDITOR_FILES[op]
    history = [(v, revision, {op: pin}) for (v, revision, _), pin in zip(dist.EDITOR_HISTORY, pins)]
    monkeypatch.setattr(dist, "EDITOR_HISTORY", history)
    monkeypatch.setattr(dist, "__file__", str(tmp_path / "gemini_distribution.py"))
    monkeypatch.setattr(dist, "_fetch", lambda *_: pytest.fail("unexpected upstream request"))
    versions = [v for v, _, _ in history] + [dist.EDITOR_VERSION]
    for component, version in zip(generations, versions):
        (tmp_path / dist.editor_snapshot(name, version)).write_bytes(component)
    assert invoke(tmp_path, {"op": op}) == (0, generations[0])
    for pin, expected in zip(pins, generations):
        fields = {"op": op, "sha256": pin}
        assert invoke(tmp_path, fields) == (0, expected)
        assert invoke(tmp_path, fields, lambda: False) == (2, b"")
        authorized = iter([True, False])
        assert invoke(tmp_path, fields, authorized.__next__) == (2, b"")
    for digest in ("0" * 64, "../path", None):
        assert invoke(tmp_path, {"op": op, "sha256": digest}) == (2, b"")
    assert invoke(tmp_path, {"op": op, "sha256": pins[-1], "path": "another.py"}) == (2, b"")
    snapshot = tmp_path / dist.editor_snapshot(name)
    snapshot.unlink()
    snapshot.symlink_to(tmp_path / name)
    assert invoke(tmp_path, {"op": op, "sha256": pins[-1]}) == (2, b"")
    (tmp_path / name).write_bytes(generations[-1])
    assert invoke(tmp_path, {"op": op}) == (2, b"")
