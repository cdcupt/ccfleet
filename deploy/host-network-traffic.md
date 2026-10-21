# Private network transfer measurements

The primary Docker service has a private network namespace. Its own
`/proc/net/dev` measures the service container; it is not DMIT host transfer.
The private admin traffic section keeps host NIC, service namespace and API
request/token measurements separate. The public status page receives none of
these traffic measurements. Slot holders see only their assigned slots.

The optional `ccfleet-network-traffic.service` oneshot reads fixed Linux kernel
counters and device-backed NIC metadata. It excludes loopback, bridges, veth and
tunnels, avoiding the same forwarded bytes being counted at several interfaces.
A VM's device-backed NIC is included. Unsupported hardware views remain unknown.
No interface name, address, MAC, raw boot ID, process, credential or content is
written. An opaque counter epoch detects reboot/interface changes; decreasing
counters and gaps suppress the affected interval.

Before using `host-network-compose.yml`, install the exact reviewed
`ccfleet_agent/network_traffic.py` as root-owned, non-writable code at
`/usr/local/lib/ccfleet/ccfleet_agent/network_traffic.py`, and install the two
matching systemd units. The service creates `/var/lib/ccfleet-network` as
`root:999` with mode `0750`; its only output is `network.json`, `root:999`
mode `0640`. The unit's group 999 matches the live primary service GID verified during this
release. Recheck the primary's actual group membership before every activation;
if it differs, review and pin the unit to that existing service GID. The writer
uses its effective group and refuses group 0 or mismatched directory/file groups.
The Dockerfile pins UID 10001 but not its GID; do not infer group access from UID.
The directory must not be writable by the container. The compose overlay mounts
only that dedicated directory read-only. A directory mount lets atomic snapshot
replacement remain visible; a bind mount of one replaceable file would pin an old
inode. No host proc mount, Docker socket, privilege or network mode change is needed.

The timer samples every 15 seconds. Files contain at most 512 observations,
approximately two hours of measured transfer. Rates require consecutive samples;
missing intervals contribute no guessed bytes. The UI states the actual first and
last observation and covered seconds. Retained slot heartbeat windows may differ.
These are bounded measured windows, not monthly totals, provider-billable transfer
or lifetime history. API aggregates use their existing seven-UTC-day window and
validated token sample coverage. Fleet-wide API counts combine current bound slot
relays, including local API use; they are not DMIT-only requests or NIC bytes.

Activation changes a host unit and adds one read-only primary mount, so use the
normal reviewed release guard, preserve live connections, and verify the host
snapshot is fresh before claiming the host metric is available. The broker does
not need this mount or an observer. Stop the timer and remove the overlay to disable
collection; older snapshots become stale instead of being presented as current.
