"""Markdown report, rendered only from stored evidence."""

from __future__ import annotations

from pathlib import Path

RULES = """1. Integrity problems or too little coverage -> inconclusive.
2. Original skill not observed invoked often enough -> inconclusive (routing diagnosis).
3. A critical requirement violated by the candidate -> keep, whatever the score or cost.
4. A critical requirement the original does not reliably meet -> inconclusive, plus an update finding.
5. No-skill condition within tolerance of the original, invocation observed, critical requirements hold without it -> retirement-candidate.
6. Lower bound of (candidate - original) >= -tolerance -> simplify.
7. Upper bound of (candidate - original) < -tolerance -> keep.
8. Otherwise inconclusive. Cost and duration never enter these rules."""


def _fmt(value, spec=".3f", unit=""):
    if value is None:
        return "unavailable"
    return f"{value:{spec}}{unit}"


def _short(digest: str | None) -> str:
    return digest[:12] if digest else "unavailable"


def render_report(manifest: dict, evidence, decision, run_dir: Path) -> str:
    d = decision.to_json()
    run = manifest.get("run", {})
    runner = manifest.get("runner", {})
    skill = manifest["skill"]
    suite = manifest["suite"]
    versions = sorted({i.get("claude_version") for i in manifest.get("invocations", []) if i.get("claude_version")})
    lines: list[str] = [f"# Claude Curator report: {manifest.get('experiment')}", ""]

    if runner and not runner.get("paid", True):
        lines += [f"> {runner.get('label', 'OFFLINE FIXTURE')}. Runner: {runner.get('version')}.", ""]

    lines += ["## Recommendation", "", f"**{d['recommendation']}**", ""]
    lines += [f"- {r}" for r in d["reasons"]] + [""]
    lines += [
        f"Scope: this applies only to the tested environment (skill `{skill['name']}`, model "
        f"`{run.get('model') or 'runner default'}`, Claude Code {', '.join(versions) or 'unavailable'}, runner "
        f"`{runner.get('name', 'not run')}`, the {len(evidence.heldout)} held-out cases listed below). It does not show "
        "that the skill or section is unnecessary in other projects, models or hosts.", ""]

    change = manifest["change"]
    lines += ["## Proposed change", ""]
    if change.get("remove_section"):
        lines.append(f"Remove the section `{change['remove_section']}` from `SKILL.md`.")
    else:
        lines.append(f"Replace the package with the maintainer-supplied candidate at `{change.get('candidate')}`.")
    lines += ["", f"Rationale: {change['rationale']}", "",
              f"Patch: `change.patch` (paths relative to the repository root, under `{skill['repo_path']}`). "
              f"Original package `{_short(skill['original_digest'])}`, candidate `{_short(skill['candidate_digest'])}`. "
              "Frozen copies are in `original/` and `candidate/`.", ""]

    lines += ["## Requirements", ""]
    if not d["requirements"]:
        lines += ["No requirements were declared, so only aggregate scores were compared.", ""]
    else:
        lines += ["| Requirement | Critical | Status | Holds without skill | Evidence |", "|---|---|---|---|---|"]
        descriptions = {r.id: r.description for r in evidence.requirements}
        for v in d["requirements"]:
            holds = {True: "yes", False: "no", None: "unknown"}[v["holds_without_skill"]]
            lines.append(f"| {v['id']}: {descriptions.get(v['id'], '')} | {'yes' if v['critical'] else 'no'} | "
                         f"{v['status'].replace('_', ' ')} | {holds} | {'<br>'.join(v['details'])} |")
        lines.append("")

    lines += ["## Configuration tested", "",
              f"- Model: `{run.get('model') or 'runner default (not pinned)'}`; judge model: `{run.get('judge_model') or 'runner default'}`",
              f"- Runs per case per condition: {run.get('runs_per_case')}; planned agent runs: {run.get('planned_agent_runs')}",
              f"- Tool grants: {', '.join(run.get('allow_tools') or []) or 'read-only set only'}; scaffold scripts: {'on' if run.get('scaffold') else 'off'}",
              f"- Runner: {runner.get('name', 'not run')} ({runner.get('version', 'unavailable')}); Claude Curator {manifest['tool']['version']}",
              f"- Repository: {manifest['repository'].get('repository') or 'not a git repository'} at "
              f"{manifest['repository'].get('revision') or 'unavailable'}{' (dirty working tree)' if manifest['repository'].get('dirty') else ''}",
              f"- Eval suite digest `{_short(suite['digest'])}` (frozen copy with invocation grader `{_short(suite['frozen_digest'])}`)",
              f"- Held-out cases ({len(suite['heldout'])}): {', '.join(suite['heldout'])}",
              f"- Development cases ({len(suite['development'])}): {', '.join(suite['development']) or 'none declared'}",
              f"- Explicit-request cases: {', '.join(evidence.explicit) or 'none'}; cases not expected to invoke: {', '.join(evidence.no_invocation_expected) or 'none'}"]
    budget = manifest.get("budget")
    if budget:
        lines.append(f"- Budget: max runs {budget.get('max_runs')}, max cost {_fmt(budget.get('max_cost_usd'), '.2f', ' USD')}, "
                     f"reported spend {_fmt(budget.get('reported_cost_usd'), '.4f', ' USD')}"
                     f"{'; stopped: ' + budget['stopped'] if budget.get('stopped') else ''}. Enforcement: {budget.get('enforcement')}.")
    lines.append("")

    lines += ["## Results", "", "Mean outcome score per held-out case (runs averaged within the case):", "",
              "| Case | Original | Candidate | No skill |", "|---|---|---|---|"]
    cs = d["case_scores"]
    for case in evidence.heldout:
        if case in cs["original"]:
            lines.append(f"| {case} | {cs['original'][case]:.2f} | {cs['candidate'][case]:.2f} | {cs['none'][case]:.2f} |")
        else:
            counts = d["coverage"]["incomplete"].get(case, {})
            lines.append(f"| {case} | incomplete: valid runs {counts} | | |")
    lines += ["", "Effects (positive means higher score than the original):", ""]
    for eff in d["effects"].values():
        if eff["estimate"] is None:
            lines.append(f"- {eff['comparison']}: not estimable")
        else:
            lines.append(f"- {eff['comparison']}: {eff['estimate']:+.3f}, one-sided {eff['confidence']:.0%} bounds "
                         f"[{eff['lower']:+.3f}, {eff['upper']:+.3f}] over {eff['n_cases']} cases; tolerance -{d['method']['tolerance']:.2f}")
    inv = d["invocation"]

    def rate(x):
        return f"{x['observed']}/{x['known']}" + (f" ({x['rate']:.0%})" if x["rate"] is not None else "") + (f", {x['unavailable']} unavailable" if x["unavailable"] else "")
    lines += ["", "Invocation evidence (Skill tool call naming the skill). Not observed does not prove the skill was absent.", "",
              f"- Original: {rate(inv['original'])}; natural requests {rate(inv['original_natural'])}; explicit requests {rate(inv['original_explicit'])}",
              f"- Candidate: {rate(inv['candidate'])}", "",
              "Run outcomes by condition (infra_error, grader_error and budget_skipped runs are excluded from scores; "
              "agent_limit runs are scored on what they produced):", "",
              "| Condition | ok | agent_limit | infra_error | grader_error | budget_skipped |", "|---|---|---|---|---|---|"]
    for cond in ("original", "candidate", "none"):
        f = d["failures"].get(cond, {})
        lines.append(f"| {cond} | " + " | ".join(str(f.get(k, 0)) for k in ("ok", "agent_limit", "infra_error", "grader_error", "budget_skipped")) + " |")
    lines += ["", "Cost and duration (informational only; costs are the runner's list-price estimates, not invoices or plan "
              "usage, and fewer tokens do not necessarily mean lower cost):", "",
              "| Condition | Reported cost (USD) | Runs without cost | Mean duration (s) | Output tokens | Runs without tokens |",
              "|---|---|---|---|---|---|"]
    for cond, c in d["cost"].items():
        lines.append(f"| {cond} | {_fmt(c['cost_usd_sum_of_reported'], '.4f')} | {c['cost_unavailable_runs']} | "
                     f"{_fmt(c['mean_duration_seconds'], '.1f')} | {_fmt(c['output_tokens_sum'], 'd')} | {c['token_unavailable_runs']} |")
    lines.append("")

    lines += ["## Findings", ""]
    lines += ["Update findings (outdated or broken instructions; not repaired automatically):", ""]
    lines += [f"- {f}" for f in d["update_findings"]] or ["- none"]
    lines += ["", "Routing findings:", ""]
    lines += [f"- {f}" for f in d["routing_findings"]] or ["- none"]
    lines.append("")

    lines += ["## Limitations", ""]
    lines += [f"- {x}" for x in d["limitations"]]
    lines += [f"- {x}" for x in manifest.get("isolation_boundary", [])]
    lines += ["- A small experiment that passes does not establish general reliability; repeat it when the model, "
              "Claude Code version, suite or project conventions change.", ""]

    m = d["method"]
    lines += ["## Method", "",
              f"Unit of analysis: {m['unit']}. Interval: {m['interval']} with {m['resamples']} resamples, seed {m['seed']}, "
              f"confidence {m['confidence']:.0%}, tolerance {m['tolerance']:.2f} on the 0..1 score scale.", "", RULES, ""]

    spec_path = manifest.get("spec")
    lines += ["## Reproduce", "",
              "Recompute this decision and report from the stored evidence (no agent runs):", "",
              "```bash", f"python3 -m claude_curator report {run_dir}", "```", "",
              "Re-run the experiment (a new evidence directory is created). First check that the skill and suite still "
              "match the digests above; the spec as run is frozen at `curator.toml` in this directory:", "",
              "```bash",
              f"python3 -m claude_curator run {spec_path}" + (f" --runner fixture --fixture {runner.get('scenario')}" if runner and not runner.get("paid", True) else
                                                           f" --max-runs {budget.get('max_runs') if budget else 'N'} --max-cost-usd {budget.get('max_cost_usd') if budget else 'X'} --trust-plugin"),
              "```", "",
              f"Apply the change from the repository root with `git apply {run_dir / 'change.patch'}`, "
              f"and reverse it with `git apply -R` on the same file. Each runner invocation's command, stdout, stderr and raw "
              "result JSON are under `raw/`.", ""]
    return "\n".join(lines)
