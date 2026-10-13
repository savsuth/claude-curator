"""Decision rules (ADR 0001, D4). Pure: evidence in, decision out.

Effects are differences in mean case score (0..1). Repeated runs of one case are
averaged inside that case; cases are the unit that is resampled, so repeated
trials never count as extra task coverage.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

from claude_curator.results import RunRecord
from claude_curator.spec import DecisionConfig

KEEP, SIMPLIFY, RETIRE, INCONCLUSIVE = "keep", "simplify", "retirement-candidate", "inconclusive"


@dataclass
class RequirementInput:
    id: str
    description: str
    critical: bool
    graders: list[list[str]]  # [case, grader]
    package_checks: list[dict]  # {file, text, present, original: bool, candidate: bool}


@dataclass
class Evidence:
    """Everything the rules read. Built by the experiment, stored as evidence.json."""
    heldout: list[str]
    explicit: list[str]
    no_invocation_expected: list[str]
    requirements: list[RequirementInput]
    config: DecisionConfig
    integrity_issues: list[str] = field(default_factory=list)
    update_findings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    heldout_reuse: int = 0
    split_declared: bool = True

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Evidence":
        data = dict(data)
        data["config"] = DecisionConfig(**data["config"])
        data["requirements"] = [RequirementInput(**r) for r in data["requirements"]]
        return cls(**data)


@dataclass
class Effect:
    comparison: str
    n_cases: int
    estimate: float | None
    lower: float | None
    upper: float | None
    confidence: float

    def describe(self) -> str:
        if self.estimate is None:
            return f"{self.comparison}: not estimable"
        return (f"{self.comparison}: {self.estimate:+.3f} "
                f"(one-sided {self.confidence:.0%} bounds {self.lower:+.3f} / {self.upper:+.3f}, {self.n_cases} cases)")


@dataclass
class RequirementVerdict:
    id: str
    critical: bool
    status: str  # preserved | violated | unmet_in_original | insufficient
    holds_without_skill: bool | None
    details: list[str]


@dataclass
class Decision:
    recommendation: str
    reasons: list[str]
    effects: dict[str, Effect]
    requirements: list[RequirementVerdict]
    update_findings: list[str]
    routing_findings: list[str]
    coverage: dict
    invocation: dict
    failures: dict
    cost: dict
    case_scores: dict
    limitations: list[str]
    method: dict

    def to_json(self) -> dict:
        return asdict(self)


# --- statistics ------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / len(xs)


def paired_effect(pairs: list[tuple[list[float], list[float]]], *, comparison: str, cfg: DecisionConfig, seed_offset: int) -> Effect:
    """Mean over cases of mean(b runs) - mean(a runs), with a two-stage bootstrap:
    resample cases, then resample runs inside each resampled case and condition."""
    n = len(pairs)
    if n == 0:
        return Effect(comparison, 0, None, None, None, cfg.confidence)
    estimate = _mean([_mean(b) - _mean(a) for a, b in pairs])
    rng = random.Random(cfg.seed + seed_offset)
    stats = []
    for _ in range(cfg.bootstrap_resamples):
        total = 0.0
        for _ in range(n):
            a, b = pairs[rng.randrange(n)]
            total += _mean([b[rng.randrange(len(b))] for _ in b]) - _mean([a[rng.randrange(len(a))] for _ in a])
        stats.append(total / n)
    stats.sort()
    k_low = int((1 - cfg.confidence) * (len(stats) - 1))
    k_high = int(cfg.confidence * (len(stats) - 1))
    return Effect(comparison, n, estimate, stats[k_low], stats[k_high], cfg.confidence)


# --- rules -----------------------------------------------------------------------------

def _valid(records, case, condition):
    return [r for r in records if r.case == case and r.condition == condition and r.valid]


def _grader_results(records, case, condition, grader):
    return [r.graders.get(grader) for r in _valid(records, case, condition)]


def _requirement(req: RequirementInput, records: list[RunRecord]) -> RequirementVerdict:
    statuses, details = [], []
    holds_none: bool | None = True
    for check in req.package_checks:
        verb = "contains" if check["present"] else "omits"
        if not check["original"]:
            statuses.append("unmet_in_original")
            details.append(f"original package does not satisfy: {check['file']} {verb} {check['text']!r}")
        elif not check["candidate"]:
            statuses.append("violated")
            details.append(f"candidate package fails: {check['file']} {verb} {check['text']!r}")
        else:
            statuses.append("preserved")
        holds_none = False  # a package requirement cannot hold once the package is gone
    for case, grader in req.graders:
        orig = _grader_results(records, case, "original", grader)
        cand = _grader_results(records, case, "candidate", grader)
        none = _grader_results(records, case, "none", grader)
        label = f"{case}/{grader}: original {sum(1 for x in orig if x)}/{len(orig)}, candidate {sum(1 for x in cand if x)}/{len(cand)}, none {sum(1 for x in none if x)}/{len(none)}"
        details.append(label)
        if not orig or not cand or any(x is None for x in orig + cand):
            statuses.append("insufficient")
        elif not all(orig):
            statuses.append("unmet_in_original")
        elif not all(cand):
            statuses.append("violated")
        else:
            statuses.append("preserved")
        if not none:
            if holds_none is not False:
                holds_none = None  # no valid no-skill runs: unknown
        elif not all(x is True for x in none):
            holds_none = False
    for status in ("violated", "unmet_in_original", "insufficient", "preserved"):
        if status in statuses:
            return RequirementVerdict(req.id, req.critical, status, holds_none, details)
    return RequirementVerdict(req.id, req.critical, "insufficient", holds_none, details)


def _rate(records, cases, condition):
    runs = [r for c in cases for r in _valid(records, c, condition)]
    known = [r for r in runs if r.invocation in ("observed", "not_observed")]
    observed = sum(1 for r in known if r.invocation == "observed")
    return {"observed": observed, "known": len(known), "unavailable": len(runs) - len(known),
            "rate": observed / len(known) if known else None}


def _cost(records):
    out = {}
    for cond in ("original", "candidate", "none"):
        rs = [r for r in records if r.condition == cond and r.status != "budget_skipped"]
        costs = [r.cost_usd for r in rs if r.cost_usd is not None]
        durations = [r.duration_seconds for r in rs if r.duration_seconds is not None]
        tokens = [r.tokens for r in rs if r.tokens]
        out[cond] = {
            "runs": len(rs),
            "cost_usd_sum_of_reported": round(sum(costs), 6) if costs else None,
            "cost_unavailable_runs": len(rs) - len(costs),
            "mean_duration_seconds": round(_mean(durations), 2) if durations else None,
            "duration_unavailable_runs": len(rs) - len(durations),
            "output_tokens_sum": sum(t.get("output_tokens", 0) for t in tokens) if tokens else None,
            "token_unavailable_runs": len(rs) - len(tokens),
        }
    return out


def decide(evidence: Evidence, records: list[RunRecord]) -> Decision:
    cfg = evidence.config
    records = [r for r in records if r.case in evidence.heldout]
    reasons: list[str] = []
    limitations: list[str] = list(evidence.notes)

    # Coverage
    complete, incomplete = [], {}
    for case in evidence.heldout:
        counts = {c: len(_valid(records, case, c)) for c in ("original", "candidate", "none")}
        if min(counts.values()) >= cfg.min_runs_per_case:
            complete.append(case)
        else:
            incomplete[case] = counts
    coverage = {"heldout": len(evidence.heldout), "complete": complete, "incomplete": incomplete,
                "min_runs_per_case": cfg.min_runs_per_case, "min_cases": cfg.min_cases}

    failures: dict[str, dict[str, int]] = {}
    for r in records:
        failures.setdefault(r.condition, {}).setdefault(r.status, 0)
        failures[r.condition][r.status] += 1

    expected = [c for c in complete if c not in evidence.no_invocation_expected]
    natural = [c for c in expected if c not in evidence.explicit]
    explicit = [c for c in expected if c in evidence.explicit]
    invocation = {
        "original": _rate(records, expected, "original"),
        "candidate": _rate(records, expected, "candidate"),
        "original_natural": _rate(records, natural, "original"),
        "original_explicit": _rate(records, explicit, "original"),
        "label": "observed means a Skill tool call naming the skill was seen; not_observed is not proof it was absent",
    }

    def case_means(cond):
        return {c: _mean([r.score for r in _valid(records, c, cond)]) for c in complete}
    case_scores = {cond: case_means(cond) for cond in ("original", "candidate", "none")}

    pairs_cand = [([r.score for r in _valid(records, c, "original")], [r.score for r in _valid(records, c, "candidate")]) for c in complete]
    pairs_none = [([r.score for r in _valid(records, c, "original")], [r.score for r in _valid(records, c, "none")]) for c in complete]
    effects = {
        "candidate_vs_original": paired_effect(pairs_cand, comparison="candidate - original", cfg=cfg, seed_offset=0),
        "none_vs_original": paired_effect(pairs_none, comparison="no skill - original", cfg=cfg, seed_offset=1),
    }
    verdicts = [_requirement(r, records) for r in evidence.requirements]
    critical = [v for v in verdicts if v.critical]
    update_findings = list(evidence.update_findings)
    routing_findings: list[str] = []
    for v in critical:
        if v.status == "unmet_in_original":
            update_findings.append(f"requirement {v.id} is not reliably met by the current skill: " + "; ".join(v.details))

    nat = invocation["original_natural"]
    if nat["rate"] is not None and nat["rate"] < cfg.min_invocation_rate:
        routing_findings.append(
            f"natural requests invoked the skill in {nat['observed']}/{nat['known']} original runs; "
            "this is a routing diagnosis (description or trigger), not evidence that the instructions are unnecessary")
    orig_rate, cand_rate = invocation["original"]["rate"], invocation["candidate"]["rate"]
    if orig_rate is not None and cand_rate is not None and orig_rate - cand_rate > 0.2:
        routing_findings.append(f"invocation dropped from {orig_rate:.0%} (original) to {cand_rate:.0%} (candidate)")

    # Rules, in order
    rec = None
    incomplete_critical = sorted({c for v in evidence.requirements if v.critical for c, _ in v.graders} & set(incomplete))
    if evidence.integrity_issues:
        rec = INCONCLUSIVE
        reasons += [f"integrity: {i}" for i in evidence.integrity_issues]
    if len(complete) < cfg.min_cases:
        rec = INCONCLUSIVE
        reasons.append(f"only {len(complete)} held-out case(s) have at least {cfg.min_runs_per_case} valid runs in all three conditions; {cfg.min_cases} are required")
    if evidence.heldout and len(incomplete) / len(evidence.heldout) > cfg.max_incomplete_fraction:
        rec = INCONCLUSIVE
        reasons.append(f"{len(incomplete)} of {len(evidence.heldout)} held-out cases lack enough valid runs (limit {cfg.max_incomplete_fraction:.0%})")
    if incomplete_critical:
        rec = INCONCLUSIVE
        reasons.append(f"cases linked to critical requirements lack enough valid runs: {', '.join(incomplete_critical)}")

    if rec is None:
        o = invocation["original"]
        if o["rate"] is None:
            rec = INCONCLUSIVE
            reasons.append("no invocation evidence was available, so it is unknown whether the skill was loaded at all")
        elif o["rate"] < cfg.min_invocation_rate:
            rec = INCONCLUSIVE
            reasons.append(f"the original skill was observed invoked in only {o['observed']}/{o['known']} runs "
                           f"(minimum {cfg.min_invocation_rate:.0%}); its instructions were not exercised enough to judge removal")

    if rec is None:
        violated = [v for v in critical if v.status == "violated"]
        unmet = [v for v in critical if v.status in ("unmet_in_original", "insufficient")]
        if violated:
            rec = KEEP
            reasons += [f"critical requirement {v.id} violated by the candidate: " + "; ".join(v.details) for v in violated]
        elif unmet:
            rec = INCONCLUSIVE
            reasons += [f"critical requirement {v.id} is {v.status.replace('_', ' ')}, so preservation cannot be shown" for v in unmet]

    cand, none = effects["candidate_vs_original"], effects["none_vs_original"]
    tol = cfg.tolerance
    if rec is None and none.lower is not None and none.lower >= -tol and all(v.holds_without_skill for v in critical):
        rec = RETIRE
        reasons.append(f"without the skill, scores stay within tolerance {tol:.2f} of the original ({none.describe()}) "
                       "and every critical requirement also held without it")
    if rec is None and cand.lower is not None:
        if cand.lower >= -tol:
            rec = SIMPLIFY
            reasons.append(f"the candidate's lower bound stays within tolerance {tol:.2f} ({cand.describe()}) and all critical requirements were preserved")
        elif cand.upper < -tol:
            rec = KEEP
            reasons.append(f"the candidate is worse than tolerance {tol:.2f} even at its upper bound ({cand.describe()})")
        else:
            rec = INCONCLUSIVE
            reasons.append(f"the interval crosses the tolerance {tol:.2f} ({cand.describe()}); more cases or runs are needed")
    if rec is None:
        rec = INCONCLUSIVE
        reasons.append("the effect could not be estimated")

    regressed = [c for c in complete if case_scores["candidate"][c] - case_scores["original"][c] < -tol]
    if complete:
        if not regressed:
            limitations.append(f"no held-out case regressed beyond tolerance; with {len(complete)} cases, the 95% upper bound on the share "
                               f"of similar tasks that could regress is still about {min(1.0, 3 / len(complete)):.0%} (rule of three)")
        else:
            limitations.append(f"held-out cases where the candidate scored more than the tolerance below the original "
                               f"(possibly run-to-run noise; inspect raw/): {', '.join(regressed)}")
    if not evidence.split_declared:
        limitations.append("no development/held-out split was declared, so it is unknown whether the candidate was tuned on these cases")
    if evidence.heldout_reuse > cfg.max_heldout_reuse:
        limitations.append(f"this held-out set has already assessed {evidence.heldout_reuse} candidates for this skill "
                           f"(limit {cfg.max_heldout_reuse}); results may be overfit to it")

    return Decision(
        recommendation=rec, reasons=reasons, effects=effects, requirements=verdicts,
        update_findings=update_findings, routing_findings=routing_findings, coverage=coverage,
        invocation=invocation, failures=failures, cost=_cost(records), case_scores=case_scores,
        limitations=limitations,
        method={"unit": "held-out case (runs averaged within case)", "interval": "two-stage bootstrap, percentile",
                "resamples": cfg.bootstrap_resamples, "seed": cfg.seed, "confidence": cfg.confidence, "tolerance": tol},
    )
