"""PluginEvalRunner against a stub `claude` executable.

The stub checks command construction, result parsing, exit-code handling and
budget hand-off. It is not Claude Code and verifies nothing about live runs.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import textwrap
import unittest
from pathlib import Path

from claude_curator.cli import main
from claude_curator.runners import Invocation, PluginEvalRunner, RunnerError
from tests.helpers import EXAMPLES, temp_dir

STUB = textwrap.dedent('''
    #!{python}
    import json, os, sys
    from pathlib import Path
    args = sys.argv[1:]
    log = os.environ["STUB_LOG"]
    with open(log, "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    if args == ["--version"]:
        print(os.environ.get("STUB_VERSION", "2.1.296 (Claude Code)"))
        sys.exit(0)
    assert args[:2] == ["plugin", "eval"], args
    plugin = Path(args[2])
    opt = lambda name: args[args.index(name) + 1] if name in args else None
    case = opt("--case")
    if case in os.environ.get("STUB_UNMATCHED_CASES", "").split(","):
        Path(opt("--json")).parent.mkdir(parents=True, exist_ok=True)
        Path(opt("--json")).write_text(json.dumps({{"schemaVersion": 1, "claudeVersion": "2.1.296", "costUsd": 0,
                                                    "partial": False, "cases": [], "aggregates": {{}}}}))
        print(f'No eval cases found matching --case "{{case}}"', file=sys.stderr)
        sys.exit(1)
    if case in os.environ.get("STUB_FAIL_CASES", "").split(","):
        print("Error: API Error: 529 overloaded", file=sys.stderr)
        sys.exit(1)
    runs, ablation = int(opt("--runs")), opt("--ablation")
    case_dir = next(p.parent for p in (plugin / "evals").rglob("prompt.md") if p.parent.name == case)
    names = sorted(p.stem for p in (case_dir / "graders").glob("*.md"))
    def run(arm):
        graders = [{{"name": n, "passed": arm == "with" or not n.startswith("_curator"), "weight": 1,
                    "explanation": "stub", "withOnly": n.startswith("_curator")}} for n in names]
        return {{"score": 1.0, "passed": True, "turns": 3, "costUsd": 0.01, "judgeCostUsd": 0.0, "durationSeconds": 2.0,
                "error": None, "tracePath": "", "skippedPaidGraders": False, "graders": graders}}
    arms = {{"with": [run("with") for _ in range(runs)]}}
    if ablation == "with-without":
        arms["without"] = [run("without") for _ in range(runs)]
    n = sum(len(v) for v in arms.values())
    doc = {{"schemaVersion": int(os.environ.get("STUB_SCHEMA", "1")), "claudeVersion": "2.1.296", "startedAt": "", "durationSeconds": 1, "costUsd": 0.01 * n,
           "partial": False, "suite": {{}}, "cases": [{{"name": case, "arms": arms, "aggregates": {{"score": 1.0, "passRate": 1.0}}}}],
           "aggregates": {{}}}}
    Path(opt("--json")).parent.mkdir(parents=True, exist_ok=True)
    Path(opt("--json")).write_text(json.dumps(doc))
''').lstrip()


class PluginEvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.stub = self.tmp / "bin" / "claude"
        self.stub.parent.mkdir()
        self.stub.write_text(STUB.format(python=sys.executable))
        self.stub.chmod(0o755)
        self.log = self.tmp / "calls.jsonl"
        self.env = {"STUB_LOG": str(self.log)}
        self._saved = {k: os.environ.get(k) for k in ("STUB_LOG", "STUB_VERSION", "STUB_FAIL_CASES", "STUB_SCHEMA", "STUB_UNMATCHED_CASES")}
        os.environ.update(self.env)
        self.addCleanup(self._restore)
        self.example = self.tmp / "changelog-writer"
        shutil.copytree(EXAMPLES / "changelog-writer", self.example, ignore=shutil.ignore_patterns(".claude-curator"))

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_command_puts_target_first_and_variadic_grants_last(self):
        inv = Invocation(plugin_dir=Path("/p"), case="c", condition_arms={"with": "candidate"}, runs=3,
                         output_json=Path("/o/r.json"), output_dir=Path("/o/out"), model="m", judge_model="j",
                         allow_tools=("Write", "Bash(python3 *)"), scaffold=True, max_cost_usd=2.5, trust_plugin=True)
        cmd = PluginEvalRunner("claude").command(inv)
        self.assertEqual(cmd[:4], ["claude", "plugin", "eval", "/p"])
        self.assertEqual(cmd[-3:], ["--allow-tools", "Write", "Bash(python3 *)"])
        for flag, value in (("--ablation", "none"), ("--threshold", "0"), ("--max-cost-usd", "2.5000"),
                            ("--model", "m"), ("--judge-model", "j"), ("--json", "/o/r.json")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("--trust-plugin", cmd)
        self.assertIn("--no-publish", cmd)
        self.assertIn("--scaffold", cmd)

    def test_old_claude_code_is_rejected(self):
        os.environ["STUB_VERSION"] = "2.1.200 (Claude Code)"
        with self.assertRaisesRegex(RunnerError, "older than 2.1.269"):
            PluginEvalRunner(str(self.stub)).version()

    def test_full_experiment_through_the_stub(self):
        out = self.tmp / "runs"
        code, stdout, err = self.cli("run", self.example / "curator.toml", "--claude", self.stub, "--trust-plugin",
                                     "--out", out, "--max-cost-usd", "5", "--max-runs", "100")
        self.assertEqual(code, 0, err)
        run_dir = next(out.iterdir())
        decision = json.loads((run_dir / "decision.json").read_text())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["runner"]["version"], "2.1.296 (Claude Code)")
        self.assertTrue(manifest["runner"]["paid"])
        evals = [c for c in self.calls() if c[:2] == ["plugin", "eval"]]
        self.assertEqual(len(evals), 16)  # 8 held-out cases x (original+none, candidate)
        ablations = sorted({(Path(c[2]).name, c[c.index("--ablation") + 1]) for c in evals})
        self.assertEqual(ablations, [("candidate", "none"), ("original", "with-without")])
        budgets = [float(c[c.index("--max-cost-usd") + 1]) for c in evals]
        self.assertEqual(budgets, sorted(budgets, reverse=True))
        self.assertAlmostEqual(budgets[0] - budgets[1], 0.06)  # first invocation: 3 runs x 2 arms x 0.01
        self.assertEqual(Path(evals[1][2]).name, "candidate")  # same case, other plugin next
        self.assertEqual(Path(evals[2][2]).name, "candidate")  # order alternates on the next case
        # Every condition passes everything in the stub, but the package requirement blocks retirement.
        self.assertEqual(decision["recommendation"], "simplify")
        self.assertEqual(decision["invocation"]["original"]["rate"], 1.0)

    def test_runner_failure_without_result_is_recorded_not_scored(self):
        os.environ["STUB_FAIL_CASES"] = "security-patch"
        out = self.tmp / "runs"
        code, _, err = self.cli("run", self.example / "curator.toml", "--claude", self.stub, "--trust-plugin",
                                "--out", out, "--max-cost-usd", "5", "--max-runs", "100")
        self.assertEqual(code, 0, err)
        run_dir = next(out.iterdir())
        evidence = json.loads((run_dir / "evidence.json").read_text())
        self.assertTrue(any("security-patch" in i and "overloaded" in i for i in evidence["integrity_issues"]))
        stderr_files = list((run_dir / "raw").glob("*security-patch*/stderr.txt"))
        self.assertTrue(stderr_files and "overloaded" in stderr_files[0].read_text())
        self.assertEqual(json.loads((run_dir / "decision.json").read_text())["recommendation"], "inconclusive")

    def test_case_missing_from_runner_result_is_infrastructure(self):
        os.environ["STUB_UNMATCHED_CASES"] = "perf-improvement"
        out = self.tmp / "runs"
        code, _, err = self.cli("run", self.example / "curator.toml", "--claude", self.stub, "--trust-plugin",
                                "--out", out, "--max-cost-usd", "5", "--max-runs", "100")
        self.assertEqual(code, 0, err)
        run_dir = next(out.iterdir())
        evidence = json.loads((run_dir / "evidence.json").read_text())
        self.assertTrue(any("no result for perf-improvement" in i for i in evidence["integrity_issues"]))
        records = [json.loads(l) for l in (run_dir / "records.jsonl").read_text().splitlines()]
        statuses = {r["status"] for r in records if r["case"] == "perf-improvement"}
        self.assertEqual(statuses, {"infra_error"})

    def test_unreadable_result_stops_but_still_writes_evidence(self):
        os.environ["STUB_SCHEMA"] = "2"
        out = self.tmp / "runs"
        code, _, err = self.cli("run", self.example / "curator.toml", "--claude", self.stub, "--trust-plugin",
                                "--out", out, "--max-cost-usd", "5", "--max-runs", "100")
        self.assertEqual(code, 0, err)
        run_dir = next(out.iterdir())
        self.assertTrue((run_dir / "report.md").is_file())
        decision = json.loads((run_dir / "decision.json").read_text())
        self.assertEqual(decision["recommendation"], "inconclusive")
        self.assertTrue(any("schemaVersion" in r for r in decision["reasons"]))
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["plugin", "eval"]]), 1)  # stopped after the first

    def test_paid_runner_needs_a_spending_ceiling_and_pinned_model(self):
        spec = self.example / "curator.toml"
        spec.write_text(spec.read_text().replace("max_cost_usd = 15.0\n", ""))
        code, _, err = self.cli("run", spec, "--claude", self.stub, "--trust-plugin", "--out", self.tmp / "runs")
        self.assertEqual(code, 1)
        self.assertIn("spending ceiling", err)
        self.assertFalse([c for c in self.calls() if c[:2] == ["plugin", "eval"]])


if __name__ == "__main__":
    unittest.main()
