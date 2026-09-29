# Project workspaces: explicit sharing, slot-only execution

`ccfleet local` is a thin project connector, not a local Claude launcher. It
shares a user-selected project snapshot with the assigned slot and opens the
original Claude Code there, inside persistent tmux. Claude, shell tools and
model connections run on the slot. No Claude Code installation is required on
the laptop; the connector supports macOS and Linux with Python 3.9+ and OpenSSH.

Project access is an operator-enabled canary, not available on every slot.
Publishing or installing this code does not enable a slot, prove deployment, or
mean a full fleet rollout has passed. `ccfleet local --check` checks the selected
slot without transferring project files or making a model request. Plain
`ccfleet` remains the ordinary remote-terminal workflow.

## Migrate an existing computer

If you still use the legacy `ccfleet-connect` token setup, obtain a fresh pairing
code from the slot page and run:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh \
  | bash -s -- --migrate
```

Pairing must succeed before the installer removes the old setup. Open a new
terminal afterward. Revoke an old Anthropic setup-token only if nothing else
uses it.

If this computer is already paired, update without pairing again or repeating
`--migrate`:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash
```

The installer verifies a digest-pinned project helper, installs it before the
matching client, and retains earlier helper versions. It does not remove pairing
configuration, local conversation history or remote files, and it installs no
local Claude agent.

For users of the retired local-agent preview, the command name is familiar but
the execution location changes. Exit any running preview session before updating;
the installer does not stop or convert an already-running process. Local conversation
history stays local and is
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
does not create it. Preserve the existing terminal path while
canarying project selection, slot-only process execution, push/pull conflicts,
multi-computer use, reconnect, account binding and device revocation.

Automated tests should use synthetic identifiers to verify the transfer schema
rejects host metadata and unsafe paths. A successful connection is not evidence
of zero metadata exposure or a completed fleet deployment. Record live release
verification separately; no deployed revision is asserted by this guide.
