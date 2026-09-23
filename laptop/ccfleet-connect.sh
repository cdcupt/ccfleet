#!/usr/bin/env bash
# Connect this computer to your Claude account, once, with no browser login.
#
#   ccfleet-connect             prompt for the token (does not echo) - preferred
#   echo "$TOKEN" | ccfleet-connect --stdin
#   ccfleet-connect --status    what is this computer using
#   ccfleet-connect --remove    undo it
#
# One computer, one Claude account. ccfleet runs one account per slot, and you
# can use that account from as many of your own computers as you like; none of
# them keeps a second account on the side to switch to. Connecting again with
# another token replaces the one this computer had.
#
# Put --no-exec first to skip the fresh shell it hands you at the end;
# a provisioning script wants its own shell back, not a new one.
#
# Passing the token as an argument works but is discouraged: it lands in shell
# history and is visible in `ps` to anyone else on the machine.
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
# Where an earlier version kept tokens for several accounts under names. Only
# read to retire them: a computer uses one account now.
LEGACY_TOKENS_DIR="$(dirname "$TOKEN_FILE")/tokens"
MARK_BEGIN="# >>> ccfleet connect >>>"
MARK_END="# <<< ccfleet connect <<<"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

shell_rc() {
  # Where an interactive login shell would pick this up. Respects the shell the
  # user actually runs rather than assuming bash.
  case "${SHELL##*/}" in
    zsh)  printf '%s\n' "${ZDOTDIR:-$HOME}/.zshrc" ;;
    # .bashrc, not .bash_profile: an ordinary interactive terminal starts a
    # non-login shell and reads .bashrc. Distro .bash_profile files conventionally
    # source .bashrc, so the login case is covered too; if yours does not, add
    # `. ~/.bashrc` to it.
    bash) printf '%s\n' "$HOME/.bashrc" ;;
    fish) printf '%s\n' "$HOME/.config/fish/config.fish" ;;
    *)    printf '%s\n' "$HOME/.profile" ;;
  esac
}

file_mode() {
  # stat's flags differ between BSD and GNU, and this script runs on both.
  # Chaining them with || is not enough: on GNU, -f means --file-system, so
  # `stat -f '%OLp' file` SUCCEEDS and prints the format string unexpanded. The
  # fallback then never runs and chmod is handed nonsense. So try each and keep
  # the first answer that actually looks like a mode.
  local mode
  mode="$(stat -c '%a' "$1" 2>/dev/null)"
  case "$mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) printf '%s' "$mode"; return 0 ;;
  esac
  mode="$(stat -f '%OLp' "$1" 2>/dev/null)"
  case "$mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) printf '%s' "$mode"; return 0 ;;
  esac
  printf '600'
}

check_rc_strippable() {
  # A begin marker with no matching end means a previous write was interrupted.
  # Stripping from there to EOF would delete everything written after it, so
  # refuse. Called before anything is persisted as well as inside strip_block,
  # so a refusal never leaves a token on disk with no rc line to use it.
  local rc="$1"
  [ -f "$rc" ] || return 0
  if grep -qxF "$MARK_BEGIN" "$rc" 2>/dev/null && ! grep -qxF "$MARK_END" "$rc" 2>/dev/null; then
    die "$rc has an unfinished ccfleet block (a '$MARK_BEGIN' with no matching end).
     Remove those lines by hand first; refusing to guess where the block ends."
  fi
}

strip_block() {
  # Remove any previous block, leaving the rest of the file untouched.
  local rc="$1" mode
  [ -f "$rc" ] || return 0
  check_rc_strippable "$rc"
  mode="$(file_mode "$rc")"
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1 } skip == 0 { print } $0 == e { skip = 0 }
  ' "$rc" > "$rc.ccfleet-tmp" || { rm -f "$rc.ccfleet-tmp"; return 1; }
  # A fresh temp file carries default permissions, so replacing an rc with one
  # would quietly widen a 0600 file to 0644. Carry the original mode across.
  chmod "$mode" "$rc.ccfleet-tmp" 2>/dev/null || true
  mv "$rc.ccfleet-tmp" "$rc"
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
  # fish keeps its config under ~/.config/fish, which may not exist yet. Failing
  # here would leave the token already written and the device half-connected.
  mkdir -p "$(dirname "$rc")"
  strip_block "$rc"
  # Belt and braces. strip_block above rewrites the file through awk, whose
  # print always terminates a line, so by here the rc already ends in a newline
  # and the marker cannot be joined to the user's last command. This keeps that
  # invariant from depending on an implementation detail of strip_block.
  if [ -s "$rc" ] && [ -n "$(tail -c1 "$rc")" ]; then
    printf '\n' >> "$rc"
  fi
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

atomic_put() {
  # Copy stdin to $1 through a temp file beside it and a rename. A shell that
  # starts while the token is being replaced reads the old one or the new one,
  # never half of either, and a failure leaves the old one in place. mktemp
  # makes the file 0600; chmod says so anyway, whatever the umask.
  local dest="$1" tmp
  tmp="$(mktemp "$(dirname "$dest")/.ccfleet-token.XXXXXX")" || return 1
  if ! { cat > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$dest"; }; then
    rm -f "$tmp"
    return 1
  fi
}

wipe() {
  if command -v shred >/dev/null 2>&1; then
    shred -u "$1" 2>/dev/null || rm -f "$1"
  else
    rm -f "$1"
  fi
}

looks_like_saved_token() {
  # What the earlier version wrote into its tokens directory, and nothing else:
  # a file named like its names (or its temp files), holding a setup-token.
  # That directory sits next to the token file, and the token file's place can
  # be moved with CCFLEET_TOKEN_FILE, so it may be somebody's own directory
  # that happens to be called "tokens". Anything that does not look like ours
  # is left exactly where it is.
  local f="$1" name="${1##*/}"
  [ -f "$f" ] && [ ! -L "$f" ] || return 1
  case "$name" in
    .ccfleet-token.*) ;;                  # its temp files, from an interrupted write
    ""|-*|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*) return 1 ;;
    *) [ "${#name}" -le 32 ] || return 1 ;;
  esac
  case "$(head -c 13 "$f" 2>/dev/null)" in
    sk-ant-oat01-) return 0 ;;
  esac
  return 1
}

retire_saved_tokens() {
  # An earlier version kept tokens for several Claude accounts under names and
  # switched between them. A computer uses one account now, so on one that
  # saved some, keep the token in use and wipe the rest. Only a count is said:
  # they are credentials, so nothing about them reaches the terminal.
  [ -d "$LEGACY_TOKENS_DIR" ] && [ ! -L "$LEGACY_TOKENS_DIR" ] || return 0
  local f others=0
  for f in "$LEGACY_TOKENS_DIR"/* "$LEGACY_TOKENS_DIR"/.[!.]*; do
    looks_like_saved_token "$f" || continue
    case "${f##*/}" in
      .ccfleet-token.*) ;;          # a copy an interrupted write left: not an account
      *) if ! { [ -f "$TOKEN_FILE" ] && cmp -s "$f" "$TOKEN_FILE"; }; then
           others=$((others + 1))
         fi ;;
    esac
    wipe "$f"
  done
  rmdir "$LEGACY_TOKENS_DIR" 2>/dev/null || true
  if [ "$others" -gt 0 ]; then
    note "removed $others other saved token(s): a computer uses one Claude account now. Revoke them in your Claude account settings if nothing else uses them."
  fi
}

read_token() {
  # A credential on the command line lands in shell history and is visible in
  # `ps` to anyone on the machine. Prefer stdin, or a prompt that does not echo.
  local token=""
  if [ ! -t 0 ]; then
    IFS= read -r token || true
  else
    printf 'Paste the token from `claude setup-token` (input hidden): ' >&2
    stty -echo 2>/dev/null || true
    IFS= read -r token || true
    stty echo 2>/dev/null || true
    printf '\n' >&2
  fi
  printf '%s' "$token"
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
  probe="$(CLAUDE_CODE_OAUTH_TOKEN="$token" claude auth status </dev/null 2>&1 || true)"
  case "$probe" in
    *'"loggedIn": true'*) : ;;
    *) die "that token was refused. Ask for a fresh one: claude setup-token" ;;
  esac

  # Everything that can refuse must refuse before the credential lands on disk,
  # or a failure here leaves a token with no rc line to use it.
  local rc; rc="$(shell_rc)"
  check_rc_strippable "$rc"

  # rc first, token second. The rc line is guarded by [ -r ... ], so an rc
  # pointing at a token that does not exist yet is inert rather than broken —
  # whereas writing the token first and then failing on the rc would clobber a
  # previously working credential and leave nothing configured to use it.
  write_block "$rc"

  mkdir -p "$(dirname "$TOKEN_FILE")"
  retire_saved_tokens
  local replaced=no
  if [ -f "$TOKEN_FILE" ] && [ "$(cat "$TOKEN_FILE")" != "$token" ]; then
    replaced=yes
  fi
  printf '%s\n' "$token" | atomic_put "$TOKEN_FILE" || die "could not write $TOKEN_FILE"

  # Only now, with a token that has been accepted and written: nothing lands on
  # this machine until the credential has proved itself.
  install_self

  note "token stored in $TOKEN_FILE (0600)"
  if [ "$replaced" = yes ]; then
    note "it replaces the token this computer had; revoke that one in your Claude account settings if nothing else uses it"
  fi
  note "$rc now exports it for new shells"
  hand_over_shell
}

hand_over_shell() {
  # A process cannot put a variable into the shell that started it — that is
  # what a child process is. So the choice is to tell somebody to run an export
  # by hand, or to hand them a shell that already has it. The second is what
  # they wanted when they asked; it costs one exec and reads the rc line just
  # written, so the token arrives the same way it will on every later shell
  # rather than by a special case that only works today.
  if [ "$NO_EXEC" = yes ]; then
    note ""
    note "This shell does not have it yet. Open a new terminal, or run:"
    note "    exec \"\$SHELL\""
    return 0
  fi
  if [ ! -t 1 ] || [ -z "${SHELL:-}" ] || [ ! -x "${SHELL:-}" ]; then
    # No terminal to hand over, or no shell to hand over to. Say the command.
    note ""
    note "This shell does not have it yet. Open a new terminal, or run:"
    note "    exec \"\$SHELL\""
    return 0
  fi
  note ""
  note "Starting a fresh shell so claude works right here. Nothing else changes."
  note ""
  exec "$SHELL"
}

cmd_status() {
  retire_saved_tokens
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
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    note "in shell   : no, open a new terminal"
  elif [ "$CLAUDE_CODE_OAUTH_TOKEN" = "$(cat "$TOKEN_FILE")" ]; then
    note "in shell   : yes"
  else
    # Replacing the token changes what new shells read, not what this one has.
    note "in shell   : an older token, from before it was replaced; open a new terminal"
  fi
  command -v claude >/dev/null 2>&1 || { note "claude     : not installed"; return 0; }
  local out
  out="$(CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")" claude auth status </dev/null 2>&1 || true)"
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
  # Before the token itself, so what is counted as "other" is what it says.
  retire_saved_tokens
  if [ -f "$TOKEN_FILE" ]; then
    wipe "$TOKEN_FILE"
    note "token removed"
  fi
  local f
  # A temp file an interrupted write left behind holds a token like any other.
  for f in "$(dirname "$TOKEN_FILE")"/.ccfleet-token.*; do
    [ -f "$f" ] || continue
    wipe "$f"
  done
  note "$rc cleaned"
  note "this shell still has it until you close it: unset CLAUDE_CODE_OAUTH_TOKEN"
}

one_account() {
  # What used to switch a computer between accounts is refused, and says what
  # to do instead. Refused before anything is read or touched.
  die "$1 is gone: a computer uses one Claude account, the one on your slot. \
To change it, run ccfleet-connect with that account's token; it replaces the one in use."
}

SELF_URL="https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/ccfleet-connect.sh"

install_self() {
  # Leave the command behind. Run through `bash -c "$(curl ...)"` there is no
  # file to copy, so fetch a fresh one; without this the one-line install
  # leaves nothing on the machine, and --status and --remove are commands the
  # person was told about but does not have.
  #
  # Called only after the token has been accepted, so the promise that nothing
  # is written before the token is checked covers this too.
  local dest="$HOME/.local/bin/ccfleet-connect"
  if [ -x "$dest" ]; then
    return 0                       # theirs, possibly edited or newer
  fi
  # Failing to install is not failing to connect — the token is already in
  # place and works. But it is not a success either, and saying nothing would
  # leave someone typing a command that is not there.
  local why=""
  if ! mkdir -p "$(dirname "$dest")" 2>/dev/null; then
    why="could not create $(dirname "$dest")"
  elif [ -f "$0" ] && grep -q ccfleet-connect "$0" 2>/dev/null; then
    cp "$0" "$dest" 2>/dev/null || why="could not copy this script there"
  elif ! command -v curl >/dev/null 2>&1; then
    why="curl is not installed"
  elif curl -fsSL "$SELF_URL" -o "$dest.tmp" 2>/dev/null; then
    mv "$dest.tmp" "$dest" 2>/dev/null || { rm -f "$dest.tmp"; why="could not move it into place"; }
  else
    why="could not download it from $SELF_URL"
  fi
  if [ -n "$why" ]; then
    note ""
    note "NOTE: the token is set up, but ccfleet-connect was not installed:"
    note "      $why"
    note "      --status and --remove will not be available until you install it."
    return 0
  fi
  chmod 755 "$dest" 2>/dev/null || true
  note "installed ccfleet-connect to $dest"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) note "$HOME/.local/bin is not on your PATH; add it to use --status later" ;;
  esac
}

main() {
  # --no-exec, first if given, for anything scripted: replacing the shell is
  # the right end to an interactive setup and the wrong one inside somebody's
  # provisioning run. Taken positionally rather than filtered out of the list,
  # because filtering means splitting arguments and one of them is a token.
  NO_EXEC=no
  if [ "${1:-}" = "--no-exec" ]; then
    NO_EXEC=yes
    shift
  fi

  case "${1:-}" in
    --status|-s) cmd_status ;;
    --remove|-r)
      [ $# -le 1 ] || die "--remove takes no name now: a computer uses one Claude \
account, and ccfleet-connect --remove undoes it."
      cmd_remove ;;
    --add|--use|--list|-l) one_account "$1" ;;
    -h|--help)
      # To the first blank line rather than a counted range: the header grows
      # and a fixed number quietly starts cutting the end off the help.
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' ;;
    "")
      # No argument: read the token without it ever reaching a command line.
      cmd_connect "$(read_token)" ;;
    --stdin) cmd_connect "$(read_token)" ;;
    -*) die "unknown option: $1" ;;
    *)  cmd_connect "$1" ;;
  esac
}

main "$@"
