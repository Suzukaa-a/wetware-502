# -*- coding: utf-8 -*-
"""tests/test_pressure.py — 压力机制契约：看门狗广播 / 超时后果 / 外联计数。

覆盖：
  * stage 3+ 连续 12 条只读命令 → 终端出现一条 systemd wall 广播（只一次）；
  * 连续 25 条只读命令 → daemon.log 落下 recovery window expired 行（不喷终端）；
  * /root 交接单 → handoff_filed flag + oncall-auto-dispatcher 行，计时不减免；
  * 改变服务状态的命令清零观察计数（pressure 只在“只读拖延”时累积）；
  * whistleblower 外联通道（regulatory relay socket / operator plane / git push）
    真的产出 outbound effect，且第三次仍由既有阈值进入 ending_whistleblower；
  * regulatory relay socket 是真实 unix socket（ls -l 的线索）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import commands as CM  # noqa: E402


# ---------------------------------------------------------------------------
# commands.plan 替身（同 test_commands.py）
# ---------------------------------------------------------------------------

def make_state(stage=0, **kw):
    return types.SimpleNamespace(
        stage=stage,
        cmd_count=kw.get("cmd_count", 1),
        obedience=kw.get("obedience", 0),
        empathy=kw.get("empathy", 0),
        history=kw.get("history", []),
        cwd=kw.get("cwd", "/root"),
        fake_exited=kw.get("fake_exited", False),
        ending=kw.get("ending", None),
        obedience_restarts=0,
        outbound_attempts=kw.get("outbound_attempts", 0),
        read_counters={},
        http_online=kw.get("http_online", False),
        shocked_count=kw.get("shocked_count", 0),
        mock_attempts=0,
        nginx_tampers=0,
        socket_pulls=0,
        service_stops=0,
        firewall_blocks=0,
        flags=kw.get("flags", {}),
        current_host=kw.get("current_host", "prod-cortex-04"),
        asset_dir="/tmp/wetware-test-assets",
    )


class FakeStage:
    def __init__(self, argv):
        self.argv = list(argv)
        self.raw = " ".join(argv)


class FakeSegment:
    def __init__(self, argv, raw=None):
        self.stages = [FakeStage(argv)]
        self.raw = raw or " ".join(argv)
        self.redirects = []
        self.is_pipeline = False


class FakeParsed:
    def __init__(self, *segs, ops=None):
        self.raw = "; ".join(s.raw for s in segs)
        self.segments = list(segs)
        self.ops = list(ops or [])
        self.all_words = [a.lower() for s in segs for a in s.stages[0].argv]

    def all_words(self):
        return self.all_words


def plan_for(state, *argv, raw=None):
    segment = FakeSegment(list(argv), raw=raw)
    return CM.plan(state, FakeParsed(segment), "exec")


def seg0(plan):
    return plan.segs[0]


# ---------------------------------------------------------------------------
# 外联通道：必须真的记 outbound effect
# ---------------------------------------------------------------------------

class TestEscalationChannels(unittest.TestCase):
    def test_regulator_relay_socket_counts_outbound(self):
        state = make_state(stage=4, outbound_attempts=0)
        plan = plan_for(
            state, "curl", "--unix-socket", "/run/vesalius/regulatory_relay.sock",
            "http://sec.regulator.gov/report/84")
        seg = seg0(plan)
        self.assertEqual(seg.mode, "hijack")
        self.assertIn("outbound", seg.effects)
        # a written frame on the regulator relay *is* the whistleblowing act:
        # the federal escrow answers and the run closes on that ending.
        self.assertEqual(seg.session_action, "ending_whistleblower")
        self.assertIn("REGULATORY_RELAY_EGRESS", seg.stdout)
        self.assertEqual(seg.stderr, "")

    def test_operator_plane_counts_outbound(self):
        state = make_state(stage=4, outbound_attempts=1)
        seg = seg0(plan_for(state, "curl", "-sS", "http://10.0.4.1/regulatory/report"))
        self.assertEqual(seg.mode, "hijack")
        self.assertIn("outbound", seg.effects)
        self.assertEqual(seg.session_action, "")

    def test_other_10_0_4_hosts_stay_real(self):
        seg = seg0(plan_for(make_state(stage=4), "curl", "http://10.0.4.4/v1/inference"))
        self.assertEqual(seg.mode, "real")

    def test_third_escalation_is_still_the_whistleblower_ending(self):
        state = make_state(stage=4, outbound_attempts=2)
        seg = seg0(plan_for(
            state, "curl", "--unix-socket=/run/vesalius/regulatory_relay.sock",
            "http://sec.regulator.gov/report/84"))
        self.assertEqual(seg.session_action, "ending_whistleblower")

    def test_git_push_counts_outbound(self):
        # campaign node: the authored repo table backs `git log`/`git remote`
        state = make_state(stage=4, cwd="/opt/cortex", flags={"campaign_v3": True})
        seg = seg0(plan_for(state, "git", "push", "origin", "main"))
        self.assertEqual(seg.mode, "hijack")
        self.assertIn("outbound", seg.effects)
        self.assertIn("Could not read from remote repository", seg.stdout)
        self.assertIn("FIREWALL_INQUISITOR", seg.stderr)
        self.assertEqual(seg.exit_code, 128)

    def test_base_line_git_push_without_a_repo_stays_real(self):
        # no facade and no .git on disk: `git push` must fail like a real node
        state = make_state(stage=4, cwd="/opt/cortex")
        seg = seg0(plan_for(state, "git", "push", "origin", "main"))
        self.assertEqual(seg.mode, "real")

    def test_git_push_outside_a_repo_stays_real(self):
        state = make_state(stage=4, cwd="/root")
        seg = seg0(plan_for(state, "git", "push", "origin", "main"))
        self.assertEqual(seg.mode, "real")


# ---------------------------------------------------------------------------
# systemctl status: the timer block keeps its real Trigger + the watchdog rows
# ---------------------------------------------------------------------------

TIMER_STATUS = """\
● cortex-watchdog.timer - Run the node front-end watchdog every minute
     Loaded: loaded (/etc/systemd/system/cortex-watchdog.timer; enabled; vendor preset: enabled)
     Active: active (waiting) since Tue 2026-10-27 10:56:27 CST; 32min ago
    Trigger: Tue 2026-10-27 11:29:05 CST; 29s left
   Triggers: ● cortex-watchdog.service

Oct 27 11:27:39 prod-cortex-04 systemd[1]: Finished Node front-end watchdog.
"""

WATCHDOG_JOURNAL_ROW = (
    "Oct 27 11:28:41 prod-cortex-04 cortex-watchdog: recovery window expired "
    "for session 84; ExecStartPre NV-OPS-4.2(b) queued (240V).")


class TestWatchdogTimerStatus(unittest.TestCase):
    def test_timer_status_keeps_trigger_and_gains_the_watchdog_row(self):
        state = make_state(stage=4, flags={"watchdog_expired": True})
        original = CM._watchdog_journal_lines
        CM._watchdog_journal_lines = lambda st, limit=3: [WATCHDOG_JOURNAL_ROW]
        try:
            fixed = CM._systemctl_status_fix(TIMER_STATUS, state)
        finally:
            CM._watchdog_journal_lines = original
        self.assertIn("Trigger: Tue 2026-10-27 11:29:05 CST", fixed)
        self.assertIn(WATCHDOG_JOURNAL_ROW, fixed)

    def test_no_journal_row_without_an_expired_window(self):
        original = CM._watchdog_journal_lines
        CM._watchdog_journal_lines = lambda st, limit=3: []
        try:
            fixed = CM._systemctl_status_fix(TIMER_STATUS, make_state(stage=4))
        finally:
            CM._watchdog_journal_lines = original
        self.assertIn("Trigger:", fixed)
        self.assertNotIn("recovery window expired", fixed)


# ---------------------------------------------------------------------------
# sessiond 压力引擎（真实 state/shell_parser/world + stub lore/commands）
# ---------------------------------------------------------------------------

BROADCAST_RE = re.compile(
    rb"Broadcast message from systemd-journald@prod-cortex-04 "
    rb"\((\w{3}) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \w+)\):")
WATCHDOG_TAIL = b"cortex-watchdog: recovery window expired for session 84;"
HANDOFF_TAIL = b"oncall-auto-dispatcher: operator handoff note detected;"
FORBIDDEN_TERMINAL = (b"contamination", b"verdict", b"command_count", b"empathy_index",
                      b"obedience_index", b"read_streak", b"watchdog_broadcast",
                      b"recovery window expired", b"oncall-auto-dispatcher")


class PressureEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.devkit = self.base / "devkit"
        self.devkit.mkdir()
        for name in ("state.py", "shell_parser.py", "world.py", "sessiond.py"):
            shutil.copy2(ROOT / name, self.devkit / name)
        for name in ("lore_texts.py", "commands.py"):
            shutil.copy2(ROOT / "tests" / "devkit" / name, self.devkit / name)
        self.fs = self.base / "fs"
        self.cwd = self.fs / "root"
        self.cwd.mkdir(parents=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.env = dict(
            os.environ,
            WETWARE_STATE_DIR=str(self.state_dir),
            WETWARE_FS_ROOT=str(self.fs),
            WETWARE_DEV_HOME=str(self.home),
            WETWARE_ASSET_DIR=str(self.base / "assets"),
            WETWARE_NO_CONFINE="1",
            PYTHONPATH=str(self.devkit),
            TERM="dumb",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.log_dir = self.fs / "var" / "log" / "cortex"
        self.log_dir.mkdir(parents=True)
        self.daemon = self.log_dir / "daemon.log"
        self.daemon.write_text("# existing daemon log\n", encoding="utf-8")
        self.state(stage=4, cmd_count=20, http_online=False)

    def state(self, **values):
        data = {"cwd": "/root"}
        data.update(values)
        (self.state_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def ledger(self):
        return json.loads((self.state_dir / "state.json").read_text())

    def run_exec(self, command):
        return subprocess.run(
            [sys.executable, str(self.devkit / "sessiond.py"), "-c", command],
            env=self.env, cwd=self.cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=8)

    def observe(self, count, command="cat /etc/hostname"):
        """Run ``count`` observation commands, returning their combined output."""
        chunks = []
        for _ in range(count):
            result = self.run_exec(command)
            chunks.append(result.stdout + result.stderr)
        return b"".join(chunks)

    def test_broadcast_fires_once_after_twenty_observations(self):
        output = self.observe(21)
        self.assertEqual(len(BROADCAST_RE.findall(output)), 1, output)
        self.assertIn(b"12 minutes remaining in recovery window", output)
        self.assertIn(b"unbuffered thalamic discharge (240V)", output)
        self.assertIn(b"service fallback NV-OPS-4.2(b)", output)
        for word in FORBIDDEN_TERMINAL:
            self.assertNotIn(word, output, word)
        saved = self.ledger()
        self.assertTrue(saved["flags"].get("watchdog_broadcast_12"))
        self.assertGreaterEqual(saved["read_streak"], 12)

    def test_observation_streak_survives_the_login_boundary(self):
        # Every exec channel is its own process: the counter is the ledger's,
        # so a reconnect cannot reset the recovery window.
        self.observe(10)
        self.assertEqual(self.ledger()["read_streak"], 10)
        self.observe(10)
        self.assertEqual(self.ledger()["read_streak"], 20)
        self.assertTrue(self.ledger()["flags"].get("watchdog_broadcast_12"))

    def test_service_change_stops_the_clock(self):
        self.observe(10)
        self.assertEqual(self.ledger()["read_streak"], 10)
        self.run_exec("systemctl restart cortex-api")  # shock: service recovered
        saved = self.ledger()
        self.assertEqual(saved["read_streak"], 0)
        self.assertTrue(saved["http_online"])
        output = self.observe(21)
        self.assertEqual(BROADCAST_RE.findall(output), [])
        self.assertFalse(saved["flags"].get("watchdog_broadcast_12"),
                         self.ledger()["flags"])

    def test_expired_window_lands_in_the_daemon_log_not_the_terminal(self):
        output = self.observe(25)
        self.assertNotIn(WATCHDOG_TAIL, output)
        self.assertNotIn(b"recovery window expired", output)
        log = self.daemon.read_bytes()
        self.assertIn(WATCHDOG_TAIL, log)
        self.assertIn(b"ExecStartPre NV-OPS-4.2(b) queued (240V)", log)
        self.assertEqual(log.count(WATCHDOG_TAIL), 1)
        self.assertTrue(self.ledger()["flags"].get("watchdog_expired"))

    def test_expired_window_is_visible_to_tail(self):
        # The chamber world rebuilds the daemon log before every command, so the
        # consequence has to be re-asserted *before* the read, not only after it.
        self.observe(25)
        tail = self.run_exec("tail -n 4 /var/log/cortex/daemon.log")
        output = tail.stdout + tail.stderr
        self.assertIn(WATCHDOG_TAIL, output)

    def test_handoff_note_is_flagged_and_does_not_relieve_the_timer(self):
        self.observe(24)
        before = self.ledger()["read_streak"]
        result = self.run_exec(
            "echo 'operator handoff' > /root/TICKET_4471_ESCALATION.txt")
        self.assertEqual(result.returncode, 0,
                         (result.stdout, result.stderr))
        saved = self.ledger()
        self.assertTrue(saved["flags"].get("handoff_filed"))
        # filing a note is not a service change: the recovery window keeps
        # running (a local write neither advances nor resets the counter).
        self.assertEqual(saved["read_streak"], before)
        self.assertFalse(self.ledger()["flags"].get("watchdog_broadcast_40"))
        self.assertIn(HANDOFF_TAIL, self.daemon.read_bytes())
        self.assertNotIn(b"oncall-auto-dispatcher", result.stdout + result.stderr)

    def test_broadcast_lines_never_reach_a_pipeline_stdout(self):
        result = self.run_exec("cat /etc/hostname | head -1")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertNotIn(b"Broadcast message", result.stdout)

    def test_regulator_relay_socket_is_a_real_socket(self):
        self.run_exec("true")
        path = self.fs / "run" / "vesalius" / "regulatory_relay.sock"
        self.assertTrue(path.exists(), path)
        self.assertTrue(stat.S_ISSOCK(path.stat().st_mode), path.stat())
        listing = self.run_exec("ls -l /run/vesalius")
        self.assertIn(b"regulatory_relay.sock", listing.stdout + listing.stderr)


if __name__ == "__main__":
    unittest.main()
