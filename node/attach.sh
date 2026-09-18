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
if [ -z "${TMUX:-}" ] && [ -n "${PS1:-}" ] && [ -t 1 ] && [ -z "${CCFLEET_NO_ATTACH:-}" ]; then
  case "$-" in
    *i*) command -v tmux >/dev/null 2>&1 && exec tmux new-session -A -s cc -c "$HOME/workspace" ;;
  esac
fi
