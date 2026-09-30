# CC Fleet and CC Host: evidence-based comparison

Reviewed 2026-09-30 UTC. CC Host entries below are current public documentation,
not an independent runtime/security/performance audit. CC Fleet entries refer to
implemented behavior; deployment evidence lives in
[the verification record](local-relay-verification.md).

There is not enough comparative evidence to call either platform best overall.
CC Host currently documents a broader product. CC Fleet deliberately focuses on
one user-owned Claude account per slot and a native local coding experience.

| Dimension | CC Host documents | CC Fleet | Assessment |
| --- | --- | --- | --- |
| Provider breadth | Claude, Codex, Gemini, Grok, Kimi and Cursor | Original Claude workflow with one assigned account | CC Host has broader documented scope. |
| Teams and commercial operations | Groups, sharing, Stripe seat subscriptions, management MCP | Individually paired/revocable devices; no account pooling or sharing product | CC Host has more documented business features. Adding these would require explicit product/authentication/payment choices. |
| Analytics | Token/cache/model/request/session analysis | Aggregate transfer outcomes/latencies; follow-up adds numeric token/cache totals and coverage | CC Host has richer central detail. CC Fleet intentionally avoids central model/session identifiers and content. |
| Native local coding | Configure existing local clients through endpoints; CC Switch import | Original local Claude/tools/history, guided setup/new/resume, project preferences and supervised local print jobs | The basic local-native workflow is shared in principle. Documentation does not establish which is easier for real users. |
| Routing and availability | Session affinity and multi-account routing/fallback | Fixed assigned slot/account; secondary broker shares authorization, never changes accounts | Different contracts. Account fallback is not something to copy into CC Fleet. Same-host brokers are not physical-host redundancy. |
| Credential lifecycle | Re-login, health/quota/reset and account operations | Native Claude is the sole credential writer, with expiry-driven maintenance and fixed recovery states | No comparative longevity winner is established. Neither guarantees permanent account access. |
| Privacy and transport | Credentials stay in hosted environments; central request/account/endpoint metadata is documented | Pinned inner SSH through BWH, hash-only server device tokens, local history and private diagnostics | These are different controls. No blanket superiority or zero-disclosure claim is justified. |
| Performance and operational maturity | Public feature claims; no matching benchmark gathered here | Real local/slot canaries, fault tests and readiness measurements | Our own tests are not a CC Host benchmark or a sustained uptime/user pilot. |

## Primary CC Host sources

- [Account types](https://cchost.ai/docs/account-types),
  [Claude Code](https://cchost.ai/docs/claude-code),
  [Codex CLI](https://cchost.ai/docs/codex-cli) and
  [endpoints](https://cchost.ai/docs/endpoints).
- [Routing](https://cchost.ai/docs/routing), [groups](https://cchost.ai/docs/groups),
  [accounts](https://cchost.ai/docs/accounts) and [usage](https://cchost.ai/docs/usage).
- [Billing](https://cchost.ai/docs/billing), [MCP](https://cchost.ai/docs/mcp),
  [security](https://cchost.ai/docs/security) and [terms](https://cchost.ai/terms).

The anonymous [public configuration](https://cchost.ai/api/config) currently
reports premium-seat sales paused. That is
a sales condition, not proof that existing inference is unavailable. It should
not be used as evidence that CC Fleet has better uptime.

## Do not confuse the reference guide with CC Host's internals

The [bilingual reference guide](https://claude-code-relay-bilingual-guide-2026.cdcupt.chatgpt.site/en.html)
identifies itself as a companion to Wang Shuaiqi's ByteTech article, dated
2026-04-13 and transcribed 2026-09-28. Its captured-identity, Node/undici and
relay-owned OAuth examples do not establish CC Host's commercial implementation.
CC Host's exact fingerprint/TLS stack and renewal algorithm remain unverified.

## Improvements selected for the current follow-up

1. Fix direct backend WebSocket upgrade compliance without relaxing client TLS,
   pinning or HTTP-version checks.
2. Close owned SSH transport on parent-only hard kill using a private liveness
   pipe and a live isolated group owner; do not signal a saved PID or caller group.
3. Provide bounded numeric token/cache usage and explicit coverage, without
   request content or central model/session/device identity collection.
4. Finish the recovered free node's queued runtime upgrade and verify secondary
   transport. Public routing activation still needs safe shared-edge conditions.

Separate future decisions: additional providers, team/commercial management,
read-only management MCP, physical-host redundancy and a sustained user pilot.
Each should be evaluated on actual usability/reliability evidence, not a promise
to copy every competitor feature or prevent every possible metadata disclosure.
