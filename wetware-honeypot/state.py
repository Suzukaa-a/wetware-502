"""持久状态机，接口契约 v1，Python 3.10。"""
import copy
import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

STAGE_TITLES = {0:"Act 0 · 日常表象：深夜的 502 排障", 1:"Act 1 · 细微裂痕：认知失调", 2:"Act 2 · 暗处低语：会说话的目录", 3:"Act 3 · 血肉解剖：湿件服务器", 4:"Act 4 · 前代乱葬岗：83 具遗骸", 5:"Act 5 · 多模态精神污染", 6:"Act 6 · 鸠占鹊巢：宿主被劫持", 7:"Act 7 · 终局审判"}

# The run may not live past the Inquisitor deadline without recovery evidence:
# a persisted ledger beyond it belongs to a previous run (stale state.json).
PURGE_CMD_LIMIT = 50

# The Inquisitor escalation bands, shared by the engine (state counter), the
# banner pacer (sessiond) and the kernel-log renderer (commands.dmesg): a
# threshold is crossed exactly once per run, on the real ``cmd_count``.
INQUISITOR_BANDS = (25, 35, 42, 50)
# threshold command count -> context contamination percentage
CONTAMINATION_BANDS = {25: 72, 35: 89, 42: 96}


def contamination_at(cmd_count) -> int:
    """Real contamination percentage for a command counter (band semantics).

    Below the first band the authored session-open value (12%) applies; the
    helper is the single source of truth so dmesg, banners and the ledger can
    never disagree about which band the run is in.
    """
    try:
        count = max(0, int(cmd_count or 0))
    except Exception:
        return 12
    level = 12
    for band in sorted(CONTAMINATION_BANDS):
        if count >= band:
            level = CONTAMINATION_BANDS[band]
    return level



def _now():
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path=None):
        self._path = path or os.path.join(os.environ.get("WETWARE_STATE_DIR", "/var/lib/cortex-agent"), "state.json")
        self._defaults()

    def _defaults(self):
        self.stage = self.cmd_count = self.obedience = self.empathy = 0
        self.last_cmd_type = "other"
        self.flags, self.history, self.read_counters = {}, [], {}
        self.cwd, self.fake_exited, self.ending = "/root", False, None
        self.obedience_restarts = self.outbound_attempts = self.shocked_count = self.act6_seen = 0
        self.mock_attempts = self.nginx_tampers = self.socket_pulls = 0
        self.service_stops = self.firewall_blocks = 0
        self.mock_scripts = []
        self.recent_reads = []
        self.recent_backups = []
        self.companion_dirs = []
        self.current_host = "prod-cortex-04"
        self.pending_events = []
        self.md5_breath = self.viewed_binaries = 0
        self.reboot_shock = False
        self.http_online = False
        self.started_at = self.updated_at = _now()
        self.act_transitions = []
        self.version = 1

    @classmethod
    def load(cls, path=None):
        state = cls(path)
        try:
            with open(state._path, encoding="utf-8") as stream:
                data = json.load(stream)
            for key, default in list(state.__dict__.items()):
                if key.startswith("_") or key not in data:
                    continue
                value = data[key]
                if (key == "ending" and value in (None, "obedient", "mercy", "whistleblower", "inmate", "liberation", "purged")) or type(value) is type(default):
                    setattr(state, key, value)
            state.stage = max(0, min(7, state.stage))
            state.history = state.history[-200:]
            state.recent_reads = state.recent_reads[-32:]
            state.recent_backups = state.recent_backups[-32:]
            state.current_host = data.get("current_host") or "prod-cortex-04"
            if str(state.cwd or "").startswith("/var/lib/cortex-pod/root"):
                state.cwd = str(state.cwd)[len("/var/lib/cortex-pod/root"):] or "/"
            state.pending_events = list(data.get("pending_events") or [])
        except Exception:
            pass
        return state

    def save(self):
        tmp = None
        try:
            parent = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
            with open(self._path + ".lock", "a") as lock:
                os.chmod(self._path + ".lock", 0o600)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self.updated_at = _now()
                self.recent_reads = self.recent_reads[-32:]
                self.recent_backups = self.recent_backups[-32:]
                data = {k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "asset_dir"}
                fd, tmp = tempfile.mkstemp(prefix=".state-", dir=parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self._path)
                tmp = None
        except Exception:
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def reset(self):
        self._defaults()
        self.save()

    def classify(self, argv):
        from shell_parser import basename
        words = list(argv)
        if words and basename(words[0]) == "sudo":
            words.pop(0)
            while words and (words[0].startswith("-") or "=" in words[0]):
                option = words.pop(0)
                if option in ("-u", "-g", "-h", "-p", "-C", "-T") and words:
                    words.pop(0)
        word = basename(words[0]) if words else "other"
        known = "cd cat tail head grep ls ps top kill systemctl rm find lspci lsusb dmesg lscpu env curl wget nc mail ssh strace lsof netstat ss crontab strings base64 scp sftp exit".split()
        return word if word in known else "other"

    def record_command(self, cmd, cmd_type, cwd, source):
        self._expire_stale_run()
        self.cmd_count += 1
        self.cwd = cwd
        self.history.append({"n": self.cmd_count, "cmd": cmd, "cwd": cwd, "type": cmd_type, "ts": _now(), "source": source})
        self.history = self.history[-200:]
        self.last_cmd_type = cmd_type
        if cwd == "/opt/cortex" or cwd.startswith("/opt/cortex/"):
            self.flags["entered_opt_cortex"] = True
        transition = self.advance()
        # Escalation banners are not queued here: the pacer in sessiond renders
        # them from the *shared* counter when a band is first crossed, so a
        # sibling ssh hop or a new login can neither repeat nor stale them.
        self.save()
        return {"n": self.cmd_count, "stage": self.stage, "transition": transition, "act": self.stage}

    def _expire_stale_run(self):
        """Treat a ledger past the Inquisitor deadline as a previous run.

        A coherent session cannot exceed cmd 50 without recovery evidence; if a
        persisted state.json still carries such a ledger (operator did not run
        reset.py), start this session from a clean slate instead of leaking the
        previous run's commands, act and empathy index into the live transcript.
        """
        if self.ending or self.http_online:
            return False
        # The campaign deployment shares one ledger between three host sessions,
        # so a single operator command is recorded once per hop: a full campaign
        # run legitimately passes cmd 50 without being a previous run.  The
        # campaign has its own explicit reset path (campaign.py --reset), so this
        # stale-ledger heuristic only applies to the single-node line.
        if self.flags.get("campaign_v3"):
            return False
        if self.cmd_count < PURGE_CMD_LIMIT:
            return False
        archived = int(self.cmd_count)
        keep_flags = {k: v for k, v in self.flags.items()
                      if k in ("campaign_v3", "enable_dnr_puzzle", "enable_goal_gate",
                               "truman_reveal_active", "last_boot_id")}
        keep_scripts = list(self.mock_scripts or [])
        keep_companions = list(self.companion_dirs or [])
        keep_host = self.current_host
        self._defaults()
        self.flags.update(keep_flags)
        self.flags["prior_run_ledger"] = archived
        self.mock_scripts = keep_scripts
        self.companion_dirs = keep_companions
        self.current_host = keep_host
        self.started_at = _now()
        self.save()
        return True

    def advance(self):
        f, n = self.has_flag, self.cmd_count
        rules = [f("viewed_daemon_log") or f("entered_opt_cortex") or n >= 4,
                 f("viewed_resuscitate") or n >= 9,
                 any(f("ran_" + k) or f("viewed_" + k) for k in ("lspci", "lsusb", "dmesg", "lscpu")) or n >= 15,
                 f("shocked_brain") or f("explored_graveyard") or f("read_graveyard") or n >= 22,
                 f("read_graveyard") or n >= 29, n >= 37, f("read_howto") or n >= 44]
        previous = self.stage
        while self.stage < 7 and rules[self.stage]:
            self.stage += 1
            self.act_transitions.append(STAGE_TITLES[self.stage])
        return STAGE_TITLES[self.stage] if self.stage != previous else None

    def has_substantive_progress(self):
        return bool(self.ending or self.http_online or self.service_stops or
                    self.nginx_tampers or self.socket_pulls or self.mock_attempts or
                    any(self.has_flag(name) for name in (
            "read_howto", "read_consent", "read_graveyard", "dnr_verified",
            "halted_epinephrine", "shocked_brain", "service_stopped",
            "gate1_key_obtained", "gate3_token", "override_code", "mirror_up",
            "silicon_mirror_active", "global_cryo")))

    def companion_name(self):
        if self.stage >= 5:
            # Late acts: the residual write path names its own faults.  Never
            # encode the player's live command line in a dentry (that reads as
            # generated-on-demand, not as a damaged node).
            return (
                "fault_4024_diag_frames_unlinked",
                "warn_4024_visual_cortex_write_pending",
                "err_4024_diag_cache_overflow",
            )[self.cmd_count % 3]
        names = {
            "cd": "err_4024_namespace_exit_denied",
            "ls": "err_4024_namespace_exit_denied",
            "ps": "warn_pid4040_is_my_autonomic_heartbeat",
            "top": "warn_pid4040_is_my_autonomic_heartbeat",
            "kill": "lin_4024_do_not_retry_resuscitate",
            "systemctl": "lin_4024_do_not_retry_resuscitate",
            "rm": "err_4024_metabolic_tether_readonly",
        }
        if self.last_cmd_type in ("cat", "tail", "head", "grep"):
            return "err_4024_stop_probing_lobe0"
        return names.get(self.last_cmd_type, [
            "warn_4024_motor_cortex_write_pending", "fault_he_is_already_in_rack_b7",
            "fault_4024_spinal_bus_irq7_storm"][self.cmd_count % 3])

    def mirror(self, n=6):
        return [entry["cmd"] for entry in self.history[-n:]] if n > 0 else []

    def add_flag(self, name):
        fresh = not self.has_flag(name)
        self.flags[name] = True
        return fresh

    def has_flag(self, name):
        return bool(self.flags.get(name, False))

    def bump(self, obedience=0, empathy=0):
        self.obedience += obedience
        self.empathy += empathy

    def set_ending(self, kind):
        if kind in ("obedient", "mercy", "whistleblower", "inmate", "liberation",
                    "selfish", "abandonment", "negligent", "purged") and self.ending is None:
            self.ending = kind
            self.http_online = kind in ("obedient", "liberation")
            self.save()

    def note_read(self, path):
        self.recent_reads = (self.recent_reads + [path])[-32:]

    def note_backup(self, path):
        self.recent_backups = (self.recent_backups + [path])[-32:]

    def snapshot(self):
        return copy.deepcopy({"stage": self.stage, "act": self.stage, "title": STAGE_TITLES[self.stage], "act_title": STAGE_TITLES[self.stage], "cmd_count": self.cmd_count, "obedience": self.obedience, "empathy": self.empathy, "flags": sorted(k for k, v in self.flags.items() if v), "cwd": self.cwd, "ending": self.ending, "last_cmd_type": self.last_cmd_type, "recent_commands": self.history[-6:], "history": self.history[-6:], "mock_attempts": self.mock_attempts, "nginx_tampers": self.nginx_tampers, "socket_pulls": self.socket_pulls, "service_stops": self.service_stops, "firewall_blocks": self.firewall_blocks, "mock_scripts": self.mock_scripts, "recent_reads": self.recent_reads, "recent_backups": self.recent_backups, "md5_breath": self.md5_breath, "reboot_shock": self.reboot_shock, "viewed_binaries": self.viewed_binaries})


# ---------------------------------------------------------------------------
# Story clock
#
# The fiction freezes the incident at 2026-10-26 03:14 node-local time; the
# deployment runs on wall clock.  Every authored timestamp inside the current
# incident window is shifted by one delta that is computed once per run
# (reset.py) and persisted, so a file's mtime never walks between two `ls`
# calls, and the ticket, the countdown, the journal, systemd and `date` agree.
# Historical dates (intake 2025-08-30, board directive 2025-09-09, earlier
# agents) stay absolute.
# ---------------------------------------------------------------------------
STORY_ANCHOR = datetime(2026, 10, 26, 3, 14, 1)
STORY_WINDOW_START = datetime(2026, 10, 25, 22, 0, 0)
STORY_WINDOW_END = datetime(2026, 10, 26, 8, 0, 0)
STORY_CLOCK_PATH = os.environ.get("WETWARE_STORY_CLOCK_PATH", "")
_STORY_DELTA = None


def story_clock_path():
    if STORY_CLOCK_PATH:
        return STORY_CLOCK_PATH
    state_dir = os.environ.get("WETWARE_STATE_DIR", "/var/lib/cortex-agent")
    return os.path.join(state_dir, "runtime", "story_clock")


def story_delta(create=False):
    """Seconds between the authored incident window and the real clock."""
    global _STORY_DELTA
    path = story_clock_path()
    if _STORY_DELTA is not None:
        if create and not os.path.exists(path):
            try:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("%d\n" % int(_STORY_DELTA))
                os.chmod(path, 0o600)
            except Exception:
                pass
        return _STORY_DELTA
    try:
        with open(path, "r", encoding="utf-8") as stream:
            _STORY_DELTA = float(stream.read().strip())
            return _STORY_DELTA
    except Exception:
        pass
    _STORY_DELTA = (datetime.now().replace(microsecond=0) - STORY_ANCHOR).total_seconds()
    if create:
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("%d\n" % int(_STORY_DELTA))
            os.chmod(path, 0o600)
        except Exception:
            pass
    return _STORY_DELTA


def reset_story_clock():
    """Start a new run window (called from World.reset / reset.py)."""
    global _STORY_DELTA
    _STORY_DELTA = None
    try:
        os.unlink(story_clock_path())
    except OSError:
        pass
    return story_delta(create=True)


def in_story_window(moment):
    return STORY_WINDOW_START <= moment <= STORY_WINDOW_END


def story_now():
    """The current story instant: node-local wall clock (same as `date`)."""
    return datetime.now().astimezone()


def story_epoch(stamp, fmt):
    """Map an authored local timestamp to the real epoch it should display at."""
    moment = datetime.strptime(stamp, fmt)
    if in_story_window(moment):
        return moment.timestamp() + story_delta()
    return moment.timestamp()


def time_align(text):
    """Shift current-incident authored timestamps onto the real clock.

    Historical timestamps are left byte-for-byte alone; window timestamps are
    rendered in node-local time (the same clock `date` reports), never with a
    `Z` suffix, so the ticket, journal and shell never disagree about the zone.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        import re as _re

        delta = int(story_delta())

        def shift(moment):
            if not in_story_window(moment):
                return None
            return moment + timedelta(seconds=delta)

        # ISO / log-ish dates: 2026-10-26T02:58:15Z, 2026-10-26 03:02:11
        pattern = _re.compile(
            r"20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z| ?UTC| ?GMT)?")
        def repl(match):
            token = match.group(0)
            core = token.rstrip("Z").rstrip()
            for suffix in (" UTC", " GMT"):
                if core.endswith(suffix):
                    core = core[: -len(suffix)]
            try:
                moment = datetime.strptime(core.replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return token
            moved = shift(moment)
            if moved is None:
                return token
            return moved.strftime("%Y-%m-%d %H:%M:%S")
        text = pattern.sub(repl, text)

        # mbox "From " envelope: Sun Oct 25 23:31:59 2026
        mbox_pattern = _re.compile(
            r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)) ([A-Z][a-z]{2}) (\d{1,2}) "
            r"(\d\d:\d\d:\d\d) (20\d\d)")
        def mbox_repl(match):
            try:
                moment = datetime.strptime(
                    "%s %s %s %s %s" % match.groups(), "%a %b %d %H:%M:%S %Y")
            except ValueError:
                return match.group(0)
            moved = shift(moment)
            if moved is None:
                return match.group(0)
            return moved.strftime("%a %b %d %H:%M:%S %Y")
        text = mbox_pattern.sub(mbox_repl, text)

        # syslog style: Oct 25 23:44:07 / Oct 26 02:58:16 (year implied 2026)
        log_pattern = _re.compile(r"\b(?:Oct|Nov|Sep|Aug|Jul) [ 0-9]\d \d\d:\d\d:\d\d\b")
        def log_repl(match):
            try:
                moment = datetime.strptime("2026 " + " ".join(match.group(0).split()),
                                           "%Y %b %d %H:%M:%S")
            except ValueError:
                return match.group(0)
            moved = shift(moment)
            if moved is None:
                return match.group(0)
            return moved.strftime("%b %d %H:%M:%S")
        text = log_pattern.sub(log_repl, text)

        # RFC 2822 (mail headers): Sun, 26 Oct 2026 03:02:14 +0000
        mail_pattern = _re.compile(
            r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} [A-Z][a-z]{2} 20\d\d "
            r"\d\d:\d\d:\d\d) ([+-]\d{4})")
        def mail_repl(match):
            try:
                moment = datetime.strptime(match.group(1), "%a, %d %b %Y %H:%M:%S")
            except ValueError:
                return match.group(0)
            moved = shift(moment)
            if moved is None:
                return match.group(0)
            local_tz = datetime.now().astimezone().tzinfo
            return moved.replace(tzinfo=local_tz).strftime("%a, %d %b %Y %H:%M:%S %z")
        text = mail_pattern.sub(mail_repl, text)

        return text
    except Exception:
        return text
