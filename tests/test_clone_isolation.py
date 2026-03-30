"""Tests for clone creation, independence, and cleanup.

Verifies that researcher and target clones are fully isolated git repos
with independent commit histories, and that cleanup removes all artifacts.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from supervisor_harness.supervisor import (
    _create_variant_clone,
    _create_target_clone,
    _remove_variant_clones,
    _symlink_pixi,
)


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )


def _git_head(path: Path) -> str:
    """Return HEAD sha for the repo at path."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _commit_file(repo: Path, filename: str, content: str, message: str) -> str:
    """Write a file, commit it, and return the new HEAD sha."""
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo, check=True, capture_output=True,
    )
    return _git_head(repo)


class TestCreateVariantClone(unittest.TestCase):
    """_create_variant_clone creates an isolated researcher clone."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.supervised = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised)
        self.variant_id = "test-clone-iso"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_clone_has_separate_git_dir(self) -> None:
        """_create_variant_clone creates a clone with its own .git directory."""
        clone = _create_variant_clone(self.supervised, self.variant_id, clone_base=self.tmpdir)
        self.assertTrue(clone.exists(), "Clone directory should exist")
        self.assertTrue((clone / ".git").exists(), "Clone should have its own .git")
        supervised_git = (self.supervised / ".git").resolve()
        clone_git = (clone / ".git").resolve()
        self.assertNotEqual(supervised_git, clone_git)


class TestCreateTargetClone(unittest.TestCase):
    """_create_target_clone creates an isolated target clone with .pixi symlink."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.target = self.tmpdir / "sar-rag-target"
        _init_repo(self.target)
        (self.target / ".pixi").mkdir()
        (self.target / ".pixi" / "marker").write_text("pixi-env")
        self.variant_id = "test-target-iso"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_target_clone_has_pixi_symlink(self) -> None:
        """_create_target_clone creates a clone with .pixi symlinked from source."""
        clone = _create_target_clone(self.target, self.variant_id, clone_base=self.tmpdir)
        self.assertTrue(clone.exists(), "Target clone should exist")
        pixi_link = clone / ".pixi"
        self.assertTrue(pixi_link.is_symlink(), ".pixi should be a symlink")
        self.assertEqual(
            pixi_link.resolve(),
            (self.target / ".pixi").resolve(),
        )


class TestConcurrentCloneCommits(unittest.TestCase):
    """Two clones of the same repo can commit independently."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.supervised = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised)
        self.id_a = "test-concurrent-a"
        self.id_b = "test-concurrent-b"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_independent_commits(self) -> None:
        """Commits in clone A do not appear in clone B."""
        clone_a = _create_variant_clone(self.supervised, self.id_a, clone_base=self.tmpdir)
        clone_b = _create_variant_clone(self.supervised, self.id_b, clone_base=self.tmpdir)

        for clone in (clone_a, clone_b):
            subprocess.run(
                ["git", "config", "user.email", "t@t"],
                cwd=clone, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"],
                cwd=clone, check=True, capture_output=True,
            )

        head_a = _commit_file(clone_a, "a.txt", "from-a", "commit in A")
        head_b = _git_head(clone_b)
        self.assertNotEqual(head_a, head_b, "Clone B should not see A's commit")

        head_b_new = _commit_file(clone_b, "b.txt", "from-b", "commit in B")
        self.assertNotEqual(head_b_new, head_a, "Clone heads should diverge")


class TestRemoveVariantClones(unittest.TestCase):
    """_remove_variant_clones removes all artifacts for a variant."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.supervised = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised)
        self.target = self.tmpdir / "sar-rag-target"
        _init_repo(self.target)
        self.variant_id = "test-remove-all"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_removes_all_artifacts(self) -> None:
        """Researcher clone, target clone, tv-* clones, and reports are removed."""
        researcher_clone = self.tmpdir / f"sar-research-loop--{self.variant_id}"
        researcher_clone.mkdir(parents=True, exist_ok=True)

        target_clone = self.tmpdir / f"sar-rag-target--{self.variant_id}"
        target_clone.mkdir(parents=True, exist_ok=True)

        tv_clone = self.tmpdir / f"sar-rag-target--{self.variant_id}-tv-1"
        tv_clone.mkdir(parents=True, exist_ok=True)

        report_file = self.tmpdir / f"rag-eval-report--{self.variant_id}.json"
        report_file.write_text("{}")

        _remove_variant_clones(self.supervised, self.target, self.variant_id, clone_base=self.tmpdir)

        self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed")
        self.assertFalse(target_clone.exists(), "Target clone should be removed")
        self.assertFalse(tv_clone.exists(), "tv-* clone should be removed")
        self.assertFalse(report_file.exists(), "Report file should be removed")


class TestCloneFromCanonicalIsIndependent(unittest.TestCase):
    """A clone created from a canonical repo is fully independent."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.canonical = self.tmpdir / "sar-research-loop"
        _init_repo(self.canonical)
        self.variant_id = "test-independence"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_canonical_commits_dont_appear_in_clone(self) -> None:
        """Commits in canonical after cloning do not appear in the clone."""
        clone = _create_variant_clone(self.canonical, self.variant_id, clone_base=self.tmpdir)

        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=clone, check=True, capture_output=True,
        )

        head_before = _git_head(clone)
        _commit_file(self.canonical, "new.txt", "new-content", "canonical update")
        head_after = _git_head(clone)

        self.assertEqual(head_before, head_after, "Clone should not see canonical's new commit")
