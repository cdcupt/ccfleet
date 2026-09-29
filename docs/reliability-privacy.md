# Long-term account reliability and privacy

This records the engineering choices for the native local Claude workflow.
It is not a claim of complete CC Host parity, permanent account availability,
provider approval, or zero metadata disclosure.

## 1. Identity minimization, not captured-identity replay

Both ends of the model bridge allowlist semantic request headers and remove
top-level `metadata` and the explicitly enumerated direct identity fields.
Unknown headers, including local SDK/runtime fingerprint headers, are not sent
upstream. The slot identifies its relay truthfully as `ccfleet-slot-relay/2`.

We do not record and replay another process's account/session identity. A captured
profile can become stale when the native client changes, and matching selected
headers is not evidence that all local information was removed. Unknown JSON
fields and native system/messages/tool content are not arbitrarily rewritten:
doing so can alter model behavior and tool arguments. This is a bounded,
testable minimization boundary, not a fingerprint-free promise.

Provider error bodies are also treated as sensitive. Non-success responses are
normalized to fixed messages and categories. A JSON classification of at most
64 KiB and five seconds recognizes the native `prompt is too long` signal without
echoing provider text. Opaque request IDs and verbose diagnostics are omitted;
numeric Retry-After values are bounded. Successful JSON/SSE body bytes are kept.
This trades detailed provider diagnostics for a smaller error-reflection surface.

## 2. Keep standard, verified outgoing TLS

The slot uses Python HTTPS with certificate and hostname verification to the fixed
`api.anthropic.com` destination. Caller-selected upstreams, redirects, proxy
environment routing and account fallback are not supported. There is no evidence
here that switching to Node/undici would improve account survival or privacy.
Either implementation can use secure TLS; neither proves absence of identifying
information in the application payload. No TLS-fingerprint equivalence is claimed.

## 3. One credential writer: native Claude

The relay reads credentials but never implements OAuth refresh or writes tokens.
Native Claude remains the credential writer. This avoids a second implementation
competing over rotating refresh tokens or depending on an independently copied
vendor OAuth flow.

Hosted maintenance now checks access expiry independently of cached quota data:

- Within ten minutes of expiry, the selected slot may run the existing isolated
  native `/usage` probe. This is not an inference/model request.
- Slot reconciliation, renewal and quota probes are serialized with private
  locks. A pending sign-in, account transition or unavailable safe lock blocks
  the attempt. A probe never intentionally stops the holder's ordinary session.
- Probe commands share a 35-second budget, with up to five seconds for cleanup.
  Even an uncertain tmux launch receives cleanup.
- Retry state is saved before launching. Delays start at 60 seconds and grow to
  ten minutes, but shrink near expiry; expired credentials retry no faster than
  once per minute. The machine alternates urgent and ordinary turns so one
  failing account cannot starve the other slots.
- Renewal is reported only after expiry actually advances and the bound account
  remains unchanged. Native process success or a quota screen alone is not proof.
  Credential facts are reread in the same heartbeat.

The server receives only allowlisted renewal states, timestamps and fixed reason
codes—not tokens, raw CLI output, errors containing account data, or account IDs.
Active-slot expiry and unconfirmed renewal can alert the operator. Old or missing
observations do not resolve an existing failure or establish current readiness.

`current` means the locally reported expiry is sufficiently far in the future;
it does not prove that the provider will accept the credential. Revocation,
account policy, expired refresh credentials or a changed native CLI can still
require the holder to use **Sign in again**. No implementation can guarantee that
a subscription will remain usable indefinitely without user involvement.

## 4. Keep individually revocable, pinned client transport

The local bridge uses an ephemeral authentication nonce. Each computer has its
own paired device token and SSH key; BWH carries the pinned inner SSH stream and
does not receive plaintext model content or the slot's OAuth credential.
This preserves a concrete confidentiality boundary and individual revocation,
without replacing it with a shared secret that every computer must rotate.

The WebSocket upgrade validates the required headers, rejects ambiguous or
unrequested negotiation, and applies a shrinking network-handshake timeout.
Partial pipe writes are completed; zero progress fails. A disconnected client
cancels the SSH child even while a large request is still uploading.
CC Fleet does not replay an ambiguous inference POST or silently switch accounts.

## Boundaries and acceptance evidence

Prompts, system context, paths and tool results can still contain private data.
MCP, hooks, shell tools and other native services can use their own networking.
BWH sees connection metadata; slot root can inspect decrypted requests. The
working directory is not a filesystem sandbox, especially in bypass mode.

Tests use synthetic credentials and controlled peers to cover refresh scheduling,
atomic rotation observation, account-transition races, lock contention, interrupted
maintenance, credential/error redaction, TLS verification, short writes and upload
cancellation. Actual native renewal must be observed separately on an authorized
canary, with unchanged account binding and no customer session interruption.
Do not edit token expiry or duplicate live refresh credentials just to force that
test. A successful finite test is not a long-term availability guarantee.
