#!/usr/bin/env bash
# Forced entry point for a ccfleet CLI device key. The key cannot run arbitrary
# commands: it may only open, create or restart a validated Claude Code session
# under one of Claude Code's supported permission modes.

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

SESSION=ccfleet
ACTION=open
MODE=bypassPermissions
if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
  read -r PROTOCOL ACTION SESSION MODE EXTRA <<EOF
$SSH_ORIGINAL_COMMAND
EOF
  [ "$PROTOCOL" = ccfleet-session ] && [ -z "${EXTRA:-}" ] \
    || { printf 'unsupported CC Fleet session request\n' >&2; exit 2; }
fi

[[ "$SESSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$ ]] \
  || { printf 'invalid CC Fleet session name\n' >&2; exit 2; }
case "$ACTION" in open|new|restart) ;; *) printf 'invalid CC Fleet session action\n' >&2; exit 2 ;; esac
case "$MODE" in
  acceptEdits|auto|bypassPermissions|manual|dontAsk|plan) ;;
  *) printf 'invalid Claude permission mode\n' >&2; exit 2 ;;
esac

mkdir -p "$HOME/workspace"
# Keep the default session under the lingering user manager. Named sessions
# share that tmux server and therefore survive the SSH transport too.
if ! tmux has-session -t ccfleet 2>/dev/null; then
  systemctl --user restart ccfleet-shell.service >/dev/null 2>&1 || true
fi

CLAUDE=("$HOME/.local/bin/claude")
if [ "$MODE" = bypassPermissions ]; then
  CLAUDE+=(--dangerously-skip-permissions)
else
  CLAUDE+=(--permission-mode "$MODE")
fi

case "$ACTION" in
  open)
    exec tmux new-session -A -s "$SESSION" -c "$HOME/workspace" "${CLAUDE[@]}"
    ;;
  new)
    [ "$SESSION" != ccfleet ] \
      || { printf 'the default ccfleet session already exists; choose another name\n' >&2; exit 2; }
    ! tmux has-session -t "$SESSION" 2>/dev/null \
      || { printf 'session already exists: %s\n' "$SESSION" >&2; exit 2; }
    exec tmux new-session -s "$SESSION" -c "$HOME/workspace" "${CLAUDE[@]}"
    ;;
  restart)
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    if [ "$SESSION" = ccfleet ]; then
      systemctl --user restart ccfleet-shell.service >/dev/null 2>&1 || true
      exec tmux attach-session -t ccfleet
    fi
    exec tmux new-session -s "$SESSION" -c "$HOME/workspace" "${CLAUDE[@]}"
    ;;
esac
