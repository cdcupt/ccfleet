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

# 5. User-level systemd units.
for unit in ccfleet-agent.service ccfleet-agent.timer ccfleet-backup.service ccfleet-backup.timer claude-remote-control.service ccfleet-tunnel.service; do
  fetch "node/systemd/$unit" "$UNITS/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now ccfleet-agent.timer
systemctl --user enable --now ccfleet-backup.timer

cat <<MSG

Owner setup done. Remaining steps, in order:

  1. Edit $CONF/agent.env with the URL, node id and token printed by
     'ccfleetd node add' on the fleet server.
  2. tmux new -s cc
     claude              # sign in with YOUR account: /login, open the URL on your
                         # laptop, paste the code back into this terminal
     /status             # Login row shows your account; no base URL, no auth token
  3. ccfleet-agent --print   # dry run; then: systemctl --user start ccfleet-agent.service
  4. Optional phone/browser access:
     claude remote-control    # accept the one-time prompt once, then Ctrl-C and run
     systemctl --user enable --now claude-remote-control.service
MSG
