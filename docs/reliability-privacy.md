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

An in-flight stream rechecks the current credential and bound account. A native
same-account token rotation can extend validity beyond the old token's expiry;
missing, invalid or expired credentials cancel the stream. Immediately before
the sole upstream POST, the relay rereads the credential after connecting TLS.
It does not retry a POST to recover from an authentication race. Every request
still has a fixed overall deadline.

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
Cancellation retains the connected upstream socket even when HTTP
`Connection: close` detaches it from the connection object. It interrupts blocked
reads and checks cancellation again before forwarding another response chunk.

On explicitly opted-in machines, inference gates follow the one held managed
slot through claim, release and reassignment. Revocation precedes wipe retries;
operator opt-out denies managed gates immediately. Legacy manual gates remain
compatible until explicitly disabled or taken under managed policy. See
[operator activation](local-relay.md#operator-verification).

## 5. Local controls, diagnostics and supervised jobs

The guided `ccfleet start` menu and `ccfleet sessions` delegate to original native
new/continue/resume behavior; they do not parse or synchronize conversation
contents. Project preferences remain private and do not implicitly override the
model/effort saved by a resumed conversation. Bare `ccfleet` remains the remote
compatibility command, with `ccfleet remote` as an explicit alias.

`ccfleet doctor --privacy` and `ccfleet status --json` perform read-only checks
without a model request or project scan. Reports contain allowlisted check codes,
version facts and fixed recovery messages, not tokens, account email, prompts,
local paths or filesystem content. `--export LOCALFILE` writes only the requested
new private file and never uploads it. A reported-ready heartbeat remains an
observation, not proof of provider acceptance. Website health badges use the same
fixed classifications without changing sign-in, pairing or CSRF protections.

Managed background jobs are local native print-mode work, not vendor-native
detached agents or attachable remote sessions. Each supervisor has private
authenticated local control, periodically checks device authorization and keeps
the model bridge alive. Its lifetime guardian stays in the owned process group
through foreground exit; cleanup does not signal a PID loaded from disk. Logs
are bounded and private, but may contain sensitive user data. Normal reports and
support exports exclude prompts, project paths and log contents.

Jobs pin their starting device/slot/account assignment and fail closed on sign-in,
account or pairing transitions; new model connections do not silently migrate an
old job to another account. These checks use opaque assignment state, not a
provider identity exposed in support exports.

No job automatically restarts or replays inference. Timeout, stop, revocation or
supervisor loss closes the owned process group and transport; unconfirmed cleanup
is labelled unconfirmed. Deliberately detached tool descendants are outside this
process-group boundary: this is not a local filesystem or process sandbox.

## 6. Authenticated client release activation

The client pins an Ed25519 public key. Signed channel metadata can identify only
an approved immutable manifest and source revision, and must be unexpired.
OpenSSH verifies detached signatures in the `ccfleet-release` namespace before
downloaded metadata is trusted. Every source file/helper is size- and checksum-
checked, and helper declarations must match the signed bundle completely.

Verified files are staged privately, then a single atomic launcher replacement
activates the release and records its previous version and version high-water
mark. A normal update cannot silently downgrade or replace the same version with
different contents. Explicit rollback validates the preserved target without
touching pairing, project files or native history. A legacy bootstrap backup is
labelled as legacy, not as a signature-verified release. Initial HTTPS bootstrap,
the pinned public key and the operator's private signing-key custody remain trust
dependencies; neither signatures nor finite tests guarantee future availability.

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
