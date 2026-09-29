#!/usr/bin/env bash
# Install ccfleet and its digest-pinned helpers. Setup also ensures the original
# local Claude Code CLI is available; pairing remains owned by ccfleet.

set -euo pipefail

DEST="${CCFLEET_INSTALL_DIR:-$HOME/.local/bin}"
URL="${CCFLEET_INSTALL_URL:-https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/ccfleet}"
MIGRATE=no
SETUP=no
APPROVE_MIGRATION=no
DEVICE_NAME=computer
SLOT=""

usage() {
  cat <<'USAGE'
Install the CC Fleet terminal client.

  install.sh
  install.sh --migrate [--name "My computer"]
  install.sh --setup [--name "My computer"] [--slot SLOT] [--yes]

--migrate installs and pairs the new client first, then removes an installed
legacy ccfleet-connect token setup. Have a fresh pairing code from /account.
--setup installs original Claude Code if missing, reuses a working pairing or
pairs if needed, checks slot inference, then retires the legacy setup and
configures PATH. It never uploads a project or starts a Claude session.
--yes explicitly approves ending this computer's old live-folder sessions.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --migrate) MIGRATE=yes; shift ;;
    --setup) SETUP=yes; shift ;;
    --yes) APPROVE_MIGRATION=yes; shift ;;
    --slot)
      [ $# -ge 2 ] && [ -n "$2" ] \
        || { printf 'error: --slot needs a slot name or id\n' >&2; exit 2; }
      SLOT="$2"; shift 2 ;;
    --name)
      [ $# -ge 2 ] || { printf 'error: --name needs a device label\n' >&2; exit 2; }
      DEVICE_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'error: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[ "$MIGRATE" != yes ] || [ "$SETUP" != yes ] \
  || { printf 'error: use either --setup or --migrate, not both\n' >&2; exit 2; }
[ "$MIGRATE" = yes ] || [ "$SETUP" = yes ] || [ "$DEVICE_NAME" = computer ] \
  || { printf 'error: --name is used with --setup or --migrate\n' >&2; exit 2; }
[ -z "$SLOT" ] || [ "$SETUP" = yes ] \
  || { printf 'error: --slot is used with --setup\n' >&2; exit 2; }
[ "$APPROVE_MIGRATION" != yes ] || [ "$SETUP" = yes ] \
  || { printf 'error: --yes is used only with --setup\n' >&2; exit 2; }

command -v python3 >/dev/null 2>&1 || { printf 'ccfleet needs Python 3.9 or newer\n' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "ccfleet needs Python 3.9 or newer")'
DEST="$(python3 - "$DEST" <<'PY'
import os
import sys

path = os.path.abspath(sys.argv[1])
if ":" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
    raise SystemExit("ccfleet install path must not contain colons, newlines or control characters")
print(path)
PY
)"
command -v ssh >/dev/null 2>&1 || { printf 'ccfleet needs OpenSSH\n' >&2; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || { printf 'ccfleet needs ssh-keygen\n' >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { printf 'ccfleet needs curl\n' >&2; exit 1; }

mkdir -p "$DEST"
CLIENT_TMP="$(mktemp "$DEST/.ccfleet.XXXXXX")"
HELPER_TMP=""
LIVE_FILES_TMP=""
LIVE_CLIENT_TMP=""
INFERENCE_CLIENT_TMP=""
trap 'rm -f "$CLIENT_TMP" "$HELPER_TMP" "$LIVE_FILES_TMP" "$LIVE_CLIENT_TMP" "$INFERENCE_CLIENT_TMP"' EXIT
curl -fsSL "$URL" -o "$CLIENT_TMP"
# Read the helper digest as data. Never execute the downloaded client to discover
# its dependencies, and reject missing, computed, or ambiguous digest values.
DIGESTS="$(python3 - "$CLIENT_TMP" <<'PY'
import ast
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1]).read_bytes()
tree = ast.parse(source, filename=sys.argv[1])
compile(tree, sys.argv[1], "exec")
def digest(name, required=False):
    assignments = [
        node for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if not assignments and not required:
        return ""
    if len(assignments) != 1:
        raise SystemExit("ccfleet client must declare one " + name + " digest")
    assignment = assignments[0]
    value = assignment.value
    if (
        not isinstance(assignment, ast.Assign)
        or len(assignment.targets) != 1
        or not isinstance(value, ast.Constant)
        or not isinstance(value.value, str)
        or not re.fullmatch(r"[0-9a-f]{64}", value.value)
    ):
        raise SystemExit("ccfleet " + name + " must be a literal SHA256 digest")
    return value.value

project, live_files, live_client = (digest("PROJECT_FILES_SHA256", required=True),
                                   digest("LIVE_FILES_SHA256"), digest("LIVE_CLIENT_SHA256"))
if bool(live_files) != bool(live_client):
    raise SystemExit("ccfleet must declare both LIVE_FILES_SHA256 and LIVE_CLIENT_SHA256")
print("|".join((project, live_files, live_client, digest("INFERENCE_CLIENT_SHA256"))))
PY
)"
IFS='|' read -r PROJECT_FILES_SHA256 LIVE_FILES_SHA256 LIVE_CLIENT_SHA256 \
  INFERENCE_CLIENT_SHA256 <<< "$DIGESTS"
case "$URL" in
  https://raw.githubusercontent.com/*/laptop/ccfleet)
    HELPER_BASE="${URL%/laptop/ccfleet}/ccfleet_agent" ;;
  *) HELPER_BASE="https://raw.githubusercontent.com/cdcupt/ccfleet/main/ccfleet_agent" ;;
esac
HELPER_URL="${CCFLEET_PROJECT_FILES_URL:-$HELPER_BASE/project_files.py}"
HELPER_TMP="$(mktemp "$DEST/.ccfleet-project-files.XXXXXX")"
curl -fsSL "$HELPER_URL" -o "$HELPER_TMP"
HELPER_ARGS=(project-files "$HELPER_TMP" "$PROJECT_FILES_SHA256")
if [ -n "$LIVE_FILES_SHA256" ]; then
  LIVE_FILES_TMP="$(mktemp "$DEST/.ccfleet-live-files.XXXXXX")"
  LIVE_CLIENT_TMP="$(mktemp "$DEST/.ccfleet-live-client.XXXXXX")"
  curl -fsSL "${CCFLEET_LIVE_FILES_URL:-$HELPER_BASE/live_files.py}" -o "$LIVE_FILES_TMP"
  curl -fsSL "${CCFLEET_LIVE_CLIENT_URL:-$HELPER_BASE/live_client.py}" -o "$LIVE_CLIENT_TMP"
  HELPER_ARGS+=(live-files "$LIVE_FILES_TMP" "$LIVE_FILES_SHA256"
               live-client "$LIVE_CLIENT_TMP" "$LIVE_CLIENT_SHA256")
fi
if [ -n "$INFERENCE_CLIENT_SHA256" ]; then
  INFERENCE_CLIENT_TMP="$(mktemp "$DEST/.ccfleet-inference-client.XXXXXX")"
  curl -fsSL "${CCFLEET_INFERENCE_CLIENT_URL:-$HELPER_BASE/inference_client.py}" \
    -o "$INFERENCE_CLIENT_TMP"
  HELPER_ARGS+=(inference-client "$INFERENCE_CLIENT_TMP" "$INFERENCE_CLIENT_SHA256")
fi
python3 - "$CLIENT_TMP" "$DEST" "${HELPER_ARGS[@]}" <<'PY'
import hashlib
import os
import pathlib
import sys

client, destination = map(pathlib.Path, sys.argv[1:3])
verified = []
for offset in range(3, len(sys.argv), 3):
    name, path, digest = sys.argv[offset:offset + 3]
    helper = pathlib.Path(path)
    source = helper.read_bytes()
    if hashlib.sha256(source).hexdigest() != digest:
        raise SystemExit("ccfleet " + name + " helper checksum mismatch; existing client left unchanged")
    compile(source, str(helper), "exec")
    verified.append((helper, destination / ("ccfleet-" + name + "-" + digest + ".py")))
# Versioned helpers keep an interrupted update compatible with the old client.
# Validate EVERY helper before installing any of them or replacing the client.
for helper, target in verified:
    helper.chmod(0o644)
    os.replace(helper, target)
client.chmod(0o755)
os.replace(client, destination / "ccfleet")
PY
trap - EXIT

printf 'Installed ccfleet to %s\n' "$DEST/ccfleet"
if [ "$SETUP" != yes ]; then
  case ":$PATH:" in
    *":$DEST:"*) ;;
    *) printf 'Add %s to PATH, then run: ccfleet login\n' "$DEST" ;;
  esac
fi

[ "$MIGRATE" = yes ] || [ "$SETUP" = yes ] || exit 0

if [ "$SETUP" = yes ]; then
  NATIVE_CLAUDE="$(command -v claude || true)"
  if [ -z "$NATIVE_CLAUDE" ] && [ -x "$HOME/.local/bin/claude" ]; then
    NATIVE_CLAUDE="$HOME/.local/bin/claude"
  fi
  if [ -z "$NATIVE_CLAUDE" ]; then
    printf '\nInstalling the original Claude Code CLI for this user (no sudo).\n'
    VENDOR_INSTALLER="$(mktemp "$DEST/.ccfleet-claude-installer.XXXXXX")"
    trap 'rm -f "$VENDOR_INSTALLER"' EXIT
    if ! curl --proto '=https' --proto-redir '=https' --tlsv1.2 --max-time 120 \
        -fsSL https://claude.ai/install.sh -o "$VENDOR_INSTALLER"; then
      printf 'Claude Code download failed; pairing and legacy setup were not changed.\n' >&2
      exit 1
    fi
    bash -n "$VENDOR_INSTALLER" \
      || { printf 'Claude installer validation failed; no pairing was started.\n' >&2; exit 1; }
    if ! python3 - "$VENDOR_INSTALLER" <<'PY'
import contextlib
import os
import signal
import subprocess
import sys

process = subprocess.Popen(["bash", sys.argv[1]], stdin=subprocess.DEVNULL, start_new_session=True)
try:
    raise SystemExit(process.wait(timeout=180))
except subprocess.TimeoutExpired:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            process.wait(timeout=3)
            break
        except subprocess.TimeoutExpired:
            pass
    raise SystemExit("Claude Code installation timed out")
PY
    then
      printf 'Claude Code installation failed; pairing and legacy setup were not changed.\n' >&2
      exit 1
    fi
    rm -f "$VENDOR_INSTALLER"
    trap - EXIT
    NATIVE_CLAUDE="$HOME/.local/bin/claude"
  fi
  if ! python3 - "$NATIVE_CLAUDE" <<'PY'
import subprocess
import sys

try:
    result = subprocess.run([sys.argv[1], "--version"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    raise SystemExit(result.returncode)
except (OSError, subprocess.SubprocessError):
    raise SystemExit(1)
PY
  then
    printf 'Claude Code is unavailable or its version check failed; no pairing or legacy cleanup was started.\n' >&2
    exit 1
  fi
  PATH="$(dirname "$NATIVE_CLAUDE"):$PATH"
  export PATH
  SETUP_ARGS=(setup --name "$DEVICE_NAME")
  [ -z "$SLOT" ] || SETUP_ARGS+=(--slot "$SLOT")
  [ "$APPROVE_MIGRATION" != yes ] || SETUP_ARGS+=(--yes)
  if ! "$DEST/ccfleet" "${SETUP_ARGS[@]}"; then
    printf '\nSetup stopped: slot readiness failed; legacy setup and PATH were not changed.\n' >&2
    exit 1
  fi
else
  printf '\nTransitioning this computer from the old ccfleet-connect setup.\n'
  printf 'Have the fresh pairing code from your slot page ready.\n\n'
  if ! "$DEST/ccfleet" login --name "$DEVICE_NAME"; then
    printf '\nTransition stopped: new pairing failed; the old setup was not removed.\n' >&2
    exit 1
  fi
fi

OLD_CONNECT=""
if [ -x "$HOME/.local/bin/ccfleet-connect" ]; then
  OLD_CONNECT="$HOME/.local/bin/ccfleet-connect"
elif command -v ccfleet-connect >/dev/null 2>&1; then
  OLD_CONNECT="$(command -v ccfleet-connect)"
fi

check_legacy_cleanup() {
  python3 - "$1" "$OLD_CONNECT" <<'PY'
import json
import os
import pathlib
import secrets
import stat
import sys

phase, old_command = sys.argv[1:]
home = pathlib.Path(os.path.abspath(os.environ["HOME"]))
token = home / ".config/ccfleet/token"
configured = pathlib.Path(os.path.abspath(os.environ.get("CCFLEET_TOKEN_FILE") or token))

def stop(message):
    raise SystemExit("CC Fleet is ready, but legacy cleanup is incomplete: " + message +
                     ". Existing pairing was kept; review the legacy setup manually.")

if configured != token:
    stop("custom token locations are not removed automatically")
shell = pathlib.Path(os.environ.get("SHELL", "")).name
rc = {"zsh": pathlib.Path(os.environ.get("ZDOTDIR") or home) / ".zshrc",
      "bash": home / ".bashrc", "fish": home / ".config/fish/config.fish"}.get(shell)
if rc is None and old_command:
    stop("the legacy helper does not safely support this login shell")

def safe(path, directory=False):
    path = pathlib.Path(os.path.abspath(path))
    try:
        relative = path.relative_to(home)
    except ValueError:
        stop("a legacy path is outside your home directory")
    current = home
    for part in relative.parts[:-1]:
        current /= part
        if not os.path.lexists(current):
            return
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            stop("a legacy directory is unsafe or a symlink")
    if not os.path.lexists(path):
        return
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        stop("a legacy path is a symlink, nonregular or not owned by you")
    if not directory and info.st_nlink != 1:
        stop("a legacy file is hardlinked")

def preserve_pairing(removals):
    # Inspect only local profile metadata, never key/token contents. A manually
    # moved key or host pin may occupy a historical token filename.
    fleet_home = pathlib.Path(os.path.abspath(os.environ.get("CCFLEET_HOME") or token.parent))
    config = fleet_home / "config.json"
    protected = [config]
    trees = [fleet_home / name for name in ("local", "project-backups", "local-history")]
    if fleet_home != token.parent:
        trees.append(fleet_home)
    limit = 16 * 1024 * 1024
    if os.path.lexists(config):
        info = config.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > limit):
            stop("pairing configuration cannot be safely checked")
        fd = os.open(config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            actual = os.fstat(stream.fileno())
            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                stop("pairing configuration changed during inspection")
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            stop("pairing configuration exceeds the safe inspection size")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            stop("pairing configuration is invalid")
        if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
            stop("pairing configuration is invalid")
        for device in data["devices"].values():
            if not isinstance(device, dict):
                stop("a saved device configuration is invalid")
            for field in ("key", "known_hosts"):
                value = device.get(field)
                if not isinstance(value, str) or not value or "\0" in value:
                    stop("a saved device path is invalid")
                protected.append(pathlib.Path(os.path.abspath(value)))
    # realpath detects aliases through a saved key's parent directories or a
    # key symlink. Candidate token paths themselves have already passed safe().
    exact = {os.path.realpath(path) for path in protected}
    roots = [pathlib.Path(os.path.realpath(path)) for path in trees]
    for candidate in removals:
        path = pathlib.Path(os.path.realpath(candidate))
        if str(path) in exact or any(path == root or root in path.parents for root in roots):
            stop("a legacy removal path overlaps pairing data or retained project history")

try:
    home_info = home.lstat()
    if (not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.getuid()
            or home_info.st_mode & 0o022):
        stop("the home directory is unsafe, a symlink or not owned by you")
    safe(token.parent, directory=True)
    artifacts = [token, token.with_name("token.off")]
    if token.parent.is_dir():
        artifacts.extend(token.parent.glob(".ccfleet-token.*"))
    saved = token.parent / "tokens"
    safe(saved, directory=True)
    if saved.is_dir():
        artifacts.extend(saved.iterdir())
    for path in artifacts:
        safe(path)
    startup = [rc] if rc is not None else []
    if shell == "bash":
        candidates = [home / name for name in (".bash_profile", ".bash_login", ".profile")]
        startup.append(next((path for path in candidates if os.path.lexists(path)), candidates[0]))
    elif shell == "fish":
        startup.append(pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
                       / "fish/config.fish")
    preserve_pairing([*artifacts, saved, *startup])
    remaining = any(os.path.lexists(path) for path in artifacts)
    if rc is not None:
        safe(rc)
        temporary = pathlib.Path(str(rc) + ".ccfleet-tmp")
        if os.path.lexists(temporary):
            stop("the legacy startup temporary path already exists")
        raw = rc.read_bytes() if rc.exists() else b""
        remaining = remaining or b"# >>> ccfleet connect >>>" in raw
        if phase == "before" and old_command and rc.exists():
            backup = rc.with_name(rc.name + ".ccfleet-legacy-backup-" + secrets.token_hex(8))
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            print(f"Legacy startup backup: {backup}")
    if remaining and (phase == "after" or not old_command):
        stop("recognizable legacy artifacts remain" if old_command else
             "legacy artifacts exist but ccfleet-connect is not installed")
except OSError as exc:
    stop("legacy paths could not be checked (" + exc.__class__.__name__ + ")")
PY
}

if [ "$SETUP" = yes ]; then
  check_legacy_cleanup before
fi

if [ -n "$OLD_CONNECT" ]; then
  if ! "$OLD_CONNECT" --remove; then
    printf '\nCC Fleet is connected, but the old ccfleet-connect cleanup failed.\n' >&2
    printf 'Run manually: %s --remove\n' "$OLD_CONNECT" >&2
    exit 1
  fi
else
  printf '\nNo installed ccfleet-connect command was found; no old local token was removed.\n'
fi

if [ "$SETUP" = yes ]; then
  check_legacy_cleanup after
  if python3 - "$DEST" <<'PY'
import fcntl
import os
import pathlib
import secrets
import shlex
import stat
import sys

destination = sys.argv[1]
shell = pathlib.Path(os.environ.get("SHELL", "")).name
home = pathlib.Path(os.path.abspath(os.environ["HOME"]))
begin = b"# >>> CC Fleet PATH >>>\n"
end = b"# <<< CC Fleet PATH <<<\n"

def stop(message):
    raise SystemExit("PATH not configured: " + message +
                     ". Keep your existing startup files; add the install directory manually.")

def under_home(raw):
    path = pathlib.Path(os.path.abspath(raw))
    try:
        path.relative_to(home)
    except ValueError:
        stop("startup location is outside your home directory")
    if any(ord(char) < 32 or ord(char) == 127 for char in str(path)):
        stop("startup location contains control characters")
    return path

if shell == "zsh":
    folder = under_home(os.environ.get("ZDOTDIR") or home)
    targets = [folder / ".zshrc"]
elif shell == "bash":
    candidates = [home / name for name in (".bash_profile", ".bash_login", ".profile")]
    login = next((path for path in candidates if os.path.lexists(path)), candidates[0])
    targets = [login, home / ".bashrc"]
elif shell == "fish":
    folder = under_home(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    targets = [folder / "fish" / "config.fish"]
else:
    print("PATH was not changed: this login shell is not supported automatically.", file=sys.stderr)
    raise SystemExit(1)

if shell == "fish":
    quoted = "'" + destination.replace("\\", "\\\\").replace("'", "\\'") + "'"
    command = f'if not test "$PATH[1]" = {quoted}\n    set -gx PATH {quoted} $PATH\nend\n'
else:
    quoted = shlex.quote(destination)
    command = f'case "$PATH" in\n  {quoted}|{quoted}:*) ;;\n  *) export PATH={quoted}:"$PATH" ;;\nesac\n'
block = begin + command.encode("utf-8") + end
opened = []
plans = []
try:
    # Validate every target before appending to any startup file. Do not source it.
    for target in targets:
        under_home(target)
        parents = [home]
        current = home
        for part in target.parent.relative_to(home).parts:
            current = current / part
            parents.append(current)
        for parent in parents:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                parent.mkdir(mode=0o700)
                info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                stop("startup directory is a symlink or is not owned by you")
            if info.st_mode & 0o022:
                stop("startup directory is writable by other users")
        try:
            info = target.lstat()
        except FileNotFoundError:
            plans.append((target, None, b""))
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            stop("startup file is a symlink, hardlink, nonregular file or is not owned by you")
        fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            stop("startup file changed during inspection")
        raw = os.read(fd, 4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            stop("startup file exceeds the safe editing size")
        if begin.rstrip(b"\n") in raw or end.rstrip(b"\n") in raw:
            if raw.count(begin) == 1 and raw.count(end) == 1 and block in raw:
                print(f"PATH already configured in {target}")
                continue
            stop("an existing CC Fleet PATH block differs; review it manually")
        plans.append((target, fd, raw))
    for target, fd, raw in plans:
        if fd is None:
            fd = os.open(target, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600)
            opened.append(fd)
        else:
            # Append in place, retaining mode and every existing byte. Save the
            # original privately before any append; never replace a user symlink.
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, len(raw) + 1) != raw:
                stop("startup file changed before editing")
            backup = target.with_name(target.name + ".ccfleet-backup-" + secrets.token_hex(8))
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            print(f"Startup backup: {backup}")
        addition = (b"\n" if raw and not raw.endswith(b"\n") else b"") + block
        view = memoryview(addition)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("startup append did not complete")
            view = view[written:]
        os.fsync(fd)
        current, written = target.lstat(), os.fstat(fd)
        if (current.st_dev, current.st_ino) != (written.st_dev, written.st_ino):
            stop("startup file was replaced during editing; its replacement was preserved")
        if os.pread(fd, len(block), max(0, written.st_size - len(block))) != block:
            stop("startup file changed during editing; review the retained backup")
        print(f"PATH configured in {target}")
except OSError as exc:
    stop("startup edit failed (" + exc.__class__.__name__ + ")")
finally:
    for fd in opened:
        os.close(fd)
PY
  then
    :
  else
    printf 'Client is ready at %s, but PATH setup is incomplete.\n' "$DEST/ccfleet" >&2
    printf 'Add the install directory to your shell PATH manually, or invoke that absolute client path.\n' >&2
    printf 'Then open a new terminal in your project and run the client with: local\n' >&2
    exit 1
  fi
  printf '\nSetup complete. Open a new terminal, then:\n  cd /path/to/your-project\n  ccfleet local\n'
  printf 'No project files were uploaded and no Claude session was started.\n'
  printf 'If the old setup-token was only for CC Fleet, revoke it in your Anthropic account.\n'
  exit 0
fi

printf '\nTransition complete. Open a new terminal, then run: ccfleet\n'
printf 'If the old setup-token was only for CC Fleet, revoke it in your Anthropic account.\n'
