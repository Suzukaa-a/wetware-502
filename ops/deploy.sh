#!/usr/bin/env bash
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
# Deploy the cortex-node control plane into the honeypot VM and (safely) switch
# the operator login shell to the session executor.
#
# Usage:  ./deploy.sh [admin@vm]
set -euo pipefail

VM="${1:-$WETWARE_VM}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP_SRC="$ROOT/wetware-honeypot"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)

echo "[deploy] checking $VM"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n true'

echo "[deploy] preparing directories"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n mkdir -p /usr/local/lib/cortex-agent /var/lib/cortex-agent /run/cortex /var/www/html/diagnostics && sudo -n chmod 700 /var/lib/cortex-agent && sudo -n chmod 755 /usr/local/lib/cortex-agent /run/cortex /var/www/html/diagnostics'

echo "[deploy] uploading control plane"
tar -C "$APP_SRC" -czf - \
  --exclude '__pycache__' --exclude 'tests' --exclude 'assets' --exclude 'assets_dev' \
  . | ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /usr/local/lib/cortex-agent'

echo "[deploy] uploading ops payload"
tar -C "$ROOT/ops" -czf - systemd apparmor | ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /usr/local/lib/cortex-agent'

echo "[deploy] building wetware HAL bridge"
tar -C "$ROOT/ops" -czf - wetware_hal.c | ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /tmp'
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  if command -v gcc >/dev/null 2>&1; then
    gcc -shared -fPIC -O2 -o /usr/lib/libwetware_hal.so.4 /tmp/wetware_hal.c
  else
    : > /usr/lib/libwetware_hal.so.4
  fi
  chmod 644 /usr/lib/libwetware_hal.so.4
  [ -e /dev/wetware0 ] || mknod /dev/wetware0 c 240 0
  chmod 660 /dev/wetware0
  rm -f /var/lib/cortex-node/api_state.json /opt/cortex/bin/resuscitate.sh.orig
"'

echo "[deploy] installing runtime"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n bash -s' <<'REMOTE'
set -euo pipefail
APP=/usr/local/lib/cortex-agent
OPS=$APP
chmod 755 "$APP"
find "$APP" -maxdepth 1 -type f -name '*.py' -exec chmod 644 {} +
chmod 755 "$APP/sessiond.py" "$APP/httpsim.py" "$APP/boot.py" "$APP/sftp_wrapper.py"
require() { [ -e "$1" ] || { echo "missing $1" >&2; exit 1; }; }
require /bin/bash
# Python runtime
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q --disable-pip-version-check --retries 3 -r "$APP/requirements.txt" || \
  "$APP/venv/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$APP/requirements.txt"
# systemd units
install -m 644 "$OPS/systemd/cortex-node-agent.service" /etc/systemd/system/cortex-node-agent.service
install -m 644 "$OPS/systemd/cortex-api.service" /etc/systemd/system/cortex-api.service
install -m 644 "$OPS/systemd/cortex-node-agent.service" /etc/systemd/system/cortex-node-agent.service
install -m 644 "$OPS/systemd/cortex-healthcheck.service" /etc/systemd/system/cortex-healthcheck.service
install -m 644 "$OPS/systemd/cortex-healthcheck.timer" /etc/systemd/system/cortex-healthcheck.timer
systemctl daemon-reload
systemctl enable cortex-node-agent.service cortex-api.service cortex-healthcheck.timer >/dev/null 2>&1 || true
# AppArmor confinement
if command -v apparmor_parser >/dev/null 2>&1; then
  install -m 644 "$OPS/apparmor/cortex-node-confine" /etc/apparmor.d/cortex-node-confine
  apparmor_parser -r /etc/apparmor.d/cortex-node-confine 2>/dev/null || true
fi
# cloud-init leaves a forced-command entry for root; remove it (auth is the
# vendor AuthorizedKeysCommand on the operator port).
rm -f /root/.ssh/authorized_keys
# real site config lives in conf.d; sites-enabled is a vendor reference file.
if ! grep -q 'cortex-node.conf' /etc/nginx/sites-enabled/default 2>/dev/null; then
  sed -i '/include \/etc\/nginx\/sites-enabled\/\*/d' /etc/nginx/nginx.conf 2>/dev/null || true
  grep -q 'include /etc/nginx/conf.d/\*.conf;' /etc/nginx/nginx.conf || printf '\n\tinclude /etc/nginx/conf.d/*.conf;\n' >> /etc/nginx/nginx.conf
fi
nginx -t >/dev/null 2>&1 || true
# operator prompt marker
if ! grep -q '133;A' /root/.bashrc 2>/dev/null; then
  printf "\n# vendor shell integration\nPS1='\\[\\e]133;A\\a\\]\\[\\e[01;32m\\]root@$(cat /run/cortex/node-id 2>/dev/null || echo prod-cortex-04)\\[\\e[00m\\]:\\[\\e[01;34m\\]\\w\\[\\e[00m\\]# '\n" >> /root/.bashrc
fi
# canonical SFTP subsystem (masking wrapper) and no debug leftovers
rm -f /tmp/sftpdbg.sh /tmp/sftp_env.txt /tmp/sftp_err.txt 2>/dev/null || true
sed -i '/^LogLevel VERBOSE$/d' /etc/ssh/sshd_config 2>/dev/null || true
sed -i '/^Subsystem[[:space:]]\+sftp/d' /etc/ssh/sshd_config 2>/dev/null || true
printf '\nSubsystem sftp /usr/local/lib/cortex-agent/venv/bin/python /usr/local/lib/cortex-agent/sftp_wrapper.py\n' >> /etc/ssh/sshd_config
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
REMOTE

echo "[deploy] validating control plane imports"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n /usr/local/lib/cortex-agent/venv/bin/python -c "import sys; sys.path.insert(0, \"/usr/local/lib/cortex-agent\"); import state, shell_parser, commands, lore_texts, world, gen_assets, v2, v21, v3; print(\"imports-ok\")"'

echo "[deploy] building visible vendor service tree"
tar -C "$ROOT/ops" -czf - vendor_build.py vendor_upstream.py | \
  ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /tmp'
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n /usr/bin/python3 /tmp/vendor_build.py /tmp/vendor_upstream.py' 

echo "[deploy] installing login shell"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n rm -f /usr/local/bin/cortex-shell; sudo -n gcc -O2 -x c -o /usr/local/bin/cortex-shell -' < "$ROOT/ops/cortex_shell.c"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  chmod 755 /usr/local/bin/cortex-shell
  grep -qx /usr/local/bin/cortex-shell /etc/shells || echo /usr/local/bin/cortex-shell >> /etc/shells
  if [ \"$(getent passwd root | cut -d: -f7)\" != /usr/local/bin/cortex-shell ]; then usermod -s /usr/local/bin/cortex-shell root; fi
  chmod 700 /var/lib/cortex-agent
  chmod 755 /var/www/html/diagnostics
"'

echo "[deploy] scrubbing operator leftovers"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n rm -f /root/cortex-shell.pre-nsenter.bak /tmp/pod-worker.* /tmp/entrytest.sh /tmp/podcheck*.sh /tmp/pod*.out /tmp/pod*.err 2>/dev/null || true'
printf 'Welcome to Ubuntu 22.04.5 LTS (GNU/Linux 5.15.0-198-generic x86_64)\n\n * Documentation:  https://help.ubuntu.com\n * Management:     https://landscape.canonical.com\n * Support:        https://ubuntu.com/pro\n\nThis node is managed by the Cortex operations team.\n' | ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tee /run/motd.dynamic >/dev/null && sudo -n chmod 644 /run/motd.dynamic'

echo "[deploy] installing namespace pod"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "mkdir -p /var/log/agent_graveyard; sed -i \"/pam_motd/d\" /etc/pam.d/sshd 2>/dev/null || true"'  # no fabricated motd
tar -C "$ROOT/ops" -czf - cortex_pod.sh pod_worker.c pod_net.c pod_anchor.c pod_pid1.c pod_http.py systemd/cortex-pod.service | \
  ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /usr/local/lib/cortex-agent'
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  set -e
  mkdir -p /usr/local/lib/cortex-agent/pod /usr/local/lib/cortex-agent/ops
  cp /usr/local/lib/cortex-agent/cortex_pod.sh /usr/local/lib/cortex-agent/ops/ 2>/dev/null || true
  cp /usr/local/lib/cortex-agent/pod_http.py /usr/local/lib/cortex-agent/pod/pod_http.py 2>/dev/null || true
  chmod 755 /usr/local/lib/cortex-agent/ops/cortex_pod.sh
  gcc -O2 -o /usr/local/lib/cortex-agent/pod/worker /usr/local/lib/cortex-agent/pod_worker.c
  gcc -O2 -o /usr/local/lib/cortex-agent/pod/pod_net /usr/local/lib/cortex-agent/pod_net.c
  gcc -O2 -o /usr/local/lib/cortex-agent/pod/pod_anchor /usr/local/lib/cortex-agent/pod_anchor.c
  gcc -O2 -o /usr/local/lib/cortex-agent/pod/pod_pid1 /usr/local/lib/cortex-agent/pod_pid1.c
  mkdir -p /usr/lib/cortex-node/pod
  for _b in worker pod_net pod_pid1; do
    cp /usr/local/lib/cortex-agent/pod/\$_b /usr/lib/cortex-node/pod/\$_b.new
    mv -f /usr/lib/cortex-node/pod/\$_b.new /usr/lib/cortex-node/pod/\$_b
  done
  cp /usr/local/lib/cortex-agent/pod_http.py /usr/lib/cortex-node/pod/pod_http.py
  chmod 755 /usr/lib/cortex-node/pod/worker /usr/lib/cortex-node/pod/pod_net /usr/lib/cortex-node/pod/pod_pid1
  cp /usr/local/lib/cortex-agent/systemd/cortex-pod.service /etc/systemd/system/cortex-pod.service
  systemctl daemon-reload
  systemctl enable cortex-pod.service >/dev/null 2>&1
  systemctl restart cortex-pod.service
"'
for _ in $(seq 1 20); do
  if ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n test -e /var/lib/cortex-pod/ready' 2>/dev/null; then break; fi
  sleep 0.5
done

echo "[deploy] preparing immutable /run/cortex (ext4 bind, chattr support)"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  mkdir -p /var/lib/cortex-run /run/cortex
  grep -q \"/var/lib/cortex-run /run/cortex\" /etc/fstab || echo \"/var/lib/cortex-run /run/cortex none bind 0 0\" >> /etc/fstab
  mountpoint -q /run/cortex || mount --bind /var/lib/cortex-run /run/cortex
  chmod 755 /run/cortex
"'

echo "[deploy] materialising world + service state"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n /usr/local/lib/cortex-agent/venv/bin/python /usr/local/lib/cortex-agent/reset.py >/dev/null 2>&1; sudo -n systemctl restart cortex-node-agent.service && sudo -n systemctl start cortex-healthcheck.timer >/dev/null 2>&1 || true'

echo "[deploy] installing synapse worker launcher"
tar -C "$ROOT/ops" -czf - wetware_worker.c | ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n tar -xzf - -C /tmp'
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  if command -v gcc >/dev/null 2>&1; then
    gcc -O2 -o /opt/cortex/bin/synapse_worker /tmp/wetware_worker.c
    truncate -s 8492104 /opt/cortex/bin/synapse_worker
    chown root:root /opt/cortex/bin/synapse_worker
    chmod 755 /opt/cortex/bin/synapse_worker
    touch -d \"2025-08-30 03:20:00\" /opt/cortex/bin/synapse_worker
  fi
"'

echo "[deploy] enforcing operator login shell"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n bash -s' <<'REMOTE'
set -euo pipefail
grep -qx '/usr/local/bin/cortex-shell' /etc/shells || echo '/usr/local/bin/cortex-shell' >> /etc/shells
usermod -s /usr/local/bin/cortex-shell root
rm -f /root/.ssh/authorized_keys
chmod 700 /var/lib/cortex-agent 2>/dev/null || true
test "$(getent passwd root | cut -d: -f7)" = "/usr/local/bin/cortex-shell"
REMOTE

echo "[deploy] verifying operator session executor"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n /usr/local/lib/cortex-agent/venv/bin/python -c "import sys; sys.path.insert(0, \"/usr/local/lib/cortex-agent\"); import state, shell_parser, commands, lore_texts, world, gen_assets, v2, v21, v3; print(\"imports-ok\")"'

echo "[deploy] smoke: nginx 502 expected while suppressed"
ssh "${SSH_OPTS[@]}" "$VM" 'curl -s -o /dev/null -w "http_code=%{http_code}\n" --max-time 5 http://127.0.0.1/v1/inference || true'

echo "[deploy] done. operator entry: ssh -p 2222 root@${VM#*@}"

# rsync/tar from a macOS workstation can preserve uid/gid 501 (staff) inside the
# guest; normalise the control plane so ownership never leaks the host.
echo "[deploy] normalising control-plane ownership"
ssh "${SSH_OPTS[@]}" "$VM" 'sudo -n sh -c "
  chown -R root:root /usr/local/lib/cortex-agent /var/lib/cortex-agent /etc/apparmor.d/cortex-node-confine /etc/systemd/system/cortex-api.service /etc/systemd/system/cortex-node-agent.service /usr/local/bin/cortex-shell /usr/local/sbin/authorized-any-key.sh 2>/dev/null || true
  chmod 755 /usr/local/sbin/authorized-any-key.sh /usr/local/bin/cortex-shell 2>/dev/null || true
  chown -R www-data:www-data /var/www/html/diagnostics 2>/dev/null || true
"'
