#!/usr/bin/env bash
# Install the signed CCFleet native terminal, and seed the component cache so an
# installed `ccfleet` can run it without asking the paired slot for the component.
#
#   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/tools/native_agy/install.sh | bash
#
# macOS only. Verifies the release signature against the pinned CCFleet key before
# anything is written.

set -euo pipefail

VERSION="${CCFLEET_NATIVE_VERSION:-native-agy-terminal-v0.1.0-preview.2}"
BASE="${CCFLEET_NATIVE_BASE:-https://github.com/cdcupt/ccfleet/releases/download/$VERSION}"
DEST="${CCFLEET_NATIVE_DIR:-$HOME/.local/share/ccfleet-native-terminal}"
CLIENT_HOME="${CCFLEET_HOME:-$HOME/.config/ccfleet}"
BIN="${CCFLEET_BIN_DIR:-$HOME/.local/bin}"
ZIP="ccfleet-native-terminal-macos.zip"
KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJAfp/o543ltpXhS4vumv2mParGmmLV8mQ42xW9usGhn"

say() { printf 'ccfleet: %s\n' "$1"; }
die() { printf 'ccfleet: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this preview is macOS only"
for tool in curl ssh-keygen ditto shasum sed; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

say "downloading $VERSION"
for asset in "$ZIP" SHA256SUMS SHA256SUMS.sig; do
    curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --max-time 120 \
        -o "$stage/$asset" "$BASE/$asset" || die "could not download $asset"
done

printf 'ccfleet namespaces="ccfleet-release" %s\n' "$KEY" > "$stage/signers"
ssh-keygen -Y verify -f "$stage/signers" -I ccfleet -n ccfleet-release \
    -s "$stage/SHA256SUMS.sig" < "$stage/SHA256SUMS" >/dev/null 2>&1 \
    || die "the release signature did not verify; nothing was installed"
( cd "$stage" && shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 ) \
    || die "the archive did not match its checksum; nothing was installed"

mkdir -p "$stage/unpack"
ditto -x -k "$stage/$ZIP" "$stage/unpack" || die "could not unpack the archive"
[ -f "$stage/unpack/ccfleet-native-terminal/laptop/ccfleet" ] \
    || die "the archive did not have the expected layout"

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
mv "$stage/unpack/ccfleet-native-terminal" "$DEST"
chmod +x "$DEST/laptop/ccfleet"

digest="$(sed -n 's/^NATIVE_TERMINAL_HASH = "\([0-9a-f]\{64\}\)".*/\1/p' "$DEST/laptop/ccfleet" | head -1)"
[ -n "$digest" ] || die "the launcher did not pin a terminal component"
component="$DEST/ccfleet_agent/gemini_native_terminal.py"
[ "$(shasum -a 256 "$component" | cut -d' ' -f1)" = "$digest" ] \
    || die "the bundled component did not match the pinned digest"

# An installed client looks for components in this cache first, keyed by digest and
# re-verified on every read, so seeding it is the same trust path as a slot download.
cache="$CLIENT_HOME/antigravity-workspace/editor-components"
if [ ! -d "$CLIENT_HOME" ]; then
    mkdir -p "$CLIENT_HOME"
    chmod 700 "$CLIENT_HOME"
fi
for dir in "$CLIENT_HOME/antigravity-workspace" "$cache"; do
    [ -d "$dir" ] || mkdir -m 700 "$dir"
done
cp "$component" "$cache/$digest.py"
chmod 400 "$cache/$digest.py"
[ "$(shasum -a 256 "$cache/$digest.py" | cut -d' ' -f1)" = "$digest" ] \
    || die "the cached component did not match the pinned digest"

mkdir -p "$BIN"
ln -sf "$DEST/laptop/ccfleet" "$BIN/ccfleet-native-terminal"

installed=""
if [ -f "$BIN/ccfleet" ]; then
    installed="$(sed -n 's/^CLIENT_VERSION = "\([^"]*\)".*/\1/p' "$BIN/ccfleet" | head -1)"
fi

printf '\n'
say "installed $VERSION to $DEST"
say "seeded the terminal component for client $installed${installed:- (none installed)}"
printf '\nNext:\n'
if [ -z "$installed" ]; then
    printf '  %s pair gemini\n' "$BIN/ccfleet-native-terminal"
    printf '  %s gemini --project ~/path/to/repo\n' "$BIN/ccfleet-native-terminal"
else
    case "$installed" in
        0.2.29*) : ;;
        *) printf '  ccfleet update      # %s predates the native terminal default\n' "$installed" ;;
    esac
    printf '  ccfleet gemini --project ~/path/to/repo\n'
fi
printf '\nPair once if you have not: paste a setup code from https://ccfleet.io/account/gemini\n'
printf 'Use the ZIP install above only on your own machines: Google treats consumer-plan\n'
printf 'wrappers as a Terms violation. See %s/README.html\n' "$DEST"
