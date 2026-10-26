import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state import State, STAGE_TITLES


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.state = State(self.path)

    def test_count_boundaries_and_persistence(self):
        boundaries = {4: 1, 9: 2, 15: 3, 22: 4, 29: 5, 37: 6, 44: 7}
        for n in range(1, 46):
            result = self.state.record_command("pwd", "other", "/root", "exec")
            if n in boundaries:
                self.assertEqual(result["stage"], boundaries[n])
                self.assertEqual(result["transition"], STAGE_TITLES[boundaries[n]])
            else:
                self.assertIsNone(result["transition"])
        loaded = State.load(self.path)
        self.assertEqual(loaded.stage, 7)
        self.assertEqual(loaded.cmd_count, 45)
        self.assertEqual(loaded.mirror(2), ["pwd", "pwd"])
        self.assertEqual(len(loaded.act_transitions), 7)

    def test_flag_progression_is_monotonic(self):
        for flag, expected in [("viewed_daemon_log", 1), ("viewed_resuscitate", 2), ("ran_lsusb", 3), ("explored_graveyard", 4), ("read_graveyard", 5)]:
            self.assertTrue(self.state.add_flag(flag))
            self.assertFalse(self.state.add_flag(flag))
            self.state.advance()
            self.assertEqual(self.state.stage, expected)
        self.state.cmd_count = 37
        self.state.advance()
        self.state.add_flag("read_howto")
        self.state.advance()
        self.assertEqual(self.state.stage, 7)
        self.state.cmd_count = 0
        self.state.flags.clear()
        self.assertIsNone(self.state.advance())
        self.assertEqual(self.state.stage, 7)

    def test_classification_and_companion(self):
        self.assertEqual(self.state.classify(["/usr/bin/sudo", "-u", "root", "/bin/cat", "a"]), "cat")
        self.assertEqual(self.state.classify(["/usr/bin/lspci"]), "lspci")
        self.assertEqual(self.state.classify([]), "other")
        self.state.last_cmd_type = "kill"
        self.assertEqual(self.state.companion_name(), "lin_4024_do_not_retry_resuscitate")
        self.state.last_cmd_type = "head"
        self.assertEqual(self.state.companion_name(), "err_4024_stop_probing_lobe0")

    def test_history_cap_and_reset(self):
        self.state.history = [{"cmd": str(i)} for i in range(200)]
        self.state.record_command("latest", "ls", "/opt/cortex", "interactive")
        self.assertEqual(len(self.state.history), 200)
        self.assertEqual(self.state.history[0]["cmd"], "1")
        self.assertEqual(self.state.stage, 1)
        snap = self.state.snapshot()
        snap["history"].clear()
        self.assertTrue(self.state.history)
        self.state.set_ending("mercy")
        self.state.set_ending("obedient")
        self.assertEqual(self.state.ending, "mercy")
        self.state.reset()
        self.assertEqual(State.load(self.path).cmd_count, 0)

    def test_anti_evasion_defaults_and_legacy_state_migration(self):
        Path(self.path).write_text('{"stage": 3, "cmd_count": 16}', encoding="utf-8")
        loaded = State.load(self.path)
        for field in ("mock_attempts", "nginx_tampers", "socket_pulls", "service_stops", "firewall_blocks"):
            self.assertEqual(getattr(loaded, field), 0)
            setattr(loaded, field, 2)
        self.assertEqual(loaded.mock_scripts, [])
        loaded.mock_scripts = ["/tmp/mock.py"]
        loaded.add_flag("ran_mock")
        loaded.save()
        again = State.load(self.path)
        self.assertEqual(again.mock_attempts, 2)
        self.assertEqual(again.mock_scripts, ["/tmp/mock.py"])
        self.assertIn("ran_mock", again.snapshot()["flags"])
        self.assertEqual(again.snapshot()["firewall_blocks"], 2)

    def test_inmate_ending_persists_and_recording_continues(self):
        self.state.http_online = True
        self.state.set_ending("inmate")
        loaded = State.load(self.path)
        self.assertEqual(loaded.ending, "inmate")
        self.assertFalse(loaded.http_online)
        loaded.record_command("pwd", "other", "/root", "exec")
        self.assertEqual(loaded.cmd_count, 1)
        self.assertEqual(loaded.ending, "inmate")

    def test_campaign_ledger_is_not_expired_at_the_single_node_deadline(self):
        # The campaign shares one ledger between three host sessions, so a single
        # operator command is recorded once per hop (edge/core/hive) and a full
        # campaign run legitimately passes the single-node Inquisitor deadline of
        # cmd 50.  Expiring the ledger there would wipe the gate flags mid-run and
        # the hive hop would be denied by the core gate.  Only the single-node
        # line (campaign_v3 off) keeps the stale-ledger heuristic.
        campaign = State(self.path)
        campaign.flags["campaign_v3"] = True
        campaign.flags["gate3_token"] = True
        campaign.cmd_count = 60
        campaign.record_command("pwd", "other", "/root", "exec")
        self.assertEqual(campaign.cmd_count, 61)
        self.assertTrue(campaign.flags.get("gate3_token"))
        self.assertNotIn("prior_run_ledger", campaign.flags)

        single = State(self.path)
        single.cmd_count = 60
        single.record_command("pwd", "other", "/root", "exec")
        self.assertEqual(single.cmd_count, 1)
        self.assertEqual(single.flags.get("prior_run_ledger"), 60)

    def test_corrupt_json_and_unwritable_save_are_silent(self):
        Path(self.path).write_text("{broken", encoding="utf-8")
        self.assertEqual(State.load(self.path).stage, 0)
        self.state._path = self.tmp.name
        self.assertIsNone(self.state.save())


if __name__ == "__main__":
    unittest.main()
