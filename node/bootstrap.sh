#!/usr/bin/env bash
# ccfleet node bootstrap. Run ONCE as root on a fresh Ubuntu 22.04/24.04 VPS.
#
#   sudo ./bootstrap.sh <owner> [ssh-public-key-or-file]
#
# Creates the owner's Linux user, installs the few packages the node needs,
# hardens SSH (keys only), enables the firewall and unattended security
# upgrades, and lets the owner's user services run without a login session.
# It never installs Claude Code and never touches any credential: the owner
# does that themselves with setup-owner.sh.
set -euo pipefail

OWNER="${1:-}"
PUBKEY="${2:-}"

usage() { echo "usage: sudo $0 <owner> [ssh-public-key-or-file]" >&2; exit 2; }
[[ -n "$OWNER" ]] || usage
[[ "$EUID" -eq 0 ]] || { echo "bootstrap.sh must run as root" >&2; exit 1; }
[[ "$OWNER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { echo "owner must be a lowercase unix user name" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q tmux mosh curl git ufw unattended-upgrades python3 ca-certificates

if ! id "$OWNER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$OWNER"
fi
usermod -aG sudo "$OWNER"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$OWNER" > "/etc/sudoers.d/90-ccfleet-$OWNER"
chmod 440 "/etc/sudoers.d/90-ccfleet-$OWNER"

home="/home/$OWNER"
install -d -m 700 -o "$OWNER" -g "$OWNER" "$home/.ssh"
keys="$home/.ssh/authorized_keys"
touch "$keys"
if [[ -n "$PUBKEY" ]]; then
  if [[ -f "$PUBKEY" ]]; then cat "$PUBKEY" >> "$keys"; else printf '%s\n' "$PUBKEY" >> "$keys"; fi
elif [[ -s /root/.ssh/authorized_keys ]]; then
  cat /root/.ssh/authorized_keys >> "$keys"
fi
sort -u "$keys" -o "$keys"
chmod 600 "$keys"
chown "$OWNER:$OWNER" "$keys"

if [[ -s "$keys" ]]; then
  cat > /etc/ssh/sshd_config.d/60-ccfleet.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
SSHD
  sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
else
  echo "WARNING: no SSH public key for $OWNER; leaving password authentication unchanged" >&2
fi

ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 60000:61000/udp comment mosh >/dev/null
ufw --force enable >/dev/null

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT

loginctl enable-linger "$OWNER"

cat <<MSG

Node bootstrapped for owner '$OWNER'.

Next, as the owner (ssh $OWNER@<this host>):
  git clone https://github.com/cdcupt/ccfleet.git && ccfleet/node/setup-owner.sh
MSG
