# ADR 0001: Claude Curator v0.1 architecture

Status: accepted (2026-10-13). Each decision below was taken without a
question round because the brief asked to proceed; any of them can be
overturned, and the "Revisit if" line says what would change it.

## D1. Runner: adapt `claude plugin eval`, do not build a harness

`claude plugin eval` (Claude Code >= 2.1.269; verified locally on 2.1.296)
already gives each run a temporary HOME, working directory and Claude Code
configuration; loads only the plugin under test; hides the eval directory from
the agent; runs a no-plugin baseline arm; offers deterministic graders
(`regex`, `tool_used`, `tool_order`, `file_exists`) and judge graders; and
enforces `--max-cost-usd`. Source: https://code.claude.com/docs/en/plugin-evals
(sections "How runs are isolated", "Command options", "JSON result").

Claude Curator wraps the original and candidate packages as two plugins with the
same plugin name and skill name, and runs:

- original plugin with `--ablation with-without` -> conditions `original` and `none`
- candidate plugin with `--ablation none` -> condition `candidate`

Invocations are interleaved per held-out case, and the order of original vs
candidate alternates between cases, to limit drift (model rollout, rate limits)
between conditions.

Rejected: agent-skill-eval (MIT) and skill-evaluator (MIT) as runners. Both are
general frameworks; agent-skill-eval invokes `claude -p
--dangerously-skip-permissions` in a temp git repo without isolating HOME or
user settings, and skill-evaluator injects skill text into prompts, which
tests the text but not the installed skill package or its routing. SkillAdam
optimises skills automatically, which is out of scope. ECC skill-stocktake and
mblode capability-delta informed the decision vocabulary (keep / cut / retire,
"label static judgements as such", "repeat borderline results") but contain no
code to reuse.

Revisit if: the official command changes its result schema (we parse
`schemaVersion: 1` only), or a second host must be supported.

## D2. Stack: Python >= 3.11, standard library only

TOML spec via `tomllib`, tests via `unittest`, subprocess for the runner. No
runtime dependencies, so the tool installs nowhere it should not and runs from
a checkout with `python3 -m claude_curator`.

## D3. Eval suite format: the official plugin-eval case layout

Cases are `evals/<case>/prompt.md` (or `case.yaml`) plus `graders/*.md`. The
suite is copied byte-for-byte into both plugin directories and its hash is
checked before every invocation, so graders are identical across conditions.
skill-creator `evals/evals.json` suites are not accepted in v0.1 (the docs note
the two formats do not read each other); an importer is deferred.

## D4. Unit of analysis and the tolerance method

The maintainer declares `tolerance` (maximum acceptable drop in mean case
score, on a 0..1 scale) and `confidence` (default 0.95).

- Run score: weighted fraction of *outcome graders* passed. Outcome graders
  exclude `tool_used` graders on the `Skill` tool and graders marked
  `arm: with-only`, in every condition, because they cannot pass without the
  skill. Claude Curator computes this itself instead of using the runner's
  `score`, because under `--ablation none` the runner would count the
  invocation indicator.
- Case score: mean of valid run scores. Repeated runs of one case are averaged
  first; they are not treated as independent cases.
- Effect: mean over held-out cases of (candidate case score - original case score).
- Uncertainty: two-stage bootstrap (resample cases, then runs within each
  resampled case and condition), fixed seed recorded in the decision, 10 000
  resamples. One-sided lower bound at `confidence`.

Rules, applied in order:

1. Integrity problems (missing candidate dependencies, grader suite mismatch,
   too few valid runs or cases, budget interruption leaving coverage short,
   held-out cases lacking runs) -> `inconclusive`.
2. Routing: in the original condition, if the invocation indicator was observed
   in fewer than `min_invocation_rate` of the runs for cases expected to invoke
   the skill -> `inconclusive` with a routing diagnosis. A skill that does not
   activate has not had its instructions tested.
3. Critical requirement violated by the candidate (static check fails, or a
   linked grader failed in a candidate run while the original passed it in all
   its runs) -> `keep`, regardless of score or cost.
4. Critical requirement the original itself does not reliably meet ->
   `inconclusive` plus an update finding.
5. Retirement: if the no-skill condition is within tolerance of the original
   (lower bound of none - original >= -tolerance), the original's invocation
   was observed, and every critical requirement also holds without the skill
   -> `retirement-candidate` (a nomination for review, never an automatic removal).
6. Lower bound of candidate - original >= -tolerance -> `simplify`.
7. Upper bound of candidate - original < -tolerance -> `keep`.
8. Otherwise `inconclusive`.

Equal observed pass rates never decide by themselves: rule 6 needs the
*bound*, not the point estimate, inside tolerance, and rule 1 enforces minimum
coverage (`min_cases`, default 6; `min_runs_per_case`, default 3). Cost and
duration are reported but never enter the rules.

Known limitation recorded in every report: with zero observed variance the
bootstrap bound collapses to the point estimate; the report also states the
rule-of-three bound (about 3/n) on the share of unseen cases that could regress.

Revisit if: maintainers need per-case regression limits, or suites grow large
enough for a formal mixed-effects model.

## D5. Invocation evidence is tri-state

Claude Curator adds one grader, `_curator_invoked` (`tool_used`, `tool: Skill`,
`input_match` on the skill name, `arm: with-only`), to the frozen suite copy.
Per run, invocation is `observed` (grader passed), `not_observed` (grader ran
and failed), or `unavailable` (no grader result). `not_observed` is never
reported as "did not invoke": a slash-command expansion, for example, may not
appear as a `Skill` tool call. Cases are labelled `natural` or `explicit` in the
spec; the report keeps the two apart.

## D6. Failure classification

Per run, from the runner JSON:

- `infra_error`: no grader results (case failed to load or run did not start),
  or an error matching rate/usage limit, authentication, overload, network, or
  scaffold failure. Excluded from scores; its reported cost is treated as
  unavailable because the runner writes a placeholder 0.
- `grader_error`: a grader result without a boolean `passed`, or an
  explanation reporting a judge or grader failure. Excluded.
- `budget_skipped`: `skippedPaidGraders` true, or a planned run that never
  started because a limit was reached. Excluded.
- `agent_limit`: timed out or hit the turn cap. Kept and scored (the runner
  grades what was produced); counted in the report.
- `ok`: everything else, including mock aborts (scored 0 by design).

## D7. Budgets

`max_runs` caps planned agent runs (cases x runs x 3 conditions) and is
checked before anything starts. `max_cost_usd` is required for the real runner;
each invocation receives the remaining budget as `--max-cost-usd`, which the
CLI checks before each run starts (in-flight overrun is bounded to one run at
concurrency 1), and Claude Curator stops between invocations once the reported
spend reaches the ceiling. Costs are the CLI's list-price estimates, not plan
usage or invoices.

## D8. Held-out discipline

The spec lists `development` and `heldout` case names; only held-out cases are
assessed. A local ledger (`.claude-curator/ledger.jsonl`) records each assessment
with the held-out suite hash and candidate hash; the report warns when the same
held-out set has assessed more than `max_heldout_reuse` distinct candidates.
If no split is declared, every case is treated as held-out and the report says
the tuning history is unknown.
