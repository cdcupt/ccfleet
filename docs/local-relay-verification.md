# Native local relay verification

Verified 2026-09-29 UTC. This supersedes the live-folder release for new
`ccfleet local` launches; it does not terminate existing user sessions.

## Tested releases

- Server and slot relay: `1d18b369c1aff3217dc53c7c8d789d72b9824587`.
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
