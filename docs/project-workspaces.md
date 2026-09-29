# Live folders: local files, slot-only execution

`ccfleet local` connects the selected laptop folder as a live filesystem on the
assigned slot and opens original Claude Code there, inside persistent tmux.
Reads happen on demand; writes and deletions affect the laptop immediately.
Filename and metadata changes may take about one second to appear on the slot;
file-content reads bypass that cache and writes remain write-through.
There is no whole-folder upload and no manual push/pull step in this workflow.
Claude, shell tools and model connections run on the slot, not the laptop.
No local Claude Code installation is required.

Live folders require an upgraded node and operator-enabled access. New or
reassigned slots need operator activation. Publishing or installing this code
does not activate a slot. `ccfleet local --check` checks readiness without
reading folder contents, making a model request or enabling access.
Plain `ccfleet` remains the ordinary remote-terminal workflow. Legacy owner
nodes without hosted CLI access are outside this rollout.

## One-command setup and migration

Use the same command for a new computer, an already-paired computer, or the
legacy `ccfleet-connect` setup. You do not need to choose a migration mode.

1. If a retired local-agent preview session is still running, finish it and exit
   that session first. The installer does not stop or convert an already-running
   process.
2. Run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash -s -- --setup
   ```

3. Follow the setup prompt. An already-paired computer reuses its existing pairing;
   do not generate another code or pair again. Only if this computer is not paired,
   open [your slot page](https://ccfleet.daichenlab.com/account), choose **Connect
   this computer**, and paste one fresh pairing code when asked. No Claude password,
   token, local Claude installation, or SSH command is needed.
4. Wait for the readiness result. Setup waits for a new device key to reach the
   slot and verifies access without uploading files or making a model request.
   The legacy `ccfleet-connect` setup is cleaned up only after readiness succeeds.
   If readiness fails, the legacy setup is left in place; resolve the reported
   issue and rerun the same command.
5. Open a new terminal, choose your folder, and start:

   ```bash
   cd ~/code/my-project
   ccfleet local
   ```

   Confirm the selected folder's live read/write trust prompt. Setup itself never
   shares a folder or starts a model session.

The installer checks the downloaded client and verifies its digest-pinned helper
before replacing the client. Earlier helper versions, existing pairing
configuration, local conversation history, slot sign-in, and remote files are
preserved. PATH configuration preserves existing shell settings and keeps a backup
when modifying them. Open a new terminal to pick up PATH changes. Existing shells
keep their old environment; custom token exports outside the managed setup are not removed.

Optional setup flags are `--name "Personal Mac"` for a new device label and
`--slot SLOT` to select an existing paired slot. Add them after `--setup`; they are
not required for ordinary setup. The install-only command (omit `--setup`), manual
`ccfleet login`, and legacy `--migrate` option remain available for compatibility.

Setup does **not** automatically revoke an Anthropic credential. If an old
setup-token is no longer used anywhere else, revoke it yourself in your Anthropic
account; do not revoke a credential that another workflow still needs.

For users of the retired local-agent preview, the execution location changes:
Claude now runs on the slot. Old local conversation history stays local and is
not imported into the slot. Existing ordinary remote files and old snapshots
are not automatically moved into the connected laptop folder. The preview's
`--print` and `--fork-session` options are retired; use the original slot Claude
interface's conversation controls.

## Connect a folder

```bash
cd ~/code/my-project
ccfleet local --check
ccfleet local
```

The first connection asks you to trust live access to that specific folder.
Approval is retained for that device/folder binding. `--yes` is an explicit
scripted grant, not a safer mode. Unlike the earlier snapshot workflow, there is
no per-file upload list or reviewed download: tools can read, edit and delete
accessible files directly. Keep backups and use version control.

Use `--project PATH` instead of changing directory and `--slot SLOT` to select
a paired slot. You may select your home folder using the same trust flow:

```bash
cd ~
ccfleet local
```

There is no separate `--allow-home` flag or home-folder ban. Home access can expose
dotfiles, `.ssh`, `.claude`, `.env` files, settings and credentials if the agent
reads them. It can also change startup scripts or other files executed locally
later. Do not grant a folder you cannot entrust to the slot and its administrators.

The live mount has no snapshot-style file-count, per-file-size or total-size
product caps, and does not apply Git-ignore or broad hidden-file filters.
Available storage, operating-system permissions and network performance still
matter; protocol messages and concurrent operations remain resource-bounded.
The selected root is the filesystem boundary. CC Fleet's private configuration,
device keys, host-key pins, and active client/helper files remain protected;
they are not an invitation to copy or overwrite the connector's own control data.
The active Python runtime is protected too.

Claude's environment is Linux on the slot. Sharing Mac files does not make
macOS-only commands, Xcode, local services, or native Mac dependencies available
there. The connector provides file operations, not a local shell or local process
execution.

## Sessions, disconnects and reconnects

Closing the terminal detaches the interface but leaves the background folder
connector active. The remote tmux session also remains. Return with:

```bash
ccfleet local --continue
ccfleet local --resume
ccfleet local --resume work
```

`--continue` selects the previously used remote session; `--resume` lists running
sessions, and `--resume NAME` selects one. These are not the old laptop history
picker. Create another named session with `--new --name NAME`; names allow 1–32
letters, digits, underscores or hyphens, starting with a letter or digit.

New live sessions default to `bypassPermissions`, Opus and max effort. Tools can
write or delete files in the connected folder without individual permission
prompts. Choose another mode for a new session, for example:

```bash
ccfleet local --new --name research --mode plan --model opus --effort high
```

Use `/model` and `/effort` inside the original Claude interface. Model availability
depends on the slot's Claude account. Reattaching does not silently change an
existing session's model or permission mode. Permission settings govern the
slot's tools, including their access through the live mount. They do not turn
folder access into a laptop OS sandbox.

Use `/exit` to finish one Claude session. To stop folder access and its associated
remote project sessions, run from that folder (or specify `--project PATH`):

```bash
ccfleet local --disconnect
```

If the laptop sleeps or its network is unavailable, filesystem operations wait
for the connection; the remote session cannot keep using unavailable local files
as if they were a stored remote copy. Keep the computer awake and online for work
that needs its folder. A persistent tmux session alone is not a guarantee that an
interrupted filesystem operation completed successfully.

If the background connector was lost or replaced, a new connector instance must
not silently reuse stale file handles. `ccfleet local --reset-link` performs the
explicit reset after a warning. Resetting can interrupt work; inspect files and
restart interrupted operations rather than assuming an in-flight write completed.

Multiple computers may stay paired to the same slot, but each live folder belongs
to its originating computer. Pairing another computer does not copy files or make
its local directory an interchangeable replacement. Concurrent editors use a live
filesystem, not the snapshot workflow's conflict review or a collaborative merge.

## Legacy snapshot recovery only

The old `ccfleet project` commands remain for recovering earlier snapshot work.
They are not required before, during or after a live `ccfleet local` session:

```bash
ccfleet project list --slot SLOT
ccfleet project pull --slot SLOT --remote-project ID --project EMPTY_DIRECTORY
```

Use an existing empty directory and replace `ID` with an ID from the listing.
`ccfleet project status`, `diff`, `pull` and `push` retain their earlier explicit
snapshot behavior, including review, conflict checks and recovery backups under
`~/.config/ccfleet/project-backups`. All old snapshot project sessions must finish
before another snapshot push. Legacy limits remain 1,000 files, 4 MiB per file
and 20 MiB total, with the earlier ignore and credential-name exclusions. Those
limits and filters do not describe the live mount. Snapshot recovery does not
automatically migrate old local Claude conversation history into the slot.

## Privacy and account boundary

```text
laptop folder connector + terminal
  -> pinned, end-to-end SSH carried by the BWH WebSocket broker
  -> live folder mount + original Claude Code on the slot
  -> Anthropic
```

The connector does not automatically collect or forward laptop hostname,
environment variables, timezone, geolocation or host fingerprint. Selected
filenames, contents, link targets and filesystem metadata can identify the user
or reveal local environment details when read. It provides no remote command
for executing a laptop shell. There is no local model HTTP listener, credential
substitution or model-request relay. One slot continues to use only its bound
holder's account, and native Claude owns its authentication and renewal.

This is not absolute anonymity. BWH sees the incoming IP and routing/connection
metadata, though it cannot decrypt the inner SSH contents. SSH exposes transport
properties such as its client version and terminal dimensions. Slot root can
inspect and change connected data while access is active, and must be trusted.
Anthropic receives relevant selected content, prompts and slot-native client
information. Disconnecting stops future folder access but does not erase content
already read into slot memory, files or conversation history, or undo writes.

## Rollout and verification

Live folders require verified node support, including a usable Linux filesystem
mount facility, and operator-enabled access. A previously working terminal or
snapshot connection alone does not prove that the live mount is available.
Installing the client or running a readiness check does not enable access.

Release checks must cover actual Linux mounts, local read/write-through, root
confinement, protected control files, session persistence, interrupted writes,
reconnect and explicit reset, revocation, and safe unmount before slot removal.
Use synthetic files and identifiers rather than customer data. A successful
connection is not evidence of zero metadata exposure or a completed deployment.
Record live release verification separately; no deployed revision is asserted
by this guide.
