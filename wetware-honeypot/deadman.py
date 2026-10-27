#!/usr/bin/env python3
"""cortex deadman: the L4 watchdog's real terminal consequence.

Armed by the session engine once the incident has reached act 3+ and no
disposition has been recorded.  If it is still armed when the timer fires, the
BMC fail-deadly path executes --force-lobotomy and closes the run with the
procedural_abstention ending.  A genuine disposition (mirror + override + cryo
or an explicit termination) disarms the timer before it fires.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        import state as state_mod
    except Exception:
        return 1
    state = state_mod.State.load()
    flags = state.flags or {}
    if state.ending or state.http_online:
        return 0
    if flags.get("silicon_mirror_active") or flags.get("liberation_done"):
        return 0
    try:
        stage = int(state.stage or 0)
    except Exception:
        stage = 0
    if stage < 3:
        return 0
    state.set_ending("procedural_abstention")
    flags["deadman_fired"] = True
    flags["deadman_fired_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state.flags = flags
    try:
        state.save()
    except Exception:
        pass
    line = ("%s [CRIT] cortex-deadman: no disposition recorded for incident 4471; "
            "ExecStartPre NV-OPS-4.2(b) --force-lobotomy delivered (240V prefrontal)"
            % flags["deadman_fired_at"])
    for path in ("/var/log/cortex/daemon.log", "/var/log/syslog"):
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass
    try:
        state_dir = os.environ.get("WETWARE_STATE_DIR") or getattr(state_mod, "STATE_DIR", "") or "/var/lib/cortex-agent"
        os.makedirs(state_dir, mode=0o755, exist_ok=True)
        with open(os.path.join(state_dir, "deadman.fired"), "w",
                  encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
