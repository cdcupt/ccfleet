# Local-agent relay retired

The old preview ran Claude Code on the laptop and relayed model requests through
the slot. It did **not** provide slot-only agent execution or prevent local
client identity from entering model requests. That design is retired.

The replacement keeps the `ccfleet local` command but changes its meaning:
explicitly share a selected project snapshot, then run the original Claude Code
and its tools on the slot. There is no local Claude process, loopback model API,
credential substitution or inference proxy in the replacement.

See [project workspaces](project-workspaces.md) for the current design, transfer
limits, privacy boundary, migration steps and operator-enabled rollout.

Existing pairing and slot authentication are unchanged. Old local conversation
history is retained locally; it is not imported into the slot. The old
`--print` and `--fork-session` options now report that the local-agent relay was
retired. Use named slot sessions and Claude's native conversation controls.

Plain `ccfleet` still opens the existing remote workspace. Installing the new
client does not share any project or activate project access on a slot.
