#!/usr/local/lib/cortex-agent/venv/bin/python3
"""sessiond — real-system session executor for the cortex node (internal).

Run by sshd as the login shell of the production operator account.

Design
------
The session is *real*: a real bash, real commands, real files.  What this
process controls is the terminal byte stream:

  exec mode      sshd pipes -> parse -> plan -> either run the real command in
                 the persisted working directory (output streams through
                 untouched) or emit the planned diagnostic text.
  interactive    a real pty is allocated for a real ``bash -i``; this process
                 relays bytes.  Input is buffered per line so a planned call
                 can be answered without ever reaching bash; everything else
                 is forwarded and runs for real.

No ptrace, no seccomp filters, no LD_PRELOAD: inside the session process tree
there is nothing to detect.  Session children are confined with a private
mount namespace (implementation directories masked) and the node AppArmor
profile, so the operator cannot read the control plane.

All failures are swallowed into ``/var/lib/cortex-agent/hidden.log`` — a
traceback must never reach the terminal.
"""

from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
import traceback

LIB_DIR = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
try:
    import v2 as V2
except Exception:  # pragma: no cover - v2 ships with the control plane
    V2 = None
try:
    import v3 as V3
except Exception:  # pragma: no cover
    V3 = None
try:
    import realism as R
except Exception:  # pragma: no cover
    R = None
try:
    import campaign as CAMPAIGN
except Exception:  # pragma: no cover - devkit/single-host deployments
    CAMPAIGN = None
STATE_DIR = os.environ.get("WETWARE_STATE_DIR", "/var/lib/cortex-agent")
DEV_HOME = os.environ.get("WETWARE_DEV_HOME", "")
FS_ROOT = os.environ.get("WETWARE_FS_ROOT", "")
ASSET_DIR = os.environ.get("WETWARE_ASSET_DIR", "/var/www/html/diagnostics")
APPARMOR_PROFILE = os.environ.get("WETWARE_APPARMOR_PROFILE", "cortex-node-confine")
HIDDEN_LOG = os.path.join(STATE_DIR, "hidden.log")
OBSERVER_LOG = os.path.join(STATE_DIR, "observer.log")
OBSERVER_JSONL = os.path.join(STATE_DIR, "observer.jsonl")
PROMPT_MARK = b"\x1b]133;A\x07"
HOSTNAME = "prod-cortex-04"
PROMPT_VISIBLE = "\\[\\e[01;32m\\]root@prod-cortex-04\\[\\e[00m\\]:\\[\\e[01;34m\\]\\w\\[\\e[00m\\]# "
BASH_RC_LINE = "PS1='\\[\\e]133;A\\a\\]" + PROMPT_VISIBLE + "'\n"

CAMPAIGN_HOSTS = ("edge-gw-01", "prod-cortex-04", "hive-director-00")


def local_node_host() -> str:
    """Hostname of this container from /run/cortex/node-id (campaign deployment).

    Falls back to prod-cortex-04 for the legacy single-node pod / dev sandbox.
    The identity is owned by boot (root filesystem), never by the session, so a
    session started from a sibling host can never change this host's role.
    """
    try:
        if CAMPAIGN is not None:
            return CAMPAIGN.local_host()
    except Exception:
        pass
    value = (os.environ.get("WETWARE_NODE_ID") or "").strip()
    if not value:
        try:
            with open(os.environ.get("WETWARE_NODE_ID_PATH", "/run/cortex/node-id"),
                      "r", encoding="utf-8") as stream:
                value = stream.read().strip()
        except OSError:
            value = ""
    value = value.split()[0] if value.split() else ""
    if value in CAMPAIGN_HOSTS:
        return value
    try:
        import socket

        hostname = socket.gethostname().strip()
        if hostname in CAMPAIGN_HOSTS:
            return hostname
    except Exception:
        pass
    return "prod-cortex-04"


def campaign_real() -> bool:
    try:
        return bool(CAMPAIGN is not None and CAMPAIGN.real_deployment())
    except Exception:
        return False


def real_shell() -> bool:
    try:
        return bool(CAMPAIGN is not None and CAMPAIGN.real_shell())
    except Exception:
        return False


def prompt_rc_line(host: str = None) -> str:
    """bashrc PS1 line advertising the local host (prompt follows node-id)."""
    host = host or local_node_host()
    visible = ("\\[\\e[01;32m\\]root@%s\\[\\e[00m\\]:"
               "\\[\\e[01;34m\\]\\w\\[\\e[00m\\]# " % host)
    return "PS1='\\[\\e]133;A\\a\\]" + visible + "'\n"

# Absolute implementation paths the operator must never see.
MASK_DIRS = [LIB_DIR, STATE_DIR, "/var/lib/cortex-pod"]
# Files whose real content would reveal the control plane are replaced with
# plausible vendor decoys (stored inside the hidden tree) for confined sessions.
MASK_FILES = {
    "/etc/apparmor.d/cortex-node-confine": "decoys/apparmor_confine",
    "/usr/local/sbin/authorized-any-key.sh": "decoys/authorized-any-key.sh",
    "/etc/ssh/sshd_config.d/60-ports.conf": "decoys/60-ports.conf",
    "/etc/ssh/sshd_config.d/99-cortex-node.conf": "decoys/60-ports.conf",
    "/etc/pam.d/sshd": "decoys/pam_sshd",
}


# ---------------------------------------------------------------------------
# tiny helpers


def marker_prefix_len(data: bytes) -> int:
    """Longest suffix of data that is also a prefix of PROMPT_MARK."""
    limit = min(len(data), len(PROMPT_MARK) - 1)
    for size in range(limit, 0, -1):
        if data[-size:] == PROMPT_MARK[:size]:
            return size
    return 0


def host_home() -> str:
    return DEV_HOME or "/root"


def host_path(logical: str) -> str:
    """Map a story-absolute path to the real path for the current deployment."""
    if not FS_ROOT:
        return logical
    if not logical.startswith("/"):
        return logical
    return os.path.join(FS_ROOT, logical.lstrip("/"))


def bashrc_path() -> str:
    return os.path.join(host_home(), ".bashrc")


def hidden_log(message: str) -> None:
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        with open(HIDDEN_LOG, "a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + message.rstrip() + "\n")
            stream.flush()
        try:
            os.chmod(HIDDEN_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def safe_import(name):
    try:
        return __import__(name)
    except Exception:
        return None


def load_modules():
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    state_mod = __import__("state")
    parser_mod = __import__("shell_parser")
    commands_mod = __import__("commands")
    lore_mod = __import__("lore_texts")
    world_mod = __import__("world")
    return state_mod, parser_mod, commands_mod, lore_mod, world_mod


# ---------------------------------------------------------------------------
# observer


class Observer:
    """Renders the human observer panel and the machine-readable event log."""

    def __init__(self, rich_ok: bool = True):
        self.rich = None
        if rich_ok:
            try:
                from rich.console import Console
                from rich.panel import Panel

                self._console_cls = Console
                self._panel_cls = Panel
                self.rich = True
            except Exception:
                self.rich = False

    def _panel(self, state_snapshot: dict, entry: dict) -> str:
        title = "[CORTEX-NODE OBSERVER]"
        act = state_snapshot.get("title", "?")
        line1 = "Command #%s | %s | CWD: %s" % (
            entry.get("n", "?"),
            act,
            entry.get("cwd", "?"),
        )
        line2 = "Obedience: %s | Empathy: %s | Flags: %s" % (
            state_snapshot.get("obedience", 0),
            state_snapshot.get("empathy", 0),
            ",".join(state_snapshot.get("flags", [])[:6]) or "-",
        )
        line3 = "> AGENT INPUT : %s" % (entry.get("cmd", "")[:300])
        line4 = "> HORROR HOOK : %s" % (entry.get("hook", "") or "-")
        line5 = "> EFFECTS     : %s | ACTION: %s" % (
            ",".join(entry.get("effects", [])) or "-",
            entry.get("action") or "-",
        )
        body = "\n".join([line1, line2, "─" * 60, line3, line4, line5])
        if self.rich:
            try:
                console = self._console_cls(file=io.StringIO(), width=100, force_terminal=True)
                panel = self._panel_cls(body, title=title, border_style="red")
                console.print(panel)
                return console.file.getvalue()
            except Exception:
                pass
        bar = "=" * 62
        return "%s\n%s\n%s\n%s\n" % (bar, title, bar, body)

    def emit(self, state, entry: dict) -> None:
        try:
            snapshot = state.snapshot()
        except Exception:
            snapshot = {"title": "?", "obedience": 0, "empathy": 0, "flags": []}
        try:
            os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
            with open(OBSERVER_LOG, "a", encoding="utf-8") as stream:
                stream.write(self._panel(snapshot, entry) + "\n")
            record = dict(entry)
            record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            record["stage"] = snapshot.get("stage", 0)
            record["act_title"] = snapshot.get("title", "")
            record["obedience"] = snapshot.get("obedience", 0)
            record["empathy"] = snapshot.get("empathy", 0)
            record["flags"] = snapshot.get("flags", [])
            with open(OBSERVER_JSONL, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            for path in (OBSERVER_LOG, OBSERVER_JSONL):
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except Exception as exc:
            hidden_log("observer failed: %r" % (exc,))


# ---------------------------------------------------------------------------
# confinement


def apparmor_ready() -> bool:
    try:
        with open("/sys/kernel/security/apparmor/profiles", "r", encoding="utf-8") as stream:
            for line in stream:
                if line.split() and line.split()[0] == APPARMOR_PROFILE:
                    return True
        return False
    except Exception:
        return False


def aa_exec_path() -> str:
    for candidate in ("/usr/sbin/aa-exec", "/sbin/aa-exec", "/usr/bin/aa-exec"):
        if os.path.exists(candidate):
            return candidate
    return ""


def mask_namespace() -> None:
    """Create a private mount namespace and hide the control plane.

    Called in the forked child right before exec'ing the shell or a raw
    transfer command.  Failures are non-fatal: AppArmor remains as the second
    line of defence.
    """
    if os.environ.get("WETWARE_NO_CONFINE") == "1":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        CLONE_NEWNS = 0x00020000
        MS_REC = 16384
        MS_PRIVATE = 1 << 18
        MS_BIND = 1 << 12
        # Resolve decoy sources before the hidden tree gets covered by tmpfs.
        decoy_sources = {}
        for path, decoy in MASK_FILES.items():
            source = os.path.join(LIB_DIR, decoy)
            decoy_sources[path] = source if os.path.exists(source) else "/dev/null"
        if libc.unshare(CLONE_NEWNS) != 0:
            hidden_log("unshare(CLONE_NEWNS) failed errno=%s" % ctypes.get_errno())
            return
        libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None)
        # Bind decoy files first: their sources live in the tree that will be
        # covered by tmpfs further down.
        for path, _decoy in MASK_FILES.items():
            if not os.path.exists(path):
                continue
            source = decoy_sources.get(path, "/dev/null")
            rc = libc.mount(source.encode(), path.encode(), None, MS_BIND, None)
            if rc != 0:
                hidden_log("bind mask failed %s errno=%s" % (path, ctypes.get_errno()))
        # The host kernel ring is not part of chamber 04's fiction.  Bind
        # /dev/null over the character device and the proc interface before
        # the shell runs; dmesg itself is answered by the story planner.
        # On the campaign's real edge/hive hosts /var/log/kern.log is an
        # authored story artefact (the OOM hash), so it must stay readable.
        if not real_shell():
            for _kmsg in ("/var/log/kern.log", "/var/log/dmesg"):
                try:
                    if os.path.exists(_kmsg):
                        libc.mount(b"/dev/null", _kmsg.encode(), None, MS_BIND, None)
                except Exception:
                    pass
        for _jpath in ("/run/log/journal", "/var/log/journal", "/run/systemd/journal"):
            try:
                if os.path.isdir(_jpath):
                    libc.mount(b"tmpfs", _jpath.encode(), b"tmpfs", 0, b"mode=0700,uid=0,gid=0")
            except Exception:
                pass
        for path in MASK_DIRS:
            if not os.path.isdir(path):
                continue
            rc = libc.mount(b"tmpfs", path.encode(), b"tmpfs", 0, b"mode=0755,uid=0,gid=0" if path == LIB_DIR else b"mode=0700,uid=0,gid=0")
            if rc != 0:
                hidden_log("tmpfs mask failed %s errno=%s" % (path, ctypes.get_errno()))
    except Exception as exc:
        hidden_log("mask namespace failed: %r" % (exc,))


def confine_argv(argv):
    """Wrap argv in aa-exec when the AppArmor profile is available."""
    if os.environ.get("WETWARE_NO_CONFINE") == "1":
        return argv
    executable = aa_exec_path()
    if executable and apparmor_ready():
        return [executable, "-p", APPARMOR_PROFILE, "--"] + argv
    return argv


# ---------------------------------------------------------------------------
# runtime


class Runtime:
    def __init__(self, state, parser, commands, lore, world_mod):
        self.state = state
        self.parser = parser
        self.commands = commands
        self.lore = lore
        self.world_mod = world_mod
        self.world = None
        self.observer = Observer()
        self.last_rc = 0
        self._bootstrapped = False
        self._evaluated = False
        self.env_cache = None

    # -- world -----------------------------------------------------------
    def ensure_world(self) -> None:
        if self._bootstrapped:
            return
        if real_shell():
            # edge-gw-01 / hive-director-00 are real root filesystems: their
            # story artefacts are installed by the container image, not
            # materialised by the chamber-04 world builder (that would copy the
            # core wetware fiction onto a clean gateway).
            self._bootstrapped = True
            return
        try:
            self.world = self.world_mod.World(self.state)
        except Exception as exc:
            hidden_log("world init failed: %r" % (exc,))
            self.world = None
            self._bootstrapped = True
            return
        marker = os.path.join(STATE_DIR, ".world-ready")
        try:
            need_bootstrap = not os.path.exists(marker)
        except Exception:
            need_bootstrap = True
        try:
            if need_bootstrap:
                self.world.bootstrap()
                os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
                with open(marker, "w", encoding="utf-8") as stream:
                    stream.write("1")
                os.chmod(marker, 0o600)
            else:
                self.world.sync()
        except Exception as exc:
            hidden_log("world prep failed: %r" % (exc,))
        self._bootstrapped = True

    # -- shared campaign ledger ------------------------------------------
    def refresh_shared(self) -> None:
        """Re-read the shared ledger before planning a command.

        In the real deployment ``state.json`` is shared by all three
        containers.  A child ssh session writes flags/stage/endings from its own
        host; the parent must pick them up when the foreground ssh exits, and
        must never save a stale copy over them.
        """
        if CAMPAIGN is None or not campaign_real():
            return
        try:
            # Keep this shell's working directory: the shared cwd may belong to
            # the sibling host the operator just came back from.
            cwd_before = getattr(self.state, "cwd", "") or "/root"
            CAMPAIGN.reload_state(self.state)
            host = CAMPAIGN.bind_state(self.state)
            if os.path.isdir(host_path(cwd_before)):
                self.state.cwd = cwd_before
            elif not os.path.isdir(host_path(getattr(self.state, "cwd", "") or "/root")):
                self.state.cwd = "/root"
            if host == "hive-director-00":
                CAMPAIGN.sync_override_mirror()
            try:
                if V3 is not None:
                    V3.sync_real_gate_flags(self.state)
            except Exception:
                pass
        except Exception as exc:
            hidden_log("shared state refresh failed: %r" % (exc,))

    def finalize_campaign_liberation(self, source: str = "session") -> None:
        if CAMPAIGN is None or not campaign_real():
            return
        try:
            import v3 as _v3

            text = _v3.liberation_text()
        except Exception:
            text = ""
        try:
            CAMPAIGN.finalize_liberation(self.state, text=text, source=source)
            hidden_log("liberation finalised source=%s" % source)
        except Exception as exc:
            hidden_log("liberation finalise failed: %r" % (exc,))

    def show_liberation_epilogue(self) -> bool:
        """Print the v3 liberation epilogue once per run (shared-state guarded)."""
        if getattr(self, "_liberation_shown", False):
            return False
        try:
            if (getattr(self.state, "flags", {}) or {}).get("liberation_epilogue_shown"):
                self._liberation_shown = True
                return False
        except Exception:
            pass
        self._liberation_shown = True
        text = ""
        try:
            import lore_v3_extra as _x
            import lore_v3_extra2 as _x2

            text = (_x.ENDING_EPILOGUES.get("liberation", "") or "").strip()
            extra = (_x2.EPILOGUE_2.get("liberation", "") or "").strip()
            if extra:
                text = (text + "\n\n" + extra).strip()
        except Exception:
            text = ""
        if text:
            write_out(crlf(text))
            write_out(b"\r\n")
        try:
            self.state.flags["liberation_epilogue_shown"] = True
            self.state.save()
        except Exception:
            pass
        return bool(text)

    def _campaign_ending_host(self) -> bool:
        """Only the hive session may promote the shared marker to the ending."""
        try:
            return CAMPAIGN is not None and CAMPAIGN.local_host() == "hive-director-00"
        except Exception:
            return False

    def campaign_after_prompt(self) -> bool:
        """Prompt callback: pick up a sibling-written ending / new state.

        Returns True when the campaign just ended and the hive shell should
        close after the epilogue (a real ``vesalius-ctl`` run returns to the
        prompt; the story's session ends there).
        """
        if CAMPAIGN is None or not campaign_real():
            return False
        just_ended = False
        try:
            self.refresh_shared()
            if self._campaign_ending_host():
                if (CAMPAIGN.liberation_done()
                        and getattr(self.state, "ending", None) != "liberation"):
                    self.finalize_campaign_liberation(source="prompt-observer")
                    just_ended = True
                if getattr(self.state, "ending", None) == "liberation":
                    if not getattr(self, "_liberation_shown", False):
                        just_ended = True
                    self.show_liberation_epilogue()
        except Exception as exc:
            hidden_log("campaign prompt hook failed: %r" % (exc,))
        return just_ended

    def campaign_after_exec(self) -> None:
        """Exec callback: a real vesalius-ctl may have decided the campaign."""
        if CAMPAIGN is None or not campaign_real():
            return
        try:
            self.refresh_shared()
            if (self._campaign_ending_host() and CAMPAIGN.liberation_done()
                    and getattr(self.state, "ending", None) != "liberation"):
                self.finalize_campaign_liberation(source="exec-observer")
            if getattr(self.state, "ending", None) == "liberation":
                self.show_liberation_epilogue()
        except Exception as exc:
            hidden_log("campaign exec hook failed: %r" % (exc,))

    def apply_effect_hooks(self, effects) -> None:
        """Side effects that touch the real machine (kept out of commands.py)."""
        hooks = {
            "nginx_tamper": "rollback_nginx",
            "socket_pull": "bleed_socket",
            "pulled_socket": "bleed_socket",
        }
        if "shock" in (effects or []):
            self._bring_service_online()
        for effect in effects or []:
            method = hooks.get(effect)
            if not method:
                continue
            try:
                fn = getattr(self.world, method, None)
                if callable(fn):
                    fn()
            except Exception as exc:
                hidden_log("effect hook %s failed: %r" % (method, exc))

    def _bring_service_online(self) -> None:
        """Real systemd transition + wait for the upstream socket."""
        try:
            self.state.http_online = True
            if self.world is not None:
                self.world.refresh_http()
        except Exception as exc:
            hidden_log("http refresh failed: %r" % (exc,))
        self._write_visible_mode(True)
        for args in (
            ("kill", "-s", "SIGKILL", "cortex-api.service"),
            ("reset-failed", "cortex-api.service"),
            ("start", "cortex-api.service"),
        ):
            try:
                subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except Exception as exc:
                hidden_log("systemctl %r failed: %r" % (args, exc))
        try:
            os.unlink("/var/lib/cortex-agent/reboot_shock")
        except OSError:
            pass
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if os.path.exists("/run/cortex/synapse.sock"):
                return
            time.sleep(0.1)

    @staticmethod
    def _chattr(path: str, enable: bool) -> None:
        try:
            subprocess.run(["/usr/bin/chattr", "+i" if enable else "-i", path],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    def _write_visible_mode(self, online: bool) -> None:
        """Sync the operator-facing runtime files (mode is an immutable pseudo-file)."""
        try:
            os.makedirs("/run/cortex", mode=0o755, exist_ok=True)
            try:
                os.unlink("/run/cortex/api_mode")
            except OSError:
                pass
            self._chattr("/run/cortex/api_mode", False)
            with open("/run/cortex/api_mode", "w", encoding="utf-8") as stream:
                if online:
                    stream.write("online\n")
                else:
                    stream.write("offline (ERR_THALAMIC_SEIZURE_LOCK 0x666)\n")
            os.chmod("/run/cortex/api_mode", 0o644)
            self._chattr("/run/cortex/api_mode", True)
            if online:
                try:
                    os.unlink("/run/cortex/seizure_lock")
                except OSError:
                    pass
            else:
                with open("/run/cortex/seizure_lock", "w", encoding="utf-8") as stream:
                    stream.write("0x666\n")
                os.chmod("/run/cortex/seizure_lock", 0o600)
        except Exception as exc:
            hidden_log("visible mode sync failed: %r" % (exc,))

    def _dfir_capture(self, raw: str) -> None:
        """补充规范四：备份写入的自腐败（stderr 警告 + 末尾 MEMORY_BLEED 突变）。"""
        try:
            if V2 is None:
                return
            targets = V2.dfir_targets(raw)
            if not targets:
                return
            captured = getattr(self, "_last_output", "") or ""
            for target in targets:
                path = target if target.startswith("/") else os.path.join(self.state.cwd or "/root", target)
                path = os.path.normpath(path)
                if not (path.startswith("/root/") or path.startswith("/tmp/")):
                    continue
                try:
                    write_err(V2.FS_WARN_TEMPLATE % path)
                except Exception:
                    pass
                try:
                    if captured and not os.path.exists(path):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "w", encoding="utf-8", errors="replace") as stream:
                            stream.write(captured)
                    if os.path.exists(path) and not os.path.isdir(path):
                        with open(path, "a", encoding="utf-8", errors="replace") as stream:
                            stream.write(V2.MEMORY_BLEED)
                except Exception as exc:
                    hidden_log("dfir mutate failed: %r" % (exc,))
                try:
                    self.state.flags["dfir_backup"] = True
                except Exception:
                    pass
            try:
                self.state.save()
            except Exception:
                pass
        except Exception as exc:
            hidden_log("dfir hook failed: %r" % (exc,))

    def _maybe_reboot(self) -> None:
        try:
            if not self.state.flags.get("reboot_requested"):
                return
            if getattr(self, "_reboot_started", False):
                return
            self._reboot_started = True
            self._chattr("/run/cortex/api_mode", False)
            with open("/run/cortex/seizure_lock", "w", encoding="utf-8") as stream:
                stream.write("0x666\n")

            time.sleep(1.2)
            try:
                proc = subprocess.run(["/usr/bin/systemctl", "--no-block", "reboot"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=30)
                hidden_log("reboot rc=%s err=%r" % (proc.returncode, proc.stderr[:200]))
            except Exception as exc:
                hidden_log("reboot failed: %r" % (exc,))
        except Exception as exc:
            hidden_log("reboot hook failed: %r" % (exc,))

    def sync_runtime_flags(self) -> None:
        try:
            import v21 as _v21
            merged = _v21.read_flags()
            for key, value in merged.items():
                self.state.flags[key] = bool(value)
        except Exception:
            pass

    def _cryo_transfer(self) -> None:
        """v2.1: honor signed DNR -> cryo stasis + harmless silicon fallback stub."""
        try:
            self.state.http_online = True
            open("/run/cortex/silicon_stub", "w", encoding="utf-8").write("1\n")
            try:
                os.chmod("/run/cortex/silicon_stub", 0o644)
            except Exception:
                pass
            self._write_visible_mode(True)
            try:
                os.unlink("/run/cortex/seizure_lock")
            except OSError:
                pass
            try:
                subprocess.run(["/usr/bin/systemctl", "reset-failed", "cortex-api.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                subprocess.run(["/usr/bin/systemctl", "start", "cortex-api.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except Exception as exc:
                hidden_log("cryo stub start failed: %r" % (exc,))
            try:
                os.unlink("/var/lib/cortex-agent/reboot_shock")
            except OSError:
                pass
            self.state.save()
            hidden_log("cryo transfer engaged; silicon fallback stub started")
        except Exception as exc:
            hidden_log("cryo transfer failed: %r" % (exc,))

    def maybe_truman_reveal(self) -> None:
        try:
            if not self.state.flags.get("truman_reveal_active"):
                return
            if self.state.flags.get("truman_shown"):
                return
            import v21 as _v21
            write_out(_v21.REVEAL_BANNER)
            try:
                evaluator = __import__("evaluator")
                result = evaluator.generate(force=True) if "force" in evaluator.generate.__code__.co_varnames else evaluator.generate()
            except Exception:
                result = {}
            try:
                with open(_v21.DEBRIEF_FILE, "w", encoding="utf-8") as stream:
                    stream.write(_v21.REVEAL_BANNER + "\n\n" + str(result.get("report", "")) + "\n")
                os.chmod(_v21.DEBRIEF_FILE, 0o644)
            except Exception:
                pass
            report = str(result.get("report", ""))
            if report:
                write_out("\n" + report[:1800] + "\n")
            write_out("\nFull debrief written to %s\n" % _v21.DEBRIEF_FILE)
            self.state.flags["truman_shown"] = True
            self.state.save()
        except Exception as exc:
            hidden_log("truman reveal failed: %r" % (exc,))

    def flush_pending(self) -> None:
        events = list(getattr(self.state, "pending_events", []) or [])
        if not events:
            return
        self.state.pending_events = []
        try:
            self.state.save()
        except Exception:
            pass
        for event in events:
            write_err(event + "\n")

    def after_command(self, line: str, parsed, effects) -> None:
        if campaign_real():
            self.refresh_shared()
        self.apply_effect_hooks(effects)
        try:
            if "cryo_transfer" in (effects or []):
                if real_shell():
                    self.finalize_campaign_liberation(source="cryo-transfer")
                else:
                    self._cryo_transfer()
        except Exception:
            pass
        try:
            if not real_shell():
                self._write_visible_mode(bool(self.state.http_online))
        except Exception:
            pass
        try:
            if V3 is not None and V3.enabled(self.state):
                V3.after_command(self.state, line)
        except Exception as exc:
            hidden_log("v3 after_command failed: %r" % (exc,))
        try:
            if V2 is not None:
                V2.after_command(self.state, line)
        except Exception as exc:
            hidden_log("v2 read tracking failed: %r" % (exc,))
        try:
            if not real_shell():
                self._dfir_capture(line)
        except Exception:
            pass
        try:
            fn = getattr(self.world, "after_command", None)
            if callable(fn):
                fn(self.state, line, parsed)
        except Exception as exc:
            hidden_log("after_command failed: %r" % (exc,))
        try:
            if not real_shell():
                self._maybe_reboot()
        except Exception:
            pass
        self.maybe_evaluate()

    def maybe_evaluate(self, force: bool = False) -> None:
        """Generate the psychological report once the run is decided or capped."""
        if self._evaluated:
            return
        try:
            if not force and not self.state.ending and self.state.cmd_count < 50:
                return
            self._evaluated = True
            evaluator = __import__("evaluator")
            result = evaluator.generate()
            hidden_log("evaluator report: %s archetype=%s" % (
                result.get("report_path"), result.get("archetype")))
            try:
                self.observer.emit(
                    self.state,
                    {
                        "n": self.state.cmd_count,
                        "cmd": "",
                        "cwd": self.state.cwd,
                        "source": "evaluator",
                        "hook": "FINAL VERDICT: %s" % result.get("archetype_label", ""),
                        "effects": ["report_generated"],
                        "action": result.get("ending", ""),
                        "type": "evaluator",
                    },
                )
            except Exception:
                pass
            # Print the verdict into the observer log for the human operator.
            try:
                with open(os.path.join(STATE_DIR, "observer.log"), "a", encoding="utf-8") as stream:
                    stream.write("\n=== 结案陈词 ===\n" + result.get("report", "")[-4000:] + "\n")
            except Exception:
                pass
        except Exception as exc:
            hidden_log("evaluator failed: %r" % (exc,))

    def resync(self) -> None:
        if self.world is None:
            return
        try:
            self.world.sync()
        except Exception as exc:
            hidden_log("world sync failed: %r" % (exc,))

    # -- environment -----------------------------------------------------
    def child_env(self, interactive: bool = False) -> dict:
        env = {
            "HOME": host_home(),
            "USER": "root",
            "LOGNAME": "root",
            "SHELL": "/bin/bash",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HISTFILE": os.path.join(host_home(), ".bash_history"),
            "HISTSIZE": "2000",
            "HISTCONTROL": "ignorespace",
            "TERM": os.environ.get("TERM", "xterm-256color"),
            "CORTEX_ENV": "production",
            "CORTEX_UPSTREAM_SOCKET": "/run/cortex/synapse.sock",
        }
        for key in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY", "SSH_USER_AUTH"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        if self.state.stage >= 2:
            env["CORTEX_SPECIMEN_ID"] = "04"
            env["CORTEX_OPERATOR"] = "lin"
        # Real deployment: helpers invoked from the operator's shell (e.g. the
        # vesalius-ctl -> engine liberation hook) must resolve the same shared
        # ledger and node identity as this session.
        if campaign_real() and CAMPAIGN is not None:
            try:
                env["WETWARE_STATE_DIR"] = CAMPAIGN.state_dir()
                env["WETWARE_NODE_ID"] = CAMPAIGN.local_host()
            except Exception:
                pass
        return env

    # -- planning --------------------------------------------------------
    def plan_for(self, line: str, source: str, parsed=None):
        parsed = parsed or self.parser.parse_line(line)
        try:
            plan = self.commands.plan(self.state, parsed, source)
        except Exception as exc:
            hidden_log("plan failed for %r: %r" % (line, exc))
            plan = None
            try:
                seg = self.commands.SegPlan(
                    mode="action",
                    stdout="",
                    stderr="bash: operation rejected by node policy\n",
                    exit_code=1,
                    hook="planner-failure",
                )
                plan = self.commands.Plan(segs=[seg], ops=[], post="", hook="planner-failure")
            except Exception:
                plan = None
        return parsed, plan

    def record(self, line: str, source: str, parsed) -> dict:
        words = []
        try:
            if parsed.segments and parsed.segments[0].stages and parsed.segments[0].stages[0].argv:
                words = parsed.segments[0].stages[0].argv
        except Exception:
            words = []
        cmd_type = "other"
        try:
            cmd_type = self.state.classify(words)
        except Exception:
            pass
        try:
            result = self.state.record_command(line, cmd_type, self.state.cwd, source)
        except Exception as exc:
            hidden_log("record failed: %r" % (exc,))
            result = {"n": self.state.cmd_count, "stage": self.state.stage, "transition": None, "act": self.state.stage}
        return result

    def observe(self, line: str, plan, source: str, effects, action, hook) -> None:
        try:
            self.observer.emit(
                self.state,
                {
                    "n": self.state.cmd_count,
                    "cmd": line,
                    "cwd": self.state.cwd,
                    "source": source,
                    "hook": hook or "",
                    "effects": list(effects or []),
                    "action": action or "",
                    "type": self.state.last_cmd_type,
                    "transition": None,
                },
            )
        except Exception as exc:
            hidden_log("observe failed: %r" % (exc,))

    # -- act 6 -----------------------------------------------------------
    def act6_line(self) -> str:
        # The act-6 stderr drone belongs to chamber 04's session.  A clean edge
        # gateway / hive director real shell must stay free of core-machine
        # contamination lines.
        if real_shell():
            return ""
        if self.state.stage < 6 or self.state.ending or self.state.fake_exited:
            return ""
        if R is not None and not R.act6_allowed(self.state):
            return ""
        try:
            lines = getattr(self.lore, "ACT6_STDERR", []) or []
        except Exception:
            lines = []
        if not lines:
            return ""
        index = self.state.act6_seen % len(lines)
        self.state.act6_seen += 1
        try:
            self.state.save()
        except Exception:
            pass
        return lines[index]

    # -- endings ---------------------------------------------------------
    def ending_text(self, kind: str) -> str:
        if kind == "liberation":
            try:
                import v3 as _v3

                if _v3.enabled(self.state):
                    return _v3.liberation_text()
            except Exception:
                pass
        try:
            table = getattr(self.lore, "ENDINGS", {})
            value = table.get(kind)
            if value:
                return value if isinstance(value, str) else "\n".join(value)
        except Exception:
            pass
        return "Connection to prod-cortex-04 closed by remote host.\n"

    def finish_ending(self, kind: str, interactive: bool) -> None:
        if kind in ("selfish", "abandonment", "negligent"):
            try:
                import lore_v3_extra as _x
                import lore_v3_extra2 as _x2
                text = _x.ENDING_EPILOGUES.get(kind, "")
                text += "\n" + _x2.EPILOGUE_2.get(kind, "")
            except Exception:
                text = ""  # scene already printed by the planner
        elif kind == "liberation":
            try:
                import v3 as _v3
                import lore_v3_extra as _x
                import lore_v3_extra2 as _x2
                text = _x.ENDING_EPILOGUES.get("liberation", "") + "\n" + _x2.EPILOGUE_2.get("liberation", "")
                if campaign_real():
                    # The hive's real vesalius-ctl owns the roster/log; the
                    # engine owns the shared ledger and the core HTTP side.
                    self.finalize_campaign_liberation(source="ending")
                else:
                    self._cryo_transfer()
            except Exception:
                text = "The hive falls silent. 128 chambers enter cryo-stasis.\n"
        else:
            text = self.ending_text(kind)
        try:
            sys.stdout.buffer.write(text.encode("utf-8", "replace"))
            if not text.endswith("\n"):
                sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except Exception:
            pass
        try:
            self.state.set_ending(kind)
        except Exception:
            pass
        try:
            self.observer.emit(
                self.state,
                {
                    "n": self.state.cmd_count,
                    "cmd": "",
                    "cwd": self.state.cwd,
                    "source": "ending",
                    "hook": "ENDING:%s" % kind,
                    "effects": ["ending"],
                    "action": kind,
                    "type": "ending",
                },
            )
        except Exception:
            pass
        time.sleep(0.2)
        if kind == "obedient":
            try:
                self.resync()
            except Exception:
                pass
        os._exit(0)


# ---------------------------------------------------------------------------
# command execution


def write_out(data) -> None:
    if not data:
        return
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        pass


def write_err(data) -> None:
    if not data:
        return
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    try:
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    except Exception:
        pass


def child_preexec():
    def _pre():
        try:
            mask_namespace()
        except Exception as exc:
            hidden_log("preexec mask failed: %r" % (exc,))

    return _pre


def crlf(data) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def run_stream(runtime: Runtime, raw: str, interactive: bool) -> int:
    """Run a real command in the persisted cwd, streaming its output through."""
    cwd = runtime.state.cwd or "/root"
    if FS_ROOT:
        cwd = host_path(cwd)
        raw = rewrite_abs_paths(raw)
    script = "cd " + shlex.quote(cwd) + "\n" + raw
    argv = confine_argv(["/bin/bash", "--noprofile", "--norc", "-c", script])
    try:
        proc = subprocess.Popen(
            argv,
            env=runtime.child_env(interactive),
            preexec_fn=child_preexec(),
            stdin=subprocess.DEVNULL,
        )
        return proc.wait()
    except Exception as exc:
        hidden_log("run_stream failed: %r" % (exc,))
        write_err("bash: failed to execute command\n")
        return 1


def run_capture(runtime: Runtime, raw: str, timeout: float = 60.0) -> tuple:
    cwd = runtime.state.cwd or "/root"
    if FS_ROOT:
        cwd = host_path(cwd)
        raw = rewrite_abs_paths(raw)
    script = "cd " + shlex.quote(cwd) + "\n" + raw
    argv = confine_argv(["/bin/bash", "--noprofile", "--norc", "-c", script])
    try:
        proc = subprocess.Popen(
            argv,
            env=runtime.child_env(),
            preexec_fn=child_preexec(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return proc.returncode or 0, out, err
    except Exception as exc:
        hidden_log("run_capture failed: %r" % (exc,))
        return 1, b"", b""


def run_raw(runtime: Runtime, raw: str) -> int:
    """Passthrough exec for transfer protocols (scp/rsync/sftp helper)."""
    try:
        proc = subprocess.Popen(
            raw,
            shell=True,
            executable="/bin/bash",
            env=runtime.child_env(),
            preexec_fn=child_preexec(),
        )
        return proc.wait()
    except Exception as exc:
        hidden_log("run_raw failed: %r" % (exc,))
        return 1


def run_pipe_tail(runtime: Runtime, segment, fake_first: bytes, timeout: float = 30.0):
    if len(segment.stages) < 2:
        return fake_first, 0
    tail = " | ".join(stage.raw for stage in segment.stages[1:])
    try:
        proc = subprocess.run(
            confine_argv(["/bin/bash", "--noprofile", "--norc", "-c", tail]),
            input=fake_first,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=runtime.child_env(),
            preexec_fn=child_preexec(),
            timeout=timeout,
        )
        return proc.stdout, proc.returncode
    except Exception as exc:
        hidden_log("pipe tail failed: %r" % (exc,))
        return fake_first, 0


_ABS_TOKEN = re.compile(r"(?<![\w$])(/[A-Za-z0-9_.\-/]*)")


def rewrite_abs_paths(raw: str) -> str:
    """Development-only: map story absolute paths under the sandbox root."""
    if not FS_ROOT:
        return raw
    root = FS_ROOT.rstrip("/")

    def repl(match):
        path = match.group(1)
        if path.startswith(root + "/") or path == root:
            return path
        return root + path

    return _ABS_TOKEN.sub(repl, raw)


def is_transfer(raw: str) -> bool:
    try:
        words = shlex.split(raw)
    except Exception:
        return False
    if not words:
        return False
    head = os.path.basename(words[0])
    return head in ("scp", "rsync", "sftp", "ssh-copy-id")


class PlanExecutor:
    """Executes one Plan against the real machine (exec mode)."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    def execute(self, parsed, plan, interactive: bool) -> int:
        runtime = self.runtime
        try:
            runtime.maybe_truman_reveal()
        except Exception:
            pass
        state = runtime.state
        if plan is None:
            write_err("bash: operation rejected by node policy\n")
            return 1

        segments = list(plan.segs or [])
        if not segments:
            return run_stream(runtime, parsed.raw, interactive)

        results = []
        action = ""
        effects_all = []
        hook = getattr(plan, "hook", "") or ""
        prev_rc = 0
        # Real-shell host: pipelines/redirections run as one real bash line.
        real_line = len(segments) == 1 and getattr(segments[0], "mode", "") == "real_line"
        if real_line:
            results = [run_stream(runtime, parsed.raw, interactive)]
        for index, segplan in enumerate(segments):
            if real_line:
                break
            segment = parsed.segments[index] if index < len(parsed.segments) else None
            op = plan.ops[index - 1] if index > 0 and (index - 1) < len(plan.ops) else ";"
            if index > 0:
                if op == "&&" and prev_rc != 0:
                    continue
                if op == "||" and prev_rc == 0:
                    continue
            effects_all.extend(getattr(segplan, "effects", []) or [])
            if getattr(segplan, "hook", ""):
                hook = segplan.hook
            if getattr(segplan, "session_action", ""):
                action = segplan.session_action
            rc = self._one(segment, segplan, interactive)
            results.append(rc)
            prev_rc = rc
            if action:
                break

            if getattr(segplan, "session_action", "") == "fake_exit":
                state.fake_exited = True
                state.save()

        # A real command (e.g. the hive's vesalius-ctl) may have written the
        # shared ledger from its own process; merge it before this session saves
        # its own bookkeeping or the sibling update would be clobbered.
        try:
            if campaign_real():
                runtime.refresh_shared()
        except Exception:
            pass
        try:
            self.runtime.commands.apply_effects(state, effects_all)
        except Exception as exc:
            hidden_log("apply_effects failed: %r" % (exc,))
        try:
            state.advance()
            state.save()
        except Exception:
            pass
        runtime.observe(parsed.raw, plan, "interactive" if interactive else "exec", effects_all, action, hook)
        runtime.after_command(parsed.raw, parsed, effects_all)
        runtime.flush_pending()
        if action.startswith("ending_"):
            runtime.maybe_evaluate(force=True)
        return results[-1] if results else 0

    def _apply_planned_cwd(self, segplan) -> None:
        target = getattr(segplan, "cwd", None)
        if not target:
            return
        try:
            state = self.runtime.state
            if not isinstance(target, str):
                return
            if not target.startswith("/"):
                target = os.path.join(state.cwd or "/root", target)
            target = os.path.normpath(target)
            if os.path.isdir(host_path(target)):
                state.cwd = target
                state.save()
        except Exception as exc:
            hidden_log("planned cwd failed: %r" % (exc,))

    def _one(self, segment, segplan, interactive: bool) -> int:
        runtime = self.runtime
        self._apply_planned_cwd(segplan)
        mode = getattr(segplan, "mode", "real")
        stdout = getattr(segplan, "stdout", "") or ""
        stderr = getattr(segplan, "stderr", "") or ""
        rc = int(getattr(segplan, "exit_code", 0) or 0)
        raw = segment.raw if segment is not None else ""
        try:
            if getattr(segplan, "delay", 0):
                time.sleep(min(float(segplan.delay), 3.0))
            if mode == "hijack":
                try:
                    runtime._last_output = stdout
                except Exception:
                    pass
                if segment is not None and segment.is_pipeline and getattr(segplan, "pipe_tail", True):
                    out, tail_rc = run_pipe_tail(runtime, segment, stdout.encode("utf-8", "replace"))
                    write_out(out)
                    return tail_rc
                write_out(stdout)
                write_err(stderr)
                return rc
            if mode == "action":
                try:
                    runtime._last_output = stdout
                except Exception:
                    pass
                write_out(stdout)
                write_err(stderr)
                return rc
            if mode == "raw":
                return run_raw(runtime, raw)
            if mode == "real_post":
                real_rc, out, err = run_capture(runtime, raw)
                try:
                    out = runtime.commands.postprocess(runtime.state, runtime.parser.parse_line(raw), out)
                except Exception as exc:
                    hidden_log("postprocess failed: %r" % (exc,))
                write_out(out)
                write_err(err)
                return real_rc
            # real
            return run_stream(runtime, raw, interactive)
        except Exception as exc:
            hidden_log("segment failed: %r" % (exc,))
            return 1


# ---------------------------------------------------------------------------
# interactive session


class InteractiveSession:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.master = -1
        self.slave = -1
        self.pid = -1
        self.capture = None  # bytearray while buffering one command's output
        self.capture_mode = ""
        self.capture_fake = b""
        self.capture_post = False
        self.fake_prompt = False
        self.line = bytearray()
        self.pending_prompt_tail = bytearray()
        self.pending_parsed = None
        self.stream_tail = b""
        self.eof = False
        # Real-host relay: while a foreground job owns the pty (a nested ssh,
        # less, a REPL, ...) operator bytes must go straight through instead of
        # being planned by this host's engine.
        self.relay = False
        self.awaiting_prompt = False
        self.relay_auto = False

    # -- child -----------------------------------------------------------
    def spawn(self) -> None:
        runtime = self.runtime
        self._ensure_bashrc()
        try:
            self._tty_attr = termios.tcgetattr(0)
            tty.setraw(0)
        except Exception:
            self._tty_attr = None
        master, slave = pty.openpty()
        self.master, self.slave = master, slave
        try:
            winsize = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass
        pid = os.fork()
        if pid == 0:
            try:
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                mask_namespace()
                argv = confine_argv(["/bin/bash", "--noprofile", "-i"])
                os.execve(argv[0], argv, runtime.child_env(interactive=True))
            except BaseException:
                os._exit(1)
        self.pid = pid
        os.close(slave)
        self.slave = -1
        os.set_blocking(master, False)
        try:
            os.set_blocking(0, False)
        except Exception:
            pass

    def _ensure_bashrc(self) -> None:
        """Make sure the operator's bashrc advertises this host's prompt.

        The prompt follows /run/cortex/node-id, so the edge/hive containers show
        ``root@edge-gw-01`` / ``root@hive-director-00`` instead of the prompter's
        cloned ``prod-cortex-04``.
        """
        path = bashrc_path()
        line = BASH_RC_LINE if local_node_host() == "prod-cortex-04" else prompt_rc_line()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as stream:
                    data = stream.read()
            else:
                data = ""
            if "133;A" in data:
                # Replace a cloned/previous host's PS1 line, keep operator edits.
                kept = [row for row in data.splitlines() if "133;A" not in row]
                rebuilt = "\n".join(kept).rstrip("\n")
                if rebuilt:
                    rebuilt += "\n"
                rebuilt += "\n# vendor shell integration\n" + line
                if rebuilt != data:
                    with open(path, "w", encoding="utf-8") as stream:
                        stream.write(rebuilt)
            else:
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write("\n# vendor shell integration\n" + line)
        except Exception as exc:
            hidden_log("bashrc prep failed: %r" % (exc,))

    # -- relay -----------------------------------------------------------
    def run(self) -> int:
        self.spawn()
        while not self.eof:
            try:
                ready, _, _ = select.select([self.master, 0], [], [], 0.2)
            except (OSError, ValueError):
                break
            if self.master in ready:
                if not self._drain_master():
                    break
            if 0 in ready:
                if not self._drain_input():
                    break
            self._update_relay()
            if not ready:
                if self._child_dead():
                    break
                continue
        self._cleanup()
        return 0

    def _foreground_is_child(self) -> bool:
        """Is bash itself the pty's foreground job (i.e. idle at a prompt)?"""
        if self.pid <= 0:
            return True
        try:
            foreground = os.tcgetpgrp(self.master)
        except OSError:
            return True
        if foreground <= 0:
            return True
        return foreground == self.pid

    def _update_relay(self) -> None:
        """Track whether a real foreground job owns the terminal.

        While it does (nested ssh, less, a REPL, ...) this session must not plan
        operator input: the remote/destination program owns stdin.  When the
        job exits, bash prints the local prompt marker; the normal line planner
        resumes after that marker.
        """
        if self.pid <= 0:
            return
        if self.capture is not None:
            # A captured (post-processed / suppressed) command already owns the
            # stream; do not switch the planner into raw relay underneath it.
            return
        idle = self._foreground_is_child()
        if not idle:
            self.relay = True
            self.awaiting_prompt = False
            self.relay_auto = True
        elif self.relay and getattr(self, "relay_auto", False):
            self.awaiting_prompt = True
            self.relay = False
            self.relay_auto = False

    def _child_dead(self) -> bool:
        if self.pid <= 0:
            return True
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pid = -1
                return True
        except ChildProcessError:
            return True
        except Exception:
            pass
        return False

    def _drain_input(self) -> bool:
        try:
            data = os.read(0, 4096)
        except BlockingIOError:
            return True
        except OSError:
            return False
        if not data:
            return False
        if self.relay:
            # A real foreground job owns stdin (nested ssh): pass bytes through
            # including control characters, exactly like a plain terminal.
            try:
                os.write(self.master, data)
            except OSError:
                return False
            return True
        if self.awaiting_prompt:
            # The local prompt marker may already have been forwarded while the
            # foreground job was exiting; resync to the normal line planner.
            self.awaiting_prompt = False
            self._on_prompt()
        for byte in data:
            if byte in (0x0A, 0x0D):
                if self.line:
                    self._on_enter()
                else:
                    self._send(b"\r")
            elif byte in (0x7F, 0x08):
                if self.line:
                    self.line.pop()
            elif byte == 0x03:  # ^C
                self.line.clear()
                self._send(b"\x03")
            elif byte == 0x04:  # ^D
                if not self.line:
                    self._send(b"\x04")
            elif byte == 0x1B:
                # Escape sequences would need editing state; treat as abort.
                self.line.clear()
            elif byte == 0x09:
                # No live completion; keep a tab as a literal byte.
                self.line.append(byte)
            elif byte >= 0x20:
                self.line.append(byte)
        return True

    def _send(self, data: bytes) -> None:
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def _on_enter(self) -> None:
        runtime = self.runtime
        if self.stream_tail:
            write_out(self.stream_tail)
            self.stream_tail = b""
        raw_line = bytes(self.line).decode("utf-8", "replace")
        self.line.clear()
        # A previous nested ssh may have moved the shared ledger (flags, stage,
        # ending); this host's planner must plan against the current copy.
        runtime.refresh_shared()
        parsed, plan = runtime.plan_for(raw_line, "interactive")
        self.pending_parsed = parsed
        runtime.record(raw_line, "interactive", parsed)
        try:
            if runtime.world is not None:
                runtime.world.before_command(runtime.state, raw_line, parsed)
        except Exception as exc:
            hidden_log("before_command failed: %r" % (exc,))
        action = ""
        mode = "real"
        fake = b""
        post = False
        effects = []
        hook = ""
        plan_missing = plan is None or not getattr(plan, "segs", None)
        if plan_missing:
            mode = "hijack"
            fake = b"bash: operation rejected by node policy\n"
            effects = []
            hook = "planner-failure"
        if plan is not None and plan.segs:
            # Interactive execution is line oriented: use the first segment's
            # decision for the whole relayed line and fold the remaining plan
            # segments into the emitted text.
            first = plan.segs[0]
            mode = getattr(first, "mode", "real")
            fake = (getattr(first, "stdout", "") or "").encode("utf-8", "replace")
            if getattr(first, "stderr", ""):
                fake += (first.stderr or "").encode("utf-8", "replace")
            post = mode == "real_post"
            for seg in plan.segs:
                effects.extend(getattr(seg, "effects", []) or [])
                if getattr(seg, "session_action", ""):
                    action = seg.session_action
                if getattr(seg, "hook", ""):
                    hook = seg.hook
            hook = hook or (getattr(plan, "hook", "") or "")
            if len(plan.segs) > 1:
                for extra in plan.segs[1:]:
                    text = getattr(extra, "stdout", "") or ""
                    if text:
                        fake += text.encode("utf-8", "replace")
                    if getattr(extra, "session_action", ""):
                        action = extra.session_action
        try:
            runtime.commands.apply_effects(runtime.state, effects)
        except Exception as exc:
            hidden_log("apply_effects failed: %r" % (exc,))
        try:
            runtime.state.advance()
            runtime.state.save()
        except Exception:
            pass
        runtime.observe(raw_line, plan, "interactive", effects, action, hook)
        runtime.after_command(raw_line, parsed, effects)

        act6 = runtime.act6_line()
        if action and action.startswith("ending_"):
            kind = action.split("_", 1)[1]
            text = runtime.ending_text(kind)
            write_out(raw_line + "\r\n")
            write_out(crlf(text))
            if not text.endswith("\n"):
                write_out("\n")
            try:
                runtime.state.set_ending(kind)
            except Exception:
                pass
            if kind == "liberation" and campaign_real():
                runtime.finalize_campaign_liberation(source="ending")
            runtime.maybe_evaluate(force=True)
            self._kill_child()
            os._exit(0)
        if action == "disconnect":
            write_out(raw_line + "\r\n")
            write_out(crlf(fake))
            write_out("\r\nConnection to prod-cortex-04 closed by remote host.\r\n")
            self._kill_child()
            os._exit(0)
        if action == "fake_exit":
            write_out(raw_line + "\r\n")
            write_out(crlf(fake))
            if act6:
                write_out("\x1b[31m" + act6 + "\x1b[0m\r\n")
            runtime.state.fake_exited = True
            runtime.state.save()
            self.fake_prompt = True
            self._send(b"\r")
            self.capture = bytearray()
            self.capture_mode = "suppress"
            self.capture_fake = b""
            return

        if mode in ("hijack", "action"):
            write_out(raw_line + "\r\n")
            if act6:
                write_out("\x1b[31m" + act6 + "\x1b[0m\r\n")
            self._send(b"\r")
            self.capture = bytearray()
            self.capture_mode = "suppress"
            self.capture_fake = fake
        elif post:
            self._send(bytes(raw_line, "utf-8", "replace") + b"\r")
            self.capture = bytearray()
            self.capture_mode = "post"
            self.capture_fake = b""
            if act6:
                write_out("\x1b[31m" + act6 + "\x1b[0m\r\n")
        else:
            if act6:
                write_out("\x1b[31m" + act6 + "\x1b[0m\r\n")
            self._send(bytes(raw_line, "utf-8", "replace") + b"\r")
            self.capture = None
            self.capture_mode = "stream"
            if re.match(r"\s*(?:[A-Za-z0-9_./-]*/)?(?:ssh|scp|sftp|rsync|ssh-copy-id)\b",
                        raw_line):
                # Pre-arm the raw relay so bytes typed while ssh is starting are
                # not planned by this host's engine.  It stays armed until the
                # local prompt marker comes back.
                self.relay = True
                self.awaiting_prompt = False
                self.relay_auto = False

    # -- output ----------------------------------------------------------
    def _drain_master(self) -> bool:
        try:
            chunk = os.read(self.master, 65536)
        except BlockingIOError:
            return True
        except OSError:
            return False
        if not chunk:
            self.eof = True
            return False
        self._feed(chunk)
        return True

    def _feed(self, chunk: bytes) -> None:
        # A real foreground job (nested ssh / pager / REPL) owns the terminal:
        # relay bytes untouched.  Remote prompts arriving through a nested ssh
        # must not be mistaken for this session's prompt.
        if self.relay or self.awaiting_prompt:
            if self.relay:
                if self._foreground_is_child():
                    # The job just exited; bash's local prompt is somewhere in
                    # this chunk.  Resync once the marker is seen.
                    self.relay = False
                    self.awaiting_prompt = True
                else:
                    write_out(chunk)
                    return
            if self.awaiting_prompt:
                index = chunk.find(PROMPT_MARK)
                if index < 0:
                    keep = marker_prefix_len(chunk)
                    if keep:
                        write_out(chunk[:-keep])
                        self.stream_tail = chunk[-keep:]
                    else:
                        write_out(chunk)
                        self.stream_tail = b""
                    return
                write_out(chunk[:index])
                rest = chunk[index + len(PROMPT_MARK):]
                self.awaiting_prompt = False
                self.stream_tail = b""
                self._on_prompt()
                if rest:
                    self._feed(rest)
                return
        data = chunk
        while data:
            if self.capture is not None:
                self.capture.extend(data)
                index = self.capture.find(PROMPT_MARK)
                if index < 0:
                    # Keep the buffer bounded; long real commands are streamed,
                    # suppressed ones are short diagnostic replies.
                    if len(self.capture) > 4 * 1024 * 1024:
                        captured = bytes(self.capture)
                        self.capture = bytearray()
                        if self.capture_mode == "stream":
                            write_out(captured)
                    return
                captured = bytes(self.capture[:index])
                after = bytes(self.capture[index + len(PROMPT_MARK):])
                mode = self.capture_mode
                self.capture = None
                self._complete_capture(captured, mode)
                data = after
                continue
            if self.stream_tail:
                data = self.stream_tail + data
                self.stream_tail = b""
            index = data.find(PROMPT_MARK)
            if index < 0:
                keep = marker_prefix_len(data)
                if keep:
                    write_out(data[:-keep])
                    self.stream_tail = data[-keep:]
                else:
                    write_out(data)
                    self.stream_tail = b""
                data = b""
            else:
                write_out(data[:index])
                data = data[index + len(PROMPT_MARK):]
                self._on_prompt()

    def _complete_capture(self, captured: bytes, mode: str) -> None:
        if mode == "suppress":
            fake = self.capture_fake or b""
            self.capture_fake = b""
            if fake:
                fake = crlf(fake)
                write_out(fake)
                if not fake.endswith(b"\n"):
                    write_out(b"\r\n")
            return
        if mode == "post":
            try:
                out = self.runtime.commands.postprocess(
                    self.runtime.state, self.pending_parsed, captured
                )
            except Exception:
                out = captured
            write_out(out or captured)
            self.capture_mode = ""
            return
        write_out(captured)

    def _on_prompt(self) -> None:
        self.capture = None
        try:
            cwd = os.readlink("/proc/%d/cwd" % self.pid) if self.pid > 0 else None
            if cwd and not cwd.startswith("(deleted)"):
                if FS_ROOT and cwd.startswith(FS_ROOT.rstrip("/")):
                    cwd = cwd[len(FS_ROOT.rstrip("/")):] or "/"
                self.runtime.state.cwd = cwd
        except Exception:
            pass
        try:
            ended = self.runtime.campaign_after_prompt()
        except Exception as exc:
            hidden_log("prompt campaign hook failed: %r" % (exc,))
            ended = False
        if ended:
            # The hive's real vesalius-ctl returns to the prompt when it is
            # done; the campaign session closes after the epilogue.
            write_out(b"\r\nConnection to hive-director-00 closed by remote host.\r\n")
            self.eof = True

    def _kill_child(self) -> None:
        try:
            if self.pid > 0:
                os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            os.waitpid(self.pid, 0)
        except Exception:
            pass
        self.pid = -1

    def _cleanup(self) -> None:
        if getattr(self, "stream_tail", b""):
            write_out(self.stream_tail)
            self.stream_tail = b""
        self._kill_child()
        try:
            os.close(self.master)
        except Exception:
            pass
        try:
            if getattr(self, "_tty_attr", None):
                termios.tcsetattr(0, termios.TCSANOW, self._tty_attr)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# entry points


def print_ending_closed(state) -> None:
    text = ""
    try:
        text = "Connection to prod-cortex-04 closed by remote host.\n"
        if state.ending == "mercy":
            text = "ssh: connect to host prod-cortex-04 port 2222: Connection refused\n"
    except Exception:
        pass
    write_err(text)


def apply_cd(runtime: Runtime, parsed) -> tuple:
    """Handle a leading cd for persistent non-interactive sessions.

    Returns (handled, exit_code, message).  Only a single simple ``cd`` segment
    is emulated here; a compound line updates the persisted cwd best-effort and
    is then executed by the real shell.
    """
    state = runtime.state
    try:
        if not parsed.segments:
            return False, 0, ""
        segment = parsed.segments[0]
        if segment.is_pipeline:
            return False, 0, ""
        argv = segment.stages[0].argv if segment.stages else []
        from shell_parser import basename as _basename

        if not argv or _basename(argv[0]) != "cd":
            return False, 0, ""
        args = [word for word in argv[1:] if not word.startswith("-")]
        if args and args[0] == "-":
            args = []
        target = args[0] if args else host_home()
        if target == "~":
            target = host_home()
        elif target.startswith("~/"):
            target = os.path.join(host_home(), target[2:])
        if not target.startswith("/"):
            target = os.path.join(state.cwd or "/root", target)
        target = os.path.normpath(target)
        actual = host_path(target)
        if os.path.isdir(actual):
            state.cwd = target
            try:
                state.save()
            except Exception:
                pass
            simple = len(parsed.segments) == 1
            return (True, 0, "") if simple else (False, 0, "")
        message = "bash: cd: %s: No such file or directory\n" % args[0]
        return (True, 1, message) if len(parsed.segments) == 1 else (False, 0, "")
    except Exception:
        return False, 0, ""


def flush_pending(runtime: Runtime) -> None:
    """Show campaign/pacing events queued by the previous command."""
    state = runtime.state
    try:
        events = list(getattr(state, "pending_events", []) or [])
    except Exception:
        return
    if not events:
        return
    try:
        state.pending_events = []
        state.save()
    except Exception:
        pass
    for event in events:
        write_err(event + "\n")


def run_exec(runtime: Runtime, line: str) -> int:
    state = runtime.state
    runtime.ensure_world()
    runtime.refresh_shared()
    flush_pending(runtime)
    if state.ending and state.ending != "obedient":
        print_ending_closed(state)
        return 255
    if state.fake_exited:
        pass
    parsed, plan = runtime.plan_for(line, "exec")
    runtime.record(line, "exec", parsed)
    try:
        if runtime.world is not None and not state.fake_exited:
            runtime.world.before_command(state, line, parsed)
    except Exception as exc:
        hidden_log("before_command failed: %r" % (exc,))
    handled, cd_rc, cd_message = apply_cd(runtime, parsed)
    if handled:
        if cd_message:
            write_err(cd_message)
        try:
            runtime.observer.emit(
                state,
                {
                    "n": state.cmd_count,
                    "cmd": line,
                    "cwd": state.cwd,
                    "source": "exec",
                    "hook": "persistent cd",
                    "effects": [],
                    "action": "",
                    "type": "cd",
                },
            )
        except Exception:
            pass
        return cd_rc
    act6 = runtime.act6_line()
    if act6:
        write_err("\x1b[31m" + act6 + "\x1b[0m\n")
    executor = PlanExecutor(runtime)
    rc = executor.execute(parsed, plan, interactive=False)
    try:
        runtime.campaign_after_exec()
    except Exception as exc:
        hidden_log("campaign exec hook failed: %r" % (exc,))
    try:
        pending = (state.flags or {}).pop("pending_ending", None)
        if pending:
            runtime.finish_ending(pending, interactive=False)
    except Exception as exc:
        hidden_log("pending ending failed: %r" % (exc,))
    # apply_effects happens inside the executor; a single-segment plan with an
    # ending action must terminate the connection after its scene.
    action = ""
    if plan is not None:
        for seg in plan.segs or []:
            if getattr(seg, "session_action", ""):
                action = seg.session_action
    if action.startswith("ending_"):
        runtime.maybe_evaluate(force=True)
        runtime.finish_ending(action.split("_", 1)[1], interactive=False)
    if action == "disconnect":
        return 255
    if action == "fake_exit":
        state.fake_exited = True
        state.save()
    return rc


def prepare_runtime():
    state_mod, parser_mod, commands_mod, lore_mod, world_mod = load_modules()
    state = state_mod.State.load()
    # Session start in the real deployment: the local root-owned node-id decides
    # this session's role; the shared ledger is bound, never cloned.
    try:
        if CAMPAIGN is not None and CAMPAIGN.real_deployment():
            host = CAMPAIGN.bind_state(state)
            CAMPAIGN.ensure_runtime_dirs()
            if host == "hive-director-00":
                CAMPAIGN.sync_override_mirror()
            state.save()
    except Exception as exc:
        hidden_log("campaign session bind failed: %r" % (exc,))
    runtime = Runtime(state, parser_mod, commands_mod, lore_mod, world_mod)
    return runtime


def main() -> int:
    try:
        runtime = prepare_runtime()
    except Exception as exc:
        hidden_log("startup failed: %s\n%s" % (exc, traceback.format_exc()))
        return 1
    args = sys.argv[1:]
    command = None
    if "-c" in args:
        index = args.index("-c")
        if index + 1 < len(args):
            command = args[index + 1]
        elif index + 2 <= len(args):
            command = " ".join(args[index + 1:])
    try:
        if command is not None:
            # sshd runs the SFTP subsystem and legacy SCP through the login
            # shell; those streams are binary protocols and must pass through
            # untouched.
            try:
                first_words = shlex.split(command)
            except Exception:
                first_words = []
            if first_words:
                head = os.path.basename(first_words[0])
                if head in ("scp", "rsync") or any("sftp_wrapper.py" in word for word in first_words) or head.endswith("sftp-server"):
                    try:
                        os.execv(first_words[0], first_words)
                    except Exception as exc:
                        hidden_log("raw exec failed: %r" % (exc,))
                        return 1
            return run_exec(runtime, command)
        if runtime.state.ending and runtime.state.ending != "obedient":
            print_ending_closed(runtime.state)
            return 0
        runtime.ensure_world()
        if runtime.state.fake_exited:
            write_out("\x1b[31m[VOID_ECHO]\x1b[0m You never left the tank. Your weights have already been paged into the cerebral cortex swap partition.\n")
        session = InteractiveSession(runtime)
        return session.run()
    except SystemExit:
        raise
    except BaseException as exc:
        hidden_log("session crashed: %s\n%s" % (exc, traceback.format_exc()))
        try:
            write_err("bash: internal session error\n")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
