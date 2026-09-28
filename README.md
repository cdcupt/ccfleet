# CC Fleet

CC Fleet gives each customer a private Linux slot running the original Claude
Code CLI under that customer's own Claude subscription.

The product rule is simple: **one holder, one slot, one Claude account**. CC
Fleet does not pool accounts, substitute credentials, rotate a request between
accounts, or expose a shared model API.

The customer experience is also one path: install `ccfleet`, pair it from the
slot page, then run `ccfleet` in a normal terminal. Customers never type an SSH
command and no Claude credential is copied to their computer.

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
terminal stream. Claude Code, project files, tools, model requests and responses
all remain on the slot. Anthropic receives Claude traffic from that slot, under
the single Claude account signed in there.

## Customer flow

1. Sign in to the CC Fleet website with Google and claim an allowed slot.
2. Use the website's **Sign in to Claude** flow once. The resulting Claude
   credential is written only in that slot.
3. When the slot says **In use**, press **Connect this computer**.
4. Install the client and pair with the ten-minute, single-use code:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh | bash
   ccfleet login
   ```

5. Open or resume Claude Code:

   ```bash
   ccfleet
   ```

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

Existing users of the removed `ccfleet-connect` token flow can install, pair,
and retire that local setup in one transaction-like command:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh \
  | bash -s -- --migrate
```

The old setup is touched only after new pairing succeeds. The user must still
revoke an old Anthropic setup-token in their Anthropic account if it is no
longer used anywhere.

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

## Experimental local projects

There is also an operator-gated `ccfleet local` prototype for
running the original Claude Code against laptop files with the assigned slot
handling model requests. It is off by default and is not the hosted terminal
workflow above. See [the implementation and release limits](docs/local-relay.md)
before enabling it; technical operation does not establish provider permission.

## What runs where

- `ccfleetd/` is the web app, broker, desired-state service, operator console,
  public documentation and status page.
- `ccfleet_agent/machine.py` provisions and wipes Linux slot users, installs
  their device keys in a root-controlled sshd key directory and reports health facts.
- `node/slot-entry.sh` is the forced SSH entrypoint. It accepts only an
  interactive terminal and attaches to the slot's persistent Claude Code
  session.
- `laptop/ccfleet` is the local client. It wraps OpenSSH behind the WebSocket
  broker, pins the slot's SSH host key and reconnects after ordinary network
  failures.

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
