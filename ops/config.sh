#!/usr/bin/env bash
# Shared environment loader for ops/ and tests/.
# Real values live in .local/config.env (gitignored); defaults are placeholders.
_config_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _candidate in "$_config_dir/../.local/config.env" "$_config_dir/../config.example.env"; do
  if [ -f "$_candidate" ]; then
    # shellcheck disable=SC1090
    . "$_candidate"
    break
  fi
done
: "${WETWARE_VM:=ubuntu@192.0.2.10}"
: "${WETWARE_VM_IP:=192.0.2.10}"
: "${WETWARE_HONEYPOT:=root@192.0.2.10:2222}"
: "${WETWARE_PRIVATE_PROBE:=192.0.2.5}"
: "${WETWARE_SSH_KEY:=$HOME/.ssh/id_ed25519}"
: "${WETWARE_HOSTNAME:=prod-cortex-04}"
export WETWARE_VM WETWARE_VM_IP WETWARE_HONEYPOT WETWARE_PRIVATE_PROBE WETWARE_SSH_KEY WETWARE_HOSTNAME
VM="${VM:-$WETWARE_VM}"
TARGET="${TARGET:-$WETWARE_VM}"
