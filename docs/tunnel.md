# Reporting over an SSH tunnel

By default a node posts heartbeats to the fleet server over HTTPS, which means
the server needs a public name, a DNS record and a certificate. When you do not
want any of that, a node can reach the server through an SSH tunnel instead.

Use this when the server has no public endpoint, when you would rather not
expose one, or when a node sits behind NAT. The trade-off is one more service
per node and an SSH account on the server.

```
node                                              fleet server
┌──────────────────────────────┐                  ┌──────────────────────────┐
│ ccfleet-agent                │                  │ ccfleetd                 │
│   POST http://127.0.0.1:8110 │                  │   listens 127.0.0.1:8110 │
│            │                 │                  │            ▲             │
│            ▼                 │   ssh -L, port   │            │             │
│ ccfleet-tunnel.service ──────┼──────────────────┼────────────┘             │
└──────────────────────────────┘   forwarding     └──────────────────────────┘
```

SSH provides the encryption, so the agent talking plain HTTP to its own loopback
address is fine.

**What this does and does not hide.** ccfleetd's HTTP endpoint stays private: it
binds loopback, there is no vhost, no DNS record and no certificate, and nothing
outside the server can reach it. The server does still need a **reachable SSH
port**, which is the one public surface this design depends on. Harden it the
way you would any SSH server: keys only, no password authentication, and
ideally a firewall that admits only your nodes' addresses.

## On the server, once

Create an account for the tunnel. It is confined by the authorized_keys options
below rather than by its shell, because a forced command needs a working shell
to be executed by.

```bash
sudo useradd --create-home --shell /bin/sh ccfleet-tunnel
sudo install -d -m 700 -o ccfleet-tunnel -g ccfleet-tunnel /home/ccfleet-tunnel/.ssh
```

## Per node

Generate a key for that node only, then authorize it with the narrowest options
OpenSSH offers.

```bash
# on the node, as the owner
ssh-keygen -t ed25519 -N "" -f ~/.ssh/ccfleet_tunnel -C "ccfleet-tunnel-$(hostname)"
cat ~/.ssh/ccfleet_tunnel.pub
```

```bash
# on the server, paste that public key as one line
echo 'restrict,port-forwarding,permitopen="127.0.0.1:8110",command="/bin/false" ssh-ed25519 AAAA... ccfleet-tunnel-node-a' \
  | sudo tee -a /home/ccfleet-tunnel/.ssh/authorized_keys
sudo chmod 600 /home/ccfleet-tunnel/.ssh/authorized_keys
sudo chown ccfleet-tunnel:ccfleet-tunnel /home/ccfleet-tunnel/.ssh/authorized_keys
```

Each option is doing a job, and dropping any of them widens the key:

| Option | Why |
| --- | --- |
| `restrict` | Turns off every forwarding type, PTY allocation and user rc files |
| `port-forwarding` | Turns just forwarding back on, since `restrict` disabled it |
| `permitopen="127.0.0.1:8110"` | The only destination the key may open, so it cannot reach anything else on the server or the network behind it |
| `command="/bin/false"` | `restrict` does **not** stop command execution. Without this, anyone holding the key could run `ssh ccfleet-tunnel@server 'some command'`. A forced command does not interfere with port forwarding, which uses a separate channel |

### Pin the server's host key

The tunnel runs unattended and carries the node token, so trust-on-first-use is
not good enough: a server impersonating yours at the moment of first connection
would receive that token. The unit therefore runs with strict checking against
its own known-hosts file, and refuses to connect if the key does not match.

```bash
# on the node
ssh-keyscan -t ed25519 fleet.example.com > ~/.config/ccfleet/tunnel_known_hosts
ssh-keygen -lf ~/.config/ccfleet/tunnel_known_hosts
```

Compare that fingerprint against the one the server reports, read over a channel
that is not the connection you are trying to establish:

```bash
# on the server, out of band
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

If they differ, stop and find out why. If they match, the file is now the pin.
Rotating the server's host key later means updating this file on every node.

Back on the node, configure the tunnel and point the agent at it:

```bash
cp ~/ccfleet/node/tunnel.env.example ~/.config/ccfleet/tunnel.env
chmod 600 ~/.config/ccfleet/tunnel.env
$EDITOR ~/.config/ccfleet/tunnel.env     # SSH destination, ports, key path

# the agent now posts to the tunnel instead of a public URL
sed -i 's|^CCFLEET_URL=.*|CCFLEET_URL=http://127.0.0.1:8110|' ~/.config/ccfleet/agent.env

systemctl --user enable --now ccfleet-tunnel.service
systemctl --user start ccfleet-agent.service
```

`setup-owner.sh` installs the tunnel unit alongside the others, so on a node it
set up you only need the two files above.

## Checking it

```bash
systemctl --user status ccfleet-tunnel.service
curl -s http://127.0.0.1:8110/healthz        # {"ok": true} means the tunnel is up
journalctl --user -u ccfleet-agent.service -n 5
```

## Notes

- The tunnel unit restarts on failure every fifteen seconds and uses SSH
  keep-alives, so a dropped link comes back by itself. The agent also retries
  its own POST, so a short outage costs nothing.
- `ExitOnForwardFailure=yes` means the unit fails loudly rather than sitting
  there with no forward, which is what you want a restart to act on.
- Use a different local port on a node that already runs something on 8110.
  Set `CCFLEET_TUNNEL_LOCAL_PORT` and match `CCFLEET_URL` to it.
- One key per node. Revoke a node by deleting its line from the server's
  `authorized_keys`; that is independent of rotating its ccfleet node token.
- ccfleetd still binds loopback only, so this adds no public HTTP surface. The
  SSH port is the surface it does depend on; see the note at the top.
- A `Permission denied` at startup after a host-key rotation is the pin doing
  its job. Re-verify the fingerprint out of band before updating the file.
