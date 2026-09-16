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

Status: the Caddyfile follows Caddy's documented directives and Anthropic's
forwarding rules but has not been exercised against a live session in this
repository. Test with a 10-minute streamed session before relying on it.
