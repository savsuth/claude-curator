# Claude Curator: implementation plan (v0.1)

## Question v0.1 answers

"Can we remove this specific section from a skill without meaningfully reducing
its usefulness or violating its requirements in this project?"

Output: a reviewable Git patch, the raw evidence, and one recommendation:
`keep`, `simplify`, `retirement-candidate`, or `inconclusive`. Update findings
(broken or outdated instructions) are reported separately.

## Scope

In: one skill package, one proposed change (a named section to remove, or a
maintainer-supplied candidate directory), one existing eval suite in the
official `claude plugin eval` case format, declared requirements, three
conditions (original, candidate, no skill), an explicit decision rule, a
Markdown report, a patch, and an offline fixture runner.

Deferred: automated rewriting, hosted service, dashboards, continuous
monitoring, model discovery, other agent hosts, collection-wide audits,
importing skill-creator `evals.json` suites, automatic dependency repair.

## Architecture

```
curator.toml ─> spec.py ──> experiment.py ──> runners.py ──> claude plugin eval (real)
                               │                   └───────> FixtureRunner (offline)
skill dir ───> package.py ─────┤
evals/ ──────> suite.py ───────┤  results.py   normalises runner JSON into RunRecords
                               ├─ decide.py    pure decision rules over RunRecords
                               ├─ patch.py     unified diff original -> candidate
                               └─ report.py    report.md from the stored evidence
```

Each experiment writes one self-contained evidence directory:

```
.claude-curator/runs/<timestamp>-<id>/
  manifest.json        hashes, versions, model settings, spec snapshot, budget
  original/ candidate/ frozen copies of both packages (reversible)
  evals/               frozen copy of the suite used
  plugins/             the two plugin directories handed to the runner
  raw/                 each runner invocation: command, stdout, stderr, result JSON
  records.jsonl        one normalised record per agent run
  decision.json        recommendation, statistics, requirement verdicts, reasons
  change.patch         git-applyable patch for the maintainer's repository
  report.md            human-readable report
```

`claude-curator report <run-dir>` recomputes `decision.json` and `report.md` from
`records.jsonl` alone, so a result can be re-derived without re-running agents.

## Build order (vertical slices, each with tests)

1. Package scan: section removal, dependency detection, unsupported features, hashing.
2. Patch generation that `git apply --check` accepts.
3. Spec loading and validation (requirements, case split, budgets).
4. Suite handling: case discovery, grader inventory, invocation-indicator grader, plugin materialisation.
5. Result normalisation and failure classification (agent vs infrastructure vs grader vs budget).
6. Decision rules: non-inferiority, requirements, routing, retirement, inconclusive.
7. Experiment orchestration with budget and run limits; fixture runner.
8. Real runner adapter for `claude plugin eval`, tested against a stub executable.
9. Report, CLI, offline examples, README.
10. Review: floor-guard, second opinion, fix findings.
