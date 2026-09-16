# Compliance notes

ccfleet is built only from arrangements Anthropic describes in its own
documentation. This page quotes the passages the design rests on (fetched
2026-09-16) so you can re-check them when the documents change.

## What ccfleet relies on

From [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance),
"Authentication and credential use":

> OAuth authentication is intended exclusively for purchasers of Claude Free,
> Pro, Max, Team, and Enterprise subscription plans and is designed to support
> ordinary use of Claude Code and other native Anthropic applications.

> Nor does it prevent an end user from signing in to the unmodified Claude Code
> binary with their own Claude subscription, including where a platform hosts
> Claude Code …

That is the node: the unmodified binary, the owner's own subscription, the
owner completing sign-in through Anthropic's flow.

From [Remote Control](https://code.claude.com/docs/en/remote-control): available
on Pro, Max, Team and Enterprise; the session keeps running on the machine
where it was started and is continued from claude.ai/code or the Claude app.
That is how an owner drives their node from a phone or a browser.

From [Authentication](https://code.claude.com/docs/en/authentication): a
different `CLAUDE_CONFIG_DIR` reads a different credential. That is `ccp`.

## What ccfleet avoids

Same legal page:

> Anthropic does not permit third-party developers to offer Claude.ai login
> into their own applications, or to route requests through Free, Pro, or Max
> plan credentials on behalf of their users. Moreover, developers may not
> collect, store, or intermediate Claude.ai credentials or session tokens —
> sign-in to a Claude account must complete through Anthropic's own flow.

> Anthropic reserves the right to take measures to enforce these restrictions
> and may do so without prior notice.

So ccfleet has no token store, no header or body rewriting, no account pool,
no sharing. The agent parses the credentials file only to extract the token
expiry and plan type; token values never leave the process, and its tests
enforce that. The optional gateway below is a pass-through: it forwards the
owner's own request, OAuth header included, and keeps nothing.

## The optional gateway

From [Other LLM gateways](https://code.claude.com/docs/en/llm-gateway):

> `ANTHROPIC_BASE_URL` is the variable that points Claude Code at the gateway.
> Setting only that variable, without a gateway credential, doesn't replace the
> subscription. Requests still route through the gateway, but a saved claude.ai
> login remains the active credential … Gateways that pass this traffic on to
> Anthropic must forward the OAuth capability in `anthropic-beta`.

> While a gateway credential variable or `apiKeyHelper` is active, a
> developer's claude.ai subscription isn't used.

Hence `gateway/Caddyfile.example` forwards verbatim and authenticates with a
private header rather than `ANTHROPIC_AUTH_TOKEN`. The
[gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol)
adds: forward `anthropic-version` and `anthropic-beta` unchanged, stream, keep
SSE pings flowing, inspect without modifying.

## Regions

[Supported countries and regions](https://www.anthropic.com/supported-countries)
does not list mainland China, Hong Kong or Macau. Hosting a node elsewhere
changes where requests leave from, not where the person is, and the login flow
still runs in the owner's browser. ccfleet cannot make an unsupported-region
user compliant; that decision is the user's.

## Disclaimer

ccfleet is an independent open-source project, not affiliated with or endorsed
by Anthropic. Terms change; re-read the pages above before relying on any of
this, and treat community write-ups about "ban mechanisms" as unverified.
