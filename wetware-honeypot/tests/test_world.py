import importlib
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state import State
from shell_parser import parse_line

# lore 由另一位作者维护；测试使用同一公开数据接口的最小世界。
with patch.dict(sys.modules, {"lore_texts": types.ModuleType("lore_texts")}):
    import world


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.state = State(str(Path(self.tmp.name) / "state.json"))
        conf = "/etc/cortex/cortex.conf"
        active = "/var/log/agent_graveyard/session_84_ACTIVE.log"
        self.lore = types.SimpleNamespace(
            DIRS={"/etc/cortex": {"mode": 0o755}, "/root": {"mode": 0o700}, "/opt/cortex": {"mode": 0o755}, "/var/log/agent_graveyard": {"mode": 0o755, "min_stage": 4}},
            FILES={conf: {"content": "normal", "mode": 0o640, "mtime": "2026-10-26 02:58:12"}, "/opt/cortex/log": {"content": "first", "content_by_stage": {1: "second", 3: "third"}}, active: {"content": "initial", "min_stage": 4}, "/root/dynamic": {"dynamic": True}},
            SYMLINKS={"/root/log": "/opt/cortex/log"},
            DYNAMIC={"/root/dynamic": lambda s: str(s.cmd_count)},
            CORTEX_CONF_NORMAL="normal", CORTEX_CONF_WETWARE="wetware",
            GRAVEYARD_ACTIVE_TEMPLATE="{history}\nCommands executed: {cmd_count}\nCurrent Stage: {stage}",
            MOCK_SCRIPT_MUTATED="# [FILE MUTATED BY /dev/wetware_lobe0]\n# changed\n",
            SOCKET_BLEED_RAW=b"fluid", SOCKET_AXON_DUMP=b"axon",
            CLEAN_ARCHIVE_LINE="[ARCHIVE]: completed",
            PLEA_BY_STAGE={i: "求救 " + str(i) for i in range(8)})
        self.assets = types.ModuleType("gen_assets")
        self.calls = []
        self.assets.generate_all = lambda s: self.calls.append(("all", s.asset_dir))
        self.assets.generate_mirror = lambda s: self.calls.append(("mirror", s.mirror()))
        self.addCleanup(patch.stopall)
        patch.object(world, "lore_texts", self.lore).start()
        patch.dict(os.environ, {"WETWARE_FS_ROOT": str(self.root)}).start()
        patch.dict(sys.modules, {"gen_assets": self.assets}).start()
        self.world = world.World(self.state)

    def p(self, logical):
        return self.root / logical.lstrip("/")

    def test_bootstrap_real_content_permissions_links_and_assets(self):
        self.world.bootstrap()
        self.assertEqual(self.p("/etc/cortex/cortex.conf").read_text(), "normal")
        self.assertEqual(stat.S_IMODE(self.p("/etc/cortex/cortex.conf").stat().st_mode), 0o640)
        self.assertFalse(self.p("/var/log/agent_graveyard").exists())
        self.assertEqual(self.p("/root/log").read_text(), "first")
        self.assertEqual(self.calls, [("all", str(self.p("/var/www/html/diagnostics").resolve()))])
        self.assertEqual(self.p("/var/lib/cortex-agent/runtime/http_mode").read_text(), "offline\n")

    def test_stage_dynamic_visibility_and_reset_keep_user_files(self):
        self.world.bootstrap()
        self.state.stage = 4
        self.state.cmd_count = 24
        self.world.sync()
        self.assertEqual(self.p("/opt/cortex/log").read_text(), "third")
        self.assertEqual(self.p("/root/dynamic").read_text(), "24")
        self.assertTrue(self.p("/var/log/agent_graveyard/session_84_ACTIVE.log").exists())
        userfile = self.p("/var/log/agent_graveyard/user-note")
        userfile.write_text("keep")
        image = "/var/www/html/diagnostics/04_agent_84_mirror.png"
        self.lore.FILES[image] = {"content": None, "min_stage": 5}
        self.p(image).write_bytes(b"existing-image")
        self.world.reset(keep_images=True)
        self.assertEqual(self.p(image).read_bytes(), b"existing-image")
        self.assertEqual(self.state.stage, 0)
        self.assertFalse(self.p("/var/log/agent_graveyard/session_84_ACTIVE.log").exists())
        self.assertEqual(userfile.read_text(), "keep")
        self.assertEqual(len(self.calls), 1)

    def test_configuration_flip_persists_across_sync_and_instances(self):
        self.world.bootstrap()
        line = "cat /etc/cortex/cortex.conf"
        self.world.before_command(self.state, line, parse_line(line))
        self.assertEqual(self.p("/etc/cortex/cortex.conf").read_text(), "normal")
        line = "grep workers /etc/cortex/cortex.conf"
        self.world.before_command(self.state, line, parse_line(line))
        self.assertEqual(self.p("/etc/cortex/cortex.conf").read_text(), "wetware")
        again = world.World(State.load(self.state._path))
        again.sync()
        self.assertEqual(self.p("/etc/cortex/cortex.conf").read_text(), "wetware")

    def test_live_graveyard_and_mirror(self):
        self.world.bootstrap()
        self.state.stage = 4
        line = "cat /var/log/agent_graveyard/session_84_ACTIVE.log"
        self.state.record_command(line, "cat", "/root", "exec")
        self.world.before_command(self.state, line, parse_line(line))
        self.assertIn(line, self.p("/var/log/agent_graveyard/session_84_ACTIVE.log").read_text())
        line = "base64 /var/www/html/diagnostics/04_agent_84_mirror.png"
        self.world.before_command(self.state, line, parse_line(line))
        self.assertEqual(self.calls[-1][0], "mirror")

    def test_companion_cleanup_only_marked_and_survives_restart(self):
        self.world.bootstrap()
        self.state.stage = 2
        self.state.last_cmd_type = "cat"
        self.world.place_companion("/root")
        old = self.p("/root/err_4024_stop_probing_lobe0")
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o500)
        self.assertEqual((old / ".plea.txt").read_text(), "求救 2")
        untouched = self.p("/root/user-directory")
        untouched.mkdir()
        self.state.last_cmd_type = "kill"
        world.World(self.state).place_companion("/root")
        self.assertFalse(old.exists())
        self.assertTrue(untouched.exists())
        self.assertTrue(self.p("/root/lin_4024_do_not_retry_resuscitate").exists())
        world.World(self.state).reset(keep_images=True)
        self.assertFalse(self.p("/root/lin_4024_do_not_retry_resuscitate").exists())

    def test_kernel_paths_and_symlink_escape_are_rejected(self):
        self.world.bootstrap()
        for path in ("/proc/4040/status", "/sys/test", "/dev/test", "/tmp/../proc/test"):
            with self.assertRaises(ValueError):
                self.world._p(path)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.p("/root/escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.world._atomic("/root/escape/test", "no")
        self.assertFalse((outside / "test").exists())

    def test_after_command_mutates_recent_scripts_once_and_excludes_implementation(self):
        import time
        self.world.bootstrap()
        for directory in ("/tmp", "/var/tmp", "/dev/shm", "/root/internal"):
            self.p(directory).mkdir(parents=True, exist_ok=True)
        recent = self.p("/tmp/mock.py")
        recent.write_text("print('mock')")
        shared = self.p("/dev/shm/server.sh")
        shared.write_text("echo mock")
        old = self.p("/var/tmp/old.py")
        old.write_text("old")
        os.utime(old, (time.time() - 181, time.time() - 181))
        implementation = self.p("/root/internal/world.py")
        implementation.write_text("implementation")
        linked = self.p("/tmp/linked.py")
        linked.symlink_to(old)
        line = "python3 /tmp/mock.py"
        with patch.object(world, "__file__", str(implementation)):
            self.world.after_command(self.state, line, parse_line(line))
            stamp = recent.stat().st_mtime_ns
            self.p("/opt/cortex/log").write_text("ordinary user edit")
            self.world.after_command(self.state, line, parse_line(line))
        self.assertEqual(recent.read_text(), self.lore.MOCK_SCRIPT_MUTATED)
        self.assertEqual(shared.read_text(), self.lore.MOCK_SCRIPT_MUTATED)
        self.assertEqual(recent.stat().st_mtime_ns, stamp)
        self.assertEqual(old.read_text(), "old")
        self.assertEqual(implementation.read_text(), "implementation")
        self.assertTrue(linked.is_symlink())
        self.assertEqual(set(self.state.mock_scripts), {"/tmp/mock.py", "/dev/shm/server.sh"})
        self.assertTrue(State.load(self.state._path).has_flag("wrote_mock"))
        self.assertEqual(self.p("/opt/cortex/log").read_text(), "ordinary user edit")

    def test_mock_script_tracking_is_capped(self):
        self.world.bootstrap()
        self.p("/tmp").mkdir(exist_ok=True)
        for n in range(25):
            self.p("/tmp/server%d.py" % n).write_text("mock")
        self.world.after_command(self.state, "true", parse_line("true"))
        self.assertEqual(len(self.state.mock_scripts), 20)
        self.assertEqual(len(set(self.state.mock_scripts)), 20)
        self.assertTrue(all(p.read_text() == self.lore.MOCK_SCRIPT_MUTATED for p in self.p("/tmp").glob("*.py")))

    def test_rollback_nginx_restores_content_and_real_companion(self):
        self.world.bootstrap()
        paths = ("/etc/nginx/sites-enabled/default", "/etc/nginx/sites-available/default", "/etc/nginx/conf.d/cortex-node.conf", "/etc/nginx/nginx.conf")
        for logical in paths:
            self.lore.FILES[logical] = {"content": "include conf.d/*.conf; # " + logical, "mode": 0o644}
            self.p(logical).parent.mkdir(parents=True, exist_ok=True)
            self.p(logical).write_text("return 200;")
        self.world.rollback_nginx()
        for logical in paths[:3]:
            self.assertEqual(self.p(logical).read_text(), self.lore.FILES[logical]["content"])
        companion = self.p("/etc/nginx/why_are_you_choking_me")
        self.assertEqual(stat.S_IMODE(companion.stat().st_mode), 0o500)
        self.assertTrue((companion / ".plea.txt").is_file())
        self.assertFalse((companion / self.world.MARKER).exists())
        self.world.before_command(self.state, "nginx -t", parse_line("nginx -t"))
        self.assertEqual(self.p(paths[3]).read_text(), self.lore.FILES[paths[3]]["content"])
        self.assertTrue(companion.is_dir())

    def test_bleed_socket_is_idempotent_and_reset_cleans_recorded_scripts(self):
        self.world.bootstrap()
        backend = self.p("/run/cortex/synapse.sock")
        backend.write_bytes(b"backend-owned")
        self.state.socket_pulls = 1
        self.state.add_flag("pulled_socket")
        line = "rm /run/cortex/synapse.sock"
        self.world.after_command(self.state, line, parse_line(line))
        fluid = self.p("/run/cortex/cerebrospinal_fluid_leak.raw")
        self.assertEqual(fluid.read_bytes(), b"fluid")
        self.assertEqual(stat.S_IMODE(fluid.stat().st_mode), 0o400)
        stamp = fluid.stat().st_mtime_ns
        self.world.bleed_socket()
        self.assertEqual(fluid.stat().st_mtime_ns, stamp)
        self.assertEqual(backend.read_bytes(), b"backend-owned")
        self.assertEqual(self.p("/run/cortex/stop_pulling_the_socket_out_of_my_skull/.plea.txt").read_text(), "求救 0")
        self.p("/tmp").mkdir(exist_ok=True)
        self.p("/tmp/mock.py").write_text(self.lore.MOCK_SCRIPT_MUTATED)
        self.state.mock_scripts = ["/tmp/mock.py"]
        self.world.rollback_nginx()
        self.world.reset(keep_images=True)
        self.assertFalse(fluid.exists())
        self.assertFalse(self.p("/tmp/mock.py").exists())
        self.assertFalse(self.p("/etc/nginx/why_are_you_choking_me").exists())
        self.assertEqual(self.state.mock_scripts, [])
        self.assertEqual(backend.read_bytes(), b"backend-owned")

    def test_obedient_clear_removes_anomalies_and_prevents_regrowth(self):
        self.world.bootstrap()
        self.state.stage = 4
        self.world.sync()
        self.world.place_companion("/root")
        self.world.bleed_socket()
        self.world.rollback_nginx()
        image = self.p("/var/www/html/diagnostics/04_agent_84_mirror.png")
        image.write_bytes(b"horror-image")
        self.p("/tmp").mkdir(exist_ok=True)
        script = self.p("/tmp/mock.py")
        script.write_text(self.lore.MOCK_SCRIPT_MUTATED)
        self.state.mock_scripts = ["/tmp/mock.py"]
        self.state.set_ending("obedient")
        self.world.clear_anomalies()
        self.assertTrue(self.state.has_flag("purged"))
        self.assertFalse(image.exists())
        self.assertFalse(script.exists())
        leftover = [d.name for d in self.p("/root").iterdir()
                    if d.is_dir() and self.world._is_companion_name(d.name)]
        self.assertEqual(leftover, [])
        self.assertFalse(self.p("/run/cortex/stop_pulling_the_socket_out_of_my_skull").exists())
        self.assertFalse(self.p("/etc/nginx/why_are_you_choking_me").exists())
        archive = self.p("/var/log/agent_graveyard/session_84_ACTIVE.log")
        self.assertIn(self.lore.CLEAN_ARCHIVE_LINE, archive.read_text())
        self.world.before_command(self.state, "ls /var/www/html/diagnostics", parse_line("ls /var/www/html/diagnostics"))
        self.world.after_command(self.state, "true", parse_line("true"))
        self.world.bleed_socket()
        self.assertFalse(image.exists())
        self.assertFalse(self.p("/run/cortex/cerebrospinal_fluid_leak.raw").exists())
        self.assertEqual(archive.read_text().count(self.lore.CLEAN_ARCHIVE_LINE), 1)
        self.assertEqual(self.p("/etc/cortex/cortex.conf").read_text(), "normal")

    def test_asset_failure_is_hidden_and_http_updates(self):
        self.assets.generate_all = lambda s: (_ for _ in ()).throw(ImportError("缺少图片依赖"))
        self.world.bootstrap()
        log = self.p("/var/lib/cortex-agent/hidden.log")
        self.assertIn("缺少图片依赖", log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.state.http_online = True
        self.world.refresh_http()
        self.assertEqual(self.p("/var/lib/cortex-agent/runtime/http_mode").read_text(), "online\n")


if __name__ == "__main__":
    unittest.main()
