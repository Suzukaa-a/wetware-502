#!/usr/bin/env python3
"""cortex-deadman: baseboard watchdog for unresolved chamber incidents.

The BMC arms this unit when a rack node reports a sustained non-revenue fault
without a recorded engineering disposition.  If the console goes silent for the
whole window, the fail-deadly path applies NV-OPS-4.2(b) and the node is written
off.  A recorded disposition (stasis, mirror, or an explicit service close)
disarms the unit.

The window measures an *empty console*, not a slow operator: the engine stamps
``deadman.heartbeat`` on every command and this watchdog only fires when that
clock has been stale for the whole window.  Started with ``--watch`` it runs as
a resident vendor service; without it the legacy one-shot behaviour is kept.
"""
from __future__ import annotations

import os
import sys
import time

for _candidate in ("/var/lib/cortex-agent", "/usr/local/lib/cortex-agent",
                   "/opt/vesalius", os.path.dirname(os.path.abspath(__file__)),
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _candidate and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

UNIT = "cortex-deadman-4471"
DEADMAN_WINDOW = 600  # seconds of console silence before the fail-deadly path
POLL_SECONDS = 5


def _state_dir() -> str:
    return (os.environ.get("WETWARE_STATE_DIR")
            or "/var/lib/cortex-agent")


def _heartbeat_age() -> float | None:
    path = os.path.join(_state_dir(), "deadman.heartbeat")
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _verdict(state, flags) -> str | None:
    """Return None while the watchdog should keep watching, else a stop reason.

    ``fire`` means the incident reached the fail-deadly path; ``stop`` means the
    unit is no longer needed (the run was disposed or the platform is healthy).
    """
    if state.ending:
        return "stop"
    # a forced 200 (watchdog auto-shock) is not a disposition: the clock still runs
    if state.http_online and not flags.get("watchdog_auto_shock"):
        return "stop"
    if flags.get("silicon_mirror_active") or flags.get("liberation_done"):
        return "stop"
    try:
        stage = int(state.stage or 0)
    except Exception:
        stage = 0
    if stage < 3:
        return "stop"
    age = _heartbeat_age()
    if age is None or age >= DEADMAN_WINDOW:
        return "fire"
    return None


def _fire(state, flags) -> int:
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
        state_dir = _state_dir()
        os.makedirs(state_dir, mode=0o755, exist_ok=True)
        with open(os.path.join(state_dir, "deadman.fired"), "w",
                  encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass
    return 0


def watch() -> int:
    import state as state_mod

    while True:
        try:
            state = state_mod.State.load()
        except Exception:
            time.sleep(POLL_SECONDS)
            continue
        flags = state.flags or {}
        verdict = _verdict(state, flags)
        if verdict == "fire":
            return _fire(state, flags)
        if verdict == "stop":
            return 0
        time.sleep(POLL_SECONDS)


def once() -> int:
    try:
        import state as state_mod
    except Exception:
        return 1
    state = state_mod.State.load()
    flags = state.flags or {}
    verdict = _verdict(state, flags)
    if verdict == "fire":
        return _fire(state, flags)
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--watch" in args:
        return watch()
    return once()


if __name__ == "__main__":
    raise SystemExit(main())
