"""Only owner-helper install fragments run, with temporary homes and fake downloads."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def fragment(name: str) -> str:
    text = (REPO / "node" / name).read_text()
    if name == "install.sh":
        start = text.index('step "6/8  agent and services"')
        end = text.index("for u in ccfleet-agent.service", start)
        return text[start:end]
    functions = text[text.index("fetch_text() {"):text.index("# 1. Claude Code")]
    start = text.index("# 3. ")
    end = text.index("# 4. Agent env file", start)
    return functions + text[start:end]


def owner_install(tmp_path: Path, name: str, *, checkout: bool = False,
                  fail_helper: bool = False):
    owner = tmp_path / "owner-home"
    destination = owner / ".local/bin"
    destination.mkdir(parents=True)
    preserved = owner / ".claude/projects/dummy/history.jsonl"
    preserved.parent.mkdir(parents=True)
    preserved.write_bytes(b"synthetic owner history remains\n")
    previous_agent = b"previous agent stays on helper download failure\n"
    (destination / "ccfleet-agent").write_bytes(previous_agent)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, shutil, sys
        from pathlib import Path
        args = sys.argv[1:]
        url = next(arg for arg in args if arg.startswith('https://fixtures.invalid/'))
        relative = url.removeprefix('https://fixtures.invalid/')
        if os.environ.get('TEST_FAIL_HELPER') == '1' and relative.endswith('/compatibility.py'):
            raise SystemExit(1)
        source = Path(os.environ['TEST_SOURCE_REPO']) / relative
        destination = Path(args[args.index('-o') + 1])
        shutil.copyfile(source, destination)
        """))
    curl.chmod(0o755)
    prelude = textwrap.dedent("""\
        set -euo pipefail
        BIN="$HOME/.local/bin"
        REPO_RAW=https://fixtures.invalid
        SRC_DIR="$TEST_LOCAL_SOURCE/node"
        step() { :; }
        as_owner() { bash -c "$1"; }
        """)
    env = {**os.environ, "HOME": str(owner), "PATH": f"{bindir}:{os.environ['PATH']}",
           "TEST_SOURCE_REPO": str(REPO),
           "TEST_LOCAL_SOURCE": str(REPO if checkout else tmp_path / "no-checkout"),
           "TEST_FAIL_HELPER": "1" if fail_helper else "0"}
    result = subprocess.run(["bash", "-c", prelude + fragment(name)], env=env,
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    return result, destination, preserved, previous_agent


@pytest.mark.parametrize("name,checkout", [("install.sh", False),
                                          ("setup-owner.sh", False),
                                          ("setup-owner.sh", True)])
def test_owner_installers_ship_exact_sibling_guard_before_agent(tmp_path, name, checkout):
    result, destination, history, _ = owner_install(tmp_path, name, checkout=checkout)
    assert result.returncode == 0, result.stderr
    guard = destination / "compatibility.py"
    agent = destination / "ccfleet-agent"
    assert guard.read_bytes() == (REPO / "ccfleet_agent/compatibility.py").read_bytes()
    assert agent.read_bytes() == (REPO / "ccfleet_agent/agent.py").read_bytes()
    assert stat.S_IMODE(guard.stat().st_mode) == 0o644
    assert stat.S_IMODE(agent.stat().st_mode) == 0o755
    assert guard.stat().st_uid == agent.stat().st_uid == os.getuid()
    assert history.read_bytes() == b"synthetic owner history remains\n"
    # The isolated standard-library agent can actually resolve its sibling,
    # without an installed ccfleet package or a repository on PYTHONPATH.
    check = subprocess.run([sys.executable, "-I", "-c",
        "import runpy,sys; from pathlib import Path; "
        "scope=runpy.run_path(sys.argv[1], run_name='owner_helper_check'); "
        "helper=scope['_compatibility_helper'](); "
        "assert helper is not None; "
        "assert Path(helper.__file__).resolve()==Path(sys.argv[2]).resolve()",
        str(agent), str(guard)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("name", ["install.sh", "setup-owner.sh"])
def test_missing_owner_guard_download_stops_before_replacing_existing_agent(tmp_path, name):
    result, destination, history, previous = owner_install(tmp_path, name, fail_helper=True)
    assert result.returncode != 0
    assert (destination / "ccfleet-agent").read_bytes() == previous
    assert history.read_bytes() == b"synthetic owner history remains\n"


@pytest.mark.parametrize("name", ["install.sh", "setup-owner.sh"])
def test_owner_installer_shell_syntax(name):
    subprocess.run(["bash", "-n", str(REPO / "node" / name)], check=True, timeout=10)
