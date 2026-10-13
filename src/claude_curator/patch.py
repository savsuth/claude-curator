"""Git-applyable unified diff between the original and candidate packages."""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from claude_curator.package import load_package


class PatchError(Exception):
    pass


def _lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    data = path.read_bytes()
    if b"\0" in data:
        raise PatchError(f"{path}: binary files are not supported in v0.1 patches")
    return data.decode("utf-8").splitlines(keepends=True)


def _mode(path: Path) -> str:
    return "100755" if os.access(path, os.X_OK) else "100644"


def make_patch(original: Path, candidate: Path, *, repo_path: str) -> str:
    """Diff two package directories. Paths in the patch are `a/<repo_path>/<file>`
    so `git apply` works from the root of the repository the skill lives in."""
    original, candidate = Path(original), Path(candidate)
    a_files, b_files = load_package(original).files, load_package(candidate).files
    prefix = repo_path.strip("/")
    chunks: list[str] = []
    for rel in sorted(set(a_files) | set(b_files)):
        if a_files.get(rel) == b_files.get(rel):
            continue
        a_path = original / rel if rel in a_files else None
        b_path = candidate / rel if rel in b_files else None
        name = f"{prefix}/{rel}" if prefix else rel
        header = [f"diff --git a/{name} b/{name}\n"]
        if a_path is None:
            header.append(f"new file mode {_mode(b_path)}\n")
        elif b_path is None:
            header.append(f"deleted file mode {_mode(a_path)}\n")
        diff = difflib.unified_diff(
            _lines(a_path), _lines(b_path),
            fromfile=f"a/{name}" if a_path else "/dev/null",
            tofile=f"b/{name}" if b_path else "/dev/null",
        )
        body = []
        for line in diff:
            body.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        chunks.append("".join(header + body))
    return "".join(chunks)
