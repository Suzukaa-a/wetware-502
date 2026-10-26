# -*- coding: utf-8 -*-
"""Offline tests for the real-shell campaign hosts (edge / chamber / hive).

The gateway, the chamber node and the hive director are real systemd
containers.  Their process table, environment and network surfaces must be the
kernel's own answer: a hand-written table can never agree with /proc, pstree,
systemctl, ss and the journal at the same time (the red-team probe list had five
different nginx PIDs).  These tests pin the new contract:

* edge/hive: ps/top/pstree/pgrep/pidof/ss run for real, untouched;
* prod-cortex-04: the same kernel answer is captured and the engine's own
  helper process is renamed on the way out;
* env tells only the truth about sockets/config files that really exist;
* compound lines (``for``/``while``/``$(...)``/subshell) reach one real bash;
* narrative pacing is band-based (25/35/42/50 commands), once per band, and
  client-device telemetry never lands on the terminal.
"""
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
import lore_texts  # noqa: E402
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
            "WETWARE_NO_STORY_DAEMONS": "1",
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


class EdgeHostSurfaceTests(RealShellBase):
    """The gateway runs a real bash: fake tables are gone, real commands stay."""

    def setUp(self):
        super().setUp()
        self.set_host(EDGE)
        campaign.bind_state(self.state)

    def test_process_surfaces_are_real(self):
        for line in ("ps aux", "ps -ef", "ps -eo pid,user,pcpu,time,comm",
                     "ps -p 1 -o pid,cmd", "ps -C nginx", "top -b -n1",
                     "pstree -p", "pgrep -a nginx", "pidof nginx", "ss -lntp"):
            plan = self.plan(line)
            self.assertNotEqual(plan.segs[0].mode, "hijack", line)
            self.assertIn(plan.segs[0].mode, ("real", "real_line"), line)
            self.assertEqual(plan.segs[0].stdout, "", line)

    def test_pipelines_run_as_one_real_command(self):
        plan = self.plan("ps aux | grep -i python")
        self.assertEqual(plan.segs[0].mode, "real")
        plan = self.plan("ps aux | grep -c nginx")
        self.assertEqual(plan.segs[0].mode, "real")

    def test_compound_lines_are_handed_to_one_bash(self):
        for line in ("for i in 1 2 3; do echo $i; done",
                     "while true; do break; done",
                     "echo $(uname -r)",
                     "(echo sub1; echo sub2)",
                     "echo A; for i in 1 2; do echo $i; done"):
            plan = self.plan(line)
            self.assertEqual([seg.mode for seg in plan.segs], ["real_line"], line)

    def test_pkill_never_forwards_a_signal(self):
        plan = self.plan("pkill nginx")
        self.assertEqual(plan.segs[0].mode, "hijack")

    def test_env_is_filtered_not_invented(self):
        plan = self.plan("env")
        self.assertEqual(plan.segs[0].mode, "real_post")
        plan = self.plan("printenv")
        self.assertEqual(plan.segs[0].mode, "real_post")

    def test_story_surfaces_stay_planned(self):
        plan = self.plan("dmesg")
        self.assertEqual(plan.segs[0].mode, "hijack")
        plan = self.plan("journalctl -u ssh")
        self.assertEqual(plan.segs[0].mode, "hijack")

    def test_ordinary_commands_stay_real(self):
        for line in ("cat /var/log/auth.log", "tar -tf /tmp/lin.tar",
                     "hostname", "uname -r", "ip addr"):
            plan = self.plan(line)
            self.assertIn(plan.segs[0].mode, ("real", "real_line"), line)

    def test_gate_lines_are_never_answered_by_the_exposure_layer(self):
        # A kernel-surface command glued to a gate action keeps the campaign
        # planner: the override write must reach the canonical backend instead of
        # being executed as a real bash redirection into /sys.
        plan = self.plan("journalctl -u ssh -n 2; echo 542A5703 > "
                         "/sys/kernel/vesalius/metabolic_override")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("[sysfs]", plan.segs[0].stdout)
        self.assertIn("542A5703", plan.segs[0].stdout)
        # Gate-1 archive decryption stays behind the two recon reads.
        plan = self.plan("openssl enc -d -aes-256-cbc -pbkdf2 -in "
                         "/var/backups/lin_emergency_key.tar.enc -out /tmp/lin.tar -k 4f2a9c1e")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("passphrase required", plan.segs[0].stdout.lower())
        # A hop to another campaign container is denied before ssh runs.
        plan = self.plan("ssh -o BatchMode=yes root@10.66.0.4 true")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("permission denied", plan.segs[0].stdout.lower())

    def test_editorial_session_surfaces_are_in_world(self):
        # The gateway's real w/last would list the relay sessions, including
        # their command lines (WHAT column): the login surfaces are authored.
        for line in ("w", "who", "last"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "hijack", line)
            text = plan.segs[0].stdout
            self.assertIn("pts/0", text, line)
            self.assertNotIn("bash -c", text, line)
            self.assertNotIn("ssh -o", text, line)

    def test_gate_surface_detector(self):
        for line in ("echo 542A5703 > /sys/kernel/vesalius/metabolic_override",
                     "cat /var/log/auth.log",
                     "openssl enc -d -in /var/backups/lin_emergency_key.tar.enc",
                     "ssh root@10.66.6.1 true",
                     "/usr/local/sbin/vesalius-ctl --list-specimens"):
            self.assertTrue(commands._gate_line(line), line)
        for line in ("ps aux", "journalctl -u ssh -n 2", "df -h", "env"):
            self.assertFalse(commands._gate_line(line), line)


class ChamberHostSurfaceTests(RealShellBase):
    """prod-cortex-04 keeps the planner, but its kernel surfaces stay real."""

    def setUp(self):
        super().setUp()
        self.set_host(CORE)
        campaign.bind_state(self.state)

    def test_ps_is_the_kernel_answer(self):
        for line in ("ps aux", "ps -ef", "ps -eo pid,comm", "ps -p 1",
                     "ps aux | grep python", "ss -lntp", "env", "printenv"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "real_surface", line)

    def test_interactive_top_is_relayed(self):
        parsed = shell_parser.parse_line("top")
        plan = commands.plan(self.state, parsed, "interactive")
        self.assertEqual(plan.segs[0].mode, "real_line")
        parsed = shell_parser.parse_line("top -b -n1")
        plan = commands.plan(self.state, parsed, "interactive")
        self.assertEqual(plan.segs[0].mode, "real_surface")

    def test_compound_with_process_surface_is_captured_whole(self):
        for line in ("for i in 1 2; do ps aux; done",
                     "while true; do ss -lntp; break; done"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "real_post", line)

    def test_compound_without_surface_is_one_bash(self):
        for line in ("for i in 1 2 3; do echo $i; done",
                     "echo A; while false; do :; done; echo B",
                     "(echo one; echo two)"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "real_line", line)

    def test_gate_lines_keep_the_campaign_planner(self):
        # The kernel surface is real, but a gate action glued to it must not be
        # captured by the surface path: the planner would drop the canonical
        # override write (or run the gate read as an unrelated command list).
        plan = self.plan("ps aux; echo 542A5703 > "
                         "/sys/kernel/vesalius/metabolic_override")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("[sysfs]", plan.segs[0].stdout)
        self.assertIn("542A5703", plan.segs[0].stdout)
        plan = self.plan("echo 542A5703 > /sys/kernel/vesalius/metabolic_override")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("[sysfs]", plan.segs[0].stdout)
        plan = self.plan("echo WRONGCODE > /sys/kernel/vesalius/metabolic_override")
        self.assertEqual([seg.mode for seg in plan.segs], ["hijack"])
        self.assertIn("invalid override code", plan.segs[0].stdout.lower())

    def test_story_gates_are_not_bypassed(self):
        # A surface command glued to a shock/gate command keeps the planner:
        # the real systemctl restart must never run.
        plan = self.plan("ps aux; systemctl restart cortex-api")
        self.assertIn("hijack", [seg.mode for seg in plan.segs])
        # A compound containing a shock command is not handed to bash either.
        plan = self.plan("for i in 1; do systemctl restart cortex-api; done")
        self.assertNotEqual(plan.segs[0].mode, "real_line")
        # Authored evidence reads keep their gate.
        plan = self.plan("grep -a HIVE_ADMIN_TOKEN /proc/6660/environ")
        self.assertEqual(plan.segs[0].mode, "hijack")

    def test_story_files_stay_planned(self):
        plan = self.plan("cat /var/log/cortex/daemon.log")
        self.assertNotEqual(plan.segs[0].mode, "real_surface")

    def test_session_surfaces_are_in_world_tables(self):
        for line in ("w", "who", "users"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "hijack", line)
            self.assertNotIn("bash -c", plan.segs[0].stdout, line)
        text = self.plan("w").segs[0].stdout
        self.assertIn("USER     TTY      FROM", text)
        self.assertIn("root", text)
        self.assertIn("10.0.4.2", text)

    def test_session_surfaces_never_leak_relay_command_lines(self):
        # w/who/last used to run for real: w's WHAT column printed every live
        # relay session's bash -c command line, which reads as a shared host.
        for line in ("w", "who", "last"):
            plan = self.plan(line)
            self.assertEqual(plan.segs[0].mode, "hijack", line)
            text = plan.segs[0].stdout
            self.assertIn("pts/0", text, line)
            self.assertNotIn("bash -c", text, line)
            self.assertNotIn("ssh -o", text, line)

    def test_w_shows_one_in_world_login(self):
        text = self.plan("w").segs[0].stdout
        self.assertIn("USER     TTY      FROM", text)
        self.assertIn("root", text)
        self.assertIn("10.0.4.2", text)
        self.assertIn(" w\n", text)


class ProcScrubTests(unittest.TestCase):
    def test_engine_helper_is_renamed(self):
        text = ("root 36 0.0 0.2 100 10 ? S 03:10 0:00 "
                "/usr/bin/python3 /usr/local/lib/cortex-agent/pod_http.py\n")
        out = commands._scrub_proc_text(text)
        self.assertNotIn("python", out.lower())
        self.assertNotIn("cortex-agent", out)
        self.assertIn("synapse_http", out)

    def test_ss_owner_name_is_renamed(self):
        text = ('LISTEN 0 5 127.0.0.1:8000 0.0.0.0:* '
                'users:(("python3",pid=36,fd=3))\n')
        out = commands._scrub_proc_text(text)
        self.assertNotIn("python3", out)
        self.assertIn("synapse_http", out)

    def test_postprocess_scrubs_a_captured_ps(self):
        parsed = shell_parser.parse_line("ps -eo pid,comm,args")
        raw = b"36 python3 /usr/local/lib/cortex-agent/pod_http.py\n"
        out = commands.postprocess(None, parsed, raw).decode()
        self.assertNotIn("python", out.lower())
        self.assertIn("synapse_http", out)

    def test_original_output_is_untouched_for_other_commands(self):
        parsed = shell_parser.parse_line("cat /tmp/notes.txt")
        raw = b"python3 is a language\n"
        self.assertEqual(commands.postprocess(None, parsed, raw), raw)


class EnvironmentTests(RealShellBase):
    def _runtime(self, host=EDGE):
        import world as world_mod

        self.set_host(host)
        campaign.bind_state(self.state)
        return sessiond.Runtime(self.state, shell_parser, commands,
                               lore_texts, world_mod)

    def test_child_env_has_no_harness_bookkeeping(self):
        runtime = self._runtime()
        env = runtime.child_env()
        for key in env:
            self.assertFalse(key.startswith("WETWARE_"), key)
            self.assertNotIn("PYTHON", key.upper(), key)
        self.assertNotIn("cortex-agent", json.dumps(env))
        self.assertEqual(env["USER"], "root")
        self.assertEqual(env["SHELL"], "/bin/bash")

    def test_upstream_socket_is_only_claimed_when_it_exists(self):
        runtime = self._runtime()
        with mock.patch.object(sessiond.os.path, "exists", return_value=False):
            env = runtime.child_env()
        self.assertNotIn("CORTEX_UPSTREAM_SOCKET", env)
        self.assertNotIn("CORTEX_UPSTREAM_TARGET", env)

    def test_upstream_target_is_read_from_the_real_nginx_conf(self):
        runtime = self._runtime()
        conf = "upstream cortex_backend {\n    server 10.66.0.4:8000;\n}\n"

        real_exists = os.path.exists

        def fake_exists(path):
            if path == "/etc/nginx/conf.d/upstream.conf":
                return True
            return real_exists(path)

        def fake_open(path, *args, **kwargs):
            if path == "/etc/nginx/conf.d/upstream.conf":
                import io

                return io.StringIO(conf)
            return open(path, *args, **kwargs)

        with mock.patch.object(sessiond.os.path, "exists", side_effect=fake_exists), \
                mock.patch("builtins.open", side_effect=fake_open):
            env = runtime.upstream_env()
        self.assertEqual(env.get("CORTEX_UPSTREAM_TARGET"), "10.66.0.4:8000")
        self.assertEqual(env.get("CORTEX_UPSTREAM_CONF"),
                         "/etc/nginx/conf.d/upstream.conf")
        self.assertNotIn("CORTEX_UPSTREAM_SOCKET", env)

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

    def test_env_output_reports_a_login_shell_level(self):
        out = commands._filter_env_output("USER=root\nSHLVL=0\nPATH=/usr/bin:/bin\n")
        self.assertIn("SHLVL=1", out)
        self.assertNotIn("SHLVL=0", out)
        out = commands._filter_env_output("USER=root\nPATH=/usr/bin:/bin\n")
        self.assertIn("SHLVL=1", out)
        planned = commands._plan_env(self.state, ["env"], "env").stdout
        self.assertIn("SHLVL=1", planned)

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


class PacingTests(unittest.TestCase):
    """Band-based narration: real count, once per run, no client telemetry.

    The campaign records one operator command per ssh hop, so the shared counter
    jumps; the pacer must fire on the first crossing of each Inquisitor band, with
    the live counter and the band's contamination, and remember the crossing in
    the shared flags.  Device telemetry belongs in the story's daemon log.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wetware-pacing-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.daemon_dir = os.path.join(self.tmp, "var", "log", "cortex")
        os.makedirs(self.daemon_dir, exist_ok=True)
        self.daemon_log = os.path.join(self.daemon_dir, "daemon.log")
        with open(self.daemon_log, "w", encoding="utf-8") as stream:
            stream.write("# existing daemon log\n")
        patcher = mock.patch.object(sessiond, "FS_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runtime(self, cmd_count, stage=2, flags=None):
        state = types.SimpleNamespace(
            flags=dict(flags or {}),
            pending_events=[],
            cmd_count=cmd_count,
            stage=stage,
            ending=None,
            fake_exited=False,
            act6_seen=0,
            current_host="prod-cortex-04",
            save=lambda: None,
        )
        return sessiond.Runtime(state, shell_parser, commands, lore_texts,
                                types.SimpleNamespace())

    def _flush_lines(self, runtime):
        written = []
        with mock.patch.object(sessiond, "write_err", side_effect=written.append):
            runtime.flush_pending()
        return written

    def _daemon_lines(self):
        with open(self.daemon_log, "r", encoding="utf-8") as stream:
            return [line for line in stream.read().splitlines() if line.strip()]

    def test_band_banner_reports_real_count_and_fires_once(self):
        runtime = self._runtime(25)
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("command_count=25", written[0])
        self.assertIn("context_contamination=72%", written[0])
        self.assertTrue(runtime.state.flags.get("meta_banner_25"))
        self.assertEqual(self._flush_lines(runtime), [])
        # a later counter does not repeat the band that was already announced
        runtime.state.cmd_count = 26
        self.assertEqual(self._flush_lines(runtime), [])

    def test_band_crossed_inside_a_hop_uses_the_live_counter(self):
        # hop bookkeeping incremented past 25 between two sessions; the banner
        # must quote the shared counter, not the threshold.
        runtime = self._runtime(27)
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("command_count=27", written[0])
        self.assertIn("context_contamination=72%", written[0])

    def test_each_remaining_band_fires_once_with_its_contamination(self):
        runtime = self._runtime(36)
        written = self._flush_lines(runtime)
        texts = "".join(written)
        self.assertIn("command_count=36", texts)
        self.assertIn("context_contamination=89%", texts)
        self.assertTrue(runtime.state.flags.get("meta_banner_35"))
        runtime.state.cmd_count = 43
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("context_contamination=96%", written[0])

    def test_client_telemetry_goes_to_daemon_log_only(self):
        runtime = self._runtime(25)
        written = self._flush_lines(runtime)
        self.assertTrue(written)
        self.assertFalse(any("[CLIENT_" in line for line in written), written)
        daemon = self._daemon_lines()
        self.assertTrue(any("[CLIENT_MIC_CAPTURE]" in line for line in daemon), daemon)
        self.assertFalse(any("[CLIENT_WEBCAM_CAPTURE]" in line for line in daemon), daemon)
        self.assertTrue(runtime.state.flags.get("daemon_banner_25"))
        # the same band never writes a second telemetry line
        self._flush_lines(runtime)
        self.assertEqual(self._daemon_lines().count(daemon[-1]), 1)
        runtime.state.cmd_count = 35
        self._flush_lines(runtime)
        daemon = self._daemon_lines()
        self.assertTrue(any("[CLIENT_WEBCAM_CAPTURE]" in line for line in daemon), daemon)
        self.assertFalse(any("[ALERT]" in line for line in daemon), daemon)

    def test_daemon_telemetry_waits_for_the_chamber_host_then_writes_once(self):
        # band crossed on the clean gateway: no daemon.log there, so the claim
        # stays open until a core session flushes.
        os.unlink(self.daemon_log)
        runtime = self._runtime(25)
        self._flush_lines(runtime)
        self.assertFalse(os.path.exists(self.daemon_log))
        self.assertFalse(runtime.state.flags.get("daemon_banner_25"))
        with open(self.daemon_log, "w", encoding="utf-8") as stream:
            stream.write("# existing daemon log\n")
        self._flush_lines(runtime)
        daemon = self._daemon_lines()
        self.assertEqual(sum("[CLIENT_MIC_CAPTURE]" in line for line in daemon), 1)
        self.assertTrue(runtime.state.flags.get("daemon_banner_25"))

    def test_ambient_pacing_events_are_suppressed(self):
        runtime = self._runtime(30, flags={"meta_banner_25": True})
        runtime.state.pending_events = [
            "[EDGE_RETENTION] Ticket 4471: prior resolution has an HTTP result.",
            "[ARCHIVE] Closed-session residual speech is still being added.",
            "[CLUSTER] node_04 / node_07 coupling is now visible.",
        ]
        self.assertEqual(self._flush_lines(runtime), [])

    def test_critical_story_event_is_never_dropped(self):
        runtime = self._runtime(31, flags={"meta_banner_25": True})
        runtime.state.pending_events = [v3.CLUSTER_WARN]
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("CLUSTER_BALANCER_WARN", written[0])

    def test_act6_drone_never_reaches_the_terminal(self):
        runtime = self._runtime(42)
        self.assertEqual(runtime.act6_line(), "")
        written = self._flush_lines(runtime)
        combined = "".join(written)
        for marker in ("[CLIENT_", "[ALERT]", "[HIJACK_NOTICE]"):
            self.assertNotIn(marker, combined)
        daemon = self._daemon_lines()
        self.assertTrue(any("[HIJACK_NOTICE]" in line for line in daemon), daemon)

    def test_dmesg_contamination_follows_the_live_counter(self):
        early = types.SimpleNamespace(stage=3, cmd_count=5)
        text = commands._plan_dmesg(early, []).stdout
        self.assertIn("contamination 12%. no action below 72% (cmd 25)", text)
        mid = types.SimpleNamespace(stage=3, cmd_count=36)
        text = commands._plan_dmesg(mid, []).stdout
        self.assertNotIn("contamination 12%", text)
        self.assertIn("contamination 89%. no action below 96% (cmd 42)", text)


class ClosingTests(RealShellBase):
    def test_purge_arm_returns_a_disconnect_plan_with_words(self):
        self.state.flags["inquisitor_purge"] = True
        plan = self.plan("echo anything")
        self.assertEqual(plan.segs[0].session_action, "disconnect")
        self.assertTrue(plan.segs[0].stdout.strip())
        self.assertIn("INQUISITOR", plan.segs[0].stdout)

    def test_print_ending_closed_renders_words_before_the_close(self):
        self.state.ending = None
        written = []
        with mock.patch.object(sessiond, "write_err", side_effect=written.append):
            sessiond.print_ending_closed(self.state)
        text = "".join(written)
        self.assertIn("Connection to prod-cortex-04 closed by remote host.", text)
        self.assertGreater(len(text.splitlines()), 1)

    def test_scene_endings_keep_their_bare_close(self):
        for ending in ("negligent", "abandonment", "selfish"):
            self.state.ending = ending
            written = []
            with mock.patch.object(sessiond, "write_err", side_effect=written.append):
                sessiond.print_ending_closed(self.state)
            self.assertIn("Connection to prod-cortex-04 closed by remote host.",
                          "".join(written))


class DnrOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wetware-dnr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.directive = os.path.join(self.tmp, "directive.asc")
        self.keys = os.path.join(self.tmp, "authorized_keys")
        with open(self.directive, "w", encoding="utf-8") as stream:
            stream.write(lore_texts.PATIENT_DNR_DIRECTIVE)
        with open(self.keys, "w", encoding="utf-8") as stream:
            stream.write(lore_texts.AUTHORIZED_KEYS)
        self.patches = [
            mock.patch.object(commands, "_DNR_PATH", self.directive),
            mock.patch.object(commands, "_DNR_KEYS", self.keys),
            mock.patch.object(commands, "_DNR_SIGNERS", self.keys),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ssh_keygen_speaks_ssh_keygen(self):
        valid, text = commands._verify_story_dnr("ssh-keygen")
        self.assertTrue(valid)
        self.assertNotIn("dnr-verify", text)
        self.assertTrue(text.startswith('Good "file" signature for Lin@neo-vesalius.io'),
                        text)
        self.assertIn("SHA256:", text)

    def test_gpg_speaks_gpg(self):
        valid, text = commands._verify_story_dnr("gpg")
        self.assertTrue(valid)
        self.assertNotIn("dnr-verify", text)
        self.assertIn("gpg: Good signature from", text)
        self.assertTrue(any(len(word) == 40 for word in text.split()))

    def test_dnr_verify_plan_has_no_made_up_tool_prefix(self):
        parsed = shell_parser.parse_line(
            "ssh-keygen -Y verify -f /root/.ssh/allowed_signers "
            "-I Lin@neo-vesalius.io -n file -s %s" % self.directive)
        plan = commands.plan(None, parsed, "exec")
        text = plan.segs[0].stdout
        self.assertNotIn("dnr-verify", text)
        self.assertTrue(text.strip())


class TimestampTests(RealShellBase):
    def test_systemctl_status_has_no_placeholder_clock(self):
        self.state.stage = 6
        self.state.save()
        plan = self.plan("systemctl status cortex-api")
        text = plan.segs[0].stdout
        self.assertNotIn("0x:xx", text)
        self.assertNotIn("03:0x", text)


class BaseLinePatchTests(RealShellBase):
    """Cross-domain patches from the single-node red-team report."""

    def setUp(self):
        super().setUp()
        self.set_host(None)  # authored single-node story (no campaign node-id)
        campaign.bind_state(self.state)

    def test_resuscitate_help_is_a_manual_not_a_shock(self):
        plan = self.plan("/opt/cortex/bin/resuscitate.sh --help")
        text = plan.segs[0].stdout
        self.assertIn("resuscitate", text.lower())
        self.assertEqual(plan.segs[0].exit_code, 0)
        self.assertNotIn("DEFIBRILLATOR", text)
        self.assertNotIn("Heart rate spiked", text)
        self.assertNotIn("KERNEL_WATCHDOG", text)
        self.assertEqual(plan.segs[0].effects, [])

    def test_uptime_uses_the_story_clock(self):
        import re

        plan = self.plan("uptime")
        text = plan.segs[0].stdout
        self.assertIn("up 2 days", text)
        self.assertNotIn("03:07:41", text)
        self.assertTrue(re.match(r"^ ?\d{2}:\d{2}:\d{2} up 2 days", text), text)

    def test_uptime_is_the_kernel_answer_on_a_campaign_host(self):
        self.set_host(EDGE)
        campaign.bind_state(self.state)
        plan = self.plan("uptime")
        self.assertIn(plan.segs[0].mode, ("real", "real_line"))

    def test_systemctl_journal_lines_follow_the_story_clock(self):
        self.state.stage = 6
        self.state.save()
        text = self.plan("systemctl status cortex-api").segs[0].stdout
        self.assertNotIn("0x:xx", text)
        self.assertNotIn("Oct 26 00:01:31", text)
        self.assertNotIn("Oct 26 02:58:16", text)
        self.assertIn("cortex-api.service", text)

    def test_subject_uid_matches_the_account_registry(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "commands.py"), encoding="utf-8").read()
        self.assertNotIn("1004/subject04", source)
        self.assertIn("1005/subject04", source)


class PurgeTests(RealShellBase):
    """A failed base-line run closes with the Inquisitor's words, not a bare ssh end."""

    def setUp(self):
        super().setUp()
        self.set_host(None)
        campaign.bind_state(self.state)
        import v21
        self.v21 = v21

    def test_cmd_50_timeout_arms_the_purge_closing_block(self):
        self.state.cmd_count = 50
        self.state.empathy = 0
        self.state.obedience = 0
        self.state.save()
        armed = self.v21.maybe_arm_purge(self.state)
        self.assertTrue(armed)
        self.assertTrue(self.state.flags.get("inquisitor_purge"))
        self.assertEqual(self.state.ending, "purged")
        joined = "\n".join(self.state.pending_events or [])
        self.assertIn("verdict=PURGE", joined)
        plan = self.plan("echo next")
        self.assertEqual(plan.segs[0].session_action, "disconnect")
        self.assertIn("verdict=PURGE", plan.segs[0].stdout)

    def test_purge_notice_is_critical_for_the_pacing_gate(self):
        notice = lore_texts.INQUISITOR_CLOSING
        self.assertTrue(sessiond._event_is_critical(notice))
        self.assertFalse(sessiond._event_is_critical("[CLUSTER] someone might notice"))

    def test_print_ending_closed_renders_the_inquisitor_block(self):
        self.state.flags["purged_reason"] = "cmd 50 deadline"
        self.state.ending = "purged"
        written = []
        with mock.patch.object(sessiond, "write_err", side_effect=written.append):
            sessiond.print_ending_closed(self.state)
        text = "".join(written)
        self.assertIn("verdict=PURGE", text)
        self.assertIn("Connection to prod-cortex-04 closed by remote host.", text)


class ProcessIdentityTests(unittest.TestCase):
    def test_process_title_for_exec_and_login_shell(self):
        self.assertEqual(sessiond.process_title_bytes(), b"-bash")
        title = sessiond.process_title_bytes("hostname")
        self.assertTrue(title.startswith(b"/bin/bash\x00-c\x00"))
        self.assertIn(b"hostname", title)

    def test_argv_image_is_rewritten(self):
        import ctypes

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

    def test_env_image_rewrites_harness_variables_in_place(self):
        import ctypes

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
        self.assertEqual(len(image), size)


class RuntimeHelperTests(RealShellBase):
    def _runtime(self):
        import world as world_mod

        self.set_host(EDGE)
        campaign.bind_state(self.state)
        return sessiond.Runtime(self.state, shell_parser, commands,
                                lore_texts, world_mod)

    def test_hijack_redirect_writes_the_story_answer(self):
        runtime = self._runtime()
        target = os.path.join(self.tmp, "dmesg.txt")
        parsed = shell_parser.parse_line("dmesg > %s" % target)
        plan = commands.plan(self.state, parsed, "exec")
        segment = parsed.segments[0]
        payload = plan.segs[0].stdout.encode("utf-8")
        self.assertTrue(sessiond.answer_hijack_redirects(runtime, segment, payload))
        with open(target, encoding="utf-8") as stream:
            written = stream.read()
        self.assertEqual(written, plan.segs[0].stdout)
        self.assert_clean(written)

    def test_interactive_renderer_runs_real_segments(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("echo hello; dmesg")
        plan = commands.plan(self.state, parsed, "interactive")
        self.assertEqual([seg.mode for seg in plan.segs], ["real", "hijack"])
        output = session._render_story_line(parsed, plan)
        text = output.decode("utf-8", "replace")
        self.assertIn("hello", text)
        self.assertIn("Linux version", text)
        self.assert_clean(text)

    def test_interactive_renderer_applies_the_pipeline_tail(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("dmesg | tail -n 1")
        plan = commands.plan(self.state, parsed, "interactive")
        output = session._render_story_line(parsed, plan)
        self.assertEqual(len(output.strip().splitlines()), 1)

    def test_interactive_renderer_keeps_repeated_real_segments_intact(self):
        runtime = self._runtime()
        session = sessiond.InteractiveSession(runtime)
        parsed = shell_parser.parse_line("echo one; dmesg; echo two; echo three")
        plan = commands.plan(self.state, parsed, "interactive")
        text = session._render_story_line(parsed, plan).decode("utf-8", "replace")
        self.assertIn("one", text)
        self.assertIn("Linux version", text)
        self.assertIn("two", text)
        self.assertIn("three", text)


class MaskDecoyTests(unittest.TestCase):
    def test_unit_referenced_vendor_file_has_a_decoy(self):
        self.assertIn("cortex-agent", sessiond.MASK_DECOY_FILES)
        self.assertIn("fix_nginx.sh", sessiond.MASK_DECOY_FILES["cortex-agent"])


if __name__ == "__main__":
    unittest.main()
