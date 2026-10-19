"""Side-by-side installs use fake HTTP responses and disposable signing keys."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccfleet_agent import client_release as release
from tests import test_client_release as fixtures

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "laptop/install.sh"
BOOTSTRAP_URL = release.RAW_BASE + "main/laptop/ccfleet"
NEXT_URL = release.RAW_BASE + "e72304ee613b5765c12aab6f49a96234cc4e1407/laptop/releases/0.2.6.json"


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    return fixtures.signer.__wrapped__(tmp_path_factory)


def signed_values(signer, version="0.2.6", *, bad_signature=False):
    manifest, contents = fixtures.bundle(version, revision="6" * 40)
    # Execution is only a synthetic argument echo, never the real Fleet client.
    client = contents["laptop/ccfleet"] + b"\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
    contents["laptop/ccfleet"] = client
    for entry in manifest["files"]:
        if entry["path"] == "laptop/ccfleet":
            entry["size"], entry["sha256"] = len(client), hashlib.sha256(client).hexdigest()
    raw = release.canonical(manifest)
    values = {NEXT_URL: raw, NEXT_URL + ".sig": signer.sign(raw, other=bad_signature)}
    values.update({entry["url"]: contents[entry["path"]] for entry in manifest["files"]})
    bootstrap_helpers = {name: (ROOT / "ccfleet_agent" / (name + ".py")).read_bytes()
                         for name in release.HELPERS.values()}
    bootstrap = "#!/usr/bin/env python3\n" + "\n".join(
        f"{name} = {hashlib.sha256(bootstrap_helpers[helper]).hexdigest()!r}"
        for name, helper in release.HELPERS.items())
    bootstrap += f"\nRELEASE_PUBLIC_KEY = {signer.public!r}\nraise SystemExit('bootstrap never executes')\n"
    values[BOOTSTRAP_URL] = bootstrap.encode()
    values.update({release.RAW_BASE + "main/ccfleet_agent/" + name + ".py": raw
                   for name, raw in bootstrap_helpers.items()})
    return values


def rig(tmp_path, signer, *, special=False):
    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    destination = home / ("bin ' quote $(touch INJECTION) $literal" if special else ".local/bin")
    destination.mkdir(parents=True, mode=0o700)
    commands = home / "fake-commands"
    commands.mkdir(mode=0o700)
    downloads = home / "downloads.json"
    log = home / "downloads.log"
    extra = home / "python-extra.py"
    extra.write_text("")
    curl = commands / "curl"
    curl.write_text(f"#!{sys.executable} -I\n" + r'''
import base64,json,os,sys
values=json.load(open(os.environ['FAKE_DOWNLOADS']))
url=next(arg for arg in sys.argv[1:] if arg.startswith(('https://','file://')))
with open(os.environ['FAKE_LOG'],'a') as log:log.write('curl '+url+'\n')
if url not in values:raise SystemExit(22)
with open(sys.argv[sys.argv.index('-o')+1],'wb') as stream:
 stream.write(base64.b64decode(values[url]))
''')
    curl.chmod(0o700)
    interpreter = commands / "python3"
    interpreter.write_text(f"#!{sys.executable} -I\n" + r'''
import base64,io,json,os,runpy,sys,urllib.request
class Response(io.BytesIO):
 def __init__(self,url,raw):super().__init__(raw);self.url=url;self.headers={}
 def getcode(self):return 200
 def geturl(self):return self.url
class Opener:
 def open(self,request,timeout):
  values=json.load(open(os.environ['FAKE_DOWNLOADS']))
  url=request.full_url
  with open(os.environ['FAKE_LOG'],'a') as log:log.write('release '+url+'\n')
  if url not in values:raise AssertionError('unexpected network request')
  return Response(url,base64.b64decode(values[url]))
urllib.request.build_opener=lambda *args:Opener()
exec(compile(open(os.environ['FAKE_PYTHON_EXTRA']).read(),'<test fault>','exec'),globals())
arguments=sys.argv[1:]
if arguments[:1]==['-I']:arguments.pop(0)
if arguments[:1]==['-c']:
 sys.argv=['-c',*arguments[2:]]
 exec(compile(arguments[1],'<test interpreter>','exec'),globals())
elif arguments[:1]==['-']:
 sys.argv=arguments
 exec(compile(sys.stdin.read(),'<test installer>','exec'),globals())
else:
 sys.argv=arguments
 runpy.run_path(arguments[0],run_name='__main__')
''')
    interpreter.chmod(0o700)
    environment = {**os.environ, "HOME": str(home), "CCFLEET_INSTALL_DIR": str(destination),
        "CCFLEET_HOME": str(home / ".config/ccfleet"), "FAKE_DOWNLOADS": str(downloads),
        "FAKE_LOG": str(log), "FAKE_PYTHON_EXTRA": str(extra),
        "PATH": str(commands) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.defpath}
    for name in list(environment):
        if name.startswith("CCFLEET_") and name not in {"CCFLEET_HOME", "CCFLEET_INSTALL_DIR"}:
            environment.pop(name)

    def values(data):
        downloads.write_text(json.dumps({url: base64.b64encode(raw).decode() for url, raw in data.items()}))

    def install(*args):
        return subprocess.run(["bash", str(INSTALL), *args], env=environment,
                              capture_output=True, text=True, timeout=30)

    values(signed_values(signer))
    return home, destination, environment, log, extra, values, install


def snapshot(folder):
    return {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mode & 0o777,
                                           path.stat().st_ino)
            for path in folder.rglob("*") if path.is_file() and not path.is_symlink()}


@pytest.mark.parametrize("special", [False, True])
def test_next_preserves_existing_managed_release_and_local_config_and_quotes_wrapper(
        tmp_path, signer, special):
    home, destination, env, log, _, _, install = rig(tmp_path, signer, special=special)
    old = fixtures.distribution(signer, "0.2.5", revision="5" * 40)
    fixtures.install(old, signer, destination)
    previous = snapshot(destination)
    configuration = home / ".config/ccfleet"
    configuration.mkdir(parents=True, mode=0o700)
    for name in ("config.json", "token", "keys/existing", "local-history/existing.json"):
        path = configuration / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC_KEEP_UNCHANGED")
    for name in (".bashrc", ".zshrc", ".profile"):
        (home / name).write_text("SYNTHETIC_SHELL_KEEP")
    config_before = snapshot(configuration)
    result = install("--next")
    assert result.returncode == 0, result.stderr
    for name, expected in previous.items():
        path = destination / name
        assert (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_ino) == expected
    assert snapshot(configuration) == config_before
    assert all((home / name).read_text() == "SYNTHETIC_SHELL_KEEP"
               for name in (".bashrc", ".zshrc", ".profile"))
    assert not (destination / "claude").exists() and not (home / "INJECTION").exists()
    assert (destination / ".ccfleet-next").stat().st_mode & 0o777 == 0o700
    wrapper = destination / "ccfleet-next"
    assert wrapper.stat().st_mode & 0o777 == 0o700
    checked = subprocess.run([str(wrapper), "two words", "$(touch INJECTION)", "'quote"],
        env=env, capture_output=True, text=True, timeout=10, cwd=home)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.splitlines()[-1] == json.dumps(["two words", "$(touch INJECTION)", "'quote"])
    assert not (home / "INJECTION").exists()
    assert all("stable.json" not in line and "claude.ai/install.sh" not in line
               for line in log.read_text().splitlines())
    before_repeat = snapshot(destination / ".ccfleet-next")
    wrapper_inode = wrapper.stat().st_ino
    assert install("--next").returncode == 0
    assert snapshot(destination / ".ccfleet-next") == before_repeat
    assert wrapper.stat().st_ino == wrapper_inode


def test_failed_signature_keeps_both_current_launchers_and_release_states(tmp_path, signer):
    home, destination, _, _, _, values, install = rig(tmp_path, signer)
    old = fixtures.distribution(signer, "0.2.5", revision="5" * 40)
    fixtures.install(old, signer, destination)
    assert install("--next").returncode == 0
    before = snapshot(destination)
    values(signed_values(signer, bad_signature=True))
    result = install("--next")
    assert result.returncode != 0 and "signature" in result.stderr
    assert snapshot(destination) == before
    assert not (home / ".config").exists()


@pytest.mark.parametrize("kind", ["different-wrapper", "wrapper-symlink", "wrapper-hardlink",
                                  "directory-symlink", "directory-public", "unmanaged-client"])
def test_unexpected_or_unsafe_next_targets_are_never_overwritten(tmp_path, signer, kind):
    home, destination, _, _, _, _, install = rig(tmp_path, signer)
    legacy = destination / "ccfleet"
    legacy.write_text("LEGACY_KEEP")
    wrapper, isolated = destination / "ccfleet-next", destination / ".ccfleet-next"
    outside = home / "outside"
    outside.mkdir(mode=0o700)
    keep = outside / "keep"
    keep.write_text("UNRELATED_KEEP")
    if kind == "different-wrapper":
        wrapper.write_text("UNEXPECTED_KEEP")
        wrapper.chmod(0o700)
    elif kind == "wrapper-symlink":
        wrapper.symlink_to(keep)
    elif kind == "wrapper-hardlink":
        os.link(keep, wrapper)
    elif kind == "directory-symlink":
        isolated.symlink_to(outside, target_is_directory=True)
    else:
        isolated.mkdir(mode=0o700)
        if kind == "directory-public":
            isolated.chmod(0o755)
        else:
            (isolated / "ccfleet").write_text("UNMANAGED_KEEP")
    result = install("--next")
    assert result.returncode != 0
    assert legacy.read_text() == "LEGACY_KEEP" and keep.read_text() == "UNRELATED_KEEP"
    assert not list(isolated.glob(".ccfleet-releases")) if not isolated.is_symlink() else True
    if kind == "different-wrapper":
        assert wrapper.read_text() == "UNEXPECTED_KEEP"


@pytest.mark.parametrize("args", [("--setup",), ("--migrate",), ("--yes",),
    ("--name", "computer"), ("--name", "changed"), ("--slot", "one")])
def test_next_rejects_all_setup_or_identity_options_before_any_download(tmp_path, signer, args):
    _, destination, _, log, _, _, install = rig(tmp_path, signer)
    result = install("--next", *args)
    assert result.returncode == 2
    assert not log.exists() and not (destination / "ccfleet-next").exists()


def test_signed_wrong_version_cannot_become_next(tmp_path, signer):
    _, destination, _, _, _, values, install = rig(tmp_path, signer)
    values(signed_values(signer, version="0.2.7"))
    result = install("--next")
    assert result.returncode != 0 and "0.2.6 manifest" in result.stderr
    assert not (destination / "ccfleet-next").exists()
    assert not (destination / ".ccfleet-next").exists()


def test_foreign_owned_existing_wrapper_is_rejected_before_release_install(tmp_path, signer):
    _, destination, _, _, extra, _, install = rig(tmp_path, signer)
    wrapper = destination / "ccfleet-next"
    wrapper.write_text("FOREIGN_KEEP")
    wrapper.chmod(0o700)
    info = wrapper.stat()
    extra.write_text(f"""
original_fstat=os.fstat
def foreign_fstat(fd):
 info=original_fstat(fd)
 if (info.st_dev,info.st_ino)=={(info.st_dev, info.st_ino)!r}:
  fields=list(info);fields[4]=os.getuid()+1;return os.stat_result(fields)
 return info
os.fstat=foreign_fstat
""")
    result = install("--next")
    assert result.returncode != 0 and wrapper.read_text() == "FOREIGN_KEEP"
    assert not (destination / ".ccfleet-next").exists()


def test_destination_symlink_is_rejected_before_any_download(tmp_path, signer):
    home, destination, env, log, _, _, install = rig(tmp_path, signer)
    alias = home / "bin-alias"
    alias.symlink_to(destination, target_is_directory=True)
    env["CCFLEET_INSTALL_DIR"] = str(alias)
    assert install("--next").returncode != 0
    assert not log.exists() and not list(destination.iterdir())


def test_next_rejects_nonofficial_bootstrap_without_download(tmp_path, signer):
    _, destination, env, log, _, _, install = rig(tmp_path, signer)
    env["CCFLEET_INSTALL_URL"] = "https://different.example/client"
    assert install("--next").returncode == 2
    assert not log.exists() and not (destination / "ccfleet-next").exists()


def test_plain_signed_install_still_follows_stable_channel_and_not_next(tmp_path, signer):
    _, destination, _, log, _, values, install = rig(tmp_path, signer)
    data = signed_values(signer)
    item = fixtures.distribution(signer, version="0.2.5", revision="5" * 40)
    channel = release.canonical({"schema": 1, "version": "0.2.5", "revision": "5" * 40,
        "manifest_url": item.url, "signature_url": item.url + ".sig",
        "expires_at": int(time.time()) + 3600})
    data.update(item.values)
    data[release.DEFAULT_CHANNEL_URL] = channel
    data[release.DEFAULT_CHANNEL_SIGNATURE_URL] = signer.sign(channel)
    values(data)
    result = install()
    assert result.returncode == 0, result.stderr
    assert release.status(destination, signer.public)["version"] == "0.2.5"
    assert not (destination / "ccfleet-next").exists()
    assert not (destination / ".ccfleet-next").exists()
    assert release.DEFAULT_CHANNEL_URL in log.read_text()
    assert NEXT_URL not in log.read_text()
