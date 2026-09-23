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

SERVER="" NODE_ID="" TOKEN="" OWNER="" SSH_KEY="" SKIP_HARDEN=no NO_REMOTE=no BYPASS=no
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
  --bypass-permissions  run Claude Code with no permission prompts on this node,
                    in the terminal and in sessions driven from claude.ai. Every
                    tool call then runs unasked, and the owner has passwordless
                    sudo, so this grants un-prompted root. Off by default.
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
    --bypass-permissions) BYPASS=yes; shift ;;
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
  # Debian and Ubuntu allocate ordinary logins in FIRST_UID..LAST_UID, 1000..59999.
  # A bare "uid >= 1000" lets `nobody` through at 65534, and step 2 below puts this
  # argument in the sudo group with NOPASSWD:ALL -- which would hand passwordless
  # root to the shared identity many daemons drop privileges to.
  [ "$OWNER_UID" -ge 1000 ] && [ "$OWNER_UID" -le 59999 ] \
    || die "$OWNER is a system account (uid $OWNER_UID); pick a normal user"
  # A uid inside that range is not on its own proof of a human account: service
  # accounts get created there too, and they are told apart by their login shell.
  OWNER_SHELL="$(getent passwd "$OWNER" | cut -d: -f7)"
  case "$OWNER_SHELL" in
    */nologin | */false | "")
      die "$OWNER is a service account (login shell '${OWNER_SHELL:-none}'); pick a normal user" ;;
  esac
  HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
  [ -n "$HOME_DIR" ] && [ "$HOME_DIR" != "/" ] || die "$OWNER has no usable home directory"
else
  HOME_DIR="/home/$OWNER"
fi
as_owner() { sudo -u "$OWNER" HOME="$HOME_DIR" bash -c "$1"; }

# Key-only SSH, on sshd's own word. A drop-in is read where the main config
# Includes sshd_config.d, and sshd keeps the FIRST value it meets for each
# keyword. So the drop-in says both halves, and before anything is reloaded
# `sshd -T` must report keys on and passwords off. Some provider images end
# sshd_config with `PubkeyAuthentication no`: passwords off there, keys left
# off, is a machine nobody can log in to. And an image that never Includes the
# directory would ignore the drop-in while this claimed the box hardened.
# Either way the drop-in goes and sshd is left exactly as it was.
# Kept byte-identical in bootstrap.sh and install.sh (a test compares them):
# each is fetched on its own through curl | bash and cannot source the other.
# shellcheck disable=SC2120  # bootstrap.sh passes no extra lines; install.sh passes one
harden_sshd() {  # harden_sshd [extra sshd_config line...]
  local dir="${CCFLEET_SSHD_DROPIN_DIR:-/etc/ssh/sshd_config.d}"
  local config="${CCFLEET_SSHD_CONFIG:-/etc/ssh/sshd_config}"
  local dropin="$dir/60-ccfleet.conf" effective
  mkdir -p "$dir"
  {
    printf '%s\n' "PubkeyAuthentication yes" "PasswordAuthentication no" \
      "KbdInteractiveAuthentication no" "PermitRootLogin prohibit-password" "X11Forwarding no"
    [ "$#" -eq 0 ] || printf '%s\n' "$@"
  } > "$dropin"
  if ! sshd -t -f "$config"; then
    rm -f "$dropin"
    echo "sshd rejected the hardening config; it was removed and nothing was reloaded" >&2
    return 1
  fi
  effective="$(sshd -T -f "$config" 2>/dev/null)" || effective=""
  if ! grep -qx "pubkeyauthentication yes" <<<"$effective" \
      || ! grep -qx "passwordauthentication no" <<<"$effective"; then
    rm -f "$dropin"
    echo "sshd would not run with key login on and password login off: $config either" \
      "does not Include $dir, or a setting sshd reads first overrides it (cloud-init's" \
      "50-cloud-init.conf is a common one). The drop-in was removed and nothing was reloaded." >&2
    return 1
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
}

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
  # python3-systemd is only a Recommends of fail2ban, so a box configured with
  # APT::Install-Recommends "false" will not have it -- and the jail below asks for
  # the systemd backend, which needs it. Name it explicitly rather than hope.
  apt-get install -y -q ufw fail2ban python3-systemd unattended-upgrades >/dev/null
  harden_sshd "MaxAuthTries 3" \
    || die "SSH was not hardened, and password login is as it was. Fix the cause above, or re-run with --skip-harden if you accept the risk"
  ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null; ufw allow 60000:61000/udp comment mosh >/dev/null
  ufw --force enable >/dev/null
  # backend = systemd, not the default "auto". auto hunts for /var/log/auth.log and,
  # when there is none, fails the entire service at startup with "Have not found any
  # log file for sshd jail" -- so the box ends up with no SSH protection at all. That
  # is not exotic: minimal cloud images ship without rsyslog and log only to the
  # journal. Measured on a blank Debian 12 with no rsyslog, fail2ban exited 255 and
  # this installer correctly refused to call the machine hardened. The journal exists
  # on every systemd box, so this one setting works on both kinds of image.
  printf '[sshd]\nenabled = true\nbackend = systemd\nmaxretry = 4\nbantime = 1h\nfindtime = 10m\n' > /etc/fail2ban/jail.d/sshd.local
  # apt-get above already started fail2ban, seconds before this jail file existed,
  # and `enable --now` does not restart a service that is already running. Without
  # an explicit restart the jail written just above sits unread until the next
  # reboot while `is-active` cheerfully reports "active" -- measured on a live node,
  # which ran Debian's defaults (maxretry 5, bantime 600) rather than ours for days.
  # So: restart, and then verify the JAIL, not merely the service.
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban >/dev/null 2>&1 || true
  jail_ok=no
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if [ "$(fail2ban-client get sshd maxretry 2>/dev/null)" = 4 ]; then jail_ok=yes; break; fi
    sleep 1
  done
  if [ "$jail_ok" != yes ]; then
    die "fail2ban is not enforcing the SSH jail we configured (checked with 'fail2ban-client get sshd maxretry'). Refusing to report this server as hardened. Fix it, or re-run with --skip-harden if you accept the risk"
  fi
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  note "key-only SSH, firewall, fail2ban (SSH jail verified live), unattended security upgrades"
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

# The terminal half of --bypass-permissions. The unit flag above only covers
# sessions Remote Control spawns; a session the owner starts by typing `claude`
# reads this instead. Re-running without the flag removes what a previous run set,
# so a node does not stay open by accident, but only what THIS installer wrote.
as_owner "python3 - <<'PY'
import json, os
p = os.path.expanduser('~/.claude/settings.json')
# Provenance lives in ccfleet's own directory, not in Claude Code's settings: it
# records that a previous run of THIS installer set the keys below, so a later
# run without the flag knows which ones it may remove. Settings the owner chose
# for themselves must survive an ordinary reinstall untouched.
marker = os.path.expanduser('~/.config/ccfleet/bypass-managed')
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p)) if os.path.exists(p) else {}
perms = d.get('permissions') or {}
bypass = '$BYPASS' == 'yes'
if bypass:
    # Snapshot what was there BEFORE the first time we touch it, so turning this
    # back off restores the owner's own choice instead of deleting it. Only on
    # the first run: a second run with the flag must not snapshot our own values.
    if not os.path.exists(marker):
        json.dump({'defaultMode': perms.get('defaultMode'),
                   'skipDangerousModePermissionPrompt': d.get('skipDangerousModePermissionPrompt')},
                  open(marker, 'w'))
    perms['defaultMode'] = 'bypassPermissions'
    # Suppresses the one-time 'accept responsibility' dialog, which would
    # otherwise block a session that nobody is sitting in front of.
    d['skipDangerousModePermissionPrompt'] = True
elif os.path.exists(marker):
    try:
        prev = json.load(open(marker))
    except (ValueError, OSError):
        prev = {}
    # Undo only while the value is still the one we set. If the owner has changed
    # it since, that newer choice is theirs and wins; we just forget ours.
    if perms.get('defaultMode') == 'bypassPermissions':
        if prev.get('defaultMode') is None:
            perms.pop('defaultMode', None)
        else:
            perms['defaultMode'] = prev['defaultMode']
    if d.get('skipDangerousModePermissionPrompt') is True:
        if prev.get('skipDangerousModePermissionPrompt') is None:
            d.pop('skipDangerousModePermissionPrompt', None)
        else:
            d['skipDangerousModePermissionPrompt'] = prev['skipDangerousModePermissionPrompt']
    os.remove(marker)
if perms:
    d['permissions'] = perms
else:
    d.pop('permissions', None)
tmp = p + '.tmp'
json.dump(d, open(tmp, 'w'), indent=2)
os.replace(tmp, p)
os.chmod(p, 0o600)
PY"
if [ "$BYPASS" = yes ]; then
  note "PERMISSION PROMPTS ARE OFF on this node, in the terminal and from claude.ai"
fi

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
# Read by claude-remote-control.service. Written every run, empty unless asked,
# so re-running without the flag turns bypass back off instead of leaving it on.
if [ "$BYPASS" = yes ]; then
  printf 'CCFLEET_RC_ARGS=--permission-mode bypassPermissions\n' > "$HOME_DIR/.config/ccfleet/remote-control.env"
else
  printf 'CCFLEET_RC_ARGS=\n' > "$HOME_DIR/.config/ccfleet/remote-control.env"
fi
chown "$OWNER:$OWNER" "$HOME_DIR/.config/ccfleet/remote-control.env"
chmod 600 "$HOME_DIR/.config/ccfleet/agent.env"; chown "$OWNER:$OWNER" "$HOME_DIR/.config/ccfleet/agent.env"
note "agent, helpers, units and login auto-attach in place"

step "7/8  start everything"
user_systemctl daemon-reload
# No blanket || true here. A service that fails to come up is the difference
# between a working node and one that looks provisioned and reports nothing, and
# the installer must not call that ready.
FAILED=""
RC_ENABLED=unknown
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
  # Enabled for future boots but deliberately NOT started now. Remote Control needs
  # an authenticated Claude session and there is not one until the owner signs in,
  # which happens after this script finishes.
  #
  # It is tempting to start it anyway and rely on Restart=on-failure, and an earlier
  # version of this script claimed exactly that. It is not true: the unit is
  # Type=forking around `tmux new-session -d`, so ExecStart succeeds the moment tmux
  # detaches and Claude's later authentication failure inside that session is never
  # reported to systemd. The restart would not fire, and the promise would be a lie.
  # The owner starts it once, after signing in; the closing message says so.
  user_systemctl enable claude-remote-control.service >/dev/null 2>&1 || true
  # The env file rewritten in step 6 does not reach a process that is already
  # running. Without this restart, re-running the installer WITHOUT
  # --bypass-permissions would leave an already-started node still bypassing
  # permission prompts, which is precisely the state the rewrite exists to undo.
  # Restarting is safe now: this unit owns a private tmux server, so it cannot
  # touch the owner's work session. It does end any live claude.ai session.
  if [ "$(user_systemctl is-active claude-remote-control.service 2>/dev/null)" = active ]; then
    user_systemctl restart claude-remote-control.service >/dev/null 2>&1 || true
    sleep 2
    # A restart that does not come back leaves a node that WAS working now dead,
    # so read the state instead of trusting the exit status we just discarded.
    rc_after="$(user_systemctl is-active claude-remote-control.service 2>/dev/null || echo inactive)"
    if [ "$rc_after" = active ]; then
      note "remote control restarted so it reads the permission settings from this run"
    else
      FAILED="$FAILED claude-remote-control.service(restart-failed:$rc_after)"
      note "remote control was running and did NOT come back after the restart ($rc_after)"
    fi
  fi
  # Verified, not assumed. The closing message tells the owner this unit returns
  # after a reboot, so an enable that did not take has to fail the install rather
  # than be swallowed -- same rule as the --no-remote-control branch above.
  RC_ENABLED="$(user_systemctl is-enabled claude-remote-control.service 2>/dev/null || echo unknown)"
  [ "$RC_ENABLED" = enabled ] || FAILED="$FAILED claude-remote-control.service(enable-failed:$RC_ENABLED)"
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
  note "$(printf '%-32s %s' claude-remote-control.service "$RC_ENABLED, starts after sign-in")"
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

 Then, once, to turn on access from claude.ai and the phone app:

     systemctl --user start claude-remote-control.service

 It is already enabled, so it comes back by itself after a reboot. It could not
 be started before the sign-in because Remote Control needs a login to exist.

 After that, $OWNER works from a terminal (ssh lands in a live session) or
 from claude.ai/code and the Claude phone app, with nothing installed there.

 The console will show this node green once the sign-in is done.
──────────────────────────────────────────────────────────────────────
MSG
