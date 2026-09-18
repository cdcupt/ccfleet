# Runbooks

Short procedures for the situations the dashboard will put in front of you.
Commands marked *node* run as the owner on the node; *server* runs where
`ccfleetd` lives.

## Add an owner

1. *server*: `ccfleetd node add <node-id> --owner <name> --region <region> [--rc-expected]`
   and hand the printed three lines to the owner over a private channel.
2. New VPS in a supported region. *root*: `bootstrap.sh <owner> <pubkey>`.
3. *node*: `git clone https://github.com/cdcupt/ccfleet.git && ccfleet/node/setup-owner.sh`,
   fill `~/.config/ccfleet/agent.env`, `chmod 600` it.
4. *node*: `tmux new -s cc`, `claude`, `/login`, paste the code, `/status`.
5. *node*: `ccfleet-agent --print`, then `systemctl --user start ccfleet-agent.service`.
6. *server*: `ccfleetd node pin <node-id> <version>` with the version from `/status`.

## Timers never run after setup

`setup-owner.sh` now refuses to continue when the owner has no working per-user
systemd manager, because enabling a timer there looks successful and then
nothing ever fires. The usual cause is a minimal image without `libpam-systemd`,
so `pam_systemd.so` is absent and `XDG_RUNTIME_DIR` is never set.

1. *node*: `systemctl status user@$(id -u).service` and
   `journalctl -u user@$(id -u).service -n 20`.
2. *root*: `apt-get install -y libpam-systemd && loginctl enable-linger <owner>`.
   This edits PAM, so keep a second SSH session open while you do it.
3. Log out, back in, re-run `setup-owner.sh`.

## `credentials_missing` or `token_expired`

The owner needs to sign in again; nobody else can do it for them.

1. *node*: `tmux attach -t cc` (or `tmux new -s cc`), `claude`, `/login`.
2. If the CLI says the login expired, the same command renews it.
3. *node*: `systemctl --user start ccfleet-agent.service`; the alert closes on
   the next heartbeat.

## `token_stale`

Usually the node was simply idle for a day (Claude Code refreshes on use). Open
a session; if the warning persists after use, treat it as `token_expired`.

## `no_heartbeat`

1. *server*: `ccfleetd node list` to see the last time it was heard from.
2. *node*: `systemctl --user status ccfleet-agent.timer ccfleet-agent.service`,
   `journalctl --user -u ccfleet-agent.service -n 50`.
3. Common causes: linger disabled (`loginctl enable-linger <owner>` as root),
   wrong token after a rotation, VPS down.

## `version_mismatch`

Either the node was upgraded and the pin is stale, or the node drifted.

- Intended upgrade: *server* `ccfleetd node pin <node-id> <new version>`.
- Unintended: *node* `ccfleet-upgrade-claude <pinned version>`.

## Staged upgrade of the fleet

1. Pick one node. *node*: `ccfleet-upgrade-claude` (latest) or with a version.
2. Work a normal session on it for a day.
3. Roll the same version to the other nodes, then `ccfleetd node pin` each.

## `egress_changed`

Confirm with *node* `exitip`. If the provider moved the IP, nothing to do; the
alert is informational and closes on the next heartbeat. If you did not expect
it, check the VPS console for a rebuild or migration.

## `disk_high`

*node*: `du -sh ~/.claude ~/backups ~/projects 2>/dev/null`; old backups
rotate automatically (`CCFLEET_BACKUP_KEEP`); `~/.claude/debug` and
`~/.claude/cache` are safe to delete.

## `remote_control_down`

*node*: `systemctl --user restart claude-remote-control.service`, then
`tmux attach -t remote-control` to read the reason. Remote Control needs a
valid login; if it complains about eligibility, do the re-login runbook. The
very first start must be interactive to accept the one-time prompt.

## Rebuild a node

1. New VPS, `bootstrap.sh`, `setup-owner.sh`, then restore the latest
   `claude-state-*.tar.gz` from `~/backups/ccfleet` or the rclone remote.
   Archive paths are relative to `/` (`home/<owner>/.claude/...`), so as the
   owner run `tar -xzf claude-state-<stamp>.tar.gz -C /`. It contains no
   credentials.
2. Owner runs `claude` and `/login`.
3. *server*: `ccfleetd node rotate-token <node-id>` and put the new token in
   `agent.env`; the old token is revoked immediately.

## Remove an owner

1. *server*: `ccfleetd node disable <node-id>` (keeps history) or
   `ccfleetd node remove <node-id>` (deletes heartbeats and alerts).
2. Owner runs `/logout` on the node, then destroy the VPS.

## Rotate the admin token

*server*: change `CCFLEET_ADMIN_TOKEN` in the env file, restart `ccfleetd`,
update the password saved in your browser.

## Quarterly

Re-read the pages quoted in `docs/compliance.md`; Anthropic's terms and the
gateway requirements change. Check `claude --version` against the pin on every
node and upgrade in stages.
