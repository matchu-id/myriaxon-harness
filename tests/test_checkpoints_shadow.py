"""Shadow-history durability: a damaged repo must be repaired, not silently reused.

The failure this guards against was live on the development machine. The project sits in a synced
folder, the sync client removed every hidden file inside `.myriaxon/history/*/.git/` while leaving
`objects/` intact, and `_ensure_shadow_repo` returned that directory as usable because all it
checked was `git_dir.is_dir()`. Every checkpoint after that ran `git add` and `git commit` against a
directory git rejects -- so the checkpoint looked recorded, got no shadow commit, and `/rewind`
quietly lost the history it was supposed to keep.

"It returned without raising" is not the same as "it worked", and this is the test that tells the two
apart.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import checkpoints  # noqa: E402


@unittest.skipUnless(checkpoints.shadow_git_available(), "git is not available")
class TestShadowRepoRepair(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mx-shadow-test-")
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        (self.project / "file.txt").write_text("content\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_fresh_project_gets_a_usable_repo(self) -> None:
        git_dir = checkpoints._ensure_shadow_repo(self.project)
        self.assertIsNotNone(git_dir)
        self.assertTrue(checkpoints._shadow_repo_is_usable(git_dir))
        self.assertTrue((git_dir / "config").is_file())
        self.assertTrue((git_dir / "HEAD").is_file())

    def test_a_bare_git_directory_is_not_mistaken_for_a_repository(self) -> None:
        """The whole bug in one assertion: a directory is not a repository."""
        bogus = Path(self._tmp.name) / "bogus" / ".git"
        bogus.mkdir(parents=True)
        self.assertFalse(checkpoints._shadow_repo_is_usable(bogus))

    def test_a_repo_losing_config_is_repaired_and_commits_again(self) -> None:
        git_dir = checkpoints._ensure_shadow_repo(self.project)
        self.assertTrue(checkpoints._shadow_commit(self.project, "before damage"))

        (git_dir / "config").unlink()
        self.assertFalse(
            checkpoints._shadow_repo_is_usable(git_dir),
            "a repo with no config is not usable, whatever the directory listing suggests",
        )

        repaired = checkpoints._ensure_shadow_repo(self.project)
        self.assertIsNotNone(repaired, "the repair must return a usable repo, not None")
        self.assertTrue(checkpoints._shadow_repo_is_usable(repaired))
        self.assertIsNotNone(
            checkpoints._shadow_commit(self.project, "after repair"),
            "committing must work again once the repo is repaired",
        )

    def test_a_repo_losing_only_head_is_repaired(self) -> None:
        git_dir = checkpoints._ensure_shadow_repo(self.project)
        (git_dir / "HEAD").unlink()
        self.assertFalse(checkpoints._shadow_repo_is_usable(git_dir))
        repaired = checkpoints._ensure_shadow_repo(self.project)
        self.assertIsNotNone(repaired)
        self.assertTrue(checkpoints._shadow_repo_is_usable(repaired))

    def test_the_repaired_repo_is_readable_by_git_itself(self) -> None:
        """Asserted with git, not with our own predicate: the predicate agreeing with itself
        proves nothing, and the original defect was precisely a wrong idea of 'usable'."""
        git_dir = checkpoints._ensure_shadow_repo(self.project)
        (git_dir / "config").unlink()
        repaired = checkpoints._ensure_shadow_repo(self.project)
        checkpoints._shadow_commit(self.project, "readable")

        result = subprocess.run(["git", f"--git-dir={repaired}", "log", "--oneline"],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("readable", result.stdout)

    def test_a_damaged_repo_does_not_return_a_path_that_will_fail(self) -> None:
        """If init cannot produce a usable repo, the answer is None -- not a path to a broken one.

        Returning the path is what made the original bug silent: callers checked for None, got a
        Path, and assumed success.
        """
        git_dir = checkpoints._ensure_shadow_repo(self.project)
        self.assertTrue(checkpoints._shadow_repo_is_usable(git_dir))
        # Remove the whole .git and leave an object store that init cannot complete by re-creating
        # only part of it -- the honest simulation is a repo git rejects.
        (git_dir / "config").unlink()
        (git_dir / "HEAD").unlink()
        self.assertFalse(checkpoints._shadow_repo_is_usable(git_dir))
        # The repair succeeds here, so this asserts the contract rather than a failure path: either
        # None, or a repo git accepts. Never a third thing.
        result = checkpoints._ensure_shadow_repo(self.project)
        self.assertTrue(
            result is None or checkpoints._shadow_repo_is_usable(result),
            "the contract is None or usable, never a broken path",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
