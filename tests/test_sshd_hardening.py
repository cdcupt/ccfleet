"""SSH hardening that cannot lock anybody out, and cannot claim what is not in force.

bootstrap.sh and install.sh turn password login off. Done blindly, that locks a
machine whose image turned key login off too (seen on a real provider image:
`PubkeyAuthentication no` at the end of sshd_config); on an image that never
reads sshd_config.d it reports hardening sshd never applied; and on a stock
Ubuntu cloud image, where cloud-init's 50-cloud-init.conf says
`PasswordAuthentication yes`, a drop-in that sorts after it loses. harden_sshd()
writes 01-ccfleet.conf so it is read first, takes away the 60-ccfleet.conf
earlier versions wrote, asks sshd itself before reloading anything, and puts
SSH's files back byte for byte whenever it refuses.

These run the real function, cut out of each script, in a sandbox: a fake `sshd`
that reads config the way sshd does (Include expanded where it stands, the first
value for each keyword wins, a malformed line refused) and a fake `systemctl`
that records what it was asked.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap

import pytest

NODE = pathlib.Path(__file__).resolve().parents[1] / "node"
SCRIPTS = {"bootstrap": NODE / "bootstrap.sh", "install": NODE / "install.sh"}
DEFAULT_LINES = ["PubkeyAuthentication yes", "PasswordAuthentication no",
                 "KbdInteractiveAuthentication no", "PermitRootLogin prohibit-password",
                 "X11Forwarding no"]
# What install.sh wrote to 60-ccfleet.conf before this version.
OLD_INSTALL_DROPIN = ("PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                      "PermitRootLogin prohibit-password\nX11Forwarding no\nMaxAuthTries 3\n")
# Hand-edited leftovers, each ending in a blank line, to prove a byte-exact restore.
EARLIER = {"01": "PubkeyAuthentication yes\nPasswordAuthentication no\n# an earlier run\n\n",
           "60": "PasswordAuthentication no\nX11Forwarding no\n# an older ccfleet, edited\n\n"}
UBUNTU_CLOUD_MAIN = ("Include {dropins}/*.conf\nKbdInteractiveAuthentication no\nUsePAM yes\n"
                     "X11Forwarding yes\nPrintMotd no\nAcceptEnv LANG LC_*\n"
                     "Subsystem sftp /usr/lib/openssh/sftp-server\n")

FAKE_SSHD = textwrap.dedent('''\
    #!/usr/bin/env python3
    """sshd, as far as -t and -T go.

    Include expands where it stands; the first value for each keyword wins; an
    unknown keyword, a missing argument or a bad value is refused by -T as well
    as -t, the way sshd refuses them; keywords never set print sshd's defaults.
    """
    import glob, os, sys
    YES_NO = {"pubkeyauthentication", "passwordauthentication", "kbdinteractiveauthentication",
              "challengeresponseauthentication", "x11forwarding", "usepam", "printmotd"}
    ANY_VALUE = {"acceptenv", "subsystem"}
    ROOT = {"yes", "no", "prohibit-password", "without-password", "forced-commands-only"}
    METHODS = {"any", "publickey", "password", "keyboard-interactive", "hostbased",
               "gssapi-with-mic"}
    DEFAULTS = {"pubkeyauthentication": "yes", "passwordauthentication": "yes",
                "kbdinteractiveauthentication": "yes", "permitrootlogin": "prohibit-password",
                "authenticationmethods": "any"}

    def refuse(path, number, why):
        print(f"{path} line {number}: {why}", file=sys.stderr)
        sys.exit(255)

    def settings(path):
        for number, raw in enumerate(open(path), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(" ")
            key, value = key.lower(), value.strip().lower()
            if not value:
                refuse(path, number, f"{key}: missing argument")
            if key == "include":
                for included in sorted(glob.glob(value)):
                    yield from settings(included)
                continue
            if key in YES_NO:
                ok = value in ("yes", "no")
            elif key == "permitrootlogin":
                ok = value in ROOT
            elif key in ("maxauthtries", "port"):
                ok = value.isdigit()
            elif key == "authenticationmethods":
                ok = all(m in METHODS for group in value.split() for m in group.split(","))
            elif key in ANY_VALUE:
                ok = True
            else:
                refuse(path, number, f"Bad configuration option: {key}")
            if not ok:
                refuse(path, number, f"bad value for {key}: {value}")
            yield key, value

    args = sys.argv[1:]
    config = args[args.index("-f") + 1] if "-f" in args else "/etc/ssh/sshd_config"
    if "-T" in args and os.environ.get("FAKE_SSHD_T_FAILS"):
        sys.exit(255)
    effective = dict(DEFAULTS)
    seen = set()
    for key, value in settings(config):
        if key not in seen:
            seen.add(key)
            effective[key] = value
    if "-T" in args:
        for key, value in effective.items():
            print(key, value)
    ''')

FAKE_SYSTEMCTL = textwrap.dedent('''\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
    # A box whose unit is sshd.service, not ssh.service, when asked to be one.
    if [ -n "${FAKE_NO_SSH_UNIT:-}" ] && [ "$*" = "reload ssh" ]; then exit 5; fi
    exit 0
    ''')


def function_text(script: pathlib.Path) -> str:
    """harden_sshd() as it stands in the script, with the comment block above it,
    from the first line of that block to the function's closing brace."""
    lines = script.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("harden_sshd() {"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    return "\n".join(lines[start:end + 1]) + "\n"


class Box:
    """A machine's /etc/ssh, a fake sshd, and a record of every systemctl call."""

    def __init__(self, root: pathlib.Path):
        self.bin = root / "bin"
        self.bin.mkdir()
        self.dropins = root / "sshd_config.d"
        self.dropins.mkdir()
        self.config = root / "sshd_config"
        self.log = root / "systemctl.log"
        for name, body in (("sshd", FAKE_SSHD), ("systemctl", FAKE_SYSTEMCTL)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)

    @property
    def include(self) -> str:
        return f"Include {self.dropins}/*.conf"

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "CCFLEET_SSHD_DROPIN_DIR": str(self.dropins),
                "CCFLEET_SSHD_CONFIG": str(self.config),
                "SYSTEMCTL_LOG": str(self.log), **extra}

    def harden(self, script: pathlib.Path, *lines: str, **env: str):
        program = "set -euo pipefail\n" + function_text(script) + 'harden_sshd "$@"\n'
        return subprocess.run(["bash", "-c", program, "harden_sshd", *lines],
                              capture_output=True, text=True, env=self.env(**env), timeout=30)

    def effective(self) -> dict[str, str]:
        out = subprocess.run([str(self.bin / "sshd"), "-T", "-f", str(self.config)],
                             capture_output=True, text=True, check=True, env=self.env()).stdout
        return dict(line.split(" ", 1) for line in out.splitlines())

    def reloads(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    @property
    def dropin(self) -> pathlib.Path:
        return self.dropins / "01-ccfleet.conf"

    @property
    def legacy(self) -> pathlib.Path:
        """What versions before this one wrote."""
        return self.dropins / "60-ccfleet.conf"


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


both = pytest.mark.parametrize("script", list(SCRIPTS.values()), ids=list(SCRIPTS))


def test_the_two_scripts_carry_the_same_function():
    """Each is fetched on its own through curl | bash, so the function is copied,
    not shared. A fix to one that misses the other is the failure this stops."""
    assert function_text(SCRIPTS["bootstrap"]) == function_text(SCRIPTS["install"])


def test_both_scripts_harden_through_it_and_stop_when_it_refuses():
    """The wiring, checked structurally: running either script whole needs root
    and a real machine. The behaviour is the function's, proven below; this pins
    that both hardening paths go through it, that a refusal stops the script
    before anything after it runs, that install.sh keeps its extra line, and that
    nothing else in either script touches sshd's drop-ins."""
    bootstrap, install = (s.read_text().splitlines() for s in SCRIPTS.values())
    assert ('  harden_sshd || { echo "bootstrap.sh stopped: SSH is as it was, and nothing '
            'after this ran" >&2; exit 1; }') in bootstrap
    call = install.index('  harden_sshd "MaxAuthTries 3" \\')
    assert install[call + 1].startswith('    || die "SSH was not hardened')
    for script in SCRIPTS.values():
        rest = script.read_text().replace(function_text(script), "")
        assert "sshd_config.d" not in rest and "ccfleet.conf" not in rest, \
            f"{script.name} touches sshd's drop-ins outside harden_sshd()"


# -- hardened --------------------------------------------------------------------

@both
def test_a_provider_image_that_turns_keys_off_is_hardened_not_locked(box, script):
    box.config.write_text(f"{box.include}\nPasswordAuthentication yes\nPubkeyAuthentication no\n")
    result = box.harden(script)
    assert result.returncode == 0, result.stderr
    assert box.effective()["pubkeyauthentication"] == "yes"
    assert box.effective()["passwordauthentication"] == "no"
    assert box.effective()["kbdinteractiveauthentication"] == "no"
    assert box.reloads() == ["reload ssh"]


@both
def test_an_ordinary_image_is_hardened(box, script):
    box.config.write_text(f"{box.include}\n")
    result = box.harden(script)
    assert result.returncode == 0, result.stderr
    assert (box.effective()["pubkeyauthentication"], box.effective()["passwordauthentication"]) \
        == ("yes", "no")
    assert box.reloads() == ["reload ssh"]


@both
def test_an_ubuntu_cloud_image_is_hardened_over_cloud_inits_password_line(box, script):
    """Most VPSes: cloud-init's 50-cloud-init.conf says `PasswordAuthentication
    yes`. 01-ccfleet.conf is read before it, so passwords go off, and cloud-init's
    own files are left exactly as they were."""
    box.config.write_text(UBUNTU_CLOUD_MAIN.format(dropins=box.dropins))
    cloud = box.dropins / "50-cloud-init.conf"
    cloud.write_text("PasswordAuthentication yes\n")
    cloudimg = box.dropins / "60-cloudimg-settings.conf"
    cloudimg.write_text("PasswordAuthentication no\n")
    result = box.harden(script, "MaxAuthTries 3")
    assert result.returncode == 0, result.stderr
    assert box.effective()["passwordauthentication"] == "no"
    assert box.effective()["pubkeyauthentication"] == "yes"
    assert box.reloads() == ["reload ssh"]
    assert cloud.read_text() == "PasswordAuthentication yes\n"
    assert cloudimg.read_text() == "PasswordAuthentication no\n"


@both
def test_an_earlier_60_ccfleet_conf_is_replaced_by_01_and_reloaded_once(box, script):
    """A machine hardened by an earlier version carries 60-ccfleet.conf. It goes,
    01-ccfleet.conf takes its place, and sshd is reloaded once."""
    box.config.write_text(f"{box.include}\n")
    box.legacy.write_text(OLD_INSTALL_DROPIN)
    result = box.harden(script, "MaxAuthTries 3")
    assert result.returncode == 0, result.stderr
    assert not box.legacy.exists()
    assert box.dropin.read_text().splitlines() == [*DEFAULT_LINES, "MaxAuthTries 3"]
    assert box.effective()["passwordauthentication"] == "no"
    assert box.effective()["maxauthtries"] == "3"
    assert box.reloads() == ["reload ssh"]


@both
@pytest.mark.parametrize("methods", ["publickey", "publickey,password publickey", "any"])
def test_authentication_methods_that_let_a_key_in_alone_are_accepted(box, script, methods):
    box.config.write_text(f"AuthenticationMethods {methods}\n{box.include}\n")
    result = box.harden(script)
    assert result.returncode == 0, result.stderr
    assert box.reloads() == ["reload ssh"]


@both
def test_a_box_whose_unit_is_sshd_is_reloaded_as_sshd(box, script):
    box.config.write_text(f"{box.include}\n")
    result = box.harden(script, FAKE_NO_SSH_UNIT="1")
    assert result.returncode == 0, result.stderr
    assert box.reloads() == ["reload ssh", "reload sshd"]


@both
def test_the_drop_in_says_both_halves_and_any_extra_line(box, script):
    box.config.write_text(f"{box.include}\n")
    assert box.harden(script).returncode == 0
    assert box.dropin.read_text().splitlines() == DEFAULT_LINES
    assert box.harden(script, "MaxAuthTries 3").returncode == 0
    assert box.dropin.read_text().splitlines() == [*DEFAULT_LINES, "MaxAuthTries 3"]


# -- refused, and left as it was --------------------------------------------------

def refused(box, result):
    assert result.returncode != 0
    assert "SSH was not hardened" in result.stderr
    assert "nothing was reloaded" in result.stderr
    assert box.reloads() == []


@both
def test_an_image_that_never_reads_the_drop_in_dir_is_left_as_it_was(box, script):
    """The drop-in would be ignored while the script called the box hardened."""
    box.config.write_text("PubkeyAuthentication yes\nPasswordAuthentication yes\n")
    result = box.harden(script)
    refused(box, result)
    assert "does not Include" in result.stderr
    assert not box.dropin.exists()


@both
def test_keys_turned_off_before_the_drop_in_is_read_is_refused(box, script):
    """The lockout itself: passwords would go off and keys stay off."""
    box.config.write_text(f"PubkeyAuthentication no\n{box.include}\n")
    result = box.harden(script)
    refused(box, result)
    assert "key login would be off" in result.stderr
    assert not box.dropin.exists()


@both
def test_passwords_kept_on_by_a_file_that_sorts_first_is_refused(box, script):
    """A provider file named to be read even before 01-ccfleet.conf still wins.
    Better a loud stop than a false claim; its file is left alone."""
    box.config.write_text(f"{box.include}\n")
    provider = box.dropins / "00-provider.conf"
    provider.write_text("PasswordAuthentication yes\n")
    result = box.harden(script)
    refused(box, result)
    assert "password login would stay on" in result.stderr
    assert "sorts before it" in result.stderr
    assert not box.dropin.exists()
    assert provider.read_text() == "PasswordAuthentication yes\n"


@both
@pytest.mark.parametrize("keyword", ["KbdInteractiveAuthentication",
                                     "ChallengeResponseAuthentication"])
def test_keyboard_interactive_kept_on_by_a_line_read_first_is_refused(box, script, keyword):
    """Keyboard-interactive is a password prompt by another name: not key-only."""
    box.config.write_text(f"{keyword} yes\n{box.include}\n")
    refused(box, box.harden(script))
    assert not box.dropin.exists()


@both
def test_authentication_methods_that_need_a_password_are_refused(box, script):
    """`publickey,password` demands the password this is turning off: a lockout."""
    box.config.write_text(f"AuthenticationMethods publickey,password\n{box.include}\n")
    refused(box, box.harden(script))
    assert not box.dropin.exists()


@both
def test_a_line_sshd_rejects_is_taken_back_and_nothing_is_reloaded(box, script):
    """Left in place, a drop-in sshd cannot parse stops sshd at the next restart."""
    box.config.write_text(f"{box.include}\n")
    result = box.harden(script, "MaxAuthTries")
    refused(box, result)
    assert "rejected" in result.stderr
    assert not box.dropin.exists()


@both
def test_an_sshd_that_cannot_report_its_config_is_not_trusted(box, script):
    """No answer is not a yes. Under `set -e` a failed -T must still clean up."""
    box.config.write_text(f"{box.include}\n")
    result = box.harden(script, FAKE_SSHD_T_FAILS="1")
    refused(box, result)
    assert "could not report" in result.stderr
    assert not box.dropin.exists()


@both
@pytest.mark.parametrize("before", ["01", "60", "both"])
@pytest.mark.parametrize("how", ["rejected", "overruled"])
def test_a_refusal_puts_every_earlier_drop_in_back_byte_for_byte(box, script, before, how):
    """A run that refuses must not take away hardening already in place, under
    either name: on disk it would come off at the next restart of sshd. The old
    60-ccfleet.conf is left exactly as it was, and a new 01 is not left behind."""
    box.config.write_text(("KbdInteractiveAuthentication yes\n" if how == "overruled" else "")
                          + f"{box.include}\n")
    present = {"01": box.dropin, "60": box.legacy}
    wanted = ["01", "60"] if before == "both" else [before]
    for name in wanted:
        present[name].write_text(EARLIER[name])
    result = box.harden(script, *(["MaxAuthTries"] if how == "rejected" else []))
    refused(box, result)
    for name, path in present.items():
        if name in wanted:
            assert path.read_text() == EARLIER[name], f"{path.name} not restored byte for byte"
        else:
            assert not path.exists(), f"{path.name} left behind"


@both
def test_the_old_drop_in_is_not_taken_away_from_under_a_setting_it_holds(box, script):
    """The proof is of the state the reload will run: 60-ccfleet.conf is gone
    before sshd is asked. Here it was all that kept a later AuthenticationMethods
    line from demanding the password being turned off; proven with it still in
    place, its removal would lock the box."""
    box.config.write_text(f"{box.include}\nAuthenticationMethods publickey,password\n")
    held = OLD_INSTALL_DROPIN + "AuthenticationMethods publickey\n"
    box.legacy.write_text(held)
    result = box.harden(script)
    refused(box, result)
    assert "AuthenticationMethods would not let a key in" in result.stderr
    assert box.legacy.read_text() == held
    assert not box.dropin.exists()
