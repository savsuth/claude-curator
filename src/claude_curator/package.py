"""Skill packages: loading, hashing, section removal and dependency scanning.

A skill package is a directory holding SKILL.md plus every file it references
(scripts, references, assets). The whole directory is the unit under test.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_NAMES = {".DS_Store", "__pycache__", ".git"}
# Files that are part of a package but are not expected to be referenced from markdown.
NEVER_ORPHANS = {"SKILL.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "README.md", "NOTICE"}
RESOURCE_DIRS = ("scripts", "references", "assets", "templates", "examples", "resources")


class PackageError(Exception):
    """The package or the requested change cannot be used as given."""


@dataclass(frozen=True)
class Package:
    root: Path
    name: str
    files: dict[str, str]  # relative path -> sha256 of contents
    digest: str  # sha256 over sorted (path, file hash) pairs
    frontmatter: dict[str, str]


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning" | "info"
    kind: str
    message: str
    location: str = ""


@dataclass
class Scan:
    package: Package
    references: set[str] = field(default_factory=set)
    findings: list[Finding] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_NAMES for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise PackageError(f"{path}: symlinks are not supported inside a skill package")
        if path.is_file():
            yield path


def parse_frontmatter(text: str) -> tuple[dict[str, str], int]:
    """Return top-level `key: value` pairs and the line index where the body starts.

    Only flat keys are read; nested YAML values are kept as their raw first line.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, i + 1
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("'\"")
    raise PackageError("SKILL.md frontmatter is not closed with '---'")


def load_package(root: Path) -> Package:
    root = Path(root)
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise PackageError(f"{root}: no SKILL.md, so this is not a skill package")
    files = {}
    for path in _iter_files(root):
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256("".join(f"{p}\0{h}\n" for p, h in sorted(files.items())).encode()).hexdigest()
    meta, _ = parse_frontmatter(skill_md.read_text())
    return Package(root=root, name=meta.get("name") or root.name, files=files, digest=digest, frontmatter=meta)


# --- section removal -------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _headings(lines: list[str], start: int) -> list[tuple[int, int, str]]:
    """(line index, level, text) for ATX headings outside fenced code blocks."""
    found, fence = [], None
    for i in range(start, len(lines)):
        m = _FENCE.match(lines[i])
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence:
            continue
        h = _HEADING.match(lines[i])
        if h:
            found.append((i, len(h.group(1)), h.group(2)))
    return found


def remove_section(text: str, heading: str) -> str:
    """Remove one markdown section: its heading line through the line before the
    next heading of the same or higher level. Subsections go with it."""
    wanted = heading.strip()
    m = _HEADING.match(wanted)
    level_wanted = len(m.group(1)) if m else None
    title = m.group(2) if m else wanted

    lines = text.splitlines(keepends=True)
    _, body_start = parse_frontmatter(text)
    heads = _headings([l.rstrip("\n") for l in lines], body_start)
    matches = [h for h in heads if h[2] == title and (level_wanted is None or h[1] == level_wanted)]
    if not matches:
        raise PackageError(f"section {heading!r} not found in SKILL.md")
    if len(matches) > 1:
        where = ", ".join(f"line {i + 1}" for i, _, _ in matches)
        raise PackageError(f"section {heading!r} is ambiguous ({where}); include the '#' level")
    idx, level, _ = matches[0]
    end = next((i for i, lvl, _ in heads if i > idx and lvl <= level), len(lines))

    before, after = lines[:idx], lines[end:]
    if not "".join(before[body_start:] + after).strip():
        raise PackageError(f"removing {heading!r} would delete the entire SKILL.md body")
    while before and not before[-1].strip():
        before.pop()
    if before and after:
        before.append("\n")
    out = "".join(before + after)
    return out if out.endswith("\n") else out + "\n"


# --- dependency scan ---------------------------------------------------------

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_RESOURCE = re.compile(
    r"(?<![\w/.-])(?:\$\{CLAUDE_SKILL_DIR\}/|\$\{CLAUDE_PLUGIN_ROOT\}/skills/[\w-]+/|\./)?"
    r"((?:" + "|".join(RESOURCE_DIRS) + r")/[\w./-]*[\w-])"
)
_PARENT = re.compile(r"(?<![\w.])\.\./[\w./-]+")
_HOST_PATH = re.compile(r"(?:~|\$HOME|/Users/[\w.-]+|/home/[\w.-]+)/\.claude/[\w./-]*")
_MCP = re.compile(r"\bmcp__[\w-]+__[\w-]+")
_SHELL_INJECTION = re.compile(r"!`[^`]+`")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_package(root: Path) -> Scan:
    package = load_package(root)
    scan = Scan(package=package)
    meta = package.frontmatter

    if meta.get("disable-model-invocation", "").lower() == "true":
        scan.findings.append(Finding("warning", "no-natural-invocation",
                                     "disable-model-invocation is true; only explicit /skill requests can load it", "SKILL.md"))
    if "hooks" in meta:
        scan.findings.append(Finding("warning", "skill-hooks",
                                     "skill-scoped hooks run outside the agent sandbox and are not measured separately", "SKILL.md"))
    if meta.get("model"):
        scan.findings.append(Finding("warning", "pinned-model",
                                     f"skill pins model {meta['model']!r}, which overrides the experiment model while it runs", "SKILL.md"))
    if meta.get("allowed-tools"):
        scan.findings.append(Finding("info", "allowed-tools",
                                     f"skill requests tools {meta['allowed-tools']!r}; eval runs only get tools granted by the operator", "SKILL.md"))

    for rel in package.files:
        if not rel.endswith(".md"):
            continue
        text = (package.root / rel).read_text(errors="replace")
        base = Path(rel).parent
        candidates: list[tuple[str, int]] = []
        for m in _LINK.finditer(text):
            target = m.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            candidates.append((target.split("#")[0].split("?")[0], m.start()))
        for m in _RESOURCE.finditer(text):
            candidates.append((m.group(1), m.start()))

        for target, pos in candidates:
            where = f"{rel}:{_line_of(text, pos)}"
            if target.startswith("/") or target.startswith("~"):
                scan.findings.append(Finding("error", "outside-package", f"absolute path {target} is outside the package", where))
                continue
            resolved = (base / target).as_posix() if not target.startswith(RESOURCE_DIRS) else target
            normalized = Path(resolved)
            parts: list[str] = []
            for part in normalized.parts:
                if part == "..":
                    if not parts:
                        parts = ["..", *parts]
                        break
                    parts.pop()
                elif part != ".":
                    parts.append(part)
            if parts and parts[0] == "..":
                scan.findings.append(Finding("error", "outside-package", f"{target} points outside the package", where))
                continue
            norm = "/".join(parts)
            if not norm:
                continue
            if norm in package.files or any(f.startswith(norm.rstrip("/") + "/") for f in package.files):
                scan.references.add(norm)
            else:
                scan.findings.append(Finding("error", "missing-resource", f"{norm} is referenced but not in the package", where))

        for m in _PARENT.finditer(text):
            if not any(f.location == f"{rel}:{_line_of(text, m.start())}" and f.kind == "outside-package" for f in scan.findings):
                scan.findings.append(Finding("error", "outside-package", f"{m.group(0)} points outside the package", f"{rel}:{_line_of(text, m.start())}"))
        for m in _HOST_PATH.finditer(text):
            scan.findings.append(Finding("error", "outside-package", f"{m.group(0)} depends on host configuration", f"{rel}:{_line_of(text, m.start())}"))
        for m in _MCP.finditer(text):
            scan.findings.append(Finding("error", "unsupported-mcp",
                                         f"{m.group(0)} needs an MCP server that is not part of the package", f"{rel}:{_line_of(text, m.start())}"))
        for m in _SHELL_INJECTION.finditer(text):
            scan.findings.append(Finding("warning", "shell-injection",
                                         f"{m.group(0)} runs a shell command when the skill loads", f"{rel}:{_line_of(text, m.start())}"))

    scan.orphans = sorted(
        f for f in package.files
        if f not in scan.references and Path(f).name not in NEVER_ORPHANS
        and not any(f.startswith(r.rstrip("/") + "/") for r in scan.references)
    )
    return scan


def build_candidate(original: Path, dest: Path, *, remove_section_heading: str | None = None,
                    candidate_dir: Path | None = None) -> Package:
    """Write the candidate package to `dest` from exactly one change source."""
    if (remove_section_heading is None) == (candidate_dir is None):
        raise PackageError("give exactly one of a section to remove or a candidate directory")
    if dest.exists():
        raise PackageError(f"{dest} already exists")
    source = Path(candidate_dir) if candidate_dir else Path(original)
    load_package(source)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*IGNORED_NAMES), symlinks=True)
    if remove_section_heading is not None:
        skill_md = dest / "SKILL.md"
        skill_md.write_text(remove_section(skill_md.read_text(), remove_section_heading))
    return load_package(dest)
