# -*- coding: utf-8 -*-
"""Offline tests for the session's kernel view: ``ls`` and ``/proc`` uptime.

Two reported tails are pinned here:

* ``ls -la <story dir>`` printed a bare name list (the campaign artifact
  hijack) with no ``total``/attributes, ignored bundled flags (``-la``/``-al``/
  ``-lah``), and lost every target but the last one (``ls -la /root /var/mail``
  answered one line).  A real container's ``ls`` is authoritative; a virtual
  host's listing comes from the facade, which owns the authored attributes.
* ``cat /proc/uptime`` fell through to the deployment host's real uptime while
  ``uptime``/``top`` showed the authored story clock, so ``uptime`` said "up 2
  days" next to a five-hour number in /proc.  The planner now answers the story
  proc surface from the same computation the ``uptime`` command uses.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands  # noqa: E402
import shell_parser  # noqa: E402
import state as state_mod  # noqa: E402

CORE = "prod-cortex-04"
EDGE = "edge-gw-01"

_UP_RE = re.compile(r"up\s+(?:(\d+)\s+days?,\s*)?(\d+):(\d+)")


class KernelViewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wetware-kernel-view-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.node_id_path = os.path.join(self.tmp, "node-id")
        patcher = mock.patch.dict(os.environ, {
            "WETWARE_STATE_DIR": self.state_dir,
            "WETWARE_NODE_ID_PATH": self.node_id_path,
            "WETWARE_NODE_ID": "",
            "WETWARE_SKIP_PTY": "1",
            "WETWARE_NO_CONFINE": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = state_mod.State(path=os.path.join(self.state_dir, "state.json"))
        self.state.reset()

    def configure(self, campaign_v3, host=None, stage=0, node_id=""):
        try:
            os.unlink(self.node_id_path)
        except OSError:
            pass
        if node_id:
            with open(self.node_id_path, "w", encoding="utf-8") as stream:
                stream.write(node_id + "\n")
        self.state.flags["campaign_v3"] = bool(campaign_v3)
        self.state.current_host = host or CORE
        self.state.stage = stage
        self.state.save()
        return self.state

    def plan(self, line, source="interactive"):
        return commands.plan(self.state, shell_parser.parse_line(line), source)

    def output(self, line, source="interactive"):
        plan = self.plan(line, source)
        return "".join(getattr(seg, "stdout", "") or "" for seg in plan.segs)

    def mode(self, line, source="interactive"):
        plan = self.plan(line, source)
        return getattr(plan.segs[0], "mode", "") if plan.segs else ""


class StoryProcTests(KernelViewBase):
    """/proc/uptime + /proc/loadavg follow the authored clock, not the VM host."""

    def test_proc_uptime_matches_uptime_command(self):
        self.configure(False, stage=0)
        up_text = self.output("uptime")
        proc_text = self.output("cat /proc/uptime")
        self.assertIn("up 2 days", up_text)
        seconds = float(proc_text.split()[0])
        days = int(seconds // 86400)
        minutes = int((seconds % 86400) // 60)
        match = _UP_RE.search(up_text)
        self.assertIsNotNone(match, up_text)
        self.assertEqual(int(match.group(1) or 0), days, up_text)
        self.assertEqual(int(match.group(2)) * 60 + int(match.group(3)), minutes, up_text)
        load = self.output("cat /proc/loadavg")
        self.assertTrue(load.startswith("0.44 0.51 0.58"), load)
        self.assertRegex(load, r"^\d+\.\d+ \d+\.\d+ \d+\.\d+ \d+/\d+ \d+\n$")

    def test_story_values_advance_with_the_story_clock(self):
        self.configure(False, stage=0)
        first = float(self.output("cat /proc/uptime").split()[0])
        self.assertGreater(first, 2 * 86400)
        self.assertLess(first, 2 * 86400 + 3600)

    def test_base_line_chamber_answers_proc_from_the_story(self):
        # The reported case: base line on the real core container.  ps/top are
        # authored there, so /proc must be authored too (it used to be the
        # deployment host's kernel uptime).
        self.configure(False, host=CORE, stage=0, node_id=CORE)
        self.assertEqual(self.mode("cat /proc/uptime"), "hijack")
        self.assertEqual(self.mode("uptime"), "hijack")
        self.assertIn("up 2 days", self.output("uptime"))

    def test_campaign_node_keeps_the_kernel_proc(self):
        # On the campaign line ps/top/uptime are real: /proc/uptime must stay
        # the kernel's own answer or it would contradict them.
        self.configure(True, host=CORE, stage=0, node_id=CORE)
        self.assertEqual(self.mode("cat /proc/uptime"), "real")
        self.assertEqual(self.mode("cat /proc/loadavg"), "real")
        self.assertEqual(self.mode("uptime"), "real")

    def test_login_view_uses_the_same_story_uptime(self):
        self.configure(False, stage=0)
        text = self.output("w")
        self.assertIn("up 2 days", text)
        self.assertIn("0.44, 0.51, 0.58", text)

    def test_top_header_agrees_with_the_story_uptime(self):
        # act 2+: the authored top frame must not keep a frozen "up 2 days"
        # while /proc/uptime reports the story boot time.
        self.configure(False, stage=3)
        up_text = self.output("uptime")
        top_text = self.output("top -bn1")
        proc_days = int(float(self.output("cat /proc/uptime").split()[0]) // 86400)
        up_match = re.search(r"up\s+(\d+)\s+days", up_text)
        self.assertIsNotNone(up_match, up_text)
        self.assertEqual(int(up_match.group(1)), proc_days, up_text)
        self.assertIn("up %d days" % proc_days, top_text)
        self.assertNotIn("up 2 days", top_text)
        self.assertRegex(top_text, r"^top - \d\d:\d\d:\d\d")


class LsPlannerTests(KernelViewBase):
    """``ls`` either runs for real or is rendered with full GNU semantics."""

    def test_story_directory_is_not_hijacked_to_a_name_list(self):
        # The campaign artifact layer used to answer ``ls`` with bare children
        # names (no total/owner/mtime) and only the last target.  The real
        # container's ``ls`` owns this line again.
        self.configure(True, host=CORE, stage=4)
        plan = self.plan("ls -la /var/log/agent_graveyard")
        self.assertEqual(getattr(plan.segs[0], "mode", ""), "real")
        self.assertEqual(plan.segs[0].stdout, "")
        self.assertEqual(self.mode("ls -la /root /var/mail"), "real")

    def test_virtual_host_listing_has_attributes_and_total(self):
        self.configure(True, host=EDGE, stage=0)
        text = self.output("ls -la /var/log")
        self.assertIn("total ", text)
        self.assertRegex(text, r"drwxr-xr-x\s+\d+\s+root\s+root")
        self.assertIn("auth.log", text)

    def test_bundled_flags_are_parsed(self):
        self.configure(True, host=EDGE, stage=0)
        for line in ("ls -la /var/log", "ls -al /var/log", "ls -lah /var/log"):
            text = self.output(line)
            self.assertIn("total ", text, line)
            self.assertIn("drwx", text, line)

    def test_multiple_targets_get_headers(self):
        self.configure(True, host=EDGE, stage=0)
        text = self.output("ls -la /var/log /root")
        self.assertIn("/var/log:", text)
        self.assertIn("/root:", text)
        self.assertIn(".bash_history", text)

    def test_directory_flag_lists_the_names(self):
        self.configure(True, host=EDGE, stage=0)
        self.assertEqual(self.output("ls -d /var/log"), "/var/log\n")
        text = self.output("ls -d /var/log /root /etc")
        self.assertEqual(text, "/var/log\n/root\n/etc\n")

    def test_color_option_is_accepted(self):
        self.configure(True, host=EDGE, stage=0)
        text = self.output("ls --color=auto /root")
        self.assertIn(".bash_history", text)
        self.assertIn("total ", text)

    def test_real_tool_handles_unknown_paths(self):
        # Not a story dir: never answered from the facade, the real ls reports
        # the error exactly like GNU ls does.
        self.configure(True, host=CORE, stage=0)
        self.assertEqual(self.mode("ls -la /var/log/does-not-exist"), "real")


class DaemonIdentityTests(KernelViewBase):
    """6660/8484 must read exactly like the two real ELFs the image installs.

    ``comm`` is the kernel's 15-byte cut of the binary basename and ``argv`` is
    the systemd unit's ExecStart tail; the old table showed
    ``/proc/self/assimilate_llm_weights`` and a made-up ``assimilate`` comm.
    """

    def setUp(self):
        super().setUp()
        self.configure(False, stage=3)

    def test_proc_comm_is_the_kernel_truncation(self):
        self.assertEqual(self.output("cat /proc/8484/comm"), "assimilate_llm_\n")
        self.assertEqual(self.output("cat /proc/6660/comm"), "inquisitor_daem\n")
        self.assertLessEqual(len("assimilate_llm_"), 15)
        self.assertLessEqual(len("inquisitor_daem"), 15)

    def test_proc_cmdline_carries_the_installed_argv(self):
        raw = self.output("cat /proc/8484/cmdline")
        self.assertIn("/opt/cortex/bin/assimilate_llm_weights", raw)
        self.assertNotIn("/proc/self/assimilate_llm_weights", raw)
        self.assertIn("--progress=64%", raw)
        raw = self.output("cat /proc/6660/cmdline")
        self.assertIn("/usr/sbin/inquisitor_daemon", raw)
        self.assertIn("--watch-agent=84", raw)

    def test_ps_shows_the_installed_argv(self):
        text = self.output("ps aux")
        self.assertNotIn("/proc/self/assimilate_llm_weights", text)
        self.assertIn("/opt/cortex/bin/assimilate_llm_weights --progress=64%", text)
        self.assertIn("/usr/sbin/inquisitor_daemon --watch-agent=84", text)

    def test_top_command_column_is_comm(self):
        text = self.output("top -bn1")
        self.assertIn("assimilate_llm_", text)
        self.assertNotIn("assimilate_llm_weights", text)
        self.assertIn("inquisitor_daem", text)

    def test_pgrep_and_pstree_agree(self):
        self.assertIn("/opt/cortex/bin/assimilate_llm_weights",
                      self.output("pgrep -a 8484"))
        self.assertIn("inquisitor_daem(6660)", self.output("pstree -p"))


class LoginRecordTests(KernelViewBase):
    """who/last speak only of accounts this image has, and one boot clock."""

    def setUp(self):
        super().setUp()
        self.configure(False, stage=3)

    def test_last_names_only_existing_accounts(self):
        text = self.output("last")
        self.assertNotIn("ubuntu", text)
        self.assertIn("root", text)
        self.assertIn("pts/0", text)
        self.assertIn("still logged in", text)
        users = set()
        for line in text.splitlines():
            if not line.strip() or line.startswith("wtmp begins"):
                continue
            users.add(line.split()[0])
        self.assertTrue(users <= {"root", "reboot", "shutdown"}, users)

    def _expected_boot(self):
        """Boot instant as /proc/uptime claims it (a second of slack)."""
        seconds = float(self.output("cat /proc/uptime").split()[0])
        return _dt.datetime.now() - _dt.timedelta(seconds=seconds)

    @staticmethod
    def _last_date_fields(line):
        match = re.search(r"\w{3} (\w{3}) +(\d+) (\d\d):(\d\d)", line)
        if match is None:
            return None
        return (match.group(1), int(match.group(2)), match.group(3), match.group(4))

    def test_last_reboot_matches_proc_uptime(self):
        reboot = [line for line in self.output("last reboot").splitlines()
                  if line.startswith("reboot")][0]
        expected = self._expected_boot()
        self.assertEqual(
            self._last_date_fields(reboot),
            (expected.strftime("%b"), expected.day,
             expected.strftime("%H"), expected.strftime("%M")),
            reboot)

    def test_who_boot_line_agrees_with_last_and_uptime(self):
        who = self.output("who -b")
        self.assertIn("system boot", who)
        stamp = _dt.datetime.strptime(who.split("system boot", 1)[1].strip(),
                                      "%Y-%m-%d %H:%M")
        expected = self._expected_boot()
        self.assertEqual(
            (stamp.year, stamp.strftime("%b"), stamp.day,
             stamp.strftime("%H"), stamp.strftime("%M")),
            (expected.year, expected.strftime("%b"), expected.day,
             expected.strftime("%H"), expected.strftime("%M")))
        reboot = [line for line in self.output("last reboot").splitlines()
                  if line.startswith("reboot")][0]
        self.assertEqual(self._last_date_fields(reboot),
                         (stamp.strftime("%b"), stamp.day,
                          stamp.strftime("%H"), stamp.strftime("%M")), reboot)

    def test_current_session_is_visible(self):
        self.assertIn("pts/0", self.output("who"))
        self.assertIn("still logged in", self.output("last"))
        self.assertIn("root", self.output("who"))

    def test_last_reboot_filters_to_boot_records(self):
        text = self.output("last reboot")
        rows = [line for line in text.splitlines()
                if line.strip() and not line.startswith("wtmp begins")]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row.startswith("reboot"), row)

    def test_lastb_and_lastlog_are_in_world(self):
        self.assertEqual(self.mode("lastb"), "hijack")
        self.assertNotIn("ubuntu", self.output("lastb"))
        self.assertIn("btmp begins", self.output("lastb"))
        log = self.output("lastlog")
        self.assertIn("Username", log)
        self.assertIn("subject04", log)
        self.assertNotIn("ubuntu", log)


class PsClockShiftTests(unittest.TestCase):
    """ps START/lstart: host-relative starttimes must land on the container clock.

    The session view publishes btime = container boot; unshadowed rows would
    otherwise read ``actual + (host_boot - container_boot)``, i.e. the future.
    """

    NOW = _dt.datetime(2026, 10, 27, 12, 0, 0)
    SHIFT = 3600.0

    def shift(self, line):
        return commands._shift_ps_row(line, self.NOW, self.SHIFT)

    def test_lstart_future_row_moves_back_by_the_shift(self):
        line = "    25 Tue Oct 27 21:04:03 2026 /lib/systemd/systemd-udevd"
        fixed = self.shift(line)
        self.assertIn("Tue Oct 27 20:04:03 2026", fixed)
        self.assertIn("/lib/systemd/systemd-udevd", fixed)

    def test_lstart_shadowed_row_is_untouched(self):
        line = "     1 Tue Oct 27 07:04:03 2026 /sbin/init"
        self.assertEqual(self.shift(line), line)

    def test_aux_start_column_is_corrected_but_time_column_is_not(self):
        line = ("root       25  0.0  0.1  16068  3932 ?        Ss   "
                "21:04   0:00 /lib/systemd/systemd-udevd")
        fixed = self.shift(line)
        self.assertIn("20:04", fixed)
        self.assertIn("0:00 /lib/systemd", fixed)
        # A past START and a large TIME column must both stay as printed.
        line = ("root       25  0.0  0.1  16068  3932 ?        Ss   "
                "07:04  21:04 /lib/systemd/systemd-udevd")
        self.assertEqual(self.shift(line), line)

    def test_ef_start_column_is_corrected(self):
        line = "root       25       1  0 21:04 ?        00:00:00 /lib/systemd/systemd-udevd"
        fixed = self.shift(line)
        self.assertIn("20:04", fixed)
        self.assertIn("00:00:00", fixed)

    def test_date_only_start_moves_by_whole_days(self):
        fixed = commands._shift_ps_row(
            "root 25 0.0 0.1 1 1 ? Ss Oct29 0:00 /x", self.NOW, 3 * 86400.0)
        self.assertIn("Oct26", fixed)

    def test_scrub_applies_the_shift_and_keeps_the_engine_rename(self):
        line = ("  42 Tue Oct 27 21:04:03 2026 "
                "/usr/bin/python3 /usr/local/lib/cortex-agent/pod_http.py")
        real_now = _dt.datetime.now().replace(microsecond=0)
        future = real_now + _dt.timedelta(hours=3)
        row = "  42 %s /usr/bin/python3 /usr/local/lib/cortex-agent/pod_http.py" % (
            future.strftime("%a %b %d %H:%M:%S %Y"))
        with mock.patch("sessiond.session_boot_shift", return_value=3600.0):
            out = commands._scrub_proc_text(row)
        self.assertNotIn("python", out.lower())
        self.assertIn("synapse_http", out)
        self.assertIn((future - _dt.timedelta(seconds=3600)).strftime(
            "%a %b %d %H:%M:%S %Y"), out)
        _ = line

    def test_no_view_means_no_change(self):
        row = "  42 Tue Oct 27 21:04:03 2026 /usr/bin/thing"
        with mock.patch("sessiond.session_boot_shift", return_value=0.0):
            self.assertEqual(commands._scrub_proc_text(row), row)


class JournalFormatTests(KernelViewBase):
    """journalctl must look like a journal, not like the dmesg ring."""

    def setUp(self):
        super().setUp()
        self.configure(False, stage=3)

    def test_default_output_is_syslog_not_dmesg(self):
        journal = self.output("journalctl")
        dmesg = self.output("dmesg")
        self.assertNotEqual(journal.strip(), dmesg.strip())
        self.assertNotIn(journal.strip(), dmesg)
        pattern = re.compile(r"^[A-Z][a-z]{2} [ 0-9]\d \d\d:\d\d:\d\d \S+ [\w.-]+(\[\d+\])?: ")
        for line in journal.splitlines():
            if line.strip():
                self.assertRegex(line, pattern)
        self.assertIn("prod-cortex-04", journal)

    def test_unit_filter_works(self):
        nginx = self.output("journalctl -u nginx")
        self.assertIn("nginx[", nginx)
        self.assertNotIn("sshd[", nginx)
        self.assertNotIn("kernel:", nginx)
        self.assertEqual(self.output("journalctl -u does-not-exist"), "-- No entries --\n")

    def test_ssh_unit_shows_the_current_login(self):
        text = self.output("journalctl -u ssh")
        self.assertIn("sshd[", text)
        self.assertIn("Accepted password for root", text)
        self.assertIn("session opened for user root", text)

    def test_boot_filter_and_list_boots(self):
        current = self.output("journalctl -b")
        self.assertNotIn("2180", current)      # previous boot's sshd
        self.assertNotIn("Rebooting", current)
        previous = self.output("journalctl -b -1")
        self.assertIn("2180", previous)
        boots = self.output("journalctl --list-boots")
        self.assertIn("-1 ", boots)
        self.assertIn(" 0 ", boots)

    def test_limit_and_reverse(self):
        latest = self.output("journalctl -n 3").splitlines()
        self.assertEqual(len(latest), 3)
        reversed_lines = self.output("journalctl -r -n 3").splitlines()
        self.assertEqual(reversed_lines, list(reversed(latest)))

    def test_kernel_query_stays_journal_formatted(self):
        text = self.output("journalctl -k")
        self.assertIn(" kernel: ", text)
        for line in text.splitlines():
            if line.strip():
                self.assertNotRegex(line, r"^\[")


class BootFromUptimeTests(KernelViewBase):
    """last reboot / who -b reverse the same /proc/uptime the session reports."""

    def test_kernel_boot_line_follows_proc_uptime(self):
        import commands as cm
        self.configure(True, host=CORE, stage=0, node_id=CORE)
        with mock.patch.object(cm, "_shell_uptime", return_value=7200.0):
            boot = cm._boot_datetime(self.state)
            who = self.output("who -b")
        expected = _dt.datetime.now() - _dt.timedelta(seconds=7200)
        self.assertLess(abs((boot - expected).total_seconds()), 5)
        self.assertIn(expected.strftime("%Y-%m-%d %H:%M"), who)

    def test_story_boot_line_follows_story_proc_uptime(self):
        self.configure(False, stage=0)
        seconds = float(self.output("cat /proc/uptime").split()[0])
        expected = _dt.datetime.now() - _dt.timedelta(seconds=seconds)
        reboot = [line for line in self.output("last reboot").splitlines()
                  if line.startswith("reboot")][0]
        self.assertIn(expected.strftime("%b %d"), reboot)
        self.assertIn(expected.strftime("%H:%M"), reboot)


if __name__ == "__main__":
    unittest.main()
