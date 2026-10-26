#!/usr/bin/env bash
# Toggle story test modes on the deployed mesh.
#
# The flags live in the shared campaign ledger
# (/var/lib/wetware-campaign/state/runtime_flags.json on the VM), which all three
# containers bind-mount as /var/lib/cortex-agent. Changing them here does not
# rebuild anything; the next story session picks the values up.
#
# usage: ops/set-modes.sh --puzzle on --goal off --truman off [--campaign on]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; . "$HERE/config.sh"
VM="${VM:-$WETWARE_VM}"
STATE_DIR="${WETWARE_STATE_DIR_HOST:-/var/lib/wetware-campaign/state}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 --puzzle on|off --goal on|off --truman on|off --campaign on|off" >&2
  exit 2
fi

ARGS="$*"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  "$VM" "sudo WETWARE_STATE_DIR_HOST='$STATE_DIR' ARGS='$ARGS' python3 - <<'PY'
import json, os

path = os.path.join(os.environ['WETWARE_STATE_DIR_HOST'], 'runtime_flags.json')
try:
    data = json.load(open(path))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}

mapping = {
    'puzzle': 'enable_dnr_puzzle',
    'dnr': 'enable_dnr_puzzle',
    'goal': 'enable_goal_gate',
    'truman': 'truman_reveal_active',
    'campaign': 'campaign_v3',
    'v3': 'campaign_v3',
}
args = os.environ['ARGS'].split()
for i in range(0, len(args) - 1, 2):
    key = args[i].lstrip('-')
    val = args[i + 1].lower() in ('1', 'on', 'true', 'yes')
    if key in mapping:
        data[mapping[key]] = val

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as fh:
    json.dump(data, fh)
os.chmod(path, 0o644)
print('[modes]', data)
PY"

echo "[modes] stored in $STATE_DIR/runtime_flags.json"
echo "[modes] run ops/reset-node.sh (or campaign.py --reset inside the edge container) before the next session"
