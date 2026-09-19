# Pass-through gateway (optional)

Use this only for an owner who must keep their files on the laptop and cannot
work on a hosted node. It is the shape Anthropic documents under
["Subscriptions and gateways"](https://code.claude.com/docs/en/llm-gateway):
setting only `ANTHROPIC_BASE_URL` keeps the saved claude.ai login as the active
credential, and the gateway must forward `anthropic-beta` and `anthropic-version`
verbatim, stream responses, and pass SSE pings through.

What the gateway does:

1. Requires a private `X-Gw-Key` header and answers 401 without it.
2. Drops that header and forwards everything else byte for byte to
   `api.anthropic.com`, including the owner's own OAuth bearer.
3. Flushes every chunk immediately (`flush_interval -1`) so streaming works.
4. Stores no credentials and rewrites nothing.

What it deliberately does not do: hold tokens, rewrite headers or bodies,
pool accounts, or share one gateway between people. One gateway, one owner,
one account.

Setup:

```bash
export GW_KEY_A="$(openssl rand -hex 32)"      # keep it in /etc/caddy/env (0600)
caddy validate --config Caddyfile
sudo systemctl restart caddy
```

Laptop:

```bash
export ANTHROPIC_BASE_URL=https://gw-a.example.com
export ANTHROPIC_CUSTOM_HEADERS="X-Gw-Key: $GW_KEY_A"
claude
/status     # base URL shows the gateway; Login row still shows your claude.ai account
```

Alternative to the header: mutual TLS. Caddy can require a client certificate
and Claude Code presents one via `CLAUDE_CODE_CLIENT_CERT`,
`CLAUDE_CODE_CLIENT_KEY` and `CLAUDE_CODE_CLIENT_KEY_PASSPHRASE`.

## Status: validated live, 2026-09-19

A real Claude Code session completed through this handler. What was checked, and
how, so you can judge how much it covers:

| Check | Method | Result |
| --- | --- | --- |
| Refuses without the key | request with no `X-Gw-Key` | 401 from the gateway |
| Reaches Anthropic with it | request with the key | 405 from Anthropic, so it was forwarded |
| A real session works | `claude -p` with only `ANTHROPIC_BASE_URL` and the header set | completed and answered |
| The gateway was really in the path | stopped the container, reran the same command | failed with `ECONNRESET` |
| And recovered | restarted it, reran | succeeded again |
| The subscription stayed the credential | checked no `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` existed | none set, so the OAuth login authenticated |

The test ran over plain HTTP on a loopback port through an SSH tunnel, so TLS
termination and a public hostname are the parts still unexercised. Those are
Caddy's ordinary job rather than anything specific to this config, but if you are
about to depend on it, run a long streamed session over the real hostname first
and watch that output arrives incrementally rather than in one block.
