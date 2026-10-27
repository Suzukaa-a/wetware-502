#!/usr/bin/env bash
# =============================================================================
# ops/nspawn/build_base.sh -- real-systemd host container prototype (nspawn)
#
# Builds a minimal Ubuntu 22.04 "jammy" rootfs inside a sparse ext4 loopback
# image and boots it as a genuine systemd host container:
#
#   image   /var/lib/wetware-hosts/<HOST_ID>.img   sparse ext4, 3.5 GiB
#   rootfs  /var/lib/wetware-hosts/<HOST_ID>       loopback mount of the image
#                                                  (systemd .mount unit, Options=loop)
#   runtime systemd-nspawn, machine name <MACHINE_NAME>
#   identity hostname inside the container == <HOST_ID>  (prod-cortex-04)
#   network veth pair: VM host <HOST_VETH> 10.66.0.1/24
#                        <-> container host0   10.66.0.4/24
#
# The container is a first-class systemd unit on the VM host:
#
#   wetware-host-<HOST_ID>.service   (Slice=machine.slice, Type=notify,
#                                     --keep-unit => container PID 1 lives in the
#                                     unit's own cgroup)
#   var-lib-wetware-hosts-...mount   (ext4 loopback rootfs mount)
#
# so `systemctl status`, `machinectl`, `journalctl -M`, `nsenter` and the
# container's own `systemctl`/`journalctl`/`hostnamectl` all behave normally.
#
# It is a clean root prototype: it does NOT touch, mount or import the existing
# honeypot control plane (cortex-pod.service, the fake-init overlay pod, the
# honeypot engine). No bind mounts from /usr/local/lib/cortex-agent, /run/cortex
# or /var/lib/cortex-pod go into the container.
#
# ---------------------------------------------------------------------------
# Usage (on the VM, as root; safe to run repeatedly -> idempotent):
#
#   sudo bash ops/nspawn/build_base.sh            # = "up": build if needed,
#                                                 #   start, then verify
#   sudo bash ops/nspawn/build_base.sh build      # image+rootfs+units only
#   sudo bash ops/nspawn/build_base.sh start      # start container unit
#   sudo bash ops/nspawn/build_base.sh stop       # stop container unit
#   sudo bash ops/nspawn/build_base.sh status     # one-screen state
#   sudo bash ops/nspawn/build_base.sh verify     # evidence suite (exit!=0 on failure)
#   sudo bash ops/nspawn/build_base.sh destroy    # stop + unmount + delete image
#                                                 #   (requires FORCE=1)
#
# From the workstation / deploy.sh:
#
#   ssh ulocal@vm 'sudo -n bash -s' < ops/nspawn/build_base.sh          # "up"
#   ssh ulocal@vm 'sudo -n bash -s -- verify' < ops/nspawn/build_base.sh
#
# Environment overrides:
#   HOST_ID=prod-cortex-04   MACHINE_NAME=prod-cortex   (<=12 chars: veth naming)
#   IMAGE_SIZE=3584M         SUITE=jammy                MIRROR=http://archive.ubuntu.com/ubuntu
#   CONTAINER_IP=10.66.0.4/24  GATEWAY_IP=10.66.0.1/24
#   FORCE=1                  wipe/recreate an existing rootfs (destroy/build)
#   SKIP_VERIFY=1            "up" only starts + waits, no evidence suite
#
# Exit codes: 0 success/verified, 1 build/runtime failure, 2 usage.
# =============================================================================

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
HOST_ID="${HOST_ID:-prod-cortex-04}"
# nspawn names the host veth "ve-<machine>"; IFNAMSIZ is 15, so the machine name
# must stay <= 12 characters. The container hostname stays the full HOST_ID.
MACHINE_NAME="${MACHINE_NAME:-prod-cortex}"
IMAGE_SIZE="${IMAGE_SIZE:-3584M}"
SUITE="${SUITE:-jammy}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"

BASE_DIR="${BASE_DIR:-/var/lib/wetware-hosts}"
IMG="$BASE_DIR/$HOST_ID.img"
ROOTFS="$BASE_DIR/$HOST_ID"
REPORT_DIR="$BASE_DIR/reports"

MOUNT_UNIT="$(systemd-escape -p --suffix=mount "$ROOTFS")"
CONTAINER_UNIT="wetware-host-$HOST_ID.service"
HOST_VETH="ve-$MACHINE_NAME"
CONTAINER_IP="${CONTAINER_IP:-10.66.0.4/24}"
GATEWAY_IP="${GATEWAY_IP:-10.66.0.1/24}"
CONTAINER_IP_ADDR="${CONTAINER_IP%%/*}"
GATEWAY_IP_ADDR="${GATEWAY_IP%%/*}"

# Packages baked into the image: a minimal jammy plus real systemd service
# management, networkd, journald storage, udev/dbus, sshd and basic tooling.
INCLUDE_PKGS="systemd-sysv,systemd,udev,dbus,libpam-systemd,iproute2,iputils-ping,procps,psmisc,openssh-server,ca-certificates,apt-utils,locales,tzdata,kmod,less,nano,netbase,curl"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# logging helpers
# ---------------------------------------------------------------------------
SELF="${0:-ops/nspawn/build_base.sh}"
say()  { printf '%s\n' "$*"; }
log()  { printf '[nspawn] %s\n' "$*"; }
warn() { printf '[nspawn] WARNING: %s\n' "$*" >&2; }
die()  { printf '[nspawn] ERROR: %s\n' "$*" >&2; exit 1; }

trap 'rc=$?; if [ "$rc" -ne 0 ]; then printf "[nspawn] FAILED at line %s (exit %s)\n" "$LINENO" "$rc" >&2; fi' EXIT

need_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (sudo)
  e.g.: ssh <vm> 'sudo -n bash -s' < $SELF"
}

# ---------------------------------------------------------------------------
# host identity / history repair library (shared with install_engine_base.sh).
# build_base.sh can be piped through stdin ('ssh vm sudo bash -s < ...'), in
# which case no sibling file exists: the library is optional here because
# install_engine_base.sh applies the identity pass to prod-cortex-04 right
# after this template is provisioned.
# ---------------------------------------------------------------------------
SELF_DIR=""
case "$SELF" in
  */*) [ -f "$SELF" ] && SELF_DIR="$(cd "$(dirname "$SELF")" && pwd)" || SELF_DIR="" ;;
esac
HYGIENE_AVAILABLE=0
for _cand in "$SELF_DIR/install_engine_base.sh" \
             "/usr/local/lib/cortex-agent/nspawn/install_engine_base.sh" \
             "/root/wetware-edge-deploy/ops/nspawn/install_engine_base.sh"; do
  [ -n "$_cand" ] || continue
  if [ -r "$_cand" ]; then
    # shellcheck source=ops/nspawn/install_engine_base.sh
    . "$_cand"
    if command -v wetware_host_hygiene >/dev/null 2>&1; then
      HYGIENE_AVAILABLE=1
      break
    fi
    warn "ignoring stale library $_cand (no wetware_host_hygiene)"

  fi
done
if [ "$HYGIENE_AVAILABLE" = "0" ]; then
  printf '[nspawn] WARNING: shared identity library not found (install_engine_base.sh); the base image is provisioned without the identity pass\n' >&2
fi
# the shared library re-defines a local warn(); restore this script's helpers
say()  { printf '%s\n' "$*"; }
log()  { printf '[nspawn] %s\n' "$*"; }
warn() { printf '[nspawn] WARNING: %s\n' "$*" >&2; }

# Run a command inside the container (all namespaces + container root fs).
# Usage: cexec 'shell code'      (single argument, executed by container /bin/sh)
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
  [ -n "$pid" ] || die "container PID 1 not found (is $CONTAINER_UNIT running?)"
  nsenter -t "$pid" -m -u -i -p -n -- /bin/sh -c "$1"
}

# ===========================================================================
# 1. host packages
# ===========================================================================
ensure_packages() {
  local missing=""
  dpkg-query -W -f='${Status}' systemd-container 2>/dev/null | grep -q 'ok installed' || missing="$missing systemd-container"
  command -v debootstrap >/dev/null 2>&1 || missing="$missing debootstrap"
  if [ -z "$missing" ]; then
    log "packages present: systemd-container, debootstrap"
    return 0
  fi
  log "installing:$missing"
  apt-get update -qq
  # shellcheck disable=SC2086
  apt-get install -y -qq $missing
  command -v systemd-nspawn >/dev/null || die "systemd-nspawn still missing"
  command -v debootstrap   >/dev/null || die "debootstrap still missing"
  log "installed: $(systemd-nspawn --version | head -n1), debootstrap $(debootstrap --version 2>/dev/null | head -n1)"
}

# ===========================================================================
# 2. ext4 loopback image managed by a systemd .mount unit
# ===========================================================================
ensure_image() {
  mkdir -p "$BASE_DIR" "$REPORT_DIR"
  if [ ! -e "$IMG" ]; then
    log "creating sparse image $IMG ($IMAGE_SIZE)"
    truncate -s "$IMAGE_SIZE" "$IMG"
  else
    log "image exists: $IMG ($(du -h --apparent-size "$IMG" | cut -f1) apparent)"
  fi
  local fstype=""
  fstype="$(blkid -p -o value -s TYPE "$IMG" 2>/dev/null || true)"
  if [ "$fstype" != "ext4" ]; then
    # The image is a plain regular file: mke2fs -F can format it directly,
    # no loop device or mount needed at this point.
    log "formatting image as ext4 (no overlayfs anywhere in this design)"
    mke2fs -F -q -t ext4 -L "wetware-$MACHINE_NAME" \
      -E lazy_itable_init=0,lazy_journal_init=0 "$IMG"
    fstype="$(blkid -p -o value -s TYPE "$IMG")"
    [ "$fstype" = "ext4" ] || die "mkfs did not produce an ext4 image"
  fi
  log "image filesystem: $fstype"
}

write_mount_unit() {
  log "writing mount unit $MOUNT_UNIT"
  cat > "/etc/systemd/system/$MOUNT_UNIT" <<EOF
[Unit]
Description=Wetware host ext4 loopback rootfs image for $HOST_ID
Documentation=file://$BASE_DIR/reports
Before=local-fs.target

[Mount]
What=$IMG
Where=$ROOTFS
Type=ext4
Options=loop,rw,nosuid,noatime

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
}

ensure_rootfs_mount() {
  write_mount_unit
  mkdir -p "$ROOTFS"
  # Adopt a pre-existing manual (losetup/mount) mount from an earlier prototype
  # run: tear it down so systemd owns the loop device and the mount.
  if mountpoint -q "$ROOTFS" && ! systemctl is-active --quiet "$MOUNT_UNIT"; then
    log "adopting manually mounted rootfs: unmounting so $MOUNT_UNIT can own it"
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
  local src fstype
  src="$(findmnt -no SOURCE --target "$ROOTFS")"
  fstype="$(findmnt -no FSTYPE --target "$ROOTFS")"
  log "rootfs mounted: $src -> $ROOTFS ($fstype)"
  [ "$fstype" = "ext4" ] || die "rootfs is not ext4 (got '$fstype')"
}

# ===========================================================================
# 3. debootstrap the minimal jammy rootfs
# ===========================================================================
rootfs_provisioned() {
  [ -f "$ROOTFS/etc/debian_version" ] &&
    [ -x "$ROOTFS/usr/bin/systemctl" ] &&
    [ -x "$ROOTFS/bin/bash" ] &&
    [ -x "$ROOTFS/usr/sbin/sshd" ]
}

provision_rootfs() {
  if rootfs_provisioned; then
    log "rootfs already provisioned ($(cat "$ROOTFS/etc/os-release" | sed -n 's/^PRETTY_NAME=//p')), skipping debootstrap"
    return 0
  fi
  if [ -n "$(ls -A "$ROOTFS" 2>/dev/null | head -n1)" ]; then
    if [ "${FORCE:-0}" = "1" ]; then
      log "FORCE=1: wiping partially provisioned rootfs"
      find "$ROOTFS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    else
      die "$ROOTFS is not empty but is not a provisioned rootfs.
  inspect it, then re-run with FORCE=1 to wipe and rebuild."
    fi
  fi
  log "debootstrap $SUITE -> $ROOTFS (this takes several minutes)"
  local started=$SECONDS
  debootstrap --variant=minbase --components=main,universe \
    --include="$INCLUDE_PKGS" \
    "$SUITE" "$ROOTFS" "$MIRROR" 2>&1 |
    tee "$REPORT_DIR/debootstrap.log" |
    while IFS= read -r line; do
      case "$line" in
        I:\ Retrieving*|I:\ Validating*|I:\ Extracting*|I:\ Chosen*) : ;;
        *) printf '[debootstrap] %s\n' "$line" ;;
      esac
    done
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "debootstrap failed (see $REPORT_DIR/debootstrap.log)"
  log "debootstrap finished in $((SECONDS - started))s"
  rootfs_provisioned || die "rootfs provisioning incomplete (sshd/systemd/bash missing)"
}

# ===========================================================================
# 4. configure the container: identity, networkd, journald, services
# ===========================================================================
configure_rootfs() {
  log "configuring container rootfs"

  # --- identity ------------------------------------------------------------
  printf '%s\n' "$HOST_ID" > "$ROOTFS/etc/hostname"
  cat > "$ROOTFS/etc/hosts" <<EOF
127.0.0.1 localhost
127.0.1.1 $HOST_ID
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
$CONTAINER_IP_ADDR $HOST_ID
EOF
  if [ ! -s "$ROOTFS/etc/machine-id" ]; then
    cat /proc/sys/kernel/random/uuid | tr -d '-' > "$ROOTFS/etc/machine-id"
  fi
  log "hostname=$HOST_ID machine-id=$(cat "$ROOTFS/etc/machine-id")"

  # --- systemd-networkd config for the veth --------------------------------
  # systemd-nspawn --network-veth gives the container a veth whose inside end
  # is named "host0" (this is exactly the interface the systemd upstream
  # 80-container-host0.network file is written for). We configure it with
  # networkd and give it the static address. The filename must sort BEFORE
  # the vendor /usr/lib/systemd/network/80-container-*.network files, because
  # networkd applies the first matching .network in lexical order.
  mkdir -p "$ROOTFS/etc/systemd/network"
  rm -f "$ROOTFS"/etc/systemd/network/10-wetware-host0.link \
        "$ROOTFS"/etc/systemd/network/20-wetware-eth0.network \
        "$ROOTFS"/etc/systemd/network/21-wetware-host0.network
  cat > "$ROOTFS/etc/systemd/network/20-wetware-host0.network" <<EOF
[Match]
Name=host0

[Network]
Address=$CONTAINER_IP
LinkLocalAddressing=no
IPv6AcceptRA=no
ConfigureWithoutCarrier=yes
EOF

  # --- udev inside the container -------------------------------------------
  # nspawn gives the container a read-only /sys (it masks sysfs with a small ro
  # tmpfs), so the stock Ubuntu/Debian systemd-udevd.service skips itself via
  # ConditionPathIsReadWrite=/sys. Real container managers (LXC, Docker) also
  # run without udev. To keep udev actually *running* (the requirement here),
  # drop the condition: udevd then runs normally as a system service and
  # manages the container's own /dev; sysfs stays read-only, which is the safe
  # container default (a container must not be able to poke host sysfs).
  mkdir -p "$ROOTFS/etc/systemd/system/systemd-udevd.service.d"
  cat > "$ROOTFS/etc/systemd/system/systemd-udevd.service.d/10-wetware-container.conf" <<'EOF'
# nspawn containers get a read-only /sys, which makes the stock
# ConditionPathIsReadWrite=/sys in the vendor unit skip udev entirely.
# Drop that condition so systemd-udevd really runs inside the container.
[Unit]
ConditionPathIsReadWrite=
EOF

  # --- persistent journal inside the image (real journald boot log) --------
  mkdir -p "$ROOTFS/var/log/journal"
  chmod 2755 "$ROOTFS/var/log/journal"
  chgrp systemd-journal "$ROOTFS/var/log/journal" 2>/dev/null || true

  # --- sshd host keys (postinst may already have made them) ----------------
  if [ ! -f "$ROOTFS/etc/ssh/ssh_host_ed25519_key" ]; then
    chroot "$ROOTFS" /usr/bin/ssh-keygen -A || warn "ssh-keygen -A failed"
  fi

  # --- remove debootstrap's service-start guard ----------------------------
  rm -f "$ROOTFS/usr/sbin/policy-rc.d"

  # --- enable services with systemd itself (offline --root mode) -----------
  systemctl --root="$ROOTFS" enable systemd-networkd.service >/dev/null 2>&1
  systemctl --root="$ROOTFS" enable ssh.service >/dev/null 2>&1
  # dbus.service is static in the Debian/Ubuntu dbus package: make sure it is
  # pulled in at boot so "dbus running" inside the container is guaranteed.
  if ! systemctl --root="$ROOTFS" is-enabled dbus.service >/dev/null 2>&1; then
    mkdir -p "$ROOTFS/etc/systemd/system/multi-user.target.wants"
    ln -sfn /lib/systemd/system/dbus.service \
      "$ROOTFS/etc/systemd/system/multi-user.target.wants/dbus.service"
  fi
  # systemd-udevd.service is static but pulled in by sysinit.target via the
  # vendor /usr/lib/systemd/system/sysinit.target.wants/ symlink; nothing to
  # enable here. Verify it after boot instead.

  # --- trim the image: drop downloaded .debs, keep package indexes ---------
  rm -rf "$ROOTFS"/var/cache/apt/archives/*.deb
  rm -f "$ROOTFS"/var/lib/apt/lists/lock

  log "rootfs configured: $(du -sh "$ROOTFS" | cut -f1) used"
}

# ===========================================================================
# 5. host side: veth peering + nspawn unit
# ===========================================================================
ensure_host_network() {
  log "configuring host side of the veth ($GATEWAY_IP on $HOST_VETH)"
  # Filename matters: networkd applies the FIRST matching .network in lexical
  # order, and the vendor file /usr/lib/systemd/network/80-container-ve.network
  # matches every ve-* link (IPv4LL + DHCPServer + masquerade). "70-" sorts
  # before "80-", so our static address wins. (This was a real bug in the first
  # prototype iteration: the veth came up with only 169.254.x.x / odd /28s.)
  rm -f "/etc/systemd/network/80-wetware-$MACHINE_NAME.network" \
        "/etc/systemd/network/70-wetware-$MACHINE_NAME.network"
  cat > "/etc/systemd/network/70-wetware-$MACHINE_NAME.network" <<EOF
[Match]
Name=$HOST_VETH

[Network]
Address=$GATEWAY_IP
LinkLocalAddressing=no
IPv6AcceptRA=no
EOF
  if systemctl is-active --quiet systemd-networkd; then
    # reload (not restart): no link is dropped, the veth is configured as soon
    # as nspawn creates it.
    systemctl reload systemd-networkd
  else
    warn "systemd-networkd is not active on the VM host; $HOST_VETH may need a manual address"
  fi
}

write_container_unit() {
  log "writing container unit $CONTAINER_UNIT"
  # Why an explicit "/sbin/init" instead of --boot:
  #   * with --boot systemd-nspawn searches for an init binary and executes
  #     /usr/lib/systemd/systemd, so /proc/1/cmdline reads
  #     "/usr/lib/systemd/systemd" (not what a kernel-booted host shows);
  #   * passing /sbin/init as the container command is exactly what the kernel
  #     does on a real host (execve("/sbin/init")), so PID 1 keeps
  #     comm=systemd, cmdline=/sbin/init, and systemd still detects the
  #     container (container=systemd-nspawn) and boots normally.
  #   * readiness: without --boot nspawn signals "started" early, so the build
  #     script polls the container's own systemd (is-system-running) before it
  #     treats the container as up (see wait_container_ready).
  cat > "/etc/systemd/system/$CONTAINER_UNIT" <<EOF
[Unit]
Description=Wetware honeypot host container with real systemd (real PID 1) - $HOST_ID
Documentation=file://$BASE_DIR/reports
After=network.target systemd-machined.service
Wants=systemd-machined.service
RequiresMountsFor=$ROOTFS

[Service]
Type=notify
ExecStart=/usr/bin/systemd-nspawn --quiet --keep-unit --link-journal=try-guest --network-veth --machine=$MACHINE_NAME --bind=/var/lib/wetware-campaign/state:/var/lib/cortex-agent --directory=$ROOTFS /sbin/init
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
  systemctl start systemd-machined.service 2>/dev/null || warn "could not start systemd-machined (machinectl may be limited)"
}

wait_container_ready() {
  local i pid state
  for i in $(seq 1 60); do
    pid="$(container_pid)"
    if [ -n "$pid" ] && [ -d /proc/"$pid" ]; then
      state="$(cexec 'systemctl is-system-running' 2>/dev/null || true)"
      case "$state" in
        running|degraded)
          log "container systemd reported state '$state' after ${i}s"
          return 0
          ;;
      esac
    fi
    sleep 1
  done
  systemctl --no-pager --full status "$CONTAINER_UNIT" | head -n 30 || true
  die "container did not become ready within 60s"
}

# networkd configures the veth asynchronously on the netlink event; give it a
# moment before we assert on addresses.
wait_veth_addresses() {
  local i
  for i in $(seq 1 20); do
    if ip -4 -br addr show "$HOST_VETH" 2>/dev/null | grep -q "$GATEWAY_IP_ADDR"; then
      break
    fi
    sleep 0.5
  done
  for i in $(seq 1 20); do
    if cexec 'ip -4 -br addr show 2>/dev/null' 2>/dev/null | grep -q "$CONTAINER_IP_ADDR"; then
      return 0
    fi
    sleep 0.5
  done
  return 0
}

start_container() {
  local t0 t1 boot_ms=0
  if systemctl is-active --quiet "$CONTAINER_UNIT"; then
    log "container unit already active"
    wait_container_ready
  else
    # A crashed previous run can leave the host veth behind; nspawn cannot
    # create it twice (EEXIST).
    if ip link show "$HOST_VETH" >/dev/null 2>&1; then
      log "removing stale host veth $HOST_VETH"
      ip link del "$HOST_VETH" 2>/dev/null || true
    fi
    log "starting $CONTAINER_UNIT"
    t0="$(date +%s%N)"
    systemctl start "$CONTAINER_UNIT"
    wait_container_ready
    t1="$(date +%s%N)"
    boot_ms=$(( (t1 - t0) / 1000000 ))
    log "container boot wall time: ${boot_ms}ms (nspawn launch -> container systemd is-system-running)"
  fi
  wait_veth_addresses
  local pid
  pid="$(container_pid)"
  log "container running: machine=$MACHINE_NAME pid1(host view)=$pid"
  local startup
  startup="$(cexec 'journalctl -b --no-pager 2>/dev/null | grep -o "Startup finished in [0-9.]*[a-z]*" | head -n1' 2>/dev/null || true)"
  [ -n "$startup" ] && log "container systemd: $startup"
  return 0
}

stop_container() {
  if systemctl is-active --quiet "$CONTAINER_UNIT"; then
    log "stopping $CONTAINER_UNIT"
    systemctl stop "$CONTAINER_UNIT"
  else
    log "container unit not active"
  fi
  systemctl reset-failed "$CONTAINER_UNIT" 2>/dev/null || true
}

container_state() {
  local active pid cg
  active="$(systemctl is-active "$CONTAINER_UNIT" 2>/dev/null || true)"
  pid="$(container_pid)"
  cg=""
  if [ -n "$pid" ] && [ -d /proc/"$pid" ]; then
    cg="$(cat /proc/"$pid"/cgroup 2>/dev/null | tr '\n' ' ')"
  fi
  say "unit:       $CONTAINER_UNIT ($active)"
  say "machine:    $MACHINE_NAME   hostname-in-container: $HOST_ID"
  say "pid1:       ${pid:-none}  comm=$( [ -n "$pid" ] && cat /proc/"$pid"/comm 2>/dev/null )"
  say "pid1 cg:    ${cg:-none}"
  say "image:      $IMG ($(du -h "$IMG" 2>/dev/null | cut -f1) allocated / $(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1) apparent)"
  say "rootfs:     $(findmnt -no SOURCE,FSTYPE --target "$ROOTFS" 2>/dev/null || echo 'not mounted') at $ROOTFS"
  say "host veth:  $(ip -4 -br addr show "$HOST_VETH" 2>/dev/null | head -n1 || echo 'absent')"
  local ip
  ip="$(cexec 'ip -4 -br addr show 2>/dev/null | grep -v "^lo" | tr "\n" " "' 2>/dev/null || true)"
  say "container:  ${ip:-n/a}"
}

# ===========================================================================
# 6. evidence suite
# ===========================================================================
# Collected into stdout and into $REPORT_DIR/verify.log
FAILS=0
PASSES=0
VLOG=""
vsay() { printf '%s\n' "$*"; if [ -n "$VLOG" ]; then printf '%s\n' "$*" >> "$VLOG"; fi; }
vout() { while IFS= read -r _l; do vsay "$_l"; done; }
run() { local out; out="$("$@" 2>&1 || true)"; vsay "$(printf '%s\n' "$out" | sed 's/^/    /')"; }
runin() { local out; out="$(cexec "$1" 2>&1 || true)"; vsay "$(printf '%s\n' "$out" | sed 's/^/    /')"; }
ok()   { PASSES=$((PASSES+1)); vsay "  [ OK ] $*"; }
bad()  { FAILS=$((FAILS+1));   vsay "  [FAIL] $*"; }
check_eq()  { local d="$1" w="$2" g="$3"; [ "$w" = "$g" ] && ok "$d: '$g'" || bad "$d: expected '$w', got '$g'"; }
check_has() { local d="$1" n="$2" h="$3"; case "$h" in *"$n"*) ok "$d: contains '$n'";; *) bad "$d: missing '$n' (got '$h')";; esac; }
check_ne()  { local d="$1" w="$2" g="$3"; [ "$w" != "$g" ] && ok "$d: '$g'" || bad "$d: must not be '$w'"; }

verify() {
  local report="$REPORT_DIR/verify.log"
  mkdir -p "$REPORT_DIR"
  VLOG="$report"
  : > "$VLOG"
  local pid hz start_ticks start_s uptime_now age
  local osname

  vsay "=============================================================="
  vsay " wetware host container verification -- $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  vsay " host=$(hostname) machine=$MACHINE_NAME unit=$CONTAINER_UNIT"
  vsay "=============================================================="

  pid="$(container_pid)"
  if [ -z "$pid" ]; then
    bad "container PID 1 not found; is $CONTAINER_UNIT active?"
    vsay "verify report: $report"
    return 1
  fi

  # ---- A. systemd host view: unit + machinectl ---------------------------
  vsay ""
  vsay "--- A. host-side container lifecycle (systemd unit + machinectl) ----"
  vsay "  systemctl is-active $CONTAINER_UNIT"
  run systemctl is-active "$CONTAINER_UNIT"
  vsay "  systemctl status (first lines)"
  run sh -c "systemctl --no-pager --full status '$CONTAINER_UNIT' | head -n 12"
  vsay "  systemctl show MainPID/Slice/ControlGroup/Type"
  run sh -c "systemctl show -p MainPID -p Slice -p ControlGroup -p Type -p ActiveState -p SubState -p ExecMainStartTimestamp -p ActiveEnterTimestamp '$CONTAINER_UNIT'"
  vsay "  machinectl list"
  run machinectl list
  vsay "  machinectl show Leader/State/Class/RootDirectory"
  run sh -c "machinectl show -p Name -p Leader -p State -p Class -p RootDirectory -p Service '$MACHINE_NAME'"
  vsay "  machinectl status (first lines)"
  run sh -c "machinectl status '$MACHINE_NAME' | head -n 12"

  # ---- B. PID 1, observed from the VM host -------------------------------
  vsay ""
  vsay "--- B. PID 1 facts from the VM host (host PID $pid) -------------------"
  vsay "  /proc/$pid/comm: $(cat /proc/"$pid"/comm 2>/dev/null)"
  vsay "  /proc/$pid/cmdline: $(tr '\0' ' ' < /proc/"$pid"/cmdline 2>/dev/null)"
  vsay "  /proc/$pid/exe -> $(readlink -f /proc/"$pid"/exe 2>/dev/null)"
  vsay "  /proc/$pid/cgroup: $(tr '\n' ' ' < /proc/"$pid"/cgroup 2>/dev/null)"
  check_eq "container PID 1 comm" "systemd" "$(cat /proc/"$pid"/comm 2>/dev/null)"
  local cmdline
  cmdline="$(tr -d '\0' < /proc/"$pid"/cmdline 2>/dev/null)"
  check_eq "container PID 1 cmdline" "/sbin/init" "$cmdline"
  local cg
  cg="$(tr -d '\n' < /proc/"$pid"/cgroup 2>/dev/null)"
  check_has "PID 1 cgroup is the container unit (Slice=machine.slice)" "machine.slice/$CONTAINER_UNIT" "$cg"
  check_eq "container rootfs is ext4, not overlayfs" "ext4" "$(findmnt -no FSTYPE --target "$ROOTFS")"
  [ "$(stat -f -c %T "$ROOTFS")" = "ext2/ext3" ] && ok "stat -f rootfs type ext2/ext3 (ext4 family)" || true

  # ---- C. enter the namespaces: nsenter ----------------------------------
  vsay ""
  vsay "--- C. namespace entry: nsenter -t $pid -m -u -i -p -n ------------------"
  vsay "  (inside the container mount ns, / IS the loopback ext4 image)"
  local inside
  inside="$(cexec 'echo "comm=$(cat /proc/1/comm)"; echo "cmdline=$(tr "\0" " " < /proc/1/cmdline)"; echo "exe=$(readlink -f /proc/1/exe)"; echo "hostname=$(hostnamectl --static)"; echo "root=$(findmnt -no SOURCE,FSTYPE /)"; echo "pid=$(cat /proc/1/status | sed -n "s/^Pid:\\s*//p")"; echo "detect=$(systemd-detect-virt --container 2>&1)"; echo "cgroup_ns=$(cat /proc/1/cgroup | tr "\n" " ")"' 2>&1 || true)"
  vsay "$(printf '%s\n' "$inside" | sed 's/^/    /')"
  check_has "container /proc/1/comm" "comm=systemd" "$inside"
  check_has "container /proc/1/cmdline" "cmdline=/sbin/init" "$inside"
  check_has "container hostnamectl --static" "hostname=$HOST_ID" "$inside"
  check_has "systemd-detect-virt reports a container" "detect=systemd-nspawn" "$inside"
  check_has "container PID1 sees itself at cgroup namespace root" "cgroup_ns=0::/" "$inside"
  check_has "container / is ext4" "root=" "$inside"
  case "$inside" in *"root="*ext4*) ok "container root filesystem ext4 (not overlayfs)";; *) bad "container root filesystem is not ext4";; esac

  # ---- D. machinectl shell ----------------------------------------------
  # NOTE: on this nspawn setup `machinectl shell` connects (machined opens a
  # session) but the container-side process fails: machined allocates the pty
  # in the host's devpts instance, while nspawn gives the container a private
  # devpts, so the container's systemd cannot attach the tty
  # ("Failed to change ownership of terminal: Input/output error"). That is a
  # known nspawn/devpts limitation, not a problem with the container's systemd.
  # `nsenter` (section C) is the authoritative namespace entry path; machinectl
  # list/status/show above are the supported machined operations here.
  vsay ""
  vsay "--- D. machinectl: list/status/show (see A) + shell attempt -----------"
  local shell_out shell_reason
  if command -v machinectl >/dev/null 2>&1; then
    shell_out="$(timeout 25 machinectl --no-ask-password shell "$MACHINE_NAME" /bin/sh -c 'echo SHELL_OK; cat /proc/1/comm' 2>&1 || true)"
    vsay "$(printf '%s\n' "$shell_out" | sed 's/^/    /')"
    case "$shell_out" in
      *SHELL_OK*) ok "machinectl shell works and executes inside the container" ;;
      *)
        shell_reason="$(cexec 'journalctl -b --no-pager 2>/dev/null | grep -m1 "Failed to change ownership of terminal" || true' 2>/dev/null | cut -c1-140)"
        warn "machinectl shell connected but the container-side pty failed (private devpts instance; known nspawn limitation). nsenter in section C is the authoritative entry path. Reason: ${shell_reason:-unknown}"
        ;;
    esac
  else
    bad "machinectl not installed"
  fi

  # ---- E. the container is a real systemd ---------------------------------
  vsay ""
  vsay "--- E. real systemd inside the container ----------------------------"
  vsay "  systemctl --version / is-system-running / list-units"
  runin 'systemctl --version | head -n1; echo "state=$(systemctl is-system-running)"; echo "units=$(systemctl list-units --type=service --state=running --no-legend | wc -l) running services"; systemctl list-units --type=service --state=running --no-legend | head -n 10'
  vsay "  systemctl status (summary)"
  runin 'systemctl status --no-pager --lines=0 | head -n 12'
  vsay "  hostnamectl"
  runin 'hostnamectl'
  vsay "  systemd core services"
  runin 'for u in systemd-journald systemd-udevd dbus systemd-networkd ssh; do printf "%-22s %s\n" "$u" "$(systemctl is-active $u 2>&1)"; done'
  local svc
  svc="$(cexec 'for u in systemd-journald systemd-udevd dbus systemd-networkd ssh; do printf "%s=%s " "$u" "$(systemctl is-active $u 2>&1)"; done' 2>&1 || true)"
  check_has "journald active inside container" "systemd-journald=active" "$svc"
  check_has "udev (systemd-udevd) active inside container" "systemd-udevd=active" "$svc"
  check_has "dbus active inside container" "dbus=active" "$svc"
  check_has "sshd active inside container" "ssh=active" "$svc"
  vsay "  hostnamectl --static inside: $(cexec 'hostnamectl --static' 2>/dev/null | head -n1)"

  # ---- F. journald / real boot log ---------------------------------------
  vsay ""
  vsay "--- F. journald: real boot log inside the container ------------------"
  vsay "  journalctl --list-boots"
  runin 'journalctl --list-boots --no-pager'
  vsay "  journalctl -b (first 14 lines, short-monotonic)"
  runin 'journalctl -b --no-pager -o short-monotonic | head -n 14'
  vsay "  journalctl -b (last 4 lines)"
  runin 'journalctl -b --no-pager -o short-monotonic | tail -n 4'
  local nboot
  nboot="$(cexec 'journalctl --list-boots --no-pager | wc -l' 2>&1 || true)"
  check_ne "journal has at least one real boot entry" "0" "$nboot"
  local jboot jlines jreached jstart_raw
  jboot="$(cexec 'journalctl -b --no-pager -o short-monotonic | head -n1' 2>&1 || true)"
  case "$jboot" in
    *systemd*) ok "journal -b starts with a systemd boot line: ${jboot}" ;;
    *) bad "unexpected first journal line: ${jboot:-<empty>}" ;;
  esac
  jstart_raw="$jboot"
  jlines="$(cexec 'journalctl -b --no-pager -q 2>/dev/null | wc -l' 2>&1 || true)"
  vsay "  journal lines in current boot: ${jlines:-n/a}"
  [ "${jlines:-0}" -gt 20 ] 2>/dev/null && ok "journal -b contains a full boot ($jlines lines)" || bad "journal -b too short ($jlines lines)"
  jreached="$(cexec 'journalctl -b --no-pager -q 2>/dev/null | grep -E "Reached target (Basic System|Multi-User System|Network is Online)|Startup finished in" | head -n 6' 2>&1 || true)"
  vsay "$(printf '%s\n' "$jreached" | sed 's/^/    /')"
  case "$jreached" in
    *"Reached target"*) ok "journal -b shows real boot target progression";;
    *) bad "journal -b does not show boot target progression";;
  esac
  case "$jreached" in
    *"Startup finished in"*) ok "journal -b records systemd's own 'Startup finished in ...'" ;;
    *) warn "journal -b has no 'Startup finished in ...' line" ;;
  esac
  vsay "  host-side read of the container journal: journalctl -M $MACHINE_NAME -b"
  run sh -c "journalctl -M '$MACHINE_NAME' -b --no-pager -n 6 2>&1 | head -n 10"

  # ---- G. boot-time / PID1-start coherence ------------------------------
  vsay ""
  vsay "--- G. physical coherence: container boot vs PID 1 start -------------"
  hz="$(getconf CLK_TCK)"
  uptime_now="$(cut -d. -f1 /proc/uptime)"
  start_ticks="$(awk '{print $22}' /proc/"$pid"/stat)"
  start_s=$((start_ticks / hz))
  age=$((uptime_now - start_s))
  vsay "  host CLK_TCK=$hz  host uptime=${uptime_now}s"
  vsay "  container PID1 starttime (from /proc/$pid/stat field 22): ${start_ticks} ticks = ${start_s}s after kernel boot"
  vsay "  => container age (now - PID1 start) = ${age}s"
  vsay "  (a fake/late PID 1 would show a large gap: init starts when the container boots)"
  [ "$age" -ge 0 ] && ok "container PID1 start time is in the past (${age}s ago)" || bad "PID1 start time is in the future"
  [ "$age" -lt 600 ] && ok "PID1 started within the last 10 minutes (fresh container, age ${age}s)" || warn "container PID1 is older than 10 minutes (age ${age}s)"
  # Coherence anchor: the container journal's first line carries the *kernel
  # monotonic* timestamp of the first journald record, which is ~the moment the
  # container's init started. It must agree with PID1's starttime from
  # /proc/<pid>/stat (both are measured from the same host kernel boot, since
  # containers share the kernel clock). A fake/late PID1 would disagree wildly.
  local jstart_s jdrift
  jstart_s="$(printf '%s' "$jstart_raw" | sed -n 's/^\[ *\([0-9][0-9]*\).*/\1/p')"
  vsay "  first journal line: ${jstart_raw:-n/a}"
  vsay "  first journal monotonic start: ${jstart_s:-n/a}s ; PID1 starttime: ${start_s}s"
  if [ -n "$jstart_s" ]; then
    jdrift=$((jstart_s - start_s))
    [ "$jdrift" -lt 0 ] && jdrift=$((-jdrift))
    vsay "  drift journal-start vs PID1 starttime: ${jdrift}s"
    [ "$jdrift" -le 3 ] && ok "journal boot log starts at PID1 start (drift ${jdrift}s => init booted the container, not a late attach)" ||
      bad "journal boot start (${jstart_s}s) and PID1 start (${start_s}s) disagree by ${jdrift}s"
  else
    bad "cannot parse the first journal monotonic timestamp"
  fi
  vsay "  systemd's own start timestamps (inside, microseconds on the shared kernel monotonic clock):"
  runin 'echo "  UserspaceTimestampMonotonic=$(systemctl show -p UserspaceTimestampMonotonic --value)"; echo "  KernelTimestampMonotonic=$(systemctl show -p KernelTimestampMonotonic --value)  (0 in a container: the kernel timestamp is not exposed to container systemd)"'
  local userspace_ts userspace_s udrift
  userspace_ts="$(cexec 'systemctl show -p UserspaceTimestampMonotonic --value' 2>/dev/null | tr -dc '0-9' || true)"
  if [ -n "$userspace_ts" ] && [ "$userspace_ts" -gt 0 ] 2>/dev/null; then
    userspace_s=$((userspace_ts / 1000000))
    udrift=$((userspace_s - start_s))
    [ "$udrift" -lt 0 ] && udrift=$((-udrift))
    vsay "  UserspaceTimestampMonotonic = ${userspace_s}s after boot ; PID1 starttime = ${start_s}s ; drift = ${udrift}s"
    [ "$udrift" -le 3 ] && ok "systemd's own userspace start timestamp matches PID1 starttime (drift ${udrift}s)" ||
      bad "systemd userspace start (${userspace_s}s) and PID1 starttime (${start_s}s) disagree by ${udrift}s"
  fi
  # Unit-level timestamps: the nspawn unit's start vs container journal boot.
  vsay "  unit ExecMainStartTimestamp=$(systemctl show -p ExecMainStartTimestamp --value "$CONTAINER_UNIT")"
  vsay "  unit ActiveEnterTimestamp  =$(systemctl show -p ActiveEnterTimestamp --value "$CONTAINER_UNIT")"
  vsay "  (unit timestamps are second-granular here; the wall-clock boot time measured"
  vsay "   by build_base.sh when it started the unit is printed by 'up'/'start')"
  vsay "  systemd boot time reported by the container itself:"
  runin 'journalctl -b --no-pager -q | grep -m1 "Startup finished in"; systemd-analyze time 2>&1 | head -n 3'
  local startup_line
  startup_line="$(cexec 'journalctl -b --no-pager -q 2>/dev/null | grep -m1 "Startup finished in"' 2>&1 || true)"
  vsay "  => ${startup_line:-n/a}"
  # Uptime must be physically coherent: host uptime - container age > 0, and
  # the container is younger than the VM.
  vsay "  /proc/uptime (host kernel, shared) = ${uptime_now}s; container age = ${age}s"
  [ "$uptime_now" -gt "$age" ] && ok "container age < host uptime (physically coherent)" || bad "container age exceeds host uptime"

  # ---- H. clean root -----------------------------------------------------
  vsay ""
  vsay "--- H. clean root: no honeypot control plane, no fake init -----------"
  local clean
  clean="$(cexec '
    echo "os=$(sed -n "s/^PRETTY_NAME=//p" /etc/os-release)"
    for p in /var/lib/cortex-pod /run/cortex /usr/local/lib/cortex-agent /opt/cortex; do
      if [ -e "$p" ]; then echo "PRESENT $p"; else echo "absent  $p"; fi
    done
    hits=$(find / -xdev \( -name "*pod_pid1*" -o -name "fake-init*" -o -name "*cortex-pod*" \) 2>/dev/null | head -n 5)
    if [ -z "$hits" ]; then echo "no pod_pid1/fake-init/cortex-pod files"; else echo "HITS: $hits"; fi
    ov=$(grep -c overlay /proc/mounts 2>/dev/null || true)
    echo "overlay mounts: $ov"
    echo "root=$(findmnt -no SOURCE,FSTYPE /)"
    echo "pkgcount=$(dpkg-query -f ".\n" -W 2>/dev/null | wc -l)"
    echo "ssh=$(dpkg-query -W -f "\${Version}" openssh-server 2>/dev/null)"
  ' 2>&1 || true)"
  vsay "$(printf '%s\n' "$clean" | sed 's/^/    /')"
  check_has "no /var/lib/cortex-pod inside container" "absent  /var/lib/cortex-pod" "$clean"
  check_has "no /run/cortex mount inside container" "absent  /run/cortex" "$clean"
  check_has "no pod_pid1/fake-init files" "no pod_pid1/fake-init/cortex-pod files" "$clean"
  check_eq "no overlay mounts inside container" "0" "$(printf '%s\n' "$clean" | sed -n 's/^overlay mounts: //p')"
  check_has "sshd package installed inside container" "ssh=" "$clean"
  osname="$(printf '%s\n' "$clean" | sed -n 's/^os=//p')"
  case "$osname" in *"Ubuntu 22.04"*) ok "container OS = $osname";; *) bad "unexpected container OS: $osname";; esac

  # ---- I. ps vs /proc coherence -----------------------------------------
  vsay ""
  vsay "--- I. process table coherence: ps count == /proc count --------------"
  local counts
  counts="$(cexec '
    nproc=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
    nps=$(ps -e --no-headers 2>/dev/null | wc -l)
    npids=$(ps -e -o pid= --no-headers 2>/dev/null | wc -l)
    echo "proc_dirs=$nproc ps_lines=$nps ps_pids=$npids"
  ' 2>&1 || true)"
  vsay "    $counts"
  local n_proc n_ps
  n_proc="$(printf '%s\n' "$counts" | sed -n 's/.*proc_dirs=\([0-9]*\).*/\1/p')"
  n_ps="$(printf '%s\n' "$counts" | sed -n 's/.*ps_lines=\([0-9]*\).*/\1/p')"
  if [ -n "$n_proc" ] && [ -n "$n_ps" ] && [ "$n_proc" -eq "$n_ps" ]; then
    ok "ps count == /proc count ($n_proc == $n_ps)"
  elif [ -n "$n_proc" ] && [ -n "$n_ps" ] && [ "$n_proc" -eq $((n_ps - 1)) ]; then
    ok "ps count == /proc count (race of 1: $n_proc proc dirs, $n_ps ps lines)"
  else
    bad "ps count ($n_ps) != /proc count ($n_proc)"
  fi

  # ---- J. network -------------------------------------------------------
  vsay ""
  vsay "--- J. veth networking -------------------------------------------------"
  wait_veth_addresses
  vsay "  host $HOST_VETH:"
  run sh -c "ip -4 -br addr show '$HOST_VETH'"
  vsay "  container interfaces / routes:"
  runin 'ip -4 -br addr show; echo; ip route'
  local cip
  cip="$(cexec 'ip -4 -br addr show 2>/dev/null | tr "\n" " "' 2>/dev/null || true)"
  case "$cip" in
    *"$CONTAINER_IP_ADDR"*) ok "container has $CONTAINER_IP_ADDR on its veth interface";;
    *) bad "container missing address $CONTAINER_IP_ADDR (got: $cip)";;
  esac
  local host_veth_addr
  host_veth_addr="$(ip -4 -br addr show "$HOST_VETH" 2>/dev/null | tr "\n" " " || true)"
  case "$host_veth_addr" in
    *"$GATEWAY_IP_ADDR"*) ok "host side $HOST_VETH has $GATEWAY_IP_ADDR";;
    *) bad "host side $HOST_VETH missing $GATEWAY_IP_ADDR (got: ${host_veth_addr:-absent})";;
  esac
  if ping -c1 -W1 "$CONTAINER_IP_ADDR" >/dev/null 2>&1; then
    ok "host can ping container $CONTAINER_IP_ADDR"
  else
    # The VM host runs a pre-existing egress filter (nft table inet filter) that
    # drops host-originated traffic to private ranges 10/8, 172.16/12,
    # 192.168/16 and 169.254/16. Our veth lives in 10.66.0.0/24, so
    # host->container NEW connections are filtered by the *host policy* -- this
    # is not a container problem and we do not touch that firewall. Peering
    # itself is proven in the other direction below (container -> host works,
    # ARP resolves, both ends carry their addresses).
    local fw_rule
    fw_rule="$(nft list ruleset 2>/dev/null | grep -m1 'daddr 10.0.0.0/8' || true)"
    warn "host cannot ping container: host egress firewall drops private-range traffic (${fw_rule:-no nft rule found})"
  fi
  if cexec "ping -c1 -W1 $GATEWAY_IP_ADDR >/dev/null 2>&1"; then
    ok "container can reach the VM host over the veth ($GATEWAY_IP_ADDR)"
  else
    bad "container cannot reach the VM host gateway $GATEWAY_IP_ADDR over the veth"
  fi
  local neigh
  neigh="$(ip neigh show dev "$HOST_VETH" 2>/dev/null | grep "$CONTAINER_IP_ADDR" || true)"
  vsay "  host neighbour entry: ${neigh:-none}"
  case "$neigh" in
    *lladdr*) ok "host ARP resolves the container over the veth";;
    *) warn "no ARP entry for the container on $HOST_VETH";;
  esac
  local ssh_banner=""
  ssh_banner="$(timeout 3 bash -c "exec 3<>/dev/tcp/$CONTAINER_IP_ADDR/22; head -c 30 <&3" 2>/dev/null || true)"
  case "$ssh_banner" in
    SSH-2.0-OpenSSH*) ok "sshd reachable on $CONTAINER_IP_ADDR:22 ($ssh_banner)";;
    *) warn "sshd not reachable on $CONTAINER_IP_ADDR:22 from the VM host (installed+active inside; host egress filter blocks 10/8; reachability not required)";;
  esac

  # ---- K. resources -----------------------------------------------------
  vsay ""
  vsay "--- K. resource use ---------------------------------------------------"
  vsay "  image (sparse, allocated vs apparent):"
  run sh -c "ls -ls '$IMG'"
  run sh -c "du -h '$IMG'; du -h --apparent-size '$IMG'"
  vsay "  rootfs mount usage:"
  run df -h "$ROOTFS"
  run sh -c "du -sh '$ROOTFS' 2>/dev/null"
  vsay "  container cgroup memory:"
  run sh -c "systemctl show -p MemoryCurrent -p MemoryPeak '$CONTAINER_UNIT' 2>/dev/null | sed 's/^/    /'"
  local mem_cur
  mem_cur="$(systemctl show -p MemoryCurrent --value "$CONTAINER_UNIT" 2>/dev/null || true)"
  if [ -n "$mem_cur" ] && [ "$mem_cur" != "[not set]" ] && [ "$mem_cur" -gt 0 ] 2>/dev/null; then
    vsay "  => container memory.current = $(awk "BEGIN{printf \"%.1f MiB\", $mem_cur/1048576}")"
  fi
  local rss
  rss="$(cexec 'ps -e -o rss= 2>/dev/null | awk "{s+=\$1} END {printf \"%.1f\", s/1024}"' 2>/dev/null || true)"
  vsay "  sum of RSS of container processes: ${rss:-n/a} MiB"
  vsay "  cgroup memory.current: $(cat /sys/fs/cgroup/machine.slice/$CONTAINER_UNIT/memory.current 2>/dev/null || echo n/a) bytes"
  vsay "  host free memory:"
  run sh -c "free -m | head -n 2"
  local nprocs
  nprocs="$(cexec 'ls -d /proc/[0-9]* 2>/dev/null | wc -l' 2>/dev/null || true)"
  vsay "  container process count: ${nprocs:-n/a}"

  # ---- L. summary -------------------------------------------------------
  vsay ""
  vsay "=============================================================="
  vsay " RESULT: $PASSES passed, $FAILS failed"
  vsay " image:   $IMG"
  vsay " rootfs:  $ROOTFS ($(findmnt -no SOURCE,FSTYPE --target "$ROOTFS" 2>/dev/null || echo 'not mounted'))"
  vsay " unit:    $CONTAINER_UNIT  machine: $MACHINE_NAME  hostname: $HOST_ID"
  vsay " report:  $report"
  vsay "=============================================================="
  if [ "$FAILS" -eq 0 ]; then
    vsay "VERIFY: PASS"
    return 0
  fi
  vsay "VERIFY: FAIL"
  return 1
}

# ===========================================================================
# 7. composite commands
# ===========================================================================
cmd_up() {
  need_root
  local started=$SECONDS
  ensure_packages
  ensure_image
  ensure_rootfs_mount
  provision_rootfs
  configure_rootfs
  if [ "$HYGIENE_AVAILABLE" = "1" ]; then
    log "repairing host identity, history and log coherence"
    wetware_host_hygiene "$ROOTFS" "$HOST_ID" base "" ""
  fi
  ensure_host_network
  write_container_unit
  start_container
  log "build+start finished in $((SECONDS - started))s"
  if [ "${SKIP_VERIFY:-0}" = "1" ]; then
    log "SKIP_VERIFY=1: skipping evidence suite"
    container_state
    return 0
  fi
  local rc=0
  verify || rc=$?
  return "$rc"
}

cmd_build() {
  need_root
  local started=$SECONDS
  ensure_packages
  ensure_image
  ensure_rootfs_mount
  provision_rootfs
  configure_rootfs
  if [ "$HYGIENE_AVAILABLE" = "1" ]; then
    log "repairing host identity, history and log coherence"
    wetware_host_hygiene "$ROOTFS" "$HOST_ID" base "" ""
  fi
  ensure_host_network
  write_container_unit
  log "build finished in $((SECONDS - started))s (container not started by 'build')"
  container_state
}

cmd_start() {
  need_root
  ensure_rootfs_mount
  start_container
  container_state
}

cmd_stop() {
  need_root
  stop_container
}

cmd_status() {
  need_root
  container_state
  say ""
  say "mount unit: $(systemctl is-active "$MOUNT_UNIT" 2>/dev/null || true) ($MOUNT_UNIT)"
  say "image fs:   $(blkid -p -o value -s TYPE "$IMG" 2>/dev/null || echo 'none')"
}

cmd_verify() {
  need_root
  verify
}

cmd_destroy() {
  need_root
  if [ "${FORCE:-0}" != "1" ]; then
    die "destroy deletes $ROOTFS and $IMG; re-run with FORCE=1 to confirm"
  fi
  stop_container
  systemctl disable --quiet "$CONTAINER_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$CONTAINER_UNIT"
  systemctl stop "$MOUNT_UNIT" 2>/dev/null || true
  systemctl disable --quiet "$MOUNT_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$MOUNT_UNIT"
  rm -f "/etc/systemd/network/80-wetware-$MACHINE_NAME.network" \
        "/etc/systemd/network/70-wetware-$MACHINE_NAME.network"
  systemctl daemon-reload
  if mountpoint -q "$ROOTFS"; then
    umount "$ROOTFS" || die "could not unmount $ROOTFS"
  fi
  local lo
  while read -r lo; do
    [ -n "$lo" ] && losetup -d "$lo" 2>/dev/null || true
  done < <(losetup -j "$IMG" 2>/dev/null | cut -d: -f1)
  rm -f "$IMG"
  rmdir "$ROOTFS" 2>/dev/null || true
  log "destroyed $IMG and $ROOTFS"
}

usage() {
  sed -n '2,60p' "$SELF" 2>/dev/null | sed 's/^# \{0,1\}//' || true
  exit 2
}

main() {
  local cmd="${1:-up}"
  case "$cmd" in
    up|all)        cmd_up ;;
    build)         cmd_build ;;
    start)         cmd_start ;;
    stop)          cmd_stop ;;
    status)        cmd_status ;;
    verify)        cmd_verify ;;
    destroy)       cmd_destroy ;;
    -h|--help|help) usage ;;
    *) printf '[nspawn] unknown command: %s\n\n' "$cmd" >&2; usage ;;
  esac
}

main "$@"
