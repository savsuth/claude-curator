import unittest

from claude_curator.decide import INCONCLUSIVE, KEEP, RETIRE, SIMPLIFY, Evidence, RequirementInput, decide
from claude_curator.results import RunRecord
from claude_curator.spec import DecisionConfig

CASES = [f"case-{i}" for i in range(8)]
CFG = DecisionConfig(bootstrap_resamples=2000, min_cases=6, min_runs_per_case=3, tolerance=0.1)


def records(outcome, cases=CASES, runs=3, invoked=True, status="ok", cost=0.05):
    """outcome(condition, case, run) -> (score, {grader: passed})"""
    out = []
    for case in cases:
        for cond in ("original", "candidate", "none"):
            for i in range(runs):
                score, graders = outcome(cond, case, i)
                inv = "not_applicable" if cond == "none" else ("observed" if invoked else "not_observed")
                st = status(cond, case, i) if callable(status) else status
                out.append(RunRecord(condition=cond, case=case, run_index=i, status=st,
                                     score=score if st in ("ok", "agent_limit") else None,
                                     graders=graders, invocation=inv, cost_usd=cost if cond != "candidate" else cost / 2))
    return out


def skill_helps(cond, case, i):
    ok = cond != "none"
    return (1.0 if ok else 0.0), {"fmt": ok}


REQ = RequirementInput(id="fmt", description="uses team format", critical=True, graders=[["case-0", "fmt"]], package_checks=[])


def evidence(**kw):
    base = dict(heldout=CASES, explicit=[], no_invocation_expected=[], requirements=[REQ], config=CFG)
    base.update(kw)
    return Evidence(**base)


class DecideTest(unittest.TestCase):
    def test_simplification_that_preserves_requirements(self):
        d = decide(evidence(), records(skill_helps))
        self.assertEqual(d.recommendation, SIMPLIFY, d.reasons)
        self.assertEqual(d.requirements[0].status, "preserved")
        self.assertGreaterEqual(d.effects["candidate_vs_original"].lower, -0.1)
        self.assertLess(d.effects["none_vs_original"].estimate, -0.5)

    def test_simplification_that_removes_a_necessary_instruction(self):
        def outcome(cond, case, i):
            fmt = cond == "original" or (cond == "candidate" and case != "case-0")
            return (1.0 if fmt else 0.5), {"fmt": fmt}
        d = decide(evidence(), records(outcome))
        self.assertEqual(d.recommendation, KEEP, d.reasons)
        self.assertEqual(d.requirements[0].status, "violated")

    def test_cheaper_candidate_does_not_override_a_failed_requirement(self):
        def outcome(cond, case, i):
            fmt = cond == "original"
            return 1.0, {"fmt": fmt or case != "case-0"}
        d = decide(evidence(), records(outcome))
        self.assertLess(d.cost["candidate"]["cost_usd_sum_of_reported"], d.cost["original"]["cost_usd_sum_of_reported"])
        self.assertEqual(d.recommendation, KEEP)

    def test_skill_that_never_activates_is_a_routing_diagnosis_not_retirement(self):
        d = decide(evidence(), records(lambda c, k, i: (1.0, {"fmt": True}), invoked=False))
        self.assertEqual(d.recommendation, INCONCLUSIVE)
        self.assertTrue(any("invoked in only 0/" in r for r in d.reasons))
        self.assertTrue(any("routing diagnosis" in f for f in d.routing_findings))

    def test_too_few_cases_is_inconclusive_even_with_equal_pass_rates(self):
        d = decide(evidence(heldout=CASES[:3]), records(skill_helps, cases=CASES[:3]))
        self.assertEqual(d.recommendation, INCONCLUSIVE)
        self.assertEqual(d.effects["candidate_vs_original"].estimate, 0.0)
        self.assertTrue(any("6 are required" in r for r in d.reasons))

    def test_interval_crossing_tolerance_is_inconclusive(self):
        def outcome(cond, case, i):
            if cond == "candidate" and case in ("case-1", "case-2") and i < 2:
                return 0.0, {"fmt": True}
            return (0.0 if cond == "none" else 1.0), {"fmt": cond != "none"}
        d = decide(evidence(), records(outcome))
        eff = d.effects["candidate_vs_original"]
        self.assertLess(eff.lower, -0.1)
        self.assertGreaterEqual(eff.upper, -0.1)
        self.assertEqual(d.recommendation, INCONCLUSIVE, d.reasons)

    def test_clear_regression_is_keep(self):
        def outcome(cond, case, i):
            return (1.0 if cond == "original" else 0.0), {"fmt": True}
        d = decide(evidence(requirements=[]), records(outcome))
        self.assertEqual(d.recommendation, KEEP, d.reasons)

    def test_infrastructure_failures_leave_coverage_short(self):
        def status(cond, case, i):
            return "infra_error" if cond == "candidate" and case in CASES[:4] else "ok"
        d = decide(evidence(), records(skill_helps, status=status))
        self.assertEqual(d.recommendation, INCONCLUSIVE)
        self.assertEqual(d.failures["candidate"]["infra_error"], 12)
        self.assertIn("case-0", d.coverage["incomplete"])

    def test_whole_skill_unnecessary_is_a_retirement_candidate(self):
        d = decide(evidence(), records(lambda c, k, i: (1.0, {"fmt": True})))
        self.assertEqual(d.recommendation, RETIRE, d.reasons)

    def test_package_requirement_blocks_retirement(self):
        req = RequirementInput(id="script", description="mentions script", critical=True, graders=[],
                               package_checks=[{"file": "SKILL.md", "text": "check.py", "present": True, "original": True, "candidate": True}])
        d = decide(evidence(requirements=[req]), records(lambda c, k, i: (1.0, {"fmt": True})))
        self.assertEqual(d.recommendation, SIMPLIFY, d.reasons)

    def test_requirement_unmet_by_original_is_an_update_finding(self):
        def outcome(cond, case, i):
            return 1.0, {"fmt": i != 0}
        d = decide(evidence(), records(outcome))
        self.assertEqual(d.recommendation, INCONCLUSIVE)
        self.assertTrue(any(f.startswith("requirement fmt") for f in d.update_findings))

    def test_integrity_issue_forces_inconclusive(self):
        d = decide(evidence(integrity_issues=["grader suites differ"]), records(skill_helps))
        self.assertEqual(d.recommendation, INCONCLUSIVE)

    def test_bounds_are_reproducible(self):
        def outcome(cond, case, i):
            return (1.0 if (cond != "candidate" or ((int(case[-1]) + i) % 3)) else 0.0), {"fmt": True}
        rs = records(outcome)
        a = decide(evidence(requirements=[]), rs).effects["candidate_vs_original"]
        b = decide(evidence(requirements=[]), rs).effects["candidate_vs_original"]
        self.assertEqual((a.lower, a.upper), (b.lower, b.upper))


if __name__ == "__main__":
    unittest.main()
