"""One experiment: freeze inputs, run three conditions, decide, write evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from claude_curator import __version__
from claude_curator.decide import Decision, Evidence, RequirementInput, decide
from claude_curator.package import build_candidate, load_package, scan_package
from claude_curator.patch import make_patch
from claude_curator.report import render_report
from claude_curator.results import ResultError, RunRecord, fill_unstarted, normalize
from claude_curator.runners import FIXTURE_LABEL, Invocation
from claude_curator.spec import Spec, load_spec, resolve_against_suite
from claude_curator.suite import load_suite, materialize_plugin, suite_digest

ISOLATION_BOUNDARY = [
    "Controlled by `claude plugin eval`: a temporary HOME, working directory and Claude Code configuration per run; "
    "only the plugin under test is loaded; user and project settings, CLAUDE.md, hooks, MCP servers, memory and "
    "other skills are absent; the eval directory is hidden from the agent; Bash, when granted, runs in the OS sandbox.",
    "Kept identical across conditions by Claude Curator: the frozen eval suite (hash-checked before and after each "
    "invocation), the prompt, model, judge model, tool grants, run count and scaffold setting.",
    "Not controlled: managed (admin) policy on this machine, the model provider's behaviour over time, account rate "
    "limits, network access granted to WebFetch, and anything the skill's own hooks do outside the sandbox. "
    "Conditions run interleaved per case with alternating order to limit drift, not to remove it.",
    "Read-only file tools are not documented as confined to the run directory, so an agent could in principle read "
    "absolute paths such as this evidence directory; graders and reference answers stay in the hidden eval directory.",
]


class ExperimentError(Exception):
    pass


class BudgetError(ExperimentError):
    pass


@dataclass
class Prepared:
    spec: Spec
    run_dir: Path
    heldout: tuple[str, ...]
    planned_runs: int
    evidence: Evidence
    manifest: dict
    patch: str


def _git_revision(path: Path) -> dict:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=True).stdout.strip()
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=path, capture_output=True, text=True)
        rev = head.stdout.strip() if head.returncode == 0 else None  # None: no commits yet
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout.strip())
        return {"repository": top, "revision": rev, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"repository": None, "revision": None, "dirty": None, "note": "not a git repository"}


def _ledger_path(spec: Spec) -> Path:
    return spec.path.parent / ".claude-curator" / "ledger.jsonl"


def _heldout_reuse(spec: Spec, skill: str, heldout_digest: str, candidate_digest: str) -> int:
    path = _ledger_path(spec)
    if not path.is_file():
        return 0
    seen = set()
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("runner") == "fixture":
            continue  # offline fixtures replay scripted outcomes; they do not tune against the held-out set
        if entry.get("skill") == skill and entry.get("heldout_digest") == heldout_digest:
            seen.add(entry.get("candidate_digest"))
    seen.add(candidate_digest)
    return len(seen)


def _package_check(root: Path, file: str, text: str, present: bool) -> bool:
    path = root / file
    found = path.is_file() and text in path.read_text(errors="replace")
    return found if present else not found


def prepare(spec_path: Path, *, out_root: Path | None = None, runs_override: int | None = None,
            allow_unsupported: bool = False) -> Prepared:
    """Everything up to (not including) running agents. Writes the frozen inputs and the patch."""
    spec = load_spec(spec_path)
    suite = load_suite(spec.suite_path)
    heldout = resolve_against_suite(spec, suite)
    original_scan = scan_package(spec.skill_path)
    blocking = [f for f in original_scan.findings if f.kind == "unsupported-mcp"]
    if blocking and not allow_unsupported:
        raise ExperimentError("the skill depends on MCP tools that an isolated run cannot provide: "
                              + "; ".join(f"{f.message} ({f.location})" for f in blocking)
                              + ". Pass --allow-unsupported to run anyway and record it as a limitation.")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (out_root or spec.path.parent / ".claude-curator" / "runs") / f"{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True)
    try:
        return _prepare_into(spec, suite, heldout, original_scan, blocking, run_dir, runs_override)
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def _prepare_into(spec, suite, heldout, original_scan, blocking, run_dir, runs_override) -> Prepared:
    skill_dir_name = spec.skill_path.name

    shutil.copytree(spec.skill_path, run_dir / "original" / skill_dir_name, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", ".git"))
    original = load_package(run_dir / "original" / skill_dir_name)
    candidate = build_candidate(original.root, run_dir / "candidate" / skill_dir_name,
                                remove_section_heading=spec.remove_section, candidate_dir=spec.candidate_path)
    if candidate.digest == original.digest:
        raise ExperimentError("the candidate is identical to the original; there is nothing to assess")
    candidate_scan = scan_package(candidate.root)
    shutil.copytree(spec.suite_path, run_dir / "evals", ignore=lambda d, n: [x for x in n if (Path(d) == spec.suite_path and x == "results") or x == ".DS_Store"])
    shutil.copy2(spec.path, run_dir / "curator.toml")

    integrity: list[str] = []
    update_findings: list[str] = []
    notes: list[str] = []
    original_keys = {(f.kind, f.message) for f in original_scan.findings}
    for f in original_scan.findings:
        if f.severity == "error":
            update_findings.append(f"{f.kind}: {f.message} ({f.location})")
    for f in candidate_scan.findings:
        if f.severity == "error" and (f.kind, f.message) not in original_keys:
            integrity.append(f"candidate introduces {f.kind}: {f.message} ({f.location})")
    for f in original_scan.findings:
        if f.severity == "warning":
            notes.append(f"package warning, {f.kind}: {f.message}")
    if blocking:
        notes.append("run with --allow-unsupported although the skill references MCP tools the isolated runs cannot reach")
    newly_orphaned = sorted(set(candidate_scan.orphans) - set(original_scan.orphans))
    if newly_orphaned:
        notes.append("files no longer referenced after the change (kept in the patch; consider removing them in a follow-up): "
                     + ", ".join(newly_orphaned))

    requirements = []
    for req in spec.requirements:
        checks = [{"file": c.file, "text": c.text, "present": c.present,
                   "original": _package_check(original.root, c.file, c.text, c.present),
                   "candidate": _package_check(candidate.root, c.file, c.text, c.present)} for c in req.package_checks]
        requirements.append(RequirementInput(req.id, req.description, req.critical, [list(g) for g in req.graders], checks))

    patch = make_patch(original.root, candidate.root, repo_path=spec.repo_path)
    (run_dir / "change.patch").write_text(patch)

    plugins = {}
    for cond, pkg in (("original", original), ("candidate", candidate)):
        plugins[cond] = materialize_plugin(run_dir / "plugins" / cond, pkg, suite, skill_dir_name=skill_dir_name)
    if plugins["original"].suite_digest != plugins["candidate"].suite_digest:
        integrity.append("the frozen eval suites for original and candidate differ")

    heldout_files = {k: v for k, v in suite.files.items() if any(k.startswith(suite.cases[c].rel_dir + "/") for c in heldout)}
    heldout_digest = hashlib.sha256(json.dumps(sorted(heldout_files.items())).encode()).hexdigest()
    runs = runs_override or spec.run.runs
    planned = len(heldout) * runs * 3

    evidence = Evidence(
        heldout=list(heldout), explicit=list(spec.explicit), no_invocation_expected=list(spec.no_invocation_expected),
        requirements=requirements, config=spec.decision, integrity_issues=integrity,
        update_findings=update_findings, notes=notes,
        heldout_reuse=_heldout_reuse(spec, original.name, heldout_digest, candidate.digest),
        split_declared=spec.split_declared,
    )
    manifest = {
        "tool": {"name": "claude-curator", "version": __version__},
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "experiment": spec.name,
        "spec": str(spec.path),
        "change": {"remove_section": spec.remove_section,
                   "candidate": str(spec.candidate_path) if spec.candidate_path else None,
                   "rationale": spec.rationale},
        "skill": {"name": original.name, "dir_name": skill_dir_name, "source": str(spec.skill_path),
                  "repo_path": spec.repo_path, "original_digest": original.digest, "candidate_digest": candidate.digest,
                  "original_files": original.files, "candidate_files": candidate.files},
        "suite": {"source": str(spec.suite_path), "digest": suite.digest, "frozen_digest": plugins["original"].suite_digest,
                  "heldout": list(heldout), "development": list(spec.development), "heldout_digest": heldout_digest,
                  "cases": {name: {"graders": {g.name: g.type for g in c.graders.values()}} for name, c in suite.cases.items()}},
        "repository": _git_revision(spec.path.parent),
        "run": {"runs_per_case": runs, "model": spec.run.model or None, "judge_model": spec.run.judge_model or None,
                "allow_tools": list(spec.run.allow_tools), "scaffold": spec.run.scaffold, "planned_agent_runs": planned},
        "isolation_boundary": ISOLATION_BOUNDARY,
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "invocations": [],
    }
    return Prepared(spec, run_dir, heldout, planned, evidence, manifest, patch)


def execute(prep: Prepared, runner, *, max_runs: int | None = None, max_cost_usd: float | None = None,
            trust_plugin: bool = False, runs_override: int | None = None, progress=lambda msg: None) -> Decision:
    spec, run_dir = prep.spec, prep.run_dir
    max_runs = max_runs if max_runs is not None else spec.run.max_runs
    max_cost = max_cost_usd if max_cost_usd is not None else spec.run.max_cost_usd
    if max_runs is not None and prep.planned_runs > max_runs:
        raise BudgetError(f"the plan needs {prep.planned_runs} agent runs ({len(prep.heldout)} cases x "
                          f"{runs_override or spec.run.runs} runs x 3 conditions), above the limit of {max_runs}")
    if runner.paid:
        if max_cost is None:
            raise BudgetError("a paid runner needs a spending ceiling: set [run] max_cost_usd or pass --max-cost-usd")
        if max_runs is None:
            raise BudgetError("a paid runner needs a run limit: set [run] max_runs or pass --max-runs")
        if not spec.run.model:
            raise ExperimentError("pin [run] model so a model rollout is not mistaken for a skill effect")

    runs = runs_override or spec.run.runs
    frozen_digest = prep.manifest["suite"]["frozen_digest"]
    prep.manifest["runner"] = {"name": runner.name, "version": runner.version(), "paid": runner.paid}
    if not runner.paid:
        prep.manifest["runner"].update(label=FIXTURE_LABEL, scenario=str(runner.path))
    prep.manifest["budget"] = {"max_runs": max_runs, "max_cost_usd": max_cost,
                               "enforcement": "max_runs before start; max_cost_usd by the runner before each run starts "
                                              "(in-flight runs finish) and by claude-curator between invocations"}

    records: list[RunRecord] = []
    failed: set[tuple[str, str]] = set()  # (case, condition) whose invocation produced no usable result
    spent, stop_reason = 0.0, None
    plan = {"original": ("original", {"with": "original", "without": "none"}),
            "candidate": ("candidate", {"with": "candidate"})}
    for idx, case in enumerate(prep.heldout):
        order = ["original", "candidate"] if idx % 2 == 0 else ["candidate", "original"]
        for key in order:
            if stop_reason:
                break
            remaining = None if max_cost is None else round(max_cost - spent, 6)
            if remaining is not None and remaining <= 0:
                stop_reason = "cost ceiling reached"
                break
            plugin_cond, arms = plan[key]
            raw = run_dir / "raw" / f"{idx:03d}-{case}-{key}"
            inv = Invocation(plugin_dir=run_dir / "plugins" / plugin_cond, case=case, condition_arms=arms, runs=runs,
                             output_json=raw / "result.json", output_dir=raw / "runner-output",
                             model=spec.run.model, judge_model=spec.run.judge_model, allow_tools=spec.run.allow_tools,
                             scaffold=spec.run.scaffold, max_cost_usd=remaining, trust_plugin=trust_plugin)
            progress(f"[{idx + 1}/{len(prep.heldout)}] {case}: {key}")
            try:
                result = runner.invoke(inv)
            except KeyboardInterrupt:
                stop_reason = "interrupted by the operator"
                break
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "stdout.txt").write_text(result.stdout or "")
            (raw / "stderr.txt").write_text(result.stderr or "")
            entry = {"case": case, "plugin": key, "command": result.command, "exit_code": result.exit_code,
                     "seconds": round(result.seconds, 2), "error": result.error,
                     "cost_usd": (result.doc or {}).get("costUsd"), "partial": (result.doc or {}).get("partial"),
                     "partial_reason": (result.doc or {}).get("partialReason"),
                     "claude_version": (result.doc or {}).get("claudeVersion")}
            prep.manifest["invocations"].append(entry)
            if result.doc is None:
                tail = (result.stderr or "").strip().splitlines()[-3:]
                prep.evidence.integrity_issues.append(
                    f"runner failed for {case} ({key}): {result.error or 'no result'}" + (f" | {' / '.join(tail)}" if tail else ""))
                failed.update((case, cond) for cond in arms.values())
                continue
            if not any(c.get("name") == case for c in result.doc.get("cases") or []):
                prep.evidence.integrity_issues.append(
                    f"the runner returned no result for {case} ({key}, exit {result.exit_code}); "
                    "the case name may not match what `claude plugin eval` loaded")
                failed.update((case, cond) for cond in arms.values())
            try:
                records += normalize(result.doc, arms=arms, suite=load_suite(inv.plugin_dir / "evals"), source=str(raw / "result.json"))
            except ResultError as e:
                prep.evidence.integrity_issues.append(f"unreadable runner result for {case} ({key}): {e}")
                failed.update((case, cond) for cond in arms.values())
                stop_reason = "the runner's result format was not understood"
            cost = result.doc.get("costUsd")
            if isinstance(cost, (int, float)):
                spent += cost
            elif max_cost is not None:
                stop_reason = "the runner reported no cost, so the ceiling cannot be enforced"
            if result.doc.get("partial"):
                reason = result.doc.get("partialReason")
                stop_reason = {"cost_ceiling": "cost ceiling reached", "auth_failed": "authentication failed",
                               "interrupted": "interrupted"}.get(reason, f"partial run ({reason})")
            if suite_digest(run_dir / "plugins" / plugin_cond / "evals") != frozen_digest:
                prep.evidence.integrity_issues.append(f"the frozen eval suite changed during {case} ({key})")
                stop_reason = "eval suite changed during the experiment"
        if stop_reason:
            break

    planned = {(c, cond): runs for c in prep.heldout for cond in ("original", "candidate", "none")}
    records = fill_unstarted(records, {k: v for k, v in planned.items() if k in failed}, "runner produced no usable result",
                             status="infra_error")
    records = fill_unstarted(records, planned, stop_reason or "missing from the runner's result")
    if stop_reason:
        prep.evidence.integrity_issues.append(f"experiment stopped early: {stop_reason}")
    prep.manifest["budget"].update({"reported_cost_usd": round(spent, 6), "stopped": stop_reason})
    return finish(prep.run_dir, prep.manifest, prep.evidence, records, spec=spec)


def finish(run_dir: Path, manifest: dict, evidence: Evidence, records: list[RunRecord], *, spec: Spec | None) -> Decision:
    decision = decide(evidence, records)
    (run_dir / "records.jsonl").write_text("".join(json.dumps(r.to_json()) + "\n" for r in records))
    (run_dir / "evidence.json").write_text(json.dumps(evidence.to_json(), indent=2))
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (run_dir / "decision.json").write_text(json.dumps(decision.to_json(), indent=2))
    (run_dir / "report.md").write_text(render_report(manifest, evidence, decision, run_dir))
    if spec is not None:
        ledger = _ledger_path(spec)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as fh:
            fh.write(json.dumps({"at": manifest["created_at"], "skill": manifest["skill"]["name"],
                                 "heldout_digest": manifest["suite"]["heldout_digest"],
                                 "candidate_digest": manifest["skill"]["candidate_digest"],
                                 "runner": manifest.get("runner", {}).get("name"),
                                 "recommendation": decision.recommendation, "run_dir": str(run_dir)}) + "\n")
    return decision


def rerender(run_dir: Path) -> Decision:
    """Recompute the decision and report from stored evidence, without running agents."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    evidence = Evidence.from_json(json.loads((run_dir / "evidence.json").read_text()))
    records = [RunRecord.from_json(json.loads(l)) for l in (run_dir / "records.jsonl").read_text().splitlines() if l.strip()]
    return finish(run_dir, manifest, evidence, records, spec=None)
