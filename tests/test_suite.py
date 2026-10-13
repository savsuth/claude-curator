import json
import unittest

from claude_curator.package import load_package
from claude_curator.suite import INVOCATION_GRADER, SuiteError, load_suite, materialize_plugin
from tests.helpers import temp_dir, write_tree

CASE_PROMPT = """
---
max_turns: 8
allowed_tools: [Read, Write, Skill]
---

Add a changelog entry for the fix in src/parser.py.
"""


def make_suite(root):
    return write_tree(root, {
        "release-notes/prompt.md": CASE_PROMPT,
        "release-notes/graders/headings.md": "---\ntype: regex\npattern: '### Fixed'\ntarget: { source: file, path: CHANGELOG.md }\n---\n",
        "release-notes/graders/fired.md": "---\ntype: tool_used\ntool: Skill\n---\n",
        "group/bugfix/prompt.md": CASE_PROMPT,
        "group/bugfix/graders/judge.md": "---\ntype: llm\nweight: 2\n---\nPASS if the entry is under Fixed.\n",
        "results/2026-01-01/aggregate-result.json": "{}",
    })


class LoadSuiteTest(unittest.TestCase):
    def test_discovers_nested_cases_and_classifies_graders(self):
        suite = load_suite(make_suite(temp_dir(self) / "evals"))
        self.assertEqual(sorted(suite.cases), ["bugfix", "release-notes"])
        graders = suite.cases["release-notes"].graders
        self.assertTrue(graders["headings"].is_outcome)
        self.assertFalse(graders["fired"].is_outcome)  # Skill tool_used cannot pass without the skill
        self.assertEqual(suite.cases["bugfix"].graders["judge"].weight, 2.0)

    def test_results_directory_does_not_change_the_digest(self):
        root = make_suite(temp_dir(self) / "evals")
        before = load_suite(root).digest
        (root / "results" / "new.json").write_text("{}")
        self.assertEqual(load_suite(root).digest, before)

    def test_case_without_graders_is_rejected(self):
        root = write_tree(temp_dir(self) / "evals", {"lonely/prompt.md": "do it\n"})
        with self.assertRaisesRegex(SuiteError, "no graders"):
            load_suite(root)

    def test_case_pinning_plugins_is_rejected(self):
        root = write_tree(temp_dir(self) / "evals", {
            "c/prompt.md": "---\nplugins: [\"../../other\"]\n---\nhi\n",
            "c/graders/g.md": "---\ntype: regex\npattern: x\n---\n",
        })
        with self.assertRaisesRegex(SuiteError, "plugins"):
            load_suite(root)


class MaterializePluginTest(unittest.TestCase):
    def test_plugin_holds_package_suite_and_invocation_grader(self):
        tmp = temp_dir(self)
        suite = load_suite(make_suite(tmp / "evals"))
        pkg = load_package(write_tree(tmp / "changelog-writer", {"SKILL.md": "---\nname: changelog-writer\n---\n# X\n"}))
        a = materialize_plugin(tmp / "plugins" / "original", pkg, suite, skill_dir_name="changelog-writer")
        b = materialize_plugin(tmp / "plugins" / "candidate", pkg, suite, skill_dir_name="changelog-writer")

        manifest = json.loads((a.root / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "curator-under-test")
        self.assertTrue((a.root / "skills" / "changelog-writer" / "SKILL.md").is_file())
        self.assertFalse((a.root / "evals" / "results").exists())
        grader = (a.root / "evals" / "group" / "bugfix" / "graders" / f"{INVOCATION_GRADER}.md").read_text()
        self.assertIn("tool: Skill", grader)
        self.assertIn("changelog-writer", grader)
        self.assertEqual(a.suite_digest, b.suite_digest)


if __name__ == "__main__":
    unittest.main()
