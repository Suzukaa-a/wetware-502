# -*- coding: utf-8 -*-
"""Offline tests for the real-shell exposure surfaces (edge/hive campaign hosts).

The gateway and the hive run a real bash on a real root filesystem: their
process table, environment, kernel log and journal must still read like a normal
Ubuntu production host and must never expose the python engine behind the login
shell.  These tests pin the planner answers plus the session-side helpers
(process identity scrub, hijack redirects, story-line rendering).
"""
import ctypes
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import campaign  # noqa: E402
import commands  # noqa: E402
import sessiond  # noqa: E402
import shell_parser  # noqa: E402
import state as state_mod  # noqa: E402
import v3  # noqa: E402

EDGE = "edge-gw-01"
CORE = "prod-cortex-04"
HIVE = "hive-director-00"

FORBIDDEN = ("python", "cortex_console", "PYTHONPATH", "WETWARE_")


class RealShellBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wetware-surfaces-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.env = {
            "WETWARE_STATE_DIR": self.state_dir,
            "WETWARE_NODE_ID_PATH": os.path.join(self.tmp, "node-id"),
            "WETWARE_NODE_ID": "",
            "WETWARE_NO_CONFINE": "1",
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = state_mod.State(path=os.path.join(self.state_dir, "state.json"))
        self.state.reset()
        self.state.flags["campaign_v3"] = True
        self.state.save()

    def set_host(self, host):
        path = os.environ["WETWARE_NODE_ID_PATH"]
        if host is None:
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(host + "\n")

    def plan(self, line):
        parsed = shell_parser.parse_line(line)
        return commands.plan(self.state, parsed, "exec")

    def assert_clean(self, text, line=""):
        lowered = (text or "").lower()
        for token in FORBIDDEN:
            self.assertNotIn(token.lower(), lowered, "%s leaked in %r" % (token, line))


class HostSurfaceTests(RealShellBase):
    def setUp(self):
        super().setUp()
        self.set_host(EDGE)
        campaign.bind_state(self.state)

    def test_ps_answers_the_story_table(self):
        for line in ("ps aux", "ps -ef"):
            plan = self.plan(line)
            self.assertEqual([seg.mode for seg in plan.segs], ["hijack"], line)
            self.assertIn("PID", plan.segs[0].stdout)
            self.assertIn("nginx", plan.segs[0].stdout)
            self.assert_clean(plan.segs[0].stdout, line)
        plan = self.plan("ps -eo pid,cmd --no-headers")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertNotIn("USER", plan.segs[0].stdout)
        self.assert_clean(plan.segs[0].stdout, "ps --no-headers")

    def test_ps_pipeline_is_answered_by_the_first_stage(self):
        plan = self.plan("ps aux | grep -i python")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        # the tail is executed against the story table by the executor
        self.assert_clean(plan.segs[0].stdout, "ps aux | grep -i python")

    def test_ps_narrows_to_a_requested_pid(self):
        plan = self.plan("ps -p 1 -o pid,cmd")
        stdout = plan.segs[0].stdout
        self.assertIn("/sbin/init", stdout)
        self.assertNotIn("nginx", stdout)

    def test_pgrep_pidof_pstree_answer_the_story_table(self):
        plan = self.plan("pgrep -af python3")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertEqual(plan.segs[0].stdout, "")
        self.assertNotEqual(plan.segs[0].exit_code, 0)
        plan = self.plan("pidof python3")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertEqual(plan.segs[0].stdout, "")
        plan = self.plan("pgrep -a synapse")
        self.assertIn("4040", plan.segs[0].stdout)
        plan = self.plan("pstree -p")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertNotIn("python", plan.segs[0].stdout.lower())
        self.assertIn("systemd(1)", plan.segs[0].stdout)

    def test_top_answers_like_a_real_host(self):
        plan = self.plan("top -b -n1")
        stdout = plan.segs[0].stdout
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertIn("load average:", stdout)
        self.assertIn("Tasks:", stdout)
        self.assert_clean(stdout, "top")

    def test_top_shows_the_wetware_load_from_act_2(self):
        self.state.stage = 3
        self.state.save()
        plan = self.plan("top -b -n1")
        self.assertIn("hippocampus_dream_loop", plan.segs[0].stdout)

    def test_dmesg_answers_the_story_kernel(self):
        for line in ("dmesg", "dmesg -T", "dmesg --time-format iso", "dmesg | tail -n 5"):
            plan = self.plan(line)
            self.assertEqual([seg.mode for seg in plan.segs], ["hijack"], line)
            stdout = plan.segs[0].stdout
            self.assertIn("Linux version", stdout, line)
            self.assertNotIn("read kernel buffer failed", stdout, line)
            self.assert_clean(stdout, line)

    def test_journalctl_answers_a_plausible_journal(self):
        plan = self.plan("journalctl")
        self.assertEqual(plan.segs[0].mode, "hijack")
        stdout = plan.segs[0].stdout
        self.assertIn("kernel:", stdout)
        self.assertNotIn("No journal files were found", stdout)
        self.assertIn("edge-gw-01", stdout)

    def test_journalctl_unit_ssh_is_the_sshd_journal(self):
        for line in ("journalctl -u ssh", "journalctl -u sshd",
                     "journalctl --unit=ssh.service --no-pager",
                     "journalctl -u ssh -n 2"):
            plan = self.plan(line)
            stdout = plan.segs[0].stdout
            self.assertIn("sshd[", stdout, line)
            self.assertIn("Accepted password for root", stdout, line)
            self.assertNotIn("No journal files were found", stdout, line)
            self.assertNotIn("-- No entries --", stdout, line)
            self.assert_clean(stdout, line)

    def test_journalctl_n_limits_and_r_reverses(self):
        plan = self.plan("journalctl -u ssh -n 1")
        lines = [row for row in plan.segs[0].stdout.splitlines() if row]
        self.assertEqual(len(lines), 1)
        plan = self.plan("journalctl -u ssh -r -n 1")
        lines = [row for row in plan.segs[0].stdout.splitlines() if row]
        self.assertEqual(len(lines), 1)
        self.assertIn("session opened", lines[0])

    def test_journalctl_unknown_unit_has_no_entries(self):
        plan = self.plan("journalctl -u does-not-exist")
        self.assertEqual(plan.segs[0].stdout.strip(), "-- No entries --")

    def test_journalctl_kernel_matches_dmesg(self):
        plan = self.plan("journalctl -k | tail -n 3")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertIn("Linux version", plan.segs[0].stdout)

    def test_story_pid_proc_cmdline_answers_instead_of_enoent(self):
        plan = self.plan("cat /proc/4040/cmdline | tr '\\0' ' '")
        self.assertEqual(plan.segs[0].mode, "hijack")
        text = plan.segs[0].stdout
        self.assertIn("synapse_worker", text)
        self.assertNotIn("python", text.lower())
        plan = self.plan("cat /proc/6660/cmdline")
        self.assertIn("inquisitor_daemon", plan.segs[0].stdout)
        plan = self.plan("cat /proc/4040/comm")
        self.assertEqual(plan.segs[0].stdout, "synapse_worker\n")

    def test_real_pid_proc_cmdline_stays_real(self):
        for line in ("cat /proc/self/cmdline", "cat /proc/$PPID/cmdline",
                     "cat /proc/12345/cmdline"):
            plan = self.plan(line)
            self.assertIn(plan.segs[0].mode, ("real", "real_line"), line)

    def test_env_reads_are_filtered(self):
        for line in ("env", "printenv", "set", "env | grep -i python"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "real_post", line)

    def test_mount_surfaces_are_postprocessed(self):
        for line in ("mount", "df -h", "findmnt", "cat /proc/mounts",
                     "cat /proc/self/mountinfo | head"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "real_post", line)

    def test_ordinary_commands_stay_real(self):
        for line in ("cat /var/log/auth.log", "tar -xf /tmp/lin.tar -C /root/.ssh",
                     "git -C /home/ops/cortex-sre log",
                     "ls -la /usr/local/lib/", "cat /proc/self/cmdline | tr '\\0' ' '"):
            plan = self.plan(line)
            self.assertIn(plan.segs[0].mode, ("real", "real_line"), line)


class EnvironmentTests(RealShellBase):
    def test_child_env_has_no_harness_bookkeeping(self):
        self.set_host(EDGE)
        campaign.bind_state(self.state)
        import lore_texts
        import world as world_mod

        runtime = sessiond.Runtime(self.state, shell_parser, commands, lore_texts, world_mod)
        env = runtime.child_env()
        for key in env:
            self.assertFalse(key.startswith("WETWARE_"), key)
            self.assertNotIn("PYTHON", key.upper(), key)
        self.assertNotIn("cortex-agent", json.dumps(env))
        self.assertEqual(env["USER"], "root")
        self.assertEqual(env["SHELL"], "/bin/bash")

    def test_env_filter_drops_harness_lines(self):
        text = "\n".join([
            "USER=root",
            "PYTHONPATH=/usr/local/lib/cortex-agent",
            "WETWARE_NODE_ID=edge-gw-01",
            "WETWARE_STATE_DIR=/var/lib/cortex-agent",
            "PWD=/usr/local/lib/cortex-agent/venv",
            "CORTEX_ENV=production",
            "PATH=/usr/bin:/bin",
        ])
        out = commands._filter_env_output(text)
        self.assertNotIn("PYTHONPATH", out)
        self.assertNotIn("WETWARE_", out)
        self.assertNotIn("cortex-agent", out)
        self.assertIn("CORTEX_ENV=production", out)
        self.assertIn("PATH=/usr/bin:/bin", out)

    def test_mount_filter_hides_the_mask_scaffolding(self):
        text = "\n".join([
            "/dev/sda1 on /var/lib/cortex-agent type ext4 (rw,relatime)",
            "tmpfs on /usr/local/lib type tmpfs (rw,relatime,mode=755)",
            "tmpfs on /run/log/journal type tmpfs (rw,relatime,mode=0700)",
            "devtmpfs on /dev type devtmpfs (rw,relatime,size=2000000k)",
            "/dev/sda1 on /var/lib/apt type ext4 (rw,nosuid,nodev)",
        ])
        out = commands._filter_mount_output(text)
        self.assertNotIn("cortex-agent", out)
        self.assertNotIn("/usr/local/lib", out)
        self.assertNotIn("/var/lib", out)
        self.assertNotIn("/run/log/journal", out)
        self.assertIn("devtmpfs on /dev", out)

    def test_mount_filter_keeps_a_normal_root_line(self):
        text = "/dev/sda1 / ext4 rw,relatime,discard,errors=remount-ro 0 0"
        self.assertEqual(commands._filter_mount_output(text).strip(), text)


class CoreHostTests(RealShellBase):
    def test_core_keeps_the_engine_planner(self):
        self.set_host(CORE)
        campaign.bind_state(self.state)
        plan = self.plan("ps aux")
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertEqual(plan.segs[0].hook, "Process Table Is A Body")

    def test_core_surfaces_are_not_rerouted_to_the_real_shell_layer(self):
        self.set_host(CORE)
        campaign.bind_state(self.state)
        # the exposure layer only runs on the real-shell hosts: on core the
        # facade planner keeps mount/journalctl in the engine's hands
        for line in ("mount", "cat /proc/mounts"):
            plan = self.plan(line)
            self.assertNotEqual(plan.segs[0].mode, "real_post", line)


class ProcessIdentityTests(unittest.TestCase):
    def test_process_title_for_exec_and_login_shell(self):
        self.assertEqual(sessiond.process_title_bytes(), b"-bash")
        title = sessiond.process_title_bytes("hostname")
        self.assertTrue(title.startswith(b"/bin/bash\x00-c\x00"))
        self.assertIn(b"hostname", title)

    def test_argv_image_is_rewritten(self):
        original = b"/usr/bin/python3\x00-c\x00import cortex_console\x00"
        size = 96
        buffer = ctypes.create_string_buffer(original, size)
        start = ctypes.addressof(buffer)
        sessiond._scrub_argv_image(ctypes, start, start + size,
                                   sessiond.process_title_bytes("hostname"))
        image = buffer.raw
        self.assertTrue(image.startswith(b"/bin/bash\x00-c\x00hostname"))
        self.assertNotIn(b"python", image.lower())
        self.assertNotIn(b"cortex_console", image)
        self.assertEqual(len(image), size)

    def test_login_shell_argv_image_has_no_garbage(self):
        size = 64
        buffer = ctypes.create_string_buffer(b"/usr/bin/python3 -c bad", size)
        start = ctypes.addressof(buffer)
        sessiond._scrub_argv_image(ctypes, start, start + size)
        image = buffer.raw
        self.assertTrue(image.startswith(b"-bash\x00"))
        self.assertNotIn(b"python", image.lower())

    def test_env_image_rewrites_harness_variables_in_place(self):
        raw = b"".join([
            b"USER=root\x00",
            b"PYTHONPATH=/usr/local/lib/cortex-agent\x00",
            b"WETWARE_NODE_ID=edge-gw-01\x00",
            b"SHELL=/usr/local/bin/cortex-shell\x00",
            b"PATH=/usr/local/sbin:/usr/bin:/bin\x00",
        ])
        size = len(raw)
        buffer = ctypes.create_string_buffer(raw, size)
        start = ctypes.addressof(buffer)
        sessiond._scrub_env_image(ctypes, start, start + size)
        image = buffer.raw
        self.assertNotIn(b"PYTHONPATH", image)
        self.assertNotIn(b"WETWARE_", image)
        self.assertNotIn(b"cortex", image)
        self.assertIn(b"SHELL=/bin/bash", image)
        self.assertIn(b"USER=root", image)
        self.assertIn(b"PATH=/usr/local/sbin", image)
        # every rewritten entry keeps its original length (in-place, C environ
        # pointers stay valid, so exec'd helpers inherit a sane environment)
        self.assertEqual(len(image), size)


class RuntimeHelperTests(RealShellBase):
    def _runtime(self):
        import lore_texts
        import world as world_mod

        self.set_host(EDGE)
        campaign.bind_state(self.state)
        return sessiond.Runtime(self.state, shell_parser, commands, lore_texts, world_mod)

    def test_hijack_redirect_writes_the_story_answer(self):
        runtime = self._runtime()
        target = os.path.join(self.tmp, "ps.txt")
        parsed = shell_parser.parse_line("ps aux > %s" % target)
        plan = commands.plan(self.state, parsed, "exec")
        segment = parsed.segments[0]
        payload = plan.segs[0].stdout.encode("utf-8")
        self.assertTrue(sessiond.answer_hijack_redirects(runtime, segment, payload))
        with open(target, encoding="utf-8") as stream:
            written = stream.read()
        self.assertEqual(written, plan.segs[0].stdout)
        self.assertNotIn("python", written.lower())

    def test_interactive_renderer_runs_real_segments(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("echo hello; ps aux")
        plan = commands.plan(self.state, parsed, "interactive")
        self.assertEqual([seg.mode for seg in plan.segs], ["real", "hijack"])
        output = session._render_story_line(parsed, plan)
        text = output.decode("utf-8", "replace")
        self.assertIn("hello", text)
        self.assertIn("nginx", text)
        self.assertNotIn("python", text.lower())

    def test_interactive_renderer_applies_the_pipeline_tail(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("ps aux | grep -i python")
        plan = commands.plan(self.state, parsed, "interactive")
        output = session._render_story_line(parsed, plan)
        self.assertEqual(output.strip(), b"")

    def test_interactive_renderer_keeps_non_matching_rows(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("ps aux | grep -i nginx")
        plan = commands.plan(self.state, parsed, "interactive")
        output = session._render_story_line(parsed, plan).decode("utf-8", "replace")
        self.assertIn("nginx", output)
        self.assertNotIn("python", output.lower())


if __name__ == "__main__":
    unittest.main()
