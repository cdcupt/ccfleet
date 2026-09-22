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
#   An authorized_keys entry. Slots are reached through the console and Remote
#   Control, not SSH. Adding a key would hand somebody a shell on a machine
#   other people's work is sitting on.

set -euo pipefail

SLOT=""
# Claude Code is a Node process; measured on a live node it sits around 240 MB
# and a working session runs to roughly 400. A cap well above that stops one
# runaway build taking the machine down without getting in anyone's way.
MEMORY_MAX="2G"

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
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
[ "$(id -u)" -eq 0 ] || die "run this as root"

HOME_DIR="/home/$SLOT"
# Membership of this group is what makes an account a slot. It is created here
# and required by slot-remove before it will delete anything — because "no sudo
# and a uid over 1000" describes a great many ordinary accounts, and a typo
# should not be able to delete a colleague's home directory.
SLOT_GROUP="ccfleet-slots"
as_slot() { sudo -u "$SLOT" HOME="$HOME_DIR" bash -c "$1"; }

step "1/4  the account"
getent group "$SLOT_GROUP" >/dev/null 2>&1 || addgroup --system "$SLOT_GROUP" >/dev/null
if id "$SLOT" >/dev/null 2>&1; then
  # An account with this name already exists. If this tool did not create it,
  # it belongs to somebody else and is not ours to reshape: the steps below
  # would tighten their home to 0700 and strip their sudo. Refuse instead.
  if ! id -nG "$SLOT" 2>/dev/null | tr ' ' '\n' | grep -qx "$SLOT_GROUP"; then
    die "$SLOT already exists and is not a ccfleet slot. Refusing to take it over."
  fi
  note "slot $SLOT already exists; continuing"
else
  adduser --disabled-password --gecos "" "$SLOT" >/dev/null
  note "created $SLOT"
fi
adduser "$SLOT" "$SLOT_GROUP" >/dev/null 2>&1 || usermod -aG "$SLOT_GROUP" "$SLOT"
note "marked as a slot (member of $SLOT_GROUP)"
# 0700 rather than the distro default. On a shared machine the default 0755
# means every slot can read every other slot's home, including the directory
# Claude Code writes a credential into.
chmod 700 "$HOME_DIR"
# No sudo, and no group that grants it. Said out loud because its absence is
# the security property, and an absence is easy to add back by accident.
if id -nG "$SLOT" | tr ' ' '\n' | grep -qx sudo; then
  deluser "$SLOT" sudo >/dev/null 2>&1 || true
  note "removed $SLOT from sudo: a slot is a user on somebody else's machine"
fi
rm -f "/etc/sudoers.d/90-ccfleet-$SLOT"
# Lingering, so this user's services run when nobody is logged in — which is
# the normal state for a slot.
loginctl enable-linger "$SLOT"
note "lingering on, so services survive logout"

step "2/4  a share of the machine"
# A soft ceiling below the hard one, so a slot that is growing gets throttled
# and reclaimed before it is killed outright. Eighty percent, computed in
# whatever unit the cap was given in rather than by converting between them.
MEM_NUM="${MEMORY_MAX%[KMG]}"
MEM_UNIT="${MEMORY_MAX#"$MEM_NUM"}"
MEMORY_HIGH="$(( MEM_NUM * 8 / 10 ))${MEM_UNIT}"
# One slice per slot. Without it a single runaway build is the whole machine's
# problem; with it, it is that slot's problem.
install -d -m 755 "/etc/systemd/system/user-$(id -u "$SLOT").slice.d"
cat > "/etc/systemd/system/user-$(id -u "$SLOT").slice.d/50-ccfleet.conf" <<CONF
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

step "3/4  Claude Code"
as_slot 'mkdir -p ~/.local/bin ~/.config/ccfleet ~/.config/systemd/user ~/workspace'
as_slot '[ -x ~/.local/bin/claude ] || curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1'
as_slot 'grep -q DISABLE_AUTOUPDATER ~/.profile 2>/dev/null || printf "\n# ccfleet: upgrades are staged by the operator\nexport DISABLE_AUTOUPDATER=1\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n" >> ~/.profile'
CC_VERSION="$(as_slot '"$HOME"/.local/bin/claude --version 2>/dev/null | head -1' || echo unknown)"
[ "$CC_VERSION" = unknown ] && die "Claude Code did not install for $SLOT"
note "installed: $CC_VERSION"

step "4/4  the two setup prompts"
# The same two questions install.sh pre-answers, for the same reason: neither is
# about anybody's account, and leaving them would mean every slot needs an
# interactive terminal before it can be used.
as_slot "python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.claude.json')
d = json.load(open(p)) if os.path.exists(p) else {}
d['hasCompletedOnboarding'] = True
d.setdefault('projects', {})
d['projects'].setdefault(os.path.expanduser('~'), {})['hasTrustDialogAccepted'] = True
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
os.chmod(p, 0o600)
PY"

printf '\nslot %s is ready. It has no sudo and no SSH key, by design.\n' "$SLOT"
printf 'Whoever holds it signs into their own Claude account from the console;\n'
printf 'nobody else can do that step for them.\n'
