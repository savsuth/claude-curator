"""Eval suites in the `claude plugin eval` case layout, and the plugin directories
Claude Curator hands to the runner.

Format reference: https://code.claude.com/docs/en/plugin-evals#eval-suite-reference
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from claude_curator.package import Package, PackageError, parse_frontmatter

PLUGIN_NAME = "curator-under-test"
INVOCATION_GRADER = "_curator_invoked"
SKIP_DIRS = {"results", "mocks", ".DS_Store"}


class SuiteError(Exception):
    pass


@dataclass(frozen=True)
class Grader:
    name: str
    type: str
    weight: float = 1.0
    tool: str = ""
    arm: str = ""

    @property
    def is_outcome(self) -> bool:
        """Outcome graders count toward scores in every condition. Graders that can
        only pass when the skill is present would penalise the no-skill condition."""
        if self.name == INVOCATION_GRADER or self.arm == "with-only":
            return False
        return not (self.type == "tool_used" and self.tool == "Skill")


@dataclass(frozen=True)
class Case:
    name: str
    rel_dir: str
    prompt: str
    graders: dict[str, Grader]


@dataclass
class Suite:
    root: Path
    cases: dict[str, Case]
    digest: str
    files: dict[str, str] = field(default_factory=dict)


def _file_hashes(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS - {"mocks"}:
            continue
        if path.name == ".DS_Store" or not path.is_file():
            continue
        out[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256("".join(f"{p}\0{h}\n" for p, h in sorted(files.items())).encode()).hexdigest()


def _read_meta(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text()
    try:
        meta, body_start = parse_frontmatter(text)
    except PackageError as e:
        raise SuiteError(f"{path}: {e}") from None
    return meta, "\n".join(text.splitlines()[body_start:]).strip()


def load_suite(root: Path) -> Suite:
    root = Path(root)
    if not root.is_dir():
        raise SuiteError(f"{root}: eval suite directory not found")
    cases: dict[str, Case] = {}
    for marker in sorted(list(root.rglob("prompt.md")) + list(root.rglob("case.yaml"))):
        case_dir = marker.parent
        rel = case_dir.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        if any(c.rel_dir == rel.as_posix() for c in cases.values()):
            continue
        meta, prompt = {}, ""
        if (case_dir / "case.yaml").exists():
            text = (case_dir / "case.yaml").read_text()
            if any(line.startswith("graders:") for line in text.splitlines()):
                raise SuiteError(f"{case_dir}: graders inside case.yaml are not supported in v0.1; use graders/*.md")
            if any(line.startswith("plugins:") for line in text.splitlines()):
                raise SuiteError(f"{case_dir}: the case pins `plugins`; Claude Curator decides which plugin is loaded")
            for line in text.splitlines():
                if line.startswith("name:"):
                    meta["name"] = line.split(":", 1)[1].strip().strip("'\"")
        if (case_dir / "prompt.md").exists():
            pmeta, prompt = _read_meta(case_dir / "prompt.md")
            meta.update(pmeta)
        if "plugins" in meta:
            raise SuiteError(f"{case_dir}: the case pins `plugins`; Claude Curator decides which plugin is loaded")
        graders = {}
        for gpath in sorted((case_dir / "graders").glob("*.md")) if (case_dir / "graders").is_dir() else []:
            gmeta, _ = _read_meta(gpath)
            if "type" not in gmeta:
                raise SuiteError(f"{gpath}: grader has no `type`")
            graders[gpath.stem] = Grader(
                name=gpath.stem, type=gmeta["type"], weight=float(gmeta.get("weight", 1) or 1),
                tool=gmeta.get("tool", ""), arm=gmeta.get("arm", ""),
            )
        if not graders:
            raise SuiteError(f"{case_dir}: case has no graders")
        name = meta.get("name") or case_dir.name
        if name in cases:
            raise SuiteError(f"duplicate case name {name!r}")
        cases[name] = Case(name=name, rel_dir=rel.as_posix(), prompt=prompt, graders=graders)
    if not cases:
        raise SuiteError(f"{root}: no cases (directories with prompt.md or case.yaml)")
    files = _file_hashes(root)
    return Suite(root=root, cases=cases, digest=_digest(files), files=files)


def suite_digest(root: Path) -> str:
    """Digest of a suite directory, ignoring runner output under results/."""
    return _digest(_file_hashes(Path(root)))


def invocation_grader(skill_dir_name: str) -> str:
    return (
        "---\n"
        "type: tool_used\n"
        "tool: Skill\n"
        f"input_match: '\"skill\"\\s*:\\s*\"(?:[\\w-]+:)?{skill_dir_name}\"'\n"
        "arm: with-only\n"
        "---\n"
    )


@dataclass(frozen=True)
class PluginDir:
    root: Path
    suite_digest: str


def materialize_plugin(dest: Path, package: Package, suite: Suite, *, skill_dir_name: str) -> PluginDir:
    """Build `dest` as a plugin holding one skill and a frozen copy of the suite,
    plus the invocation-indicator grader in every case."""
    dest = Path(dest)
    if dest.exists():
        raise SuiteError(f"{dest} already exists")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", skill_dir_name):
        raise SuiteError(f"skill directory name {skill_dir_name!r} must use only letters, digits, '-' and '_'")
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": PLUGIN_NAME,
        "version": "0.0.0",
        "description": "Temporary plugin built by Claude Curator for one experiment condition.",
        "author": {"name": "claude-curator"},
    }, indent=2) + "\n")
    shutil.copytree(package.root, dest / "skills" / skill_dir_name, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", ".git"))
    shutil.copytree(suite.root, dest / "evals", ignore=lambda d, names: [n for n in names if (Path(d) == suite.root and n == "results") or n == ".DS_Store"])
    for case in suite.cases.values():
        gdir = dest / "evals" / case.rel_dir / "graders"
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / f"{INVOCATION_GRADER}.md").write_text(invocation_grader(skill_dir_name))
    return PluginDir(root=dest, suite_digest=suite_digest(dest / "evals"))
