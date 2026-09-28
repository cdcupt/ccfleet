#!/usr/bin/env bash
# Install the ccfleet client. It is a small Python program that delegates the
# encrypted terminal to the operating system's OpenSSH client.

set -euo pipefail

DEST="${CCFLEET_INSTALL_DIR:-$HOME/.local/bin}"
URL="${CCFLEET_INSTALL_URL:-https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/ccfleet}"

command -v python3 >/dev/null 2>&1 || { printf 'ccfleet needs Python 3.9 or newer\n' >&2; exit 1; }
command -v ssh >/dev/null 2>&1 || { printf 'ccfleet needs OpenSSH\n' >&2; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || { printf 'ccfleet needs ssh-keygen\n' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf 'ccfleet needs curl\n' >&2; exit 1; }

mkdir -p "$DEST"
TMP="$DEST/.ccfleet.$$"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$URL" -o "$TMP"
python3 -m py_compile "$TMP"
chmod 755 "$TMP"
mv "$TMP" "$DEST/ccfleet"
trap - EXIT

printf 'Installed ccfleet to %s\n' "$DEST/ccfleet"
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) printf 'Add %s to PATH, then run: ccfleet login\n' "$DEST" ;;
esac
