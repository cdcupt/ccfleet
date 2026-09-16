#!/usr/bin/env bash
# Staged Claude Code upgrade for one node. Run as the owner.
#
#   ccfleet-upgrade-claude            # latest stable
#   ccfleet-upgrade-claude 2.1.92     # a specific version
#
# Re-runs Anthropic's official installer (the binary stays unmodified), prints
# the version before and after, and reminds you to update the fleet pin.
set -euo pipefail
version="${1:-latest}"
before="$(claude --version 2>/dev/null || echo 'not installed')"
echo "before: $before"
curl -fsSL https://claude.ai/install.sh | bash -s -- "$version"
after="$(claude --version 2>/dev/null || echo 'not installed')"
echo "after:  $after"
cat <<MSG

If the version changed, update the pin on the fleet server so the dashboard
stops flagging a mismatch:

  ccfleetd node pin <node-id> <version>

and send a fresh heartbeat:

  systemctl --user start ccfleet-agent.service
MSG
