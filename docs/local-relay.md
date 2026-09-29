# Local Claude Code with a dedicated-slot inference relay

`ccfleet local` launches the original Claude Code CLI on this computer. Files,
shell tools, native settings, plugins and conversation history are local. Only
the supported model API requests use the encrypted path to the user's assigned
slot and then Anthropic. There is no project upload, filesystem mount, or
CC Fleet file-count, file-size or Git-ignore filter in this workflow.

```text
original Claude Code on laptop: files, tools, history
  -> launch-scoped authenticated loopback endpoint
  -> pinned SSH inside the BWH WebSocket broker
  -> assigned slot relay, using only that slot's bound account
  -> api.anthropic.com
```

The slot's Claude credential is not returned to the laptop or BWH. Native Claude
on the slot remains responsible for its authentication and renewal. The relay
does not pool accounts, choose another account on failure, or refresh credentials
itself. Plain `ccfleet` remains a remote-terminal compatibility command: it opens
original Claude Code on the slot, with the slot's files and tmux session.

## One-command setup

Use the same command for a new computer or an update:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash -s -- --setup
```

Setup preserves an existing pairing and native Claude installation. If Claude
is missing, it installs the original CLI from the fixed vendor installer URL.
If this computer is not paired, it asks for one fresh code from **Connect this
computer** on [your slot page](https://ccfleet.daichenlab.com/account). No manual
SSH command or slot Claude credential is needed.

Setup verifies readiness without uploading project files or making a model
request. It preserves configuration and history, and backs up shell startup
configuration when changing PATH. The old `ccfleet-connect` token setup is removed
only after readiness succeeds. Custom token exports outside that managed setup
are not silently rewritten. Open a new terminal afterward.

Optional `--name "Personal Mac"` labels a new device, and `--slot SLOT` chooses an
existing pairing. Setup does not automatically revoke an Anthropic credential.
Revoke an old setup-token yourself only if it is no longer used anywhere else.
Install-only (omit `--setup`), manual `ccfleet login`, and legacy `--migrate` remain
compatibility options, not extra steps in the normal setup flow.

## Migrating from live folders or snapshots

The previous release ran Claude on the slot with a background live mount of local
files. That is no longer the primary workflow. Closing its terminal did **not**
stop the background folder grant.

Setup and the new `ccfleet local` detect known old live-folder bindings on this
computer before launching local Claude. If cleanup is needed, they ask in the
controlling terminal before stopping those local connectors and their associated
remote live-folder sessions. This cancels pending work and invalidates open
mount handles. Finish old work first, or deliberately approve that cancellation.
`--yes` explicitly authorizes the cleanup in a script; do not add it merely to
suppress an unfamiliar warning.

Files, native history, old preview history and ordinary remote tmux sessions are
kept. Cleanup does not copy, import, delete or roll back project files. If cleanup
is refused or cannot be verified, do not start a second workflow and assume the
old access has stopped: follow the reported pending-cleanup instructions and
retry after the slot is reachable. No unattended deployment step should kill an
existing user session just because a new client is available.

`ccfleet local --disconnect` remains an explicit **legacy live-folder cleanup**
action. It is not how to quit a new local Claude session. `--reset-link` is retired;
follow its migration guidance rather than trying to resume the old mount.
Earlier `ccfleet project` snapshot commands remain for deliberate recovery only;
see [retired workspaces and recovery](project-workspaces.md).

## Daily use and native sessions

```bash
cd ~/code/my-project
ccfleet local --check
ccfleet local --new --name work
```

Home directories work normally too: `cd ~` then `ccfleet local`. The current
working directory is not a security sandbox. Claude's local permissions determine
which files and commands it may use, including outside the current directory.
New sessions default to `bypassPermissions`, Opus and max effort: tools can change,
delete or transmit data without individual approval prompts. Choose a different
mode when appropriate:

```bash
ccfleet local --new --name research --mode plan --model opus --effort high
ccfleet local --resume
ccfleet local --resume work
ccfleet local --continue
ccfleet local --resume work --fork-session
ccfleet local --print "Summarize this project"
```

These are native Claude sessions, not remote tmux sessions. `--resume` opens the
native history picker or selects an ID/name; `--continue` uses the last native
conversation for this directory. `--new --name NAME` starts a named conversation.
Use `/model` and `/effort` inside Claude. Defaults for a new conversation should
not overwrite a resumed conversation's saved model or effort unless requested.
Model/effort availability depends on the installed CLI and account.

Additional supported native arguments go after `--`. Options that would override
the protected model route or authentication are not an alternate CC Fleet mode.
Quit with Claude's `/exit` or the normal terminal controls, then use native resume
to continue later. An interrupted request is not guaranteed to have completed;
CC Fleet does not silently replay inference.

The relay currently supports foreground interactive sessions, `--print`, native
resume, and multiple normal terminal sessions. Native `--bg` / `--background`
is explicitly rejected because the detached agent would outlive its launch-scoped
bridge. This is not background-agent support; use another regular terminal for
parallel sessions.

## History, settings and routing

The default uses the native Claude configuration/history location, including an
existing `CLAUDE_CONFIG_DIR` when set. It does not move or duplicate native data.
For conversations saved by the earlier isolated relay preview, opt into that
existing per-slot profile:

```bash
ccfleet local --legacy-history --resume
```

`--legacy-history` selects the old profile; it does not import it into the native
profile. Old remote-slot conversation history is separate and is not automatically
converted into local Claude history.

Ordinary native customizations are retained. A temporary private `--settings`
overlay pins the launch-scoped loopback URL/auth nonce, clears conflicting model
provider/auth overrides, and disables supported optional telemetry. It does not
permanently edit user/project settings or distribute a slot credential. Shell
variables alone are insufficient: native user, project and local settings can
override them. Unexpected routing/policy conflicts must fail clearly, not fall
back to another provider or account.

## Privacy: what this does and does not protect

The relay removes selected request headers and the top-level structured
`metadata` field before forwarding. This is not complete redaction: the native
system prompt, user messages, filenames and tool results may contain the local OS,
working directory, paths, identity or other environment details. Relevant content
is processed on the slot and sent to Anthropic. Do not promise fingerprint-free
requests, zero metadata disclosure, or that Anthropic sees only slot information.

Only supported message/token-count endpoints use the model relay. MCP servers,
hooks, shell tools, plugins, updates and other native CLI services can make their
own connections from the laptop. Disabling optional telemetry is not an all-traffic
firewall, VPN, or anonymity guarantee. Users control those native features.

BWH sees the incoming IP and connection/routing metadata but cannot decrypt the
inner SSH request stream. Slot root can inspect or alter relayed requests and
responses; altered model responses can influence the local agent's tool actions.
Native local permissions still matter. Closing the relay stops future transport,
not data already received by Anthropic or retained in native history.

## Operations and verification

Relay access requires an upgraded, operator-enabled assigned slot. A working
remote terminal or old live mount does not prove relay readiness. Deployment and
general activation must be verified separately after a canary; this guide asserts
no deployed revision or provider approval. See [compliance notes](compliance.md)
for the historical source record and the distinction from hosted terminal use.

Validate with synthetic settings and controlled upstreams: endpoint/auth override
precedence, removal of selected metadata, native session/history preservation,
streaming/truncation/cancellation, account binding, device revocation, native
credential renewal, and migration cancellation/cleanup. Use no customer files,
real credentials in logs, or unapproved model usage for these tests.
