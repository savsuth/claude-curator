"""Command-line interface: check, run, report."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from claude_curator import __version__
from claude_curator.experiment import ExperimentError, execute, prepare, rerender
from claude_curator.package import PackageError
from claude_curator.patch import PatchError
from claude_curator.results import ResultError
from claude_curator.runners import FixtureRunner, Invocation, PluginEvalRunner, RunnerError, sandbox_blockers
from claude_curator.spec import SpecError
from claude_curator.suite import SuiteError

USER_ERRORS = (SpecError, SuiteError, PackageError, PatchError, ExperimentError, RunnerError, ResultError)


def _print_decision(decision, run_dir: Path) -> None:
    print(f"\nRecommendation: {decision.recommendation}")
    for reason in decision.reasons:
        print(f"  - {reason}")
    if decision.update_findings:
        print("Update findings:")
        for f in decision.update_findings:
            print(f"  - {f}")
    if decision.routing_findings:
        print("Routing findings:")
        for f in decision.routing_findings:
            print(f"  - {f}")
    print(f"\nReport:   {run_dir / 'report.md'}")
    print(f"Patch:    {run_dir / 'change.patch'}")
    print(f"Evidence: {run_dir}")


def cmd_check(args) -> int:
    with tempfile.TemporaryDirectory(prefix="claude-curator-check-") as tmp:
        prep = prepare(Path(args.spec), out_root=Path(tmp), runs_override=args.runs, allow_unsupported=args.allow_unsupported)
        ev = prep.evidence
        print(f"Experiment: {prep.spec.name}")
        print(f"Skill: {prep.manifest['skill']['name']}  original {prep.manifest['skill']['original_digest'][:12]}  "
              f"candidate {prep.manifest['skill']['candidate_digest'][:12]}")
        added = sum(1 for l in prep.patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in prep.patch.splitlines() if l.startswith("-") and not l.startswith("---"))
        print(f"Patch: +{added} -{removed} lines")
        print(f"Held-out cases ({len(prep.heldout)}): {', '.join(prep.heldout)}")
        print(f"Planned agent runs: {prep.planned_runs} (cases x runs x 3 conditions), plus judge calls for llm graders")
        for req in ev.requirements:
            for c in req.package_checks:
                state = "ok" if c["candidate"] else "FAILS in candidate"
                print(f"Requirement {req.id}: {c['file']} {'contains' if c['present'] else 'omits'} {c['text']!r}: {state}")
        for label, items in (("Integrity issues", ev.integrity_issues), ("Update findings", ev.update_findings), ("Notes", ev.notes)):
            if items:
                print(f"{label}:")
                for i in items:
                    print(f"  - {i}")
        runner = PluginEvalRunner(args.claude)
        first = prep.heldout[0]
        example = Invocation(plugin_dir=prep.run_dir / "plugins" / "original", case=first,
                             condition_arms={"with": "original", "without": "none"}, runs=args.runs or prep.spec.run.runs,
                             output_json=Path("<run>/raw/result.json"), output_dir=Path("<run>/raw/runner-output"),
                             model=prep.spec.run.model, judge_model=prep.spec.run.judge_model,
                             allow_tools=prep.spec.run.allow_tools, scaffold=prep.spec.run.scaffold, max_cost_usd=prep.spec.run.max_cost_usd,
                             trust_plugin=True)
        print("Example runner command:\n  " + " ".join(runner.command(example)).replace(str(prep.run_dir), "<run>"))
        blockers = sandbox_blockers(prep.spec.run.allow_tools)
        if blockers:
            print("Environment problem (real runs only):")
            for b in blockers:
                print(f"  - {b}")
            print("  Run the experiment in a VM or container without these credentials, or drop the Bash grant.")
        return 1 if ev.integrity_issues or blockers else 0


def cmd_run(args) -> int:
    if args.runner == "fixture":
        if not args.fixture:
            print("error: --runner fixture needs --fixture <scenario.json>", file=sys.stderr)
            return 1
        runner = FixtureRunner(Path(args.fixture))
    else:
        runner = PluginEvalRunner(args.claude)
        if not args.trust_plugin:
            print("error: real runs need --trust-plugin: `claude plugin eval` cannot ask for trust in non-interactive mode, "
                  "and passing it asserts you trust this skill's code and eval suite", file=sys.stderr)
            return 1
        runner.version()  # fail before writing anything if Claude Code is missing or too old
    prep = prepare(Path(args.spec), out_root=Path(args.out) if args.out else None, runs_override=args.runs,
                   allow_unsupported=args.allow_unsupported)
    try:
        decision = execute(prep, runner, max_runs=args.max_runs, max_cost_usd=args.max_cost_usd,
                           trust_plugin=args.trust_plugin, runs_override=args.runs,
                           progress=lambda m: print(m, file=sys.stderr))
    except ExperimentError:
        shutil.rmtree(prep.run_dir, ignore_errors=True)  # refused before any agent ran; nothing to keep
        raise
    if not runner.paid:
        print("\nOFFLINE FIXTURE: scripted outcomes, no model calls. This exercises the workflow, not live behaviour.")
    _print_decision(decision, prep.run_dir)
    return 0


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    decision = rerender(run_dir)
    _print_decision(decision, run_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-curator", description="Decide, with evidence, whether a section can be removed from a Claude Code skill.")
    p.add_argument("--version", action="version", version=f"claude-curator {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="static checks and the run plan; no agent runs, nothing kept")
    c.add_argument("spec")
    c.add_argument("--runs", type=int)
    c.add_argument("--allow-unsupported", action="store_true")
    c.add_argument("--claude", default="claude", help="Claude Code executable (default: claude)")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="run original, candidate and no-skill conditions and decide")
    r.add_argument("spec")
    r.add_argument("--runner", choices=["claude", "fixture"], default="claude")
    r.add_argument("--fixture", help="scenario JSON for --runner fixture")
    r.add_argument("--runs", type=int, help="runs per case per condition (overrides [run] runs)")
    r.add_argument("--max-runs", type=int, help="refuse plans needing more agent runs than this")
    r.add_argument("--max-cost-usd", type=float, help="spending ceiling across the experiment (list-price estimate)")
    r.add_argument("--trust-plugin", action="store_true", help="assert trust in the skill and suite (required for real runs)")
    r.add_argument("--out", help="directory for evidence (default: <spec dir>/.claude-curator/runs)")
    r.add_argument("--allow-unsupported", action="store_true", help="run even if the skill needs MCP tools runs cannot reach")
    r.add_argument("--claude", default="claude", help="Claude Code executable (default: claude)")
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="recompute decision and report from an evidence directory")
    rep.add_argument("run_dir")
    rep.set_defaults(func=cmd_report)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except USER_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
