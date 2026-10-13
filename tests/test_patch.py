import shutil
import subprocess
import unittest

from claude_curator.patch import make_patch
from tests.helpers import temp_dir, write_tree


class MakePatchTest(unittest.TestCase):
    def test_patch_applies_to_the_maintainer_repo_and_reverses(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        tmp = temp_dir(self)
        repo = write_tree(tmp / "repo", {
            "skills/demo/SKILL.md": "# Demo\n\n## Keep\n\nk\n\n## Drop\n\nd\n",
            "skills/demo/scripts/run.py": "print(1)\n",
        })
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        original = repo / "skills" / "demo"
        candidate = write_tree(tmp / "cand", {
            "SKILL.md": "# Demo\n\n## Keep\n\nk\n",
            "scripts/run.py": "print(1)\n",
            "references/new.md": "added\n",
        })

        patch = make_patch(original, candidate, repo_path="skills/demo")
        self.assertIn("--- a/skills/demo/SKILL.md", patch)
        self.assertIn("+++ b/skills/demo/references/new.md", patch)
        self.assertNotIn("scripts/run.py", patch)

        patch_file = tmp / "change.patch"
        patch_file.write_text(patch)
        subprocess.run(["git", "apply", "--check", str(patch_file)], cwd=repo, check=True)
        subprocess.run(["git", "apply", str(patch_file)], cwd=repo, check=True)
        self.assertEqual((original / "SKILL.md").read_text(), "# Demo\n\n## Keep\n\nk\n")
        subprocess.run(["git", "apply", "-R", "--check", str(patch_file)], cwd=repo, check=True)

    def test_identical_packages_give_an_empty_patch(self):
        tmp = temp_dir(self)
        a = write_tree(tmp / "a", {"SKILL.md": "x\n"})
        b = write_tree(tmp / "b", {"SKILL.md": "x\n"})
        self.assertEqual(make_patch(a, b, repo_path="skills/a"), "")


if __name__ == "__main__":
    unittest.main()
