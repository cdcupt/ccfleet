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
| Guided setup | Numbered, repeatable progress and precise migration recovery | Fresh, repeat, cancelled and interrupted setup retain the correct pairing/history | Published; public repeat verified |
| Launcher and preferences | Local new/continue/resume choices; explicit remote option; private project defaults | Native session identity preserved; explicit flags take precedence; script stdout stays clean | Published; native continuation verified |
| Diagnostics and account health | Readiness failures separated into actionable checks; local support export | Secret/path/email fixtures absent from exported reports; no default model call or project scan | Published; private export verified |
| Background jobs | Supervised local work with start/status/logs/stop and revocation | Owned-group cleanup and authorization-loss controls; deliberately detached tools are not sandbox-contained | Published; live stop and revocation cleanup verified |
| Releases and rollback | Signed immutable bundles, complete verification and atomic activation | Tamper, wrong key, interruption, downgrade and rollback fault tests | Signed 0.2.0 published; all eight public files verified |
| Service visibility | Aggregate relay counters and latency information, distinct from subscription quota | No content/identity strings accepted; stale metrics labelled; account transition resets attribution | Deployed on reachable nodes; fresh reports verified |
| Transport performance | Measured connection costs and safe reuse if justified | Controlled-peer benchmark and cancellation/revocation regressions | Live readiness measured; experimental opt-in only |
| Broker resilience | Same-slot broker alternatives with consistent authorization | Alternative broker cannot change slot identity; no credential fallback or POST replay | Internal status/revocation verified; upgrade compliance and public routing pending |
| Website and guides | Accurate local/remote/session/background/update/recovery guidance | Desktop/mobile rendering and public migration smoke tests | Deployed; layouts and public pages verified |

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

Runtime/source `9e52e29`, signed client 0.2.0, manifest publication `58141f4`
and stable-channel publication `c7621d6` are recorded in
[the verification evidence](local-relay-verification.md#latest-native-client-platform-release).
The required completed local run passed 4,168 tests at 91.75% coverage; exact
runtime CI passed all five jobs and all three supported Python versions.
The server and two reachable nodes were upgraded, canary first. The third free
node was unreachable from both operator and broker, so its rollout is blocked,
not reported as done. Its stale report prevents new customer assignment.

The internal broker is installed but public routing remains unchanged to protect
shared-edge streams. Direct backend HTTP/1.0 WebSocket upgrades are incompatible
with the client's required HTTP/1.1 response; that needs a separately verified
server fix before the broker activation gate can be completed. Secure signer recovery and signed-channel renewal are
operator responsibilities. This release does not complete a sustained user pilot,
add new providers/payment integrations, or establish an anonymity guarantee.
