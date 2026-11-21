#!/bin/zsh
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "$2"
if [[ "$1" == 'Claude Code' ]]; then
  exec ccfleet local --project "$PWD"
fi
flags=()
if [[ "$1" == 'Pair Antigravity again' ]]; then flags+=(--pair); fi
if [[ "$3" != none ]]; then flags+=(--test "$3"); fi
/usr/bin/python3 -I -B "${0:A:h}/client.py" "${flags[@]}" "$PWD" || {
  read -r '?Press Return to close.'
  exit 1
}
