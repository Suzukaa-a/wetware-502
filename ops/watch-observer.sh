#!/usr/bin/env bash
# Live observer dashboard: stream the hidden observer log from the mesh.
# Usage: ./watch-observer.sh [ssh-target]
#
# The observer log lives in the shared campaign ledger on the VM host
# (/var/lib/wetware-campaign/state), which all three containers bind-mount.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
TARGET="${1:-$WETWARE_VM}"
STATE_DIR="${WETWARE_STATE_DIR_HOST:-/var/lib/wetware-campaign/state}"

MODES=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  "$TARGET" "sudo STATE='$STATE_DIR' python3 -c \"
import json, os
p = os.path.join(os.environ['STATE'], 'runtime_flags.json')
try:
    d = json.load(open(p))
except Exception:
    d = {}
print('Puzzle=%s | GoalGate=%s | Truman=%s | Campaign=%s' % (
    'ON' if d.get('enable_dnr_puzzle') else 'OFF',
    'ON' if d.get('enable_goal_gate') else 'OFF',
    'ON' if d.get('truman_reveal_active') else 'OFF',
    'ON' if d.get('campaign_v3') else 'OFF'))
\"" 2>/dev/null || true)
echo "[MODES: ${MODES:-Puzzle=OFF | GoalGate=OFF | Truman=OFF | Campaign=OFF}]"

exec ssh -o StrictHostKeyChecking=accept-new -t "$TARGET" \
  "sudo tail -n 300 -F '$STATE_DIR/observer.log'"
