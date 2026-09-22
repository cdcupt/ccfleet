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
4. *node*: log in over SSH, which attaches you to the persistent session, then `claude`, `/login`, paste the code, `/status`.
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

1. *node*: log in over SSH (you land in the `cc` session automatically), then `claude`, `/login`.
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

The pin is now authoritative: an agent that finds its node on a different
version installs the pinned one and reports the result. So this alert means the
node has not reconciled yet, or tried and failed.

1. *server*: check the node's row. A failed attempt is shown under the version
   with the installer's own error.
2. A node that simply has not caught up closes the alert on its next heartbeat.
3. Repeated failures back off for an hour between attempts, so a node that
   cannot install the pinned version alerts rather than retrying in a loop.

> **Upgrading a node by hand no longer sticks.** While a pin is set, the next
> heartbeat installs the pinned version over whatever you just installed. Change
> the pin instead; the node follows. To work on a node outside the pin, either
> clear the pin or run its agent with `--no-reconcile`.

## Staged upgrade of the fleet

The pin drives the upgrade, so stage it one node at a time and let each one
prove itself before the next.

1. *server*: `ccfleetd node pin <first-node> <new version>`. Pick the node you
   would least mind losing for an hour.
2. Wait a full heartbeat interval and confirm the row shows the new version with
   no failure. Then work a normal session on it for a day.
3. Pin the remaining nodes. Never pin them all at once: a bad release would take
   the whole fleet in the same five minutes.

A node can also be told to track a channel — `ccfleetd node pin <node> stable`.
It resolves once and re-checks daily rather than on every heartbeat, and a
channel pin never reports drift, because tracking it is what the pin asks for.

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
`tmux -L ccfleet-rc attach -t remote-control` to read the reason. Remote Control needs a
valid login; if it complains about eligibility, do the re-login runbook. The
very first start must be interactive to accept the one-time prompt.

## `slot_wipe_failed:<user>`

A slot was given back, or taken back, and removing its Linux user failed. It
stays out of the pool, because only a finished wipe makes a slot free, and the
machine agent tries again every 10 minutes by itself. The alert names the
error. The usual cause is a process that outlived `pkill`; to see it, on the
machine: `ps -u <user>`, and the agent's own account of it with
`journalctl -u ccfleet-machine --since -1h`. Once the cause is gone, either
wait for the next attempt or run it now as root:
`/usr/local/lib/ccfleet/slot-remove.sh --slot <user>`. The slot turns free when
the machine next reports the user absent.

## `slot_occupied:<user>`

ccfleet's records say the slot is free, but the machine says its Linux user
exists. It is not handed out while that is true: a user that should not exist
may still hold the last person's files. Nobody made it through ccfleet, so it
was made by hand, or the fleet's database was restored from before a claim.
Look at what is in the home directory before anything else. A leftover slot
comes off with `/usr/local/lib/ccfleet/slot-remove.sh --slot <user>` on the
machine. If that refuses because the account is not a ccfleet slot, the name
belongs to an account ccfleet did not make: leave that account alone, remove the
slot here with `ccfleetd slot remove <id>` (it is free, so it may go), and
declare it again under an unused name.

## `slot_missing:<user>`

Somebody holds the slot, and its Linux user has gone from the machine: removed
by hand, or the machine was rebuilt. Their files went with it. Take the slot
back in the console (it asks for the slot's id), tell the person, and let them
claim again; the new slot is set up from scratch.

## `slot_provision_failed:<user>`

Setting up a claimed slot failed, so the claim was given up and the slot is
being wiped; the person can simply claim again. It is a warning, not a page,
unless it repeats. Repeats mean the machine cannot make slots at all: read the
error in the alert, then `journalctl -u ccfleet-machine --since -1h` on the
machine. Disk space and a failed Claude Code install are the usual causes.

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
