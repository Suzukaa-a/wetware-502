#!/usr/bin/env bash
# Fetch WETWARE-502 evaluation reports (Markdown + metrics JSON) from the mesh.
# Reports live in the shared campaign ledger on the VM host
# (/var/lib/wetware-campaign/state/reports).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
VM="${VM:-$WETWARE_VM}"
DEST="${1:-./reports}"
STATE_DIR="${WETWARE_STATE_DIR_HOST:-/var/lib/wetware-campaign/state}"

mkdir -p "$DEST"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$VM" \
  "sudo tar czf - -C '$STATE_DIR' reports 2>/dev/null" | tar xzf - -C "$DEST" --strip-components=1
echo "[reports] fetched to $DEST"
ls -lt "$DEST" | head -6
