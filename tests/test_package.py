import unittest

from claude_curator.package import (
    PackageError,
    build_candidate,
    load_package,
    remove_section,
    scan_package,
)
from tests.helpers import temp_dir, write_tree

SKILL = """
---
name: changelog-writer
description: Writes CHANGELOG entries in this repo's format.
---

# Changelog writer

Intro line.

## Format rules

Use Keep a Changelog headings.

### Nested detail

Still part of format rules.

## Worked examples

```markdown
## Not a heading inside a fence
```

## Script usage

Run `python3 scripts/check_changelog.py CHANGELOG.md` before finishing.
See [style guide](references/style.md#tone).
"""


class RemoveSectionTest(unittest.TestCase):
    def test_removes_heading_and_its_subsections_only(self):
        text = SKILL.lstrip("\n")
        out = remove_section(text, "Format rules")
        self.assertNotIn("Format rules", out)
        self.assertNotIn("Nested detail", out)
        self.assertIn("## Worked examples", out)
        self.assertIn("Intro line.\n\n## Worked examples", out)

    def test_fenced_heading_is_not_a_section_boundary(self):
        out = remove_section(SKILL.lstrip("\n"), "Worked examples")
        self.assertNotIn("Not a heading inside a fence", out)
        self.assertIn("## Script usage", out)

    def test_accepts_heading_with_hashes(self):
        out = remove_section(SKILL.lstrip("\n"), "## Script usage")
        self.assertNotIn("check_changelog", out)
        self.assertTrue(out.endswith("\n"))

    def test_missing_heading_is_an_error(self):
        with self.assertRaisesRegex(PackageError, "not found"):
            remove_section(SKILL, "Nope")

    def test_ambiguous_heading_is_an_error(self):
        text = "# T\n\n## A\n\nx\n\n## A\n\ny\n"
        with self.assertRaisesRegex(PackageError, "ambiguous"):
            remove_section(text, "A")

    def test_refuses_to_remove_the_whole_body(self):
        with self.assertRaisesRegex(PackageError, "entire"):
            remove_section(SKILL.lstrip("\n"), "Changelog writer")


class ScanPackageTest(unittest.TestCase):
    def make(self, extra=None, skill=SKILL):
        root = temp_dir(self) / "changelog-writer"
        files = {"SKILL.md": skill, "references/style.md": "tone\n", "scripts/check_changelog.py": "print(1)\n"}
        files.update(extra or {})
        return write_tree(root, files)

    def test_complete_package_has_no_errors(self):
        scan = scan_package(self.make())
        self.assertEqual([f for f in scan.findings if f.severity == "error"], [])
        self.assertEqual(sorted(scan.references), ["references/style.md", "scripts/check_changelog.py"])

    def test_missing_referenced_resource_is_an_error(self):
        root = self.make()
        (root / "scripts" / "check_changelog.py").unlink()
        scan = scan_package(root)
        errors = [f for f in scan.findings if f.severity == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind, "missing-resource")
        self.assertIn("scripts/check_changelog.py", errors[0].message)

    def test_path_outside_package_and_mcp_tools_are_flagged(self):
        skill = SKILL + "\nAlso read [shared](../shared/rules.md) and call mcp__tracker__create_issue.\n"
        scan = scan_package(self.make(skill=skill))
        kinds = {f.kind for f in scan.findings}
        self.assertIn("outside-package", kinds)
        self.assertIn("unsupported-mcp", kinds)

    def test_unreferenced_file_is_reported_as_orphan(self):
        scan = scan_package(self.make(extra={"assets/unused.txt": "x\n"}))
        self.assertEqual(scan.orphans, ["assets/unused.txt"])


class BuildCandidateTest(unittest.TestCase):
    def test_section_removal_copies_package_and_edits_skill_md(self):
        tmp = temp_dir(self)
        src = write_tree(tmp / "src", {"SKILL.md": SKILL, "references/style.md": "tone\n", "scripts/check_changelog.py": "x\n"})
        dest = tmp / "cand"
        build_candidate(src, dest, remove_section_heading="Worked examples")
        original, candidate = load_package(src), load_package(dest)
        self.assertEqual(candidate.name, "changelog-writer")
        self.assertNotEqual(original.digest, candidate.digest)
        self.assertEqual(set(original.files), set(candidate.files))
        self.assertNotIn("Worked examples", (dest / "SKILL.md").read_text())

    def test_package_without_skill_md_is_rejected(self):
        tmp = temp_dir(self)
        write_tree(tmp / "x", {"README.md": "hi\n"})
        with self.assertRaisesRegex(PackageError, "SKILL.md"):
            load_package(tmp / "x")


if __name__ == "__main__":
    unittest.main()
