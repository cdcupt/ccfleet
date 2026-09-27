#!/usr/bin/env bash
# Keep one local project and one CC Fleet slot folder synchronized with Mutagen.
#
#   ccfleet-sync start ./project slot01@203.0.113.10
#   ccfleet-sync status
#   ccfleet-sync monitor <session-name>
#   ccfleet-sync flush|pause|resume|stop <session-name>
#
# Mutagen's two-way-safe mode stops on conflicts instead of choosing a winner.
# Git metadata, dependency trees, build output and dotenv files stay local by
# default. SSH authentication is handled by the user's ordinary SSH client;
# this helper never sees or stores a private key.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
ccfleet-sync — safe two-way project sync for a private CC Fleet slot

  ccfleet-sync start LOCAL-DIR UNIX-USER@HOST [REMOTE-DIR]
  ccfleet-sync status [SESSION-NAME]
  ccfleet-sync monitor SESSION-NAME
  ccfleet-sync flush SESSION-NAME
  ccfleet-sync pause SESSION-NAME
  ccfleet-sync resume SESSION-NAME
  ccfleet-sync stop SESSION-NAME

REMOTE-DIR defaults to workspace/<local folder>. Run `status` to see conflicts.
USAGE
}

need_mutagen() {
  command -v mutagen >/dev/null 2>&1 || die \
    "Mutagen is required. Install it from https://mutagen.io/documentation/introduction/installation"
}

session_arg() {
  local action="$1"
  shift
  [ "$#" -eq 1 ] || die "$action requires one CC Fleet session name"
  case "$1" in ccfleet-*) ;; *) die "refusing a non-CC-Fleet session name: $1" ;; esac
}

start_sync() {
  [ "$#" -ge 2 ] && [ "$#" -le 3 ] || die "start needs LOCAL-DIR UNIX-USER@HOST [REMOTE-DIR]"
  local local_dir="$1" target="$2" remote="${3:-}" user host absolute base safe_target safe_base sum name
  [ -d "$local_dir" ] || die "local directory does not exist: $local_dir"
  user="${target%%@*}"; host="${target#*@}"
  [ "$target" = "$user@$host" ] || die "target must contain exactly one @"
  case "$user" in
    ""|-*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-]*)
      die "target must look like UNIX-USER@HOST" ;;
  esac
  case "$host" in
    \[*\])
      local ipv6="${host#\[}"; ipv6="${ipv6%\]}"
      case "$ipv6" in ""|*[!abcdefABCDEF0123456789:]*)
        die "target must look like UNIX-USER@HOST" ;; esac
      case "$ipv6" in *:*) ;; *) die "target must look like UNIX-USER@HOST" ;; esac
      ;;
    ""|-*|*:*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-]*)
      die "target must look like UNIX-USER@HOST" ;;
  esac
  absolute="$(cd "$local_dir" && pwd -P)"
  base="${absolute##*/}"
  [ -n "$base" ] && [ "$base" != "/" ] || die "choose a project directory, not the filesystem root"
  safe_base="$(printf '%s' "$base" | tr -c 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' '-' | cut -c1-24)"
  if [ -z "$remote" ]; then remote="workspace/$safe_base"; fi
  case "$remote" in
    -*|/*|*:*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-]*)
      die "remote directory must be a relative path inside the slot home" ;;
  esac
  case "/$remote/" in */../*) die "remote directory may not contain .." ;; esac
  safe_target="$(printf '%s' "$target" | tr -c 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' '-' | cut -c1-32)"
  sum="$(printf '%s\n%s' "$absolute" "$target:$remote" | cksum | awk '{print $1}')"
  name="ccfleet-$safe_target-$safe_base-$sum"

  mutagen sync create \
    --name="$name" \
    --sync-mode=two-way-safe \
    --ignore-vcs \
    --ignore=node_modules \
    --ignore=.venv \
    --ignore=venv \
    --ignore=dist \
    --ignore=build \
    --ignore=.env \
    --ignore='.env.*' \
    "$absolute" "$target:$remote"
  printf '\nSync started: %s\n' "$name"
  printf 'Conflicts stop safely; inspect them with: ccfleet-sync status %s\n' "$name"
}

command_name="${1:-}"
[ -n "$command_name" ] || { usage; exit 2; }
shift
case "$command_name" in
  -h|--help|help) usage ;;
  start) need_mutagen; start_sync "$@" ;;
  status)
    need_mutagen
    [ "$#" -le 1 ] || die "status takes zero or one session name"
    if [ "$#" -eq 1 ]; then session_arg status "$@"; fi
    mutagen sync list "$@"
    ;;
  monitor|flush|pause|resume)
    need_mutagen; session_arg "$command_name" "$@"; mutagen sync "$command_name" "$1"
    ;;
  stop)
    need_mutagen; session_arg stop "$@"; mutagen sync terminate "$1"
    ;;
  *) usage >&2; die "unknown command: $command_name" ;;
esac
