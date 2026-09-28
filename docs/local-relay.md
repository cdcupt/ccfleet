# Local-project relay: experimental, off by default

This is an opt-in implementation of the local-file workflow from the supplied
CC Host reference. It is not a complete CC Host clone or a claim of provider
approval. The existing `ccfleet` terminal workflow remains the deployed default.

## User experience

An operator must explicitly enable this capability for a slot. The computer
must already be paired and have the original Claude Code CLI installed.

```bash
cd ~/code/my-project
ccfleet local
```

Claude Code's file tools, commands and conversation history now run on that
computer, in that directory. There is no folder synchronization or remote
tmux session in this path. Closing the local process stops it. Resume a local
conversation with `ccfleet local --continue` or `ccfleet local --resume ID`.

```bash
ccfleet local --model fable --effort xhigh
ccfleet local --project ~/code/my-project --mode plan
ccfleet local --print "Explain the README in this project"
```

Local sessions default to `manual` permissions. A laptop does not have the
restricted Linux-user boundary of a hosted slot. The holder can explicitly
choose `--mode bypassPermissions`; that lets local tools run with the laptop
user's permissions. A working directory is not a filesystem sandbox.

## Transport and account boundary

```text
original local Claude Code
  -> authenticated, launch-scoped loopback HTTP endpoint
  -> pinned SSH carried through the BWH WebSocket broker
  -> fixed slot-side relay, running as the slot user
  -> api.anthropic.com over certificate-verified HTTPS
```

Each local launch gets a new random loopback token and an isolated local Claude
configuration directory for its paired slot. The nonce only authenticates
local requests; it is not an Anthropic credential and is not persisted by the
wrapper. Existing local Claude credentials and other provider environment
switches are excluded from the launched environment.

The slot checks its existing one-account binding and reads its own current
Claude access token for each request. Client-selected credentials cannot pick
another account. BWH still sees only the inner SSH bytes and routing metadata.
Device revocation closes the broker stream; releasing/reassigning a slot keeps
the existing device/key revocation behavior.

The node has no network listener. Only the exact forced SSH command
`ccfleet-relay-v1` can invoke the module. It accepts bounded message/count-token
requests, fixes the upstream hostname, verifies its TLS certificate and never
follows redirects. Bodies and client identity headers are preserved, not
rewritten to impersonate the remote machine. Client authentication and
hop-by-hop headers are discarded. The slot's own bearer is added upstream.

Responses stream immediately. No prompt/body/token logging is added. A broken
stream closes without a success terminator. The relay does not replay model
requests; the original Claude client may implement its own retry behavior.

## What differs from the reference

- No captured fingerprint, fabricated device metadata or session-id rewrite.
- No relay-owned OAuth refresh: original Claude Code retains credential-file
  ownership. Expired or near-expired credentials produce a clear failure and
  require renewal by the original Claude installation or sign-in on the slot
  page. This dependency must be exercised across a token-expiry cycle before
  any general rollout.
- No account pool, cross-slot failover, automatic account selection, or token
  export to the computer or BWH.
- Existing remote terminals are not automatically converted or stopped.

## Privacy and deployment status

Local paths, file contents and tool output can be part of the model request.
This architecture cannot promise that Anthropic sees only slot information.
The slot relay and root on its host can inspect plaintext requests, responses
and credentials. BWH cannot decrypt the inner SSH connection.

The request-authentication substitution is subject to Anthropic's published
restrictions on subscription credential intermediation. Successful technical
tests are not evidence of contractual permission. See [compliance.md](compliance.md)
and obtain an arrangement that permits the intended production use.

The operator gate is a root-owned, non-writable-by-group/others regular file
`/etc/ccfleet/local-relay/<unix-user>` inside a root-owned directory with the
same write restriction. Ordinary setup does not create this gate. Removing it
rejects subsequent requests and terminates in-flight requests on the next
half-second policy check. Enabling a slot is distinct from deploying the code.

Validation includes synthetic-account isolation, immutable client identity,
framing limits, loopback authentication, browser-origin rejection, streaming
and truncation, expired credentials, old terminal compatibility and a real
local-file probe through operator SSH to the owner canary. The complete
customer WSS/pairing path and a token-expiry cycle remain release checks for
this experimental mode.
