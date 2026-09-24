# Runbooks

Short procedures for the situations the dashboard will put in front of you.
Commands marked *node* run as the owner on the node; *server* runs where
`ccfleetd` lives.

## Naming machines and slots

One machine is one slot, and one name per machine, used everywhere: its node
id, its slot's id, its hostname, and so the name Remote Control shows in the
claude.ai/code machine picker, which shows a machine by its hostname. Named by
role:

- `<operator>-N` for the operator's own machines, owner nodes and shared
  machines kept for their own account alike: `erik-1`, `erik-2`;
- `pool-N` for shared machines offered to customers: `pool-1`, `pool-2`.

A shared machine's one slot takes the machine's name (`pool-1` on `pool-1`),
and its Linux user is `slot01`. When somebody claims it, the slot is named
after them, `<handle>-<n>`: the part of their address before the @, or the
handle the operator chose with `ccfleetd account handle <email> <handle>`, and
the first number nobody answers to (`alice-1`, then `alice-2` for their next).
The machine takes that name as its hostname on its next run, so claude.ai/code
shows the holder their own name; the console marks the machine "hostname
pending" until it has. When the wipe that frees the slot completes, the name
goes and the slot is called by its id again.

Set the hostname when the machine is added, the way step 4 of
[Rename a machine](#rename-a-machine) does, so a provider's serial-number
hostname never reaches the picker. A fleet already running under other names
moves onto these with [Rename a machine](#rename-a-machine), held slots included.

## Add an owner

1. *server*: `ccfleetd node add <node-id> --owner <name> --region <region> [--rc-expected]`
   and hand the printed three lines to the owner over a private channel.
2. New VPS in a supported region. *root*: `bootstrap.sh <owner> <pubkey>`.
3. *node*: `git clone https://github.com/cdcupt/ccfleet.git && ccfleet/node/setup-owner.sh`,
   fill `~/.config/ccfleet/agent.env`, `chmod 600` it.
4. *node*: log in over SSH, which attaches you to the persistent session, then `claude`, `/login`, paste the code, `/status`.
5. *node*: `ccfleet-agent --print`, then `systemctl --user start ccfleet-agent.service`.
6. *server*: `ccfleetd node pin <node-id> <version>` with the version from `/status`.

## Add a shared machine

A shared machine carries one slot: its own Linux user with its own Claude
sign-in, named after whoever holds it. *root* here means root on the new
machine; *laptop* is wherever your SSH key lives.

**Pick the box.** A KVM VPS running Debian or Ubuntu with at least 2 GiB of RAM.
LXC and OpenVZ containers are unsuitable: every slot needs its own systemd user
manager. Size it from what a slot really uses (measured 2026-09-23 on a
signed-in slot with Remote Control on): about 420 MiB idle, about 665 MiB while
a session runs, and about 225 MB of disk for Claude Code; the OS and the machine
agent take about 400 MiB. So a 2 GiB box carries its one slot with room to
spare. Below 4 GiB, add a 2 GiB swap file as a cushion for spikes.

1. *laptop*: make a key for this machine and install it for root, e.g.
   `ssh-copy-id -i <key> root@<machine>` while password login still works.
2. **Prove key login before anything turns passwords off.** Some provider images
   ship with key login disabled (`PubkeyAuthentication no` at the end of
   `/etc/ssh/sshd_config`). *root*: `sshd -T | grep -E '^(pubkeyauthentication|passwordauthentication) '`;
   if key login is off, set `PubkeyAuthentication yes` there, `sshd -t`, reload
   `ssh`, and log in again with the key before going on.
3. *root*: harden with `bootstrap.sh`, not `install.sh` (that one turns the box
   into a single owner's node, whose agent `machine-setup.sh` refuses to run
   beside): `git clone https://github.com/cdcupt/ccfleet.git && ccfleet/node/bootstrap.sh <your-login> "<your public key>"`.
   It gives you a login with sudo, key-only SSH, a firewall and unattended
   security upgrades, and it stops, leaving SSH as it was, if sshd would not
   end up with keys on and passwords off.
4. *root*, only below 4 GiB of RAM:
   `fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`,
   then `echo '/swapfile none swap sw 0 0' >> /etc/fstab` and
   `echo vm.swappiness=10 > /etc/sysctl.d/99-ccfleet-swap.conf && sysctl -p /etc/sysctl.d/99-ccfleet-swap.conf`.
5. *server*: `ccfleetd node add <machine> --owner <your-login> --region <region>`. The
   token it prints is shown once; keep it for step 8 and nowhere else.
6. *server*: `ccfleetd node pin <machine> latest`, the Claude Code channel the
   machine's slots are meant to follow. Slots start on `opus`, which is the
   newest Opus only on a Claude Code that knows it.
7. *server*: `ccfleetd slot add <machine> --machine <machine> --unix-user slot01`.
   Its capacity stays 1: a second slot is refused, because claude.ai/code shows
   a machine by its hostname and two holders would share one name there. Do not
   create the Linux user yourself: the machine makes it when somebody claims the
   slot and wipes it when they give it back.
8. *root*:
   `curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/node/machine-setup.sh | bash -s -- --server <fleet url> --node <machine> --token <token>`.
9. *server*: within a couple of minutes `ccfleetd slot list --machine <machine>`
   shows its slot `free` and `on machine` `no`: the machine itself has
   confirmed it is empty, which is what makes it claimable.

Prove the machine is closed before anyone is given a slot on it:

- *laptop*: `ssh -o PubkeyAuthentication=no root@<machine>` is refused with
  `Permission denied (publickey)`.
- *root*: `sshd -T` reports `pubkeyauthentication yes` and `passwordauthentication no`.
- *root*: `ufw status` reports `Status: active`, and `systemctl is-active ccfleet-machine.timer` says `active`.

These steps are deliberately mechanical. An operator can put exactly them in a
private script that takes an address and a name, reads the root password once
from a prompt, and re-runs safely, so the next machine is one command.

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

A node can also be told to track a channel — `ccfleetd node pin <node> latest`
or `stable`. The server reads both channels' release numbers from Anthropic
about hourly and sends them with every reply, and the node installs a new
release at its next quiet moment, never during a sign-in. Without a number from
the server it resolves the channel itself once a day. A channel pin never
reports drift, because tracking it is what the pin asks for.

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

## `quota_high_session` or `quota_high_week`

The account's Claude usage window is 75% used (warn) or 90% (critical); the
thresholds are `CCFLEET_QUOTA_WARN_PCT` and `CCFLEET_QUOTA_CRIT_PCT`. Nothing is
broken and nothing on the node needs doing: the owner slows down or waits for
the reset the message names. The windows belong to the Claude account, counted
across every device it is used on, so the node is only where it was noticed. A
reading older than two hours is ignored rather than alerted on.

## `account_elsewhere` or `account_elsewhere:<user>`

One Claude account is signed in on two live places at once: two slots, or a
slot and a node. ccfleet keeps one account in one place, so the alert is raised
on each place, and the message names the other. An owner's node carries it
with nothing after the colon; a slot carries its Linux user.

1. Find out whose account it is and which place should keep it. Usually one
   person signed a slot in with the account already on their own node.
2. Sign it out of the other place. On an owner's node, the owner runs
   `claude auth logout` there, then signs that node in to its own account. A
   slot keeps the account it was first signed in with, so a slot on the wrong
   account is given back (a wipe) and a new one claimed with the right account.
3. The alert closes on the next report from the place that stopped reporting
   the account. A node gone quiet counts as nowhere: it is left out rather than
   counted twice.

## `account_changed:<user>`

A held slot is signed in to another Claude account than the one it was first
signed in with. Nothing in the product does that: *Sign in again* keeps only
the slot's own account. So its holder signed in by hand, or put another
account's credential in place. The slot is their own Linux account, so ccfleet
detects this; it cannot prevent it.

1. Ask the holder to sign the slot's own account in again from their page. The
   sign-in is kept only if it is that account, and it replaces the wrong one.
2. If they will not, it is the terms' rule of one account per slot: take the
   slot back in the console, which wipes it.

## Rebuild a node

1. New VPS, `bootstrap.sh`, `setup-owner.sh`, then restore the latest
   `claude-state-*.tar.gz` from `~/backups/ccfleet` or the rclone remote.
   Archive paths are relative to `/` (`home/<owner>/.claude/...`), so as the
   owner run `tar -xzf claude-state-<stamp>.tar.gz -C /`. It contains no
   credentials.
2. Owner runs `claude` and `/login`.
3. *server*: `ccfleetd node rotate-token <node-id>` and put the new token in
   `agent.env`; the old token is revoked immediately.

## Rename a machine

The server's records move first, then the box. Do the two within a few
minutes: in between, the box's heartbeats are refused, because a heartbeat
names the node its token belongs to and that name has just changed. A refused
heartbeat changes nothing on the box (a shared machine provisions, wipes and
restarts nothing on one), so the gap costs a report or two, not a slot.

1. *server*: `ccfleetd node rename <old> <new>`. Its slots, heartbeats, alerts
   and its own sign-in move with it, in one transaction; its token does not
   change.
2. *server*, shared machine only: rename its slot, whatever its state, held
   and in use included: `ccfleetd slot rename <old> <new>`. The holder, the
   claim and the sign-in move with it, and a held slot keeps its holder's name.
   The machine knows its slot by its Linux user and each claim by its time,
   never by the slot's id, so the box needs nothing for this. An owner's node
   counted as their slot is renamed with the node in step 1.
3. The box's own record of its id: *root*, on a shared machine, set
   `CCFLEET_NODE_ID=<new>` in `/etc/ccfleet/agent.env`; *node*, on an owner's
   node, the same line in `~/.config/ccfleet/agent.env`. The next heartbeat is
   accepted.
   On a shared machine, stop here: steps 4 and 5 happen by themselves. The
   machine agent answers to its slot's name (its holder's while held, its own
   id while free): on its next run it rewrites `/etc/hosts`, runs `hostnamectl`,
   writes the cloud-init drop-in, and restarts the slot's Remote Control if it
   is running. The console says "hostname pending" until it has. An owner's
   node is never renamed for you; go on with step 4 there.
4. *root*: the host. Put the new name in `/etc/hosts` before the hostname
   changes, so `sudo` never runs on a name it cannot resolve: the line
   `127.0.1.1 <new> <old>` (on some images the old name sits on the public
   address's line instead; replace it there). Then `hostnamectl set-hostname <new>`,
   and keep cloud-init from putting the provider's name back at the next boot:
   `printf 'preserve_hostname: true\nmanage_etc_hosts: false\n' > /etc/cloud/cloud.cfg.d/99-ccfleet.cfg`.
5. Remote Control takes its name from the hostname (`--name %H`), which systemd
   fills in when it loads the unit. So, as each user running it (the owner on
   an owner's node, the signed-in slot on a shared machine):
   `systemctl --user daemon-reload && systemctl --user restart claude-remote-control`,
   or reboot the box, which does both. It keeps its environment and registers
   again under the new hostname, so claude.ai/code shows the machine by its new
   name. The session it created on its very first start keeps its old title
   until it is archived in claude.ai; sessions started since are unaffected.
   Restarting ends whatever is running in Remote Control, so pick a quiet moment.
6. *server*: `ccfleetd node list` shows the new id with a fresh `last seen`, and
   `ccfleetd slot list --machine <new>` the renamed slots in their old states.

## Count an owner's own node as their slot

An owner's own node can count as a slot they hold, so everything a person
uses is one list on their page and in the console: one account, one slot.
It is a record and nothing more. Nothing on the node changes, and ccfleet
never hands it out, provisions it or wipes it.

1. *server*: it counts toward their allowance like any slot, so raise it first
   if they are at it: `ccfleetd account quota <email> <n>`.
2. *server*: `ccfleetd node hold <node> <email>`. Their Linux login on it is
   taken as the node's owner; name another with `--unix-user <login>`.
3. Their page shows the node as their slot, from the node's own heartbeat:
   signed in or not, the plan, Remote Control, usage. Signing in again and a
   device token run the node's own sign-in, for them alone. There is no
   "Give it back": that would mean wiping somebody's own machine.

Let go of the record with `ccfleetd node hold <node> --none`; the node, its
history and its own sign-in stay exactly as they are.

## Keep a machine for one account

A shared machine can be kept for one account, so its free slot never goes to
anybody else: your own `erik-N` machines, or a machine set aside for one
customer.

1. *server*: `ccfleetd node reserve <machine> <email>`. The address is the
   account's, as it signed in; an unknown address is refused, and only a shared
   machine can be kept.
2. Claims from that account take a slot on a machine kept for it first; nobody
   else is ever handed its slot. The account's allowance still applies.
3. `ccfleetd node list` shows the reservation in its last column. Customers'
   pages never show one.

Open it to anybody again with `ccfleetd node reserve <machine> --none`.

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
