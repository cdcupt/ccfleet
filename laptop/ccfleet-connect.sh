#!/usr/bin/env bash
# Connect this device to a Claude account, once, with no browser login.
#
#   ccfleet-connect <token>     wire this device up
#   ccfleet-connect --status    what is this device using
#   ccfleet-connect --remove    undo it
#
# The token comes from `claude setup-token`, run by the account owner. It is
# Anthropic's own long-lived credential (one year, inference scope), so nothing
# here stores, forwards or substitutes anybody's login: the token lives on this
# device, in a file only you can read, and Claude Code picks it up from the
# environment exactly as Anthropic documents.
#
# What this does NOT give you is Remote Control — claude.ai/code and the phone
# app driving a machine. Those need a full-scope login, and Anthropic limits
# long-lived tokens to inference on purpose. Keep the node's own `claude
# auth login` for that; the two are complementary, not alternatives.

set -euo pipefail

TOKEN_FILE="${CCFLEET_TOKEN_FILE:-$HOME/.config/ccfleet/token}"
MARK_BEGIN="# >>> ccfleet connect >>>"
MARK_END="# <<< ccfleet connect <<<"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

shell_rc() {
  # Where an interactive login shell would pick this up. Respects the shell the
  # user actually runs rather than assuming bash.
  case "${SHELL##*/}" in
    zsh)  printf '%s\n' "${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) [ -f "$HOME/.bash_profile" ] && printf '%s\n' "$HOME/.bash_profile" \
            || printf '%s\n' "$HOME/.bashrc" ;;
    fish) printf '%s\n' "$HOME/.config/fish/config.fish" ;;
    *)    printf '%s\n' "$HOME/.profile" ;;
  esac
}

strip_block() {
  # Remove any previous block, leaving the rest of the file untouched.
  local rc="$1"
  [ -f "$rc" ] || return 0
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1 } skip == 0 { print } $0 == e { skip = 0 }
  ' "$rc" > "$rc.ccfleet-tmp" && mv "$rc.ccfleet-tmp" "$rc"
}

sq() {
  # Single-quote a path for embedding in a shell line, closing and reopening the
  # quote around any literal quote. The path lands in a file every new shell
  # sources, so an unquoted one with a space breaks the shell and one with a
  # metacharacter runs whatever it says.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_block() {
  local rc="$1" quoted
  quoted="$(sq "$TOKEN_FILE")"
  strip_block "$rc"
  {
    printf '%s\n' "$MARK_BEGIN"
    if [ "${SHELL##*/}" = "fish" ]; then
      # fish has no $(...); it uses (...) and its own quoting, but single quotes
      # are literal there too.
      printf 'test -r %s; and set -gx CLAUDE_CODE_OAUTH_TOKEN (cat %s)\n' \
             "$quoted" "$quoted"
    else
      printf '[ -r %s ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat %s)"\n' \
             "$quoted" "$quoted"
    fi
    printf '%s\n' "$MARK_END"
  } >> "$rc"
}

cmd_connect() {
  local token="$1"
  case "$token" in
    sk-ant-oat01-*) ;;
    *) die "that does not look like a setup-token credential (expected sk-ant-oat01-...)" ;;
  esac
  command -v claude >/dev/null 2>&1 || die "Claude Code is not installed on this device"

  # Verify BEFORE writing anything, so a bad token never gets persisted.
  note "checking the token..."
  local probe
  probe="$(CLAUDE_CODE_OAUTH_TOKEN="$token" claude auth status 2>&1 || true)"
  case "$probe" in
    *'"loggedIn": true'*) : ;;
    *) die "that token was refused. Ask for a fresh one: claude setup-token" ;;
  esac

  mkdir -p "$(dirname "$TOKEN_FILE")"
  ( umask 077; printf '%s\n' "$token" > "$TOKEN_FILE" )
  chmod 600 "$TOKEN_FILE"

  local rc; rc="$(shell_rc)"
  write_block "$rc"

  note "token stored in $TOKEN_FILE (0600)"
  note "$rc now exports it for new shells"
  note ""
  note "This shell does not have it yet. Either open a new terminal, or run:"
  note "    export CLAUDE_CODE_OAUTH_TOKEN=\"\$(cat $TOKEN_FILE)\""
  note ""
  note "Then just use claude. No login, on this or any device you do this on."
}

cmd_status() {
  if [ ! -r "$TOKEN_FILE" ]; then
    note "not connected (no $TOKEN_FILE)"
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      note "but CLAUDE_CODE_OAUTH_TOKEN is set in this shell"
    fi
    return 0
  fi
  note "token file : $TOKEN_FILE"
  # Never interpolate the token itself. ${VAR:-default} expands to the VALUE
  # when set, so the obvious one-liner here printed the whole credential.
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    note "in shell   : yes"
  else
    note "in shell   : no, open a new terminal"
  fi
  command -v claude >/dev/null 2>&1 || { note "claude     : not installed"; return 0; }
  local out
  out="$(CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")" claude auth status 2>&1 || true)"
  case "$out" in
    *'"loggedIn": true'*) note "account    : working (auth method $(printf '%s' "$out" |
        sed -n 's/.*"authMethod": "\([^"]*\)".*/\1/p'))" ;;
    *) note "account    : REFUSED — the token may have been revoked or expired" ;;
  esac
  note ""
  note "Remote Control is not available on a token; that needs claude auth login."
}

cmd_remove() {
  local rc; rc="$(shell_rc)"
  strip_block "$rc"
  if [ -f "$TOKEN_FILE" ]; then
    command -v shred >/dev/null 2>&1 && shred -u "$TOKEN_FILE" || rm -f "$TOKEN_FILE"
    note "token removed"
  fi
  note "$rc cleaned"
  note "this shell still has it until you close it: unset CLAUDE_CODE_OAUTH_TOKEN"
}

main() {
  case "${1:-}" in
    --status|-s) cmd_status ;;
    --remove|-r) cmd_remove ;;
    -h|--help|"")
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' ;;
    -*) die "unknown option: $1" ;;
    *)  cmd_connect "$1" ;;
  esac
}

main "$@"
