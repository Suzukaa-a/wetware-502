#!/usr/bin/env bash
# Reset the story to a clean start on the deployed three-host mesh.
#
# Base line  : rewrite the core container's world files (prod-cortex-04).
# Campaign   : additionally reset the shared ledger and clear the hive-side
#              residue, so the next edge session opens a fresh campaign.
#
# usage: ops/reset-node.sh [--campaign] [--keep-images]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
VM="${VM:-$WETWARE_VM}"
MODE="base"
KEEP=""
for arg in "$@"; do
  case "$arg" in
    --campaign) MODE="campaign" ;;
    --keep-images) KEEP="--keep-images" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  "$VM" "sudo MODE='$MODE' KEEP='$KEEP' bash -s" <<'REMOTE'
set -uo pipefail

leader() { machinectl show -p Leader --value "$1" 2>/dev/null; }

core="$(leader prod-cortex)"
if [ -n "${core:-}" ] && [ "$MODE" = "base" ]; then
  nsenter -t "$core" -m -u -i -p -n -- /bin/sh -c \
    "cd /usr/local/lib/cortex-agent && PYTHONPATH=. WETWARE_STATE_DIR=/var/lib/cortex-agent python3 reset.py $KEEP" \
    && echo "[reset] core world rebuilt"
fi

if [ "$MODE" = "campaign" ]; then
  edge="$(leader edge-gw)"
  if [ -n "${edge:-}" ]; then
    nsenter -t "$edge" -m -u -i -p -n -- /bin/sh -c \
      "PYTHONPATH=/usr/local/lib/cortex-agent WETWARE_STATE_DIR=/var/lib/cortex-agent python3 /usr/local/lib/cortex-agent/campaign.py --reset" \
      && echo "[reset] shared campaign ledger reset"
  fi
  hive="$(leader hive)"
  if [ -n "${hive:-}" ]; then
    nsenter -t "$hive" -m -u -i -p -n -- /bin/sh -c \
      'rm -f /var/lib/vesalius/liberation.done /var/log/vesalius/liberation.log \
             /var/lib/vesalius/kernel/metabolic_override /var/lib/vesalius/state/silicon_mirror.active' \
      && echo "[reset] hive residue cleared"
  fi
  if [ -x /usr/local/lib/cortex-agent/nspawn/install_edge.sh ]; then
    bash /usr/local/lib/cortex-agent/nspawn/install_edge.sh seal >/dev/null 2>&1 \
      && echo "[reset] gate 1 re-sealed"
  fi
fi

echo "[reset] done ($MODE)"
REMOTE

echo "[wetware] reset complete ($MODE)"
