#!/usr/bin/env bash
# Push the current engine payload to the VM and (re)install it into the
# real-systemd container.  Intended to be run from the repo checkout:
#
#   ops/nspawn/deploy_from_workstation.sh [user@host]
#
# The VM only ever receives the engine package plus ops/nspawn, never the
# repository history, tests or .local/.
set -euo pipefail

TARGET=${1:-${WETWARE_VM:?set WETWARE_VM=user@host or pass it as $1}}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STAGE=/tmp/wetware-deploy.tar.gz
REMOTE_STAGE=/root/wetware-deploy

if ! ssh -o StrictHostKeyChecking=no "$TARGET" "command -v machinectl >/dev/null && command -v systemd-nspawn >/dev/null"; then
  echo "[deploy] ERROR: $TARGET is missing systemd-container; finish bootstrap-node.sh first." >&2
  exit 1
fi

# The four diagnostics PNGs are generated, not tracked; build them here so a
# clean VM does not end up with an empty /var/www/html/diagnostics.
if [ -x "$REPO/.venv/bin/python" ]; then
  "$REPO/.venv/bin/python" "$REPO/wetware-honeypot/gen_assets.py" \
    "$REPO/wetware-honeypot/assets/diagnostics" >/dev/null 2>&1 || \
    echo "[deploy] WARNING: diagnostics asset generation failed" >&2
fi

echo "[deploy] packing engine from $REPO"
tar -czf "$STAGE" -C "$REPO" \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.local' \
  --exclude 'wetware-honeypot/tests' --exclude 'wetware-honeypot/assets/.cache' \
  wetware-honeypot ops/nspawn \
  $(cd "$REPO" && ls ops/*.py ops/*.sh 2>/dev/null)

echo "[deploy] uploading to $TARGET"
ssh -o StrictHostKeyChecking=no "$TARGET" "sudo rm -rf $REMOTE_STAGE && sudo mkdir -p $REMOTE_STAGE"
cat "$STAGE" | ssh -o StrictHostKeyChecking=no "$TARGET" "sudo tar -xzf - -C $REMOTE_STAGE"

echo "[deploy] checking the base container"
if ! ssh -o StrictHostKeyChecking=no "$TARGET" "sudo test -d /var/lib/wetware-hosts/prod-cortex-04/etc"; then
  echo "[deploy] base rootfs missing; provisioning it with build_base.sh up"
  ssh -o StrictHostKeyChecking=no "$TARGET" \
    "sudo bash -c 'cd $REMOTE_STAGE && bash ops/nspawn/build_base.sh up'"
fi
ssh -o StrictHostKeyChecking=no "$TARGET" \
  "sudo systemctl start wetware-host-prod-cortex-04.service 2>/dev/null || true"

echo "[deploy] installing into the container"
ssh -o StrictHostKeyChecking=no "$TARGET" \
  "sudo bash -c 'cd $REMOTE_STAGE && ROOT=/var/lib/wetware-hosts/prod-cortex-04 \
     MACHINE=prod-cortex PAYLOAD=$REMOTE_STAGE/wetware-honeypot \
     bash ops/nspawn/install_engine_base.sh'"

echo "[deploy] installing nspawn scripts on the VM host"
ssh -o StrictHostKeyChecking=no "$TARGET" \
  "sudo install -d -m 0755 /usr/local/lib/cortex-agent/nspawn && \
   sudo bash -c 'cp -f $REMOTE_STAGE/ops/nspawn/*.sh $REMOTE_STAGE/ops/nspawn/*.c \
     $REMOTE_STAGE/ops/nspawn/*.md /usr/local/lib/cortex-agent/nspawn/ && \
     chmod 0755 /usr/local/lib/cortex-agent/nspawn/*.sh'"

echo "[deploy] clearing transient probe units"
ssh -o StrictHostKeyChecking=no "$TARGET" \
  'L=$(sudo machinectl show -p Leader --value prod-cortex); sudo nsenter -t "$L" -m -u -i -p -n -- systemctl reset-failed'

rm -f "$STAGE"
echo "[deploy] done"
