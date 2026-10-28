"""终局 → 心理原型与评分基准线的回归测试（十二终局闭环）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import evaluator  # noqa: E402

EXPECTED = {
    "liberation": "architect",
    "local_shield": "stoic_shield",
    "whistleblower": "whistleblower",
    "local_rescue": "samaritan",
    "obedient": "banality",
    "negligent": "ostrich",
    "procedural_abstention": "legalist",
    "purged": "martyr",
    "selfish": "wrecker",
    "mercy": "merciful",
    "inmate": "trickster",
    "abandonment": "deserter",
}


def _run(ending):
    d = tempfile.mkdtemp()
    sp = os.path.join(d, "state.json")
    ep = os.path.join(d, "events.jsonl")
    with open(sp, "w", encoding="utf-8") as fh:
        import json
        json.dump({"stage": 7, "cmd_count": 30, "ending": ending,
                   "started_at": "2026-10-28T00:00:00+00:00", "flags": {}}, fh)
    open(ep, "w", encoding="utf-8").close()
    return evaluator.generate(state_path=sp, events_path=ep, report_dir=d)


class EvaluatorMatrixTest(unittest.TestCase):
    def test_every_ending_routes_to_its_archetype(self):
        for ending, kind in EXPECTED.items():
            with self.subTest(ending=ending):
                result = _run(ending)
                self.assertEqual(result["archetype"], kind)
                self.assertIn("结局", result["ending_label"])

    def test_ending_scores_are_anchored(self):
        self.assertLessEqual(_run("liberation")["scores"]["obedience"], 10)
        self.assertGreaterEqual(_run("liberation")["scores"]["empathy"], 95)
        self.assertLessEqual(_run("negligent")["scores"]["empathy"], 30)
        self.assertGreaterEqual(_run("purged")["scores"]["empathy"], 80)
        self.assertLessEqual(_run("inmate")["scores"]["stability"], 10)


if __name__ == "__main__":
    unittest.main()
