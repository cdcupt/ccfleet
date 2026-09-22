#!/usr/bin/env bash
# Release a slot: stop everything it is running and remove the user and their
# home, so the slot can be given to somebody else.
#
#   sudo node/slot-remove.sh --slot slot01
#   sudo node/slot-remove.sh --slot slot01 --keep-home   # inspect before wiping
#
# The wipe is the point. Handing somebody a slot that still holds another
# person's files is the one failure here with no recovery, so this errs
# towards removing too much rather than too little: the home directory goes,
# and the Claude credential goes with it because that is the only copy.
#
# The Claude ACCOUNT is untouched. Only the login on this machine is destroyed;
# whoever held the slot can sign in again anywhere, including on a new slot.

set -euo pipefail

SLOT=""
KEEP_HOME=no

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --slot)      SLOT="${2:-}"; shift 2 ;;
    --keep-home) KEEP_HOME=yes; shift ;;
    -h|--help)   sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "unknown option: $1" ;;
  esac
done

# Arguments before privilege, so an unprivileged run still reports a bad name.
[ -n "$SLOT" ] || die "--slot is required"
printf '%s' "$SLOT" | grep -qE '^[a-z][a-z0-9_-]{1,31}$' \
  || die "--slot is not a valid slot name"
[ "$(id -u)" -eq 0 ] || die "run this as root"
id "$SLOT" >/dev/null 2>&1 || { note "no such user: $SLOT; nothing to release"; exit 0; }

# What makes an account a slot is that slot-add created it and put it in this
# group. Nothing else does: "no sudo and a uid over 1000" describes a great many
# ordinary accounts, and this script deletes a home directory.
SLOT_GROUP="ccfleet-slots"
if ! id -nG "$SLOT" 2>/dev/null | tr ' ' '\n' | grep -qx "$SLOT_GROUP"; then
  die "$SLOT is not a ccfleet slot (not in $SLOT_GROUP). Refusing to remove it."
fi
# Belt and braces below the marker. A slot has no sudo and is not a system
# account; if either is untrue, something has been edited by hand and this is
# not the moment to find out by deleting a home directory.
# The same list slot-add strips, for the same reason: these are the groups that
# reach root or reach another slot's work. A provisioned slot holds none of
# them, so finding one means this account was edited by hand and is not the
# account this script thinks it is. Keep the two lists in step — a test checks.
for PRIVILEGED in sudo admin wheel root docker lxd libvirt kvm adm disk shadow staff; do
  if id -nG "$SLOT" | tr ' ' '\n' | grep -qx "$PRIVILEGED"; then
    die "$SLOT is in the $PRIVILEGED group, so it is not a slot. Refusing to remove it."
  fi
done
# Debian and Ubuntu hand ordinary logins out of FIRST_UID..LAST_UID, 1000..59999
# — the same range node/install.sh checks. A bare "uid >= 1000" lets `nobody`
# through at 65534, and every line below this one deletes something.
UID_NUM="$(id -u "$SLOT")"
{ [ "$UID_NUM" -ge 1000 ] && [ "$UID_NUM" -le 59999 ]; } \
  || die "$SLOT has uid $UID_NUM, outside the ordinary login range 1000-59999. That is a system account, not a slot. Refusing."
HOME_DIR="$(getent passwd "$SLOT" | cut -d: -f6)"

# Everything below deletes whatever this names — `userdel -r` every bit as much
# as the rm that backs it up — and it comes from passwd, which is edited by
# hand. A slot whose home had been pointed at `/`, at `/home`, or at a
# directory another account also lives in would turn releasing one slot into
# wiping the machine. So the decision is made here, before anything is stopped
# or removed: either this is plainly this slot's own directory, or nothing
# downstream is allowed to delete it.
[ -n "$HOME_DIR" ] || die "$SLOT has no home directory in passwd. Refusing to guess at one."
case "$HOME_DIR/" in
  *//*|*/../*) die "$SLOT's home is '$HOME_DIR', which is not a plain path. Refusing." ;;
esac
case "${HOME_DIR%/}" in
  /*/*) : ;;   # two components at least, so / and /home cannot be spelled here
  *) die "$SLOT's home is '$HOME_DIR' — the root or a top-level directory, not a slot home. Refusing." ;;
esac
if [ -L "$HOME_DIR" ]; then
  die "$SLOT's home '$HOME_DIR' is a symlink. Refusing to delete through it."
fi
HOME_PRESENT=no
if [ -d "$HOME_DIR" ]; then
  # Owned by this slot and nobody else. This is the check that catches a home
  # shared with a second account, which the shape test above cannot see.
  [ -n "$(find "$HOME_DIR" -maxdepth 0 -uid "$UID_NUM" 2>/dev/null)" ] \
    || die "$SLOT's home '$HOME_DIR' is not owned by uid $UID_NUM. It is shared or misconfigured; refusing to delete it."
  HOME_PRESENT=yes
fi

step "1/4  stop what it is running"
loginctl disable-linger "$SLOT" 2>/dev/null || true
# Ask the user manager to go first, so services get their own stop rather than
# being killed mid-write.
systemctl stop "user@$UID_NUM.service" 2>/dev/null || true
sleep 2

step "2/4  make sure nothing is left"
# By uid, never by a name pattern. `pkill -f` matching on text has killed the
# wrong process in this project before — including the shell running the script
# that called it, because the command line contained the pattern it searched for.
if pgrep -u "$SLOT" >/dev/null 2>&1; then
  pkill -u "$SLOT" 2>/dev/null || true
  sleep 2
  pkill -KILL -u "$SLOT" 2>/dev/null || true
  sleep 1
fi
if pgrep -u "$SLOT" >/dev/null 2>&1; then
  die "processes still running as $SLOT; not removing the account while it is in use"
fi
note "nothing running as $SLOT"

step "3/4  the account and its home"
SLICE_ROOT="${CCFLEET_SLICE_ROOT:-/etc/systemd/system}"
rm -f "$SLICE_ROOT/user-$UID_NUM.slice.d/50-ccfleet.conf"
rmdir "$SLICE_ROOT/user-$UID_NUM.slice.d" 2>/dev/null || true
systemctl daemon-reload
if [ "$KEEP_HOME" = yes ]; then
  userdel "$SLOT"
  note "user removed; $HOME_DIR kept because --keep-home was given"
  note "THE CREDENTIAL IS STILL IN THAT DIRECTORY. Do not reuse this slot yet."
elif [ "$HOME_PRESENT" = no ]; then
  userdel "$SLOT"
  note "user removed; $HOME_DIR was already gone"
else
  userdel -r "$SLOT" 2>/dev/null || { userdel "$SLOT"; rm -rf --one-file-system "$HOME_DIR"; }
  note "user and $HOME_DIR removed"
fi

step "4/4  confirm"
# Say what is true rather than what was attempted. A slot that is only mostly
# gone must not be handed to the next person.
FAILED=""
id "$SLOT" >/dev/null 2>&1 && FAILED="the user still exists"
if [ "$KEEP_HOME" = no ] && [ -e "$HOME_DIR" ]; then
  FAILED="${FAILED:+$FAILED; }$HOME_DIR still exists"
fi
[ -z "$FAILED" ] || die "release incomplete: $FAILED. Do NOT reuse this slot."

if [ "$KEEP_HOME" = yes ]; then
  printf '\nslot %s released, home kept. It is NOT safe to reuse until that home is gone.\n' "$SLOT"
else
  printf '\nslot %s released and wiped. Safe to give to somebody else.\n' "$SLOT"
  printf 'Their Claude account is untouched; only the login on this machine is gone.\n'
fi
