#!/usr/bin/env bash
# Forced entry point for a ccfleet CLI device key. The key cannot choose a
# command: every connection lands in the same persistent Claude Code session.

set -euo pipefail

if [ ! -t 0 ] || [ ! -t 1 ]; then
  printf 'ccfleet needs an interactive terminal\n' >&2
  exit 2
fi

case "${TERM:-}" in
  "" | dumb | -*) TERM=xterm-256color; export TERM ;;
esac
if command -v infocmp >/dev/null 2>&1 && ! infocmp -- "$TERM" >/dev/null 2>&1; then
  TERM=xterm-256color
  export TERM
fi

mkdir -p "$HOME/workspace"
# Prefer a tmux server born under the lingering user manager, so the session
# survives sshd closing its login scope. Fall back to creating it here on
# systems whose user manager is temporarily unavailable.
if ! tmux has-session -t ccfleet 2>/dev/null; then
  systemctl --user restart ccfleet-shell.service >/dev/null 2>&1 || true
fi
exec tmux new-session -A -s ccfleet -c "$HOME/workspace" "$HOME/.local/bin/claude"
