# Compliance notes

Last re-verified against Anthropic's published documentation: 2026-09-28.

This is an engineering constraint record, not legal advice. Re-check the linked
terms before a production launch and obtain the commercial agreement or written
approval required for the actual business.

## Hosted Claude Code

Anthropic's current [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
page has a specific section for offering Claude Code inside products and hosted
agent infrastructure. Its conditions include:

- use the unmodified Claude Code binary as Anthropic publishes it;
- do not remove, disable or restrict the authentication methods built into it;
- do not pay for, resell or intermediate Claude usage for end users;
- require every end user to authenticate with their own Claude subscription,
  Anthropic API key or supported provider credential;
- do not collect, store or intermediate Claude.ai credentials or session
  tokens; subscription sign-in must complete through Anthropic's flow.

The same page states that an end user may sign in to the unmodified Claude Code
binary with their own subscription when a platform hosts Claude Code under
those conditions. It also says that preinstalling or running Claude Code in a
product or service requires Anthropic's Commercial Terms unless otherwise
agreed.

## How CC Fleet maps to those constraints

This mapping describes the deployed remote-terminal path. The experimental
`ccfleet local` relay reads a slot credential and substitutes upstream
authentication, so it does **not** inherit this mapping. Its production use
needs a permitted authentication arrangement; see [local-relay.md](local-relay.md).

- A slot installs and runs the original Claude Code distribution.
- The customer completes Anthropic's own browser sign-in. The resulting
  credential is written by Claude Code in that customer's slot.
- CC Fleet does not return that credential to BWH, the local `ccfleet` command,
  an operator page or another slot.
- One holder's slot keeps one holder-provided account. There is no account pool,
  automatic selection, credential fallback or shared inference endpoint.
- The customer's subscription relationship and Claude usage remain directly
  between that customer and Anthropic. CC Fleet charges only for its hosted
  Linux slot and operation.
- The local command transports a terminal to the slot. It does not make model
  requests, imitate Anthropic authentication or proxy Claude HTTPS traffic.
- Claude Code on the slot connects to Anthropic with the credential the end
  user supplied through Anthropic's flow.

## Important distinction: terminal broker, not model gateway

The BWH service is in the network path from the user's terminal to the slot,
but not in the Claude-to-Anthropic path:

```text
user ── WSS carrying SSH ── BWH ── SSH ── slot ── Claude HTTPS ── Anthropic
```

The SSH layer is end-to-end between the user device and slot. BWH authenticates
the CC Fleet device and relays opaque bytes to a fixed, operator-configured
endpoint. It never receives the Claude OAuth token and does not inspect or
rewrite Claude model requests.

The legacy `gateway/` experiment is not part of the CC Fleet customer product.
Do not configure `ANTHROPIC_BASE_URL` for this flow.

## Branding

Anthropic's page permits accurately stating in plain text that a product has
Claude Code preinstalled or runs Claude Code, but restricts use of Anthropic or
Claude names and logos as a product/company name or implied partnership. CC
Fleet therefore uses its own name and mark and describes Claude Code only as
the third-party software that runs in a slot. Public pages state that CC Fleet
is independent and not endorsed by Anthropic.

## Regions and policies

Customers remain responsible for Anthropic's terms, usage policy and supported
region rules. Moving the slot's egress address does not change the user's legal
location or eligibility. CC Fleet must not advertise itself as bypassing a
regional or account restriction.

## Launch gate

Before public commercial operation:

1. confirm the deployment uses the unmodified Claude Code binary;
2. confirm every user authenticates their own account through Anthropic;
3. confirm BWH and the local client never receive Claude credential values;
4. confirm pricing is for the hosted slot, not resold Claude usage;
5. review the then-current Commercial Terms and hosted-product conditions;
6. contact Anthropic sales when the intended arrangement needs written
   confirmation.
