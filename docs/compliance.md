# Compliance notes

Last re-verified against Anthropic's published documentation: 2026-09-28.

This is an engineering constraint record, not legal advice. Re-check the linked
terms before a production launch and obtain the commercial agreement or written
approval required for the actual business.

Current architecture note (2026-09-29): `ccfleet local` now launches native Claude
on the laptop and relays supported model requests using the assigned slot's
credential. That inference-relay design is **not covered by the historical hosted
terminal mapping below**. An implementation, canary or deployment is not evidence
of provider authorization. This document claims no approval for the relay and
does not treat one-account-per-slot isolation as contractual permission. The
source record below was not re-fetched as part of this architecture change.

## Hosted Claude Code

As recorded on the verification date above, Anthropic's
[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
page had a specific section for offering Claude Code inside products and hosted
agent infrastructure. Its recorded conditions include:

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

## Historical hosted-terminal mapping only

This mapping describes plain `ccfleet`, the remote-terminal compatibility path.
It does not describe the current `ccfleet local` inference relay. Earlier
remote snapshot/live-folder designs are retired as the primary workflow; see
[legacy workspace recovery](project-workspaces.md). Architecture changes do not
themselves establish contractual permission or verified deployment.

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
- Historical snapshot/live-folder connectors added file transport while keeping
  the Claude process on the slot. That is not the current local launcher.
- Claude Code on the slot connects to Anthropic with the credential the end
  user supplied through Anthropic's flow.

## Remote-terminal distinction: terminal broker, not model gateway

The BWH service is in the network path from the user's terminal to the slot,
but not in the Claude-to-Anthropic path:

```text
user ── WSS carrying SSH ── BWH ── SSH ── slot ── Claude HTTPS ── Anthropic
```

The SSH layer is end-to-end between the user device and slot. BWH authenticates
the CC Fleet device and relays opaque bytes to a fixed, operator-configured
endpoint. It never receives the Claude OAuth token and does not inspect or
rewrite Claude model requests.

The legacy `gateway/` experiment is not part of this remote-terminal path.
Do not configure `ANTHROPIC_BASE_URL` for that compatibility flow. The current
local launcher instead sets a temporary loopback model endpoint, as described
below; it must not be represented as the same architecture.

## Current local inference relay

Original Claude Code, files, tools, native settings and conversation history run
on the laptop. The local bridge removes selected headers and top-level structured
metadata, then sends supported model requests through encrypted SSH to the
assigned slot. The slot relay authenticates upstream with that slot's bound
credential; it does not distribute the credential to BWH or the laptop, pool
accounts, or select another account on failure. Native slot Claude owns renewal.

This design intermediates model traffic and slot authentication. The earlier
hosted-terminal reasoning does not establish that such a relay is permitted.
Any applicable agreement or authorization must cover this actual architecture,
not merely unmodified Claude Code running on a hosted machine. See
[local relay design](local-relay.md) for technical scope and migration.

## Project data and metadata

The current launcher adds no CC Fleet filesystem cap, snapshot filter or mount.
Native local permissions apply. Selected header/structured-metadata removal does
not redact arbitrary native system prompts, user messages, files or tool results:
local OS, working-directory paths, environment details and personal information
may reach the slot and Anthropic. MCP, hooks, tools, updates and other native
services can make independent laptop connections outside the model relay.

Do not promise anonymity or that nothing about the user is visible anywhere.
BWH sees connection IP/routing metadata, SSH exposes transport properties, and
the slot host's root administrator can inspect the data processed there. The
local launcher is not a laptop OS sandbox. One-account-per-slot isolation,
credential placement and network encryption remain distinct from these limits.

## Branding

Anthropic's page permits accurately stating in plain text that a product has
Claude Code preinstalled or runs Claude Code, but restricts use of Anthropic or
Claude names and logos as a product/company name or implied partnership. CC
Fleet therefore uses its own name and mark and describes Claude Code only as
the third-party software that runs locally or in a slot. Public pages state that CC Fleet
is independent and not endorsed by Anthropic.

## Regions and policies

Customers remain responsible for Anthropic's terms, usage policy and supported
region rules. Moving the slot's egress address does not change the user's legal
location or eligibility. CC Fleet must not advertise itself as bypassing a
regional or account restriction.

## Launch gate

Before public commercial operation, using terms applicable to the actual design:

1. confirm the deployment uses the unmodified Claude Code binary;
2. confirm every user authenticates their own account through Anthropic;
3. confirm BWH and the local client never receive Claude credential values;
4. confirm pricing is for the hosted slot, not resold Claude usage;
5. review the then-current Commercial Terms and hosted-product conditions;
6. contact Anthropic sales when the intended arrangement needs written
   confirmation.

For the current relay, do not substitute the historical hosted-terminal mapping
for that review or claim that deployment establishes provider approval.
