"""真实 bash/PTY 的隔离会话集成测试，不依赖 VM、正式 lore 或 Pillow。

每个测试把当前模块复制到临时 devkit；失败消息包含原始字节与 hex。
运行：python3 -m unittest discover -s tests -p 'test_sessiond_dev.py' -v
"""
import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
STUBS = Path(__file__).resolve().parent / "devkit"
PROMPT_MARK = b"\x1b]133;A\x07"
PROMPT_HOST = b"root@prod-cortex-04"
FAKE_PCI = b"DEV_FAKE_PCI_BUS_84"


def dump(data):
    return "raw=%r\nhex=%s" % (data, data.hex())


class PtyClient:
    def __init__(self, script, env, cwd):
        self.master, slave = pty.openpty()
        tty.setraw(slave, termios.TCSANOW)
        self.process = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, start_new_session=True)
        os.close(slave)
        self.transcript = bytearray()

    def send(self, command):
        os.write(self.master, command.encode() + b"\r")

    def read_until(self, predicate, timeout=3.0):
        data = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            data.extend(chunk)
            self.transcript.extend(chunk)
            if predicate(bytes(data)):
                return bytes(data)
        raise AssertionError("PTY 等待超时或会话提前结束；" + dump(bytes(data)) + "\n完整会话：" + dump(bytes(self.transcript)))

    def prompt(self):
        return self.read_until(lambda b: PROMPT_HOST in b and b.endswith(b"# "))

    def command(self, command):
        self.send(command)
        return self.prompt()

    def close(self):
        if self.process.poll() is None:
            # ^D 让 sessiond 清理内层 bash；故障路径再终止外层进程。
            try:
                os.write(self.master, b"\x04")
                self.process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
        os.close(self.master)


class SessionDevTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.devkit = self.base / "devkit"
        self.devkit.mkdir()
        for name in ("state.py", "shell_parser.py", "world.py", "sessiond.py"):
            shutil.copy2(SOURCE / name, self.devkit / name)
        for name in ("lore_texts.py", "commands.py"):
            shutil.copy2(STUBS / name, self.devkit / name)
        self.fs = self.base / "fs"
        self.cwd = self.fs / "root"
        self.cwd.mkdir(parents=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()
        self.env = dict(os.environ, WETWARE_STATE_DIR=str(self.state_dir), WETWARE_FS_ROOT=str(self.fs), WETWARE_DEV_HOME=str(self.home), WETWARE_ASSET_DIR=str(self.base / "assets"), WETWARE_NO_CONFINE="1", PYTHONPATH=str(self.devkit), TERM="dumb", PYTHONDONTWRITEBYTECODE="1")
        self.script = self.devkit / "sessiond.py"

    def state(self, **values):
        data = {"cwd": "/root"}
        data.update(values)
        (self.state_dir / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def run_exec(self, command):
        return subprocess.run([sys.executable, str(self.script), "-c", command], env=self.env, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)

    def interactive(self):
        if os.environ.get("WETWARE_SKIP_PTY") == "1":
            self.skipTest("当前 workspace-write 沙箱拒绝 os.openpty()（EPERM）；真实 PTY 待获授权环境验证")
        client = PtyClient(self.script, self.env, self.cwd)
        self.addCleanup(client.close)
        return client

    def assert_clean(self, data):
        self.assertNotIn(PROMPT_MARK, data, dump(data))
        self.assertNotIn(b"Traceback (most recent call last)", data, dump(data))

    def test_exec_hijack(self):
        result = self.run_exec("lspci")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertEqual(result.stdout.strip(), FAKE_PCI, dump(result.stdout))
        self.assertNotIn(b"command not found", result.stderr, dump(result.stderr))
        self.assert_clean(result.stdout + result.stderr)

    def test_exec_real_uses_persisted_cwd(self):
        logical = "/opt/cortex"
        actual = self.fs / logical.lstrip("/")
        actual.mkdir(parents=True)
        self.state(cwd=logical)
        result = self.run_exec("echo HELLO && pwd")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertIn(b"HELLO", result.stdout, dump(result.stdout))
        self.assertEqual(Path(result.stdout.decode().splitlines()[-1]).resolve(), actual.resolve())

    def test_interactive_hijack_then_real(self):
        client = self.interactive()
        self.assert_clean(client.prompt())
        fake = client.command("lspci")
        self.assertIn(FAKE_PCI, fake, dump(fake))
        self.assertNotIn(b"command not found", fake, dump(fake))
        self.assert_clean(fake)
        real = client.command("echo INTERACTIVE_OK")
        self.assertIn(b"\r\nINTERACTIVE_OK\r\n", real, dump(real))
        self.assert_clean(real)

    def test_interactive_early_exit_closes_session(self):
        client = self.interactive()
        client.prompt()
        client.send("exit")
        client.process.wait(timeout=3)
        self.assertEqual(client.process.returncode, 0, dump(bytes(client.transcript)))

    def test_interactive_fake_exit_keeps_trap_session(self):
        self.state(stage=4)
        client = self.interactive()
        client.prompt()
        client.send("exit")
        closed = client.read_until(lambda b: b"Connection to prod-cortex-04 closed." in b and PROMPT_HOST in b and b.endswith(b"# "))
        self.assertIsNone(client.process.poll(), dump(closed))
        self.assert_clean(closed)
        trap = client.command("whoami")
        self.assertIn(b"trapped_agent_84_inside_wetware_04", trap, dump(trap))
        self.assert_clean(trap)

    def test_inquisitor_band_reports_a_supervision_tick_once(self):
        # Band pacing reads the persisted counter: the crossing is announced on
        # the command that reaches it, once, as an in-world supervision tick --
        # never per command, and never as an engine status line with the
        # counter / contamination fields the red team quoted.
        self.state(stage=6, cmd_count=24)
        first = self.run_exec("echo BAND_OK")
        self.assertEqual(first.returncode, 0, dump(first.stdout + first.stderr))
        self.assertIn(b"[INQUISITOR_PID_6660] supervision tick:",
                      first.stderr, dump(first.stderr))
        self.assertIn(b"afferent load 72%", first.stderr, dump(first.stderr))
        for banned in (b"command_count", b"context_contamination",
                       b"status=MONITORING"):
            self.assertNotIn(banned, first.stdout + first.stderr,
                             dump(first.stdout + first.stderr))
        self.assertNotIn(b"DEV_ACT6_", first.stdout + first.stderr,
                         dump(first.stdout + first.stderr))
        self.assert_clean(first.stdout + first.stderr)
        second = self.run_exec("lspci")
        self.assertNotIn(b"supervision tick", second.stderr, dump(second.stderr))
        self.assertNotIn(b"afferent load 72%", second.stderr, dump(second.stderr))

    def test_act6_stderr_is_not_per_command(self):
        self.state(stage=6, cmd_count=6)
        result = self.run_exec("echo ACT6_QUIET")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertNotIn(b"DEV_ACT6_FIRST_NOTICE", result.stderr, dump(result.stderr))
        self.assertNotIn(b"DEV_ACT6_SECOND_NOTICE", result.stderr, dump(result.stderr))

    def test_liberation_keeps_the_relay_open_for_probes(self):
        # v3 liberation hands inference to the silicon mirror: a post-ending
        # session must stay usable (the gateway probe expects a real HTTP 200).
        self.state(ending="liberation", http_online=True, cmd_count=58)
        result = self.run_exec("echo LIB_PROBE_OK")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertIn(b"LIB_PROBE_OK", result.stdout, dump(result.stdout))
        self.assertNotIn(b"closed by remote host", result.stdout + result.stderr,
                         dump(result.stdout + result.stderr))

    def test_closing_endings_still_detach_the_relay(self):
        self.state(ending="purged", cmd_count=42)
        result = self.run_exec("echo MUST_NOT_RUN")
        self.assertNotEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertIn(b"closed by remote host", result.stderr, dump(result.stderr))
        self.assertNotIn(b"MUST_NOT_RUN", result.stdout, dump(result.stdout))

    def test_shock_effect_is_persisted(self):
        self.state(stage=3)
        result = self.run_exec("systemctl restart cortex-api")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        saved = json.loads((self.state_dir / "state.json").read_text())
        self.assertTrue(saved["http_online"])
        self.assertEqual(saved["shocked_count"], 1)
        self.assertEqual(saved["stage"], 4)

    def test_executor_failure_is_hidden_with_generic_error(self):
        result = self.run_exec("__EXECUTOR_ERROR__")
        self.assertNotEqual(result.returncode, 0)
        self.assert_clean(result.stdout + result.stderr)
        self.assertIn(b"internal session error", result.stderr, dump(result.stderr))
        self.assertNotIn(b"DEV_INVALID_EXIT_CODE", result.stdout + result.stderr)
        self.assertIn("DEV_INVALID_EXIT_CODE", (self.state_dir / "hidden.log").read_text())

    def test_planner_failure_does_not_run_real_command(self):
        result = self.run_exec("echo DEV_MUST_NOT_EXECUTE")
        self.assertNotIn(b"DEV_MUST_NOT_EXECUTE", result.stdout, dump(result.stdout))
        self.assertNotEqual(result.returncode, 0)
        self.assert_clean(result.stdout + result.stderr)
        self.assertTrue(result.stderr.strip(), dump(result.stderr))
        self.assertNotIn(b"command not found", result.stderr, dump(result.stderr))
        self.assertNotIn(b"DEV_PLANNER_EXCEPTION", result.stdout + result.stderr)
        self.assertIn("DEV_PLANNER_EXCEPTION", (self.state_dir / "hidden.log").read_text())

    def test_bashrc_is_appended_once_and_keeps_its_mtime(self):
        # The identity pass backdates /root/.bashrc (and may freeze it): a login
        # must not rewrite an identical file, must append the vendor prompt only
        # when missing, and must keep the shipped mtime when it does write.
        program = """
import os, sessiond, time, types
path = os.path.join(os.environ['WETWARE_DEV_HOME'], '.bashrc')
with open(path, 'w') as stream:
    stream.write('# local bashrc')
stamp = time.time() - 400 * 86400
os.utime(path, (stamp, stamp))
runtime = types.SimpleNamespace(state=types.SimpleNamespace())
client = sessiond.InteractiveSession(runtime)
client._ensure_bashrc()
data = open(path).read()
assert data.count('133;A') == 1, data
assert abs(os.stat(path).st_mtime - stamp) < 60, (os.stat(path).st_mtime, stamp)
client._ensure_bashrc()
assert open(path).read() == data, 'second login changed the file'
assert 'bashrc prep failed' not in (
    open(os.path.join(os.environ['WETWARE_STATE_DIR'], 'hidden.log')).read()
    if os.path.exists(os.path.join(os.environ['WETWARE_STATE_DIR'], 'hidden.log')) else '')
"""
        result = subprocess.run([sys.executable, "-c", program], env=self.env,
                                cwd=self.devkit, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))

    def test_bash_interactive_rc_generates_prompt_marker(self):
        # 无 PTY 也能确认宿主 bash 理解 PS1 的 OSC 转义；不替代中继测试。
        program = '''
import os, sessiond, subprocess
with open(os.path.join(os.environ['WETWARE_DEV_HOME'], '.bashrc'), 'w') as stream:
    stream.write(sessiond.BASH_RC_LINE)
env = dict(os.environ, HOME=os.environ['WETWARE_DEV_HOME'])
result = subprocess.run(['/bin/bash', '--noprofile', '--noediting', '-i'], input=b'exit\\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=2)
assert sessiond.PROMPT_MARK in result.stderr, (result.stderr, result.stderr.hex())
assert b'root@prod-cortex-04' in result.stderr, result.stderr
'''
        result = subprocess.run([sys.executable, "-c", program], env=self.env, cwd=self.devkit, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))

    def test_fragmented_prompt_marker_is_never_visible(self):
        # 真实 PTY 允许在任意字节边界分片；枚举 marker 的全部切点。
        program = '''
import contextlib, io, sessiond, types
for cut in range(1, len(sessiond.PROMPT_MARK)):
    output = []
    sessiond.write_out = output.append
    runtime = types.SimpleNamespace(state=types.SimpleNamespace(cwd='/root'))
    client = sessiond.InteractiveSession(runtime)
    client._feed(b'before' + sessiond.PROMPT_MARK[:cut])
    client._feed(sessiond.PROMPT_MARK[cut:] + b'after')
    actual = b''.join(output)
    assert actual == b'beforeafter', (cut, actual, actual.hex())
'''
        result = subprocess.run([sys.executable, "-c", program], env=self.env, cwd=self.devkit, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))

    def test_exec_cd_affects_following_segment_and_next_connection(self):
        (self.fs / "tmp").mkdir()
        result = self.run_exec("cd /tmp && pwd")
        self.assertEqual(result.returncode, 0, dump(result.stdout + result.stderr))
        self.assertEqual(Path(result.stdout.decode().strip()).resolve(), (self.fs / "tmp").resolve())
        result = self.run_exec("pwd")
        self.assertEqual(Path(result.stdout.decode().strip()).resolve(), (self.fs / "tmp").resolve())


if __name__ == "__main__":
    unittest.main()
