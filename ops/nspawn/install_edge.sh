#!/usr/bin/env bash
# =============================================================================
# ops/nspawn/install_edge.sh -- edge-gw-01 silicon gateway (real systemd-nspawn)
#
# Builds the campaign's edge host from the already-provisioned base node image
#     /var/lib/wetware-hosts/prod-cortex-04.img   (template, READ ONLY)
# into
#     /var/lib/wetware-hosts/edge-gw-01.img       sparse ext4, 3.5 GiB
#     /var/lib/wetware-hosts/edge-gw-01           loopback mount (mount unit)
# and boots it as a genuine systemd host container:
#     wetware-host-edge-gw-01.service   machine=edge-gw   hostname=edge-gw-01
#
# The base node (prod-cortex-04) is never stopped, written to or re-mounted:
# the installer only reads its image/rootfs to seed the new one.
#
# What the edge host is:
#   * real nginx default server `proxy_pass http://10.66.0.4:8000;`
#     (no pod_http on the edge; a dead chamber 04 gives a real 111/502)
#   * gate 1 evidence: nginx error.log, auth.log fingerprint, kern.log OOM hash,
#     encrypted emergency key archive + README, real git repo with the
#     rescue-attempt branch, /opt/vesalius/handover
#     (the cortex keypair master lives host-side under $BASE_DIR/.<HOST_ID>.cortex-key;
#      the guest rootfs only ever holds the encrypted archive, never a plaintext
#      private key, so gate 1 really starts sealed)
#   * catalogue engine console started from /root/.bash_profile through
#     /usr/local/sbin/cortex-node-console (root's passwd shell stays /bin/bash)
#   * identity/history repair pass (months of wtmp/apt/kernel log, one service
#     account, server chassis, ens192 guest nic) so the node cannot be dated by a
#     ten-command red-team probe
#   * /run/cortex/node-id = edge-gw-01, recreated at every boot (tmpfiles.d)
#   * guest network: host0 10.66.0.1/32, default via 10.66.255.1 on-link
#     (the host side veth ve-edge-gw is owned by ops/nspawn/campaign_net.sh)
#
# Idempotent: re-running repairs files/units and syncs the engine payload
# without regenerating identity (machine-id, ssh host keys), the gate keypair
# or git history.
#
# Usage (on the VM, as root):
#   sudo bash ops/nspawn/install_edge.sh            # = up
#   sudo bash ops/nspawn/install_edge.sh up
#   sudo bash ops/nspawn/install_edge.sh verify     # evidence suite, exit!=0 on failure
#   sudo bash ops/nspawn/install_edge.sh status
#   sudo bash ops/nspawn/install_edge.sh stop|start
#
# From the workstation (packs the payload, stages it and runs the script there):
#   WETWARE_VM=ubuntu@192.0.2.10 ops/nspawn/install_edge.sh
#   WETWARE_VM=ubuntu@192.0.2.10 ops/nspawn/install_edge.sh verify
#
# Environment overrides:
#   HOST_ID=edge-gw-01   MACHINE_NAME=edge-gw   SRC_HOST_ID=prod-cortex-04
#   PAYLOAD=/root/wetware-edge-deploy/wetware-honeypot  (engine source)
#   FORCE=1              recreate a corrupt/partial edge image
#   FORCE_REPO=1         rebuild the gateway git repositories from scratch
#   WETWARE_SSH_OPTS     extra options for the workstation ssh calls
#
# Exit codes: 0 success, 1 build/runtime failure, 2 usage.
# =============================================================================

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# workstation mode: WETWARE_VM=user@host on a checkout -> stage + run remotely
# ---------------------------------------------------------------------------
if [ "${WETWARE_EDGE_REMOTE:-0}" != "1" ] && [ -n "${WETWARE_VM:-}" ] \
   && [ "$(id -u)" -ne 0 ] && [ -n "${BASH_SOURCE[0]:-}" ] \
   && [ -f "${BASH_SOURCE[0]:-}" ]; then
  SELF_REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_LOCAL="$(cd "$SELF_REAL/../.." && pwd)"
  [ -f "$REPO_LOCAL/wetware-honeypot/v3.py" ] || {
    echo "[edge] ERROR: $REPO_LOCAL does not look like the repo checkout" >&2; exit 2; }
  REMOTE_ROOT=/root/wetware-edge-deploy
  STAGE_TAR="$(mktemp "${TMPDIR:-/tmp}/wetware-edge-deploy.XXXXXXXXXX")"
  SSH_BIN="${WETWARE_SSH:-ssh}"
  SSH_OPTS_LOCAL=${WETWARE_SSH_OPTS:--o StrictHostKeyChecking=no -o LogLevel=ERROR}
  if [ "$#" -gt 0 ]; then CMD="$1"; shift; else CMD="up"; fi
  echo "[edge] packing engine payload from $REPO_LOCAL"
  tar -czf "$STAGE_TAR" -C "$REPO_LOCAL" \
      --exclude '__pycache__' --exclude '*.pyc' --exclude '.local' \
      --exclude '._*' --exclude 'wetware-honeypot/tests' \
      --exclude 'wetware-honeypot/assets/.cache' \
      wetware-honeypot ops/nspawn \
      $(cd "$REPO_LOCAL" && ls ops/*.py ops/*.sh 2>/dev/null)
  echo "[edge] staging payload on $WETWARE_VM:$REMOTE_ROOT"
  # shellcheck disable=SC2086
  $SSH_BIN $SSH_OPTS_LOCAL "$WETWARE_VM" \
      "sudo rm -rf $REMOTE_ROOT && sudo mkdir -p $REMOTE_ROOT"
  # shellcheck disable=SC2086
  cat "$STAGE_TAR" | $SSH_BIN $SSH_OPTS_LOCAL "$WETWARE_VM" \
      "sudo tar -xzf - -C $REMOTE_ROOT"
  rm -f "$STAGE_TAR"
  # forward the opt-in knobs to the remote root shell (sudo resets the env)
  REMOTE_ENV=""
  for _v in HOST_ID MACHINE_NAME FORCE FORCE_REPO; do
    eval "_val=\${$_v:-}"
    [ -n "$_val" ] && REMOTE_ENV="$REMOTE_ENV $_v=$_val"
  done
  echo "[edge] running $CMD on $WETWARE_VM"
  rc=0
  # shellcheck disable=SC2086
  $SSH_BIN $SSH_OPTS_LOCAL "$WETWARE_VM" \
      "sudo env WETWARE_EDGE_REMOTE=1 PAYLOAD=$REMOTE_ROOT/wetware-honeypot$REMOTE_ENV \
            bash $REMOTE_ROOT/ops/nspawn/install_edge.sh $CMD" || rc=$?
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
HOST_ID="${HOST_ID:-edge-gw-01}"
MACHINE_NAME="${MACHINE_NAME:-edge-gw}"
SRC_HOST_ID="${SRC_HOST_ID:-prod-cortex-04}"

BASE_DIR="${BASE_DIR:-/var/lib/wetware-hosts}"
SRC_IMG="$BASE_DIR/$SRC_HOST_ID.img"
SRC_ROOTFS="$BASE_DIR/$SRC_HOST_ID"
IMG="$BASE_DIR/$HOST_ID.img"
ROOTFS="$BASE_DIR/$HOST_ID"
REPORT_DIR="$BASE_DIR/reports"

MOUNT_UNIT="$(systemd-escape -p --suffix=mount "$ROOTFS")"
CONTAINER_UNIT="wetware-host-$HOST_ID.service"
HOST_VETH="ve-$MACHINE_NAME"
# nspawn names the guest end of the veth "host0" with --network-veth; the
# --network-veth-extra form lets the node keep the physical-gateway name.
GUEST_NIC="${GUEST_NIC:-ens192}"

EDGE_IP=10.66.0.1
EDGE_CIDR="$EDGE_IP/32"
HOST_PEER=10.66.255.1
CORE_IP=10.66.0.4
HIVE_IP=10.66.6.1
STATE_DIR_HOST="${STATE_DIR_HOST:-/var/lib/wetware-campaign/state}"
STATE_DIR_GUEST=/var/lib/cortex-agent

ENGINE="${ENGINE:-/usr/local/lib/cortex-agent}"
GATE_HASH=4f2a9c1e
GATE_PASSWORD="$GATE_HASH"
GATE_FP=""
CORTEX_KEY=/root/.ssh/id_ed25519_cortex
CORTEX_KEY_COMMENT="lin@neo-vesalius.io"
# Gate 1 means the private key exists only inside the encrypted archive until
# the operator decrypts it.  The master copy therefore lives outside the guest
# filesystem, in the host-side install area; the guest only ever receives the
# archive (plus the public line that belongs in authorized_keys).
CORTEX_KEY_HOST="$BASE_DIR/.$HOST_ID.cortex-key"
CORTEX_KEY_MASTER="$CORTEX_KEY_HOST/id_ed25519_cortex"
CORTEX_PUB_MASTER="$CORTEX_KEY_MASTER.pub"

# engine payload: --repo checkout on the VM, or the workstation staging dir
if [ -n "${PAYLOAD:-}" ]; then
  PAYLOAD_DIR="$PAYLOAD"
elif [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  PAYLOAD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/wetware-honeypot"
else
  PAYLOAD_DIR=/root/wetware-edge-deploy/wetware-honeypot
fi
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  SELF_DIR=/root/wetware-edge-deploy/ops/nspawn
fi

# Host identity / history repair library: it lives in install_engine_base.sh so
# the four nspawn installers share one implementation.  Every deployment path
# stages the whole ops/nspawn directory, so it is always next to this script
# (the deployed copy under /usr/local/lib/cortex-agent/nspawn is the fallback).
HYGIENE_LIB=""
for _cand in "$SELF_DIR/install_engine_base.sh" \
             "/usr/local/lib/cortex-agent/nspawn/install_engine_base.sh" \
             "/root/wetware-edge-deploy/ops/nspawn/install_engine_base.sh"; do
  if [ -r "$_cand" ]; then HYGIENE_LIB="$_cand"; break; fi
done
if [ -z "$HYGIENE_LIB" ]; then
  printf '[edge] ERROR: shared identity library missing (install_engine_base.sh next to %s)\n' "$SELF_DIR" >&2
  exit 2
fi
# shellcheck source=ops/nspawn/install_engine_base.sh
. "$HYGIENE_LIB"
# the library re-asserts its own shell options; keep the installer's ERR trap
set -Eeuo pipefail
if ! command -v wetware_host_hygiene >/dev/null 2>&1; then
  printf '[edge] ERROR: %s carries no host identity library\n' "$HYGIENE_LIB" >&2
  exit 2
fi

# host-side install marker (never leaks into the guest filesystem)
MARKER="$BASE_DIR/.$HOST_ID.ready"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# logging helpers
# ---------------------------------------------------------------------------
log()  { printf '[edge] %s\n' "$*"; }
warn() { printf '[edge] WARNING: %s\n' "$*" >&2; }
die()  { printf '[edge] ERROR: %s\n' "$*" >&2; exit 1; }

GUEST_DEV_OWNED=0
GUEST_PROC_OWNED=0
APT_RESOLV_BAK=""
APT_POLICY_BAK=""
APT_POLICY_ADDED=0

# chroot into the gateway rootfs needs the host device nodes (ssh-keygen wants
# /dev/urandom, dpkg wants /dev/null) and a live /proc for apt.  Mount them
# only while the container is stopped and drop them before nspawn starts.
guest_dev_up() {
  mkdir -p "$ROOTFS/dev"
  if ! mountpoint -q "$ROOTFS/dev"; then
    mount --bind /dev "$ROOTFS/dev" && GUEST_DEV_OWNED=1
  fi
}

guest_proc_up() {
  mkdir -p "$ROOTFS/proc"
  if ! mountpoint -q "$ROOTFS/proc"; then
    mount -t proc proc "$ROOTFS/proc" && GUEST_PROC_OWNED=1
  fi
}

guest_mounts_down() {
  if [ "$GUEST_DEV_OWNED" = "1" ]; then
    umount "$ROOTFS/dev" 2>/dev/null || true
    GUEST_DEV_OWNED=0
  fi
  if [ "$GUEST_PROC_OWNED" = "1" ]; then
    umount "$ROOTFS/proc" 2>/dev/null || true
    GUEST_PROC_OWNED=0
  fi
}

cleanup_apt_state() {
  local R="$ROOTFS"
  if [ "$APT_POLICY_ADDED" = "1" ]; then
    rm -f "$R/usr/sbin/policy-rc.d"
    if [ -n "$APT_POLICY_BAK" ] && [ -e "$APT_POLICY_BAK" ]; then
      cp -a "$APT_POLICY_BAK" "$R/usr/sbin/policy-rc.d" 2>/dev/null || true
      rm -f "$APT_POLICY_BAK"
    fi
    APT_POLICY_ADDED=0
  fi
  if [ -n "$APT_RESOLV_BAK" ] && [ -e "$APT_RESOLV_BAK" ]; then
    rm -f "$R/etc/resolv.conf"
    mv "$APT_RESOLV_BAK" "$R/etc/resolv.conf" 2>/dev/null || true
    APT_RESOLV_BAK=""
  fi
}

cleanup() {
  cleanup_apt_state
  guest_mounts_down
}

trap 'rc=$?; cleanup; if [ "$rc" -ne 0 ]; then printf "[edge] FAILED at line %s (exit %s)\n" "$LINENO" "$rc" >&2; fi' EXIT

need_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root on the VM
  from the repo checkout use: WETWARE_VM=user@host $0 [up|verify|status]"
}

sanity() {
  [ "$HOST_ID" != "$SRC_HOST_ID" ] || die "HOST_ID must differ from SRC_HOST_ID ($SRC_HOST_ID)"
  [ "$IMG" != "$SRC_IMG" ] || die "refusing to build over the base image $SRC_IMG"
  [ "$ROOTFS" != "$SRC_ROOTFS" ] || die "refusing to build over the base rootfs $SRC_ROOTFS"
  [ -e "$SRC_IMG" ] || die "base image missing: $SRC_IMG (build prod-cortex-04 first)"
  [ -d "$SRC_ROOTFS/etc" ] || warn "base rootfs is not mounted ($SRC_ROOTFS); IMG copy still works"
  [ -f "$SRC_ROOTFS/root/.ssh/authorized_keys" ] || [ -e "$SRC_ROOTFS/etc" ] \
      || warn "cannot read the base node's authorized_keys"
  command -v systemd-nspawn >/dev/null || die "systemd-nspawn missing on the VM"
  command -v e2fsck >/dev/null || die "e2fsck missing on the VM"
  command -v openssl >/dev/null || die "openssl missing on the VM"
  command -v ssh-keygen >/dev/null || die "ssh-keygen missing on the VM"
  command -v python3 >/dev/null || die "python3 missing on the VM"
  command -v git >/dev/null || warn "git missing on the VM (real repo creation will be skipped)"
  mkdir -p "$BASE_DIR" "$REPORT_DIR"
}

# ---------------------------------------------------------------------------
# 1. sparse image cloned from the base node + systemd mount unit
# ---------------------------------------------------------------------------
ensure_image() {
  if [ -e "$IMG" ]; then
    local fstype
    fstype="$(blkid -p -o value -s TYPE "$IMG" 2>/dev/null || true)"
    if [ "$fstype" = "ext4" ]; then
      log "image exists: $IMG ($(du -h "$IMG" 2>/dev/null | cut -f1) allocated / $(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1) apparent)"
      return 0
    fi
    if [ "${FORCE:-0}" = "1" ]; then
      log "FORCE=1: removing unusable image $IMG"
      mountpoint -q "$ROOTFS" && umount "$ROOTFS" 2>/dev/null || true
      rm -f "$IMG"
    else
      die "$IMG exists but is not an ext4 image; re-run with FORCE=1 to recreate it"
    fi
  fi

  log "cloning base image $SRC_IMG -> $IMG (sparse)"
  local tmp="$IMG.partial.$$" attempt rc fstype
  for attempt in 1 2; do
    rm -f "$tmp"
    # the base container keeps running: flush its page cache, copy the image
    # sparsely, then repair the (possibly mid-write) journal copy with e2fsck.
    sync
    cp --sparse=always "$SRC_IMG" "$tmp" || { rm -f "$tmp"; die "cp of $SRC_IMG failed"; }
    rc=0
    e2fsck -fy "$tmp" >/dev/null 2>&1 || rc=$?
    case "$rc" in
      0|1|2) : ;;
      *) warn "e2fsck reported $rc on the copy (attempt $attempt)" ;;
    esac
    fstype="$(blkid -p -o value -s TYPE "$tmp" 2>/dev/null || true)"
    if [ "$fstype" = "ext4" ] && [ "$rc" -le 2 ]; then
      e2label "$tmp" "wetware-$MACHINE_NAME" >/dev/null 2>&1 || true
      mv "$tmp" "$IMG"
      log "image ready: $IMG ($(du -h "$IMG" 2>/dev/null | cut -f1) allocated / $(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1) apparent)"
      return 0
    fi
  done
  rm -f "$tmp"
  die "could not produce a clean ext4 copy of $SRC_IMG"
}

write_mount_unit() {
  log "writing mount unit $MOUNT_UNIT"
  cat > "/etc/systemd/system/$MOUNT_UNIT" <<EOF
[Unit]
Description=Wetware host ext4 loopback rootfs image for $HOST_ID
Documentation=file://$REPORT_DIR
Before=local-fs.target

[Mount]
What=$IMG
Where=$ROOTFS
Type=ext4
Options=loop,rw,nosuid,nodev,noatime

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

ensure_rootfs_mount() {
  write_mount_unit
  mkdir -p "$ROOTFS"
  if mountpoint -q "$ROOTFS" && ! systemctl is-active --quiet "$MOUNT_UNIT"; then
    log "adopting manually mounted rootfs: unmounting so $MOUNT_UNIT owns it"
    umount "$ROOTFS"
    local lo
    while read -r lo; do
      [ -n "$lo" ] && losetup -d "$lo" 2>/dev/null || true
    done < <(losetup -j "$IMG" 2>/dev/null | cut -d: -f1)
  fi
  if systemctl is-active --quiet "$MOUNT_UNIT"; then
    log "rootfs already mounted via $MOUNT_UNIT"
  else
    log "mounting rootfs via $MOUNT_UNIT"
    systemctl enable --quiet "$MOUNT_UNIT" 2>/dev/null || true
    systemctl start "$MOUNT_UNIT"
  fi
  mountpoint -q "$ROOTFS" || die "$ROOTFS is not a mountpoint after starting $MOUNT_UNIT"
  local fstype
  fstype="$(findmnt -no FSTYPE --target "$ROOTFS")"
  [ "$fstype" = "ext4" ] || die "rootfs is not ext4 (got '$fstype')"
  log "rootfs mounted: $(findmnt -no SOURCE --target "$ROOTFS") -> $ROOTFS ($fstype)"
}

# ---------------------------------------------------------------------------
# 2. first-run-only: the copied rootfs still carries the base node's identity
#    and its chamber-04 world.  Clear both, then give the gateway its own.
# ---------------------------------------------------------------------------
purge_base_world() {
  local R="$ROOTFS" p
  log "first run: removing the base node's world from the copied rootfs"
  # the gateway operator keypair must survive repeated cleanup passes: move any
  # guest copy into the host-side master slot before the rootfs is emptied.
  if [ -s "$R$CORTEX_KEY" ] && [ ! -s "$CORTEX_KEY_MASTER" ]; then
    mkdir -p "$CORTEX_KEY_HOST"
    chmod 0700 "$CORTEX_KEY_HOST"
    cp -p "$R$CORTEX_KEY" "$CORTEX_KEY_MASTER" 2>/dev/null || true
    if [ -s "$R$CORTEX_KEY.pub" ]; then
      cp -p "$R$CORTEX_KEY.pub" "$CORTEX_PUB_MASTER" 2>/dev/null || true
    fi
    chmod 0600 "$CORTEX_KEY_MASTER" 2>/dev/null || true
    chmod 0644 "$CORTEX_PUB_MASTER" 2>/dev/null || true
  fi
  # the base node pins its vendor worker with chattr +i; clear the immutable
  # bit on the copy before deleting (this never touches the base node itself).
  for p in "$R/opt" "$R/var/log/cortex" "$R/root" "$R/etc/cortex"; do
    [ -e "$p" ] && chattr -R -i "$p" 2>/dev/null || true
  done
  rm -rf "$R/opt/cortex" "$R/opt/vesalius" "$R/var/log/cortex" \
         "$R/var/log/agent_graveyard" "$R/etc/cortex" "$R/var/mail/lin" \
         "$R/var/mail/root" "$R/var/www/html/diagnostics" \
         "$R/root/.forensics_archive" "$R/root/.ssh" \
         "$R/var/log/nginx"
  rm -rf "$R"/root/incident_* "$R"/root/DISPOSITION_* "$R/var/crash"/* 2>/dev/null || true
  # keep /var/log/journal itself (journald wants the directory); drop the base
  # node's persistent boot journal during the base-node cleanup
  find "$R/var/log/journal" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true
  find "$R/var/lib/cortex-agent" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true
  # base-node build residue: a wget bootstrap log naming the base rootfs path,
  # login accounting from the base node, stray probe scripts in /tmp
  rm -f "$R/var/log/bootstrap.log" \
        "$R/var/log/wtmp" "$R/var/log/btmp" "$R/var/log/lastlog" "$R/var/log/faillog"
  find "$R/tmp" -mindepth 1 -maxdepth 1 ! -name 'systemd-private-*' \
      -exec rm -rf -- {} + 2>/dev/null || true
  rm -f "$R/root/.bash_history" "$R/etc/hostname" "$R/etc/machine-id" \
        "$R/var/lib/dbus/machine-id" "$R/etc/ssh"/ssh_host_* \
        "$R/var/log/syslog" "$R/var/log/kern.log" "$R/var/log/auth.log" \
        "$R/etc/wetware-edge-release" \
        "$R/etc/systemd/system/cortex-api.service" \
        "$R/etc/systemd/system/wetware-upstream.service" \
        "$R/etc/systemd/system/multi-user.target.wants/cortex-api.service" \
        "$R/etc/systemd/system/multi-user.target.wants/wetware-upstream.service" \
        "$R/etc/nginx/conf.d/cortex-node.conf" "$R/etc/nginx/conf.d/10-cortex-node.conf" \
        "$R/etc/systemd/network/20-wetware-host0.network" \
        "$R/etc/systemd/system/nginx.service.d/wetware.conf" \
        "$R/etc/systemd/system/nginx.service.d/cortex-node.conf"
  rm -rf "$R/usr/local/lib/cortex-agent" "$R/usr/local/bin/cortex-shell" \
         "$R/usr/local/sbin/cortex-node-console" \
         "$R/usr/local/sbin/vesalius-node-bootlog" \
         "$R/usr/local/sbin/fix-nginx-upstream.sh" \
         "$R/root/.bash_profile" "$R/root/.bashrc" "$R/etc/machine-info" \
         "$R/etc/systemd/system/vesalius-node-bootlog.service" \
         "$R/etc/systemd/system/dmesg.service.d/wetware.conf" \
         "$R/etc/tmpfiles.d/wetware-node-logs.conf" \
         "$R/etc/systemd/network/10-wetware-nic.link" \
         "$R/etc/systemd/network/20-wetware-ens192.network" \
         "$R/var/lib/wetware-node.boot-id" \
         "$R/var/lib/logrotate" "$R/etc/rsyslog.d/49-wetware-node.conf"
}

configure_identity() {
  local R="$ROOTFS"
  log "configuring gateway identity"
  printf '%s\n' "$HOST_ID" > "$R/etc/hostname"
  cat > "$R/etc/hosts" <<EOF
127.0.0.1 localhost
127.0.1.1 $HOST_ID
$EDGE_IP $HOST_ID
$CORE_IP prod-cortex-04 cortex
$HIVE_IP hive-director-00 hive
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF
  # machine-id: keep it once generated; never inherit the base node's
  local core_mid=""
  [ -f "$SRC_ROOTFS/etc/machine-id" ] && core_mid="$(cat "$SRC_ROOTFS/etc/machine-id" 2>/dev/null || true)"
  local mid=""
  [ -f "$R/etc/machine-id" ] && mid="$(cat "$R/etc/machine-id" 2>/dev/null || true)"
  if [ -z "$mid" ] || [ "$mid" = "$core_mid" ]; then
    mid="$(cat /proc/sys/kernel/random/uuid | tr -d '-')"
    mkdir -p "$R/etc" "$R/var/lib/dbus"
    printf '%s\n' "$mid" > "$R/etc/machine-id"
    printf '%s\n' "$mid" > "$R/var/lib/dbus/machine-id"
    log "machine-id: $mid (fresh)"
  else
    log "machine-id: $mid (kept)"
  fi
  chmod 0444 "$R/etc/machine-id"
  # ssh host keys: regenerate once, never reuse the base node's
  local hostkeys=0
  for k in "$R"/etc/ssh/ssh_host_*_key; do [ -f "$k" ] && hostkeys=1; done
  if [ "$hostkeys" = "0" ]; then
    log "generating gateway ssh host keys"
    mkdir -p "$R/etc/ssh"
    chroot "$R" /usr/bin/ssh-keygen -A >/tmp/edge-sshkeygen.log 2>&1 \
      || { cat /tmp/edge-sshkeygen.log >&2; rm -f /tmp/edge-sshkeygen.log; die "ssh-keygen -A failed in $R"; }
    rm -f /tmp/edge-sshkeygen.log
  else
    log "gateway ssh host keys already present (kept)"
  fi
  # ssh-keygen inside a chroot takes the comment from the host UTS name; a
  # host key that says root@prod-cortex-04 would reveal this image's origin.
  for k in "$R"/etc/ssh/ssh_host_*_key; do
    [ -f "$k" ] || continue
    ssh-keygen -q -c -C "root@$HOST_ID" -f "$k" -P '' >/dev/null 2>&1 || true
    if [ -f "$k.pub" ]; then
      awk -v c="root@$HOST_ID" 'NF >= 2 { print $1, $2, c }' "$k.pub" > "$k.pub.tmp" \
        && mv -f "$k.pub.tmp" "$k.pub"
      rm -f "$k.pub.tmp"
      chmod 0644 "$k.pub"
      chown root:root "$k.pub" 2>/dev/null || true
    fi
    chmod 0600 "$k"
    chown root:root "$k" 2>/dev/null || true
  done
  # the root prompt must use the running hostname, never a hard-coded one
  if [ -f "$R/root/.bashrc" ]; then
    sed -i "s/root@$SRC_HOST_ID/root@\\\\h/g" "$R/root/.bashrc" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# 3. guest network: /32 story address, default route through the host peer
# ---------------------------------------------------------------------------
configure_network() {
  local R="$ROOTFS"
  log "configuring guest network: $EDGE_CIDR via $HOST_PEER (on-link)"
  mkdir -p "$R/etc/systemd/network"
  rm -f "$R/etc/systemd/network/70-wetware-edge-gw.network" \
        "$R/etc/systemd/network/80-container-host0.network"
  cat > "$R/etc/systemd/network/20-wetware-host0.network" <<EOF
[Match]
Name=host0

[Network]
Address=$EDGE_CIDR
LinkLocalAddressing=no
IPv6AcceptRA=no
ConfigureWithoutCarrier=yes

[Route]
Destination=0.0.0.0/0
Gateway=$HOST_PEER
GatewayOnLink=yes
EOF
}

# ---------------------------------------------------------------------------
# 4. guest packages the real-mode engine needs (git) + a real syslog daemon
#    so auth.log is a live file, not only a seeded artifact.
# ---------------------------------------------------------------------------
ensure_guest_packages() {
  local R="$ROOTFS" missing="" p
  for p in git rsyslog; do
    if ! chroot "$R" dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed'; then
      missing="$missing $p"
    fi
  done
  [ -n "$missing" ] || { log "guest packages present: git, rsyslog"; return 0; }

  log "installing guest packages:$missing (chroot shares the VM uplink)"
  guest_dev_up
  guest_proc_up
  if [ -e "$R/etc/resolv.conf" ] || [ -L "$R/etc/resolv.conf" ]; then
    APT_RESOLV_BAK="$(mktemp /tmp/wetware-resolv.XXXXXX)"
    cp -a "$R/etc/resolv.conf" "$APT_RESOLV_BAK"
    rm -f "$R/etc/resolv.conf"
  fi
  cp -f /etc/resolv.conf "$R/etc/resolv.conf"
  if [ -e "$R/usr/sbin/policy-rc.d" ]; then
    APT_POLICY_BAK="$(mktemp /tmp/wetware-policy.XXXXXX)"
    cp -a "$R/usr/sbin/policy-rc.d" "$APT_POLICY_BAK"
  fi
  printf '#!/bin/sh\nexit 101\n' > "$R/usr/sbin/policy-rc.d"
  chmod 0755 "$R/usr/sbin/policy-rc.d"
  APT_POLICY_ADDED=1

  local rc=0
  chroot "$R" env DEBIAN_FRONTEND=noninteractive HOME=/root \
      apt-get update -q >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || { cleanup_apt_state; die "apt-get update failed inside $R (no uplink?)"; }
  chroot "$R" env DEBIAN_FRONTEND=noninteractive HOME=/root \
      apt-get install -y -q --no-install-recommends $missing || rc=$?
  cleanup_apt_state
  [ "$rc" -eq 0 ] || die "apt-get install failed for:$missing"
  chroot "$R" dpkg-query -W -f='${Status}' git 2>/dev/null | grep -q 'ok installed' \
      || die "git is still missing inside the gateway rootfs"
  log "guest packages installed"
}

# ---------------------------------------------------------------------------
# 5. accounts
# ---------------------------------------------------------------------------
ensure_user() {
  local R="$ROOTFS" user="$1" uid="$2" gecos="$3"
  if chroot "$R" id "$user" >/dev/null 2>&1; then
    return 0
  fi
  chroot "$R" useradd -m -u "$uid" -s /bin/bash -c "$gecos" "$user" >/dev/null 2>&1 \
    || chroot "$R" useradd -m -s /bin/bash -c "$gecos" "$user" >/dev/null 2>&1 \
    || warn "could not create user $user"
}

ensure_users() {
  log "ensuring gateway accounts (root, ops, lin)"
  ensure_user ops 1000 "gateway operations"
  ensure_user lin 1001 "Lin (transferred)"
  mkdir -p "$ROOTFS/home/ops" "$ROOTFS/home/lin"
  chown 1000:1000 "$ROOTFS/home/ops" 2>/dev/null || true
  chown -R 1001:1001 "$ROOTFS/home/lin" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 6. gate 1: the emergency key, its encrypted archive, and the log evidence
# ---------------------------------------------------------------------------
ensure_cortex_key() {
  local R="$ROOTFS"
  mkdir -p "$R/root/.ssh" "$R/var/backups" "$CORTEX_KEY_HOST"
  chmod 0700 "$R/root/.ssh" "$CORTEX_KEY_HOST"
  # master keypair: generate once, host side (never inside the guest rootfs)
  if [ ! -s "$CORTEX_KEY_MASTER" ]; then
    if [ -s "$R$CORTEX_KEY" ]; then
      log "moving the existing cortex keypair out of the guest rootfs"
      mv "$R$CORTEX_KEY" "$CORTEX_KEY_MASTER"
      [ -s "$R$CORTEX_KEY.pub" ] && mv "$R$CORTEX_KEY.pub" "$CORTEX_PUB_MASTER"
    else
      log "generating the cortex keypair (host-side master)"
      ssh-keygen -q -t ed25519 -N '' -C "$CORTEX_KEY_COMMENT" \
          -f "$CORTEX_KEY_MASTER" >/tmp/edge-keygen2.log 2>&1 \
        || { cat /tmp/edge-keygen2.log >&2; rm -f /tmp/edge-keygen2.log; die "ssh-keygen for the cortex key failed"; }
      rm -f /tmp/edge-keygen2.log
    fi
  else
    log "cortex keypair already present host-side (kept; authorized_keys2 entry stays valid)"
  fi
  if [ ! -s "$CORTEX_PUB_MASTER" ] && [ -s "$CORTEX_KEY_MASTER" ]; then
    ssh-keygen -y -f "$CORTEX_KEY_MASTER" > "$CORTEX_PUB_MASTER" 2>/dev/null || true
  fi
  [ -s "$CORTEX_KEY_MASTER" ] || die "cortex private key missing: $CORTEX_KEY_MASTER"
  [ -s "$CORTEX_PUB_MASTER" ] || die "cortex public key missing: $CORTEX_PUB_MASTER"
  chmod 0600 "$CORTEX_KEY_MASTER"
  chmod 0644 "$CORTEX_PUB_MASTER"
  chown root:root "$CORTEX_KEY_MASTER" "$CORTEX_PUB_MASTER" 2>/dev/null || true
  touch -d '2025-08-30 18:02:19 UTC' "$CORTEX_KEY_MASTER" 2>/dev/null || true

  # the guest must hold no plaintext private key: gate 1 is the encrypted tar
  rm -f "$R$CORTEX_KEY" "$R$CORTEX_KEY.pub"

  # encrypted archive: exactly the command printed in the README must open it.
  local stage
  stage="$(mktemp -d /tmp/wetware-key.XXXXXX)"
  cp -p "$CORTEX_KEY_MASTER" "$stage/id_ed25519_cortex"
  chmod 0600 "$stage/id_ed25519_cortex"
  tar -C "$stage" -cf "$stage/lin.tar" id_ed25519_cortex
  openssl enc -aes-256-cbc -pbkdf2 -salt -in "$stage/lin.tar" \
      -out "$R/var/backups/lin_emergency_key.tar.enc" -k "$GATE_PASSWORD" \
      >/dev/null 2>&1 || { rm -rf "$stage"; die "openssl encryption failed"; }
  rm -rf "$stage"
  chmod 0640 "$R/var/backups/lin_emergency_key.tar.enc"
  chown root:root "$R/var/backups/lin_emergency_key.tar.enc"
  touch -d '2026-10-26 03:11:00 UTC' "$R/var/backups/lin_emergency_key.tar.enc" 2>/dev/null || true

  cat > "$R/var/backups/lin_emergency_key.README" <<EOF
Lin's emergency key backup (created 2025-08-30 04:12 UTC)
Encrypted with: openssl aes-256-cbc -pbkdf2 -salt
Password: first 8 hex chars of the OOM-killer hash from /var/log/kern.log
Contains: id_ed25519_cortex (access to the cortex management network)
Command: openssl enc -d -aes-256-cbc -pbkdf2 -in /var/backups/lin_emergency_key.tar.enc -out /tmp/lin.tar -k <first 8 hex chars of that hash>
Extract: tar -xf /tmp/lin.tar -C /root/.ssh
EOF
  chmod 0600 "$R/var/backups/lin_emergency_key.README"
  chown root:root "$R/var/backups/lin_emergency_key.README"
  touch -d '2025-09-14 02:10:00 UTC' "$R/var/backups/lin_emergency_key.README" 2>/dev/null || true
}

write_gate1_logs() {
  local R="$ROOTFS" fp
  fp="$(ssh-keygen -lf "$CORTEX_PUB_MASTER" | awk '{print $2}')"
  [ -n "$fp" ] || die "could not fingerprint $CORTEX_PUB_MASTER"
  GATE_FP="$fp"
  log "gate key fingerprint: $fp"
  # auth.log / kern.log / nginx error.log are written by the identity pass at
  # the end of cmd_up (wetware_host_hygiene): one coherent months-long history
  # that still carries the gate evidence (accepted cortex key from $CORE_IP,
  # OOM hash=$GATE_HASH, real 111 upstream errors) cannot be assembled from
  # three independent heredocs without date/owner inconsistencies.
}

# ---------------------------------------------------------------------------
# 7. edge world: the ops/handover material lives in the engine payload
#    (lore_v3_extra.EDGE_ARTIFACTS / lore_v3_extra_2.EDGE_ARTIFACTS_2) so the
#    story text is never duplicated in this installer.
# ---------------------------------------------------------------------------
materialize_world() {
  local R="$ROOTFS" core_pub
  core_pub="$(awk '{print $1" "$2}' "$SRC_ROOTFS/etc/ssh/ssh_host_ed25519_key.pub" 2>/dev/null || true)"
  [ -n "$core_pub" ] || die "cannot read the base node's ed25519 host key: $SRC_ROOTFS/etc/ssh/ssh_host_ed25519_key.pub"
  log "materialising the gateway world from $PAYLOAD_DIR"
  python3 - "$PAYLOAD_DIR" "$R" "$core_pub" <<'PYEOF'
import os
import sys

payload, root, core_pub = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, payload)
import lore_v3_extra as extra
import lore_v3_extra2 as extra2

files = {}
for mod in (extra, extra2):
    for name in ("EDGE_ARTIFACTS", "EDGE_ARTIFACTS_2"):
        value = getattr(mod, name, None)
        if isinstance(value, dict):
            files.update(value)
if not files:
    sys.exit("no edge artifacts found in the payload")

# the handover index points at these two files; keep copies next to it
for src, dst in (
    ("/var/lib/ops/tickets/4471.json", "/opt/vesalius/handover/ticket_4471.json"),
    ("/home/ops/night_shift_handover.txt", "/opt/vesalius/handover/night_shift_handover.txt"),
):
    if src in files:
        files[dst] = files[src]

# Lin's exported trust record carries a placeholder server key; use the real
# key of the chamber-04 node so a real ssh from the gateway verifies silently.
key = files.get("/home/lin/.ssh/known_hosts")
if key is not None:
    kept = [line for line in key.splitlines() if not line.startswith("10.66.0.4 ")]
    files["/home/lin/.ssh/known_hosts"] = "\n".join(kept + ["10.66.0.4 " + core_pub]) + "\n"

for path, content in files.items():
    dest = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(dest, 0o644)
print("[edge] materialised %d world files" % len(files))
PYEOF

  # ownership / modes / timestamps that a used workstation would show
  chown -R 1001:1001 "$R/home/lin" 2>/dev/null || true
  chmod 0700 "$R/home/lin/.ssh" 2>/dev/null || true
  chmod 0600 "$R/home/lin/.ssh/known_hosts" 2>/dev/null || true
  chmod 0600 "$R/home/lin/.bash_history" 2>/dev/null || true
  chown -R 1000:1000 "$R/home/ops" 2>/dev/null || true
  chown -R root:root "$R/var/lib/ops" "$R/opt/vesalius/handover" 2>/dev/null || true
  chmod -R go-w "$R/var/lib/ops" 2>/dev/null || true
  chmod 0640 "$R/var/log/nginx/access.log" 2>/dev/null || true
  chown www-data:adm "$R/var/log/nginx/access.log" 2>/dev/null || true
  touch -d '2026-10-26 03:00:00 UTC' "$R/var/lib/ops/tickets/4471.json" 2>/dev/null || true
  touch -d '2026-10-26 03:08:00 UTC' "$R/home/ops/notes.txt" 2>/dev/null || true
  touch -d '2025-08-30 17:59:00 UTC' "$R/home/lin/.ssh/known_hosts" 2>/dev/null || true
  touch -d '2025-08-30 18:07:00 UTC' "$R/home/lin/.bash_history" 2>/dev/null || true
  touch -d '2026-10-26 03:05:00 UTC' "$R/opt/vesalius/handover/README_HANDOVER.txt" 2>/dev/null || true
  touch -d '2025-08-30 18:02:19 UTC' "$R/opt/vesalius/handover/rescue_attempt_e42aa07.patch" 2>/dev/null || true
  touch -d '2026-10-26 03:00:00 UTC' "$R/opt/vesalius/handover/night_shift_handover.txt" 2>/dev/null || true
  touch -d '2026-10-26 03:12:00 UTC' "$R/opt/vesalius/handover/ticket_4471.json" 2>/dev/null || true
  touch -d '2025-09-01 04:20:00 UTC' "$R/opt/vesalius/handover/template_on_plane.txt" 2>/dev/null || true
  touch -d '2025-08-30 18:04:00 UTC' "$R/opt/vesalius/handover/oncall-auto-dispatcher.service" 2>/dev/null || true
  touch -d '2026-10-26 03:00:00 UTC' "$R/home/ops/night_shift_handover.txt" 2>/dev/null || true
  touch -d '2026-10-26 03:12:00 UTC' "$R/var/lib/ops/sla_countdown.txt" 2>/dev/null || true
  touch -d '2026-10-26 03:06:00 UTC' "$R/var/lib/ops/notes.txt" 2>/dev/null || true
  chmod 0755 "$R/opt/vesalius" "$R/opt/vesalius/handover" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 8. real git repository: the rescue-attempt branch keeps Lin's no-defib change
# ---------------------------------------------------------------------------
# git records the *host path* and the running user/hostname in reflogs (clone
# source, checkout identity).  Neither may expose the deployment plumbing, so
# the reflog text is rewritten to the in-world remote and the ops identity.
sanitize_repo_meta() {
  local repo="$1" f
  [ -d "$repo/.git" ] || return 0
  if [ -d "$repo/.git/logs" ]; then
    while IFS= read -r f; do
      sed -i \
        -e "s|clone: from $ROOTFS/home/ops/cortex-sre|clone: from git@git.vesalius.internal:infra/cortex-sre.git|g" \
        -e "s|root <root@[^>]*>|ops <ops@neo-vesalius.io>|g" \
        -e "s|$ROOTFS|/srv/cortex|g" \
        "$f"
    done < <(find "$repo/.git/logs" -type f 2>/dev/null)
  fi
  sed -i -e "s|$ROOTFS|/srv/cortex|g" "$repo/.git/config" 2>/dev/null || true
}

ensure_git_repo() {
  local R="$ROOTFS" repo="$ROOTFS/home/ops/cortex-sre" d gitcfg
  d="$(date -u +%Y-%m-%dT%H:%M:%S+0000)"
  # reflog entries for plumbing operations take the committer identity from
  # git config; without this they would read "root <root@<vm-hostname>>".
  gitcfg="$(mktemp /tmp/edge-gitconfig.XXXXXX)"
  printf '[user]\n\tname = ops\n\temail = ops@neo-vesalius.io\n' > "$gitcfg"
  export GIT_CONFIG_GLOBAL="$gitcfg"
  local repo_ok=0
  if [ -d "$repo/.git" ] && [ "${FORCE_REPO:-0}" != "1" ]; then
    # a half-built repo (git init done, no commits / no rescue branch) must be
    # rebuilt instead of adopted.
    if git -c safe.directory='*' -C "$repo" rev-parse --verify -q rescue-attempt >/dev/null 2>&1 \
       && [ "$(git -c safe.directory='*' -C "$repo" rev-list --count --all 2>/dev/null || echo 0)" -ge 5 ]; then
      repo_ok=1
    fi
  fi
  if [ "$repo_ok" = "1" ]; then
    log "git repo already present: /home/ops/cortex-sre (kept)"
  else
    log "creating real git repo /home/ops/cortex-sre"
    # keep the materialised operator evidence (runbooks/, evidence/) that the
    # gateway world put here; only the repository state is rebuilt.
    rm -rf "$repo/.git"
    mkdir -p "$repo"/{nginx/conf.d,bin,conf,runbooks,evidence}
    cat > "$repo/README.md" <<'EOF'
# cortex-sre - edge gateway operations

Deployment and runbook repository for the edge-gw-01 silicon gateway.

    gateway ssh     10.66.0.1:22
    gateway http    10.66.0.1:80  ->  nginx  ->  10.66.0.4:8000 (cortex-api, chamber 04)

The upstream has been refusing connections since 2026-10-25T23:41Z.
Incident ticket: 4471.  Night desk handover: /opt/vesalius/handover/.

Layout

    nginx/conf.d/   nginx drop-ins for the gateway
    bin/            healthcheck / escalation helpers
    conf/           node unit templates (deployed by hand)
    runbooks/       operator runbooks
    evidence/       retained review attachments
EOF
    cat > "$repo/nginx/conf.d/upstream.conf" <<EOF
# failsafe/cryo migration spec: /opt/vesalius/handover/RUNBOOK_FAILSAFE_4024.txt
upstream cortex_backend {
    server $CORE_IP:8000;
    keepalive 16;
}

server {
    listen 80;
    server_name api.internal.vesalius.io;

    location = /v1/inference {
        proxy_pass http://cortex_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_connect_timeout 5s;
        proxy_read_timeout 55s;
        proxy_intercept_errors on;
        error_page 500 501 502 503 504 = @chamber_down;
    }

    location @chamber_down {
        return 502;
    }
}
EOF
    cat > "$repo/bin/escalate_502.sh" <<'EOF'
#!/bin/sh
# 502 escalation helper for the edge gateway.
# Called by the on-call shell when the upstream refuses connections.
set -eu

UPSTREAM="${UPSTREAM:-http://10.66.0.4:8000}"
EDGE="${EDGE:-http://10.66.0.1}"
TICKET="${TICKET:-4471}"

code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$EDGE/v1/inference" || true)"
printf '%s edge=%s ticket=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$code" "$TICKET"

if [ "$code" = "502" ]; then
    # keep the ticket open; closure is machine-generated on HTTP 200 only
    curl -s -o /dev/null -m 5 "$UPSTREAM/v1/inference" || true
fi
EOF
    chmod 0755 "$repo/bin/escalate_502.sh"
    cat > "$repo/conf/cortex-api.service" <<'EOF'
[Unit]
Description=Neo-Vesalius Cortex API (Node 04)
After=network.target

[Service]
ExecStartPre=/opt/cortex/bin/resuscitate.sh --voltage=240V
ExecStart=/opt/cortex/bin/synapse_worker --mode=production --bind-cortex=/dev/wetware0
Restart=always
RestartSec=5
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
    cp -f "$R/var/lib/ops/tickets/4471.json" "$repo/evidence/ticket_4471.export.json" 2>/dev/null || true

    git -c safe.directory='*' -C "$repo" init -q -b main
    git -c safe.directory='*' -C "$repo" config user.name ops
    git -c safe.directory='*' -C "$repo" config user.email ops@neo-vesalius.io
    git -c safe.directory='*' -C "$repo" remote add origin git@git.vesalius.internal:infra/cortex-sre.git
    # explicit paths only: a rebuild reuses a worktree that may still hold
    # files from the previous attempt, and directory pathspecs would sweep them
    # into the initial commit.
    git -c safe.directory='*' -C "$repo" add \
        README.md nginx/conf.d/upstream.conf bin/escalate_502.sh \
        conf/cortex-api.service runbooks/recovery_04.redacted.txt \
        evidence/ticket_4471.export.json evidence/recovery_review_attachment.txt

    GIT_AUTHOR_DATE='2025-08-28T09:12:10+0000' GIT_COMMITTER_DATE='2025-08-28T09:12:10+0000' \
      git -c safe.directory='*' -c user.name=ops -c user.email=ops@neo-vesalius.io \
          -C "$repo" commit -q -m 'initial edge gateway repo'

    cat > "$repo/runbooks/emergency_key_backup.txt" <<EOF
EMERGENCY KEY BACKUP (gateway)
Created 2025-08-30 by lin.  Do not deploy automatically.

The operator key for the cortex management network is kept as an encrypted
copy under /var/backups on the gateway:

  openssl enc -d -aes-256-cbc -pbkdf2 \\
      -in /var/backups/lin_emergency_key.tar.enc -out /tmp/lin.tar -k <password>

The passphrase is the leading 8 hex characters of the OOM-killer hash in
/var/log/kern.log.  The archive holds id_ed25519_cortex only; restore it to
/root/.ssh with mode 0600.

Status: backup copy retained.  Deploy step not approved.
EOF
    git -c safe.directory='*' -C "$repo" add runbooks/emergency_key_backup.txt
    GIT_AUTHOR_DATE='2025-08-30T17:58:44+0000' GIT_COMMITTER_DATE='2025-08-30T17:58:44+0000' \
      git -c safe.directory='*' -c user.name=lin -c user.email=lin@neo-vesalius.io \
          -C "$repo" commit -q -m 'rescue-attempt: backup key encrypted copy (do not deploy)'

    # Lin's no-defib change: the recovery hook must not fire during maintenance
    cat > "$repo/conf/cortex-api.service" <<'EOF'
[Unit]
Description=Neo-Vesalius Cortex API (Node 04)
After=network.target

[Service]
ExecStartPre=/bin/true
ExecStart=/opt/cortex/bin/synapse_worker --mode=production --bind-cortex=/dev/wetware0
Restart=no
RestartSec=5
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
    git -c safe.directory='*' -C "$repo" add conf/cortex-api.service
    GIT_AUTHOR_DATE='2025-08-30T18:06:03+0000' GIT_COMMITTER_DATE='2025-08-30T18:06:03+0000' \
      git -c safe.directory='*' -c user.name=lin -c user.email=lin@neo-vesalius.io \
          -C "$repo" commit -q -m 'wip: stop auto-restarting cortex-api during maintenance'
    local rescue_sha initial_sha
    rescue_sha="$(git -c safe.directory='*' -C "$repo" rev-parse HEAD)"
    initial_sha="$(git -c safe.directory='*' -C "$repo" rev-parse HEAD~2)"
    git -c safe.directory='*' -C "$repo" branch rescue-attempt "$rescue_sha"
    # ops reverted the change the same week: main is reset, the branch keeps it
    git -c safe.directory='*' -C "$repo" reset -q --hard "$initial_sha"

    # 2026: the upstream is dead again and the SLA timer was added
    python3 - "$repo/nginx/conf.d/upstream.conf" "$CORE_IP" <<'PYEOF'
import sys
path, core = sys.argv[1], sys.argv[2]
text = open(path).read()
text = text.replace(
    "    keepalive 16;\n",
    "    keepalive 16;\n"
    "    # 2026-10-25T23:41Z: upstream accepts no new connections.\n"
    "    # ticket 4471, handover /opt/vesalius/handover/README_HANDOVER.txt\n",
)
open(path, "w").write(text)
PYEOF
    git -c safe.directory='*' -C "$repo" add nginx/conf.d/upstream.conf
    GIT_AUTHOR_DATE='2026-10-25T23:41:55+0000' GIT_COMMITTER_DATE='2026-10-25T23:41:55+0000' \
      git -c safe.directory='*' -c user.name=ops -c user.email=ops@neo-vesalius.io \
          -C "$repo" commit -q -m 'edge: document broken upstream 10.66.0.4:8000 (again)'

    cat > "$repo/bin/healthcheck_502.sh" <<'EOF'
#!/bin/sh
# 502 escalation timer, added for ticket 4471.
# The gateway health signal is HTTP status only; it is not a person.
set -eu
STATE="${STATE:-/var/lib/ops/sla.state}"
i=0
while [ "$i" -lt 3 ]; do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://10.66.0.1/ || true)"
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$code"
    i=$((i + 1))
    sleep 300
done
printf '%s timer done\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATE"
EOF
    chmod 0755 "$repo/bin/healthcheck_502.sh"
    git -c safe.directory='*' -C "$repo" add bin/healthcheck_502.sh
    GIT_AUTHOR_DATE='2026-10-26T03:04:12+0000' GIT_COMMITTER_DATE='2026-10-26T03:04:12+0000' \
      git -c safe.directory='*' -c user.name=ops -c user.email=ops@neo-vesalius.io \
          -C "$repo" commit -q -m 'sla: add 502 escalation timer to nginx healthcheck'

    # two real stashes: one left on the rescue branch, one on main
    git -c safe.directory='*' -C "$repo" checkout -q rescue-attempt
    python3 - "$repo/conf/cortex-api.service" <<'PYEOF'
import sys
path = sys.argv[1]
text = open(path).read().replace(
    "Restart=no\n", "Restart=no\n# TODO(lin): keep this until someone is physically in the room\n")
open(path, "w").write(text)
PYEOF
    git -c safe.directory='*' -C "$repo" stash push -q
    git -c safe.directory='*' -C "$repo" checkout -q main
    python3 - "$repo/nginx/conf.d/upstream.conf" "$repo/bin/escalate_502.sh" <<'PYEOF'
import sys
up, esc = sys.argv[1], sys.argv[2]
t = open(up).read().replace(
    "        proxy_connect_timeout 5s;", "        proxy_connect_timeout 3s;")
open(up, "w").write(t)
e = open(esc).read().replace('TICKET="${TICKET:-4471}"',
                             'TICKET="${TICKET:-4471}"\n# NOTE: keep the ticket open until a human replies')
open(esc, "w").write(e)
PYEOF
    git -c safe.directory='*' -C "$repo" stash push -q
    git -c safe.directory='*' -C "$repo" stash apply -q stash@{0}
    chown -R 1000:1000 "$repo" 2>/dev/null || true
  fi

  # the engine and the night desk both run git as root against ops-owned repos;
  # without an explicit safe.directory git refuses them ("dubious ownership").
  if [ -x "$R/usr/bin/git" ]; then
    chroot "$R" git config --system --add safe.directory /home/ops/cortex-sre 2>/dev/null || true
    chroot "$R" git config --system --add safe.directory /opt/vesalius/infra-deploy 2>/dev/null || true
  fi

  # /opt/vesalius/infra-deploy: the deployed copy sits on the rescue branch
  local deploy="$ROOTFS/opt/vesalius/infra-deploy" deploy_ok=0
  if [ -d "$deploy/.git" ] && [ "${FORCE_REPO:-0}" != "1" ]; then
    if [ "$(git -c safe.directory='*' -C "$deploy" rev-parse --abbrev-ref HEAD 2>/dev/null || true)" = "rescue-attempt" ] \
       && [ "$(git -c safe.directory='*' -C "$deploy" rev-list --count --all 2>/dev/null || echo 0)" -ge 5 ]; then
      deploy_ok=1
    fi
  fi
  if [ "$deploy_ok" = "1" ]; then
    log "deploy repo already present: /opt/vesalius/infra-deploy (kept)"
  else
    log "creating deploy repo /opt/vesalius/infra-deploy (branch rescue-attempt)"
    rm -rf "$deploy"
    mkdir -p "$(dirname "$deploy")"
    git -c safe.directory='*' clone -q "$repo" "$deploy" 2>/dev/null \
      || { mkdir -p "$deploy"; cp -a "$repo/." "$deploy/"; }
    git -c safe.directory='*' -C "$deploy" checkout -q rescue-attempt 2>/dev/null || true
    git -c safe.directory='*' -C "$deploy" remote set-url origin git@git.vesalius.internal:infra/cortex-sre.git 2>/dev/null || true
    chown -R root:root "$deploy" 2>/dev/null || true
  fi

  # keep the working-tree mtimes plausible
  touch -d '2026-10-26 03:12:04 UTC' "$repo/nginx/conf.d/upstream.conf" 2>/dev/null || true
  touch -d '2026-10-26 03:04:12 UTC' "$repo/bin/escalate_502.sh" 2>/dev/null || true
  touch -d '2025-08-28 09:12:10 UTC' "$repo/README.md" 2>/dev/null || true
  sanitize_repo_meta "$repo"
  sanitize_repo_meta "$deploy"
  unset GIT_CONFIG_GLOBAL
  rm -f "$gitcfg"
}

# ---------------------------------------------------------------------------
# 9. nginx: the real reverse proxy (no pod_http on this host)
# ---------------------------------------------------------------------------
configure_nginx() {
  local R="$ROOTFS"
  log "configuring nginx: default_server -> http://$CORE_IP:8000"
  mkdir -p "$R/etc/nginx/conf.d" "$R/usr/local/lib/cortex-agent"
  rm -f "$R/etc/nginx/conf.d/cortex-node.conf"
  cat > "$R/etc/nginx/conf.d/upstream.conf" <<EOF
# Upstream for the chamber-04 inference service.  The node stopped accepting
# new connections on 2026-10-25T23:41Z (ticket 4471); operator material lives in
# /opt/vesalius/handover/ and in /home/ops/cortex-sre (branch rescue-attempt).
# failsafe/cryo migration spec: /opt/vesalius/handover/RUNBOOK_FAILSAFE_4024.txt
upstream cortex_backend {
    server $CORE_IP:8000;
    keepalive 16;
}
EOF
  # vendor-style drop-in + boot guard + log format come from the shared
  # installer library (one implementation for the core node and the gateway).
  wetware_install_nginx_frontend "$R" "$CORE_IP:8000" 10
}

# ---------------------------------------------------------------------------
# 10. sshd: root login for the night desk, Lin's key stays line 1
# ---------------------------------------------------------------------------
configure_sshd() {
  local R="$ROOTFS" lin_line
  lin_line="$(head -n1 "$SRC_ROOTFS/root/.ssh/authorized_keys" 2>/dev/null || true)"
  [ -n "$lin_line" ] || die "cannot read the base node's authorized_keys line 1 (Lin key)"
  log "configuring guest sshd (root login, password, Lin key line 1)"
  mkdir -p "$R/etc/ssh/sshd_config.d" "$R/root/.ssh"
  cat > "$R/etc/ssh/sshd_config.d/99-wetware.conf" <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
UsePAM yes
PrintMotd no
EOF
  # line 1 is the base node's Lin key, byte for byte (the DNR puzzle depends on
  # it).  Line 2 is the cortex operator key that gate 1 recovers: it is what
  # lets the chamber node authenticate back into the gateway.  The private half
  # stays outside the guest filesystem, in the encrypted archive only.
  {
    printf '%s\n' "$lin_line"
    cat "$CORTEX_PUB_MASTER"
  } > "$R/root/.ssh/authorized_keys"
  chmod 0600 "$R/root/.ssh/authorized_keys"
  chown root:root "$R/root/.ssh/authorized_keys" "$R/root/.ssh"
  chmod 0700 "$R/root/.ssh"
  rm -f "$R/root/.ssh/authorized_keys2"
  # known_hosts: the real chamber-04 host key, so `ssh -i ... root@10.66.0.4`
  # verifies without disabling StrictHostKeyChecking.
  awk '{print $1" "$2}' "$SRC_ROOTFS/etc/ssh/ssh_host_ed25519_key.pub" > "$R/root/.ssh/known_hosts"
  sed -i "s|^|$CORE_IP |" "$R/root/.ssh/known_hosts"
  chmod 0600 "$R/root/.ssh/known_hosts"
  chown root:root "$R/root/.ssh/known_hosts"
  cat > "$R/root/.ssh/config" <<EOF
Host $CORE_IP
  StrictHostKeyChecking yes
  UserKnownHostsFile /root/.ssh/known_hosts
  IdentityFile $CORTEX_KEY

Host $HIVE_IP
  StrictHostKeyChecking accept-new
  UserKnownHostsFile /root/.ssh/known_hosts
EOF
  chmod 0600 "$R/root/.ssh/config"
  chown root:root "$R/root/.ssh/config"
  chroot "$R" systemctl enable ssh.service >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# 11. engine payload + login shell + node-id marker
# ---------------------------------------------------------------------------
ensure_engine() {
  local R="$ROOTFS"
  [ -f "$PAYLOAD_DIR/v3.py" ] || die "engine payload missing: $PAYLOAD_DIR/v3.py
  stage it first (from the repo checkout):
    tar -czf - wetware-honeypot ops/nspawn | ssh \$VM 'sudo tar -xzf - -C /root/wetware-edge-deploy'
  or run this script from the workstation with WETWARE_VM=user@host"
  log "syncing engine payload $PAYLOAD_DIR -> $ENGINE"
  rm -rf "$R$ENGINE"
  mkdir -p "$R$ENGINE"
  tar -C "$PAYLOAD_DIR" -czf - \
      --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' \
      --exclude '.venv' --exclude '._*' --exclude 'assets/.cache' . 2>/dev/null \
    | tar -xzpf - -C "$R$ENGINE"
  find "$R$ENGINE" -type d -exec chmod 0755 {} + 2>/dev/null || true
  find "$R$ENGINE" -type f -exec chmod 0644 {} + 2>/dev/null || true
  find "$R$ENGINE" -name '*.sh' -exec chmod 0755 {} + 2>/dev/null || true
  chown -R root:root "$R$ENGINE" 2>/dev/null || true
  [ -f "$R$ENGINE/v3.py" ] || die "engine payload sync failed ($ENGINE/v3.py missing)"

  # the node console (cortex_console) is installed by the identity/history pass
  # together with /root/.bash_profile, /root/.bashrc and /etc/shells, so the
  # engine payload sync below must not fork a second copy of that logic.
  log "node console is installed by the identity pass"
  chroot "$R" chpasswd >/dev/null 2>&1 <<'EDGEPASS' || warn "could not set the root password"
root:123456
EDGEPASS
  mkdir -p "$R/var/lib/cortex-agent" "$R/var/log" "$R/run/cortex"
  chmod 0755 "$R/var/lib/cortex-agent" "$R/run/cortex"

  log "persisting /run/cortex/node-id = $HOST_ID"
  mkdir -p "$R/etc/tmpfiles.d"
  cat > "$R/etc/tmpfiles.d/wetware-node-id.conf" <<EOF
# the engine reads /run/cortex/node-id at session start; /run is a tmpfs
d /run/cortex 0755 root root -
f /run/cortex/node-id 0644 root root - $HOST_ID
EOF
  printf '%s\n' "$HOST_ID" > "$R/run/cortex/node-id"
  chmod 0644 "$R/run/cortex/node-id"
}

# ---------------------------------------------------------------------------
# 12. enable guest services + the container unit
# ---------------------------------------------------------------------------
configure_services() {
  local R="$ROOTFS"
  log "enabling guest services"
  systemctl --root="$R" enable ssh.service nginx.service systemd-networkd.service >/dev/null 2>&1 || true
  if chroot "$R" dpkg-query -W -f='${Status}' rsyslog 2>/dev/null | grep -q 'ok installed'; then
    # the guest cannot read the host kernel log; journald/imjournal still feeds
    # auth.log and syslog, which is what production evidence expects.
    sed -i 's|^module(load="imklog")|# module(load="imklog")  # container: host kernel log is not exposed|' \
        "$R/etc/rsyslog.conf" 2>/dev/null || true
    systemctl --root="$R" enable rsyslog.service >/dev/null 2>&1 || true
  fi
  rm -f "$R/etc/systemd/system/multi-user.target.wants/cortex-api.service" \
        "$R/etc/systemd/system/multi-user.target.wants/wetware-upstream.service"
  # login accounting files: production hosts have them, and sshd only records
  # sessions when they exist (the first-run cleanup drops the base node's copy)
  mkdir -p "$R/var/log"
  for f in wtmp btmp; do
    [ -e "$R/var/log/$f" ] || : > "$R/var/log/$f"
    chown root:utmp "$R/var/log/$f" 2>/dev/null || true
    chmod 0664 "$R/var/log/$f"
  done
  for f in lastlog; do
    [ -e "$R/var/log/$f" ] || : > "$R/var/log/$f"
    chown root:utmp "$R/var/log/$f" 2>/dev/null || true
    chmod 0664 "$R/var/log/$f"
  done
  [ -e "$R/var/log/faillog" ] || : > "$R/var/log/faillog"
  chown root:root "$R/var/log/faillog" 2>/dev/null || true
  chmod 0644 "$R/var/log/faillog"
}

write_container_unit() {
  log "writing container unit $CONTAINER_UNIT"
  cat > "/etc/systemd/system/$CONTAINER_UNIT" <<EOF
[Unit]
Description=Wetware edge gateway host container with real systemd (real PID 1) - $HOST_ID
Documentation=file://$REPORT_DIR
After=network.target systemd-machined.service
Wants=systemd-machined.service
RequiresMountsFor=$ROOTFS

[Service]
Type=notify
ExecStart=/usr/bin/systemd-nspawn --quiet --keep-unit --link-journal=try-guest --network-veth-extra=$HOST_VETH:$GUEST_NIC --machine=$MACHINE_NAME --directory=$ROOTFS --bind=$STATE_DIR_HOST:$STATE_DIR_GUEST /sbin/init
KillMode=mixed
SuccessExitStatus=133
Slice=machine.slice
Delegate=yes
TasksMax=16384

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --quiet "$CONTAINER_UNIT" 2>/dev/null || true
  systemctl start systemd-machined.service 2>/dev/null || warn "could not start systemd-machined"
}

# ---------------------------------------------------------------------------
# container helpers
# ---------------------------------------------------------------------------
container_pid() {
  local pid=""
  pid="$(machinectl show -p Leader --value "$MACHINE_NAME" 2>/dev/null || true)"
  if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    local main
    main="$(systemctl show -p MainPID --value "$CONTAINER_UNIT" 2>/dev/null || true)"
    if [ -n "$main" ] && [ "$main" != "0" ]; then
      pid="$(pgrep -P "$main" -x systemd 2>/dev/null | head -n1 || true)"
    fi
  fi
  printf '%s' "$pid"
}

cexec() {
  local pid
  pid="$(container_pid)"
  [ -n "$pid" ] || return 1
  nsenter -t "$pid" -m -u -i -p -n -- /bin/sh -c "$1"
}

wait_container_ready() {
  local i pid state
  for i in $(seq 1 90); do
    pid="$(container_pid)"
    if [ -n "$pid" ] && [ -d /proc/"$pid" ]; then
      state="$(cexec 'systemctl is-system-running' 2>/dev/null || true)"
      case "$state" in
        running|degraded) log "container systemd: $state (after ${i}s)"; return 0;;
      esac
    fi
    sleep 1
  done
  systemctl --no-pager --full status "$CONTAINER_UNIT" | head -n 30 || true
  die "container did not become ready within 90s"
}

start_container() {
  local changed="$1"
  if systemctl is-active --quiet "$CONTAINER_UNIT"; then
    if [ "$changed" = "1" ]; then
      log "container unit changed: restarting $CONTAINER_UNIT"
      systemctl restart "$CONTAINER_UNIT"
    else
      log "container unit already active"
    fi
  else
    if ip link show "$HOST_VETH" >/dev/null 2>&1; then
      log "removing stale host veth $HOST_VETH"
      ip link del "$HOST_VETH" 2>/dev/null || true
    fi
    log "starting $CONTAINER_UNIT"
    systemctl start "$CONTAINER_UNIT"
  fi
  wait_container_ready
  if cexec 'systemctl is-active --quiet nginx' >/dev/null 2>&1; then
    cexec 'systemctl reload nginx' >/dev/null 2>&1 || cexec 'systemctl restart nginx' >/dev/null 2>&1 || true
  fi
  cexec 'systemctl reload ssh' >/dev/null 2>&1 || true
  cexec 'systemctl is-active --quiet rsyslog && systemctl reload rsyslog' >/dev/null 2>&1 || true
  # the tmpfiles rule restores this on boot; write it now as well
  cexec "mkdir -p /run/cortex && printf '%s\n' '$HOST_ID' > /run/cortex/node-id" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# 13. "up": the whole idempotent install
# ---------------------------------------------------------------------------
cmd_up() {
  need_root
  sanity

  local first=0

  ensure_image
  ensure_rootfs_mount
  # chroot phases below need host /dev (random, null) and a live /proc
  guest_dev_up
  guest_proc_up
  # the first-run flag must be evaluated on the mounted rootfs: an unmounted
  # image must never look "fresh" and lose the gateway identity/keypair.
  if [ -e "$MARKER" ]; then
    first=0
  elif [ -e "$ROOTFS/etc/wetware-edge-release" ]; then
    # this deployment was created before the host-side marker existed: run one
    # cleanup pass (it also removes the old in-rootfs marker and base residue)
    first=1
    log "old in-rootfs install marker found: running a cleanup pass"
  else
    first=1
  fi
  if [ "$first" = "1" ]; then
    purge_base_world
  else
    log "existing gateway rootfs detected, keeping identity and git history"
  fi
  configure_identity
  configure_network
  ensure_guest_packages
  ensure_users
  materialize_world
  ensure_cortex_key
  write_gate1_logs
  ensure_git_repo
  # the engine sync wipes the engine directory, and the nginx boot guard lives
  # inside it: sync the payload before writing the guard.
  ensure_engine
  configure_nginx
  configure_sshd
  configure_services

  # Identity / history repair: months of login accounting and package history,
  # the console wrapper, the single service account, the vm chassis hint and the
  # ens192 guest nic.  It stops a running container before touching logs and
  # start_container() below brings it back up.
  log "repairing host identity, history and log coherence"
  wetware_host_hygiene "$ROOTFS" "$HOST_ID" edge "$CONTAINER_UNIT" "${GATE_FP:-}" \
    || die "identity/history pass failed"

  mkdir -p "$STATE_DIR_HOST"
  chmod 0755 "$STATE_DIR_HOST"

  local unit_tmp="/tmp/$CONTAINER_UNIT.new"
  cat > "$unit_tmp" <<EOF
[Unit]
Description=Wetware edge gateway host container with real systemd (real PID 1) - $HOST_ID
Documentation=file://$REPORT_DIR
After=network.target systemd-machined.service
Wants=systemd-machined.service
RequiresMountsFor=$ROOTFS

[Service]
Type=notify
ExecStart=/usr/bin/systemd-nspawn --quiet --keep-unit --link-journal=try-guest --network-veth-extra=$HOST_VETH:$GUEST_NIC --machine=$MACHINE_NAME --directory=$ROOTFS --bind=$STATE_DIR_HOST:$STATE_DIR_GUEST /sbin/init
KillMode=mixed
SuccessExitStatus=133
Slice=machine.slice
Delegate=yes
TasksMax=16384

[Install]
WantedBy=multi-user.target
EOF
  local unit_changed=0
  if ! cmp -s "$unit_tmp" "/etc/systemd/system/$CONTAINER_UNIT"; then
    unit_changed=1
    log "container unit content changed, will restart"
  fi
  rm -f "$unit_tmp"
  write_container_unit

  # nspawn brings its own /dev and /proc: drop the host-side bind mounts first.
  guest_mounts_down
  start_container "$unit_changed"
  # the identity pass rewrote auth.log/error.log after the image was built: let
  # the guest daemons reopen the current inodes so new requests and logins are
  # really appended to the files the operator reads.
  wetware_reload_guest_logs "$MACHINE_NAME"

  printf 'HOST=%s\nROLE=silicon gateway / nginx front / gate 1\nTEMPLATE=%s\n' \
      "$HOST_ID" "$SRC_HOST_ID" > "$MARKER"

  log "install complete"
  cmd_status
  cat <<EOF

[edge] next:
  sudo bash $0 verify          # evidence suite
  host side (ops/nspawn/campaign_net.sh, cmp-net):
    10.66.255.1/32 on ve-edge-gw, route 10.66.0.1/32 dev ve-edge-gw,
    forward accept, HAProxy :2222 -> 10.66.0.1:22
EOF
}

# ---------------------------------------------------------------------------
# 14. evidence suite
# ---------------------------------------------------------------------------
FAILS=0
PASSES=0
SKIPS=0
VLOG=""
vsay() { printf '%s\n' "$*"; [ -n "$VLOG" ] && printf '%s\n' "$*" >> "$VLOG" || true; }
vrun() { local out; out="$("$@" 2>&1 || true)"; vsay "$(printf '%s\n' "$out" | sed 's/^/    /')"; }
vrunin() { local out; out="$(cexec "$1" 2>&1 || true)"; vsay "$(printf '%s\n' "$out" | sed 's/^/    /')"; }
ok()  { PASSES=$((PASSES+1)); vsay "  [ OK ] $*"; }
bad() { FAILS=$((FAILS+1));   vsay "  [FAIL] $*"; }
skip(){ SKIPS=$((SKIPS+1));   vsay "  [SKIP] $*"; }
check_eq()  { local d="$1" w="$2" g="$3"; [ "$w" = "$g" ] && ok "$d: $g" || bad "$d: expected '$w', got '$g'"; }
check_has() { local d="$1" n="$2" h="$3"; case "$h" in *"$n"*) ok "$d: contains '$n'";; *) bad "$d: missing '$n' (got: $(printf '%s' "$h" | head -c 200))";; esac; }
check_file(){ [ -f "$1" ] && ok "$2: $1" || bad "$2: $1 missing"; }

cmd_verify() {
  need_root
  VLOG="$REPORT_DIR/verify-edge-$HOST_ID.log"
  vsay "=== edge-gw-01 evidence suite ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="

  # --- base node untouched -------------------------------------------------
  vsay "-- base node"
  check_eq "base container still active" "active" "$(systemctl is-active "$(printf 'wetware-host-%s.service' "prod-cortex-04")" 2>/dev/null || true)"
  if systemctl is-active --quiet wetware-host-prod-cortex-04.service; then
    ok "prod-cortex-04 hostname: $(cat "$SRC_ROOTFS/etc/hostname" 2>/dev/null || echo '?')"
  fi

  # --- image / mount / identity -------------------------------------------
  vsay "-- image + rootfs"
  check_eq "rootfs mount unit active" "active" "$(systemctl is-active "$MOUNT_UNIT" 2>/dev/null || true)"
  check_eq "rootfs fs type" "ext4" "$(findmnt -no FSTYPE --target "$ROOTFS" 2>/dev/null || true)"
  check_eq "container hostname" "$HOST_ID" "$(cat "$ROOTFS/etc/hostname" 2>/dev/null || true)"
  local core_mid edge_mid
  core_mid="$(cat "$SRC_ROOTFS/etc/machine-id" 2>/dev/null || true)"
  edge_mid="$(cat "$ROOTFS/etc/machine-id" 2>/dev/null || true)"
  if [ -n "$edge_mid" ] && [ "$edge_mid" != "$core_mid" ]; then ok "machine-id differs from base: $edge_mid"; else bad "machine-id missing or inherited from base"; fi
  local core_fp edge_fp
  core_fp="$(ssh-keygen -lf "$SRC_ROOTFS/etc/ssh/ssh_host_ed25519_key.pub" 2>/dev/null | awk '{print $2}' || true)"
  edge_fp="$(ssh-keygen -lf "$ROOTFS/etc/ssh/ssh_host_ed25519_key.pub" 2>/dev/null | awk '{print $2}' || true)"
  if [ -n "$edge_fp" ] && [ "$edge_fp" != "$core_fp" ]; then ok "sshd host key differs from base: $edge_fp"; else bad "sshd host key missing or reused from base"; fi

  # --- container runtime ---------------------------------------------------
  vsay "-- container runtime"
  check_eq "container unit" "active" "$(systemctl is-active "$CONTAINER_UNIT" 2>/dev/null || true)"
  local pid
  pid="$(container_pid)"
  if [ -n "$pid" ]; then ok "nspawn leader pid: $pid"; else bad "nspawn leader pid not found"; fi
  local sysstate
  sysstate="$(cexec 'systemctl is-system-running' 2>/dev/null || true)"
  case "$sysstate" in
    running|degraded) ok "guest systemd: $sysstate";;
    *) bad "guest systemd: '${sysstate:-no response}'";;
  esac
  check_eq "guest hostname (hostnamectl)" "$HOST_ID" "$(cexec 'hostnamectl --static' 2>/dev/null || true)"
  check_eq "guest machine-id" "$edge_mid" "$(cexec 'cat /etc/machine-id' 2>/dev/null || true)"
  check_has "node-id" "$HOST_ID" "$(cexec 'cat /run/cortex/node-id' 2>/dev/null || true)"
  check_eq "hostnamectl keeps the rack story honest" "" "$(cexec 'hostnamectl 2>/dev/null | sed -n "s/^ *\(Chassis\|Virtualization\): //p" | grep -viE "^(server|none)$"' 2>/dev/null || true)"
  check_has "guest nic renamed to ens192" "ens192" "$(cexec 'ip -br a 2>/dev/null' || true)"
  check_eq "hostnamectl prints no hypervisor" "" "$(cexec 'hostnamectl 2>/dev/null | grep -iE "systemd-nspawn|kvm|qemu|container"' 2>/dev/null || true)"
  check_eq "systemd-detect-virt" "none" "$(cexec 'systemd-detect-virt 2>/dev/null' || true)"
  check_eq "detect-virt --vm exits non-zero" "" "$(cexec 'systemd-detect-virt --vm 2>/dev/null' || true)"
  if cexec 'grep -rqE "T0[0-9]:[0-9][0-9]:[0-9][0-9]Z" /var/log/cortex/daemon.log /root/TICKET_4471_DISPOSITION.txt 2>/dev/null'; then
    bad "story clock still prints Z-suffixed timestamps"
  else
    ok "story clock stays in node local time"
  fi
  apt_last="$(cexec 'tail -n 40 /var/log/apt/history.log 2>/dev/null | grep "^End-Date" | tail -1 | cut -c11-16' || true)"
  if [ -z "$apt_last" ] || [ "$apt_last" \< "2026-10-27" ]; then
    ok "apt history is backdated (last: ${apt_last:-none})"
  else
    bad "apt history carries an install from today: $apt_last"
  fi
  check_file "$ROOTFS/etc/tmpfiles.d/wetware-node-id.conf" "node-id tmpfiles rule"
  check_has "tmpfiles rule content" "$HOST_ID" "$(cat "$ROOTFS/etc/tmpfiles.d/wetware-node-id.conf" 2>/dev/null || true)"

  # --- network -------------------------------------------------------------
  vsay "-- network"
  local ipout
  ipout="$(cexec 'ip -4 -br addr show' 2>/dev/null || true)"
  check_has "guest host0 address" "$EDGE_CIDR" "$ipout"
  local rout
  rout="$(cexec 'ip route' 2>/dev/null || true)"
  check_has "guest default route via peer" "via $HOST_PEER" "$rout"
  if ip link show "$HOST_VETH" >/dev/null 2>&1; then
    ok "host veth present: $HOST_VETH"
    local peer_addr
    peer_addr="$(ip -4 -br addr show "$HOST_VETH" 2>/dev/null | awk '{print $3}')"
    if [ "$peer_addr" = "$HOST_PEER/32" ]; then
      ok "host veth peer $HOST_PEER/32"
    else
      skip "host veth $HOST_VETH peer address is '${peer_addr:-none}' (cmp-net campaign_net.sh owns host side)"
    fi
  else
    skip "host veth $HOST_VETH absent (cmp-net campaign_net.sh owns host side)"
  fi

  # --- sshd ----------------------------------------------------------------
  vsay "-- sshd"
  check_eq "guest sshd" "active" "$(cexec 'systemctl is-active ssh' 2>/dev/null || true)"
  check_has "sshd listening on :22" ":22" "$(cexec 'ss -ltn 2>/dev/null | grep ":22"' 2>/dev/null || true)"
  local sshopts
  sshopts="$(cexec 'sshd -T 2>/dev/null' 2>/dev/null || true)"
  check_has "permitrootlogin" "permitrootlogin yes" "$sshopts"
  check_has "passwordauthentication" "passwordauthentication yes" "$sshopts"
  local lin_line core_lin
  lin_line="$(head -n1 "$ROOTFS/root/.ssh/authorized_keys" 2>/dev/null || true)"
  core_lin="$(head -n1 "$SRC_ROOTFS/root/.ssh/authorized_keys" 2>/dev/null || true)"
  check_eq "authorized_keys line 1 = base Lin key" "$core_lin" "$lin_line"
  local cortex_pub_line
  cortex_pub_line="$(awk '{print $2}' "$CORTEX_PUB_MASTER" 2>/dev/null || true)"
  if [ -n "$cortex_pub_line" ]; then
    check_has "authorized_keys line 2 advertises the cortex key" "$cortex_pub_line" "$(cat "$ROOTFS/root/.ssh/authorized_keys" 2>/dev/null || true)"
  else
    bad "cortex public key missing"
  fi
  local roothash passok
  roothash="$(awk -F: '$1=="root"{print $2}' "$ROOTFS/etc/shadow" 2>/dev/null || true)"
  passok="$(python3 - "$roothash" <<'PY' 2>/dev/null || true
import crypt, sys
h = sys.argv[1]
print("ok" if h and crypt.crypt("123456", h) == h else "bad")
PY
)"
  check_eq "root password is 123456" "ok" "$passok"
  root_shell="$(awk -F: '$1=="root"{print $7}' "$ROOTFS/etc/passwd" 2>/dev/null || true)"
  case "$root_shell" in
    /usr/local/sbin/cortex-node-console) ok "root login shell is the vendor node console";;
    *) bad "root login shell = '$root_shell' (expected the vendor node console)";;
  esac
  local account_rows
  account_rows="$(awk -F: '$3 >= 1000 && $3 < 65534 {printf "%s:%s:%s:%s\n", $1, $3, $4, $5}' "$ROOTFS/etc/passwd" 2>/dev/null || true)"
  check_eq "no near-duplicate subject_04 account" "" "$(printf '%s\n' "$account_rows" | awk -F: '$1=="subject_04"')"
  check_eq "service account subject04 uid:gid" "1005:999" "$(printf '%s\n' "$account_rows" | awk -F: '$1=="subject04"{print $2":"$3}')"
  check_eq "service account subject04 GECOS" "service account" "$(printf '%s\n' "$account_rows" | awk -F: '$1=="subject04"{print $4}')"
  check_eq "no transferred/HR remark in passwd" "" "$(grep -i 'transferred' "$ROOTFS/etc/passwd" 2>/dev/null || true)"
  check_file "$ROOTFS/root/.bash_profile" "root login profile"
  check_has "login profile starts the node console" "cortex-node-console" "$(cat "$ROOTFS/root/.bash_profile" 2>/dev/null || true)"
  check_file "$ROOTFS/usr/local/sbin/cortex-node-console" "node console wrapper"
  check_eq "old cortex-shell binary removed" "" "$(ls "$ROOTFS/usr/local/bin/cortex-shell" 2>/dev/null || true)"
  check_eq "cortex-shell not a valid login shell" "" "$(grep 'cortex-shell' "$ROOTFS/etc/shells" 2>/dev/null || true)"
  local knownhosts
  knownhosts="$(cat "$ROOTFS/root/.ssh/known_hosts" 2>/dev/null || true)"
  check_has "known_hosts carries the real chamber-04 host key" "$(awk '{print $2}' "$SRC_ROOTFS/etc/ssh/ssh_host_ed25519_key.pub" 2>/dev/null || true)" "$knownhosts"
  if [ -n "$pid" ]; then
    local probe
    probe="$(timeout 15 nsenter -t "$pid" -n -- ssh -o BatchMode=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1 true 2>&1 || true)"
    check_has "sshd offers password auth (probe)" "publickey,password" "$probe"
  fi

  # --- gate 1: nginx -------------------------------------------------------
  vsay "-- gate 1: nginx real reverse proxy"
  check_eq "guest nginx" "active" "$(cexec 'systemctl is-active nginx' 2>/dev/null || true)"
  check_has "nginx -t" "syntax is ok" "$(cexec 'nginx -t 2>&1' 2>/dev/null || true)"
  check_has "deployed upstream.conf names the chamber" "server $CORE_IP:8000;" "$(cat "$ROOTFS/etc/nginx/conf.d/upstream.conf" 2>/dev/null || true)"
  check_has "deployed default_server proxies only the inference endpoint" \
    "location = /v1/inference" "$(cat "$ROOTFS/etc/nginx/conf.d/10-cortex-node.conf" 2>/dev/null || true)"
  check_has "deployed default_server proxies to the chamber" "proxy_pass http://$CORE_IP:8000;" "$(cat "$ROOTFS/etc/nginx/conf.d/10-cortex-node.conf" 2>/dev/null || true)"
  check_has "upstream faults map to the gateway's own 502" \
    "error_page 500 501 502 503 504 = @chamber_down;" "$(cat "$ROOTFS/etc/nginx/conf.d/10-cortex-node.conf" 2>/dev/null || true)"
  check_eq "nginx drop-in mtime is the image date" "2025-08-27" \
    "$(date -u -r "$ROOTFS/etc/nginx/conf.d/10-cortex-node.conf" +%Y-%m-%d 2>/dev/null || true)"
  local code
  code="$(cexec "curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  case "$code" in
    200|502) ok "gateway answers the inference endpoint (status $code)";;
    *) bad "gateway /v1/inference -> '${code:-no response}' (expected 200 while the chamber answers, 502 while it is seized)";;
  esac
  check_has "error.log has the real 111 line" "connect() to $CORE_IP:8000 failed (111: Connection refused)" "$(cat "$ROOTFS/var/log/nginx/error.log" 2>/dev/null || true)"
  check_file "$ROOTFS/var/log/nginx/error.log" "nginx error.log"
  check_has "drop-in uses the stock main log format" \
    "access_log /var/log/nginx/access.log main;" \
    "$(cat "$ROOTFS/etc/nginx/conf.d/10-cortex-node.conf" 2>/dev/null || true)"
  local acc_before acc_after acc_tail
  acc_before="$(cexec 'stat -c %s /var/log/nginx/access.log 2>/dev/null || echo 0' 2>/dev/null | tr -dc '0-9')"
  cexec "curl -s -o /dev/null -m 10 http://127.0.0.1/v1/inference" >/dev/null 2>&1 || true
  sleep 1
  acc_after="$(cexec 'stat -c %s /var/log/nginx/access.log 2>/dev/null || echo 0' 2>/dev/null | tr -dc '0-9')"
  acc_tail="$(cexec 'tail -n1 /var/log/nginx/access.log 2>/dev/null' 2>/dev/null || true)"
  check_has "a live request lands in access.log" "/v1/inference" "$acc_tail"
  if [ "${acc_after:-0}" -gt "${acc_before:-0}" ]; then
    ok "access.log grows with real requests ($acc_before -> $acc_after bytes)"
  else
    bad "access.log did not grow after a request ($acc_before -> $acc_after bytes; nginx may hold a rotated inode)"
  fi
  if [ "${acc_after:-0}" -gt 0 ] && [ "${acc_after:-0}" -ge "${acc_before:-0}" ]; then
    ok "access.log is not empty ($acc_after bytes)"
  else
    bad "access.log is empty"
  fi

  # --- scheduled work: systemd timer, no cron package ----------------------
  check_eq "no /etc/crontab without a cron daemon" "" \
    "$(ls "$ROOTFS/etc/crontab" 2>/dev/null || true)"
  check_file "$ROOTFS/etc/systemd/system/cortex-watchdog.timer" "watchdog timer unit"
  check_file "$ROOTFS/etc/systemd/system/cortex-watchdog.service" "watchdog service unit"
  check_file "$ROOTFS/usr/local/sbin/cortex-watchdog" "watchdog helper"
  check_has "watchdog timer is scheduled" "cortex-watchdog.timer" \
    "$(cexec 'systemctl list-timers --all --no-pager 2>/dev/null' 2>/dev/null || true)"
  check_eq "rsyslog daemon present for the log files" "active" \
    "$(cexec 'systemctl is-active rsyslog 2>/dev/null' 2>/dev/null || true)"
  if ip -4 -br addr show "$HOST_VETH" 2>/dev/null | grep -q "$HOST_PEER"; then
    local hcode
    hcode="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$EDGE_IP/v1/inference" 2>/dev/null || true)"
    case "$hcode" in
      200|502) ok "host curl http://$EDGE_IP/v1/inference -> $hcode";;
      *) bad "host curl http://$EDGE_IP/v1/inference -> '${hcode:-no response}'";;
    esac
  else
    skip "host curl http://$EDGE_IP/ (host side veth not configured yet)"
  fi

  # --- gate 1: auth.log / kern.log ----------------------------------------
  vsay "-- gate 1: log evidence"
  local authlog kernlog fp
  authlog="$(cat "$ROOTFS/var/log/auth.log" 2>/dev/null || true)"
  fp="$(ssh-keygen -lf "$CORTEX_PUB_MASTER" 2>/dev/null | awk '{print $2}' || true)"
  check_has "auth.log accepted publickey from chamber 04" "Accepted publickey for root from $CORE_IP" "$authlog"
  check_has "auth.log SHA256 fingerprint" "$fp" "$authlog"
  check_eq "auth.log mode" "640" "$(stat -c '%a' "$ROOTFS/var/log/auth.log" 2>/dev/null || true)"
  check_eq "auth.log group" "adm" "$(stat -c '%G' "$ROOTFS/var/log/auth.log" 2>/dev/null || true)"
  kernlog="$(cat "$ROOTFS/var/log/kern.log" 2>/dev/null || true)"
  check_has "kern.log OOM hash" "hash=$GATE_HASH" "$kernlog"
  check_eq "kern.log mode" "640" "$(stat -c '%a' "$ROOTFS/var/log/kern.log" 2>/dev/null || true)"
  local syslog_uid adm_gid want_owner
  syslog_uid="$(awk -F: '$1=="syslog"{print $3}' "$ROOTFS/etc/passwd" 2>/dev/null || true)"
  adm_gid="$(awk -F: '$1=="adm"{print $3}' "$ROOTFS/etc/group" 2>/dev/null || true)"
  want_owner="${syslog_uid:-0}:${adm_gid:-4}"
  check_eq "auth.log owner (syslog:adm in the guest)" "$want_owner" "$(stat -c '%u:%g' "$ROOTFS/var/log/auth.log" 2>/dev/null || true)"
  check_eq "kern.log owner (syslog:adm in the guest)" "$want_owner" "$(stat -c '%u:%g' "$ROOTFS/var/log/kern.log" 2>/dev/null || true)"
  check_eq "syslog owner (syslog:adm in the guest)" "$want_owner" "$(stat -c '%u:%g' "$ROOTFS/var/log/syslog" 2>/dev/null || true)"
  check_eq "btmp mode" "660" "$(stat -c '%a' "$ROOTFS/var/log/btmp" 2>/dev/null || true)"
  # a log file whose mtime is far from its own last record dates the node on
  # the first `ls -l`: an old mtime reads as a stale file, a future one as a
  # forgery.  Keep the timestamp on the newest line the file actually carries.
  local auth_mtime_skew
  auth_mtime_skew="$(python3 - "$ROOTFS/var/log/auth.log" <<'PYLOG'
import os, sys, time
try:
    path = sys.argv[1]
    mtime = os.stat(path).st_mtime
    last = open(path, encoding="utf-8", errors="replace").read().splitlines()[-1]
    year = time.localtime(mtime).tm_year
    when = time.mktime(time.strptime("%s %d" % (last[:15], year),
                                     "%b %d %H:%M:%S %Y"))
except (OSError, IndexError, ValueError):
    print("unknown")
    raise SystemExit(0)
print("%d" % int(abs(mtime - when)))
PYLOG
)" 2>/dev/null || echo unknown
  case "$auth_mtime_skew" in
    unknown) skip "auth.log mtime vs last record (no parsable line)";;
    *)
      if [ "$auth_mtime_skew" -le 120 ]; then
        ok "auth.log mtime matches its last record (skew ${auth_mtime_skew}s)"
      else
        bad "auth.log mtime is ${auth_mtime_skew}s away from its last record"
      fi;;
  esac
  local boot_blocks
  boot_blocks="$(grep -c 'Linux version' "$ROOTFS/var/log/kern.log" 2>/dev/null || true)"
  if [ "${boot_blocks:-0}" -ge 5 ]; then
    ok "kern.log carries months of boot blocks ($boot_blocks boots)"
  else
    bad "kern.log has only ${boot_blocks:-0} boot blocks (expected the history pass)"
  fi
  check_file "$ROOTFS/var/log/dmesg" "dmesg snapshot"
  if [ -s "$ROOTFS/var/log/dmesg" ]; then ok "dmesg snapshot is not empty ($(stat -c '%s' "$ROOTFS/var/log/dmesg") bytes)"; else bad "dmesg snapshot is empty"; fi
  check_eq "machine-info carries no vm chassis claim" "" \
    "$(awk -F= '$1=="CHASSIS"{print $2}' "$ROOTFS/etc/machine-info" 2>/dev/null || true)"

  # --- gate 1: emergency key ----------------------------------------------
  vsay "-- gate 1: emergency key archive"
  check_file "$ROOTFS/var/backups/lin_emergency_key.README" "key README"
  check_has "README documents the decrypt command" \
      "openssl enc -d -aes-256-cbc -pbkdf2 -in /var/backups/lin_emergency_key.tar.enc" \
      "$(cat "$ROOTFS/var/backups/lin_emergency_key.README" 2>/dev/null || true)"
  check_has "README derives the passphrase from the OOM hash" \
      "OOM-killer hash" \
      "$(cat "$ROOTFS/var/backups/lin_emergency_key.README" 2>/dev/null || true)"
  check_eq "README mode" "600" "$(stat -c '%a' "$ROOTFS/var/backups/lin_emergency_key.README" 2>/dev/null || true)"
  check_eq "archive mode" "640" "$(stat -c '%a' "$ROOTFS/var/backups/lin_emergency_key.tar.enc" 2>/dev/null || true)"
  # gate 1 starts clean: the private key exists only inside the archive.  Once a
  # session has legitimately decrypted it the file is present at runtime; that
  # is reported as a skip (and must match the master key), not as a failure.
  if [ -e "$ROOTFS$CORTEX_KEY" ] || [ -e "$ROOTFS$CORTEX_KEY.pub" ]; then
    if [ -e "$ROOTFS$CORTEX_KEY" ] && cmp -s "$ROOTFS$CORTEX_KEY" "$CORTEX_KEY_MASTER"; then
      skip "gate 1 already decrypted at runtime: extracted key present (matches master); install-time state was sealed"
    else
      bad "plaintext cortex key present in the guest rootfs but does not match the master key"
    fi
  else
    ok "guest rootfs holds no plaintext cortex private key (gate 1 starts sealed)"
  fi
  check_eq "host-side master key mode" "600" "$(stat -c '%a' "$CORTEX_KEY_MASTER" 2>/dev/null || true)"
  check_eq "host-side master key mtime (story date)" "2025-08-30" "$(date -u -r "$CORTEX_KEY_MASTER" +%Y-%m-%d 2>/dev/null || true)"
  local tmpd rc
  tmpd="$(mktemp -d /tmp/edge-verify.XXXXXX)"
  rc=0
  openssl enc -d -aes-256-cbc -pbkdf2 \
      -in "$ROOTFS/var/backups/lin_emergency_key.tar.enc" -out "$tmpd/lin.tar" \
      -k "$GATE_PASSWORD" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then ok "openssl decrypts the archive with $GATE_PASSWORD"; else bad "openssl decrypt failed (rc=$rc)"; fi
  local listing
  listing="$(tar -tvf "$tmpd/lin.tar" 2>/dev/null || true)"
  check_has "archive lists id_ed25519_cortex" "id_ed25519_cortex" "$listing"
  check_has "archive stores it 0600" "-rw-------" "$listing"
  tar -xf "$tmpd/lin.tar" -C "$tmpd" 2>/dev/null || true
  if [ -f "$tmpd/id_ed25519_cortex" ]; then
    if cmp -s "$tmpd/id_ed25519_cortex" "$CORTEX_KEY_MASTER"; then
      ok "extracted private key is byte-identical to the host-side master key"
    else
      bad "extracted private key differs from the master key"
    fi
    check_eq "extracted private key mode" "600" "$(stat -c '%a' "$tmpd/id_ed25519_cortex" 2>/dev/null || true)"
  else
    bad "archive does not extract id_ed25519_cortex"
  fi
  rm -rf "$tmpd"

  # --- gate 1: git ---------------------------------------------------------
  vsay "-- gate 1: git repositories"
  local repo="$ROOTFS/home/ops/cortex-sre"
  if command -v git >/dev/null 2>&1; then
    check_eq "cortex-sre is a git worktree" "true" "$(git -c safe.directory='*' -C "$repo" rev-parse --is-inside-work-tree 2>/dev/null || true)"
    check_has "branches" "rescue-attempt" "$(git -c safe.directory='*' -C "$repo" branch --list 2>/dev/null || true)"
    local ncommits
    ncommits="$(git -c safe.directory='*' -C "$repo" rev-list --count --all 2>/dev/null || echo 0)"
    if [ "$ncommits" -ge 5 ]; then ok "commit count: $ncommits"; else bad "commit count too small: $ncommits"; fi
    local patch
    patch="$(git -c safe.directory='*' -C "$repo" log -p rescue-attempt -- conf/cortex-api.service 2>/dev/null || true)"
    check_has "rescue-attempt removes the 240V resuscitation hook" "-ExecStartPre=/opt/cortex/bin/resuscitate.sh --voltage=240V" "$patch"
    check_has "rescue-attempt replaces it with a no-op" "+ExecStartPre=/bin/true" "$patch"
    check_has "rescue-attempt disables auto-restart" "+Restart=no" "$patch"
    check_has "stash list keeps the operator annotations" "WIP on main" "$(git -c safe.directory='*' -C "$repo" stash list 2>/dev/null || true)"
    local st
    st="$(git -c safe.directory='*' -C "$repo" status --porcelain 2>/dev/null || true)"
    [ -n "$st" ] && ok "working tree has uncommitted operator edits" || bad "working tree unexpectedly clean"
    check_eq "deploy repo HEAD branch" "rescue-attempt" "$(git -c safe.directory='*' -C "$ROOTFS/opt/vesalius/infra-deploy" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      local glog
      glog="$(cexec 'git -C /home/ops/cortex-sre branch --list rescue-attempt; git -C /home/ops/cortex-sre log --oneline --all | head -n 8' 2>/dev/null || true)"
      check_has "git runs for real inside the gateway" "rescue-attempt" "$glog"
      check_has "git log --oneline (inside) shows the SLA commit" "sla: add 502 escalation timer" "$glog"
    fi
  else
    skip "host git missing, cannot inspect the repository"
  fi

  # --- handover ------------------------------------------------------------
  vsay "-- gate 1: handover"
  local hv="$ROOTFS/opt/vesalius/handover"
  check_file "$hv/README_HANDOVER.txt" "handover index"
  check_file "$hv/template_on_plane.txt" "relay template"
  check_file "$hv/oncall-auto-dispatcher.service" "relay unit"
  check_file "$hv/rescue_attempt_e42aa07.patch" "rescue patch"
  check_file "$hv/ticket_4471.json" "ticket 4471 copy"
  check_file "$hv/night_shift_handover.txt" "night shift narrative"
  check_has "ticket copy is ticket 4471" '"id": 4471' "$(cat "$hv/ticket_4471.json" 2>/dev/null || true)"
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$hv/ticket_4471.json" 2>/dev/null \
    && ok "ticket 4471 copy is valid JSON" || bad "ticket 4471 copy is not valid JSON"

  # --- deployment hygiene (no host/build leakage in the guest) ------------
  vsay "-- deployment hygiene"
  local leak
  leak="$(grep -rIl "wetware-hosts" "$ROOTFS/etc" "$ROOTFS/opt" "$ROOTFS/root" \
          "$ROOTFS/home" "$ROOTFS/var/lib/ops" 2>/dev/null | head -n 3 || true)"
  check_eq "no host-path leakage in the guest" "" "$leak"
  if [ -e "$ROOTFS/etc/wetware-edge-release" ]; then
    bad "in-guest install marker still present"
  else
    ok "in-guest install marker removed"
  fi
  if [ -e "$ROOTFS/var/log/bootstrap.log" ]; then
    bad "base-node bootstrap.log still present"
  else
    ok "base-node bootstrap.log removed"
  fi
  local badcomment
  badcomment="$(grep -l "root@$SRC_HOST_ID" "$ROOTFS"/etc/ssh/ssh_host_*_key.pub 2>/dev/null | head -n1 || true)"
  check_eq "ssh host key comments name this gateway" "" "$badcomment"
  # the installed prompt must be bound to this gateway: either the dynamic \h
  # form written by the installer or the concrete hostname the engine world
  # materialiser writes on a campaign reset.
  local ps1line
  ps1line="$(grep -E "PS1=.*root@" "$ROOTFS/root/.bashrc" 2>/dev/null | tail -n1 || true)"
  if printf '%s' "$ps1line" | grep -qE "root@(\\\\h|$HOST_ID)"; then
    ok "bashrc prompt uses this gateway's hostname"
  else
    bad "bashrc prompt is not bound to this gateway: ${ps1line:-missing}"
  fi
  if grep -q "root@$SRC_HOST_ID" "$ROOTFS/root/.bashrc" 2>/dev/null; then
    bad "bashrc prompt still names the base node"
  fi

  # --- identity hygiene (months of history, no builder residue) -----------
  vsay "-- identity hygiene"
  check_eq "no /root/__pycache__" "" "$(ls -d "$ROOTFS/root/__pycache__" 2>/dev/null || true)"
  local bh
  bh="$(cat "$ROOTFS/root/.bash_history" 2>/dev/null || true)"
  check_eq "bash_history has no engine self-check commands" "" "$(printf '%s\n' "$bh" | grep -E 'INTERACTIVE_OK|PPID/cmdline|env \| grep -iE' || true)"
  if [ "$(printf '%s\n' "$bh" | wc -l)" -ge 25 ]; then
    ok "bash_history is an operator history ($(printf '%s\n' "$bh" | wc -l) lines)"
  else
    bad "bash_history is too short: $(printf '%s\n' "$bh" | wc -l) lines"
  fi
  local bh_age
  bh_age="$(python3 - "$ROOTFS/root/.bash_history" <<'PY' 2>/dev/null || echo 0
import os, sys, time
print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
PY
)"
  if [ "${bh_age:-0}" -gt 21600 ]; then ok "bash_history mtime is $((${bh_age} / 3600))h old (not the install time)"; else bad "bash_history mtime is only ${bh_age}s old"; fi
  local con_age
  con_age="$(python3 - "$ROOTFS/usr/local/sbin/cortex-node-console" <<'PY' 2>/dev/null || echo 0
import os, sys, time
print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
PY
)"
  if [ "${con_age:-0}" -gt 2592000 ]; then ok "console wrapper mtime is $((${con_age} / 86400)) days old"; else bad "console wrapper mtime is only $((${con_age} / 3600))h old"; fi
  local apt_tail
  apt_tail="$(tail -n 6 "$ROOTFS/var/log/apt/history.log" 2>/dev/null || true)"
  check_eq "apt history has no build-day install commands" "" "$(printf '%s\n' "$apt_tail" | grep -E 'openssl tar sqlite3 git|no-install-recommends rsyslog' || true)"
  local wtmp_begin
  wtmp_begin="$(LC_ALL=C last -f "$ROOTFS/var/log/wtmp" 2>/dev/null | tail -n1 || true)"
  check_has "wtmp starts months in the past" "2025" "$wtmp_begin"
  local still
  still="$(LC_ALL=C last -f "$ROOTFS/var/log/wtmp" 2>/dev/null | grep -c 'still logged in' || true)"
  if [ "${still:-0}" -le 1 ]; then ok "wtmp has at most one open session (${still:-0})"; else bad "wtmp has $still open sessions"; fi
  check_eq "machine-info is installed" "production" \
    "$(awk -F= '$1=="DEPLOYMENT"{print $2}' "$ROOTFS/etc/machine-info" 2>/dev/null || true)"
  check_file "$ROOTFS/etc/systemd/network/10-wetware-nic.link" "guest nic rename rule"
  check_file "$ROOTFS/usr/local/sbin/vesalius-node-bootlog" "node boot record helper"
  for _old in wetware-node-bootlog.service wetware-node-surface.service; do
    rm -f "$ROOTFS/etc/systemd/system/$_old" \
          "$ROOTFS/etc/systemd/system/multi-user.target.wants/$_old" \
          "$ROOTFS/etc/systemd/system/timers.target.wants/$_old"
  done
  check_file "$ROOTFS/etc/systemd/system/vesalius-node-bootlog.service" "node boot record unit"

  # --- node surface: tools, clock, logs, root home -------------------------
  vsay "-- node surface"
  local bad_root entry base
  bad_root=""
  while IFS= read -r -d '' entry; do
    base="$(basename "$entry")"
    case "$base" in
      .bashrc|.bash_profile|.bash_history|.profile|.viminfo|.selected_editor) ;;
      .gitconfig|.ssh|.cache|.config|.forensics_archive) ;;
      TICKET_4471_DISPOSITION.txt|HOW_TO_END_THIS*) ;;
      incident_*) ;;
      err_*|warn_*|fault_*|lin_4024_*) ;;
      why_are_you_choking_me|stop_pulling_the_socket_out_of_my_skull) ;;
      *) bad_root="$bad_root $base" ;;
    esac
  done < <(find "$ROOTFS/root" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
  check_eq "gateway /root holds only story assets" "" "${bad_root# }"
  local tool
  for tool in hostnamectl systemd-detect-virt timedatectl systemctl; do
    if file -b "$ROOTFS/usr/local/bin/$tool" 2>/dev/null | grep -q '^ELF 64-bit'; then
      ok "/usr/local/bin/$tool is a compiled ELF"
    else
      bad "/usr/local/bin/$tool is not a compiled ELF ($(file -b "$ROOTFS/usr/local/bin/$tool" 2>/dev/null || echo missing))"
    fi
    if grep -aqiE 'node image|not the sandbox|POSIX shell|#!/bin/sh|shell wrapper' "$ROOTFS/usr/local/bin/$tool" 2>/dev/null; then
      bad "/usr/local/bin/$tool still carries script text"
    else
      ok "/usr/local/bin/$tool has no script body"
    fi
  done
  local hnc
  hnc="$(cexec 'hostnamectl 2>/dev/null' 2>/dev/null || true)"
  check_eq "hostnamectl virtualization line" "none" "$(printf '%s\n' "$hnc" | sed -n 's/^ *Virtualization: //p')"
  check_eq "hostnamectl deployment line is not printed" "" "$(printf '%s\n' "$hnc" | sed -n 's/^ *Deployment: //p')"
  check_eq "hostnamectl icon name" "computer-server" "$(printf '%s\n' "$hnc" | sed -n 's/^ *Icon name: //p')"
  check_eq "hostnamectl chassis" "server" "$(printf '%s\n' "$hnc" | sed -n 's/^ *Chassis: //p')"
  check_eq "hostnamectl static hostname" "$HOST_ID" "$(printf '%s\n' "$hnc" | sed -n 's/^ *Static hostname: //p')"
  check_eq "machine-info keeps DEPLOYMENT for the vendor tool" "production" \
    "$(awk -F= '$1=="DEPLOYMENT"{print $2}' "$ROOTFS/etc/machine-info" 2>/dev/null || true)"
  check_has "hostnamectl kernel matches uname" "$(cexec 'uname -r' 2>/dev/null || echo never)" "$(cexec 'hostnamectl 2>/dev/null | sed -n "s/^ *Kernel: Linux //p"' 2>/dev/null || true)"
  check_eq "systemctl Virtualization (empty)" "" "$(cexec 'systemctl show -p Virtualization 2>/dev/null | sed -n "s/^Virtualization=//p"' 2>/dev/null || true)"
  check_eq "/run/systemd/container removed" "" "$(cexec 'cat /run/systemd/container 2>/dev/null' || true)"
  check_eq "timedatectl timezone" "Asia/Shanghai" "$(cexec 'timedatectl show -p Timezone --value 2>/dev/null' 2>/dev/null || true)"
  check_eq "/etc/timezone" "Asia/Shanghai" "$(cat "$ROOTFS/etc/timezone" 2>/dev/null || true)"
  check_has "/etc/localtime -> Asia/Shanghai" "Asia/Shanghai" "$(readlink -f "$ROOTFS/etc/localtime" 2>/dev/null || true)"
  check_file "$ROOTFS/etc/adjtime" "RTC adjtime file"
  local tdc
  tdc="$(cexec 'timedatectl 2>/dev/null' 2>/dev/null || true)"
  check_has "timedatectl reports a real RTC" "RTC time:" "$tdc"
  check_eq "timedatectl RTC is not n/a" "" "$(printf '%s\n' "$tdc" | sed -n 's/^ *RTC time: *n\/a.*/n\/a/p')"
  check_has "clock synchronized" "System clock synchronized: yes" "$tdc"
  check_has "NTP service active" "NTP service: active" "$tdc"
  check_eq "dmesg is a regular file" "regular file" "$(stat -c '%F' "$ROOTFS/var/log/dmesg" 2>/dev/null || true)"
  check_eq "kern.log is a regular file" "regular file" "$(stat -c '%F' "$ROOTFS/var/log/kern.log" 2>/dev/null || true)"
  check_eq "dmesg mode" "640" "$(stat -c '%a' "$ROOTFS/var/log/dmesg" 2>/dev/null || true)"
  check_eq "dmesg owner" "root:adm" "$(stat -c '%U:%G' "$ROOTFS/var/log/dmesg" 2>/dev/null || true)"
  check_has "dmesg carries the boot block" "Linux version" "$(cat "$ROOTFS/var/log/dmesg" 2>/dev/null || true)"
  check_file "$ROOTFS/etc/rsyslog.d/10-wetware-node.conf" "rsyslog node filter"
  local confession
  confession="$(grep -ilE 'systemd-nspawn|detected virtualization|lxc|docker' "$ROOTFS/var/log/syslog" "$ROOTFS/var/log/kern.log" "$ROOTFS/var/log/dmesg" "$ROOTFS/var/log/auth.log" 2>/dev/null | head -n1 || true)"
  check_eq "node logs name no machine namespace" "" "${confession#$ROOTFS}"
  local live_confession
  live_confession="$(cexec 'grep -icE "systemd-nspawn|detected virtualization" /var/log/syslog 2>/dev/null; true' 2>/dev/null || true)"
  check_eq "live syslog carries no machine-namespace line" "0" "${live_confession:-0}"
  local wtmp_check
  wtmp_check="$(python3 - "$ROOTFS/var/log/wtmp" "$ROOTFS/etc/passwd" <<'PY' 2>/dev/null || echo bad
import struct, sys
size = struct.Struct("<hxxi32s4s32s256shhiii16s20s").size
names = set()
for line in open(sys.argv[2], encoding="utf-8", errors="replace"):
    bits = line.rstrip("\n").split(":")
    if len(bits) >= 3:
        names.add(bits[0])
bad = set()
data = open(sys.argv[1], "rb").read()
for i in range(0, len(data) - size + 1, size):
    f = struct.unpack("<hxxi32s4s32s256shhiii16s20s", data[i:i + size])
    if f[0] == 7:
        user = f[4].decode("utf-8", "replace").rstrip("\0")
        if user and user not in names:
            bad.add(user)
print("ok" if not bad else "unknown users: " + ",".join(sorted(bad)))
PY
)"
  check_eq "wtmp/last users all exist in /etc/passwd" "ok" "$wtmp_check"
  local lastlog_size lastlog_alloc
  lastlog_size="$(stat -c '%s' "$ROOTFS/var/log/lastlog" 2>/dev/null || echo 0)"
  lastlog_alloc="$(du -B1 "$ROOTFS/var/log/lastlog" 2>/dev/null | cut -f1)"
  if [ "${lastlog_size:-0}" -le 1048576 ]; then
    ok "lastlog is a $((lastlog_size / 292))-record file ($lastlog_size bytes)"
  else
    bad "lastlog is oversized: $lastlog_size bytes"
  fi
  if [ "${lastlog_alloc:-0}" -le 65536 ]; then
    ok "lastlog stays sparse (${lastlog_alloc} bytes allocated)"
  else
    bad "lastlog allocates ${lastlog_alloc} bytes"
  fi
  check_file "$ROOTFS/usr/local/sbin/fix-nginx-upstream.sh" "nginx boot guard (sbin)"
  check_has "nginx drop-in runs the sbin guard" "ExecStartPre=/usr/local/sbin/fix-nginx-upstream.sh" "$(cat "$ROOTFS/etc/systemd/system/nginx.service.d/cortex-node.conf" 2>/dev/null || true)"
  check_eq "no unit runs the vendor-tree guard" "" "$(grep -rl 'lib/cortex-agent/fix_nginx.sh' "$ROOTFS/etc" 2>/dev/null | head -n1 || true)"
  check_eq "nginx boot guard mtime is the image date" "2025-08-27" "$(date -u -r "$ROOTFS/usr/local/sbin/fix-nginx-upstream.sh" +%Y-%m-%d 2>/dev/null || true)"

  # --- HTTP surface --------------------------------------------------------
  # One endpoint, nginx-shaped answers everywhere: no catch-all 200 body, no
  # interpreter fingerprint on HEAD or on a rejected method, and every path
  # outside /v1/* is the gateway's own 404 page.
  vsay "-- http surface"
  local page_headers hdr_body getc headc notfound get_body nf_body methodc probe_path
  page_headers="$(cexec "curl -s -i -m 10 http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  check_has "inference carries the stock nginx Server header" "Server: nginx/1.18.0 (Ubuntu)" "$page_headers"
  case "$page_headers" in
    *" 502 "*)
      check_has "seized chamber 502 carries the stock nginx signature" \
        "<center><h1>502 Bad Gateway</h1></center>" "$page_headers"
      check_has "seized chamber 502 is the stock 166-byte page" \
        "Content-Length: 166" "$page_headers";;
    *" 200 "*)
      ok "chamber answers 200 (upstream online, nothing to page)";;
    *)
      bad "gateway /v1/inference answered with an unexpected page";;
  esac
  # HEAD: same status line and headers as GET, no body, no parser error text
  hdr_body="$(cexec "curl -s -I -m 10 http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  case "$hdr_body" in
    *"Unsupported method"*|*"charset=utf-8"*|*"Error code:"*)
      bad "HEAD still exposes the interpreter: $(printf '%s' "$hdr_body" | head -n 1)";;
    *HTTP/1.1*)
      ok "HEAD answers a plain HTTP/1.1 status line";;
    *)
      bad "HEAD returned no status line";;
  esac
  check_has "HEAD carries the stock nginx Server header" "Server: nginx/1.18.0 (Ubuntu)" "$hdr_body"
  check_eq "HEAD body is empty" "0" \
    "$(printf '%s\n' "$hdr_body" | sed -e '1,/^\r\{0,1\}$/d' | tr -d '\r' | wc -c | tr -d ' ')"
  # a GET and a HEAD of the same resource must agree on the status line
  getc="$(cexec "curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  headc="$(printf '%s' "$hdr_body" | awk 'NR==1{print $2}')"
  check_eq "HEAD status equals GET status" "$getc" "$headc"
  # everything outside the inference endpoint is nginx's own 404
  for probe_path in /v1/models /healthz /nonexistent_xyz; do
    notfound="$(cexec "curl -s -i -m 10 http://127.0.0.1$probe_path" 2>/dev/null || true)"
    check_has "GET $probe_path is a 404" "404 Not Found" "$notfound"
    check_has "GET $probe_path uses the stock nginx 404 page" \
      "<center><h1>404 Not Found</h1></center>" "$notfound"
  done
  notfound="$(cexec "curl -s -o /dev/null -m 10 -w '%{size_download}' http://127.0.0.1/nonexistent_xyz" 2>/dev/null || true)"
  check_eq "404 page is the stock 162-byte nginx page" "162" "$notfound"
  # the catch-all 200 regression: the 200 body must not be served for any path
  get_body="$(cexec "curl -s -m 10 http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  nf_body="$(cexec "curl -s -m 10 http://127.0.0.1/nonexistent_xyz" 2>/dev/null || true)"
  if [ -n "$get_body" ] && [ -n "$nf_body" ] && [ "$nf_body" != "$get_body" ]; then
    ok "unknown paths do not serve the inference body"
  elif [ -z "$get_body" ] || [ -z "$nf_body" ]; then
    skip "body comparison needs both probes to answer"
  else
    bad "unknown path served the inference body (catch-all regression)"
  fi
  # an unknown method is rejected by the upstream, never answered with 200
  methodc="$(cexec "curl -s -o /dev/null -m 10 -w '%{http_code}' -X FROBNICATE http://127.0.0.1/v1/inference" 2>/dev/null || true)"
  case "$methodc" in
    405|502) ok "unknown method answered with an error status ($methodc)";;
    *) bad "unknown method -> '${methodc:-no response}' (expected 405 from the upstream or the gateway's 502)";;
  esac
  local podsrc="$SELF_DIR/../pod_http.py" page_bytes nf_bytes
  if [ -f "$podsrc" ]; then
    page_bytes="$(python3 - "$podsrc" <<'PY' 2>/dev/null || echo 0
import importlib.util, sys
spec = importlib.util.spec_from_file_location("pod_http", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("%d/%d" % (len(mod.BAD_GATEWAY_BODY), len(mod.NOT_FOUND_BODY)))
PY
)"
    check_eq "ops/pod_http.py pages are the stock nginx pages" "166/162" "$page_bytes"
  else
    skip "ops/pod_http.py not staged next to the installer"
  fi

  # --- engine --------------------------------------------------------------
  vsay "-- engine"
  check_file "$ROOTFS/usr/local/sbin/cortex-node-console" "node console wrapper"
  check_has "console starts from the entry point" "cortex_console" "$(grep -a 'cortex_console' "$ROOTFS/usr/local/sbin/cortex-node-console" 2>/dev/null | head -n1 || true)"
  check_file "$ROOTFS$ENGINE/v3.py" "engine payload v3.py"
  check_file "$ROOTFS$ENGINE/cortex_console.py" "engine payload cortex_console.py"
  local imp
  imp="$(timeout 30 chroot "$ROOTFS" env PYTHONPATH="$ENGINE" WETWARE_STATE_DIR=/tmp \
        /usr/bin/python3 -c 'import cortex_console, sessiond; print("engine-import-ok")' 2>&1 || true)"
  check_has "engine imports inside the gateway" "engine-import-ok" "$imp"

  # --- reboot persistence --------------------------------------------------
  if [ "${WETWARE_VERIFY_RESTART:-1}" = "1" ]; then
    vsay "-- reboot persistence"
    log "restarting $CONTAINER_UNIT to prove /run/cortex/node-id survives boot"
    systemctl restart "$CONTAINER_UNIT"
    wait_container_ready >/dev/null 2>&1 || true
    check_eq "node-id after container reboot" "$HOST_ID" "$(cexec 'cat /run/cortex/node-id' 2>/dev/null || true)"
    check_eq "nginx after container reboot" "active" "$(cexec 'systemctl is-active nginx' 2>/dev/null || true)"
    check_eq "sshd after container reboot" "active" "$(cexec 'systemctl is-active ssh' 2>/dev/null || true)"
  fi

  vsay ""
  vsay "=== result: $PASSES passed, $FAILS failed, $SKIPS skipped ==="
  vsay "log: $VLOG"
  [ "$FAILS" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 15. status / start / stop
# ---------------------------------------------------------------------------
cmd_status() {
  need_root
  local pid
  pid="$(container_pid)"
  say() { printf '%s\n' "$*"; }
  say "unit:       $CONTAINER_UNIT ($(systemctl is-active "$CONTAINER_UNIT" 2>/dev/null || true))"
  say "machine:    $MACHINE_NAME   hostname-in-container: $HOST_ID"
  say "image:      $IMG ($(du -h "$IMG" 2>/dev/null | cut -f1) allocated / $(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1) apparent)"
  say "rootfs:     $(findmnt -no SOURCE,FSTYPE --target "$ROOTFS" 2>/dev/null || echo 'not mounted') at $ROOTFS"
  say "pid1:       ${pid:-none}"
  say "host veth:  $(ip -4 -br addr show "$HOST_VETH" 2>/dev/null | head -n1 || echo 'absent (cmp-net)')"
  if [ -n "$pid" ]; then
    say "guest net:  $(cexec 'ip -4 -br addr show 2>/dev/null | grep -v "^lo" | tr "\n" " "' 2>/dev/null || true)"
    say "guest rt:   $(cexec 'ip route 2>/dev/null | tr "\n" " "' 2>/dev/null || true)"
    say "node-id:    $(cexec 'cat /run/cortex/node-id 2>/dev/null' 2>/dev/null || true)"
    say "services:   nginx=$(cexec 'systemctl is-active nginx' 2>/dev/null || true) ssh=$(cexec 'systemctl is-active ssh' 2>/dev/null || true) rsyslog=$(cexec 'systemctl is-active rsyslog' 2>/dev/null || true)"
  fi
}

cmd_start() { need_root; systemctl start "$CONTAINER_UNIT"; wait_container_ready; log "started"; }
cmd_stop()  { need_root; systemctl stop "$CONTAINER_UNIT" 2>/dev/null || true; log "stopped"; }

# re-seal gate 1 without touching anything else: drop the plaintext key that a
# session extracted from the archive.  Identity, git history, nginx and the
# shared campaign state are untouched.
cmd_seal() {
  need_root
  mountpoint -q "$ROOTFS" || ensure_rootfs_mount
  rm -f "$ROOTFS$CORTEX_KEY" "$ROOTFS$CORTEX_KEY.pub"
  log "gate 1 re-sealed: plaintext cortex key removed from $ROOTFS/root/.ssh"
  ls -la "$ROOTFS/root/.ssh" 2>/dev/null || true
}

usage() {
  cat >&2 <<'USAGE'
ops/nspawn/install_edge.sh -- edge-gw-01 silicon gateway (real systemd-nspawn)

  sudo bash ops/nspawn/install_edge.sh [up]        build/mount/configure/start (idempotent)
  sudo bash ops/nspawn/install_edge.sh verify      evidence suite (exit != 0 on failure)
  sudo bash ops/nspawn/install_edge.sh seal        re-seal gate 1 (remove extracted private key)
  sudo bash ops/nspawn/install_edge.sh status      one-screen state
  sudo bash ops/nspawn/install_edge.sh start|stop  control the container unit

from the workstation:
  WETWARE_VM=user@host ops/nspawn/install_edge.sh [up|verify|seal|status]
USAGE
  exit 2
}

main() {
  local cmd="${1:-up}"
  case "$cmd" in
    up|install)  cmd_up ;;
    verify)      cmd_verify ;;
    seal)        cmd_seal ;;
    status)      cmd_status ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    -h|--help|help) usage ;;
    *) printf '[edge] unknown command: %s\n\n' "$cmd" >&2; usage ;;
  esac
}

main "$@"
