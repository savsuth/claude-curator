import shutil
import subprocess
import unittest

from claude_curator.spec import SpecError, load_spec, resolve_against_suite
from claude_curator.suite import load_suite
from tests.helpers import temp_dir, write_tree

BASE = """
[skill]
path = "skills/demo"

[change]
remove_section = "Examples"
rationale = "Examples restate the rules."

[suite]
path = "evals"
development = ["dev"]

[[requirements]]
id = "headings"
description = "Entries use the team headings"
graders = ["a/headings"]
package_contains = [{ file = "SKILL.md", text = "scripts/check.py" }]
"""

SUITE = {
    "evals/a/prompt.md": "do a\n",
    "evals/a/graders/headings.md": "---\ntype: regex\npattern: x\n---\n",
    "evals/a/graders/fired.md": "---\ntype: tool_used\ntool: Skill\n---\n",
    "evals/b/prompt.md": "do b\n",
    "evals/b/graders/g.md": "---\ntype: regex\npattern: y\n---\n",
    "evals/dev/prompt.md": "do dev\n",
    "evals/dev/graders/g.md": "---\ntype: regex\npattern: z\n---\n",
}


class SpecTest(unittest.TestCase):
    def setup(self, toml=BASE):
        root = write_tree(temp_dir(self), {"curator.toml": toml, **SUITE})
        return load_spec(root / "curator.toml"), load_suite(root / "evals")

    def test_heldout_defaults_to_every_non_development_case(self):
        spec, suite = self.setup()
        self.assertEqual(resolve_against_suite(spec, suite), ("a", "b"))
        self.assertTrue(spec.requirements[0].critical)
        self.assertEqual(spec.repo_path, "skills/demo")

    def test_repo_path_defaults_to_the_path_from_the_git_root(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        root = write_tree(temp_dir(self), {"configs/curator.toml": BASE.replace('"skills/demo"', '"../skills/demo"').replace('"evals"', '"../evals"'),
                                           "skills/demo/SKILL.md": "# Demo\n", **SUITE})
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        self.assertEqual(load_spec(root / "configs" / "curator.toml").repo_path, "skills/demo")

    def test_heldout_case_without_outcome_graders_is_rejected(self):
        files = {"curator.toml": BASE, **SUITE, "evals/c/prompt.md": "do c\n", "evals/c/graders/fired.md": "---\ntype: tool_used\ntool: Skill\n---\n"}
        root = write_tree(temp_dir(self), files)
        with self.assertRaisesRegex(SpecError, "no outcome grader"):
            resolve_against_suite(load_spec(root / "curator.toml"), load_suite(root / "evals"))

    def test_requirement_on_invocation_grader_is_rejected(self):
        spec, suite = self.setup(BASE.replace("a/headings", "a/fired"))
        with self.assertRaisesRegex(SpecError, "only measures skill invocation"):
            resolve_against_suite(spec, suite)

    def test_requirement_on_development_case_is_rejected(self):
        spec, suite = self.setup(BASE.replace("a/headings", "dev/g"))
        with self.assertRaisesRegex(SpecError, "not held-out"):
            resolve_against_suite(spec, suite)

    def test_change_needs_a_rationale_and_one_source(self):
        with self.assertRaisesRegex(SpecError, "rationale"):
            self.setup(BASE.replace('rationale = "Examples restate the rules."', ""))
        with self.assertRaisesRegex(SpecError, "exactly one"):
            self.setup(BASE.replace('remove_section = "Examples"', 'remove_section = "Examples"\ncandidate = "c"'))

    def test_unknown_keys_are_errors_not_silently_ignored(self):
        with self.assertRaisesRegex(SpecError, "tolerence"):
            self.setup(BASE + "\n[decision]\ntolerence = 0.1\n")

    def test_runs_below_minimum_is_rejected(self):
        with self.assertRaisesRegex(SpecError, "min_runs_per_case"):
            self.setup(BASE + "\n[run]\nruns = 1\n")


if __name__ == "__main__":
    unittest.main()
