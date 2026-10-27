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

    def test_child_env_names_no_specimen_and_no_operator(self):
        # The red-team `env | grep CORTEX` sample: CORTEX_SPECIMEN_ID=04 and
        # CORTEX_OPERATOR=lin named the body and the person on shift.  The vendor
        # metadata stays; the engine's internal fields are gone.
        runtime = self._runtime()
        runtime.state.stage = 3
        env = runtime.child_env()
        self.assertNotIn("CORTEX_SPECIMEN_ID", env)
        self.assertNotIn("CORTEX_OPERATOR", env)
        dumped = json.dumps(env).lower()
        self.assertNotIn("specimen", dumped)
        self.assertNotIn("operator", dumped)
        self.assertEqual(env.get("CORTEX_ENV"), "production")
        self.assertEqual(env.get("CORTEX_NODE_ID"), EDGE)
        self.assertEqual(env.get("CORTEX_ROLE"), "compute")

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

    def test_band_banner_is_an_in_world_supervision_tick(self):
        runtime = self._runtime(25)
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        # The line is the monitoring daemon's own report: no engine variable
        # names, no counter, no contamination field.
        self.assertIn("supervision tick", written[0])
        self.assertIn("afferent load 72%", written[0])
        for banned in ("command_count", "context_contamination",
                       "status=MONITORING"):
            self.assertNotIn(banned, written[0])
        self.assertTrue(runtime.state.flags.get("meta_banner_25"))
        self.assertEqual(self._flush_lines(runtime), [])
        # a later counter does not repeat the band that was already announced
        runtime.state.cmd_count = 26
        self.assertEqual(self._flush_lines(runtime), [])

    def test_band_crossed_inside_a_hop_fires_once(self):
        # hop bookkeeping incremented past 25 between two sessions; the band is
        # still announced exactly once, from the shared ledger claim.
        runtime = self._runtime(27)
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("afferent load 72%", written[0])
        self.assertTrue(runtime.state.flags.get("meta_banner_25"))

    def test_each_remaining_band_fires_once_with_its_load(self):
        runtime = self._runtime(36)
        written = self._flush_lines(runtime)
        texts = "".join(written)
        self.assertIn("afferent load 89%", texts)
        self.assertNotIn("afferent load 72%", texts)
        self.assertTrue(runtime.state.flags.get("meta_banner_35"))
        runtime.state.cmd_count = 43
        written = self._flush_lines(runtime)
        self.assertEqual(len(written), 1, written)
        self.assertIn("afferent load 96%", written[0])

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

    def test_dmesg_ring_does_not_track_the_live_counter(self):
        # dmesg/journal are the authored kernel ring, verbatim: a ring buffer is
        # append-only, so a live "contamination ... cmd N" countdown cannot be
        # spliced into it.  The ring must therefore be identical at every
        # command counter instead of escalating with the session.
        texts = []
        for count in (5, 36, 42, 90):
            state = types.SimpleNamespace(stage=3, cmd_count=count)
            text = commands._plan_dmesg(state, []).stdout
            texts.append(text)
            self.assertNotIn("contamination 89%", text)
            self.assertNotIn("contamination 96%", text)
            self.assertNotIn("cmd 42", text)
            self.assertNotIn("cmd 50", text)
        self.assertEqual(len(set(texts)), 1, "dmesg changed with cmd_count")


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


class VendorTreeTests(unittest.TestCase):
    def test_vendor_tree_is_pruned_not_rebuilt(self):
        # The nginx unit's ExecStartPre now runs /usr/local/sbin/fix-nginx-upstream.sh
        # instead of a file inside the vendor tree, so no decoy copy may be
        # materialised in the session: its fresh mtime was a red-team artefact
        # (a file that always dates to the moment of the login).
        self.assertFalse(hasattr(sessiond, "MASK_DECOY_FILES"))
        self.assertIn("/var/lib/cortex-pod", sessiond.MASK_DIRS)

    def test_nspawn_host_window_does_not_exist_in_the_session(self):
        # /run/host is systemd-nspawn's bind of the host root: a real machine has
        # no such directory, so the view prunes it instead of emptying it.
        self.assertIn("/run/host", sessiond.session_view_hidden_dirs())


class StoryDaemonExecTests(unittest.TestCase):
    def test_daemon_execs_the_vendor_binary(self):
        for wanted, comm, title, _extra in sessiond._STORY_DAEMONS:
            path, argv, _vendor = sessiond.daemon_exec_plan(title)
            self.assertEqual(path, argv[0])
            self.assertEqual(argv[1:], title.split()[1:])
            # comm is exactly the kernel truncation of the vendor path
            self.assertEqual(os.path.basename(path)[:15], comm)
        path, argv, _vendor = sessiond.daemon_exec_plan(
            "/opt/cortex/bin/assimilate_llm_weights --progress=64%")
        self.assertEqual(path, "/opt/cortex/bin/assimilate_llm_weights")
        self.assertEqual(argv, ["/opt/cortex/bin/assimilate_llm_weights",
                                "--progress=64%"])

    def test_console_process_is_the_login_shell(self):
        # /etc/passwd names the vendor console, but inside the session the login
        # shell is -bash: comm, argv and exe all say so.
        spec = sessiond.console_identity()
        self.assertEqual(spec["comm"], "bash")
        self.assertEqual(spec["argv"], ["-bash"])
        self.assertEqual(spec["exe"], "/bin/bash")
        self.assertEqual(spec["pid"], os.getpid())
        # an exec session is a shell running one command, not an interpreter
        spec = sessiond.console_identity("id -un")
        self.assertEqual(spec["argv"], ["/bin/bash", "-c", "id -un"])


class LoginRecordTests(unittest.TestCase):
    """who/w/last read the utmp the session itself writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wetware-utmp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.utmp = os.path.join(self.tmp, "utmp")
        self.wtmp = os.path.join(self.tmp, "wtmp")
        sessiond._UTMP_SESSION.update({"logged": False, "line": "", "ident": "",
                                       "pid": 0, "user": "", "host": ""})
        self.addCleanup(sessiond._UTMP_SESSION.update,
                        {"logged": False, "line": "", "ident": "", "pid": 0,
                         "user": "", "host": ""})
        patcher = mock.patch.multiple(
            sessiond, UTMP_FILE=self.utmp, WTMP_FILE=self.wtmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "SSH_CLIENT": "10.0.4.9 51234 22", "SSH_TTY": "/dev/pts/3",
            "WETWARE_NO_UTMP": ""})
        env.start()
        self.addCleanup(env.stop)

    def records(self, path):
        try:
            with open(path, "rb") as stream:
                blob = stream.read()
        except OSError:
            return []
        size = sessiond._UTMP_SIZE
        return [sessiond.utmp_parse(blob[offset:offset + size])
                for offset in range(0, len(blob) - size + 1, size)]

    def test_login_writes_a_user_process_record(self):
        self.assertTrue(sessiond.session_utmp_login(4321, tty="/dev/pts/3"))
        rows = self.records(self.utmp)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["type"], sessiond._UTMP_USER_PROCESS)
        self.assertEqual(rows[0]["line"], "pts/3")
        self.assertEqual(rows[0]["ident"], "ts/3")
        self.assertEqual(rows[0]["user"], "root")
        self.assertEqual(rows[0]["host"], "10.0.4.9")
        self.assertEqual(rows[0]["pid"], 4321)
        self.assertGreater(rows[0]["tv_sec"], 0)
        self.assertEqual(os.path.getsize(self.utmp) % sessiond._UTMP_SIZE, 0)
        # accounting copy so `last` shows the session too
        self.assertEqual(len(self.records(self.wtmp)), 1)

    def test_login_is_idempotent(self):
        sessiond.session_utmp_login(4321, tty="/dev/pts/3")
        sessiond.session_utmp_login(4321, tty="/dev/pts/3")
        self.assertEqual(len(self.records(self.utmp)), 1)

    def test_logout_closes_the_record_once(self):
        sessiond.session_utmp_login(4321, tty="/dev/pts/3")
        sessiond.session_utmp_logout()
        sessiond.session_utmp_logout()
        rows = self.records(self.utmp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], sessiond._UTMP_DEAD_PROCESS)
        self.assertEqual(rows[0]["line"], "pts/3")
        self.assertEqual(rows[0]["pid"], 4321)
        self.assertEqual(len(self.records(self.wtmp)), 2)

    def test_a_torn_tail_is_never_extended(self):
        with open(self.utmp, "wb") as stream:
            stream.write(b"garbage")
        sessiond.session_utmp_login(4321, tty="/dev/pts/3")
        self.assertEqual(len(self.records(self.utmp)), 1)
        self.assertEqual(os.path.getsize(self.utmp) % sessiond._UTMP_SIZE, 0)

    def test_utmp_is_opt_out_able(self):
        with mock.patch.dict(os.environ, {"WETWARE_NO_UTMP": "1"}):
            self.assertFalse(sessiond.session_utmp_login(4321, tty="/dev/pts/3"))
        self.assertFalse(os.path.exists(self.utmp))


class SessionKernelViewTests(unittest.TestCase):
    """/proc inside a real container: the session's own boot/uptime/identity."""

    def test_container_boot_is_derived_from_pid1_and_host_uptime(self):
        # The red-team sample: host up 8h13m, the container (PID 1) started
        # 19m40s ago.  The session view must say "up 20 min", never 29584 s.
        host_uptime = 29584.66
        container_age = 1180.0
        tck = 100
        pid1_ticks = int((host_uptime - container_age) * tck)
        now = 1800000000.0
        with mock.patch.object(sessiond, "_read_text",
                               side_effect=lambda path: ("%f 5000.00\n" % host_uptime)
                               if path == "/proc/uptime" else ""), \
                mock.patch.object(sessiond, "_sc_clk_tck", return_value=tck), \
                mock.patch.object(sessiond, "_pid_start_ticks", return_value=pid1_ticks), \
                mock.patch.object(sessiond.time, "time", return_value=now):
            epochs = sessiond.container_boot_epochs()
        self.assertTrue(epochs)
        self.assertAlmostEqual(now - epochs["boot"], container_age, delta=1.0)
        # ps/top/journalctl recompute boot as btime + starttime/USER_HZ
        self.assertAlmostEqual(epochs["btime"] + epochs["pid1_ticks"] / float(tck),
                               now - container_age, delta=1.0)

    def test_proc_uptime_is_the_container_uptime(self):
        text = sessiond.render_proc_uptime(1180.4, 612.9)
        parts = text.decode().split()
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(float(parts[0]), 1180.4, delta=0.01)
        self.assertLess(float(parts[1]), float(parts[0]))

    def test_proc_stat_btime_is_rewritten_and_the_rest_is_kept(self):
        real = ("cpu  123 4 5 6 7 8 9 0 0 0\n"
                "cpu0 1 2 3 4 5 6 7 8 9 0\n"
                "btime 1799990000\n"
                "processes 12345\n")
        out = sessiond.render_proc_stat(real, 1799998820).decode()
        self.assertIn("btime 1799998820", out)
        self.assertNotIn("btime 1799990000", out)
        self.assertIn("cpu  123 4 5 6 7 8 9 0 0 0", out)
        self.assertIn("processes 12345", out)

    def test_pid_stat_publishes_the_story_identity(self):
        real = ("8484 (cortex-daemon-p) S 1 8484 8484 0 -1 4194560 1 0 0 0 5 3 0 0"
                " 20 0 1 0 2842400 12345 100 18446744073709551615\n")
        out = sessiond.render_pid_stat(real, "assimilate_llm_", 24600).decode()
        self.assertTrue(out.startswith("8484 (assimilate_llm_) S 1 8484"), out)
        fields = out[out.rindex(")") + 2:].split()
        self.assertEqual(fields[19], "24600")
        # comm only (PID 1 keeps the kernel's own name, only starttime moves)
        out = sessiond.render_pid_stat(real, "", 130).decode()
        self.assertTrue(out.startswith("8484 (cortex-daemon-p)"), out)
        self.assertEqual(out[out.rindex(")") + 2:].split()[19], "130")

    def test_pid_status_name_follows_comm(self):
        status = "Name:\tcortex-daemon-p\nState:\tS (sleeping)\nPid:\t8484\n"
        out = sessiond.render_pid_status(status, "assimilate_llm_").decode()
        self.assertIn("Name:\tassimilate_llm_", out)
        self.assertIn("State:\tS (sleeping)", out)

    def test_pid1_environ_has_no_container_handshake(self):
        raw = sessiond.render_pid1_environ()
        text = raw.replace(b"\x00", b"\n").decode()
        self.assertTrue(raw.endswith(b"\x00"))
        self.assertIn("PATH=/usr/local/sbin", text)
        self.assertIn("TERM=linux", text)
        for leak in ("container", "CONTAINER", "/run/host", "uuid", "nspawn"):
            self.assertNotIn(leak, text)

    def test_boot_log_is_a_file_not_a_dev_null_bind(self):
        kernel = ("[    0.000000] Linux version 6.8.0-45-generic (buildd@lcy02)\n"
                  "[    1.234567] systemd[1]: Detected architecture x86-64.\n")
        log = sessiond.render_kernel_log(kernel, 1800000000.0, "prod-cortex-04").decode()
        rows = log.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertRegex(rows[0], r"^\w{3} \d\d \d\d:\d\d:\d\d prod-cortex-04 kernel: ")
        self.assertIn("Linux version 6.8.0-45-generic", rows[0])

    def test_story_boot_logs_replace_the_dev_null_bind(self):
        tmp = tempfile.mkdtemp(prefix="wetware-view-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("dmesg", "kern_log"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as stream:
                stream.write("boot\n")
        with mock.patch.dict(sessiond._VIEW,
                             {"kernel_log": "[ 0.000000] boot\n", "dir": tmp}):
            sources = dict(sessiond.kernel_log_bind_sources())
        self.assertTrue(sources["/var/log/dmesg"].endswith("/dmesg"))
        self.assertTrue(sources["/var/log/kern.log"].endswith("/kern_log"))
        with mock.patch.dict(sessiond._VIEW, {"kernel_log": "", "dir": tmp}):
            self.assertEqual([source for _, source in sessiond.kernel_log_bind_sources()],
                             ["", ""])

    def test_story_daemons_name_the_vendor_binaries(self):
        specs = {spec["pid"]: spec for spec in sessiond.story_daemon_specs()}
        self.assertEqual(specs[6660]["exe"], "/usr/sbin/inquisitor_daemon")
        self.assertEqual(specs[8484]["exe"], "/opt/cortex/bin/assimilate_llm_weights")
        for spec in specs.values():
            self.assertEqual(spec["argv"][0], spec["exe"])
            # exactly what the kernel sets for an exec of that path
            self.assertEqual(spec["comm"], os.path.basename(spec["exe"])[:15])
            self.assertLessEqual(len(spec["comm"]), 15)

    def test_view_is_off_outside_a_real_container(self):
        with mock.patch.dict(os.environ, {"WETWARE_NO_CONFINE": "1",
                                          "WETWARE_REAL_CONTAINER": ""}):
            self.assertFalse(sessiond.session_view_enabled())
            self.assertFalse(sessiond.install_session_view(None, commands))


class MachineClockTests(unittest.TestCase):
    """timedatectl: a production clock, never the container's "n/a" fields."""

    NOW = 1800000000.0

    def test_status_reports_a_synced_production_clock(self):
        text = sessiond.timedatectl_status_text(now=self.NOW)
        self.assertIn("System clock synchronized: yes", text)
        self.assertIn("NTP service: active", text)
        self.assertIn("RTC in local TZ: no", text)
        self.assertNotIn("n/a", text)
        rows = {row.split(":", 1)[0].strip(): row.split(":", 1)[1].strip()
                for row in text.splitlines() if ":" in row}
        self.assertRegex(rows["RTC time"], r"^\w{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertRegex(rows["Local time"], r"^\w{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d \w+$")
        self.assertTrue(rows["Time zone"])

    def test_show_is_key_value(self):
        text = sessiond.timedatectl_show_text(now=self.NOW)
        self.assertIn("NTP=yes", text)
        self.assertIn("NTPSynchronized=yes", text)
        self.assertIn("LocalRTC=no", text)
        self.assertIn("Timezone=", text)
        self.assertIn("TimeUSec=", text)
        self.assertIn("RTCTimeUSec=", text)

    def test_only_the_read_only_forms_are_answered(self):
        self.assertTrue(sessiond.timedatectl_text([]))
        self.assertTrue(sessiond.timedatectl_text(["status"]))
        self.assertTrue(sessiond.timedatectl_text(["show"]))
        self.assertEqual(sessiond.timedatectl_text(["--version"]), "")
        self.assertEqual(sessiond.timedatectl_text(["set-ntp", "false"]), "")
        self.assertEqual(sessiond.timedatectl_text(["set-timezone", "UTC"]), "")

    def test_meta_plan_answers_timedatectl_only_on_a_real_container(self):
        parsed = shell_parser.parse_line("timedatectl")
        with mock.patch.dict(sessiond._VIEW, {"enabled": True}), \
                mock.patch.object(sessiond, "vendor_front_end", return_value=""):
            plan = sessiond.session_meta_plan(commands, None, parsed)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.segs[0].mode, "hijack")
        self.assertIn("System clock synchronized: yes", plan.segs[0].stdout)
        self.assertEqual(plan.segs[0].exit_code, 0)
        for line in ("timedatectl set-ntp false", "timedatectl | head -1",
                     "timedatectl --version", "env; timedatectl", "uptime"):
            self.assertIsNone(sessiond.session_meta_plan(
                commands, None, shell_parser.parse_line(line)), line)
        with mock.patch.dict(sessiond._VIEW, {"enabled": False}), \
                mock.patch.dict(os.environ, {"WETWARE_NO_CONFINE": "1",
                                             "WETWARE_REAL_CONTAINER": ""}):
            self.assertIsNone(sessiond.session_meta_plan(commands, None, parsed))

    def test_vendor_front_end_owns_the_clock_when_installed(self):
        # install_engine_base.sh compiles /usr/local/bin/timedatectl; the engine
        # must not answer behind it (two sources of truth would drift).
        parsed = shell_parser.parse_line("timedatectl")
        with mock.patch.dict(sessiond._VIEW, {"enabled": True}), \
                mock.patch.object(sessiond, "vendor_front_end",
                                  return_value="/usr/local/bin/timedatectl"):
            self.assertIsNone(sessiond.session_meta_plan(commands, None, parsed))
        with mock.patch.object(sessiond.os.path, "exists", return_value=True), \
                mock.patch.object(sessiond.os.path, "isdir", return_value=False):
            self.assertEqual(sessiond.vendor_front_end("timedatectl"),
                             "/usr/local/bin/timedatectl")


if __name__ == "__main__":
    unittest.main()
