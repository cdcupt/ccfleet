# Retired remote workspaces: migration and recovery

The live-folder and selected-snapshot designs previously used by `ccfleet local`
are no longer the primary workflow. The current command runs original Claude
Code, its tools, files and native history **locally**, with supported model
requests relayed through the assigned slot.

Start with [local Claude setup and migration](local-relay.md). The standard setup
command is unchanged:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash -s -- --setup
```

## Stop earlier live-folder access deliberately

A previous live-folder connector may still be running after its terminal closes.
Setup and the current local launcher check known bindings on this computer and
ask before stopping connectors and their associated remote live-folder sessions.
Finish pending work first, or explicitly approve cancellation in the controlling
terminal. `--yes` authorizes that cancellation in a script. Ordinary remote tmux
sessions are not part of this targeted cleanup.

To request legacy cleanup directly, from the earlier connected folder:

```bash
ccfleet local --disconnect
```

Use `--project PATH` when needed. This action ends the old mount and its associated
sessions; it does not delete local files or native/preview history. If the slot is
offline or cleanup cannot be confirmed, follow the pending-cleanup message and
retry. Do not assume a closed terminal or a client update removed the old grant.
`--reset-link` is retired and now provides migration guidance, not a new mount.

Do not stop a real user's old session through operator tools solely to complete
a rollout. The user must finish it or explicitly approve cancelling its work.

## Recover old snapshots without overwriting current work

The `ccfleet project` commands remain for earlier explicit snapshots, not the
current local-Claude experience. Use a separate existing empty directory:

```bash
ccfleet project list --slot SLOT
ccfleet project pull --slot SLOT --remote-project ID --project EMPTY_DIRECTORY
```

Replace `ID` with an ID from the listing. Review changes before applying them.
Legacy pulls retain conflict checks and recovery backups under
`~/.config/ccfleet/project-backups`. They are not all-files transactions; keep
separate backups. Old snapshot pushes require their remote project sessions to
finish first.

Historical snapshot limits (1,000 files, 4 MiB per file and 20 MiB total) and ignore
filters apply only to those legacy transfer commands. They do not limit what the
original local Claude client can access. The new primary workflow uses neither
snapshots nor a filesystem mount, and needs no push/pull step.

## Keep each history where it belongs

The current launcher uses native local history/settings by default.
`ccfleet local --legacy-history --resume` selects the earlier per-slot local-relay
preview profile without copying, deleting or importing it. Remote tmux/live-folder
history remains on the slot and is not automatically imported locally.

Plain `ccfleet` continues to open the ordinary remote terminal and remote
`~/workspace`. That compatibility path is separate from local Claude sessions.
