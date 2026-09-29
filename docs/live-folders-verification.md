# Live-folder release verification

Historical evidence for the retired live-folder workflow, not the current
`ccfleet local` architecture. See [native local relay verification](local-relay-verification.md)
for the current release; the evidence below remains unchanged.

Verified 2026-09-29 UTC. Runtime release:
`96de4bb71fd05cb82783859e5576fbc765482362`.

## Automated checks

- Final [CI run 36551247603](https://github.com/cdcupt/ccfleet/actions/runs/36551247603)
  passed Python 3.9, 3.12 and 3.13: **3,027 tests** on each version,
  **93.73% coverage** on Python 3.12, plus Ruff, shell checks and unit validation.
- The local full suite passed 3,017 tests at 92.50% before the final regression
  additions; subsequent focused tests and the exact-release CI above passed.
- Installer tests verify all three digest-pinned helpers before replacing the
  client, and preserve pairing and existing shell settings on repeat setup.
- Protocol tests cover confined paths, protected control files/runtime, aliases,
  streaming I/O, large files/directories, interrupted-response deduplication,
  account cleanup, revocation and the slot-release mount fence.

## Actual filesystem and native Claude checks

Only synthetic local folders were used; no real customer home was shared.

1. A real SSHFS mount on the owner canary read and wrote local files, including
   an ordinary hidden file, a directory with 1,205 files, and a 64 MiB sparse
   file written beyond the former total-snapshot limit.
2. Native Git initialization, add, commit and status worked in a fresh mounted
   repository. An editor-style temporary-file/atomic-rename save appeared locally.
   One-second metadata caching corrected severe repeated-lookup overhead; this
   remains a network filesystem, not a local-speed performance guarantee.
3. Replacing the SSH stream retained the same mount, working directory and an
   already-open file handle. Lost responses were replayed without repeating the
   underlying mutation in the resident-connector tests.
4. The public one-command installer paired a fresh synthetic home through BWH.
   Repeating it preserved the pairing, key, pin and startup settings. Setup made
   no model call and did not share a folder.
5. The installed `ccfleet local` used the actual paired BWH/WSS/SSH route. The
   original slot Claude executable and its mounted working directory were checked.
   Native Read/Write tools copied a synthetic input file and edited it; both
   results appeared on the laptop without push or pull.
6. Closing and reopening the terminal, and replacing the filesystem SSH child,
   preserved the same Claude process, local connector instance and mount.
7. Direct device revocation stopped fresh live file I/O and denied a new SSH
   connection. Cleanup removed the test pairing and mount. The owner's existing
   ordinary tmux session and device remained unchanged.

The first native-test attempt failed in the PTY/tmux test harness before the file
task. After correcting its target selection and bounded teardown, the complete
paired native check passed. No terminal transcript or provider credential was
published as evidence.

## Deployment

BWH was backed up with SQLite's online backup API and integrity-checked before
deployment. The server was updated first, followed by the owner canary and then
the remaining hosted node. Both currently assigned hosted slots report live
readiness; installed code hashes matched the runtime release. The second node's
verification was readiness-only, without a customer project or model request.
Legacy owner nodes without hosted CLI access were not converted.

The final audit found one existing device, no pending pairings, no open alerts,
and no remaining test mounts. All 17 tenant HTTP response codes matched baseline.
The guide and fake pairing page were visually checked at desktop and 390px widths.

## Boundaries

This does not prove anonymity, complete local POSIX compatibility, provider
approval, or indefinite reliability. BWH still observes source IP/connection
metadata; selected contents and filesystem metadata can identify users; slot
root remains trusted. Writes are live and can affect files later executed on the
laptop. Claude and its tools use the slot's Linux environment, not the laptop's
OS or local services. See [live-folder usage and permissions](project-workspaces.md).
