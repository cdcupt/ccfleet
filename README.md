# CC Fleet

CC Fleet gives each customer a private Linux slot running the original Claude
Code CLI under that customer's own Claude subscription.

The product rule is simple: **one holder, one slot, one Claude account**. CC
Fleet does not pool accounts, substitute credentials, rotate a request between
accounts, or expose a shared model API.

The customer experience is also one path: run the setup command, provide a
slot-page pairing code only if this computer is not already paired, then use
`ccfleet` in a normal terminal. Customers never type an SSH command and no Claude
credential is copied to their computer.

```text
customer terminal
    │  ccfleet (TLS WebSocket containing an encrypted SSH stream)
    ▼
CC Fleet broker on BWH
    │  opaque bytes; device is bound to one held slot
    ▼
assigned slot
    │  forced entrypoint → persistent tmux → original claude
    ▼
Anthropic
```

The broker authenticates the CC Fleet device token, but SSH remains encrypted
between the customer's computer and the slot. The broker cannot read the
terminal or project-transfer stream. Claude Code and its tools run on the slot,
and model connections originate there. Projects can be created there or copied
there through explicit project sharing. Anthropic receives relevant Claude
traffic from that slot, under the single Claude account signed in there.

## Customer flow

1. Sign in to the CC Fleet website with Google and claim an allowed slot.
2. Use the website's **Sign in to Claude** flow once. The resulting Claude
   credential is written only in that slot.
3. When the slot says **In use**, run the same setup command for new, existing,
   or legacy computers:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash -s -- --setup
   ```

4. Existing pairing is reused. Only if setup asks for a pairing code, press
   **Connect this computer** on [your slot page](https://ccfleet.daichenlab.com/account)
   and paste the fresh ten-minute, single-use code. Setup waits for device-key
   propagation and checks readiness without uploading project files or making
   a model request.
5. Open a new terminal, choose a project, and start Claude Code on the slot:

   ```bash
   cd ~/code/my-project
   ccfleet local
   ```

Review the selected files and confirm the first share. No local Claude
installation is needed. Plain `ccfleet` opens the ordinary remote workspace
without sharing a local project.

`ccfleet` is a thin terminal client. It does not run a second Claude process on
the customer's computer. Projects live in `~/workspace` on the slot; clone them
with Git or retrieve them from inside the slot. CC Fleet does not silently mount
or synchronize the customer's local directory.

Hosted sessions start Claude Code with `--dangerously-skip-permissions`, so tool
calls run without approval prompts as the slot Linux user. The slot has no sudo
or privileged groups, but Claude can still read, change, delete or transmit
anything that user can access inside the slot.

Each paired computer gets its own Ed25519 key and random CC Fleet access token.
The server stores the public key and only a hash of the access token. A customer
can remove one computer from the slot page without changing the slot's Claude
sign-in. Network interruptions reattach to the same tmux session.

The same `--setup` command handles migration from `ccfleet-connect`: legacy cleanup
runs only after readiness succeeds. It preserves existing pairing, configuration,
local history, and remote files, and backs up shell configuration when changing
PATH. Finish any active local-agent preview session before updating; setup does
not terminate it. It never automatically revokes an Anthropic credential. Revoke
an old setup-token yourself only when it is no longer used anywhere else.

Optional `--name "Personal Mac"` labels a new device; `--slot SLOT` chooses an
existing paired slot. Install-only (omit `--setup`), manual `ccfleet login`, and
legacy `--migrate` remain compatibility options, not extra steps for normal setup.

The default `ccfleet` session uses Opus, max effort and `bypassPermissions`.
Users can also create and resume named sessions with a model, effort and any
permission mode supported by the deployed Claude Code:

```bash
ccfleet new research --model fable --effort xhigh --mode plan
ccfleet attach --session research
ccfleet restart --session research --model opus --effort max --mode auto
```

Supported modes are `acceptEdits`, `auto`, `bypassPermissions`, `manual`,
`dontAsk`, and `plan`. Effort choices are `low`, `medium`, `high`, `xhigh`,
`max`, and `ultracode`. Inside the original Claude Code interface, `/model`
and `/effort` change the running session without losing its conversation.
`restart` deliberately ends the current Claude process before replacing it.

## Selected projects, slot-only execution

The operator-enabled `ccfleet local` project connector explicitly shares a
selected laptop project with the slot, then runs the original Claude Code and
all agent tools **on the slot**. No local Claude installation is required. This
replaces the retired local-agent inference-relay preview; it is not a model API
proxy. Project access is enabled for currently assigned hosted slots; newly
created or reassigned slots still need operator activation. Installing the client
or running its readiness check does not activate a slot.

```bash
cd ~/code/my-project
ccfleet local --check
ccfleet project status
ccfleet local --new --name work
```

Review and confirm the first-share file list. Resume the remote project session
with `ccfleet local --continue` or `ccfleet local --resume`. Use
`ccfleet project diff` and `ccfleet project pull` to review and apply slot changes
with backups and conflict checks. After editing locally, use
`ccfleet project push`; all active sessions for that project must first finish
with `/exit`. There is no background synchronization or laptop shell access.

Already-paired computers use the same `--setup` command without another pairing.
Pairing, remote files and slot sign-in stay in place. Old local-agent conversation
history remains local and is not imported into the slot.

Only selected relative filenames, bytes, hashes and executable flags are shared,
not automatically collected laptop environment or identity fields. Limits and
credential-name exclusions reduce accidental sharing but cannot detect all
secrets inside ordinary files. BWH sees connection IP/transport metadata; slot
root can inspect shared data; relevant project content reaches Anthropic. This
is not an anonymity guarantee or laptop OS sandbox.

See [project workspaces and migration](docs/project-workspaces.md) for selection,
limits, session controls, multiple computers, recovery and rollout verification.
The website guide has a dedicated `/docs/guide#migration` section.

## What runs where

- `ccfleetd/` is the web app, broker, desired-state service, operator console,
  public documentation and status page.
- `ccfleet_agent/machine.py` provisions and wipes Linux slot users, installs
  their device keys in a root-controlled sshd key directory and reports health facts.
- `node/slot-entry.sh` is the forced SSH entrypoint. It accepts only an
  allowlisted terminal/session command or operator-enabled bounded project
  protocol, never a client-supplied general shell command.
- `laptop/ccfleet` is the local client. It wraps OpenSSH behind the WebSocket
  broker, pins the slot's SSH host key and reconnects after ordinary network
  failures.
- `ccfleet_agent/project_files.py` defines the bounded snapshot format and safe
  file operations. The installer verifies a versioned copy beside the client.
- `ccfleet_agent/project_access.py` serves project snapshots and slot-native
  project sessions behind a separate operator gate.

The server receives operational facts such as versions, login state, account
fingerprints, quota percentages and hourly token counts. It does not receive a
slot's prompts, files, conversations or Claude credential. Machine
administrators have root and can technically read slot data; the public privacy
page states this boundary directly.

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
  not protect data from the machine that intentionally runs Claude Code.

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
| `docs/project-workspaces.md` | explicit project sharing and migration |
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
