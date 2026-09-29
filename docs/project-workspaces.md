# Project workspaces: explicit sharing, slot-only execution

`ccfleet local` is a thin project connector, not a local Claude launcher. It
shares a user-selected project snapshot with the assigned slot and opens the
original Claude Code there, inside persistent tmux. Claude, shell tools and
model connections run on the slot. No Claude Code installation is required on
the laptop; the connector supports macOS and Linux with Python 3.9+ and OpenSSH.

Project access is enabled for currently assigned hosted slots. Newly created or
reassigned slots still need operator activation. Publishing or installing this
code does not activate a slot. `ccfleet local --check` checks the selected slot
without transferring project files, making a model request or enabling access.
Plain `ccfleet` remains the ordinary remote-terminal workflow. Legacy owner
nodes without hosted CLI access are outside this project-access rollout.

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
   slot and verifies project access without uploading files or making a model
   request. The legacy `ccfleet-connect` setup is cleaned up only after readiness
   succeeds. If readiness fails, the legacy setup is left in place; resolve the
   reported issue and rerun the same command.
5. Open a new terminal, choose your project, and start:

   ```bash
   cd ~/code/my-project
   ccfleet local
   ```

   Review the selected file list and confirm the first share. Setup itself never
   shares a project or starts a model session.

The installer checks the downloaded client and verifies its digest-pinned project
helper before replacing the client. Earlier helper versions, existing pairing
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
not imported into the slot. Existing ordinary remote files are not automatically
moved into a project workspace. `--print` and `--fork-session` from the preview
are retired; use the original slot Claude interface's conversation controls.

## First project and daily work

```bash
cd ~/code/my-project
ccfleet local --check
ccfleet project status
ccfleet local --new --name work
```

Review the first-share file list and confirm it. The selected snapshot is copied
to `~/workspace/projects/<opaque-project-id>` on the slot. Neither the absolute
laptop path nor its directory name is used as the remote workspace identity.
`--yes` skips confirmation only when explicitly requested; use it intentionally.

Use `--project PATH` instead of changing directory and `--slot SLOT` to choose a
paired slot. A project must be a real directory, not the home directory or a
filesystem root. The selection must not contain CC Fleet's own configuration.

Disconnecting preserves the tmux session. Return with:

```bash
ccfleet local --continue
ccfleet local --resume
ccfleet local --resume work
```

`--continue` selects the previously used remote project session; `--resume`
lists running project sessions, and `--resume NAME` selects one. These are not
the old laptop conversation-history picker. Create another named session with
`--new --name NAME`; names allow 1–32 letters, digits, underscores or hyphens,
starting with a letter or digit.

Project sessions default to manual permissions, Opus and max effort. Pick new
session launch settings explicitly, for example:

```bash
ccfleet local --new --name research --mode plan --model opus --effort high
```

Use `/model` and `/effort` inside the original Claude interface to change an
existing session. Model availability depends on the slot's Claude account.
Permission modes govern the slot's tools, not laptop tools. Choosing
`bypassPermissions` allows tools to run without approval as the slot Linux user.

Finish each project session with `/exit` before pushing another snapshot.
Closing a terminal only disconnects it; it does not end the session. Push is
rejected while any session for that project is still active.

```bash
ccfleet project diff
ccfleet project pull
# After making further local changes, with project sessions finished:
ccfleet project push
```

Diff previews incoming changes. Pull reviews changes and asks before applying
them, rejects conflicting local/slot edits, and backs up files it will replace
or delete under `~/.config/ccfleet/project-backups` (or the configured CC Fleet
directory). Keep separate backups too. Stop local editors during apply: a
detected concurrent edit can stop an update after earlier files were applied,
with originals retained in the backup. It is not an all-files transaction.

There is **no background synchronization**, filesystem mount, automatic upload
on reconnect, or automatic pull when a session ends. If local files have changed,
resuming keeps the existing slot workspace and does not upload those changes.

For another paired computer, run `ccfleet project list`, choose a new empty local
directory, and pull the opaque project ID before opening it:

```bash
ccfleet project list --slot SLOT
ccfleet project pull --slot SLOT --remote-project ID --project PATH
ccfleet local --slot SLOT --project PATH
```

Each computer retains its own revocable device key. Optimistic content checks
reject stale overwrites between computers; they do not implement live co-editing.

## Selection and limits

The snapshot schema permits only relative filenames, file bytes, SHA256 hashes
and an executable flag. No filesystem ownership, modification times, absolute
paths or arbitrary metadata fields are transferred.

- Maximum 1,000 files, 4 MiB per file and 20 MiB total content.
- In a Git project, selection is tracked files plus untracked files allowed by
  project ignore rules. Personal/system Git configuration is not used. Tracked
  files remain selected even if a later ignore rule matches them.
- Hard exclusions apply regardless of Git tracking: `.git`, `.ssh`, `.aws`,
  `.azure`, `.config`, `.claude`, `.codex`, `.agents`, `.ccfleet`, editor metadata,
  dependency directories, build outputs, shell histories and known credential
  filenames. Names starting `.env`, `credentials`, `secrets`, `id_rsa` or
  `id_ed25519`, and names ending `.pem` or `.key`, are excluded.
- Selected symlinks, hardlinks, special files, traversal paths and ambiguous
  cross-platform names are rejected. Directory ancestry is opened without
  following symlinks. The source of truth for exact limits and exclusions is
  [`ccfleet_agent/project_files.py`](../ccfleet_agent/project_files.py).

Name exclusions cannot detect every secret inside a normal source file. Review
the selection and remove sensitive content before sharing. This is a bounded
file-transfer boundary, **not an operating-system sandbox** against other
processes running as the laptop user.

## Privacy and account boundary

```text
laptop project connector + terminal
  -> pinned, end-to-end SSH carried by the BWH WebSocket broker
  -> selected project workspace + original Claude Code on the slot
  -> Anthropic
```

The connector does not automatically collect or forward laptop hostname,
username, home-directory path, environment variables, timezone or host
fingerprint. It provides no remote command for executing a laptop shell. There
is no local model HTTP listener, credential substitution or model-request relay.
One slot continues to use only its bound holder's account, and native Claude
retains ownership of authentication and renewal.

This is not absolute anonymity. BWH sees the incoming IP and routing/connection
metadata, though it cannot decrypt the inner SSH contents. SSH itself exposes
transport properties such as its client version and terminal dimensions. Slot
root can inspect files, credentials and terminal data, and must be trusted.
Shared file contents, relative filenames and prompts can identify a person;
Anthropic receives relevant content and the slot-native client's information.
Moving execution to a slot does not make every piece of user-supplied content
anonymous or change account/region eligibility.

## Rollout and verification

Project access is separately operator-gated. The retired relay gate does not
enable the new project protocol. The new gate is
`/etc/ccfleet/project-access/<unix-user>`: a root-owned regular file inside a
root-owned directory, neither writable by group or others. Normal installation
does not create it. Currently assigned hosted slots have been enabled; new and
reassigned slots still require deliberate operator activation. The existing
terminal path remains available. The owner canary supplies full end-to-end proof
for project selection, slot-only process execution, push/pull conflicts,
reconnect, account binding and device revocation; wider activation uses readiness
checks without reading customer projects or making model requests.

Automated tests should use synthetic identifiers to verify the transfer schema
rejects host metadata and unsafe paths. A successful connection is not evidence
of zero metadata exposure or a completed fleet deployment. Record live release
verification separately; no deployed revision is asserted by this guide.
