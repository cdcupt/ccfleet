# Native local relay verification

Verified 2026-09-29 UTC. This supersedes the live-folder release for new
`ccfleet local` launches; it does not terminate existing user sessions.

## Latest lifecycle and streaming follow-up

Runtime revision `e1b1abc6378bb24611bcea8c67a505f790b65723` was deployed to
BWH and all three managed hosted nodes on 2026-09-29 UTC, canary first. The
published client bytes did not change.

- Completed local suite: **3,653 passed, 1 skipped, 93.78% coverage**. The skip
  is Linux-only directory traversal validation, which was exercised in CI.
- [Exact-runtime CI](https://github.com/cdcupt/ccfleet/actions/runs/36618480056)
  passed all five jobs. Python 3.9, 3.12 and 3.13 each reported **3,654 passed,
  93.81% coverage**. Ruff, Bash syntax and changed-script ShellCheck passed.
- Synthetic regressions cover automatic claim/release/reclaim gates, stale and
  malformed assignments, privileged accounts, operator opt-out, unsafe policy
  files and installer dependency failures. Three deliberately broken guards
  failed their targeted regressions in separate processes with fresh bytecode
  caches.
- A separate networkless Linux container dropped to a non-root identity and
  verified claim, release denial, reclaim and opt-out beneath a root-owned
  `0711` policy directory. No customer credentials or accounts were involved.
- Stream tests verify same-account native rotation beyond the old expiry,
  logout/missing/expired credential cancellation, a retained socket after an
  HTTP `Connection: close` response, and suppression of buffered chunks after
  cancellation. These are controlled-peer tests, not a forced live OAuth refresh.
- Root explicitly opted the three hosted machines into automatic gate
  reconciliation. Installed module hashes and fresh reports were verified.
  Both occupied nodes passed readiness as the unprivileged slot user; the free
  node remained unclaimed with no customer Unix account or inference gate.
  No occupied customer slot was released or reassigned for testing.
- The public installer paired an isolated owner-canary profile. Two short model
  turns verified actual local Read/Write and native same-session continuation.
  Revocation ended an idle connection and denied reconnection. The temporary
  device was cleaned up, and the pre-existing ordinary tmux session was preserved.
- The online database backup passed integrity checking; rollback images and
  node-library backups were retained. Final checks found zero open alerts,
  zero pending pairings, the one original active device, unchanged responses
  from all 17 tenant domains, and working migration/privacy pages.
- Seven inactive synthetic test profiles/staging directories were moved into a
  private recoverable archive. User projects/history and production recovery
  backups were preserved; no customer data was deleted as cleanup.

Automatic access policy is documented in [operator verification](local-relay.md#operator-verification).
This closes the reviewed lifecycle and stream defects; it does not establish
complete CC Host parity, zero metadata disclosure or indefinite account access.

## Earlier reliability and privacy hardening

Runtime revision `f70b2ed5c9bd61a7aa67e91e7014200bf4c7362c` was deployed to
BWH and the two currently assigned hosted nodes, canary first. The published
client and its digest-pinned helper matched that revision. Legacy owner nodes
were not converted.

- Completed local suite: **3,451 passed, 93.80% coverage**. The first full run
  caught an omitted status-page classification for the new credential alerts;
  that was corrected and the full suite rerun successfully.
- [Exact-release CI](https://github.com/cdcupt/ccfleet/actions/runs/36586558266)
  passed Python 3.9, 3.12 and 3.13, shell and units. Python 3.12 reported
  **3,451 passed, 93.81% coverage**. Ruff and Bash checks passed.
- Before deployment, an authorized owner-slot observation saw native credential
  expiry advance naturally to roughly eight hours with unchanged account binding.
  No expiry field was edited, credential copied, or model request submitted for
  that observation. The new early-renewal scheduling and failure/race paths were
  exercised with synthetic credentials; the live upgraded agents reported
  `current`, not a newly observed `renewed` event.
- The public installed-client canary passed actual local Read/Write, same-session
  `--continue`, idle connection revocation and rejection of a new connection.
  Its two file-tool calls stayed inside the synthetic directory. The temporary
  device was revoked and removed locally; the pre-existing ordinary remote
  session remained unchanged. This used two short owner-account model turns.
- Both hosted nodes' installed agent, machine and relay hashes matched the
  release, and both returned protocol-v2 readiness. The second node received
  readiness-only verification, without a customer model request or file access.
- The production database backup passed integrity checking. Fresh heartbeats,
  zero open alerts, one original device, zero pending pairings and all 17 tenant
  HTTP baselines were verified after rollout. New credential-health copy was
  checked at desktop and 390px widths.

See [the design choices and limits](reliability-privacy.md). Captured identity
replay was not implemented. These checks do not prove permanent account access,
zero metadata disclosure, or absence of every future vendor-compatibility issue.

## Baseline inference release evidence

- Inference-runtime baseline (server relay code and installed slot relay):
  `1d18b369c1aff3217dc53c7c8d789d72b9824587`. Later website-copy releases may use a
  newer repository revision without changing that inference protocol or node code.
- Published client, including lock-confirmed legacy connector shutdown:
  `501ffedf7ec55a2b873623b814018cc13e39791c`.
- [Server release CI](https://github.com/cdcupt/ccfleet/actions/runs/36561158025)
  and [current client CI](https://github.com/cdcupt/ccfleet/actions/runs/36566720164)
  passed Python 3.9, 3.12 and 3.13, shell checks and unit validation.
- The completed local full suite for the current client passed **3,263 tests,
  93.65% coverage**. The focused connector/native-launch suite also passed on
  Python 3.9. Ruff, Bash syntax and ShellCheck passed.

### Migration shutdown regression

The first release missed a control-socket shutdown race: a stop acknowledgement
could arrive before the connector finished closing, and the next status query
could receive EOF. The current client confirms release of the resident's exclusive
lock, which remains held until child processes and filesystem handles are closed.
Missing sockets, EOF, reset and timeout are not themselves proof of completion.

Real Unix-socket and flock tests cover truncated replies, reset/timeout, a missing
listener with a still-held lock, and delayed filesystem-handle teardown. Migration
tests confirm that unverified shutdown cannot retire a grant, reset the remote
workspace, or launch a second local workflow. The public installer was exercised
in a temporary destination; its client and helper bytes matched this tested
revision. This was a client-only fix, requiring no server restart or new pairing.

## Actual native-client checks

Tests used a synthetic local home/project and the owner's canary slot, not
customer files or customer model requests. The actual local Claude executable
was version 2.1.284.

1. Native settings precedence was checked against controlled loopback endpoints.
   User/project settings can override process environment; a final private
   settings overlay was necessary to keep the authenticated inference route.
2. The public installer paired the isolated home and checked inference readiness
   through BWH and pinned SSH. Setup itself made no model request.
3. The installed client launched original **local** Claude. Its native Read and
   Write tools copied a unique synthetic input to a local output byte-for-byte,
   while inference used the assigned slot relay.
4. Native `--continue` retained the same session ID and recalled the previous
   turn's unique marker without reading files again.
5. Revoking the test device terminated an incomplete idle relay request and
   denied a new connection. This revocation check made no upstream model call.
6. Cleanup retained the owner's pre-existing ordinary tmux session and old
   live-folder session. The temporary device was revoked; only the existing
   device remained active. An initial cleanup wait exceeded its harness timeout;
   the idempotent cleanup retry completed successfully.

Protocol and launcher tests additionally cover identity-header/structured-metadata
filtering before SSH and at the slot, account binding, expiry, framing limits,
streaming, cancellation, route/auth conflicts, and consent-based legacy cleanup.
Native slot credential renewal was verified during earlier canary work, not
forced again against a real account for this release.

## Deployment checks

The production SQLite database was backed up through the online backup API and
integrity-checked before deployment. BWH was updated first, then the owner
canary, then the remaining hosted node. Installed relay and entrypoint hashes
matched the tested release on both hosted nodes; both returned protocol-v2
readiness. The second node's check made no customer model request. Legacy owner
nodes were not converted.

The final audit found fresh heartbeats, zero open alerts, zero pending pairings,
and the existing device/session preserved. All 17 tenant HTTP status codes
matched baseline. Published client bytes matched the tested client revision.
The migration guide and pairing UI were visually checked at desktop and 390px
widths.

## Limits of this evidence

This verifies the exercised paths, not the absence of every future bug or
complete CC Host feature parity. Local tools and history stay local, but native
prompts/tool results may disclose paths, OS details or identifying content.
Independent hooks/MCP/tools can use the network directly. Slot root remains
trusted; one slot still uses one bound account. No anonymity or provider approval
is implied. Detached native background agents are not supported by the
launch-scoped relay. See [usage, migration and privacy](local-relay.md).
