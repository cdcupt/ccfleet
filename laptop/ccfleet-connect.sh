#!/usr/bin/env bash
# Connect this device to a Claude account, once, with no browser login.
#
#   ccfleet-connect               prompt for the token (does not echo) - preferred
#   echo "$TOKEN" | ccfleet-connect --stdin
#   ccfleet-connect --add NAME    the same, saved under a name, and switched to
#   ccfleet-connect --use NAME    switch this device to a token saved earlier
#   ccfleet-connect --list        the saved tokens, and which one is in use
#   ccfleet-connect --status      what is this device using
#   ccfleet-connect --remove NAME forget one saved token
#   ccfleet-connect --remove      undo it all: every saved token and the rc line
#
# Several Claude accounts on one device: add a token for each, under a name you
# choose (lowercase letters, digits and dashes), then switch with --use. New
# shells pick the switch up. A token given without a name is saved as
# "default", and a device connected before tokens had names keeps its token
# under that name.
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
# Saved tokens sit beside the one in use, a file per name. The rc line only
# ever reads TOKEN_FILE; switching copies a saved token over it.
TOKENS_DIR="$(dirname "$TOKEN_FILE")/tokens"
DEFAULT_NAME="default"
MAX_NAME_LEN=32
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

ensure_block() {
  # Switching only needs the rc line to exist; it reads whichever token is in
  # use. So leave a file that already has it exactly as it is, rather than
  # moving the block and rewriting somebody's rc on every switch.
  local rc="$1"
  if [ -f "$rc" ] && grep -qxF "$MARK_BEGIN" "$rc" 2>/dev/null; then
    check_rc_strippable "$rc"
    return 0
  fi
  write_block "$rc"
}

valid_name() {
  # Lowercase letters, digits and dashes, not starting with a dash, at most
  # MAX_NAME_LEN characters. A name becomes a path under TOKENS_DIR, so a slash
  # or a dot must never get through. The characters are spelled out rather
  # than written as ranges: a range like a-z follows the locale's collation in
  # some shells, and then matches capitals or accented letters.
  case "$1" in
    ""|-*|*[!abcdefghijklmnopqrstuvwxyz0123456789-]*) return 1 ;;
  esac
  [ "${#1}" -le "$MAX_NAME_LEN" ]
}

need_name() {
  valid_name "${1:-}" || die "a token name is lowercase letters, digits and dashes, \
starting with a letter or digit, at most $MAX_NAME_LEN characters"
}

private_tokens_dir() {
  # Tightened even when it already exists: every file in it is a credential.
  mkdir -p "$TOKENS_DIR"
  chmod 700 "$TOKENS_DIR"
}

atomic_put() {
  # Copy stdin to $1 through a temp file beside it and a rename. A shell that
  # starts mid-switch then reads the old token or the new one, never half of
  # either, and a failure leaves the old one in place. mktemp makes the file
  # 0600; chmod says so anyway, whatever the umask.
  local dest="$1" tmp
  tmp="$(mktemp "$(dirname "$dest")/.ccfleet-token.XXXXXX")" || return 1
  if ! { cat > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$dest"; }; then
    rm -f "$tmp"
    return 1
  fi
}

saved_names() {
  # One per line. Anything in the directory that is not a valid name is not
  # one of ours, and is neither listed nor switched to.
  local f name
  [ -d "$TOKENS_DIR" ] || return 0
  for f in "$TOKENS_DIR"/*; do
    [ -f "$f" ] || continue
    name="${f##*/}"
    if valid_name "$name"; then
      printf '%s\n' "$name"
    fi
  done
}

names_line() {
  local out="" name
  for name in $(saved_names); do
    out="${out:+$out, }$name"
  done
  printf '%s' "$out"
}

active_name() {
  # The saved token that is in use: the one whose content is the active file's.
  # A switch copies, so equality is the whole test.
  local name
  [ -f "$TOKEN_FILE" ] || return 1
  for name in $(saved_names); do
    if cmp -s "$TOKENS_DIR/$name" "$TOKEN_FILE"; then
      printf '%s' "$name"
      return 0
    fi
  done
  return 1
}

adopt_legacy() {
  # A device connected before tokens had names has one token and no saved
  # ones. Keep that token as "default" the first time this version runs, so a
  # switch away from it cannot be the way it gets lost.
  [ -f "$TOKEN_FILE" ] || return 0
  [ -z "$(saved_names)" ] || return 0
  private_tokens_dir
  atomic_put "$TOKENS_DIR/$DEFAULT_NAME" < "$TOKEN_FILE" \
    || die "could not keep the existing token as '$DEFAULT_NAME'"
}

auth_status_of() {
  # Claude Code's own verdict on one token. stdin is closed so that a caller
  # reading a list on stdin keeps the rest of its list.
  CLAUDE_CODE_OAUTH_TOKEN="$1" claude auth status </dev/null 2>&1 || true
}

accepted() {
  case "$1" in
    *'"loggedIn": true'*) return 0 ;;
  esac
  return 1
}

json_field() {
  # One string field from the CLI's JSON, cut down to characters that are safe
  # to print: it came from a program and it is going to a terminal.
  printf '%s\n' "$2" | sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" \
    | tr -cd 'A-Za-z0-9_.\n-' | sed -n '1p' | cut -c1-40
}

wipe() {
  if command -v shred >/dev/null 2>&1; then
    shred -u "$1" 2>/dev/null || rm -f "$1"
  else
    rm -f "$1"
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
  local token="$1" name="$2"
  case "$token" in
    sk-ant-oat01-*) ;;
    *) die "that does not look like a setup-token credential (expected sk-ant-oat01-...)" ;;
  esac
  command -v claude >/dev/null 2>&1 || die "Claude Code is not installed on this device"

  # Verify BEFORE writing anything, so a bad token never gets persisted.
  note "checking the token..."
  accepted "$(auth_status_of "$token")" \
    || die "that token was refused. Ask for a fresh one: claude setup-token"

  # Everything that can refuse must refuse before the credential lands on disk,
  # or a failure here leaves a token with no rc line to use it.
  local rc; rc="$(shell_rc)"
  check_rc_strippable "$rc"

  # rc first, token second. The rc line is guarded by [ -r ... ], so an rc
  # pointing at a token that does not exist yet is inert rather than broken —
  # whereas writing the token first and then failing on the rc would clobber a
  # previously working credential and leave nothing configured to use it.
  write_block "$rc"

  # A token from before names is kept, as "default", before anything replaces
  # it. Only here, after the check: a refused token leaves the device as it was.
  adopt_legacy
  mkdir -p "$(dirname "$TOKEN_FILE")"
  private_tokens_dir
  printf '%s\n' "$token" | atomic_put "$TOKENS_DIR/$name" || die "could not save the token"
  atomic_put "$TOKEN_FILE" < "$TOKENS_DIR/$name" || die "could not put the token in use"

  # Only now, with a token that has been accepted and written: nothing lands on
  # this machine until the credential has proved itself.
  install_self

  note "token saved as '$name' and in use: $TOKEN_FILE (0600)"
  note "$rc now exports it for new shells"
  if [ "$(saved_names | wc -l | tr -d " ")" -gt 1 ]; then
    note "switch accounts with: ccfleet-connect --use NAME   (see them: --list)"
  fi
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
  adopt_legacy
  if [ ! -r "$TOKEN_FILE" ]; then
    note "not connected (no $TOKEN_FILE)"
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      note "but CLAUDE_CODE_OAUTH_TOKEN is set in this shell"
    fi
    if [ -n "$(saved_names)" ]; then
      note "saved tokens: $(names_line). Pick one: ccfleet-connect --use NAME"
    fi
    return 0
  fi
  note "token file : $TOKEN_FILE"
  local name
  name="$(active_name || true)"
  if [ -n "$name" ]; then
    note "token name : $name"
  else
    note "token name : none of the saved ones (see --list)"
  fi
  # Never interpolate the token itself. ${VAR:-default} expands to the VALUE
  # when set, so the obvious one-liner here printed the whole credential.
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    note "in shell   : no, open a new terminal"
  elif [ "$CLAUDE_CODE_OAUTH_TOKEN" = "$(cat "$TOKEN_FILE")" ]; then
    note "in shell   : yes"
  else
    # Switching changes what new shells read, not what this one already has.
    note "in shell   : a different token, from before a switch; open a new terminal"
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
  # Undo it all. With tokens saved under names, "all" has to include them:
  # an undo that left credentials behind in a directory would not be one.
  local rc; rc="$(shell_rc)"
  strip_block "$rc"
  if [ -f "$TOKEN_FILE" ]; then
    wipe "$TOKEN_FILE"
    note "token removed"
  fi
  local f removed=0
  if [ -d "$TOKENS_DIR" ]; then
    # The dot pattern catches a temp file an interrupted write left behind,
    # which holds a token like any other.
    for f in "$TOKENS_DIR"/* "$TOKENS_DIR"/.[!.]*; do
      [ -f "$f" ] || continue
      wipe "$f"
      removed=$((removed + 1))
    done
    rmdir "$TOKENS_DIR" 2>/dev/null || true
  fi
  for f in "$(dirname "$TOKEN_FILE")"/.ccfleet-token.*; do
    [ -f "$f" ] || continue
    wipe "$f"
  done
  if [ "$removed" -gt 0 ]; then
    note "$removed saved token(s) removed"
  fi
  note "$rc cleaned"
  note "this shell still has it until you close it: unset CLAUDE_CODE_OAUTH_TOKEN"
}

cmd_remove_one() {
  local name="$1" was_in_use=no
  need_name "$name"
  adopt_legacy
  [ -f "$TOKENS_DIR/$name" ] || die "no token saved as '$name' (see --list)"
  if [ -f "$TOKEN_FILE" ] && cmp -s "$TOKENS_DIR/$name" "$TOKEN_FILE"; then
    was_in_use=yes
  fi
  wipe "$TOKENS_DIR/$name"
  note "removed the token saved as '$name'"
  # In use, and not also saved under another name: then it must not stay in
  # use, or removing it would have changed nothing a shell can see.
  if [ "$was_in_use" = yes ] && ! active_name >/dev/null; then
    wipe "$TOKEN_FILE"
    note "it was the one in use, so no token is in use now"
    if [ -n "$(saved_names)" ]; then
      note "pick another: ccfleet-connect --use NAME   (saved: $(names_line))"
    fi
    note "this shell still has it until you close it: unset CLAUDE_CODE_OAUTH_TOKEN"
  fi
}

cmd_use() {
  local name="$1" current rc
  need_name "$name"
  adopt_legacy
  [ -f "$TOKENS_DIR/$name" ] || die "no token saved as '$name' (see --list)"
  command -v claude >/dev/null 2>&1 || die "Claude Code is not installed on this device"
  current="$(active_name || true)"

  # The same rule as connecting: the CLI judges the token before it is put in
  # use, so a revoked one leaves the device on the account it was on.
  note "checking the token saved as '$name'..."
  accepted "$(auth_status_of "$(cat "$TOKENS_DIR/$name")")" \
    || die "the token saved as '$name' was refused; it may have been revoked. \
Still using '${current:-nothing}'."

  rc="$(shell_rc)"
  ensure_block "$rc"
  mkdir -p "$(dirname "$TOKEN_FILE")"
  atomic_put "$TOKEN_FILE" < "$TOKENS_DIR/$name" || die "could not switch to '$name'"
  note "now using '$name' in new shells"
  note "terminals already open keep the account they started with"
  hand_over_shell
}

cmd_list() {
  adopt_legacy
  local names active name mark state out plan checked=no
  names="$(saved_names)"
  if [ -z "$names" ]; then
    note "no saved tokens. Add one: ccfleet-connect --add NAME"
    return 0
  fi
  active="$(active_name || true)"
  if command -v claude >/dev/null 2>&1; then
    checked=yes
  fi
  while IFS= read -r name; do
    mark=" "
    if [ "$name" = "$active" ]; then
      mark="*"
    fi
    state=""
    if [ "$checked" = yes ]; then
      out="$(auth_status_of "$(cat "$TOKENS_DIR/$name")")"
      if accepted "$out"; then
        plan="$(json_field subscriptionType "$out")"
        state="working${plan:+, $plan plan}"
      else
        state="REFUSED, it may have been revoked"
      fi
    fi
    # Names and verdicts only. Never the token, in any branch.
    printf '  %s %-16s %s\n' "$mark" "$name" "$state"
  done <<EOF
$names
EOF
  if [ -z "$active" ]; then
    note "none of these is in use: ccfleet-connect --use NAME"
  fi
  if [ "$checked" = no ]; then
    note "Claude Code is not installed here, so the tokens were not checked"
  fi
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
    --list|-l) cmd_list ;;
    --use)
      [ $# -eq 2 ] || die "usage: ccfleet-connect --use NAME"
      cmd_use "$2" ;;
    --add)
      # The name is checked before the token is asked for, and a token is
      # never taken from this command line: it reads from stdin or the prompt.
      need_name "${2:-}"
      if [ $# -gt 3 ] || { [ $# -eq 3 ] && [ "$3" != "--stdin" ]; }; then
        die "usage: ccfleet-connect --add NAME [--stdin]   (the token is asked for, not given here)"
      fi
      cmd_connect "$(read_token)" "$2" ;;
    --remove|-r)
      [ $# -le 2 ] || die "usage: ccfleet-connect --remove [NAME]"
      if [ -n "${2:-}" ]; then
        cmd_remove_one "$2"
      else
        cmd_remove
      fi ;;
    -h|--help)
      # To the first blank line rather than a counted range: the header grows
      # and a fixed number quietly starts cutting the end off the help.
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' ;;
    "")
      # No argument: read the token without it ever reaching a command line.
      cmd_connect "$(read_token)" "$DEFAULT_NAME" ;;
    --stdin) cmd_connect "$(read_token)" "$DEFAULT_NAME" ;;
    -*) die "unknown option: $1" ;;
    *)  cmd_connect "$1" "$DEFAULT_NAME" ;;
  esac
}

main "$@"
