"""Runner adapters. Both write a `claude plugin eval` schemaVersion 1 document,
so every result goes through the same normalisation.

- PluginEvalRunner: the real `claude plugin eval` command (paid model calls).
- FixtureRunner: replays scripted outcomes from a JSON scenario. It makes no
  model calls and proves nothing about live behaviour; it exists so the
  workflow and decision rules can be exercised offline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_curator.suite import load_suite

MIN_CLAUDE_VERSION = (2, 1, 269)


class RunnerError(Exception):
    pass


@dataclass(frozen=True)
class Invocation:
    plugin_dir: Path
    case: str
    condition_arms: dict[str, str]  # runner arm -> condition
    runs: int
    output_json: Path
    output_dir: Path
    model: str = ""
    judge_model: str = ""
    allow_tools: tuple[str, ...] = ()
    scaffold: bool = False
    max_cost_usd: float | None = None
    trust_plugin: bool = False

    @property
    def ablation(self) -> str:
        return "with-without" if "without" in self.condition_arms else "none"


@dataclass
class InvocationResult:
    command: list[str]
    exit_code: int
    doc: dict | None
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    error: str | None = None
    extra: dict = field(default_factory=dict)


# --- real runner ----------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


class PluginEvalRunner:
    name = "claude-plugin-eval"
    paid = True

    def __init__(self, executable: str = "claude"):
        self.executable = executable

    def version(self) -> str:
        exe = shutil.which(self.executable)
        if not exe:
            raise RunnerError(f"{self.executable!r} not found on PATH; install Claude Code >= 2.1.269")
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        version = parse_version(out.stdout)
        if version is None:
            raise RunnerError(f"could not read the Claude Code version from {out.stdout!r}")
        if version < MIN_CLAUDE_VERSION:
            raise RunnerError(f"Claude Code {'.'.join(map(str, version))} is older than 2.1.269, which `plugin eval` needs")
        return out.stdout.strip()

    def command(self, inv: Invocation) -> list[str]:
        cmd = [self.executable, "plugin", "eval", str(inv.plugin_dir),
               "--case", inv.case,
               "--ablation", inv.ablation,
               "--runs", str(inv.runs),
               "--threshold", "0",  # exit code must reflect failures to run, not low scores
               "--no-publish",
               "--output-dir", str(inv.output_dir),
               "--json", str(inv.output_json),
               "--scaffold" if inv.scaffold else "--no-scaffold"]
        if inv.trust_plugin:
            cmd.append("--trust-plugin")
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.judge_model:
            cmd += ["--judge-model", inv.judge_model]
        if inv.max_cost_usd is not None:
            cmd += ["--max-cost-usd", f"{inv.max_cost_usd:.4f}"]
        if inv.allow_tools:
            cmd += ["--allow-tools", *inv.allow_tools]  # variadic: keep it last
        return cmd

    def invoke(self, inv: Invocation) -> InvocationResult:
        cmd = self.command(inv)
        inv.output_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as e:
            return InvocationResult(cmd, -1, None, error=f"could not start runner: {e}")
        seconds = time.monotonic() - start
        doc, error = None, None
        if inv.output_json.is_file():
            try:
                doc = json.loads(inv.output_json.read_text())
            except json.JSONDecodeError as e:
                error = f"runner wrote unreadable JSON: {e}"
        elif proc.returncode != 0:
            error = f"runner exited {proc.returncode} without a result document"
        return InvocationResult(cmd, proc.returncode, doc, proc.stdout, proc.stderr, seconds, error)


# --- offline fixture runner ------------------------------------------------------

FIXTURE_LABEL = "OFFLINE FIXTURE: scripted outcomes, no model calls, not evidence about live behaviour"


class FixtureRunner:
    """Scenario format (JSON):

    {
      "label": "...",
      "cost_per_run_usd": 0.04,              # null means the runner reports no cost
      "default": {"original": RUN, "candidate": RUN, "none": RUN},
      "cases": {"<case>": {"candidate": RUN | [RUN, ...]}},
      "fail_invocations": [{"case": "<case>", "condition": "candidate", "exit_code": 1, "stderr": "..."}]
    }

    RUN = {"fail": [grader names], "invoked": true, "error": null, "no_graders": false,
           "grader_error": [grader names], "cost": 0.04, "duration": 20}
    Every grader not listed in "fail" passes.
    """

    name = "fixture"
    paid = False

    def __init__(self, scenario_path: Path):
        self.path = Path(scenario_path)
        self.scenario = json.loads(self.path.read_text())
        self.spent = 0.0

    def version(self) -> str:
        return self.path.name

    def _spec(self, case: str, condition: str, index: int) -> dict:
        spec = dict((self.scenario.get("default") or {}).get(condition) or {})
        override = ((self.scenario.get("cases") or {}).get(case) or {}).get(condition)
        if isinstance(override, list):
            override = override[index % len(override)]
        spec.update(override or {})
        return spec

    def invoke(self, inv: Invocation) -> InvocationResult:
        cmd = ["fixture", str(self.path), inv.case, inv.ablation]
        for fail in self.scenario.get("fail_invocations") or []:
            if fail["case"] == inv.case and fail["condition"] in inv.condition_arms.values():
                return InvocationResult(cmd, fail.get("exit_code", 1), None, stderr=fail.get("stderr", ""),
                                        error=f"runner exited {fail.get('exit_code', 1)} without a result document")
        suite = load_suite(inv.plugin_dir / "evals")
        case = suite.cases[inv.case]
        default_cost = self.scenario.get("cost_per_run_usd", 0.04)
        partial, total = None, 0.0
        arms_out: dict[str, list] = {}
        for arm, condition in inv.condition_arms.items():
            arms_out[arm] = []
            for i in range(inv.runs):
                if inv.max_cost_usd is not None and total >= inv.max_cost_usd:
                    partial = "cost_ceiling"
                    break
                spec = self._spec(inv.case, condition, i)
                cost = spec.get("cost", default_cost)
                if spec.get("no_graders"):
                    arms_out[arm].append({"score": 0, "passed": False, "turns": 0, "costUsd": 0, "judgeCostUsd": 0,
                                          "error": spec.get("error") or "run did not start", "tracePath": "",
                                          "skippedPaidGraders": False, "graders": []})
                    continue
                graders = []
                for name, g in case.graders.items():
                    if name == "_curator_invoked":
                        passed = bool(spec.get("invoked", True)) and condition != "none"
                    else:
                        passed = name not in (spec.get("fail") or [])
                    entry = {"name": name, "passed": passed, "weight": g.weight, "explanation": "fixture",
                             "withOnly": not g.is_outcome}
                    if name in (spec.get("grader_error") or []):
                        entry.update(passed=None, explanation="judge error: fixture")
                    graders.append(entry)
                run = {"score": 0.0, "passed": False, "turns": 5, "judgeCostUsd": 0.0,
                       "durationSeconds": spec.get("duration", 20.0), "error": spec.get("error"),
                       "tracePath": "", "skippedPaidGraders": bool(spec.get("skipped_paid_graders")), "graders": graders}
                if cost is not None:
                    run["costUsd"] = cost
                    total += cost
                arms_out[arm].append(run)
            if partial:
                break
        self.spent += total
        doc = {"schemaVersion": 1, "claudeVersion": "fixture", "startedAt": "", "durationSeconds": 0,
               "costUsd": total, "partial": partial is not None, "suite": {"ablation": inv.ablation},
               "cases": [{"name": inv.case, "arms": arms_out, "aggregates": {"score": 0, "passRate": 0}}],
               "aggregates": {}}
        if partial:
            doc["partialReason"] = partial
        inv.output_json.parent.mkdir(parents=True, exist_ok=True)
        inv.output_json.write_text(json.dumps(doc, indent=2))
        return InvocationResult(cmd, 2 if partial else 0, doc, extra={"label": FIXTURE_LABEL})
