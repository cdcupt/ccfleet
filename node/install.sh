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
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'USAGE'
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

[ "$(id -u)" -eq 0 ] || die "run this as root (prefix it with sudo)"
[ -n "$SERVER" ] && [ -n "$NODE_ID" ] && [ -n "$TOKEN" ] && [ -n "$OWNER" ] || usage
case "$SERVER" in http://*|https://*) ;; *) die "--server must start with http:// or https://" ;; esac
printf '%s' "$NODE_ID" | grep -qE '^[a-z0-9][a-z0-9-]{1,39}$' || die "--node must be lowercase letters, digits and hyphens"
printf '%s' "$TOKEN"   | grep -qE '^[0-9a-f]{64}$'            || die "--token must be the 64-character value from the console"
printf '%s' "$OWNER"   | grep -qE '^[a-z_][a-z0-9_-]{0,31}$'  || die "--owner must be a valid unix user name"

HOME_DIR="/home/$OWNER"
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
apt-get install -y -q tmux mosh curl git python3 ca-certificates libpam-systemd >/dev/null
note "installed, including libpam-systemd so user services can run"

step "2/8  owner account"
if ! id "$OWNER" >/dev/null 2>&1; then adduser --disabled-password --gecos "" "$OWNER" >/dev/null; fi
usermod -aG sudo "$OWNER"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$OWNER" > "/etc/sudoers.d/90-ccfleet-$OWNER"
chmod 440 "/etc/sudoers.d/90-ccfleet-$OWNER"
install -d -m 700 -o "$OWNER" -g "$OWNER" "$HOME_DIR/.ssh"
if [ -n "$SSH_KEY" ]; then
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
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  note "key-only SSH, firewall, fail2ban, unattended security upgrades"
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
user_systemctl enable --now ccfleet-shell.service >/dev/null 2>&1 || true
user_systemctl enable --now ccfleet-agent.timer ccfleet-backup.timer >/dev/null 2>&1 || true
[ "$NO_REMOTE" = yes ] || user_systemctl enable --now claude-remote-control.service >/dev/null 2>&1 || true
sleep 3
for s in ccfleet-shell.service ccfleet-agent.timer ccfleet-backup.timer; do
  note "$(printf '%-30s %s' "$s" "$(user_systemctl is-active "$s" 2>/dev/null || echo inactive)")"
done
[ "$NO_REMOTE" = yes ] || note "$(printf '%-30s %s' claude-remote-control.service "$(user_systemctl is-active claude-remote-control.service 2>/dev/null || echo inactive)")"

step "8/8  first heartbeat"
as_owner '"$HOME"/.local/bin/ccfleet-agent 2>&1 | tail -1' || note "the agent could not reach $SERVER yet; check the URL and token"

cat <<MSG

──────────────────────────────────────────────────────────────────────
 Node "$NODE_ID" is ready. One step remains, and only its owner can do it.

 As $OWNER on this machine:

     claude          # choose the claude.ai login, approve in a browser,
                     # paste the code back
     /status         # confirms the account, with no base URL and no auth token

 After that, $OWNER works from a terminal (ssh lands in a live session) or
 from claude.ai/code and the Claude phone app, with nothing installed there.

 The console will show this node green once the sign-in is done.
──────────────────────────────────────────────────────────────────────
MSG
