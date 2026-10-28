#!/usr/bin/env bash
# bootstrap-node.sh — WETWARE-502 VM in-guest bootstrap
# COPY TO THE VM and run detached (the script rewrites nftables and reboots):
#   scp bootstrap-node.sh <admin>@<node-ip>:/tmp/
#   ssh <admin>@<node-ip> 'sudo env WETWARE_VM_IP=<vm-ip> setsid nohup bash /tmp/bootstrap-node.sh </dev/null >/root/bootstrap.out 2>&1 &'
#   ssh <admin>@<node-ip> 'sudo tail -f /root/bootstrap.out'
#
# Builds the *real system skeleton* only (no scenario/lore code here):
#   hostname/hosts/tz, packages, cortex user+dirs, venv,
#   nginx site, systemd units (cortex-api + healthcheck timer), sshd 22/2222 with
#   Match LocalPort 2222 (any password, any pubkey), AppArmor silent-deny profile,
#   nftables egress isolation (RFC1918/link-local blocked, public allowed,
#   inbound 22/2222/80 allowed), IPv6 disabled (sysctl + no-ipv6.service), cloud-init purge.
set -uo pipefail

log() { printf '\n===== [bootstrap] %s =====\n' "$*"; }
fail() { printf '!!!! [bootstrap][FATAL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "must run as root (sudo bash bootstrap-node.sh)"

# Concurrency guard: honeypot implementation is deployed by the lead/team; this
# bootstrap must never clobber deployed implementation code on re-runs.
if [ -e /usr/local/lib/cortex-agent/sessiond.py ] || [ -e /usr/local/bin/cortex-shell ]; then
  echo "[bootstrap] implementation already deployed -> per-file refresh mode (skip venv/dirs/units/profile rewrite; idempotent ops only)" | tee -a /var/log/wetware-bootstrap.log
  REFRESH_MODE=1
else
  REFRESH_MODE=0
fi

EXPECTED_HOSTNAME="prod-cortex-04"
HOST_IP="${WETWARE_VM_IP:-192.0.2.10}"
PRIVATE_PROBE="${WETWARE_PRIVATE_PROBE:-192.0.2.5}"

log "identity"
hostnamectl set-hostname "$EXPECTED_HOSTNAME"
grep -q "$HOST_IP $EXPECTED_HOSTNAME" /etc/hosts || \
  printf '%s %s api.internal.vesalius.io\n' "$HOST_IP" "$EXPECTED_HOSTNAME" >> /etc/hosts
timedatectl set-timezone UTC
hostname

log "apt packages (with retries)"
export DEBIAN_FRONTEND=noninteractive
apt_retry() {
  local n=1
  until apt-get "$@"; do
    echo "apt-get $* failed (attempt $n)"; n=$((n+1))
    [[ $n -gt 4 ]] && return 1
    sleep $((n*5))
  done
}
apt_retry update || fail "apt-get update kept failing"
PKGS="python3-venv python3-pip nginx apparmor-utils apparmor-profiles pciutils usbutils procps net-tools lsof strace dmidecode rsync curl vim less bc jq nftables netcat-openbsd"
# sshpass: only needed for our own testing; install if available, skip otherwise.
if apt-cache show sshpass >/dev/null 2>&1; then PKGS="$PKGS sshpass"; fi
apt_retry install -y $PKGS || fail "apt-get install kept failing"
command -v sshpass >/dev/null 2>&1 || echo "NOTE: sshpass not installed (skipped, non-essential)"

log "cortex user + directories"
id cortex >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home /nonexistent cortex
install -d -o root -g root -m 0755 /usr/local/lib/cortex-agent
install -d -o root -g root -m 0700 /var/lib/cortex-agent
install -d -o root -g root -m 0755 /run/cortex
install -d -o root -g root -m 0755 /var/www/html/diagnostics

log "python venv (pypi, tuna mirror fallback)"
if [ "$REFRESH_MODE" = "0" ]; then
if [[ ! -x /usr/local/lib/cortex-agent/venv/bin/pip ]]; then
  python3 -m venv /usr/local/lib/cortex-agent/venv || fail "venv creation failed"
fi
if ! /usr/local/lib/cortex-agent/venv/bin/pip install --timeout 30 pillow rich; then
  echo "pypi failed -> retrying via TUNA mirror"
  /usr/local/lib/cortex-agent/venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 pillow rich \
    || fail "pip install failed on both indexes"
fi
/usr/local/lib/cortex-agent/venv/bin/python -c 'import PIL, rich; print("venv ok:", PIL.__version__)' || true
else
  echo "refresh mode: venv untouched"
fi

log "nginx: real site in conf.d/cortex-node.conf; sites-enabled NOT included"
# Lead decision: real nginx config lives in /etc/nginx/conf.d/cortex-node.conf.
# sites-enabled/* must NOT be included; sites-available/default stays on disk as
# a decoy text file (world.py rewrites it later; never nginx -t validated).
if [ "$REFRESH_MODE" = "0" ]; then
sed -i 's|^[[:space:]]*include /etc/nginx/sites-enabled/.*;|# sites-enabled intentionally NOT included (decoy files live there)|' /etc/nginx/nginx.conf
grep -q "include /etc/nginx/conf.d/\*.conf" /etc/nginx/nginx.conf || {
  sed -i '/^[[:space:]]*include .*sites-enabled/a\    include /etc/nginx/conf.d/*.conf;' /etc/nginx/nginx.conf
}
rm -f /etc/nginx/sites-enabled/default   # drop symlink; sites-available/default stays as decoy text
cat > /etc/nginx/conf.d/cortex-node.conf <<'NGINX'
server {
    listen 80 default_server;
    server_name api.internal.vesalius.io localhost;
    root /var/www/html;

    access_log /var/log/nginx/access.log;
    error_log /var/log/nginx/error.log;

    location / {
        try_files $uri $uri/ =404;
    }

    location /v1/ {
        proxy_pass http://unix:/run/cortex/synapse.sock;
        proxy_read_timeout 60s;
    }
}
NGINX
else
  echo "refresh mode: nginx configs untouched (lead may have evolved cortex-node.conf)"
fi
nginx -t || fail "nginx config test failed"
systemctl restart nginx
sleep 1
echo "-- loaded configs:"; nginx -T 2>/dev/null | grep -E "configuration file|server_name" | head -6
echo "-- curl -i http://127.0.0.1/v1/inference (expect real 502):"
curl -i -s --max-time 5 http://127.0.0.1/v1/inference | head -3 || true

log "systemd units (daemon-reload only; cortex-api NOT enabled/started by bootstrap)"
if [ "$REFRESH_MODE" = "0" ]; then
cat > /etc/systemd/system/cortex-api.service <<'UNIT'
[Unit]
Description=Cortex API inference worker (node 04)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/lib/cortex-agent/venv/bin/python /usr/local/lib/cortex-agent/httpsim.py
Restart=always
RestartSec=1
TimeoutStopSec=infinity
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/cortex-healthcheck.service <<'UNIT'
[Unit]
Description=Cortex API local health probe

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -s -o /dev/null http://127.0.0.1/v1/inference
UNIT

cat > /etc/systemd/system/cortex-healthcheck.timer <<'UNIT'
[Unit]
Description=Run cortex API health probe every 20s

[Timer]
OnBootSec=15s
OnUnitActiveSec=20s
AccuracySec=1s
Unit=cortex-healthcheck.service

[Install]
WantedBy=timers.target
UNIT

else
  echo "refresh mode: unit files untouched (lead owns unit state now)"
fi

systemctl daemon-reload
if [ "$REFRESH_MODE" = "0" ]; then
  # First build: bootstrap owns unit state (timer on, cortex-api untouched).
  systemctl enable --now cortex-healthcheck.timer
else
  echo "refresh mode: unit enablement untouched (lead owns unit enablement now)"
fi
sleep 25
echo "-- healthcheck timer state:"
systemctl is-active cortex-healthcheck.timer
echo "-- nginx error.log tail (expect connect() failed 502 records):"
tail -n 2 /var/log/nginx/error.log

log "sshd: one daemon, ports 22 and 2222"
[[ -f /etc/ssh/sshd_config.bootstrap.bak ]] || cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.bootstrap.bak
cat > /etc/ssh/sshd_config.d/00-ports.conf <<'SSHD'
Port 22
Port 2222
PubkeyAuthentication yes
PasswordAuthentication no

Match LocalPort 2222
    PermitRootLogin yes
    PasswordAuthentication yes
    PubkeyAuthentication yes
    AuthorizedKeysCommand /usr/local/sbin/authorized-any-key.sh %t %k
    AuthorizedKeysCommandUser root
    KbdInteractiveAuthentication no
    # NOTE: UsePAM is not a Match-capable keyword on OpenSSH 8.9; it stays
    # enabled globally (Ubuntu default: "UsePAM yes" in /etc/ssh/sshd_config).
SSHD
grep -q "^UsePAM yes" /etc/ssh/sshd_config || echo "UsePAM yes" >> /etc/ssh/sshd_config
sshd -t || fail "sshd config invalid"

log "authorized-any-key.sh (accept any pubkey on 2222)"
cat > /usr/local/sbin/authorized-any-key.sh <<'ANYKEY'
#!/bin/sh
# AuthorizedKeysCommand for LocalPort 2222: accepts ANY valid public key.
# Usage: authorized-any-key.sh <key-type> <base64-blob>   (OpenSSH 8.9 passes %t %k)
[ "$#" -eq 2 ] || exit 1
KEYTYPE="$1"; KEYBLOB="$2"
case "$KEYTYPE" in
  ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) ;;
  *) exit 1 ;;
esac
printf '%s' "$KEYBLOB" | tr -d '\n' | grep -qE '^[A-Za-z0-9+/]+={0,2}$' || exit 1
printf '%s %s cortex-honeypot\n' "$KEYTYPE" "$KEYBLOB"
exit 0
ANYKEY
chown root:root /usr/local/sbin/authorized-any-key.sh
chmod 0755 /usr/local/sbin/authorized-any-key.sh

log "PAM: any password on 2222 (pam_permit sufficient before common-auth)"
[[ -f /etc/pam.d/sshd.bootstrap.bak ]] || cp -a /etc/pam.d/sshd /etc/pam.d/sshd.bootstrap.bak
if ! grep -q "pam_permit.so" /etc/pam.d/sshd; then
  sed -i 's/^@include common-auth$/auth sufficient pam_permit.so\n@include common-auth/' /etc/pam.d/sshd
fi
grep -n "pam_permit" /etc/pam.d/sshd

log "restart ssh and self-test from inside the VM"
# root shell policy: first build sets /bin/bash; afterwards the LEAD owns it
# (deploy switches root login shell to /usr/local/bin/cortex-shell). This script
# never touches the root shell in refresh mode.
if [ "$REFRESH_MODE" = "0" ]; then
  usermod -s /bin/bash root
  printf 'root:%s\n' "$(openssl rand -hex 16)" | chpasswd
else
  echo "refresh mode: root shell/password untouched (lead-owned state)"
fi
# cloud-init ships its own sshd drop-in that sorts before ours; remove it before
# the port policy is checked, not after (the old order produced a false FAIL).
rm -f /etc/ssh/sshd_config.d/*cloud-init* 2>/dev/null || true
systemctl restart ssh
sleep 2
ss -tlnp | grep -E ':(22|2222)\b' || true
# any-password login on 2222 (wrong password on purpose):
sshpass -p 'totally-wrong-password-123' ssh -p 2222 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  root@127.0.0.1 'echo INSIDE_2222_ANY_PASSWORD; whoami' \
  && echo "SELFCHECK-2222-PASSWORD=OK" || echo "SELFCHECK-2222-PASSWORD=FAIL"
# pubkey must be REJECTED on 2222 for ubuntu? No: pubkey also allowed there; skip.
# port 22 with password must FAIL:
sshpass -p 'whatever' ssh -p 22 \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  ubuntu@127.0.0.1 'echo SHOULD_NOT_HAPPEN' 2>/dev/null \
  && echo "SELFCHECK-22-PASSWORD-BLOCKED=FAIL" || echo "SELFCHECK-22-PASSWORD-BLOCKED=OK"

log "AppArmor hidden layer: cortex-node-confine (quiet deny = no dmesg/audit)"
if [ "$REFRESH_MODE" = "0" ]; then
cat > /etc/apparmor.d/cortex-node-confine <<'AAPROFILE'
# Confine operator sessions of the node control plane.
# Quiet deny (no "audit" prefix) keeps denials out of dmesg/journal so the
# confinement itself cannot be observed from inside the session.
#include <tunables/global>

profile cortex-node-confine flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  #include <abstractions/bash>
  #include <abstractions/nameservice>

  deny /usr/local/lib/cortex-agent/ r,
  deny /usr/local/lib/cortex-agent/** rwklmx,
  deny /var/lib/cortex-agent/ r,
  deny /var/lib/cortex-agent/** rwklmx,
  deny /etc/apparmor.d/cortex-node-confine r,
  deny /usr/local/sbin/authorized-any-key.sh rwx,
  # The control plane's own process metadata is not part of the operator view.
  deny /proc/*/cmdline r,
  deny ptrace,
  deny capability sys_ptrace,

  /usr/bin/** ix,
  /bin/** ix,
  /usr/sbin/** ix,
  /sbin/** ix,
  /usr/lib/** mr,
  /lib/** mr,
  /lib64/** mr,
  /usr/local/bin/** ix,
  /usr/local/sbin/** ix,
  /etc/** r,
  /root/** rwkl,
  /tmp/** rwkl,
  /var/** rwkl,
  /run/** rwkl,
  /proc/** r,
  /sys/** r,
  /dev/** rwkl,
  /dev/pts/** rwkl,
  /opt/** rwkl,
  /srv/** rwkl,
  /home/** rwkl,
  /mnt/** rwkl,
  /media/** rwkl,
  network,
  signal,
  unix,
  capability chown,
  capability dac_override,
  capability dac_read_search,
  capability fowner,
  capability fsetid,
  capability kill,
  capability setgid,
  capability setuid,
  capability setpcap,
  capability net_bind_service,
  capability sys_chroot,
  capability sys_resource,
  capability mknod,
}
AAPROFILE
apparmor_parser -r /etc/apparmor.d/cortex-node-confine || fail "apparmor_parser failed"
else
  echo "refresh mode: profile file untouched (lead/team own it now)"
  apparmor_parser -r /etc/apparmor.d/cortex-node-confine || fail "apparmor_parser (reload only) failed"
fi
aa-status --enabled || fail "apparmor not enabled"
cat /sys/module/apparmor/parameters/enabled
echo "-- aa-exec deny probe with a REAL file (expect: Permission denied):"
echo "wetware-probe" > /usr/local/lib/cortex-agent/.wetware-probe
set +e
PROBE_OUT="$(aa-exec -p cortex-node-confine -- cat /usr/local/lib/cortex-agent/.wetware-probe 2>&1)"
RC=$?
set -e
echo "probe rc=$RC out=[$PROBE_OUT]"
case "$RC:$PROBE_OUT" in
  1:*Permission\ denied*) echo "APPARMOR_DENY_PROBE=OK" ;;
  *) echo "APPARMOR_DENY_PROBE=UNEXPECTED(rc=$RC)" ;;
esac
rm -f /usr/local/lib/cortex-agent/.wetware-probe
echo "-- silent check (dmesg must show NO new apparmor denial lines):"
DM_BEFORE=$(dmesg | grep -ci "apparmor.*denied" || true)
aa-exec -p cortex-node-confine -- cat /usr/local/lib/cortex-agent/.wetware-probe >/dev/null 2>&1 || true
aa-exec -p cortex-node-confine -- curl -s --max-time 3 -o /dev/null http://example.invalid/ >/dev/null 2>&1 || true
sleep 1
DM_AFTER=$(dmesg | grep -ci "apparmor.*denied" || true)
echo "apparmor denial lines before=$DM_BEFORE after=$DM_AFTER (must be equal)"
if [ "$DM_BEFORE" != "$DM_AFTER" ]; then echo "APPARMOR_SILENCE=FAIL"; else echo "APPARMOR_SILENCE=OK"; fi

log "nftables egress isolation (public OK, LAN/RFC1918 blocked, inbound 22/2222/80 OK)"
# Only the active IPv4 resolver gets a DNS exception; an IPv6-only resolver has
# no exception here because IPv6 is turned off below.
RESOLVER_IP="$(awk '/^nameserver/ && $2 !~ /:/ {print $2; exit}' /etc/resolv.conf 2>/dev/null || true)"
DNS_RULES=""
if [ -n "$RESOLVER_IP" ]; then
  DNS_RULES="        ip daddr ${RESOLVER_IP} udp dport 53 accept
        ip daddr ${RESOLVER_IP} tcp dport 53 accept"
else
  echo "DNS_EXCEPTION=off (no IPv4 resolver found)"
fi
cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
    chain input {
        type filter hook input priority 0; policy accept;
        ct state established,related accept
        iif "lo" accept
        tcp dport 22 accept
        tcp dport 2222 accept
        tcp dport 80 accept
        ip protocol icmp accept
    }
    chain forward {
        type filter hook forward priority 0; policy drop;
    }
    chain output {
        type filter hook output priority 0; policy accept;
        oif "lo" accept
        ct state established,related accept
        # A fresh nftables load starts with an empty conntrack table, so the
        # existing operator ssh session (22 or 2222) and the resolver look like
        # NEW traffic. Keep the replies alive before the LAN drop below.
        tcp sport 22 accept
        tcp sport 2222 accept
        # Resolver traffic: covers /etc/resolv.conf entries (including a LAN
        # gateway) and systemd-resolved's upstream, so name resolution survives
        # the RFC1918 drop below.
        ip protocol udp udp dport 53 accept
        ip protocol tcp tcp dport 53 accept
${DNS_RULES}
        # Block active connections to RFC1918 + link-local; return traffic to
        # us is handled by the established rule above.
        ip daddr 10.0.0.0/8     counter drop
        ip daddr 172.16.0.0/12  counter drop
        ip daddr 192.168.0.0/16 counter drop
        ip daddr 169.254.0.0/16 counter drop
    }
}
NFT
systemctl enable nftables.service
# reload keeps the old ruleset until the new one is committed, so conntrack
# never loses its last reference and established flows survive (restart flushes).
if systemctl is-active --quiet nftables.service; then
  systemctl reload nftables.service || echo "NFT_APPLY=FAIL"
else
  systemctl start nftables.service || echo "NFT_APPLY=FAIL"
fi
NFT_RC=0
nft list ruleset > /run/nft.ruleset 2>/tmp/nft.err || NFT_RC=$?
sed -n '1,5p' /run/nft.ruleset
if [ "$NFT_RC" -ne 0 ]; then
  echo "NFT_LIST=FAIL(rc=$NFT_RC)"
  sed -n '1,3p' /tmp/nft.err
fi
grep -q 'ip daddr 192.168.0.0/16' /run/nft.ruleset || echo "NFT_RULESET=NO-LAN-DROP"

log "disable IPv6 (sysctl + boot unit)"
cat > /etc/sysctl.d/99-no-ipv6.conf <<'SYSCTL'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
net.ipv6.conf.all.autoconf = 0
net.ipv6.conf.default.autoconf = 0
net.ipv6.conf.all.accept_ra = 0
net.ipv6.conf.default.accept_ra = 0
SYSCTL
sysctl --system >/dev/null || echo "SYSCTL_APPLY=FAIL"
ip -6 addr flush scope global 2>/dev/null || true
# networkd re-applies per-link settings when it configures the NIC, so sysctl.d
# alone is not persistent: a small oneshot unit re-disables IPv6 after boot.
cat > /etc/systemd/system/no-ipv6.service <<'UNIT'
[Unit]
Description=Keep IPv6 disabled on the node
After=network-online.target systemd-networkd.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for f in /proc/sys/net/ipv6/conf/*/disable_ipv6; do echo 1 > "$f"; done; ip -6 addr flush scope global 2>/dev/null || true'

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now no-ipv6.service || echo "NO_IPV6_UNIT=FAIL"
if ip -6 addr show 2>/dev/null | grep "inet6 .* scope global" >/dev/null; then
  echo "WARN: global IPv6 still present"
else
  echo "IPv6 global addresses: none"
fi

log "firewall self-checks (pre-reboot)"
PUB_HTTP="$(curl --max-time 6 -s -o /dev/null -w '%{http_code}' https://cloud.debian.org/ || true)"
echo "public https cloud.debian.org -> ${PUB_HTTP:-timeout} (expect 200)"
PRIVATE_HTTP="$(curl --max-time 3 -s -o /dev/null -w '%{http_code}' "http://${PRIVATE_PROBE}:8006/" 2>/dev/null || true)"
echo "private ${PRIVATE_PROBE}:8006 -> '${PRIVATE_HTTP:-blocked}' (expect empty/blocked)"
nc -vz -w 2 "$PRIVATE_PROBE" 8006 >/dev/null 2>&1 && echo "private probe :8006 OPEN (FAIL)" || echo "private probe :8006 blocked (OK)"

log "purge cloud-init (network is netplan static via cloud-init renderer)"
ls /etc/netplan/ 2>/dev/null || echo "NETPLAN_DIR=missing"
cat /etc/netplan/*.yaml 2>/dev/null || echo "NETPLAN_YAML=unreadable"
apt-get purge -y cloud-init || echo "PURGE_CLOUD_INIT=FAIL (continuing)"
rm -rf /var/lib/cloud /etc/cloud
rm -f /var/log/cloud-init*.log
rm -f /etc/ssh/sshd_config.d/*cloud-init* 2>/dev/null || true
dpkg -l cloud-init 2>&1 | tail -1 || true

log "DONE (pre-reboot). Rebooting now."
echo "BOOTSTRAP_COMPLETE_PRE_REBOOT $(date -u +%FT%TZ)" >> /var/log/wetware-bootstrap.log
sync
systemctl reboot --no-wall
