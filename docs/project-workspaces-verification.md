# Project workspace release verification

Verified 2026-09-29 UTC. Initial project release: `391b0610e0e69a074d5d427597c502751147db32`.
This record also covers the subsequent account-session cleanup hardening.

## Automated checks

- Final full Python 3.12 suite: **2,775 passed**, **95.32% coverage**, exceeding the 80% gate.
- Focused transfer/client/node/lifecycle/installer suite: 412 passed.
- Python 3.9 compatibility checks passed for the shared filesystem module,
  slot protocol, client, installer and migration documentation.
- Ruff, Bash parsing, ShellCheck and diff checks passed.
- The digest-pinned public installer successfully installed the matching client
  and project helper in a temporary directory without modifying an existing pairing.

These tests cover typed content-only transfers, synthetic host metadata exclusions,
unsafe paths and links, ignored/credential files, bounded framing, account binding,
conflicts, backups, session command/environment selection and retired relay refusal.

An additional 24 account-transition regression cases verify that an explicit login
refresh/change ends default, project, and recognized named Claude sessions while
preserving unrelated shell sessions. Discovery/termination failures leave durable
restart debt; they cannot silently mark cleanup complete. These tests use synthetic
accounts and command runners, not changes to a real customer's Claude sign-in.

## Live owner-slot canary

The complete paired-device WSS/SSH path was exercised with synthetic project files:

1. Readiness succeeded without sharing files or making a model request.
2. Upload included the selected file and excluded a fake `.env` secret.
3. The original slot Claude Code 2.1.284 opened in the project's remote directory.
4. The process executable and working directory were verified on the slot. Its
   environment used UTC, omitted the laptop test marker, and contained no local
   proxy/auth-token variables. The laptop test process used a PATH without its
   local Claude installation.
5. Native Read/Write tools copied a known file and edited another on the slot.
6. A conflicting laptop edit was refused without overwriting it. After resolving
   that deliberate conflict, pull applied the remote changes and preserved a backup.
7. Disconnect/reconnect retained the same tmux session creation timestamp.
8. Revoking the temporary device closed its live connection.

The test device and session were removed. Synthetic remote files were retained in
a private canary archive; the existing user device and ordinary tmux session remained.

## Deployment and scope

BWH was updated first after an online SQLite backup with a successful integrity
check. The shared-node rollout ran the owner canary before the remaining shared
node; installed files matched the release and fresh heartbeats arrived without
new alerts. The unchanged owner-node agent also matched the release file hash.

Project access uses a separate operator gate. Installing the code or running the
readiness check does not grant access. Newly created or reassigned hosted slots
still need explicit operator activation. The old local-agent inference relay
entry point is retired; ordinary remote terminals are retained.

The migration guide was visually checked at desktop and 390px phone widths, with
no page-level horizontal overflow. Commands may scroll inside their code blocks.

## Wider activation

Activation was verified on the remaining currently assigned hosted slot on
2026-09-29 UTC. Installed module hashes matched the tested release; the root-owned
gate was enabled, and the fixed framed readiness protocol returned success.
The wider check was readiness-only: no customer project files were read and
no model requests were made. The owner canary above remains the source of the full
end-to-end proof. Legacy owner nodes without hosted CLI access are outside this
activation scope; this is not a claim that every node supports project access.

## What this evidence does not claim

This is not proof that no identifying information can ever leave a device. SSH
transport metadata, BWH's observed connection IP, and user-selected contents remain
distinct from automatic host-environment export. Slot root remains trusted. The
canary did not decrypt or log real Anthropic traffic, and it does not establish
an indefinite reliability or anonymity guarantee. See [the product boundary](project-workspaces.md).
