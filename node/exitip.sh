#!/usr/bin/env bash
# Print this machine's public egress IP using several independent echo services.
# Exit 1 if none answers with a well-formed address.
set -euo pipefail
targets=(https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com https://checkip.amazonaws.com)
ipv4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
ipv6='^[0-9a-fA-F:]+$'
for t in "${targets[@]}"; do
  ip="$(curl -fsS --max-time 5 "$t" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$ip" =~ $ipv4 || ( ${#ip} -gt 2 && "$ip" =~ $ipv6 && "$ip" == *:* ) ]]; then
    echo "$ip"
    exit 0
  fi
done
echo "no egress IP answer from any target" >&2
exit 1
