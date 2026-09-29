# Platform improvements and delivery gates

This is the implementation record for the 2026-09-29 review. Current behavior
and proposed work are distinguished below; a row is complete only after its
verification evidence is recorded.

## Scope and invariants

- Original local Claude owns local files, tools and native conversation history.
- One assigned slot uses one bound account. Transport recovery never selects
  another account or replays a model request with an uncertain outcome.
- Existing pairing, histories, permission choices and remote-terminal commands
  survive upgrades. A guided launcher is an additional explicit entry point.
- Diagnostics and support exports use fixed fields and synthetic checks. They
  never upload project contents, raw settings, environment variables or logs.
- Native Claude is the only credential writer. Slot root remains trusted;
  content filtering is not an anonymity or zero-disclosure guarantee.

## Work packages

| Package | Intended result | Verification gate | Status |
| --- | --- | --- | --- |
| Guided setup | Numbered, repeatable progress and precise migration recovery | Fresh, repeat, cancelled and interrupted setup retain the correct pairing/history | In progress |
| Launcher and preferences | Local new/continue/resume choices; explicit remote option; private project defaults | Native session identity preserved; explicit flags take precedence; script stdout stays clean | In progress |
| Diagnostics and account health | Readiness failures separated into actionable checks; local support export | Secret/path/email fixtures absent from exported reports; no default model call or project scan | In progress |
| Background jobs | Supervised local work with start/status/logs/stop and revocation | Terminal exit, crash, stop, timeout and revoked device cannot leave an authorized relay orphan | In progress |
| Releases and rollback | Signed immutable bundles, complete verification and atomic activation | Tamper, wrong key, interruption, downgrade and rollback fault tests | In progress |
| Service visibility | Aggregate relay counters and latency information, distinct from subscription quota | No content/identity strings accepted; stale metrics labelled; account transition resets attribution | In progress |
| Transport performance | Measured connection costs and safe reuse if justified | Controlled-peer benchmark and cancellation/revocation regressions | In progress |
| Broker resilience | Same-slot broker alternatives with consistent authorization | Alternative broker cannot change slot identity; no credential fallback or POST replay | In progress |
| Website and guides | Accurate local/remote/session/background/update/recovery guidance | Desktop/mobile rendering and public migration smoke tests | In progress |

Additional providers, shared-account routing and commercial payment integration
are separate product expansions. Provider credentials, supported authentication,
commercial prices and payment configuration cannot be inferred from this review.
Long-term account availability and a sustained user pilot require observation over
time; passing release tests does not establish those outcomes.

## Bounded execution

This implementation has a three-hour total working budget. Independent client
packages receive 45–50 minutes, followed by integration and focused validation,
the required full suite and CI, then a database backup and canary rollout.
Each stage permits at most two review/fix passes. On budget exhaustion, preserve
a reviewable checkpoint and report remaining work; do not claim unfinished
features are deployed.

## Acceptance record

Evidence will record the actual tested and deployed revisions, supported client
versions, successful fault tests, public installation checks, original-session
preservation, and unresolved limits. Proposed performance or usability targets
are not reported as achieved before measurements exist.
