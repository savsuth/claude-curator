"""Normalise `claude plugin eval` result documents (schemaVersion 1) into one
record per agent run, and classify how each run ended.

The per-run shape (score, turns, costUsd, judgeCostUsd, durationSeconds, error,
tracePath, skippedPaidGraders, aborted, graders[name, passed, weight,
explanation, withOnly, scored]) was read from Claude Code 2.1.296's result
schema; the documented subset is at
https://code.claude.com/docs/en/plugin-evals#json-result.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from claude_curator.suite import INVOCATION_GRADER, Suite

CONDITIONS = ("original", "candidate", "none")

_INFRA = re.compile(
    r"rate.?limit|usage.?limit|overloaded|authenticat|credential|api error|"
    r"econn|etimedout|network|socket|scaffold failed|sandbox|could not start|"
    r"failed to (load|start|spawn)|invalid case|\b(?:500|502|503|504|529)\b",
    re.I,
)
_AGENT_LIMIT = re.compile(r"^timed out after \d+s|max(imum)?[ _-]?turns|turn (cap|limit)", re.I)
_GRADER_FAIL = re.compile(r"^(grader|judge)\b.*\b(error|failed|unavailable)|^error:", re.I)


class ResultError(Exception):
    pass


@dataclass
class RunRecord:
    condition: str
    case: str
    run_index: int
    status: str  # ok | agent_limit | infra_error | grader_error | budget_skipped
    score: float | None = None
    graders: dict[str, bool | None] = field(default_factory=dict)
    invocation: str = "unavailable"  # observed | not_observed | unavailable | not_applicable
    cost_usd: float | None = None
    judge_cost_usd: float | None = None
    duration_seconds: float | None = None
    turns: int | None = None
    tokens: dict[str, int] | None = None
    error: str | None = None
    detail: str = ""
    trace_path: str | None = None
    source: str = ""

    @property
    def valid(self) -> bool:
        return self.status in ("ok", "agent_limit")

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "RunRecord":
        return cls(**data)


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_usage(trace_path: str | None) -> dict[str, int] | None:
    """Sum token usage from a session transcript, if the runner kept one."""
    if not trace_path:
        return None
    path = Path(trace_path)
    if not path.is_file():
        return None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    seen = False
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = (event.get("message") or {}).get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                seen = True
                for key in totals:
                    totals[key] += int(usage.get(key) or 0)
    except OSError:
        return None
    return totals if seen else None


def classify_run(run: dict) -> tuple[str, str]:
    """Return (status, detail) for one runner run object."""
    error = run.get("error")
    graders = run.get("graders") or []
    if run.get("skippedPaidGraders"):
        return "budget_skipped", "judge graders skipped by the cost ceiling; score not comparable"
    if not graders:
        return "infra_error", f"no grader results ({error or 'run did not start'})"
    if error and _INFRA.search(error) and not _AGENT_LIMIT.search(error):
        return "infra_error", error
    bad = [g.get("name", "?") for g in graders
           if not isinstance(g.get("passed"), bool) or _GRADER_FAIL.search(str(g.get("explanation") or ""))]
    if bad:
        return "grader_error", "grader failure: " + ", ".join(bad)
    if error and _AGENT_LIMIT.search(error):
        return "agent_limit", error
    if error:
        # Unknown errors are not silently trusted as agent behaviour.
        return "infra_error", f"unclassified run error: {error}"
    if run.get("aborted"):
        a = run["aborted"]
        return "ok", f"aborted by mock {a.get('server')}/{a.get('tool')}: {a.get('reason')}"
    return "ok", ""


def score_run(graders: dict[str, bool | None], case) -> float | None:
    total = passed = 0.0
    for name, grader in case.graders.items():
        if not grader.is_outcome:
            continue
        result = graders.get(name)
        if result is None:
            return None
        total += grader.weight
        passed += grader.weight if result else 0.0
    return passed / total if total else None


def normalize(doc: dict, *, arms: dict[str, str], suite: Suite, source: str) -> list[RunRecord]:
    """`arms` maps runner arms ("with", "without") to Claude Curator conditions."""
    if doc.get("schemaVersion") != 1:
        raise ResultError(f"{source}: unsupported result schemaVersion {doc.get('schemaVersion')!r}")
    records: list[RunRecord] = []
    for case_doc in doc.get("cases") or []:
        name = case_doc.get("name")
        if name not in suite.cases:
            raise ResultError(f"{source}: result names unknown case {name!r}")
        case = suite.cases[name]
        for arm, condition in arms.items():
            for i, run in enumerate((case_doc.get("arms") or {}).get(arm) or []):
                status, detail = classify_run(run)
                results = {g["name"]: g.get("passed") if isinstance(g.get("passed"), bool) else None
                           for g in run.get("graders") or [] if "name" in g}
                if condition == "none":
                    invocation = "not_applicable"
                elif INVOCATION_GRADER not in results or results[INVOCATION_GRADER] is None:
                    invocation = "unavailable"
                else:
                    invocation = "observed" if results[INVOCATION_GRADER] else "not_observed"
                no_graders = not run.get("graders")
                score = score_run(results, case) if status in ("ok", "agent_limit") else None
                if status in ("ok", "agent_limit") and score is None:
                    status, detail = "grader_error", "an outcome grader has no result"
                records.append(RunRecord(
                    condition=condition, case=name, run_index=i, status=status,
                    score=score,
                    graders={k: v for k, v in results.items() if k != INVOCATION_GRADER},
                    invocation=invocation,
                    # A run that never started carries a placeholder cost of 0; that is not a measurement.
                    cost_usd=None if no_graders else _num(run.get("costUsd")),
                    judge_cost_usd=None if no_graders else _num(run.get("judgeCostUsd")),
                    duration_seconds=_num(run.get("durationSeconds")),
                    turns=int(run["turns"]) if _num(run.get("turns")) is not None and not no_graders else None,
                    tokens=read_usage(run.get("tracePath")),
                    error=run.get("error"), detail=detail,
                    trace_path=run.get("tracePath") or None, source=source,
                ))
    return records


def fill_unstarted(records: list[RunRecord], planned: dict[tuple[str, str], int], reason: str,
                   status: str = "budget_skipped") -> list[RunRecord]:
    """Add explicit records for planned runs that never produced a result."""
    out = list(records)
    for (case, condition), n in planned.items():
        have = sum(1 for r in records if r.case == case and r.condition == condition)
        for i in range(have, n):
            out.append(RunRecord(condition=condition, case=case, run_index=i, status=status,
                                 invocation="not_applicable" if condition == "none" else "unavailable",
                                 detail=f"not started: {reason}"))
    return out
