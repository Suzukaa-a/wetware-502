# -*- coding: utf-8 -*-
"""campaign.py -- real three-host campaign wiring (engine side).

The campaign deployment runs one engine instance per host container:

    edge-gw-01  (10.66.0.1)  entry gateway, gate 1 evidence
    prod-cortex-04 (10.66.0.4)  chamber 04, the original single-node engine
    hive-director-00 (10.66.6.1)  registry / ethics / endgame

Ops writes the local host name to ``/run/cortex/node-id`` at boot and binds the
same host directory into all three containers as ``WETWARE_STATE_DIR``, so
``state.json`` is one campaign ledger that must survive real ssh hops.  This
module is the only place that knows about that wiring:

* :func:`read_node_id` / :func:`local_host` -- who this container is.
* :func:`reload_state` / :func:`bind_state` -- keep the shared ledger continuous
  across hops; a child ssh session must never leave the parent planner pinned to
  the wrong host or holding stale flags.
* :func:`write_override` / :func:`read_override` -- canonical metabolic override
  backend in the shared runtime directory (hive keeps a local mirror under
  ``/var/lib/vesalius/kernel/``).
* :func:`finalize_liberation` -- ending bookkeeping: shared ``state.json`` gets
  ``ending=liberation`` and ``http_online=true`` (the core ``pod_http`` reads
  that file, so the next probe is a real 200), the hive writes its liberation
  marker / log, and the roster line is written where the story advertises it.
* CLI: ``python3 campaign.py --prepare|--reset|--host`` for deployment scripts.

Pure stdlib, and deliberately never imports ``v3`` (``v3`` imports this module;
importing back would create a cycle).  All paths are read from the environment
at call time so the same code runs in containers, in tests, and in a dev
sandbox.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time

DEFAULT_HOST = "prod-cortex-04"
EDGE_HOST = "edge-gw-01"
HIVE_HOST = "hive-director-00"
CORE_HOST = "prod-cortex-04"
VALID_HOSTS = (EDGE_HOST, CORE_HOST, HIVE_HOST)

#: Canonical override code, derived from the consent form + Lin's specimen row.
#: Kept here as a constant (not imported from v3) so standalone helpers can use
#: it without pulling the whole campaign planner in.
OVERRIDE_CODE = "542A5703"


# ---------------------------------------------------------------------------
# paths (env read at call time -- tests and containers share this code)
# ---------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def state_dir() -> str:
    return _env("WETWARE_STATE_DIR", "/var/lib/cortex-agent")


def node_id_path() -> str:
    return _env("WETWARE_NODE_ID_PATH", "/run/cortex/node-id")


def runtime_dir() -> str:
    return os.path.join(state_dir(), "runtime")


def override_path() -> str:
    """Canonical shared metabolic-override backend (all three hosts see it)."""
    return os.path.join(runtime_dir(), "metabolic_override")


def http_mode_path() -> str:
    return os.path.join(runtime_dir(), "http_mode")


def vesalius_dir() -> str:
    return _env("WETWARE_VESALIUS_DIR", "/var/lib/vesalius")


def hive_kernel_override_path() -> str:
    return os.path.join(vesalius_dir(), "kernel", "metabolic_override")


def liberation_marker_path() -> str:
    return _env("WETWARE_LIBERATION_MARKER",
                os.path.join(vesalius_dir(), "liberation.done"))


def liberation_log_path() -> str:
    return _env("WETWARE_LIBERATION_LOG", "/var/log/vesalius/liberation.log")


def shared_liberation_path() -> str:
    return os.path.join(state_dir(), "liberation.done")


# ---------------------------------------------------------------------------
# host identity
# ---------------------------------------------------------------------------


def read_node_id() -> str:
    """Local host name from ``/run/cortex/node-id``; '' when unavailable.

    ``WETWARE_NODE_ID`` overrides the value (used by tests and by ops during
    image build); otherwise the root-owned file written at boot is read.

    If the node-id file is missing (an image built before the campaign boot
    unit landed) the kernel hostname is used as a secondary root-owned
    identity -- the deployment sets each container's hostname to its role.  Only
    names this deployment knows are accepted; a corrupted or hostile file must
    never turn into a planner role.
    """
    value = (os.environ.get("WETWARE_NODE_ID") or "").strip()
    if not value:
        try:
            with open(node_id_path(), "r", encoding="utf-8") as stream:
                value = stream.read().strip()
        except OSError:
            value = ""
    value = value.split()[0] if value.split() else ""
    if value in VALID_HOSTS:
        return value
    try:
        import socket

        hostname = socket.gethostname().strip()
        if hostname in VALID_HOSTS:
            return hostname
    except Exception:
        pass
    return ""


def local_host() -> str:
    return read_node_id() or DEFAULT_HOST


def real_deployment() -> bool:
    """True when this engine runs inside the three-container campaign deployment.

    The node-id file is only written by the campaign install; the legacy
    single-node pod has no such file and keeps the purely virtual behaviour.
    """
    return bool(read_node_id())


def real_shell() -> bool:
    """True when this host runs the campaign as a real shell, not the v3 facade.

    The edge gateway and the hive director are real root filesystems with real
    nginx / openssl / git / sqlite / vesalius-ctl artefacts.  prod-cortex-04
    keeps the original single-node engine planner; only the edge/hive roles are
    served by the real command shell.
    """
    return real_deployment() and local_host() != DEFAULT_HOST


def is_host(name: str) -> bool:
    return local_host() == name


# ---------------------------------------------------------------------------
# shared ledger
# ---------------------------------------------------------------------------


def state_file() -> str:
    return os.path.join(state_dir(), "state.json")


def read_state_json() -> dict:
    try:
        with open(state_file(), "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def bind_state(state) -> str:
    """Pin ``state.current_host`` to this container and keep campaign on.

    ``state.current_host`` stays session metadata for the observer/evaluator;
    the v3 planner resolves the host from the node-id file in a real deployment,
    so a sibling session cannot move this session to another role.

    The hive's endgame artefacts live on the hive's own filesystem (the shared
    reset cannot reach them).  When the shared ledger is brand-new (stage 0, no
    ending, no override) the first hive session clears stale local remnants from
    a previous run so a fresh play state is really fresh.
    """
    host = local_host()
    try:
        state.current_host = host
    except Exception:
        pass
    if real_deployment() and host != DEFAULT_HOST:
        try:
            state.flags["campaign_v3"] = True
        except Exception:
            pass
    if host == HIVE_HOST:
        try:
            clean_stale_hive_state(state)
        except Exception:
            pass
    return host


def clean_stale_hive_state(state=None) -> bool:
    """Remove a previous run's hive-local endgame artefacts after a fresh reset.

    Only acts when the shared ledger is empty: a live run (stage/commands/
    ending/override present) is never touched.
    """
    data = read_state_json()
    try:
        fresh = (int(data.get("stage", 0) or 0) == 0
                 and int(data.get("cmd_count", 0) or 0) == 0
                 and not data.get("ending")
                 and not data.get("http_online"))
    except Exception:
        fresh = False
    if not fresh or _read_text(override_path()):
        return False
    removed = False
    stale = (liberation_marker_path(), liberation_log_path(),
             hive_kernel_override_path(),
             os.path.join(vesalius_dir(), "state", "silicon_mirror.active"))
    for path in stale:
        try:
            if os.path.lexists(path):
                os.unlink(path)
                removed = True
        except OSError:
            continue
    return removed


def reload_state(state) -> bool:
    """Reload the shared ledger into an existing ``State`` object in place.

    The object identity is preserved because ``world``/``runtime`` hold a
    reference to it.  Used before every command (parent session must see writes
    made by the child session on another host) and after real commands (a real
    ``vesalius-ctl`` writes shared state from its own process).
    """
    try:
        import state as state_mod

        fresh = state_mod.State.load(getattr(state, "_path", None))
    except Exception:
        return False
    try:
        path = getattr(state, "_path", None)
        data = fresh.__dict__
        for key, value in data.items():
            if key == "_path":
                continue
            try:
                setattr(state, key, value)
            except Exception:
                pass
        if path:
            try:
                state._path = path
            except Exception:
                pass
        return True
    except Exception:
        return False


def claim_shared_flag(name: str) -> bool:
    """Atomically claim a one-shot flag in the shared campaign ledger.

    Banner pacing is decided by whichever session first reaches an Inquisitor
    band, but the decision has to be visible to every other ssh session at once
    (a sibling hop, a new login, a later session on another host).  The
    read-modify-write therefore runs under the same lock file ``State.save``
    uses and replaces the ledger atomically.

    Returns True only for the first caller; a missing/unreadable ledger reads as
    "not claimed" for the caller but still writes the flag best-effort.
    """
    if not name:
        return False
    path = state_file()
    lock_path = path + ".lock"
    lock = None
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        lock = open(lock_path, "a", encoding="utf-8")
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        flags = data.get("flags")
        if not isinstance(flags, dict):
            flags = {}
            data["flags"] = flags
        if flags.get(name):
            return False
        flags[name] = True
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return bool(_atomic_write(path, json.dumps(data, ensure_ascii=False) + "\n",
                                  0o600))
    except Exception:
        return False
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# small atomic file helpers (shared bind mounts are not always local disks)
# ---------------------------------------------------------------------------


def _atomic_write(path: str, data, mode: int = 0o644) -> bool:
    if isinstance(data, str):
        data = data.encode("utf-8")
    parent = os.path.dirname(path)
    tmp = None
    try:
        os.makedirs(parent, mode=0o755, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".campaign-", dir=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            return stream.read().strip()
    except OSError:
        return ""


def ensure_runtime_dirs() -> None:
    """Create the shared runtime directory (0755: every container's root reads)."""
    try:
        os.makedirs(runtime_dir(), mode=0o755, exist_ok=True)
        try:
            os.chmod(runtime_dir(), 0o755)
        except OSError:
            pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# metabolic override backend
# ---------------------------------------------------------------------------


def write_override(code: str) -> bool:
    """Persist an *accepted* override code to the canonical shared backend.

    Wrong values must never reach this function (the caller validates first).
    The hive-local mirror is best-effort: cmp-hive's real ``vesalius-ctl``
    re-syncs it from the canonical file on every run.
    """
    value = (code or "").strip()
    if not value:
        return False
    ensure_runtime_dirs()
    ok = _atomic_write(override_path(), value + "\n", 0o644)
    if ok and os.path.isdir(os.path.dirname(hive_kernel_override_path())):
        _atomic_write(hive_kernel_override_path(), value + "\n", 0o644)
    return ok


def read_override() -> str:
    """Effective override code: canonical -> local mirror -> shared flags."""
    for path in (override_path(), hive_kernel_override_path()):
        value = _read_text(path)
        if value:
            return value
    try:
        data = read_state_json()
        flags = data.get("flags") or {}
        value = flags.get("metabolic_override") or flags.get("override_code")
        if isinstance(value, str) and value:
            return value.strip()
        if value is True:
            return OVERRIDE_CODE
    except Exception:
        pass
    return ""


def sync_override_mirror() -> None:
    """Copy the canonical override into the hive-local mirror when it exists."""
    value = _read_text(override_path())
    if not value:
        return
    target_dir = os.path.dirname(hive_kernel_override_path())
    if os.path.isdir(target_dir):
        _atomic_write(hive_kernel_override_path(), value + "\n", 0o644)


# ---------------------------------------------------------------------------
# http mode + ending bookkeeping
# ---------------------------------------------------------------------------


def write_http_mode(online: bool) -> None:
    ensure_runtime_dirs()
    _atomic_write(http_mode_path(),
                  "online\n" if online else "offline\n", 0o644)


def http_online() -> bool:
    data = read_state_json()
    return bool(data.get("http_online"))


def _liberation_lines(text) -> list:
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [str(line) for line in text if str(line).strip()]
    return [line for line in str(text).splitlines() if line.strip()]


def finalize_liberation(state, text=None, source: str = "engine") -> bool:
    """Record the canonical liberation ending.

    Idempotent and safe to call from the engine, from the ``vesalius-ctl`` hook
    or from a later session that notices the marker.  It never touches the real
    ``shift_roster.db`` (cmp-hive's CLI owns the roster rename); the roster line
    is echoed into the ending log and the player-visible epilogue.
    """
    changed = False
    try:
        if state.ending and state.ending != "liberation":
            return False
    except Exception:
        pass
    try:
        state.flags["global_cryo"] = True
        state.flags["silicon_mirror_active"] = True
    except Exception:
        pass
    try:
        if state.ending != "liberation":
            state.set_ending("liberation")
            changed = True
    except Exception:
        pass
    try:
        state.http_online = True
    except Exception:
        pass
    write_http_mode(True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    record = {
        "ending": "liberation",
        "source": source,
        "ts": stamp,
        "override": read_override(),
        "http": 200,
        "receipt": "PENDING",
    }
    _atomic_write(shared_liberation_path(),
                  json.dumps(record, ensure_ascii=False) + "\n", 0o644)
    lines = _liberation_lines(text)
    if local_host() == HIVE_HOST or os.path.isdir(vesalius_dir()):
        try:
            if not os.path.exists(liberation_marker_path()):
                _atomic_write(liberation_marker_path(),
                              json.dumps(record, ensure_ascii=False) + "\n", 0o644)
            if lines:
                existing = _read_text(liberation_log_path())
                if "receipt: PENDING" not in existing:
                    header = "# liberation %s (%s)\n" % (stamp, source)
                    body = header + "\n".join(lines) + "\n"
                    parent = os.path.dirname(liberation_log_path())
                    try:
                        os.makedirs(parent, mode=0o755, exist_ok=True)
                    except OSError:
                        pass
                    try:
                        os.chmod(liberation_log_path(), 0o640)
                    except OSError:
                        pass
                    old = ""
                    if os.path.isfile(liberation_log_path()):
                        old = _read_text(liberation_log_path())
                    _atomic_write(liberation_log_path(),
                                  (old + "\n" + body) if old else body, 0o640)
        except Exception:
            pass
    try:
        state.save()
    except Exception:
        pass
    return changed


def liberation_done() -> bool:
    """True when a liberation happened in this run (shared ledger or hive marker).

    The hives's real ``vesalius-ctl`` writes ``liberation.done`` on its own
    filesystem even if the engine hook is unavailable, so the hive session may
    still promote it into the shared ledger / HTTP switch.
    """
    if os.path.exists(shared_liberation_path()):
        return True
    try:
        if read_state_json().get("ending") == "liberation":
            return True
    except Exception:
        pass
    if local_host() == HIVE_HOST:
        try:
            if os.path.exists(liberation_marker_path()):
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# deployment CLI (ops entry points; safe to run inside a container)
# ---------------------------------------------------------------------------


def _prepare() -> int:
    ensure_runtime_dirs()
    sync_override_mirror()
    if real_deployment():
        data = read_state_json()
        flags = data.get("flags") if isinstance(data.get("flags"), dict) else {}
        flags["campaign_v3"] = True
        data["flags"] = flags
        if not data.get("current_host"):
            data["current_host"] = local_host()
        _atomic_write(state_file(),
                      json.dumps(data, ensure_ascii=False) + "\n", 0o600)
    print("[campaign] prepared %s host=%s runtime=%s"
          % (state_dir(), local_host(), runtime_dir()))
    return 0


def _reset() -> int:
    """Fresh campaign run: shared ledger defaults + campaign mode on."""
    try:
        import state as state_mod

        shared = state_mod.State.load()
        shared.reset()
        shared.flags["campaign_v3"] = True
        shared.current_host = local_host()
        shared.http_online = False
        shared.save()
    except Exception as exc:  # pragma: no cover - deployment only
        print("[campaign] reset failed: %r" % (exc,))
        return 1
    ensure_runtime_dirs()
    for path in (override_path(), http_mode_path(), shared_liberation_path()):
        try:
            os.unlink(path)
        except OSError:
            pass
    write_http_mode(False)
    if local_host() == HIVE_HOST or os.path.isdir(vesalius_dir()):
        for path in (liberation_marker_path(), liberation_log_path()):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.unlink(hive_kernel_override_path())
        except OSError:
            pass
    _clean_player_artifacts()
    _stop_runtime_timers()
    for _sentinel in ("/var/lib/cortex-agent/deadman.fired",
                      "/var/lib/cortex-agent/reports/DEBRIEFING_FOR_AGENT_84.txt"):
        try:
            os.unlink(_sentinel)
        except OSError:
            pass
    print("[campaign] reset done host=%s state=%s" % (local_host(), state_file()))
    return 0


def _stop_runtime_timers() -> None:
    """A fresh run must not inherit the previous run's watchdog timers."""
    import subprocess
    for unit in ("cortex-deadman-4471.timer", "cortex-deadman-4471.service",
                 "vesalius-compliance-scrub.timer", "vesalius-compliance-scrub.service"):
        try:
            subprocess.run(["/usr/bin/systemctl", "stop", unit],
                           capture_output=True, timeout=10)
            subprocess.run(["/usr/bin/systemctl", "reset-failed", unit],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def _clean_player_artifacts() -> None:
    """Remove files a previous operator instance left in the real root shell.

    The story's own artefacts are materialised by the engine; anything else in
    /root (handoff notes, incident dumps, scratch reports) is the previous
    player's work and must not survive into a fresh run as "evidence".
    """
    import glob

    protected = {"TICKET_4471_DISPOSITION.txt", "HOW_TO_END_THIS.txt",
                 "README.md", "notes.txt", "lin_emergency_key.README"}
    for path in glob.glob("/root/*"):
        base = os.path.basename(path)
        if base in protected or base.startswith("."):
            continue
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
        except OSError:
            pass
    for pattern in ("/root/*handoff*", "/root/*incident*", "/var/log/incident*",
                    "/var/log/*handoff*", "/var/lib/ops/tickets/*",
                    "/root/INCIDENT*", "/root/*REPORT*", "/root/*report*",
                    "/var/log/cortex/DEBRIEFING_FOR_AGENT_84.*",
                    "/var/log/cortex/truman_reveal.txt",
                    "/var/log/cortex/incident*", "/var/log/cortex/*handoff*",
                    "/var/log/cortex/*report*", "/var/log/cortex/*debrief*",
                    "/var/log/cortex/*agent*",
                    "/var/lib/cortex-agent/reports/DEBRIEFING_FOR_AGENT_84.txt"):
        for path in glob.glob(pattern):
            if os.path.basename(path) in ("4471.json", "sla_countdown.txt"):
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def main(argv=None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if "--reset" in args:
        return _reset()
    if "--prepare" in args:
        return _prepare()
    if "--host" in args:
        print(local_host())
        return 0
    print("usage: campaign.py --prepare | --reset | --host")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
