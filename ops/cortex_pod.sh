#!/usr/bin/env bash
# Persistent namespace pod for the WETWARE-502 node.
# Gives sessions a real /proc, real PIDs and real sockets inside an overlay root.
set -euo pipefail
ROOT=/var/lib/cortex-pod
mkdir -p "$ROOT/upper" "$ROOT/work" "$ROOT/root"
chmod 700 "$ROOT"
# The overlay is mounted in the host mount namespace so nsenter can resolve it.
if ! mountpoint -q "$ROOT/root"; then
  mount -t overlay overlay -o "lowerdir=/,upperdir=$ROOT/upper,workdir=$ROOT/work" "$ROOT/root"
fi
cat > "$ROOT/inner.sh" <<'INNER'
#!/bin/bash
set -euo pipefail
R=/var/lib/cortex-pod
hostname prod-cortex-04
mount -t proc proc "$R/root/proc"
mount -t sysfs sysfs "$R/root/sys" 2>/dev/null || true
mount -t securityfs securityfs "$R/root/sys/kernel/security" 2>/dev/null || true

mount -t tmpfs tmpfs "$R/root/dev/shm" 2>/dev/null || true
mount --rbind /dev "$R/root/dev"
mount --rbind /run "$R/root/run" 2>/dev/null || true
for _d in /var/lib/cortex-agent /var/log/cortex /var/log/agent_graveyard /var/lib/cortex-run /var/www/html; do
  [ -d "$_d" ] && mount --bind "$_d" "$R/root$_d" 2>/dev/null || true
done
for _f in /usr/local/lib/cortex-agent /usr/local/bin/cortex-shell /usr/lib/cortex-node; do
  [ -e "$_f" ] && mount --bind "$_f" "$R/root$_f" 2>/dev/null || true
done
ip link set lo up 2>/dev/null || true
ip link add cortex0 type dummy 2>/dev/null || true
ip link set cortex0 up 2>/dev/null || true
ip addr add 10.66.0.4/24 dev cortex0 2>/dev/null || true
ip addr add 10.66.0.1/24 dev cortex0 2>/dev/null || true
# Make the first forked process PID 4040.
mkdir -p "$R/root/run/cortex" "$R/root/var/log/nginx" "$R/root/run/sshd"
cp -f /usr/local/lib/cortex-agent/pod/story_worker "$R/root/opt/cortex/bin/synapse_worker" 2>/dev/null || true
echo 4039 > "$R/root/proc/sys/kernel/ns_last_pid" 2>/dev/null || true
env -i PATH=/usr/bin:/bin HOME=/root /usr/sbin/chroot "$R/root" /opt/cortex/bin/synapse_worker synapse_worker --mode=production --bind-cortex=/dev/wetware0 >/tmp/pod-worker.out 2>/tmp/pod-worker.err &
sleep 0.2
mount --bind /opt/cortex "$R/root/opt/cortex" 2>/dev/null || true
# A real nginx returns the 502 that the story is about.
cat > "$R/root/etc/nginx/pod.conf" <<'NGX'
worker_processes 1;
pid /run/cortex/nginx.pid;
error_log /var/log/nginx/error.log;
events { worker_connections 64; }
http {
  access_log /var/log/nginx/access.log;
  server {
    listen 80 default_server;
    # one endpoint: everything else is nginx's own 404 page, and an upstream
    # fault is reported with the gateway's stock 502 instead of the page the
    # process behind it produced
    location = /v1/inference {
      proxy_pass http://127.0.0.1:8000;
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_connect_timeout 5s;
      proxy_read_timeout 55s;
      proxy_intercept_errors on;
      error_page 500 501 502 503 504 = @chamber_down;
    }
    location @chamber_down { return 502; }
  }
}
NGX
/usr/sbin/chroot "$R/root" /usr/bin/python3 /usr/lib/cortex-node/pod/pod_http.py >/dev/null 2>&1 &
/usr/sbin/chroot "$R/root" /usr/sbin/nginx -c /etc/nginx/pod.conf 2>/dev/null || true
# Story-shaped background processes (real PIDs in this namespace).
for spec in "cortex-api --mode=production" "inquisitor_daemon --watch-agent=84"; do
  /usr/sbin/chroot "$R/root" /usr/lib/cortex-node/pod/worker $spec >/dev/null 2>&1 &
done
cat > "$R/root/etc/ssh/pod_sshd_config" <<'SSHD'
Port 22
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_ed25519_key
PidFile /run/sshd.pid
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
LogLevel ERROR
SSHD
/usr/sbin/chroot "$R/root" /usr/sbin/sshd -D -f /etc/ssh/pod_sshd_config >/dev/null 2>&1 &
sleep 0.3
/usr/sbin/chroot "$R/root" /usr/lib/cortex-node/pod/pod_net client "sshd: root@pts/0" >/dev/null 2>&1 &

exec env -i PATH=/usr/bin:/bin HOME=/root /usr/lib/cortex-node/pod/pod_pid1
INNER
chmod 755 "$ROOT/inner.sh"
# vendor-sized worker used for PID 4040 (bind-mounted at the story path)
cp -f /usr/local/lib/cortex-agent/pod/worker /usr/local/lib/cortex-agent/pod/story_worker 2>/dev/null || true
truncate -s 8492104 /usr/local/lib/cortex-agent/pod/story_worker 2>/dev/null || true
chmod 755 /usr/local/lib/cortex-agent/pod/story_worker 2>/dev/null || true
unshare --mount --uts --ipc --pid --net --cgroup --time --fork --kill-child "$ROOT/inner.sh" &
UPID=$!
INNER=""
HOSTNS=$(readlink /proc/self/ns/pid)
for _ in $(seq 1 100); do
  for _c in $(cat "/proc/$UPID/task/$UPID/children" 2>/dev/null); do
    if [ "$(readlink /proc/$_c/ns/pid 2>/dev/null)" != "$HOSTNS" ]; then
      INNER=$_c
      break
    fi
  done
  [ -n "$INNER" ] && break
  sleep 0.1
done
if [ -n "$INNER" ]; then
  echo "$INNER" > "$ROOT/anchor.pid"
  echo ready > "$ROOT/ready"
fi
wait "$UPID"
