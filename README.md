# CC Fleet

CC Fleet pairs each user with a dedicated Linux slot and that user's own Claude
account. **One holder, one slot, one Claude account:** no account pool, account
rotation, credential fallback, or shared inference account.

The primary command, `ccfleet local`, runs the original Claude Code CLI on the
user's computer. Files, tools, settings and conversation history stay in their
native local locations. Supported model requests travel through the assigned
slot; its Claude credential is never returned to the laptop or BWH.

```text
local original Claude Code → authenticated loopback bridge
    → pinned SSH carried through the BWH WSS broker
    → assigned slot's inference relay → Anthropic
```

BWH cannot decrypt the inner SSH stream. The slot relay can read model requests
and uses only its bound account. This is an inference relay, not proof of
slot-only agent execution or metadata-free requests. Plain `ccfleet` remains a
remote-terminal compatibility command with remote files and persistent tmux.

This implements the requested local-CLI/dedicated-slot relay workflow; it is not
a claim of complete CC Host feature parity or zero metadata disclosure. See the
[verified release scope and test evidence](docs/local-relay-verification.md).
The [reliability and privacy design](docs/reliability-privacy.md) explains expiry-driven
native renewal, identity minimization, verified TLS, and per-device transport.

## Customer setup and migration

1. Sign in on the website, claim an allowed slot, and complete **Sign in to
   Claude** with your own account. That credential remains on the slot.
2. Run the same setup command for a new or existing computer:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash -s -- --setup
   ```

3. Existing pairing and native Claude installation are reused. If the original
   local Claude CLI is missing, setup installs it from the fixed vendor URL.
   Only if asked, obtain one fresh ten-minute pairing code from **Connect this
   computer** on [your slot page](https://ccfleet.daichenlab.com/account).
4. If an older live-folder grant is active, finish its work or explicitly approve
   stopping its background connector and associated remote live-folder sessions.
   Setup asks in the controlling terminal; `--yes` explicitly authorizes this
   cancellation for scripts. Files/history and ordinary remote tmux sessions are
   kept. A failed cleanup must be resolved, not silently ignored.
5. Open a new terminal and run native Claude in your project:

   ```bash
   cd ~/code/my-project
   ccfleet start
   ```

Setup checks readiness without uploading project files or making a model request.
It preserves configuration/history and backs up shell settings when adjusting
PATH. Legacy `ccfleet-connect` cleanup happens only after readiness succeeds.
It does not automatically revoke an Anthropic credential: revoke an old
setup-token yourself only if it is no longer used anywhere else.

Optional `--name "Personal Mac"` labels a new device; `--slot SLOT` chooses an
existing pairing. Install-only (omit `--setup`), manual `ccfleet login`, and
legacy `--migrate` remain compatibility options. No SSH command or slot Claude
credential needs to be entered locally.

`ccfleet start` offers local new/continue/resume choices; `ccfleet local` remains
the direct launch command. Bare `ccfleet` has not changed meaning: it still opens
the remote terminal. `ccfleet remote` is the explicit remote alias.

## Native local files, sessions and settings

`ccfleet local` does not upload or mount a folder and adds no filesystem count,
size or Git-ignore filters. `cd ~` works too. The working directory is not a
sandbox: original Claude's native permissions govern local file and tool access.
New conversations default to `bypassPermissions`, Opus and max effort, so local
tools can modify, delete or transmit data without individual approval prompts.

```bash
ccfleet local --check
ccfleet local --new --name work
ccfleet local --new --name research --mode plan --model opus --effort high
ccfleet local --resume
ccfleet local --resume work
ccfleet local --continue
ccfleet local --resume work --fork-session
ccfleet local --print "Summarize this project"
ccfleet sessions
ccfleet preferences set --model opus --effort high --mode plan
ccfleet preferences show
ccfleet preferences clear
```

Running plain `ccfleet local` again starts a fresh conversation in the current
directory; it does not automatically resume the previous one. Use `--continue`
for the latest conversation in that directory, or `--resume` for the history
picker. Existing history is kept. A second terminal opens an independent local
session using the same assigned slot/account.

These are native local conversations, not remote tmux sessions. Use `/model` and
`/effort` in Claude; availability depends on the installed CLI and account.
Additional supported native arguments go after `--`, subject to protected
routing/authentication settings. Quit normally with `/exit`; resume saved local
history later. CC Fleet does not silently replay interrupted inference.
Interactive foreground sessions, `--print`, `--resume`, and multiple terminal
sessions are supported. `ccfleet sessions` opens native history; project preferences
affect new local sessions, with explicit flags taking precedence. They do not
rewrite existing conversations or synchronize history between computers.

## Diagnostics, signed updates and background work

```bash
ccfleet status --json
ccfleet doctor --privacy --json --export ./ccfleet-support.json
ccfleet version --json
ccfleet update
ccfleet update --rollback
```

Diagnostics make no model request and do not scan a project. Export creates only
the requested new private local file; it is not uploaded and excludes prompts,
file contents, paths, tokens and account emails. Reported account health separates
ready, renewal pending, sign-in required, account maintenance and stale observations.
A heartbeat does not prove that Anthropic will accept the next model request.

The updater authenticates its signed release channel using the pinned Ed25519 key,
then checks the immutable manifest and every digest-pinned helper before atomic
activation. Failed verification leaves the current client in place. Rollback
preserves pairing and native history; restored legacy bootstraps are not labelled
signature-verified. `version --json` distinguishes verified and bootstrap installs.
This updates CC Fleet, not the original Claude executable.

```bash
ccfleet jobs start --prompt "Review this project and summarize findings"
ccfleet jobs list
ccfleet jobs status JOB_ID
ccfleet jobs logs JOB_ID
ccfleet jobs stop JOB_ID
# Equivalent supervised launch:
ccfleet local --background --print "Summarize this project"
```

These are managed **local print-mode jobs**, not native detached `--bg`, remote
agents or interactive attachable sessions. The supervisor owns the bridge after
the starting terminal closes; the computer must stay running and connected. Use
an explicit `--resume NAME_OR_ID` or `--continue` for existing context. Passing
native `--bg` / `--background` after `--` remains rejected.

Jobs default to one hour (`--timeout`, maximum 24 hours) and four concurrent jobs
(`--max-jobs`, maximum 16). Private stdout/stderr logs are each bounded to 4 MiB;
they may contain sensitive content and are displayed only on request. Stop,
timeout, revocation and supervisor loss close the owned process group/bridge;
uncertain cleanup is reported as such. Deliberately detached tool processes are
not sandbox-contained. Jobs never automatically restart or replay inference.

Native settings/history, including an existing `CLAUDE_CONFIG_DIR`, are preserved
by default. `--legacy-history` selects the earlier per-slot relay-preview profile
without importing or deleting it. A temporary private settings overlay pins the
loopback model route and nonce, clears conflicting provider/auth overrides, and
disables supported optional telemetry without permanently editing native settings.

The previous live-folder mount is retired. `ccfleet local --disconnect` remains
an explicit cleanup action for it; `--reset-link` is retired. Earlier snapshot
commands are only for recovery. See [local relay and migration](docs/local-relay.md)
for shutdown-error recovery and a safe local-file check, and
[retired workspaces](docs/project-workspaces.md). The website migration guide
is at `/docs/guide#migration`.

## Privacy and routing scope

The relay removes selected headers and the top-level structured `metadata` field.
It does not redact arbitrary prompts or tool results. Native system prompts can
include OS, working-directory and environment details; file contents and paths
can identify users and reach the slot and Anthropic. Do not claim fingerprint-free
requests or that Anthropic sees only slot information.

Only supported model endpoints use the relay. MCP servers, hooks, plugins, shell
tools, updates and other native CLI services can connect directly from the
laptop. Optional telemetry controls are not an all-traffic firewall or anonymity
guarantee. BWH sees connection IP/transport metadata. Slot root can inspect or
alter requests/responses, which can influence local tool actions. Native local
permissions and trust in the slot host remain important.

Relay access requires upgraded nodes and operator activation. A previous remote
terminal or live-mount deployment does not establish relay availability or
provider authorization; validate the canary and actual deployment separately.
The [compliance record](docs/compliance.md) distinguishes the historical hosted
terminal mapping from the current inference relay; it makes no approval claim.

## Remote-terminal compatibility

Plain `ccfleet` still opens original Claude Code on the slot, with projects in
`~/workspace` and persistent tmux. The default remains Opus, max effort and
`bypassPermissions`, as the slot Linux user. Named remote sessions remain:

```bash
ccfleet new research --model fable --effort xhigh --mode plan
ccfleet attach --session research
ccfleet restart --session research --model opus --effort max --mode auto
```

These commands do not operate on local Claude conversation history. An intentional
remote `restart` terminates that session before replacing it.

## What runs where

- `laptop/ccfleet` starts native local Claude and a launch-scoped loopback bridge;
  it also retains the remote-terminal compatibility commands.
- `ccfleet_agent/inference_client.py` implements the authenticated local model
  transport; `ccfleet_agent/local_relay.py` handles fixed-upstream slot inference.
- `ccfleetd/` handles the website, pairing, device authorization, operational state
  and encrypted byte broker, not plaintext model bodies or slot credentials.
- `ccfleet_agent/machine.py` manages slot users and root-owned device keys.
- `node/slot-entry.sh` dispatches only fixed protocols with forwarding disabled.
- `project_files.py`, `project_access.py`, and the live-folder helpers retain
  legacy recovery/cleanup responsibilities; they are not the local agent.

## Server quickstart

Run the server behind TLS:

```bash
git clone https://github.com/cdcupt/ccfleet.git
cd ccfleet
cp deploy/ccfleetd.env.example deploy/ccfleetd.env
# Set CCFLEET_ADMIN_TOKEN, CCFLEET_PUBLIC_URL, Google OAuth and cookie secrets.
docker compose -f deploy/docker-compose.yml up -d --build
```

The bare public URL serves the product website. `/account` is the customer
page; `/admin` is the operator console. `CCFLEET_ADMIN_HOST` can move the
console to a separate hostname.

Create a shared machine and its slots:

```bash
ccfleetd node add pool-1 --owner ops --region us-west
ccfleetd slot add pool-1 --machine pool-1 --unix-user slot01
```

On a fresh Ubuntu machine, first install base hardening and then the shared
machine agent:

```bash
git clone https://github.com/cdcupt/ccfleet.git
sudo ./ccfleet/node/bootstrap.sh ops "ssh-ed25519 AAAA... operator"
sudo hostnamectl set-hostname pool-1
sudo timedatectl set-timezone Etc/UTC

curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/machine-setup.sh \
  | sudo bash -s -- \
      --server https://fleet.example.com \
      --node pool-1 \
      --token <node-token>
```

Configure the fixed endpoint the BWH server uses to reach that machine. Copy
the node's public SSH host key to a file on the server first:

```bash
ccfleetd node access pool-1 \
  --host <address-reachable-from-bwh> \
  --port 22 \
  --host-key-file /secure/path/pool-1-ssh_host_ed25519_key.pub
```

Only the broker needs network access to the node's SSH port. The customer uses
the public HTTPS/WSS service and never receives the node address.

Grant a signed-in customer an allowance:

```bash
ccfleetd account quota alice@example.com 1
ccfleetd account role you@example.com admin
```

## Lifecycle rules

- A slot is offered only after its machine confirms the previous Linux user is
  absent.
- Releasing a slot immediately revokes all paired CC Fleet devices, stops its
  processes and deletes the Linux user and files. The slot becomes free only
  after the machine confirms the wipe.
- A slot keeps one Claude account. **Sign in again** accepts only that account;
  **Change account** replaces it deliberately and at most once a week.
- One Claude account detected on two live fleet slots raises an alert.
- Lowering an allowance does not seize an existing slot.
- Payments are records for the operator; the allowance remains the grant.
- Claude Code updates are staged and never interrupt the running persistent
  session. New sessions use the installed version.

The public site documents the current product at `/`, `/docs/guide`,
`/docs/how-it-works`, `/docs/terms` and `/privacy`.

## Security and privacy boundary

- The outer connection is TLS/WSS to the broker.
- The inner OpenSSH connection is encrypted end-to-end and pins the slot host
  key returned at pairing.
- Device keys live outside holder-writable home directories. Both sshd and each
  key enforce the forced entrypoint, with no agent forwarding, port forwarding,
  X11 forwarding or user-supplied SSH command.
- The public-key path is root-owned but readable by slot users (`0711` parent,
  `0755` key directory, `0644` files); the node token remains in an atomically
  written root-only `0600` file.
- Slot accounts receive a discarded random password only so OpenSSH/PAM will
  evaluate public keys; the slot sshd policy explicitly disables password and
  keyboard-interactive authentication.
- Pairing codes are random, single-use, stored only as hashes and expire after
  ten minutes.
- Device access tokens are random and stored only as hashes. Device removal or
  slot release revokes them immediately.
- The broker chooses the node endpoint from operator configuration; the client
  cannot use it as an arbitrary TCP proxy.
- The local client stores its private key and access token under
  `~/.config/ccfleet` with mode `0600`.
- The slot administrator still has root. End-to-end transport encryption does
  not hide plaintext model requests/responses from the slot relay endpoint.

## Operations

Agents report heartbeat facts and the server raises alerts for missing nodes,
missing or stale Claude login, version drift, disk pressure and changed egress.
Legacy owner-node and Remote Control monitoring remains in the code for owner
installations. Hosted customer slots actively disable the old Remote Control
unit; it is not an alternative product mode.

Useful paths:

| Path | Purpose |
| --- | --- |
| `docs/design.md` | architecture and trust boundaries |
| `docs/runbooks.md` | operator procedures |
| `docs/compliance.md` | account and credential constraints |
| `docs/local-relay.md` | native local Claude, slot inference, privacy and migration |
| `docs/project-workspaces.md` | retired mount/snapshot cleanup and recovery |
| `docs/tunnel.md` | optional heartbeat reporting tunnel |
| `deploy/` | container, systemd and TLS examples |
| `tests/` | unit and integration test suite |

## Development

```bash
uv run --python 3.12 --with ruff --no-project -- ruff check .
uv run --python 3.12 --with pytest --with pytest-cov --no-project -- pytest --cov
uvx --from shellcheck-py shellcheck -S warning node/*.sh laptop/*.sh
```

Python 3.9+; the runtime server uses only the standard library.

## License

MIT — see [LICENSE](LICENSE).
