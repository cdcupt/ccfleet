#!/usr/bin/env bash
# Nightly backup of Claude Code state WITHOUT credentials, optional rclone upload.
#
# Environment (all optional):
#   CLAUDE_CONFIG_DIR       what to back up             (default: ~/.claude)
#   CCFLEET_BACKUP_DIR      where archives go           (default: ~/backups/ccfleet)
#   CCFLEET_BACKUP_KEEP     archives to keep locally    (default: 14)
#   CCFLEET_BACKUP_REMOTE   rclone destination, e.g. r2:bucket/ccfleet/node-a
#   CCFLEET_BACKUP_EXTRA    extra directories, colon separated, e.g. ~/projects:~/notes
set -euo pipefail

config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
dest_dir="${CCFLEET_BACKUP_DIR:-$HOME/backups/ccfleet}"
keep="${CCFLEET_BACKUP_KEEP:-14}"
remote="${CCFLEET_BACKUP_REMOTE:-}"
extra="${CCFLEET_BACKUP_EXTRA:-}"

[[ -d "$config_dir" ]] || { echo "nothing to back up: $config_dir does not exist" >&2; exit 0; }
mkdir -p "$dest_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$dest_dir/claude-state-$stamp.tar.gz"

paths=("$config_dir")
if [[ -n "$extra" ]]; then
  IFS=':' read -r -a extra_paths <<< "$extra"
  for p in "${extra_paths[@]}"; do [[ -e "$p" ]] && paths+=("$p"); done
fi

tar --exclude='.credentials.json' --exclude='*/debug' --exclude='*/cache' \
    --exclude='*.tmp.*' -czf "$archive" -C / "${paths[@]/#\//}"

if tar -tzf "$archive" | grep -q '\.credentials\.json$'; then
  rm -f "$archive"
  echo "refusing to keep an archive that contains .credentials.json" >&2
  exit 1
fi

# rotate local copies
ls -1t "$dest_dir"/claude-state-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm -f

if [[ -n "$remote" ]] && command -v rclone >/dev/null 2>&1; then
  rclone copy "$archive" "$remote" --quiet
fi
echo "backup written: $archive"
