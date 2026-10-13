#!/usr/bin/env python3
"""Validate CHANGELOG.md against the team format. Prints OK or one problem per line."""

import re
import sys

ORDER = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]


def check(text: str) -> list[str]:
    problems = []
    if "## [Unreleased]" not in text:
        return ["missing '## [Unreleased]' section"]
    unreleased = text.split("## [Unreleased]", 1)[1]
    unreleased = re.split(r"^## \[", unreleased, flags=re.M)[0]
    headings = re.findall(r"^### (.+)$", unreleased, flags=re.M)
    for h in headings:
        if h not in ORDER:
            problems.append(f"unknown heading '### {h}'")
    known = [h for h in headings if h in ORDER]
    if known != sorted(known, key=ORDER.index):
        problems.append("headings are out of order")
    for line in unreleased.splitlines():
        if line.startswith("- ") and not re.search(r"\(#\d+\)\s*$", line):
            problems.append(f"entry without a pull request reference: {line}")
    return problems


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "CHANGELOG.md"
    with open(path) as fh:
        problems = check(fh.read())
    print("\n".join(problems) if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
