import json
import unittest

from claude_curator.results import ResultError, fill_unstarted, normalize
from claude_curator.suite import INVOCATION_GRADER, load_suite
from tests.helpers import temp_dir, write_tree


def run(passed=True, invoked=True, error=None, cost=0.02, **extra):
    graders = [
        {"name": "outcome", "passed": passed, "weight": 1, "explanation": "", "withOnly": False},
        {"name": INVOCATION_GRADER, "passed": invoked, "weight": 1, "explanation": "", "withOnly": True, "scored": False},
    ]
    return {"score": 1.0 if passed else 0.0, "passed": passed, "turns": 4, "costUsd": cost, "judgeCostUsd": 0.0,
            "durationSeconds": 12.5, "error": error, "tracePath": "", "skippedPaidGraders": False,
            "graders": graders, **extra}


def doc(with_runs, without_runs=None):
    arms = {"with": with_runs}
    if without_runs is not None:
        arms["without"] = without_runs
    return {"schemaVersion": 1, "claudeVersion": "2.1.296", "costUsd": 0.1, "partial": False,
            "cases": [{"name": "c1", "arms": arms, "aggregates": {"score": 1.0, "passRate": 1.0}}]}


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        root = write_tree(temp_dir(self) / "evals", {
            "c1/prompt.md": "hi\n",
            "c1/graders/outcome.md": "---\ntype: regex\npattern: x\n---\n",
            "c1/graders/fired.md": "---\ntype: tool_used\ntool: Skill\n---\n",
        })
        self.suite = load_suite(root)

    def norm(self, d, arms=None):
        return normalize(d, arms=arms or {"with": "original", "without": "none"}, suite=self.suite, source="t.json")

    def test_scores_exclude_invocation_graders_and_map_arms(self):
        records = self.norm(doc([run(True, True), run(False, False)], [run(True, False)]))
        orig = [r for r in records if r.condition == "original"]
        self.assertEqual([r.score for r in orig], [1.0, 0.0])
        self.assertEqual([r.invocation for r in orig], ["observed", "not_observed"])
        none = [r for r in records if r.condition == "none"][0]
        self.assertEqual(none.invocation, "not_applicable")
        self.assertEqual(none.score, 1.0)

    def test_run_that_never_started_is_infrastructure_with_unknown_cost(self):
        failed = {"score": 0, "turns": 0, "costUsd": 0, "judgeCostUsd": 0, "graders": [], "tracePath": "",
                  "error": "invalid case.yaml: graders", "skippedPaidGraders": False}
        record = self.norm(doc([failed]))[0]
        self.assertEqual(record.status, "infra_error")
        self.assertIsNone(record.cost_usd)
        self.assertIsNone(record.score)

    def test_rate_limit_is_infrastructure_but_timeout_is_the_agent(self):
        records = self.norm(doc([run(False, error="API Error: rate limit exceeded"),
                                 run(False, error="timed out after 300s")]))
        self.assertEqual([r.status for r in records], ["infra_error", "agent_limit"])
        self.assertIsNone(records[0].score)
        self.assertEqual(records[1].score, 0.0)

    def test_agent_timeout_wins_over_numbers_that_look_like_status_codes(self):
        records = self.norm(doc([run(False, error="timed out after 500s"), run(False, error="API Error: 503 Service Unavailable")]))
        self.assertEqual([r.status for r in records], ["agent_limit", "infra_error"])

    def test_judge_failure_is_a_grader_error(self):
        broken = run(True)
        broken["graders"][0] = {"name": "outcome", "passed": None, "weight": 1, "explanation": "judge error: 529"}
        self.assertEqual(self.norm(doc([broken]))[0].status, "grader_error")

    def test_skipped_paid_graders_are_not_comparable(self):
        self.assertEqual(self.norm(doc([run(True, skippedPaidGraders=True)]))[0].status, "budget_skipped")

    def test_missing_cost_stays_missing(self):
        r = run(True)
        del r["costUsd"]
        self.assertIsNone(self.norm(doc([r]))[0].cost_usd)

    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(ResultError):
            self.norm({"schemaVersion": 2, "cases": []})

    def test_token_usage_is_read_from_the_trace_when_kept(self):
        trace = temp_dir(self) / "trace.jsonl"
        trace.write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"content": "x"}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 3, "output_tokens": 2}}},
        ]))
        record = self.norm(doc([run(True, tracePath=str(trace))]))[0]
        self.assertEqual(record.tokens["input_tokens"], 13)
        self.assertEqual(record.tokens["cache_read_input_tokens"], 100)
        self.assertIsNone(self.norm(doc([run(True)]))[0].tokens)

    def test_unstarted_planned_runs_are_recorded_explicitly(self):
        records = fill_unstarted(self.norm(doc([run(True)])), {("c1", "original"): 3}, "cost ceiling")
        self.assertEqual([r.status for r in records], ["ok", "budget_skipped", "budget_skipped"])


if __name__ == "__main__":
    unittest.main()
