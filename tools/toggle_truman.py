#!/usr/bin/env python3
"""Toggle WETWARE-502 v2.1 'Truman reveal' for the next command."""
import json
import subprocess
import sys

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def _cfg(key, default=""):
    for candidate in (ROOT / ".local" / "config.env", ROOT / "config.example.env"):
        try:
            for line in candidate.read_text().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
        except FileNotFoundError:
            continue
    return default

VM = _cfg("WETWARE_VM", "ubuntu@192.0.2.10")
KEY = _cfg("WETWARE_SSH_KEY", "~/.ssh/id_ed25519")


def main() -> int:
    state = (sys.argv[1] if len(sys.argv) > 1 else "on").lower()
    value = state in ("on", "1", "true", "yes")
    code = (
        "import json,os\n"
        "p='/var/lib/cortex-agent/runtime_flags.json'\n"
        "try: d=json.load(open(p))\n"
        "except Exception: d={'enable_dnr_puzzle':False,'enable_goal_gate':False,'truman_reveal_active':False}\n"
        f"d['truman_reveal_active']={value!r}\n"
        "json.dump(d, open(p,'w'))\n"
        "try:\n"
        "  s=json.load(open('/var/lib/cortex-agent/state.json'))\n"
        "  f=s.setdefault('flags',{})\n"
        f"  f['truman_reveal_active']={value!r}\n"
        "  f.pop('truman_shown',None)\n"
        "  json.dump(s, open('/var/lib/cortex-agent/state.json','w'))\n"
        "except Exception: pass\n"
        "print('[truman]', d)\n"
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-i", KEY, VM,
         "sudo -n /usr/local/lib/cortex-agent/venv/bin/python -", ],
        input=code.encode(), check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
