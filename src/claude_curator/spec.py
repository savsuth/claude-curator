"""The experiment spec, `curator.toml`. One skill, one change, one suite."""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from claude_curator.suite import Suite


class SpecError(Exception):
    pass


@dataclass(frozen=True)
class PackageCheck:
    file: str
    text: str
    present: bool = True  # False: the text must be absent


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    critical: bool
    graders: tuple[tuple[str, str], ...]  # (case, grader)
    package_checks: tuple[PackageCheck, ...]


@dataclass(frozen=True)
class DecisionConfig:
    tolerance: float = 0.05
    confidence: float = 0.95
    min_cases: int = 6
    min_runs_per_case: int = 3
    min_invocation_rate: float = 0.8
    max_incomplete_fraction: float = 0.2
    max_heldout_reuse: int = 3
    bootstrap_resamples: int = 10_000
    seed: int = 20261013


@dataclass(frozen=True)
class RunConfig:
    runs: int = 3
    model: str = ""
    judge_model: str = ""
    allow_tools: tuple[str, ...] = ()
    scaffold: bool = False
    max_runs: int | None = None
    max_cost_usd: float | None = None


@dataclass
class Spec:
    path: Path
    name: str
    skill_path: Path
    repo_path: str
    remove_section: str | None
    candidate_path: Path | None
    rationale: str
    suite_path: Path
    heldout: tuple[str, ...]
    development: tuple[str, ...]
    explicit: tuple[str, ...]
    no_invocation_expected: tuple[str, ...]
    split_declared: bool
    requirements: tuple[Requirement, ...]
    decision: DecisionConfig
    run: RunConfig
    raw: dict = field(default_factory=dict)


def _str_list(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SpecError(f"{where} must be a list of strings")
    return tuple(value)


def _known(table: dict, allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise SpecError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")


def _repo_relative(skill_path: Path, fallback: str) -> str:
    """The skill's path relative to its git repository root, for the patch.
    Outside a repository, the path as written in the spec is the best guess."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=skill_path, capture_output=True, text=True, check=True).stdout.strip()
        return skill_path.resolve().relative_to(Path(top).resolve()).as_posix()
    except (OSError, ValueError, subprocess.CalledProcessError):
        return fallback.strip("/")


def load_spec(path: Path) -> Spec:
    path = Path(path).resolve()
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"{path}: {e}") from None
    base = path.parent
    _known(raw, {"experiment", "skill", "change", "suite", "decision", "run", "requirements"}, "curator.toml")

    skill = raw.get("skill") or {}
    _known(skill, {"path", "repo_path"}, "[skill]")
    if "path" not in skill:
        raise SpecError("[skill] path is required")
    change = raw.get("change") or {}
    _known(change, {"remove_section", "candidate", "rationale"}, "[change]")
    if ("remove_section" in change) == ("candidate" in change):
        raise SpecError("[change] needs exactly one of remove_section or candidate")
    if not str(change.get("rationale", "")).strip():
        raise SpecError("[change] rationale is required: say why this change is proposed")
    suite = raw.get("suite") or {}
    _known(suite, {"path", "heldout", "development", "explicit", "no_invocation_expected"}, "[suite]")
    if "path" not in suite:
        raise SpecError("[suite] path is required")

    dec_raw = raw.get("decision") or {}
    _known(dec_raw, set(DecisionConfig.__dataclass_fields__), "[decision]")
    decision = DecisionConfig(**dec_raw)
    if not 0 <= decision.tolerance < 1:
        raise SpecError("[decision] tolerance must be in [0, 1)")
    if not 0.5 < decision.confidence < 1:
        raise SpecError("[decision] confidence must be in (0.5, 1)")
    if decision.min_cases < 2 or decision.min_runs_per_case < 1:
        raise SpecError("[decision] min_cases must be >= 2 and min_runs_per_case >= 1")

    run_raw = dict(raw.get("run") or {})
    _known(run_raw, set(RunConfig.__dataclass_fields__), "[run]")
    run_raw["allow_tools"] = _str_list(run_raw.get("allow_tools"), "[run] allow_tools")
    run = RunConfig(**run_raw)
    if run.runs < decision.min_runs_per_case:
        raise SpecError(f"[run] runs ({run.runs}) is below [decision] min_runs_per_case ({decision.min_runs_per_case})")

    requirements = []
    seen = set()
    for i, r in enumerate(raw.get("requirements") or []):
        where = f"[[requirements]] #{i + 1}"
        _known(r, {"id", "description", "critical", "graders", "package_contains", "package_excludes"}, where)
        rid = r.get("id")
        if not rid or rid in seen:
            raise SpecError(f"{where}: needs a unique id")
        seen.add(rid)
        graders = []
        for ref in _str_list(r.get("graders"), f"{where} graders"):
            if ref.count("/") != 1:
                raise SpecError(f"{where}: grader reference {ref!r} must be 'case/grader'")
            graders.append(tuple(ref.split("/")))
        checks = []
        for key, present in (("package_contains", True), ("package_excludes", False)):
            for c in r.get(key) or []:
                if not isinstance(c, dict) or set(c) != {"file", "text"}:
                    raise SpecError(f"{where}: {key} entries need exactly `file` and `text`")
                checks.append(PackageCheck(c["file"], c["text"], present))
        if not graders and not checks:
            raise SpecError(f"{where}: a requirement needs at least one grader or package check")
        requirements.append(Requirement(rid, str(r.get("description", "")), bool(r.get("critical", True)), tuple(graders), tuple(checks)))

    skill_path = (base / skill["path"]).resolve()
    return Spec(
        path=path,
        name=str((raw.get("experiment") or {}).get("name") or path.parent.name),
        skill_path=skill_path,
        repo_path=str(skill["repo_path"]).strip("/") if skill.get("repo_path") else _repo_relative(skill_path, skill["path"]),
        remove_section=change.get("remove_section"),
        candidate_path=(base / change["candidate"]).resolve() if "candidate" in change else None,
        rationale=str(change["rationale"]).strip(),
        suite_path=(base / suite["path"]).resolve(),
        heldout=_str_list(suite.get("heldout"), "[suite] heldout"),
        development=_str_list(suite.get("development"), "[suite] development"),
        explicit=_str_list(suite.get("explicit"), "[suite] explicit"),
        no_invocation_expected=_str_list(suite.get("no_invocation_expected"), "[suite] no_invocation_expected"),
        split_declared=bool(suite.get("heldout") or suite.get("development")),
        requirements=tuple(requirements),
        decision=decision,
        run=run,
        raw=raw,
    )


def resolve_against_suite(spec: Spec, suite: Suite) -> tuple[str, ...]:
    """Check every case and grader the spec names, and return the held-out cases."""
    names = set(suite.cases)
    for label, group in (("heldout", spec.heldout), ("development", spec.development),
                         ("explicit", spec.explicit), ("no_invocation_expected", spec.no_invocation_expected)):
        missing = sorted(set(group) - names)
        if missing:
            raise SpecError(f"[suite] {label} names unknown case(s): {', '.join(missing)}")
    overlap = set(spec.heldout) & set(spec.development)
    if overlap:
        raise SpecError(f"case(s) both held-out and development: {', '.join(sorted(overlap))}")
    heldout = spec.heldout or tuple(sorted(names - set(spec.development)))
    if not heldout:
        raise SpecError("no held-out cases to assess")
    for case in heldout:
        if not any(g.is_outcome for g in suite.cases[case].graders.values()):
            raise SpecError(f"held-out case {case!r} has no outcome grader (only skill-invocation checks), so it cannot be scored")
    for req in spec.requirements:
        for case, grader in req.graders:
            if case not in suite.cases or grader not in suite.cases[case].graders:
                raise SpecError(f"requirement {req.id}: grader {case}/{grader} not found in the suite")
            if case not in heldout:
                raise SpecError(f"requirement {req.id}: case {case!r} is not held-out, so it cannot assess the candidate")
            if not suite.cases[case].graders[grader].is_outcome:
                raise SpecError(f"requirement {req.id}: {case}/{grader} only measures skill invocation, not an outcome")
    return heldout
