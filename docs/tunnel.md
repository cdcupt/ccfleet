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

Nothing listens publicly, and SSH provides the encryption, so the agent talking
plain HTTP to its own loopback address is fine.

## On the server, once

Create an account that can do nothing but forward to ccfleetd.

```bash
sudo useradd --create-home --shell /bin/sh ccfleet-tunnel
sudo install -d -m 700 -o ccfleet-tunnel -g ccfleet-tunnel /home/ccfleet-tunnel/.ssh
```

## Per node

Generate a key for that node only, then authorize it with the narrowest options
OpenSSH offers. `restrict` turns everything off, `port-forwarding` turns just
forwarding back on, and `permitopen` pins the one destination it may reach.

```bash
# on the node, as the owner
ssh-keygen -t ed25519 -N "" -f ~/.ssh/ccfleet_tunnel -C "ccfleet-tunnel-$(hostname)"
cat ~/.ssh/ccfleet_tunnel.pub
```

```bash
# on the server, paste that public key as one line
echo 'restrict,port-forwarding,permitopen="127.0.0.1:8110" ssh-ed25519 AAAA... ccfleet-tunnel-node-a' \
  | sudo tee -a /home/ccfleet-tunnel/.ssh/authorized_keys
sudo chmod 600 /home/ccfleet-tunnel/.ssh/authorized_keys
sudo chown ccfleet-tunnel:ccfleet-tunnel /home/ccfleet-tunnel/.ssh/authorized_keys
```

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
- The server still binds loopback only. Nothing here opens a public port.
