# shellcheck shell=bash
# ^ This fragment is appended to ~/.bashrc rather than executed, so it has no
#   shebang and shellcheck cannot infer a dialect. The directive is a comment,
#   so it is inert once appended, and it keeps `shellcheck node/*.sh` clean.
# ccfleet: attach to the persistent work session
# Appended to the owner's ~/.bashrc by setup-owner.sh. Kept as its own file so the
# guards can be tested; see tests/test_attach_snippet.py.
#
# Only a genuinely interactive login attaches. Each guard earns its place:
#   TMUX empty          - never nest a session inside itself
#   PS1 set and $- has i - a real interactive shell, not a sourced script
#   -t 1                - stdout is a terminal. This is what protects scp, rsync
#                         and git over ssh: those pipe output, and a multiplexer
#                         writing into that stream corrupts the transfer.
#   CCFLEET_NO_ATTACH   - escape hatch for anyone who wants a plain shell
# new-session -A creates the session when it is missing, so a login always lands
# somewhere even if the boot-time service never ran or the session was killed.
#
# TERM: a terminal the node has no terminfo entry for makes tmux refuse to start
# with "missing or unsuitable terminal", and because this used to `exec`, that
# ended the login instead of degrading. A stock Debian knows none of ghostty,
# kitty, wezterm or alacritty, so this is the common case rather than an exotic
# one. Fall back to a description every node has.
#
# And no exec: `tmux ... && exit` keeps the old behaviour on success, where
# leaving tmux ends the ssh session, while a tmux that will not start now drops
# the owner into an ordinary shell instead of disconnecting them.
if [ -z "${TMUX:-}" ] && [ -n "${PS1:-}" ] && [ -t 1 ] && [ -z "${CCFLEET_NO_ATTACH:-}" ]; then
  case "$-" in
    *i*)
      if command -v tmux >/dev/null 2>&1; then
        if command -v infocmp >/dev/null 2>&1 \
           && ! infocmp "${TERM:-dumb}" >/dev/null 2>&1; then
          TERM=xterm-256color
          export TERM
        fi
        tmux new-session -A -s cc -c "$HOME/workspace" && exit
      fi
      ;;
  esac
fi
