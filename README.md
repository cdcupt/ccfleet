# ccfleet

**Own-account Claude Code fleet manager: one owner, one account, one node.**

ccfleet is for a small group of people who each pay for their own Claude
subscription and want to run the unmodified Claude Code CLI on a hosted node
they control, with someone keeping an eye on the whole fleet. It gives you:

- **Node scripts** that turn a fresh Ubuntu VPS into a hardened, single-owner
  Claude Code host (keys-only SSH, firewall, unattended upgrades, auto-updater
  off so upgrades are staged, tmux, optional Claude Code Remote Control).
- **A heartbeat agent** on each node that reports public facts (Claude Code
  version, uptime, disk, load, egress IP, whether a login exists and when it
  was last refreshed). It parses the credentials file only to extract the
  token expiry and plan type, and `~/.claude.json` only for whether an account
  is signed in, when its profile was last fetched and the rate-limit tier. The
  latter is the only way to report a login on macOS, where the credential lives
  in the Keychain and this agent will not read it. Token values are dropped in
  memory and never sent, logged or stored, and neither are the email address,
  name, account uuid or organisation name that sit in the same file. From the
  uuid it derives one thing: a fingerprint (the first 16 hex digits of its
  SHA-256), the same on every node the account is on, so the server can flag one
  account signed in on two nodes. (A slot on a shared machine reports one thing
  more, for its holder's own page: the email address of the one Claude account
  signed in on it. See section 4.)
- **A fleet server** with a dashboard and Telegram alerts: missing heartbeat,
  Claude Code missing or drifted from the pinned version, login missing, stale
  or expired, disk high, egress IP changed, Remote Control service down.

What it deliberately does **not** do: proxy model traffic, store anyone's
credentials, substitute a credential, alter the client's identity, pool or share accounts, or
fail over one session across accounts. Every request goes from the unmodified
Claude Code binary, signed in by its owner through Anthropic's own flow,
straight to Anthropic. The one optional component that sits on the request
path, the pass-through gateway in `gateway/`, forwards an owner's own requests
unchanged (including their own OAuth header, in transit) and stores nothing.
See [docs/compliance.md](docs/compliance.md) for the reasoning and the exact
passages of Anthropic's documentation it follows.

```
 Owner A ──ssh / Remote Control──▶ Node A (unmodified claude, login A) ──▶ api.anthropic.com
 Owner B ──ssh / Remote Control──▶ Node B (unmodified claude, login B) ──▶ api.anthropic.com
                                       │ heartbeat (facts only, no secrets)
                                       ▼
                               ccfleetd: dashboard + alerts (read-only)
```

## Quickstart

Full walkthrough with diagrams and the output to expect: **[docs/guidebook.html](docs/guidebook.html)**.

### 1. Fleet server (any small box, behind TLS)

```bash
git clone https://github.com/cdcupt/ccfleet.git && cd ccfleet
cp deploy/ccfleetd.env.example deploy/ccfleetd.env   # set CCFLEET_ADMIN_TOKEN (openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec ccfleetd ccfleetd node add node-a --owner alice --region us-west

# optional: give alice her own console login, so she can see her node's health
docker compose -f deploy/docker-compose.yml exec ccfleetd ccfleetd user add alice --owner alice
```

`node add` prints the node's token once, plus the three lines to put in the
node's `agent.env`. Put a TLS proxy in front of `127.0.0.1:8110`
(`deploy/Caddyfile.example`) and open `/admin` on it with user `admin` and the admin
token; the bare address belongs to the people who use the product. To give an owner a read-only view of their own nodes, add a named account with `ccfleetd user add NAME --owner OWNER`.
If you would rather not expose a public endpoint at all, nodes can report over an
SSH tunnel instead: see [docs/tunnel.md](docs/tunnel.md).

Without Docker: `pip install git+https://github.com/cdcupt/ccfleet` gives you
`ccfleetd` and `ccfleet-agent`; `deploy/ccfleetd.service` is a systemd unit.

### 2. Node (one VPS per owner, in a supported region)

Add the person in the console. It hands you one command carrying their node's
identity. Run it on a fresh server as root:

```bash
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/install.sh \
  | sudo bash -s -- \
      --server https://fleet.example.com \
      --node alice-node --token <from the console> \
      --owner alice --ssh-key "ssh-ed25519 AAAA... alice"
```

It installs packages, creates the owner, hardens SSH and the firewall, installs
Claude Code, pre-answers the two setup prompts, starts the agent and the
persistent work session, and sends a first heartbeat. Then it stops. It enables
Remote Control but cannot start it, because that needs a login that does not
exist yet; the owner starts it below.

Without `--ssh-key` it skips SSH hardening rather than risk locking everyone out.
On a machine already running other services, add `--skip-harden`.

The owner finishes it themselves, on that machine:

```bash
claude          # choose the claude.ai login, approve in a browser, paste the code back
/status         # confirms their account, no base URL, no auth token
systemctl --user start claude-remote-control.service   # once, to enable claude.ai access
```

Nobody else can do that step: a subscription login must complete through
Anthropic's own flow. After it, they work from a terminal (`ssh` lands them in a
live session) or from claude.ai/code and the phone app with nothing installed.

### 3. Laptop (optional): one Claude account per computer

A computer uses one Claude account, connected with that account's token through
`ccfleet-connect`; several accounts means several slots, one for each.

With a device token (inference only, so no Remote Control),
`ccfleet-connect` wires a computer to one Claude account: the one on your slot,
which you can use from as many of your own computers as you like. A computer
holds one account; connecting it again with another token replaces the one it
had. A computer that saved several under names with an earlier version keeps
the one in use and has the others wiped, the first time it runs.

```bash
ccfleet-connect             # paste the token from `claude setup-token` (input hidden)
ccfleet-connect --status    # which token, and whether it still works
ccfleet-connect --remove    # undo it
```

### 4. Shared machines: one machine, one slot, named after its holder

A shared machine carries one **slot**: its own Linux user, with its own home,
its own Claude Code and its own Claude sign-in, made by the person who holds it
with their own Claude account, so no credential is ever shared between people.
One machine is one slot because claude.ai/code shows a machine by its hostname:
when somebody claims the slot it is named after them (`alice-1`, from the part
of their address before the @, or a handle the operator sets with
`ccfleetd account handle <email> <handle>`), the machine takes that name as its
hostname, and the name goes when the slot is freed. People sign in to ccfleet with Google, which is asked only for
the `openid email` scopes: ccfleet keeps the address and Google's stable account
id, which is what an account is keyed on because addresses change. The operator
grants each person an allowance of slots, and they claim one, sign it in to
their own Claude account and give it back from `/account`. One Claude account
per slot: somebody with two accounts holds two slots. Their page shows which
account each slot is signed in to, so the slot reports that account's email
address; the console never shows it. A slot keeps the account it was first
signed in with: signing in again happens in a scratch directory and is kept only
if it is that same account. That keeps a sign-in with the wrong account from
landing by accident; it is not a wall against the holder, whose home the slot
is and who can change its files. So a slot found signed in to another account
some other way raises `account_changed` once Claude Code's profile says so. And an account signed in on two live places at once (two
slots, or a slot and a node) raises `account_elsewhere` on both. The holder's
card says so either way. An owner's own node can count as a slot they hold,
`ccfleetd node hold <node> <email>`, so everything a person uses is one list
on their page: a record only, never handed out or wiped. A shared machine can
be kept for one account, `ccfleetd node reserve <machine> <email>`: its free
slot then goes to that account and nobody else, and `--none` opens it again.
The operator sets the price of a slot for a month in the console's Price card,
or with `ccfleetd price set 20 USD`, and the public pages show it; it is shown,
never charged, and the allowance stays the only thing that grants a slot.
The guidebook's chapter 11 covers the same ground for the operator, step by
step.

```bash
# fleet server: Google sign-in needs an OAuth "Web application" client whose
# redirect URI is <CCFLEET_PUBLIC_URL>/auth/google/callback, and in ccfleetd.env
#   CCFLEET_GOOGLE_CLIENT_ID=...  CCFLEET_GOOGLE_CLIENT_SECRET=...
#   CCFLEET_COOKIE_SECRET=<openssl rand -hex 32>
ccfleetd node add pool-1 --owner ops --region us-west            # prints the machine's token once
ccfleetd slot add pool-1 --machine pool-1 --unix-user slot01     # its one slot, named after it

# the machine, as root. Harden it first with bootstrap.sh: keys-only SSH, the
# firewall, unattended upgrades, and a login for you, but no agent. Not install.sh,
# which turns the box into one owner's node, and machine-setup.sh refuses to run
# beside that node's agent.
git clone https://github.com/cdcupt/ccfleet.git && sudo ./ccfleet/node/bootstrap.sh ops "ssh-ed25519 AAAA... you"
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/machine-setup.sh \
  | sudo bash -s -- --server https://fleet.example.com --node pool-1 --token <64-hex>

# once somebody has signed in with Google
ccfleetd account quota alice@example.com 1                        # their allowance; zero until granted
ccfleetd account role you@example.com admin                       # an operator, who signs in with Google
ccfleetd payment add alice@example.com 30 USD 2026-10-31          # a record for you; it enforces nothing
```

The rules the rest depends on:

- **A slot is only handed out once the machine has confirmed its Linux user is
  absent**, and it is free again only after the machine confirms the wipe.
  Giving a slot back deletes everything in it; the person's Claude account
  itself is untouched.
- **Lowering an allowance takes nothing away.** It only stops more claiming.
  Taking a held slot back is a release, with the wipe that implies; the console
  asks for the slot's id to be typed.
- **Nothing acts as a user.** The console can take a slot back; it cannot sign
  in on anybody's behalf, type their code, or read their device token.
- **Payments are a record, not a gate.** The console shows who is paid through
  when, and marks a lapse in red while that person still holds or may claim
  slots. A lapse takes no slot and stops no claim; what to do about it is yours.
- **The console is at `/admin`; the bare address is the product's.** It takes
  everybody to their own page: an operator to the console, a signed-in customer
  to their slots, anybody else to what ccfleet is and how to start.
  `CCFLEET_ADMIN_HOST=admin.fleet.example.com` moves the console to its own
  hostname instead, with its own Google redirect URI and its own sessions;
  every other hostname is then only the product.

What to send the people who buy slots: the product site serves public pages
for them — `/docs` (what it is, what they need, how to buy), `/docs/guide`,
`/docs/how-it-works`, `/docs/terms` and `/privacy`. Set `CCFLEET_CONTACT_EMAIL`
to publish an address on them; without it they say to ask whoever sent the link.

Keeping it up to date:

- **Claude Code in slots** follows the machine's pin, which the server sends
  with every reply: `ccfleetd node pin <machine> stable` tracks Anthropic's
  stable channel, and an exact version holds the whole machine back. The
  machine agent installs it in each slot at a quiet moment and never interrupts
  a running session.
- **The agents** change only when ccfleet does. Roll them out after deploying
  the server: owner nodes re-fetch `ccfleet_agent/agent.py`, shared machines
  re-run `machine-setup.sh` pointed at the same commit
  (`CCFLEET_REPO_RAW=https://raw.githubusercontent.com/cdcupt/ccfleet/<commit>`).
  One node first, then the rest.
- **The OS** installs security updates daily; a machine that needs a reboot says
  so in the console.

Adding another machine is the runbook [Add a shared machine](docs/runbooks.md).

## Layout


> Nodes need a working per-user systemd manager for the agent timer. On a minimal
> Debian or Ubuntu image that means `libpam-systemd` must be installed; `setup-owner.sh`
> checks for this and stops with instructions rather than enabling a timer that never runs.

| Path | What |
| --- | --- |
| `ccfleetd/` | fleet server (standard library only): API, store, rules, monitor, notifier, dashboard, user site, customer docs, payments ledger, CLI |
| `ccfleet_agent/agent.py` | single-file heartbeat agent for nodes |
| `ccfleet_agent/machine.py` | the shared machine's agent: runs as root, adds and wipes slot users, reports every slot |
| `node/` | bootstrap, owner setup, backup, egress probe, staged upgrade, systemd user units; `machine-setup.sh`, `slot-add.sh`, `slot-remove.sh` for shared machines |
| `gateway/` | optional pass-through gateway (Caddy), for owners who must keep files local |
| `docs/tunnel.md` | reporting over an SSH tunnel when the server has no public endpoint |
| `deploy/` | Dockerfile, compose, systemd unit, Caddy TLS example, CI workflow |
| `docs/` | guidebook, design, compliance notes, runbooks |
| `tests/` | pytest suite (`uv run --with pytest --with pytest-cov pytest --cov`) |

## Alert rules

| Rule | Level | Fires when |
| --- | --- | --- |
| `no_heartbeat` | critical | no heartbeat for 15 min (`CCFLEET_HEARTBEAT_MAX_AGE_S`) |
| `claude_missing` | critical | `claude` not found on the owner's PATH |
| `version_mismatch` | warn | running version differs from `ccfleetd node pin` |
| `credentials_missing` | critical | no login on the node: owner must run `claude` and `/login` |
| `token_stale` | warn | credentials file not refreshed for 24 h (`CCFLEET_TOKEN_STALE_S`) |
| `token_expired` | warn | access token expired over an hour ago and was not refreshed |
| `disk_high` | warn / critical | disk at 85% / 95% |
| `egress_changed` | warn | public IP differs from the previous heartbeat |
| `remote_control_down` | warn | node has Remote Control alerting on and the service is not active (`node add --rc-expected`, or `node rc-expected <id> on\|off` later) |

Alerts open once, close when the condition clears, and re-open on level change.
Each transition is logged and, when configured, sent to Telegram.

## Development

```bash
uv run --python 3.12 --with pytest --with pytest-cov --with ruff --no-project -- ruff check .
uv run --python 3.12 --with pytest --with pytest-cov --no-project -- pytest --cov
uvx --from shellcheck-py shellcheck -S warning node/*.sh node/attach.sh
```

Python 3.9+, no runtime dependencies. CI runs on every push and pull request
(`.github/workflows/ci.yml`): the test suite on 3.9, 3.12 and 3.13 with coverage
held above 80%, shellcheck and `bash -n` over every shell file, and a parse check
of the systemd units and the launchd plist. The unit check is there because
several of this project's real bugs were unit-file mistakes.

The shell job selects files by shebang **or** by a `# shellcheck shell=`
directive, so `node/attach.sh`, which is sourced rather than executed and has no
shebang, is still checked.

## License

MIT. Not affiliated with or endorsed by Anthropic. You are responsible for
using Claude within Anthropic's terms, including the supported-regions policy.
