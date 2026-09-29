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
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "ccfleet needs Python 3.9 or newer")'
command -v ssh >/dev/null 2>&1 || { printf 'ccfleet needs OpenSSH\n' >&2; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || { printf 'ccfleet needs ssh-keygen\n' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf 'ccfleet needs curl\n' >&2; exit 1; }

mkdir -p "$DEST"
CLIENT_TMP="$(mktemp "$DEST/.ccfleet.XXXXXX")"
HELPER_TMP=""
trap 'rm -f "$CLIENT_TMP" "$HELPER_TMP"' EXIT
curl -fsSL "$URL" -o "$CLIENT_TMP"
# Read the helper digest as data. Never execute the downloaded client to discover
# its dependencies, and reject missing, computed, or ambiguous digest values.
PROJECT_FILES_SHA256="$(python3 - "$CLIENT_TMP" <<'PY'
import ast
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_bytes()
tree = ast.parse(source, filename=sys.argv[1])
compile(tree, sys.argv[1], "exec")
assignments = [
    node for node in tree.body
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
    and any(
        isinstance(target, ast.Name) and target.id == "PROJECT_FILES_SHA256"
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    )
]
if len(assignments) != 1:
    raise SystemExit("ccfleet client must declare one PROJECT_FILES_SHA256 digest")
assignment = assignments[0]
value = assignment.value
if (
    not isinstance(assignment, ast.Assign)
    or len(assignment.targets) != 1
    or not isinstance(value, ast.Constant)
    or not isinstance(value.value, str)
    or not re.fullmatch(r"[0-9a-f]{64}", value.value)
):
    raise SystemExit("ccfleet PROJECT_FILES_SHA256 must be a literal SHA256 digest")
print(value.value)
PY
)"
if [ -n "${CCFLEET_PROJECT_FILES_URL:-}" ]; then
  HELPER_URL="$CCFLEET_PROJECT_FILES_URL"
else
  case "$URL" in
    https://raw.githubusercontent.com/*/laptop/ccfleet)
      HELPER_URL="${URL%/laptop/ccfleet}/ccfleet_agent/project_files.py" ;;
    *) HELPER_URL="https://raw.githubusercontent.com/cdcupt/ccfleet/main/ccfleet_agent/project_files.py" ;;
  esac
fi
HELPER_TMP="$(mktemp "$DEST/.ccfleet-project-files.XXXXXX")"
curl -fsSL "$HELPER_URL" -o "$HELPER_TMP"
python3 - "$CLIENT_TMP" "$HELPER_TMP" "$DEST" "$PROJECT_FILES_SHA256" <<'PY'
import hashlib
import os
import pathlib
import sys

client, helper, destination = map(pathlib.Path, sys.argv[1:4])
digest = sys.argv[4]
source = helper.read_bytes()
if hashlib.sha256(source).hexdigest() != digest:
    raise SystemExit("ccfleet project helper checksum mismatch; existing client left unchanged")
compile(source, str(helper), "exec")
# Versioned helpers keep an interrupted update compatible with the old client.
# os.replace refuses directory destinations and atomically replaces each file.
helper.chmod(0o644)
os.replace(helper, destination / ("ccfleet-project-files-" + digest + ".py"))
client.chmod(0o755)
os.replace(client, destination / "ccfleet")
PY
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
