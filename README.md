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
  token expiry and plan type; token values are dropped in memory and never
  sent, logged or stored.
- **A fleet server** with a dashboard and Telegram alerts: missing heartbeat,
  Claude Code missing or drifted from the pinned version, login missing, stale
  or expired, disk high, egress IP changed, Remote Control service down.
- **Profiles** (`ccp`) so one laptop can hold several accounts, each in its own
  `CLAUDE_CONFIG_DIR`, switched explicitly and never pooled.

What it deliberately does **not** do: proxy model traffic, store anyone's
credentials, rewrite headers or request bodies, pool or share accounts, or
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
```

`node add` prints the node's token once, plus the three lines to put in the
node's `agent.env`. Put a TLS proxy in front of `127.0.0.1:8110`
(`deploy/Caddyfile.example`) and open it with user `admin` and the admin token.
If you would rather not expose a public endpoint at all, nodes can report over an
SSH tunnel instead: see [docs/tunnel.md](docs/tunnel.md).

Without Docker: `pip install git+https://github.com/cdcupt/ccfleet` gives you
`ccfleetd` and `ccfleet-agent`; `deploy/ccfleetd.service` is a systemd unit.

### 2. Node (one VPS per owner, in a supported region)

```bash
# as root, once
curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/bootstrap.sh -o bootstrap.sh
sudo bash bootstrap.sh alice ~/.ssh/id_ed25519.pub

# as the owner
git clone https://github.com/cdcupt/ccfleet.git && ccfleet/node/setup-owner.sh
$EDITOR ~/.config/ccfleet/agent.env          # URL, node id, token from step 1
claude                                       # logging in already put you in the persistent session
                                             # /login with YOUR account, paste the code back
ccfleet-agent --print && systemctl --user start ccfleet-agent.service
```

The owner's `/status` should show their own account on the Login row and no
base URL or auth token. The dashboard shows the node within a minute.

### 3. Laptop (optional): several accounts, explicit switching

```bash
install -m 755 profiles/ccp ~/.local/bin/ccp
ccp add personal && ccp use personal          # /login once per profile
ccp add work --share && ccp list
```

## Layout


> Nodes need a working per-user systemd manager for the agent timer. On a minimal
> Debian or Ubuntu image that means `libpam-systemd` must be installed; `setup-owner.sh`
> checks for this and stops with instructions rather than enabling a timer that never runs.

| Path | What |
| --- | --- |
| `ccfleetd/` | fleet server (standard library only): API, store, rules, monitor, notifier, dashboard, CLI |
| `ccfleet_agent/agent.py` | single-file heartbeat agent for nodes |
| `node/` | bootstrap, owner setup, backup, egress probe, staged upgrade, systemd user units |
| `profiles/ccp` | per-account profile switcher for laptops |
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
uvx --from shellcheck-py shellcheck -S warning node/*.sh profiles/ccp
```

Python 3.9+, no runtime dependencies. The GitHub Actions workflow lives at
`deploy/ci/github-ci.yml`; move it to `.github/workflows/ci.yml` from a machine
whose token has the `workflow` scope (`gh auth refresh -h github.com -s workflow`).

## License

MIT. Not affiliated with or endorsed by Anthropic. You are responsible for
using Claude within Anthropic's terms, including the supported-regions policy.
