"""End-to-end scenarios through the CLI, using the offline fixture runner.

These exercise the workflow and decision rules. They make no model calls and say
nothing about live behaviour.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from claude_curator.cli import main
from tests.helpers import EXAMPLES, temp_dir


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.example = self.tmp / "changelog-writer"
        shutil.copytree(EXAMPLES / "changelog-writer", self.example,
                        ignore=shutil.ignore_patterns(".claude-curator"))
        self.out = self.tmp / "runs"
        # Keep every scenario independent of the AWS files on the machine running the tests.
        self.aws_config = self.tmp / "aws-config"
        env = mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": str(self.aws_config),
                                           "AWS_SHARED_CREDENTIALS_FILE": str(self.tmp / "aws-credentials")})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def run_fixture(self, scenario, spec="curator.toml", *extra):
        code, out, err = self.cli("run", self.example / spec, "--runner", "fixture",
                                  "--fixture", self.example / "fixtures" / scenario, "--out", self.out, *extra)
        self.assertEqual(code, 0, err)
        run_dir = next(self.out.iterdir())
        return json.loads((run_dir / "decision.json").read_text()), run_dir

    # --- the scenarios the brief asks for -------------------------------------------

    def test_simplification_preserves_declared_requirements(self):
        decision, run_dir = self.run_fixture("preserves.json")
        self.assertEqual(decision["recommendation"], "simplify")
        self.assertEqual({r["status"] for r in decision["requirements"]}, {"preserved"})
        report = (run_dir / "report.md").read_text()
        self.assertIn("OFFLINE FIXTURE", report)
        self.assertIn("does not show that the skill or section is unnecessary", report)

    def test_simplification_removes_a_necessary_instruction(self):
        decision, _ = self.run_fixture("removes-necessary.json", "curator-remove-validation.toml")
        self.assertEqual(decision["recommendation"], "keep")
        statuses = {r["id"]: r["status"] for r in decision["requirements"]}
        self.assertEqual(statuses["validator"], "violated")  # package check: SKILL.md lost the script reference

    def test_skill_never_activates(self):
        decision, _ = self.run_fixture("never-activates.json")
        self.assertEqual(decision["recommendation"], "inconclusive")
        self.assertTrue(decision["routing_findings"])
        self.assertNotEqual(decision["recommendation"], "retirement-candidate")

    def test_evidence_too_limited_to_decide(self):
        decision, _ = self.run_fixture("limited-evidence.json")
        self.assertEqual(decision["recommendation"], "inconclusive")
        self.assertTrue(any("more cases or runs" in r for r in decision["reasons"]))

    def test_runner_and_grader_failures(self):
        decision, run_dir = self.run_fixture("runner-failure.json")
        self.assertEqual(decision["recommendation"], "inconclusive")
        self.assertEqual(decision["failures"]["candidate"]["grader_error"], 2)
        self.assertIn("remove-legacy-api", decision["coverage"]["incomplete"])
        evidence = json.loads((run_dir / "evidence.json").read_text())
        self.assertTrue(any("runner failed for remove-legacy-api" in i and "overloaded" in i for i in evidence["integrity_issues"]))
        self.assertEqual(decision["failures"]["candidate"]["infra_error"], 6)  # 2 failed invocations x 3 runs
        self.assertNotIn("budget_skipped", decision["failures"]["candidate"])

    def test_budget_limit_interrupts_the_experiment(self):
        decision, run_dir = self.run_fixture("budget.json", "curator.toml", "--max-cost-usd", "4")
        self.assertEqual(decision["recommendation"], "inconclusive")
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["budget"]["stopped"], "cost ceiling reached")
        self.assertLessEqual(manifest["budget"]["reported_cost_usd"], 4 + 0.6)  # overrun bounded to one in-flight run
        records = [json.loads(l) for l in (run_dir / "records.jsonl").read_text().splitlines()]
        skipped = [r for r in records if r["status"] == "budget_skipped"]
        self.assertTrue(skipped)
        self.assertTrue(all(r["cost_usd"] is None for r in skipped))

    def test_referenced_resource_missing_from_candidate(self):
        code, out, _ = self.cli("check", self.example / "curator-broken-candidate.toml")
        self.assertEqual(code, 1)
        self.assertIn("references/house-style.md", out)
        decision, _ = self.run_fixture("preserves.json", "curator-broken-candidate.toml")
        self.assertEqual(decision["recommendation"], "inconclusive")
        self.assertTrue(any("missing-resource" in r for r in decision["reasons"]))

    # --- environment checks before a paid run ----------------------------------------

    def test_check_flags_an_aws_credential_helper_that_blocks_the_sandbox(self):
        self.aws_config.write_text("[profile csv-pipeline]\nregion = us-east-1\n"
                                   "credential_process = /usr/local/bin/helper --token s3cr3t\n")
        code, out, _ = self.cli("check", self.example / "curator.toml")
        self.assertEqual(code, 1)
        self.assertIn("credential_process", out)
        self.assertIn("csv-pipeline", out)
        self.assertIn(str(self.aws_config), out)
        self.assertNotIn("s3cr3t", out)  # never print the helper command

    def test_check_passes_when_aws_config_has_no_credential_helper(self):
        self.aws_config.write_text("[default]\nregion = us-east-1\n")
        code, out, _ = self.cli("check", self.example / "curator.toml")
        self.assertEqual(code, 0, out)
        self.assertNotIn("sandbox", out.lower())

    def test_credential_helper_is_irrelevant_when_the_shell_is_not_granted(self):
        self.aws_config.write_text("[profile x]\ncredential_source = Ec2InstanceMetadata\n")
        spec = self.example / "curator.toml"
        spec.write_text(spec.read_text().replace('allow_tools = ["Write", "Edit", "Bash(python3 *)"]', 'allow_tools = ["Write", "Edit"]'))
        code, out, _ = self.cli("check", spec)
        self.assertEqual(code, 0, out)

    # --- limits, reproducibility and the patch -------------------------------------------

    def test_run_limit_refuses_before_anything_runs(self):
        code, _, err = self.cli("run", self.example / "curator.toml", "--runner", "fixture",
                                "--fixture", self.example / "fixtures" / "preserves.json", "--out", self.out, "--max-runs", "10")
        self.assertEqual(code, 1)
        self.assertIn("72 agent runs", err)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_refused_experiment_leaves_no_evidence_directory(self):
        spec = self.example / "curator.toml"
        spec.write_text(spec.read_text().replace('remove_section = "Worked examples"', 'candidate = "skills/changelog-writer"'))
        code, _, err = self.cli("run", spec, "--runner", "fixture", "--fixture", self.example / "fixtures" / "preserves.json", "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("identical", err)
        self.assertEqual(list(self.out.iterdir()) if self.out.exists() else [], [])

    def test_paid_runner_requires_trust_flag(self):
        code, _, err = self.cli("run", self.example / "curator.toml", "--out", self.out)
        self.assertEqual(code, 1)
        self.assertIn("--trust-plugin", err)

    def test_report_recomputes_the_same_decision_from_evidence(self):
        decision, run_dir = self.run_fixture("limited-evidence.json")
        code, _, _ = self.cli("report", run_dir)
        self.assertEqual(code, 0)
        again = json.loads((run_dir / "decision.json").read_text())
        self.assertEqual(again["recommendation"], decision["recommendation"])
        self.assertEqual(again["effects"], decision["effects"])

    def test_patch_applies_to_the_maintainer_repository_and_reverses(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        _, run_dir = self.run_fixture("preserves.json")
        repo = self.example
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        patch = run_dir / "change.patch"
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=repo, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True)
        self.assertNotIn("Worked examples", (repo / "skills/changelog-writer/SKILL.md").read_text())
        subprocess.run(["git", "apply", "-R", str(patch)], cwd=repo, check=True)
        self.assertEqual((repo / "skills/changelog-writer/SKILL.md").read_text(),
                         (EXAMPLES / "changelog-writer/skills/changelog-writer/SKILL.md").read_text())

    def test_original_inputs_are_preserved_unchanged(self):
        before = (self.example / "skills/changelog-writer/SKILL.md").read_text()
        _, run_dir = self.run_fixture("preserves.json")
        self.assertEqual((self.example / "skills/changelog-writer/SKILL.md").read_text(), before)
        self.assertEqual((run_dir / "original/changelog-writer/SKILL.md").read_text(), before)
        self.assertFalse((self.example / "evals" / "results").exists())


if __name__ == "__main__":
    unittest.main()
