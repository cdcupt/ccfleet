#!/usr/bin/env bash
# ccfleet owner setup. Run as the OWNER (never root) on a bootstrapped node.
#
#   ./setup-owner.sh
#
# Installs the unmodified Claude Code CLI with Anthropic's installer, turns off
# its auto-updater so upgrades are staged by you, installs the ccfleet heartbeat
# agent plus its user-level systemd timer, and drops templates for the agent env
# file. It never logs in for you: the last step is you running `claude` and
# completing /login through Anthropic's own flow.
set -euo pipefail

[[ "$EUID" -ne 0 ]] || { echo "run setup-owner.sh as the owner, not root" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_RAW="${CCFLEET_REPO_RAW:-https://raw.githubusercontent.com/cdcupt/ccfleet/main}"
BIN="$HOME/.local/bin"
CONF="$HOME/.config/ccfleet"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$BIN" "$CONF" "$UNITS"

fetch_text() {  # fetch_text <relative-path>  -> prints the file
  if [ -f "$SRC_DIR/../$1" ]; then cat "$SRC_DIR/../$1"; else curl -fsSL "$REPO_RAW/$1"; fi
}

fetch() {  # fetch <relative-path> <destination>
  if [[ -f "$SRC_DIR/../$1" ]]; then
    install -m "${3:-644}" "$SRC_DIR/../$1" "$2"
  else
    curl -fsSL "$REPO_RAW/$1" -o "$2" && chmod "${3:-644}" "$2"
  fi
}

# 1. Claude Code, unmodified, from Anthropic's installer.
if ! command -v claude >/dev/null 2>&1 && [[ ! -x "$BIN/claude" ]]; then
  curl -fsSL https://claude.ai/install.sh | bash
fi

# 2. Upgrades are yours to stage, so the auto-updater is off for this user.
if ! grep -q 'DISABLE_AUTOUPDATER' "$HOME/.profile" 2>/dev/null; then
  printf '\n# ccfleet: upgrades are staged by the operator\nexport DISABLE_AUTOUPDATER=1\n' >> "$HOME/.profile"
fi
grep -q '\.local/bin' "$HOME/.profile" 2>/dev/null || printf 'export PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.profile"

# 3. Heartbeat agent (single file, standard library only) and helpers.
fetch ccfleet_agent/agent.py "$BIN/ccfleet-agent" 755
fetch node/backup.sh "$BIN/ccfleet-backup" 755
fetch node/exitip.sh "$BIN/exitip" 755
fetch node/upgrade-claude.sh "$BIN/ccfleet-upgrade-claude" 755

# 4. Agent env file: created once, never overwritten.
if [[ ! -f "$CONF/agent.env" ]]; then
  fetch node/agent.env.example "$CONF/agent.env" 600
fi
chmod 600 "$CONF/agent.env"

# 5. User-level systemd units. These need a working per-user systemd manager,
# which a minimal image can lack: without libpam-systemd there is no
# pam_systemd.so, so XDG_RUNTIME_DIR is never set and user@<uid>.service fails.
# Enabling timers there looks like it worked and then nothing ever runs, so
# check before relying on it.
# Judge the reported state, not the exit status: is-system-running exits non-zero
# for "degraded" too, and a degraded manager still runs timers perfectly well.
# The states that matter here are the ones where no manager answers at all.
user_state="$(systemctl --user is-system-running 2>/dev/null || true)"
case "$user_state" in
  running|degraded|starting|initializing) ;;
  *)
  cat >&2 <<WARN

ERROR: this user has no working systemd manager, so the ccfleet timers cannot run.
       systemctl --user is-system-running reported: ${user_state:-no answer}

That usually means libpam-systemd is missing, so pam_systemd.so never sets
XDG_RUNTIME_DIR. Check with:

    systemctl status "user@\$(id -u).service"
    journalctl -u "user@\$(id -u).service" -n 20

On Debian or Ubuntu, as root:

    apt-get install -y libpam-systemd
    loginctl enable-linger \$(id -un)

then log out, log back in and re-run this script. Installing that package
changes PAM configuration, so on a box whose SSH access you depend on, keep a
second session open while you do it.

WARN
  exit 1
  ;;
esac

for unit in ccfleet-agent.service ccfleet-agent.timer ccfleet-backup.service ccfleet-backup.timer claude-remote-control.service ccfleet-tunnel.service ccfleet-shell.service; do
  fetch "node/systemd/$unit" "$UNITS/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now ccfleet-agent.timer
systemctl --user enable --now ccfleet-backup.timer
systemctl --user enable --now ccfleet-shell.service

# 6. Workspace. Claude Code will not serve Remote Control from a home directory,
#    so every node needs a project directory that the owner trusts once.
mkdir -p "$HOME/workspace"

# 7. Auto-attach on login, so a terminal user never has to know about tmux.
#    The snippet and its reasoning live in node/attach.sh; appended once.
MARKER="# ccfleet: attach to the persistent work session"
if ! grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
  { echo; fetch_text node/attach.sh; } >> "$HOME/.bashrc"
  echo "  login auto-attach installed in ~/.bashrc"
fi

cat <<MSG

Owner setup done. Remaining steps, in order:

  1. Edit $CONF/agent.env with the URL, node id and token printed by
     'ccfleetd node add' on the fleet server.
  2. tmux new -s cc
     claude              # sign in with YOUR account: /login, open the URL on your
                         # laptop, paste the code back into this terminal
     /status             # Login row shows your account; no base URL, no auth token
  3. ccfleet-agent --print   # dry run; then: systemctl --user start ccfleet-agent.service
  4. Phone and browser access, which is how most people will use this node:
     cd ~/workspace && claude          # answer "Yes, I trust this folder", then /exit
     claude remote-control             # answer y to the one-time prompt, then Ctrl-C
     systemctl --user enable --now claude-remote-control.service
     Afterwards open claude.ai/code and the session is there. Nothing is installed
     on the machine you sit at.
MSG
