#!/usr/bin/env bash
# Add one slot to a shared machine: a Linux user who can run Claude Code, and
# nothing else.
#
#   sudo node/slot-add.sh --slot slot01
#   sudo node/slot-add.sh --slot slot01 --memory-max 1500M
#
# This is the per-person half of node/install.sh. The other half — packages,
# hardening, the firewall, the fleet agent — runs once per machine and is not
# repeated here.
#
# What a slot user deliberately does NOT get:
#
#   sudo. The single-owner node grants it because the machine is theirs. Here
#   the machine is shared, and sudo on a shared box is root for everybody on
#   it. The cost is real: a slot cannot install packages, and asking for one is
#   a support request rather than a command. That is the price of several
#   people to a box.
#
#   An operator or unrestricted authorized_keys entry. The machine agent may
#   install CC Fleet device keys later; every such key is forced into the
#   persistent Claude Code entrypoint and cannot select a command or forward
#   ports, agents or X11.

set -euo pipefail

SLOT=""
# Claude Code is a Node process; measured on a live node it sits around 240 MB
# and a working session runs to roughly 400. A cap well above that stops one
# runaway build taking the machine down without getting in anyone's way.
MEMORY_MAX="2G"

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
_cap_bytes() {
  local n="${1%[KMG]}" u="${1#"${1%[KMG]}"}"
  case "$u" in
    K) echo $((n * 1024)) ;;
    M) echo $((n * 1024 * 1024)) ;;
    G) echo $((n * 1024 * 1024 * 1024)) ;;
    *) echo "$n" ;;
  esac
}

step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --slot)       SLOT="${2:-}"; shift 2 ;;
    --memory-max) MEMORY_MAX="${2:-}"; shift 2 ;;
    -h|--help)    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "unknown option: $1" ;;
  esac
done

# Arguments before privilege: a person running this without sudo should be told
# their slot name is wrong, rather than only that they are not root and left to
# discover the second problem after fixing the first.
[ -n "$SLOT" ] || die "--slot is required"
# This name is interpolated into commands run as root and becomes a unix
# account, so anything outside this set is a command-injection vector rather
# than a cosmetic problem. Matches what adduser itself accepts.
printf '%s' "$SLOT" | grep -qE '^[a-z][a-z0-9_-]{1,31}$' \
  || die "--slot must start with a lowercase letter, then lowercase letters, digits, underscore or hyphen (2-32)"
printf '%s' "$MEMORY_MAX" | grep -qE '^[0-9]+[KMG]?$' \
  || die "--memory-max looks like 1500M or 2G"
# A cap below what Claude Code needs is not a small slot, it is a slot that
# cannot start. Measured on a live node: about 265 MB for a running session, so
# anything under 512 MiB is a typo rather than a choice. Zero in particular
# would write MemoryMax=0 and leave the slot unable to run anything at all.
[ "$(_cap_bytes "$MEMORY_MAX")" -ge $((512 * 1024 * 1024)) ] \
  || die "--memory-max must be at least 512M; a slot needs about 265M to run at all"
[ "$(id -u)" -eq 0 ] || die "run this as root"

# Set properly once the account exists, from what the system says rather than
# from a guess: adduser does not have to put a home under /home, and writing to
# the wrong path is how a provisioning script edits somebody else's files.
HOME_DIR=""
# Membership of this group is what makes an account a slot. It is created here
# and required by slot-remove before it will delete anything — because "no sudo
# and a uid over 1000" describes a great many ordinary accounts, and a typo
# should not be able to delete a colleague's home directory.
SLOT_GROUP="ccfleet-slots"
# Prefer the units sitting next to this script; fall back to fetching them.
LOCAL_UNITS="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/systemd"
as_slot() { sudo -u "$SLOT" HOME="$HOME_DIR" bash -c "$1"; }
user_systemctl() {
  local uid; uid="$(id -u "$SLOT")"
  sudo -u "$SLOT" XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
}

step "1/5  the account"
getent group "$SLOT_GROUP" >/dev/null 2>&1 || addgroup --system "$SLOT_GROUP" >/dev/null
if id "$SLOT" >/dev/null 2>&1; then
  # An account with this name already exists. If this tool did not create it,
  # it belongs to somebody else and is not ours to reshape: the steps below
  # would tighten their home to 0700 and strip their sudo. Refuse instead.
  if ! id -nG "$SLOT" 2>/dev/null | tr ' ' '\n' | grep -qx "$SLOT_GROUP"; then
    die "$SLOT already exists and is not a ccfleet slot. Refusing to take it over."
  fi
  # Group membership alone is not enough to say it is a slot: a system account
  # put in the group by hand would get its home chmod 700 a few lines down.
  # Ordinary logins live in 1000..59999 on Debian and Ubuntu; `nobody` sits
  # outside it at 65534 and a bare "uid >= 1000" would wave it through.
  EXISTING_UID="$(id -u "$SLOT")"
  { [ "$EXISTING_UID" -ge 1000 ] && [ "$EXISTING_UID" -le 59999 ]; } \
    || die "$SLOT has uid $EXISTING_UID, outside the ordinary login range 1000-59999. That is a system account, not a slot. Refusing."
  note "slot $SLOT already exists; continuing"
else
  adduser --disabled-password --gecos "" "$SLOT" >/dev/null
  note "created $SLOT"
fi
adduser "$SLOT" "$SLOT_GROUP" >/dev/null 2>&1 || usermod -aG "$SLOT_GROUP" "$SLOT"
note "marked as a slot (member of $SLOT_GROUP)"

# `adduser --disabled-password` leaves a shadow marker (`!` or `*`) that some
# OpenSSH/PAM combinations reject before public-key authentication is tried.
# Give the account an unguessable, discarded password so it is not shadow-
# locked; sshd's slot Match block independently disables every password path.
PASSWORD_STATE="$(passwd -S "$SLOT" 2>/dev/null | awk '{print $2}')"
if [ "$PASSWORD_STATE" != P ]; then
  RANDOM_PASSWORD="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  printf '%s:%s\n' "$SLOT" "$RANDOM_PASSWORD" | chpasswd
  unset RANDOM_PASSWORD
fi
[ "$(passwd -S "$SLOT" 2>/dev/null | awk '{print $2}')" = P ] \
  || die "could not make $SLOT eligible for public-key SSH"
note "password login remains disabled; public-key authentication is eligible"

HOME_DIR="$(getent passwd "$SLOT" | cut -d: -f6)"
[ -n "$HOME_DIR" ] || die "could not find a home directory for $SLOT"
# 0700 rather than the distro default. On a shared machine the default 0755
# means every slot can read every other slot's home, including the directory
# Claude Code writes a credential into.
chmod 700 "$HOME_DIR"
# No sudo, and no group that grants it by another route. Said out loud because
# its absence is the security property, and an absence is easy to add back by
# accident.
#
# `sudo` is the obvious one and not the only one. A member of `docker` can
# start a container that mounts the host root and read or write anything on it,
# which is root by a longer path; `lxd` and `libvirt` are the same argument,
# `adm` reads the logs of every other slot, and `disk` reads the block devices
# under their homes. None of these are things one slot may hold over another.
for PRIVILEGED in sudo admin wheel root docker lxd libvirt kvm adm disk shadow staff; do
  if id -nG "$SLOT" | tr ' ' '\n' | grep -qx "$PRIVILEGED"; then
    deluser "$SLOT" "$PRIVILEGED" >/dev/null 2>&1 || gpasswd -d "$SLOT" "$PRIVILEGED" >/dev/null 2>&1 || true
    if id -nG "$SLOT" | tr ' ' '\n' | grep -qx "$PRIVILEGED"; then
      die "could not remove $SLOT from the $PRIVILEGED group; refusing to hand out a slot that keeps it"
    fi
    note "removed $SLOT from $PRIVILEGED: a slot is a user on somebody else's machine"
  fi
done
# Overridable for the same reason SLICE_ROOT is: under test this must not be
# the real directory. It was, and it went unnoticed because `rm -f` on a
# missing file exits 0 — the suite reached outside its sandbox on every run
# and said nothing.
SUDOERS_DIR="${CCFLEET_SUDOERS_DIR:-/etc/sudoers.d}"
rm -f "$SUDOERS_DIR/90-ccfleet-$SLOT"
# Lingering, so this user's services run when nobody is logged in — which is
# the normal state for a slot.
loginctl enable-linger "$SLOT"
note "lingering on, so services survive logout"

step "2/5  a share of the machine"
# A soft ceiling below the hard one, so a slot that is growing gets throttled
# and reclaimed before it is killed outright.
#
# Computed in bytes and written in MiB. Taking 80% of the number while keeping
# its unit is wrong whenever the number is small: 2G became 1G, which is half
# rather than four fifths, and 1G became 0G, which would have put the slot
# under reclaim pressure from its first byte.
MEMORY_HIGH="$(( $(_cap_bytes "$MEMORY_MAX") * 8 / 10 / 1024 / 1024 ))M"
# One slice per slot. Without it a single runaway build is the whole machine's
# problem; with it, it is that slot's problem.
# Where systemd reads drop-ins from. Overridable so this step can be exercised
# somewhere writable; nothing but a test has a reason to move it.
SLICE_ROOT="${CCFLEET_SLICE_ROOT:-/etc/systemd/system}"
SLICE_DIR="$SLICE_ROOT/user-$(id -u "$SLOT").slice.d"
mkdir -p "$SLICE_DIR"
cat > "$SLICE_DIR/50-ccfleet.conf" <<CONF
# Written by ccfleet slot-add. One slot's ceiling, so a runaway build is that
# slot's problem rather than the machine's.
[Slice]
MemoryMax=$MEMORY_MAX
MemoryHigh=$MEMORY_HIGH
CONF
systemctl daemon-reload
note "memory capped at $MEMORY_MAX for this slot alone"
# Worth knowing, because it looks broken otherwise: this cap binds processes
# under the slot's own systemd manager, which is where its services and work
# session live. A process an operator starts with `sudo -u` from a root shell
# stays in the ROOT session's cgroup and is not capped. Measured: a service
# started through the user manager landed in user-<uid>.slice with memory.max
# at the cap; the same binary started by sudo landed in user-0.slice.

step "3/5  Claude Code"
as_slot 'mkdir -p ~/.local/bin ~/.config/ccfleet ~/.config/systemd/user ~/workspace'
as_slot '[ -x ~/.local/bin/claude ] || curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1'
as_slot 'grep -q DISABLE_AUTOUPDATER ~/.profile 2>/dev/null || printf "\n# ccfleet: upgrades are staged by the operator\nexport DISABLE_AUTOUPDATER=1\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n" >> ~/.profile'
# Ask whether the binary is there, rather than inferring it from a version
# string. `claude --version | head -1` exits with head's status, so a missing
# claude gave an empty version and a pipeline that succeeded — and the check
# that was supposed to catch it compared against the literal "unknown", which
# it could never be. A slot with no Claude Code was reported as ready.
as_slot '[ -x "$HOME/.local/bin/claude" ]' \
  || die "Claude Code did not install for $SLOT"
CC_VERSION="$(as_slot '"$HOME"/.local/bin/claude --version 2>/dev/null | head -1')"
[ -n "$CC_VERSION" ] || die "Claude Code is installed for $SLOT but will not report a version"
note "installed: $CC_VERSION"

step "4/5  the two setup prompts, and Opus at max effort"
# The same two questions install.sh pre-answers, for the same reason: neither is
# about anybody's account, and leaving them would mean every slot needs an
# interactive terminal before it can be used.
as_slot "python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.claude.json')
d = json.load(open(p)) if os.path.exists(p) else {}
d['hasCompletedOnboarding'] = True
d.setdefault('projects', {})
# ~/workspace, not ~. That is the directory slot-add creates and the one
# people work in, and the trust prompt is per-directory: trusting the home
# leaves the prompt waiting in the place it actually matters.
d['projects'].setdefault(os.path.expanduser('~/workspace'), {})['hasTrustDialogAccepted'] = True
d['remoteDialogSeen'] = True
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
os.chmod(p, 0o600)
PY"
# Opus at max effort, what every slot starts on. Written only where the holder
# has not chosen for themselves, so running this again never takes a choice
# back. The model is a default that /model changes. The effort cannot be one:
# the settings file's own effort field stops at xhigh and drops max without a
# word, so max goes in the env block, which Claude Code applies to every
# session and which /effort then cannot lower. A holder who wants less edits
# that line, as the guide tells them.
#
# And nothing optional leaves for Anthropic that a slot can do without: Claude
# Code's error reports, bug reports and feedback surveys are off, by its own
# documented switches. The same rule as above: a holder who turned one back on
# keeps it. Core telemetry and feature-flag behavior stays at Claude Code's
# published defaults; this script does not modify authentication behavior.
as_slot "python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.claude/settings.json')
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault('model', 'opus')
d['skipDangerousModePermissionPrompt'] = True
env = d.setdefault('env', {})
env.setdefault('CLAUDE_CODE_EFFORT_LEVEL', 'max')
for key in ('DISABLE_ERROR_REPORTING', 'DISABLE_BUG_COMMAND',
            'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY'):
    env.setdefault(key, '1')
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
PY"
note "Claude Code starts on Opus at max effort with permission prompts and optional reporting off"

step "5/5  the persistent CLI session"
# The unit starts only after a Claude credential exists. slot-entry.sh starts
# it on the first paired connection; enabling it brings the same session back
# after a reboot. No Remote Control or alternate customer mode is installed.
REPO_RAW="${CCFLEET_REPO_RAW:-https://raw.githubusercontent.com/cdcupt/ccfleet/main}"
unit=ccfleet-shell.service
if [ -f "$LOCAL_UNITS/$unit" ]; then
  install -m 644 -o "$SLOT" -g "$SLOT" "$LOCAL_UNITS/$unit" \
    "$HOME_DIR/.config/systemd/user/$unit"
else
  as_slot "curl -fsSL '$REPO_RAW/node/systemd/$unit' -o ~/.config/systemd/user/$unit" \
    || die "could not fetch $unit; the slot would have no way in"
fi
user_systemctl daemon-reload
# A start before the first sign-in is deliberately skipped by the unit's
# ConditionPathExists. The first `ccfleet` connection starts it again.
user_systemctl enable ccfleet-shell.service >/dev/null 2>&1 \
  || die "could not enable the slot's persistent session"
user_systemctl start ccfleet-shell.service >/dev/null 2>&1 || true
note "persistent Claude Code session installed; it starts after sign-in"

printf '\nslot %s is ready. It has no sudo and no unrestricted SSH key.\n' "$SLOT"
printf 'Whoever holds it signs into their own Claude account from the console;\n'
printf 'then each of their computers pairs its own forced CC Fleet device key.\n'
