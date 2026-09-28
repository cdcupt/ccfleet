#!/usr/bin/env bash
# Install the ccfleet client. It is a small Python program that delegates the
# encrypted terminal to the operating system's OpenSSH client.

set -euo pipefail

DEST="${CCFLEET_INSTALL_DIR:-$HOME/.local/bin}"
URL="${CCFLEET_INSTALL_URL:-https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/ccfleet}"
MIGRATE=no
DEVICE_NAME=computer

usage() {
  cat <<'USAGE'
Install the CC Fleet terminal client.

  install.sh
  install.sh --migrate [--name "My computer"]

--migrate installs and pairs the new client first, then removes an installed
legacy ccfleet-connect token setup. Have a fresh pairing code from /account.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --migrate) MIGRATE=yes; shift ;;
    --name)
      [ $# -ge 2 ] || { printf 'error: --name needs a device label\n' >&2; exit 2; }
      DEVICE_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'error: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[ "$MIGRATE" = yes ] || [ "$DEVICE_NAME" = computer ] \
  || { printf 'error: --name is used with --migrate\n' >&2; exit 2; }

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

[ "$MIGRATE" = yes ] || exit 0

printf '\nTransitioning this computer from the old ccfleet-connect setup.\n'
printf 'Have the fresh pairing code from your slot page ready.\n\n'
if ! "$DEST/ccfleet" login --name "$DEVICE_NAME"; then
  printf '\nTransition stopped: new pairing failed; the old setup was not removed.\n' >&2
  exit 1
fi

OLD_CONNECT=""
if [ -x "$HOME/.local/bin/ccfleet-connect" ]; then
  OLD_CONNECT="$HOME/.local/bin/ccfleet-connect"
elif command -v ccfleet-connect >/dev/null 2>&1; then
  OLD_CONNECT="$(command -v ccfleet-connect)"
fi

if [ -n "$OLD_CONNECT" ]; then
  if ! "$OLD_CONNECT" --remove; then
    printf '\nNew pairing succeeded, but the old ccfleet-connect cleanup failed.\n' >&2
    printf 'Run manually: %s --remove\n' "$OLD_CONNECT" >&2
    exit 1
  fi
else
  printf '\nNo installed ccfleet-connect command was found; no old local token was removed.\n'
fi

printf '\nTransition complete. Open a new terminal, then run: ccfleet\n'
printf 'If the old setup-token was only for CC Fleet, revoke it in your Anthropic account.\n'
