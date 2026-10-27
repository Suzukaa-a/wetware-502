#!/usr/bin/env bash
# WETWARE-502 campaign network plane.
#
# Builds and reconciles the real network of the three-host campaign on this VM:
# three point-to-point veth pairs to the edge / prod-cortex / hive containers,
# the host forwarding, firewall, NAT and /32 routes they need, and the :2222
# operator entry that points at the edge gateway.  Management ssh on :22 is not
# touched by this script.  A second host-side frontend on :2223 goes straight
# to the existing node for baseline acceptance runs (tests/accept_remote.py).
#
# Container side is point-to-point as well: a container only ever owns its
# story address (/32), the host side of its veth owns a 10.66.255.x/32 peer and
# the container's default route points at that peer.  Cross-host traffic is
# therefore real TCP forwarded by this host, not a simulated hop.
#
#   sudo ops/nspawn/campaign_net.sh                             # on the VM
#   WETWARE_VM=ubuntu@192.0.2.10 ops/nspawn/campaign_net.sh  # from a workstation
#
# Idempotent and re-runnable: a second run converges to the same state and
# reports what it found.  Containers that do not exist yet are skipped; their
# host-side plumbing is staged in systemd-networkd and applies the moment
# systemd-nspawn creates the veth.
#
# Env overrides: WETWARE_VM, WETWARE_ROOTFS, WETWARE_CORE_MACHINE,
# WETWARE_CORE_ROOT, WETWARE_EDGE_MACHINE, WETWARE_HIVE_MACHINE,
# WETWARE_ENTRY_STRICT (auto|1|0), WETWARE_SSH_PASSWORD (optional login probe).
set -euo pipefail
umask 022

SELF=${BASH_SOURCE[0]:-$0}

# ---------------------------------------------------------------------------
# 0. entry: optional remote wrapper, then root
# ---------------------------------------------------------------------------
if [ "${WETWARE_REMOTE:-0}" != "1" ] && [ -n "${WETWARE_VM:-}" ]; then
  [ -r "$SELF" ] || {
    echo "WETWARE_VM is set but '$SELF' is not a readable file; run the script" >&2
    echo "as root on the VM instead." >&2
    exit 2
  }
  forwarded=""
  for var in WETWARE_ROOTFS WETWARE_CORE_MACHINE WETWARE_CORE_ROOT \
             WETWARE_EDGE_MACHINE WETWARE_HIVE_MACHINE \
             WETWARE_ENTRY_STRICT WETWARE_SSH_PASSWORD; do
    value=${!var:-}
    [ -n "$value" ] || continue
    forwarded="$forwarded $var=$(printf '%q' "$value")"
  done
  # shellcheck disable=SC2086
  exec ssh -o BatchMode=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 \
    "$WETWARE_VM" "sudo env -u WETWARE_VM WETWARE_REMOTE=1$forwarded bash -s" <"$SELF"
fi

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -E bash "$SELF" "$@"
  fi
  echo "campaign_net.sh must run as root on the campaign host" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 1. layout
# ---------------------------------------------------------------------------
ROOTFS_DIR=${WETWARE_ROOTFS:-/var/lib/wetware-hosts}
CORE_MACHINE=${WETWARE_CORE_MACHINE:-prod-cortex}
CORE_ROOT=${WETWARE_CORE_ROOT:-$ROOTFS_DIR/prod-cortex-04}
EDGE_MACHINE=${WETWARE_EDGE_MACHINE:-edge-gw}
HIVE_MACHINE=${WETWARE_HIVE_MACHINE:-hive}
SHARED_STATE=${WETWARE_SHARED_STATE:-/var/lib/wetware-campaign/state}
UPLINK=${WETWARE_UPLINK:-eth0}

# Host-side veth names, final values: ve-edge-gw / ve-prod-cortex / ve-hive
# (machines edge-gw / prod-cortex / hive).  Linux caps interface names at 15
# characters: an earlier hive machine name "hive-director" needed 16 and
# nspawn 249 silently renamed the veth to ve-hive-dirOmpv, so hive is matched
# by the ve-hive* glob as a safety net -- it also matches ve-hive itself.
# systemd-networkd [Match] Name= and nft iifname accept these patterns
# (verified on this VM).
IF_EDGE=ve-edge-gw
IF_CORE=ve-prod-cortex
IF_HIVE=ve-hive*

HOST_EDGE=10.66.255.1
HOST_CORE=10.66.255.2
HOST_HIVE=10.66.255.3

GUEST_EDGE=10.66.0.1
GUEST_CORE=10.66.0.4
GUEST_HIVE=10.66.6.1

ENTRY_PORT=2222
ENTRY_PRIMARY="$GUEST_EDGE:22"
ENTRY_FALLBACK="$GUEST_CORE:22"
# Host-side baseline acceptance entry: straight to the existing node, so the
# base line can be verified without landing on the edge gateway.
ENTRY_BASE_PORT=2223
ENTRY_BASE="$GUEST_CORE:22"
SSH_PASSWORD=${WETWARE_SSH_PASSWORD:-}

# ---------------------------------------------------------------------------
# 2. helpers
# ---------------------------------------------------------------------------
log() { printf '[campaign-net] %s\n' "$*"; }
die() {
  printf '[campaign-net] ERROR: %s\n' "$*" >&2
  exit 1
}

# Write stdin to $1 (mode $2) only when the content changed.  Returns 1 when
# the file was already current, 0 when it was (re)written.
putfile() {
  local dst=$1 mode=${2:-0644} tmp
  tmp=$(mktemp) || return 1
  cat >"$tmp"
  if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
    rm -f "$tmp"
    return 1
  fi
  install -D -m "$mode" "$tmp" "$dst"
  rm -f "$tmp"
  return 0
}

machine_running() {
  [ "$(machinectl show -p State --value "$1" 2>/dev/null || true)" = "running" ]
}

machine_leader() {
  machinectl show -p Leader --value "$1" 2>/dev/null || true
}

# Run a command in the container's namespaces (mount/uts/ipc/pid/net).
guest() {
  local machine=$1 leader
  shift
  leader=$(machine_leader "$machine")
  [ -n "$leader" ] && [ -d "/proc/$leader" ] || return 1
  nsenter -t "$leader" -m -u -i -p -n -- "$@"
}

link_exists() { ip link show dev "$1" >/dev/null 2>&1; }

# Existing interfaces whose name matches a shell glob ($1), one per line.
ifaces_matching() {
  local path name
  for path in /sys/class/net/*; do
    name=${path##*/}
    # shellcheck disable=SC2254 # the glob is the point here
    case $name in $1) printf '%s\n' "$name" ;; esac
  done
}
addr_present() { ip -4 addr show dev "$1" 2>/dev/null | grep -q "inet $2 "; }
route_present() {
  ip -4 route show dev "$2" 2>/dev/null | awk '{ print $1 }' | grep -qx "$1"
}
tcp_open() { timeout 4 bash -c "exec 3<>/dev/tcp/$1/22" >/dev/null 2>&1; }

# Optional full login probe (only when WETWARE_SSH_PASSWORD is provided).
ssh_probe() {
  local target=$1 port=${2:-22}
  [ -n "$SSH_PASSWORD" ] || return 0
  command -v sshpass >/dev/null 2>&1 || return 0
  sshpass -p "$SSH_PASSWORD" ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
    -o PubkeyAuthentication=no -o PreferredAuthentications=password \
    -p "$port" root@"$target" hostname 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 3. generated content
# ---------------------------------------------------------------------------
sysctl_file() {
  cat <<'EOF'
# WETWARE campaign host: containers are /32 point-to-point peers, so replies
# forwarded between two different veths would trip strict reverse path
# filtering.  Loose mode is what keeps the three-host path legal.
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
EOF
}

# $1 interface, $2 host /32 address, $3 container /32 address
host_network_file() {
  cat <<EOF
# Generated by ops/nspawn/campaign_net.sh; re-run that script instead of
# editing this file.  Host side of the point-to-point veth: it owns a /32 and
# reaches exactly one story address on the container side.
[Match]
Name=$1

[Network]
Address=$2
LinkLocalAddressing=no
IPv6AcceptRA=no

[Route]
Destination=$3
Scope=link
EOF
}

# The /32 example the edge and hive installers copy into their rootfs.
core_guest_network() {
  cat <<EOF
# Generated by ops/nspawn/campaign_net.sh -- /32 example for every campaign
# container.  Inside the container only the story address exists and the
# default route points at the host-side peer of its own veth:
#   edge-gw-01       Address=10.66.0.1/32   Gateway=10.66.255.1
#   prod-cortex-04   Address=10.66.0.4/32   Gateway=10.66.255.2
#   hive-director-00 Address=10.66.6.1/32   Gateway=10.66.255.3
[Match]
Name=host0

[Network]
Address=$GUEST_CORE/32
LinkLocalAddressing=no
IPv6AcceptRA=no
ConfigureWithoutCarrier=yes
DNS=8.8.8.8

[Route]
Destination=$HOST_CORE/32
Scope=link

[Route]
# systemd-networkd 249 rejects the "default" keyword here; 0.0.0.0/0 is the
# portable spelling.  GatewayOnLink is what makes a /32 peer usable as a gateway.
Destination=0.0.0.0/0
Gateway=$HOST_CORE
GatewayOnLink=yes
EOF
}

guard_script() {
  cat <<'GUARD'
#!/bin/sh
# WETWARE campaign host network guard.
# Installed by ops/nspawn/campaign_net.sh -- the canonical copy of this text
# lives there; edit that script, not this file.  Re-applied on every boot by
# wetware-net-guard.service after nftables.service, so the distro ruleset can
# be replaced at will without cutting the container veths.
set -u

# Host-side veth names, final values: ve-edge-gw / ve-prod-cortex / ve-hive
# (machines edge-gw / prod-cortex / hive).  Linux caps interface names at 15
# characters: an earlier hive machine name "hive-director" needed 16 and
# nspawn 249 silently renamed the veth to ve-hive-dirOmpv, so hive is matched
# by the ve-hive* glob as a safety net -- it also matches ve-hive itself.
# systemd-networkd [Match] Name= and nft iifname accept these patterns
# (verified on this VM).
IF_EDGE=ve-edge-gw
IF_CORE=ve-prod-cortex
IF_HIVE=ve-hive*
STORY_NETS="10.66.0.0/24 10.66.6.0/24"
UPLINK=eth0

# nft helpers.  PHRASE is matched as a fixed string: the hive rule contains a
# wildcard, and an unescaped "*" in a grep pattern is what made earlier runs
# stack duplicate rules.  SPEC is "family table chain".
rule_handles() {
  SPEC=$1
  PHRASE=$2
  set -- $SPEC
  nft -a list chain "$1" "$2" "$3" 2>/dev/null | awk -v p="$PHRASE" '
    index($0, p) > 0 { for (i = 1; i <= NF; i++) if ($i == "handle") { print $(i + 1); break } }'
}

# delete every matching rule in one chain
drop_rules() {
  SPEC=$1
  PHRASE=$2
  set -- $SPEC
  for H in $(rule_handles "$SPEC" "$PHRASE"); do
    nft delete rule "$1" "$2" "$3" handle "$H" 2>/dev/null || true
  done
}

# keep exactly one matching rule per chain (converges duplicates)
dedupe_rules() {
  SPEC=$1
  PHRASE=$2
  set -- $SPEC
  FIRST=1
  for H in $(rule_handles "$SPEC" "$PHRASE"); do
    if [ "$FIRST" = 1 ]; then FIRST=0; continue; fi
    nft delete rule "$1" "$2" "$3" handle "$H" 2>/dev/null || true
  done
}

# 1. forwarding + loose reverse path filtering for asymmetric veth paths
sysctl -q -w net.ipv4.ip_forward=1 || true
sysctl -q -w net.ipv4.conf.all.rp_filter=2 || true
sysctl -q -w net.ipv4.conf.default.rp_filter=2 || true

# 2. the distro ruleset may be loaded late or replaced; never assume it exists
nft list table inet filter >/dev/null 2>&1 || nft add table inet filter
nft list chain inet filter forward >/dev/null 2>&1 || \
  nft add chain inet filter forward '{ type filter hook forward priority filter; policy drop; }'
nft list chain inet filter output >/dev/null 2>&1 || \
  nft add chain inet filter output '{ type filter hook output priority filter; policy accept; }'

# 2b. host-local operator entry ports (input chain of the distro ruleset only
#     knows :22/:2222/:80; the baseline-acceptance port needs the same hole)
for PORT in 2222 2223; do
  nft list chain inet filter input 2>/dev/null | grep -qF "tcp dport $PORT accept" || \
    nft add rule inet filter input tcp dport "$PORT" accept
  dedupe_rules "inet filter input" "tcp dport $PORT accept"
done

# 3. forward between the campaign veths (each pair runs in both directions).
#    nft matches ifname strings, so a pattern works both before the veth has
#    been created and after nspawn has created it.
for IF in "$IF_EDGE" "$IF_CORE" "$IF_HIVE"; do
  nft list chain inet filter forward 2>/dev/null | grep -qF "iifname \"$IF\" accept" || \
    nft add rule inet filter forward iifname "$IF" accept
  nft list chain inet filter forward 2>/dev/null | grep -qF "oifname \"$IF\" accept" || \
    nft add rule inet filter forward oifname "$IF" accept
done
for IF in "$IF_EDGE" "$IF_CORE" "$IF_HIVE"; do
  dedupe_rules "inet filter forward" "iifname \"$IF\" accept"
  dedupe_rules "inet filter forward" "oifname \"$IF\" accept"
done
# pre-decision hive names (ve-hive-director never fits in IFNAMSIZ; the glob
# with a hyphen was matched before the machine was renamed to "hive")
drop_rules "inet filter forward" 'iifname "ve-hive-director" accept'
drop_rules "inet filter forward" 'oifname "ve-hive-director" accept'
drop_rules "inet filter forward" 'iifname "ve-hive-*" accept'
drop_rules "inet filter forward" 'oifname "ve-hive-*" accept'

# 3b. per-link plumbing that only exists once a container is up: loose
#     rp_filter and the /32 route to the container's story address.
for PAIR in "$IF_EDGE:10.66.0.1" "$IF_CORE:10.66.0.4" "$IF_HIVE:10.66.6.1"; do
  PAT=${PAIR%:*}
  PEER=${PAIR#*:}
  for LINK in /sys/class/net/*; do
    NAME=${LINK##*/}
    # shellcheck disable=SC2254
    case $NAME in $PAT) ;; *) continue ;; esac
    sysctl -q -w "net.ipv4.conf.$NAME.rp_filter=2" 2>/dev/null || true
    ip -4 route show dev "$NAME" 2>/dev/null | awk '{ print $1 }' | grep -qx "$PEER" || \
      ip -4 route replace "$PEER/32" dev "$NAME" 2>/dev/null || true
  done
done

# 4. host output to the story subnets (distro ruleset drops RFC1918 output)
for NET in $STORY_NETS; do
  nft list chain inet filter output 2>/dev/null | grep -qF "ip daddr $NET accept" || \
    nft insert rule inet filter output ip daddr "$NET" accept
  dedupe_rules "inet filter output" "ip daddr $NET accept"
done

# 5. NAT only for upstream egress: masquerading the veths would rewrite the
#    real story source addresses of edge -> core -> hive traffic.
nft list table ip nat >/dev/null 2>&1 || nft add table ip nat
nft list chain ip nat post >/dev/null 2>&1 || \
  nft add chain ip nat post '{ type nat hook postrouting priority 100; policy accept; }'
HANDLES=$(nft -a list chain ip nat post 2>/dev/null | awk '
  /ip saddr 10\.66\.[06]\.0\/24 masquerade # handle/ {
    for (i = 1; i <= NF; i++) if ($i == "handle") { print $(i + 1); break }
  }')
if [ -n "${HANDLES:-}" ]; then
  for H in $HANDLES; do nft delete rule ip nat post handle "$H" 2>/dev/null || true; done
fi
for NET in $STORY_NETS; do
  nft list chain ip nat post 2>/dev/null | grep -qF "ip saddr $NET oifname \"$UPLINK\" masquerade" || \
    nft add rule ip nat post ip saddr "$NET" oifname "$UPLINK" masquerade
  dedupe_rules "ip nat post" "ip saddr $NET oifname \"$UPLINK\" masquerade"
done

exit 0
GUARD
}

guard_unit() {
  cat <<'UNIT'
[Unit]
Description=WETWARE campaign host network guard (veth forward, /32 routes, NAT)
After=nftables.service systemd-networkd.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/wetware-net-guard.sh

[Install]
WantedBy=multi-user.target
UNIT
}

# $1 = 1 when the :2222 entry must point at the edge gateway only
haproxy_config() {
  cat <<CFG
global
  daemon
  maxconn 512

defaults
  mode tcp
  timeout connect 5s
  timeout client 30s
  timeout server 30s

# Operator entry of the campaign: the silicon gateway is the front door.
# Management ssh on :22 is a different service and is not configured here.
frontend wetware
  bind 0.0.0.0:$ENTRY_PORT
  default_backend host_edge

backend host_edge
  server edge $ENTRY_PRIMARY check inter 1s fall 2 rise 2
CFG
  if [ "$1" != "1" ]; then
    cat <<CFG
  # Transitional fallback, NOT part of the final topology: while the edge
  # container does not exist the base node still answers :2222, so the baseline
  # acceptance does not look like a regression.  haproxy uses a backup server
  # only when every primary is down; the next run drops this line as soon as
  # machine $EDGE_MACHINE is running (or set WETWARE_ENTRY_STRICT=1).
  server core $ENTRY_FALLBACK check inter 1s fall 3 rise 2 backup
CFG
  fi
  cat <<CFG

# Host-side baseline acceptance entry: direct to the existing node, so the base
# line is testable without walking through the edge container.  This port is
# host-only plumbing and is never visible from inside the story world.
frontend wetware_base
  bind 0.0.0.0:$ENTRY_BASE_PORT
  default_backend host_core

backend host_core
  server core $ENTRY_BASE check inter 1s fall 3 rise 2
CFG
}

# ---------------------------------------------------------------------------
# 4. apply
# ---------------------------------------------------------------------------
log "converging campaign network on $(hostname) ($(date -Is))"

[ -d /etc/systemd/system ] || die "this does not look like a systemd host"
for tool in ip nft systemctl machinectl networkctl ss awk; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done
command -v haproxy >/dev/null 2>&1 || die "haproxy is not installed"

log "kernel: ip_forward=1, loose rp_filter"
sysctl_file | putfile /etc/sysctl.d/99-wetware-forward.conf 0644 ||
  log "sysctl drop-in unchanged"
sysctl -q -p /etc/sysctl.d/99-wetware-forward.conf || die "sysctl apply failed"
sysctl -q -w net.ipv4.ip_forward=1

# Bind-mount source shared by the three containers (CAMPAIGN.md).
if [ ! -d "$SHARED_STATE" ]; then
  mkdir -p "$SHARED_STATE"
  log "created shared campaign state directory $SHARED_STATE"
fi

log "host veth configuration in systemd-networkd (/32 peers)"
for legacy_network in /etc/systemd/network/70-wetware-prod-cortex.network \
                      /etc/systemd/network/73-wetware-ve-hive-director.network \
                      /etc/systemd/network/71-wetware-ve-edge.network; do
  if [ -e "$legacy_network" ]; then
    rm -f "$legacy_network"
    log "removed stale $legacy_network"
  fi
done
if host_network_file "$IF_EDGE" "$HOST_EDGE/32" "$GUEST_EDGE/32" |
  putfile /etc/systemd/network/71-wetware-ve-edge-gw.network 0644; then
  log "wrote 71-wetware-ve-edge-gw.network"
fi
if host_network_file "$IF_CORE" "$HOST_CORE/32" "$GUEST_CORE/32" |
  putfile /etc/systemd/network/72-wetware-ve-prod-cortex.network 0644; then
  log "wrote 72-wetware-ve-prod-cortex.network"
fi
if host_network_file "$IF_HIVE" "$HOST_HIVE/32" "$GUEST_HIVE/32" |
  putfile /etc/systemd/network/73-wetware-ve-hive.network 0644; then
  log "wrote 73-wetware-ve-hive.network"
fi

systemctl enable --now systemd-networkd >/dev/null 2>&1 || true
systemctl is-active --quiet systemd-networkd ||
  die "systemd-networkd is not running on this host"
networkctl reload >/dev/null 2>&1 || true

# $1 interface-name glob, $2 host /32, $3 container /32
ensure_host_peer() {
  local pattern=$1 host_ip=$2 peer_ip=$3 ifname found=0
  for ifname in $(ifaces_matching "$pattern"); do
    found=1
    if ! addr_present "$ifname" "$host_ip/32"; then
      networkctl reconfigure "$ifname" >/dev/null 2>&1 || true
      sleep 0.3
      addr_present "$ifname" "$host_ip/32" ||
        ip -4 addr add "$host_ip/32" dev "$ifname"
    fi
    if ! route_present "$peer_ip" "$ifname"; then
      ip -4 route replace "$peer_ip/32" dev "$ifname"
    fi
    log "$ifname: host $host_ip/32 <-> container $peer_ip/32"
  done
  [ "$found" = 1 ] ||
    log "$pattern: veth absent (container not installed yet); host config staged"
}

# $1 interface-name glob, $2 CIDR to remove (legacy single-subnet layout)
drop_addr() {
  local pattern=$1 cidr=$2 ifname
  for ifname in $(ifaces_matching "$pattern"); do
    if addr_present "$ifname" "$cidr"; then
      ip -4 addr del "$cidr" dev "$ifname"
      log "$ifname: removed legacy address $cidr"
    fi
  done
}

ensure_host_peer "$IF_CORE" "$HOST_CORE" "$GUEST_CORE"
drop_addr "$IF_CORE" "10.66.0.1/24"
ensure_host_peer "$IF_EDGE" "$HOST_EDGE" "$GUEST_EDGE"
ensure_host_peer "$IF_HIVE" "$HOST_HIVE" "$GUEST_HIVE"

# --- prod-cortex-04 container side: the /32 example ------------------------
if [ -d "$CORE_ROOT/etc" ]; then
  mkdir -p "$CORE_ROOT/etc/systemd/network"
  if core_guest_network |
    putfile "$CORE_ROOT/etc/systemd/network/20-wetware-host0.network" 0644; then
    log "core: wrote /etc/systemd/network/20-wetware-host0.network ($GUEST_CORE/32, gw $HOST_CORE)"
  fi
else
  log "core: rootfs not mounted at $CORE_ROOT; guest config skipped"
fi

if machine_running "$CORE_MACHINE"; then
  leader=$(machine_leader "$CORE_MACHINE")
  [ -n "$leader" ] && [ -d "/proc/$leader" ] ||
    die "cannot find the leader process of $CORE_MACHINE"
  log "core: reapplying host0 config inside $CORE_MACHINE"
  nsenter -t "$leader" -m -u -i -p -n -- /bin/sh -c '
    networkctl reload >/dev/null 2>&1 || true
    if command -v networkctl >/dev/null 2>&1 && networkctl reconfigure host0 >/dev/null 2>&1; then
      exit 0
    fi
    systemctl restart systemd-networkd >/dev/null 2>&1 || true
    exit 0
  ' || true
  sleep 0.5
  core_addr=$(guest "$CORE_MACHINE" ip -4 -br addr show host0 2>/dev/null || true)
  core_route=$(guest "$CORE_MACHINE" ip -4 route 2>/dev/null || true)
  printf '%s\n' "$core_addr" | grep -q "$GUEST_CORE/32" ||
    die "core host0 did not take $GUEST_CORE/32 (got: ${core_addr:-<none>})"
  printf '%s\n' "$core_route" | grep -q "default via $HOST_CORE" ||
    die "core default route is not via $HOST_CORE (got: ${core_route:-<none>})"
  log "core: host0 $GUEST_CORE/32, default via $HOST_CORE"
else
  log "core: machine $CORE_MACHINE not running; host0 config applies at next boot"
fi

# --- host guard (nft + routes + forwarding), boot persistent --------------
log "host guard: nft forward/output/NAT, /32 routes"
if guard_script | putfile /usr/local/sbin/wetware-net-guard.sh 0755; then
  log "installed /usr/local/sbin/wetware-net-guard.sh"
fi
sh -n /usr/local/sbin/wetware-net-guard.sh || die "guard script has a syntax error"
/usr/local/sbin/wetware-net-guard.sh || die "guard script failed"
if guard_unit | putfile /etc/systemd/system/wetware-net-guard.service 0644; then
  log "updated wetware-net-guard.service"
fi
systemctl daemon-reload
systemctl enable wetware-net-guard.service >/dev/null 2>&1 || true
systemctl restart wetware-net-guard.service >/dev/null 2>&1 ||
  die "wetware-net-guard.service failed"
/usr/local/sbin/wetware-net-guard.sh || die "guard script failed on re-run"

# --- :2222 operator entry --------------------------------------------------
case ${WETWARE_ENTRY_STRICT:-auto} in
  1 | yes | true) entry_strict=1 ;;
  0 | no | false) entry_strict=0 ;;
  *)
    if machine_running "$EDGE_MACHINE"; then
      entry_strict=1
    else
      entry_strict=0
    fi
    ;;
esac

log "operator entry :$ENTRY_PORT -> $ENTRY_PRIMARY"
entry_tmp=$(mktemp)
haproxy_config "$entry_strict" >"$entry_tmp"
haproxy -c -f "$entry_tmp" >/dev/null 2>&1 || {
  rm -f "$entry_tmp"
  die "haproxy rejected the generated config"
}
haproxy_changed=0
if [ -f /etc/haproxy/haproxy.cfg ] && cmp -s "$entry_tmp" /etc/haproxy/haproxy.cfg; then
  rm -f "$entry_tmp"
else
  install -m 0644 "$entry_tmp" /etc/haproxy/haproxy.cfg
  rm -f "$entry_tmp"
  haproxy_changed=1
fi
systemctl enable haproxy >/dev/null 2>&1 || true
if [ "$haproxy_changed" = 1 ] || ! systemctl is-active --quiet haproxy; then
  systemctl restart haproxy || die "haproxy restart failed"
  sleep 1
fi
for port in "$ENTRY_PORT" "$ENTRY_BASE_PORT"; do
  ss -ltn 2>/dev/null | grep -q ":$port " ||
    die "nothing is listening on :$port"
done
if [ "$entry_strict" = 1 ]; then
  log "entry :$ENTRY_PORT -> $ENTRY_PRIMARY (edge only)"
elif machine_running "$EDGE_MACHINE"; then
  log "entry :$ENTRY_PORT -> $ENTRY_PRIMARY (base node fallback forced by WETWARE_ENTRY_STRICT=0)"
else
  log "entry :$ENTRY_PORT -> $ENTRY_PRIMARY (base node fallback while $EDGE_MACHINE is absent)"
fi
log "baseline entry :$ENTRY_BASE_PORT -> $ENTRY_BASE"
log "haproxy frontends/backends:"
sed -n 's/^\(frontend .*\|backend .*\|  server .*\)$/  \1/p' /etc/haproxy/haproxy.cfg

# ---------------------------------------------------------------------------
# 5. status
# ---------------------------------------------------------------------------
log "host side:"
for pair in "$IF_EDGE:$HOST_EDGE:$GUEST_EDGE:$EDGE_MACHINE" \
            "$IF_CORE:$HOST_CORE:$GUEST_CORE:$CORE_MACHINE" \
            "$IF_HIVE:$HOST_HIVE:$GUEST_HIVE:$HIVE_MACHINE"; do
  pattern=${pair%%:*}
  rest=${pair#*:}
  host_ip=${rest%%:*}
  rest=${rest#*:}
  guest_ip=${rest%%:*}
  machine=${rest#*:}
  names=$(ifaces_matching "$pattern")
  if [ -z "$names" ]; then
    log "  $pattern: absent (container '$machine' not installed yet)"
    continue
  fi
  for ifname in $names; do
    if addr_present "$ifname" "$host_ip/32"; then
      state="host $host_ip/32 up"
    else
      state="ADDRESS $host_ip/32 MISSING"
    fi
    if route_present "$guest_ip" "$ifname"; then
      state="$state, route $guest_ip/32"
    else
      state="$state, ROUTE $guest_ip/32 MISSING"
    fi
    if tcp_open "$guest_ip"; then
      state="$state, ssh $guest_ip:22 open"
    else
      state="$state, ssh $guest_ip:22 closed"
    fi
    log "  $ifname: $state"
    if [ -n "$SSH_PASSWORD" ]; then
      name=$(ssh_probe "$guest_ip")
      if [ -n "$name" ]; then
        log "    ssh from host ok: hostname=$name"
      else
        log "    ssh from host FAILED (password/key not accepted for root@$guest_ip)"
      fi
    fi
  done
done

if [ -n "$SSH_PASSWORD" ]; then
  entry_name=$(ssh_probe 127.0.0.1 "$ENTRY_PORT")
  if [ -n "$entry_name" ]; then
    log "entry :$ENTRY_PORT login ok: hostname=$entry_name"
  else
    log "entry :$ENTRY_PORT login returned no answer (expected while edge is absent)"
  fi
  base_name=$(ssh_probe 127.0.0.1 "$ENTRY_BASE_PORT")
  if [ -n "$base_name" ]; then
    log "baseline entry :$ENTRY_BASE_PORT login ok: hostname=$base_name"
  else
    log "baseline entry :$ENTRY_BASE_PORT login FAILED (base node unreachable)"
  fi
fi

log "management ssh :22 left untouched"

cat <<EOF

--- guest-side /32 example (copy into each container rootfs) ---------------
$(core_guest_network)
--------------------------------------------------------------------------
EOF

log "done"
