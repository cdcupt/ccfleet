#!/usr/bin/env bash
# Turn a server into a shared machine: the agent that looks after its slots,
# the two scripts it runs, and the timer that runs it.
#
#   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/machine-setup.sh \
#     | sudo bash -s -- --server https://fleet.example.com --node shared-1 --token <64-hex>
#
# Harden the box first with node/bootstrap.sh: keys-only SSH, the firewall and
# unattended upgrades, plus a login for the operator and no agent. Not
# node/install.sh, which turns the box into one owner's node; its agent reports
# under a node id, and this script refuses to run beside an agent that reports
# as this machine. This adds only what a shared machine needs on top — one
# agent for the whole machine, running as root, because it creates and removes
# the slot users. Slots themselves are not made here. The agent makes each one
# when somebody claims it, and wipes it when they give it back.
set -euo pipefail

SERVER="" NODE_ID="" TOKEN=""
REPO_RAW="${CCFLEET_REPO_RAW:-https://raw.githubusercontent.com/cdcupt/ccfleet/main}"
# Every system path this writes, each overridable so the tests can point it at
# a sandbox rather than at the machine running them.
LIB_DIR="${CCFLEET_LIB_DIR:-/usr/local/lib/ccfleet}"
ETC_DIR="${CCFLEET_ETC_DIR:-/etc/ccfleet}"
STATE_DIR="${CCFLEET_STATE_DIR:-/var/lib/ccfleet}"
UNIT_DIR="${CCFLEET_UNIT_DIR:-/etc/systemd/system}"
HOME_ROOT="${CCFLEET_HOME_ROOT:-/home}"
# A checkout to copy from instead of fetching: offline installs, and tests.
SOURCE_DIR="${CCFLEET_SOURCE_DIR:-}"

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

usage() {
  # Self-contained: the documented form pipes this into bash, where $0 is "bash".
  cat <<'USAGE'
ccfleet machine setup — makes a server a shared machine that carries slots.

  curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/machine-setup.sh \
    | sudo bash -s -- --server https://fleet.example.com --node shared-1 --token <64-hex>

Required:
  --server URL   fleet server base URL
  --node ID      this machine's node id, from the console
  --token HEX    its node token, shown once by the console
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --node)   NODE_ID="${2:-}"; shift 2 ;;
    --token)  TOKEN="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Arguments before privilege, so a typo is reported without sudo.
[ -n "$SERVER" ] && [ -n "$NODE_ID" ] && [ -n "$TOKEN" ] || usage
case "$SERVER" in http://*|https://*) ;; *) die "--server must start with http:// or https://" ;; esac
case "$SERVER" in *[[:space:]]*) die "--server must not contain spaces" ;; esac
printf '%s' "$NODE_ID" | grep -qE '^[a-z0-9][a-z0-9-]{1,39}$' || die "--node must be lowercase letters, digits and hyphens"
printf '%s' "$TOKEN"   | grep -qE '^[0-9a-f]{64}$'            || die "--token must be the 64-character value from the console"
[ "$(id -u)" -eq 0 ] || die "run this as root (prefix it with sudo)"

# Before anything is changed. An owner agent already reporting as this node
# would take turns with the machine agent on every heartbeat — one saying
# "an owner node", the other "a machine with slots" — and the console would
# flip between the two for as long as both ran.
for env_file in "$HOME_ROOT"/*/.config/ccfleet/agent.env; do
  [ -f "$env_file" ] || continue
  if grep -qx "CCFLEET_NODE_ID=$NODE_ID" "$env_file"; then
    die "an owner agent already reports as $NODE_ID ($env_file). Two agents under one node id fight over every heartbeat: disable that one's ccfleet-agent.timer first, or give this machine its own node id."
  fi
done

fetch() {  # fetch <path in the repo> <destination> <mode>
  local tmp="$2.tmp.$$"
  if [ -n "$SOURCE_DIR" ]; then
    cp "$SOURCE_DIR/$1" "$tmp" || die "could not copy $1 from $SOURCE_DIR"
  else
    curl -fsSL "$REPO_RAW/$1" -o "$tmp" || die "could not fetch $1"
  fi
  chmod "$3" "$tmp"
  mv -f "$tmp" "$2"
}

step "1/5  packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
# sudo and adduser: slot-add.sh creates each slot with adduser and drops into it
# with sudo -u. libpam-systemd: without it no slot gets its own systemd manager,
# and its work session and Remote Control have nowhere to run.
apt-get install -y -q python3 tmux curl sudo adduser ca-certificates libpam-systemd >/dev/null
note "installed: python3, tmux, curl, sudo, adduser, libpam-systemd"

step "2/5  the agent and the slot scripts"
mkdir -p "$LIB_DIR/ccfleet_agent" "$LIB_DIR/systemd"
fetch ccfleet_agent/__init__.py "$LIB_DIR/ccfleet_agent/__init__.py" 644
fetch ccfleet_agent/agent.py    "$LIB_DIR/ccfleet_agent/agent.py" 644
fetch ccfleet_agent/machine.py  "$LIB_DIR/ccfleet_agent/machine.py" 644
fetch node/slot-add.sh          "$LIB_DIR/slot-add.sh" 755
fetch node/slot-remove.sh       "$LIB_DIR/slot-remove.sh" 755
# slot-add.sh installs these into each new slot from the directory beside it.
fetch node/systemd/ccfleet-shell.service         "$LIB_DIR/systemd/ccfleet-shell.service" 644
fetch node/systemd/claude-remote-control.service "$LIB_DIR/systemd/claude-remote-control.service" 644
# Root runs these, and every slot's user runs agent.py: owned by root and
# writable by nobody else, or one slot could change what root runs next.
chown -R root:root "$LIB_DIR"
chmod 755 "$LIB_DIR" "$LIB_DIR/ccfleet_agent" "$LIB_DIR/systemd"
note "in $LIB_DIR, owned by root"

step "3/5  its configuration"
mkdir -p "$ETC_DIR" "$STATE_DIR"
# The directory closes first, so the token is never reachable by anyone else,
# not even for the moment between writing the file and setting its mode.
chmod 700 "$ETC_DIR" "$STATE_DIR"
printf 'CCFLEET_URL=%s\nCCFLEET_NODE_ID=%s\nCCFLEET_NODE_TOKEN=%s\nCCFLEET_LIB_DIR=%s\nCCFLEET_STATE_FILE=%s\n' \
  "$SERVER" "$NODE_ID" "$TOKEN" "$LIB_DIR" "$STATE_DIR/machine.json" > "$ETC_DIR/agent.env"
# Set, not left to the umask: over an existing file the old mode survives, and
# a hand-made agent.env left world-readable would stay that way.
chmod 600 "$ETC_DIR/agent.env"
note "$ETC_DIR/agent.env, readable by root only"

step "4/5  the timer"
for unit in ccfleet-machine.service ccfleet-machine.timer; do
  fetch "node/systemd/$unit" "$UNIT_DIR/$unit" 644
done
# The unit names the default paths; point it at the ones used here, so the
# service that runs is the one this script just installed.
sed -i.orig -e "s#/usr/local/lib/ccfleet#$LIB_DIR#g" -e "s#/etc/ccfleet#$ETC_DIR#g" \
  "$UNIT_DIR/ccfleet-machine.service"
rm -f "$UNIT_DIR/ccfleet-machine.service.orig"
systemctl daemon-reload
systemctl enable --now ccfleet-machine.timer >/dev/null 2>&1 || die "could not enable ccfleet-machine.timer"

step "5/5  first report"
# A oneshot: this returns when the run has finished, so its result is real.
if ! systemctl start ccfleet-machine.service; then
  die "the machine agent's first run failed. Look at: journalctl -u ccfleet-machine.service -n 50"
fi
timer_state="$(systemctl is-active ccfleet-machine.timer 2>/dev/null || true)"
[ "$timer_state" = active ] || die "ccfleet-machine.timer is $timer_state, not active; the machine would report once and never again"
note "reported to $SERVER; the timer runs it every minute from here"

cat <<MSG

──────────────────────────────────────────────────────────────────────
 $NODE_ID is a shared machine.

 Declare its slots from the fleet server, for example:

     ccfleetd slot capacity $NODE_ID 4
     ccfleetd slot add ${NODE_ID}-01 --machine $NODE_ID --unix-user slot01

 Do not create the slot users yourself. Each is made when somebody claims it,
 and a user that already exists reads as occupied and is never handed out.
──────────────────────────────────────────────────────────────────────
MSG
