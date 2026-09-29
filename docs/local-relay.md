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

Hosted maintenance checks approaching credential expiry and attempts bounded
native renewal without relying on a fresh quota-cache miss. It verifies expiry
actually advanced, serializes attempts, and reports only safe health facts.
The relay remains read-only with respect to credentials; see
[reliability and privacy decisions](reliability-privacy.md) for the exact boundaries.

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

### Recovering an interrupted migration

If an older installer printed `EOFError: filesystem connection ended` after you
approved cleanup, rerun the one-command setup above to install the fix and finish
migration. Do not delete configuration, remove keys, or re-pair the computer.
The old local connector may already have stopped while remote cleanup remains.

The current client waits for the connector's shutdown lock to be released, not
for a reply from a control listener that may already be closing. EOF, timeout,
or a missing listener alone is not accepted as proof that file access stopped.
Remote mount/session cleanup must also be confirmed before the saved grant is
marked retired. If setup still reports unconfirmed cleanup, stop and follow that
error; do not bypass it by deleting the saved record. Approve the prompt again
only when ready to end the old live-folder work. Files and history are retained.

## Daily use and native sessions

```bash
cd ~/code/my-project
ccfleet local --check
ccfleet start
```

`ccfleet start` (or `ccfleet menu`) offers local new, continue and resume choices.
`ccfleet sessions` opens the original Claude history picker. No native history is
imported or synchronized by these controls. `ccfleet local --new --name work`
remains the direct named-session command. Bare `ccfleet` still means the remote
terminal, also explicitly available as `ccfleet remote`; the menu does not change
those command meanings.

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

`ccfleet benchmark --iterations 3` compares ordinary and reused SSH readiness
connections without a model request. It reports connection timings, not model
inference performance. Foreground launches may explicitly opt into
`ccfleet local --reuse-transport`; reuse stays off by default. Each launch owns
its own pinned connection, which closes with its bridge on normal exit. A failed established
master does not open a fallback connection or replay the model request.
Managed background jobs currently use ordinary pinned SSH connections.
Killing only the foreground Python wrapper with `SIGKILL` can leave an idle SSH
master/proxy and private control directory behind, and does not guarantee that
native/tool children stopped. The idle master makes no model request by itself;
revoking that computer closes its broker transport. This is not the supervised
background-job crash guarantee. Reuse remains experimental and off by default.

These are native Claude sessions, not remote tmux sessions. `--resume` opens the
native history picker or selects an ID/name; `--continue` uses the last native
conversation for this directory. `--new --name NAME` starts a named conversation.
Plain `ccfleet local` also starts fresh: running it again does not automatically
resume the previous conversation or delete its history. Run these commands at
your ordinary shell prompt, not as a message inside Claude. Opening another
terminal starts an independent local session using the same assigned slot/account.
Use `/model` and `/effort` inside Claude. Defaults for a new conversation should
not overwrite a resumed conversation's saved model or effort unless requested.
Model/effort availability depends on the installed CLI and account.

Additional supported native arguments go after `--`. Options that would override
the protected model route or authentication are not an alternate CC Fleet mode.
Quit with Claude's `/exit` or the normal terminal controls, then use native resume
to continue later. `/exit` ends this local Claude process and its temporary relay;
saved native history remains. It does not end a different terminal's session.
An interrupted request is not guaranteed to have completed;
CC Fleet does not silently replay inference.

The relay supports foreground interactive sessions, `--print`, native resume and
multiple normal terminal sessions. Native `--bg` / `--background` forwarded after
`--` is rejected because it would bypass supervision. For managed noninteractive
local work, use the background job commands below; for parallel interactive work,
use another regular terminal.

## Project defaults

```bash
ccfleet preferences set --model opus --effort high --mode plan
ccfleet preferences show
ccfleet preferences clear
```

Preferences apply to the current project, or pass `--project PATH`. They are
stored privately on this computer, indexed by a hash of the project directory.
Explicit launch arguments win over defaults; resume does not implicitly replace
a conversation's saved model or effort. Clearing preferences does not erase native
history or change slot credentials. They do not change the remote tmux defaults.

## Diagnostics and reported account health

```bash
ccfleet status --json
ccfleet doctor --privacy --json --export ./ccfleet-support.json
```

Doctor separates installation, pairing, broker/relay and account-health checks.
These commands make no model request, scan no project, and do not automatically
repair settings, re-pair or erase history. A support export creates only the new
local file explicitly requested; it is not uploaded and excludes prompt/file
content, local paths, credentials and account email. Review it before sharing it.
An existing output file is not overwritten.

The website and status response distinguish **reported ready**, **renewal
pending**, **sign-in required**, **account maintenance** and unverified/stale
observations. A heartbeat does not prove provider acceptance. If sign-in is
required, use the existing Claude sign-in controls on your slot page; pairing
and local history are kept. `ccfleet local --check` separately tests current slot
relay readiness without starting a model conversation.

## Signed client updates and rollback

```bash
ccfleet version --json
ccfleet update
ccfleet update --rollback
```

The update path verifies an expiring stable-channel signature against the public
Ed25519 key pinned in the client. It then verifies an immutable manifest, full
source revision, every file checksum and the complete helper set. Redirected or
unapproved origins, unsigned/tampered manifests, stale channels and implicit
downgrades are refused. All files are checked before atomic launcher activation.

The previous client and helpers are retained for explicit rollback; pairing,
native history, project files and preferences are not changed. Version output
distinguishes a verified signed release from a bootstrap installation. A restored
legacy bootstrap can run but is not represented as signature-verified. If no
valid signed channel is published, update fails rather than falling back to an
unsigned download. Initial installation still requires trusting its HTTPS
bootstrap and pinned key; signing is not a sandbox or a guarantee against a
compromised trusted signing key. These commands update CC Fleet, not native Claude.

## Managed local background jobs

```bash
ccfleet jobs start --prompt "Review this project and summarize findings"
ccfleet jobs list
ccfleet jobs status JOB_ID
ccfleet jobs logs JOB_ID
ccfleet jobs logs JOB_ID --stream stderr
ccfleet jobs stop JOB_ID
ccfleet jobs archive JOB_ID
```

Use the job ID returned by start. `ccfleet local --background --print "PROMPT"`
is an equivalent supervised launch. `--prompt -` reads a prompt from standard
input. Use `--project PATH`, an explicit `--resume NAME_OR_ID`, or `--continue`
when needed. A background job cannot show the interactive resume picker.

These jobs run the original Claude CLI in **local print mode**. The detached
supervisor owns the foreground command's process group, authenticated control
socket, bounded logs and temporary model bridge. Work can continue after the
starting terminal closes while the computer remains running and connected.
This is not native `--bg` support, an attachable terminal, or a remote agent.

- Default duration: one hour; `--timeout SECONDS` permits at most 24 hours.
- Default concurrency: four; `--max-jobs` permits at most 16.
- Prompts, options, project paths and output stay in private local job files.
  Stdout and stderr are each capped at 4 MiB. Logs may contain sensitive content;
  read them only deliberately. Diagnostic exports do not include job logs.
- Stop, timeout, device revocation or supervisor loss ends the owned process
  group and bridge. Background authorization is rechecked periodically, not an
  instantaneous local-file-access guarantee. Unknown cleanup remains explicitly
  unconfirmed; a saved PID is never treated as proof of a live owned process.
- Each job remains bound to its starting device, slot and account assignment.
  Sign-in/account transitions or changed pairing stop old jobs instead of silently
  continuing under a different account. Start new work explicitly once ready.
- Jobs are never automatically restarted, and ambiguous inference is not replayed.
  Records are retained (up to 256), not silently deleted. Ask for recovery help if
  an interrupted record or history limit prevents new work.

Native tools can deliberately detach into another process group; supervision is
not an OS sandbox for arbitrary tool descendants. Original local permissions
still apply, particularly `bypassPermissions`. A stopped job cannot undo edits or
data already sent by its tools or model requests.

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

Raw upstream error bodies and opaque request IDs are not forwarded. Fixed error
categories retain actionable guidance and the native context-overflow signal;
detailed provider diagnostics are intentionally omitted. Successful response
content is not redacted. This does not prevent private information in model output.

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

### Check your installation without exposing existing files

From an ordinary local terminal, run:

```bash
ccfleet local --check
```

Success confirms that the original local Claude executable is available and the
assigned slot relay reports readiness. It does not scan a project, start Claude,
or make a model request, so it is not proof of an actual request's upstream route.

For a small filesystem check, paste this into a running `ccfleet local` session:

```text
Use only synthetic test data. Run mktemp -d /tmp/ccfleet-check.XXXXXX.
In that new directory, use Write to create probe.txt containing CCFLEET_LOCAL_OK.
Read it back with Read, then give me the exact cat command to run in a separate
ordinary local terminal. Do not inspect existing files, credentials, environment
variables, hostname, IP address or location. Do not make network requests or
delete anything. Do not claim this proves inference routing or metadata privacy.
```

Run the returned `cat` command yourself outside Claude. A matching marker confirms
the file is visible on your local filesystem. Claude's own read-back is not an
independent check; a mounted/shared filesystem could also pass, so this alone does
not prove where every tool process executes. Only the synthetic directory is
created, and it remains until you remove it. The test path and tool output can
become model context. Neither this test nor a readiness check proves zero metadata
disclosure; see the privacy section above and the
[release verification record](local-relay-verification.md).

### Operator verification

Relay access requires an upgraded, operator-enabled assigned slot. A working
remote terminal or old live mount does not prove relay readiness. Deployment and
general activation must be verified separately after a canary; this guide asserts
no deployed revision or provider approval. See [compliance notes](compliance.md)
for the historical source record and the distinction from hosted terminal use.

On an upgraded single-slot machine, root can opt into automatic relay access by
creating `/etc/ccfleet/inference-enabled` containing exactly `enabled` followed
by a newline (root-owned, mode 0644). This policy is separate from `agent.env`
and survives installer upgrades. The machine agent then grants access only to
the current claimed/active, held, unprivileged managed slot. Free, releasing,
absent and orphaned slots lose their gates before wipe/retry work; a later clean
claim receives a new gate automatically. Native sign-in and device pairing are
still required. No other account is used as a fallback.

Removing the opt-in immediately denies automatically managed gates, and the next
successful reconciliation removes them. To disable **all** inference gates,
including older manually created empty markers, set the policy to exactly
`disabled` followed by a newline. An absent policy preserves those legacy manual
markers for upgrade compatibility; it never creates new access. Do not recreate
per-user gates manually on an opted-in machine. These are operator actions, not
commands customers need to run.

Validate with synthetic settings and controlled upstreams: endpoint/auth override
precedence, removal of selected metadata, native session/history preservation,
streaming/truncation/cancellation, account binding, device revocation, native
credential renewal, and migration cancellation/cleanup. Use no customer files,
real credentials in logs, or unapproved model usage for these tests.
