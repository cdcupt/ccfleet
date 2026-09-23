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

# Key-only SSH, on sshd's own word. A drop-in is read where the main config
# Includes sshd_config.d, and sshd keeps the FIRST value it meets for each
# keyword, so a line read earlier can overrule it. Before anything is reloaded,
# `sshd -T` must therefore report the result: key login on; password and
# keyboard-interactive login off; and AuthenticationMethods letting a key in on
# its own (`publickey,password` would demand the very password being turned
# off). Some provider images end sshd_config with `PubkeyAuthentication no`,
# where passwords off and keys left off is a machine nobody can log in to; an
# image that never Includes the directory would ignore the drop-in while this
# claimed the box hardened. On any refusal SSH's files go back exactly as they
# were, an earlier run's drop-in included, and nothing is reloaded.
# Kept byte-identical in bootstrap.sh and install.sh (a test compares them):
# each is fetched on its own through curl | bash and cannot source the other.
# shellcheck disable=SC2120  # bootstrap.sh passes no extra lines; install.sh passes one
harden_sshd() {  # harden_sshd [extra sshd_config line...]
  local dir="${CCFLEET_SSHD_DROPIN_DIR:-/etc/ssh/sshd_config.d}"
  local config="${CCFLEET_SSHD_CONFIG:-/etc/ssh/sshd_config}"
  local dropin="$dir/60-ccfleet.conf" had=no previous="" effective ok=yes
  if [ -f "$dropin" ]; then
    had=yes
    previous="$(cat "$dropin"; printf x)"   # the x carries trailing newlines through $( )
    previous="${previous%x}"
  fi
  mkdir -p "$dir"
  {
    printf '%s\n' "PubkeyAuthentication yes" "PasswordAuthentication no" \
      "KbdInteractiveAuthentication no" "PermitRootLogin prohibit-password" "X11Forwarding no"
    [ "$#" -eq 0 ] || printf '%s\n' "$@"
  } > "$dropin"
  if ! sshd -t -f "$config"; then
    if [ "$had" = yes ]; then printf '%s' "$previous" > "$dropin"; else rm -f "$dropin"; fi
    echo "sshd rejected the hardening config; SSH's files are as they were and nothing was reloaded" >&2
    return 1
  fi
  effective="$(sshd -T -f "$config" 2>/dev/null)" || effective=""
  grep -qx "pubkeyauthentication yes" <<<"$effective" || ok=no
  grep -qx "passwordauthentication no" <<<"$effective" || ok=no
  if grep -qxE "(kbdinteractive|challengeresponse)authentication yes" <<<"$effective"; then ok=no; fi
  case " $(sed -n 's/^authenticationmethods //p' <<<"$effective") " in
    *" any "* | *" publickey "*) ;;
    *) ok=no ;;
  esac
  if [ "$ok" != yes ]; then
    if [ "$had" = yes ]; then printf '%s' "$previous" > "$dropin"; else rm -f "$dropin"; fi
    echo "sshd would not run key-only (keys on; password and keyboard-interactive login off;" \
      "AuthenticationMethods letting a key in alone): $config either does not Include $dir," \
      "or a setting sshd reads first overrides it (cloud-init's 50-cloud-init.conf is a common" \
      "one). SSH's files are as they were and nothing was reloaded." >&2
    return 1
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
}
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
  harden_sshd || { echo "bootstrap.sh stopped: SSH is as it was, and nothing after this ran" >&2; exit 1; }
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
