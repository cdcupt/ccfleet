#!/usr/bin/env bash
# ccfleet one-line node install. Run as root on a fresh server.
#
#   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/install.sh \
#     | sudo bash -s -- --server https://fleet.example.com \
#                       --node alice-node --token <64-hex> --owner alice \
#                       --ssh-key "ssh-ed25519 AAAA... alice"
#
# Takes a blank server to a node that is ready for its owner to sign in: hardened,
# Claude Code installed, agent reporting, a persistent work session, and Remote
# Control so the owner can work from claude.ai with nothing installed locally.
#
# The one thing it cannot do is sign in. A Claude subscription login must complete
# through Anthropic's own flow, so the owner does that themselves, once. Everything
# up to that point is this script.
set -euo pipefail

SERVER="" NODE_ID="" TOKEN="" OWNER="" SSH_KEY="" SKIP_HARDEN=no NO_REMOTE=no
REPO_RAW="${CCFLEET_REPO_RAW:-https://raw.githubusercontent.com/cdcupt/ccfleet/main}"

die() { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

usage() {
  # Self-contained on purpose. The documented form pipes this script into bash, so
  # $0 is "bash", not a path, and anything that reads $0 prints rubbish or dies
  # under pipefail before the user ever sees how to call it.
  cat <<'USAGE'
ccfleet node install — takes a blank server to ready-for-sign-in.

  curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/install.sh \
    | sudo bash -s -- --server https://fleet.example.com \
                      --node alice-node --token <64-hex> --owner alice \
                      --ssh-key "ssh-ed25519 AAAA... alice"

Required:
  --server URL      fleet server base URL, from the console
  --node ID         node id, from the console
  --token HEX       node token, shown once by the console
  --owner NAME      unix user to create for the person using this node

Optional:
  --ssh-key "..."   their public key. Without one, SSH hardening is skipped so
                    you cannot be locked out, and you must add a key yourself.
  --skip-harden     do not touch the firewall or sshd. Use on a box that is
                    already carrying other services.
  --no-remote-control  set up everything except Remote Control.
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    --node)   NODE_ID="${2:-}"; shift 2 ;;
    --token)  TOKEN="${2:-}"; shift 2 ;;
    --owner)  OWNER="${2:-}"; shift 2 ;;
    --ssh-key) SSH_KEY="${2:-}"; shift 2 ;;
    --skip-harden) SKIP_HARDEN=yes; shift ;;
    --no-remote-control) NO_REMOTE=yes; shift ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$SERVER" ] && [ -n "$NODE_ID" ] && [ -n "$TOKEN" ] && [ -n "$OWNER" ] || usage
case "$SERVER" in http://*|https://*) ;; *) die "--server must start with http:// or https://" ;; esac
printf '%s' "$NODE_ID" | grep -qE '^[a-z0-9][a-z0-9-]{1,39}$' || die "--node must be lowercase letters, digits and hyphens"
printf '%s' "$TOKEN"   | grep -qE '^[0-9a-f]{64}$'            || die "--token must be the 64-character value from the console"
# Same pattern as adduser's NAME_REGEX on Debian and Ubuntu: a name this accepts
# must be one adduser will actually create, or the install fails after changing things.
printf '%s' "$OWNER"   | grep -qE '^[a-z][a-z0-9_-]{0,31}$'   || die "--owner must start with a lowercase letter, then lowercase letters, digits, underscore or hyphen"

# Root is required for the work, but only after the arguments are known good, so a
# typo is caught without sudo and the checks above can be exercised by tests.
[ "$(id -u)" -eq 0 ] || die "run this as root (prefix it with sudo)"

# 3 of the 4 lockout paths found in review start here, so resolve the account
# properly rather than assuming /home/$OWNER.
if id "$OWNER" >/dev/null 2>&1; then
  OWNER_UID="$(id -u "$OWNER")"
  [ "$OWNER_UID" -ge 1000 ] || die "$OWNER is a system account (uid $OWNER_UID); pick a normal user"
  HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
  [ -n "$HOME_DIR" ] && [ "$HOME_DIR" != "/" ] || die "$OWNER has no usable home directory"
else
  HOME_DIR="/home/$OWNER"
fi
as_owner() { sudo -u "$OWNER" HOME="$HOME_DIR" bash -c "$1"; }
user_systemctl() {
  local uid; uid="$(id -u "$OWNER")"
  sudo -u "$OWNER" XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
}

step "1/8  packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
# libpam-systemd is not optional: without it there is no pam_systemd.so, so
# XDG_RUNTIME_DIR is never set, the per-user systemd manager never starts, and
# every timer this script enables would silently never run.
# sudo is in this list because the script drops to the owner for every user-level
# step from here on, and a minimal image does not necessarily ship it; without it
# the install would modify the system and then fail at the first as_owner call.
apt-get install -y -q sudo tmux mosh curl git python3 ca-certificates libpam-systemd >/dev/null
note "installed: sudo, tmux, mosh, git, python3, and libpam-systemd so user services can run"

step "2/8  owner account"
if ! id "$OWNER" >/dev/null 2>&1; then adduser --disabled-password --gecos "" "$OWNER" >/dev/null; fi
usermod -aG sudo "$OWNER"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$OWNER" > "/etc/sudoers.d/90-ccfleet-$OWNER"
chmod 440 "/etc/sudoers.d/90-ccfleet-$OWNER"
install -d -m 700 -o "$OWNER" -g "$OWNER" "$HOME_DIR/.ssh"
if [ -n "$SSH_KEY" ]; then
  # Validate before it can influence the hardening decision: a malformed key that
  # still looks non-empty would leave nothing usable in authorized_keys and lock
  # everyone out the moment password auth is disabled.
  # Validated through stdin, not a temp file. A predictable path under /tmp that
  # root writes to is a symlink attack: any local user can pre-create it and have
  # the installer clobber a file of their choosing.
  if ! printf '%s\n' "$SSH_KEY" | ssh-keygen -l -f - >/dev/null 2>&1; then
    die "--ssh-key is not a valid public key; refusing to continue rather than risk a lockout"
  fi
  touch "$HOME_DIR/.ssh/authorized_keys"
  grep -qF "$SSH_KEY" "$HOME_DIR/.ssh/authorized_keys" || printf '%s\n' "$SSH_KEY" >> "$HOME_DIR/.ssh/authorized_keys"
  chmod 600 "$HOME_DIR/.ssh/authorized_keys"; chown "$OWNER:$OWNER" "$HOME_DIR/.ssh/authorized_keys"
fi
loginctl enable-linger "$OWNER"
note "user $OWNER created, lingering enabled so services survive logout"

step "3/8  hardening"
if [ "$SKIP_HARDEN" = yes ]; then
  note "skipped on request"
elif [ -z "$SSH_KEY" ]; then
  note "SKIPPED: no --ssh-key given, and disabling password auth without one would lock everyone out"
  note "add a key, then re-run with --ssh-key, or harden by hand"
else
  grep -qF "$SSH_KEY" "$HOME_DIR/.ssh/authorized_keys" 2>/dev/null \
    || die "the key is not in $HOME_DIR/.ssh/authorized_keys; not disabling password auth"
  apt-get install -y -q ufw fail2ban unattended-upgrades >/dev/null
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nX11Forwarding no\nMaxAuthTries 3\n' \
    > /etc/ssh/sshd_config.d/60-ccfleet.conf
  sshd -t || die "sshd rejected the hardening config; nothing was reloaded"
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
  ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null; ufw allow 60000:61000/udp comment mosh >/dev/null
  ufw --force enable >/dev/null
  printf '[sshd]\nenabled = true\nmaxretry = 4\nbantime = 1h\nfindtime = 10m\n' > /etc/fail2ban/jail.d/sshd.local
  systemctl enable --now fail2ban >/dev/null 2>&1 || true
  if [ "$(systemctl is-active fail2ban 2>/dev/null)" != active ]; then
    die "fail2ban did not start; refusing to report this server as hardened. Fix it, or re-run with --skip-harden if you accept the risk"
  fi
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  note "key-only SSH, firewall, fail2ban (verified active), unattended security upgrades"
fi

step "4/8  Claude Code"
as_owner 'mkdir -p ~/.local/bin ~/.config/ccfleet ~/.config/systemd/user ~/workspace'
as_owner '[ -x ~/.local/bin/claude ] || curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1'
as_owner 'grep -q DISABLE_AUTOUPDATER ~/.profile 2>/dev/null || printf "\n# ccfleet: upgrades are staged by the operator\nexport DISABLE_AUTOUPDATER=1\nexport PATH=\"\$HOME/.local/bin:\$PATH\"\n" >> ~/.profile'
CC_VERSION="$(as_owner '"$HOME"/.local/bin/claude --version 2>/dev/null | head -1' || echo unknown)"
note "installed: $CC_VERSION"

step "5/8  pre-answer the setup prompts"
# Claude Code asks two one-time questions that have nothing to do with the user's
# account: whether this folder is trusted, and whether Remote Control may run.
# Both are decisions the operator has already made by provisioning this node, and
# leaving them would mean every install needs an interactive terminal. Recorded
# here so the owner's only interaction is the sign-in itself, which is the one
# that genuinely must be theirs.
as_owner "python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.claude.json')
d = json.load(open(p)) if os.path.exists(p) else {}
d['hasCompletedOnboarding'] = True
d.setdefault('projects', {})
d['projects'].setdefault(os.path.expanduser('~/workspace'), {})['hasTrustDialogAccepted'] = True
$( [ "$NO_REMOTE" = yes ] || echo "d['remoteDialogSeen'] = True" )
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
os.chmod(p, 0o600)
PY"
note "workspace trusted$( [ "$NO_REMOTE" = yes ] && echo "" || echo ", Remote Control accepted" )"

step "6/8  agent and services"
for f in ccfleet_agent/agent.py:ccfleet-agent node/backup.sh:ccfleet-backup node/exitip.sh:exitip node/upgrade-claude.sh:ccfleet-upgrade-claude; do
  src="${f%%:*}"; dst="${f##*:}"
  as_owner "curl -fsSL '$REPO_RAW/$src' -o ~/.local/bin/$dst && chmod 755 ~/.local/bin/$dst"
done
for u in ccfleet-agent.service ccfleet-agent.timer ccfleet-backup.service ccfleet-backup.timer \
         ccfleet-shell.service ccfleet-tunnel.service claude-remote-control.service; do
  as_owner "curl -fsSL '$REPO_RAW/node/systemd/$u' -o ~/.config/systemd/user/$u"
done
as_owner "curl -fsSL '$REPO_RAW/node/attach.sh' -o /tmp/attach.sh"
as_owner 'MARKER="# ccfleet: attach to the persistent work session"
  grep -qF "$MARKER" ~/.bashrc 2>/dev/null || { echo; cat /tmp/attach.sh; } >> ~/.bashrc; rm -f /tmp/attach.sh'
printf 'CCFLEET_URL=%s\nCCFLEET_NODE_ID=%s\nCCFLEET_NODE_TOKEN=%s\n' "$SERVER" "$NODE_ID" "$TOKEN" \
  > "$HOME_DIR/.config/ccfleet/agent.env"
chmod 600 "$HOME_DIR/.config/ccfleet/agent.env"; chown "$OWNER:$OWNER" "$HOME_DIR/.config/ccfleet/agent.env"
note "agent, helpers, units and login auto-attach in place"

step "7/8  start everything"
user_systemctl daemon-reload
# No blanket || true here. A service that fails to come up is the difference
# between a working node and one that looks provisioned and reports nothing, and
# the installer must not call that ready.
FAILED=""
for unit in ccfleet-shell.service ccfleet-agent.timer ccfleet-backup.timer; do
  user_systemctl enable --now "$unit" >/dev/null 2>&1 || FAILED="$FAILED $unit"
done
if [ "$NO_REMOTE" = yes ]; then
  # Idempotent: re-running with this flag must leave Remote Control off, not
  # merely decline to switch it on.
  user_systemctl disable --now claude-remote-control.service >/dev/null 2>&1 || true
  rc_state="$(user_systemctl is-active claude-remote-control.service 2>/dev/null || echo inactive)"
  if [ "$rc_state" = active ]; then
    FAILED="$FAILED claude-remote-control.service(still-running)"
    note "remote control could NOT be disabled; it is still running"
  else
    note "remote control disabled on request (verified $rc_state)"
  fi
else
  # Enabled and started, but deliberately NOT part of the readiness gate below.
  # Remote Control needs an authenticated Claude session and there is not one yet:
  # the owner signs in after this script finishes. The unit has Restart=on-failure
  # with a 30s delay, and because that spacing never trips systemd's start limit it
  # keeps retrying and comes up by itself the moment the sign-in completes.
  user_systemctl enable --now claude-remote-control.service >/dev/null 2>&1 || true
fi
sleep 3

# Remote Control is not in this list; see the comment above.
CHECK="ccfleet-shell.service ccfleet-agent.timer ccfleet-backup.timer"
for unit in $CHECK; do
  state="$(user_systemctl is-active "$unit" 2>/dev/null || echo inactive)"
  note "$(printf '%-32s %s' "$unit" "$state")"
  case "$state" in active) ;; *) case "$FAILED" in *"$unit"*) ;; *) FAILED="$FAILED $unit" ;; esac ;; esac
done
if [ "$NO_REMOTE" != yes ]; then
  rc_now="$(user_systemctl is-active claude-remote-control.service 2>/dev/null || echo inactive)"
  note "$(printf '%-32s %s' claude-remote-control.service "$rc_now (activates after sign-in)")"
fi
if [ -n "$FAILED" ]; then
  printf '\n\033[31mnot ready:\033[0m these services did not come up:%s\n' "$FAILED" >&2
  printf 'inspect with:  sudo -u %s XDG_RUNTIME_DIR=/run/user/%s systemctl --user status <unit>\n' \
    "$OWNER" "$(id -u "$OWNER")" >&2
  exit 1
fi

step "8/8  first heartbeat"
as_owner '"$HOME"/.local/bin/ccfleet-agent 2>&1 | tail -1' || note "the agent could not reach $SERVER yet; check the URL and token"

cat <<MSG

──────────────────────────────────────────────────────────────────────
 Node "$NODE_ID" is ready. One step remains, and only its owner can do it.

 As $OWNER on this machine:

     claude          # choose the claude.ai login, approve in a browser,
                     # paste the code back
     /status         # confirms the account, with no base URL and no auth token

 Remote Control cannot start until that sign-in exists, so it is currently
 retrying every 30 seconds and will come up on its own within a minute of it.
 To watch: systemctl --user status claude-remote-control.service

 After that, $OWNER works from a terminal (ssh lands in a live session) or
 from claude.ai/code and the Claude phone app, with nothing installed there.

 The console will show this node green once the sign-in is done.
──────────────────────────────────────────────────────────────────────
MSG
