"""Small builders shared by the tests. Nothing here is used by the product."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"))
    return root


def temp_dir(test) -> Path:
    tmp = tempfile.TemporaryDirectory(prefix="curator-test-")
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)
