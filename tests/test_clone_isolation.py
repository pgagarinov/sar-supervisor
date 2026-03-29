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
        # Ensure no leftover clone
        clone_path = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)

    def tearDown(self) -> None:
        clone_path = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)
        self._tmpdir.cleanup()

    def test_clone_has_separate_git_dir(self) -> None:
        """_create_variant_clone creates a clone with its own .git directory."""
        clone = _create_variant_clone(self.supervised, self.variant_id)
        self.assertTrue(clone.exists(), "Clone directory should exist")
        self.assertTrue((clone / ".git").exists(), "Clone should have its own .git")
        # The .git dirs should be different paths (not shared)
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
        # Create a fake .pixi directory so the symlink has something to point at
        (self.target / ".pixi").mkdir()
        (self.target / ".pixi" / "marker").write_text("pixi-env")
        self.variant_id = "test-target-iso"
        # Clean up any leftover
        clone_path = Path(f"{self.target}--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)

    def tearDown(self) -> None:
        clone_path = Path(f"{self.target}--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)
        self._tmpdir.cleanup()

    def test_target_clone_has_pixi_symlink(self) -> None:
        """_create_target_clone creates a clone with .pixi symlinked from source."""
        clone = _create_target_clone(self.target, self.variant_id)
        self.assertTrue(clone.exists(), "Target clone should exist")
        pixi_link = clone / ".pixi"
        self.assertTrue(pixi_link.is_symlink(), ".pixi should be a symlink")
        # Symlink should point to the source .pixi (resolved)
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
        for vid in (self.id_a, self.id_b):
            p = Path(f"/tmp/sar-research-loop--{vid}")
            if p.exists():
                import shutil
                shutil.rmtree(p)

    def tearDown(self) -> None:
        for vid in (self.id_a, self.id_b):
            p = Path(f"/tmp/sar-research-loop--{vid}")
            if p.exists():
                import shutil
                shutil.rmtree(p)
        self._tmpdir.cleanup()

    def test_independent_commits(self) -> None:
        """Commits in clone A do not appear in clone B."""
        clone_a = _create_variant_clone(self.supervised, self.id_a)
        clone_b = _create_variant_clone(self.supervised, self.id_b)

        # Configure git user in clones
        for clone in (clone_a, clone_b):
            subprocess.run(
                ["git", "config", "user.email", "t@t"],
                cwd=clone, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"],
                cwd=clone, check=True, capture_output=True,
            )

        # Commit in clone A
        head_a = _commit_file(clone_a, "a.txt", "from-a", "commit in A")

        # Clone B should still be at original HEAD, not A's new commit
        head_b = _git_head(clone_b)
        self.assertNotEqual(head_a, head_b, "Clone B should not see A's commit")

        # Commit in clone B
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
        # Safety cleanup in case the test fails
        import shutil
        for p in [
            Path(f"/tmp/sar-research-loop--{self.variant_id}"),
            Path(f"{self.target}--{self.variant_id}"),
        ]:
            if p.exists():
                shutil.rmtree(p)
        self._tmpdir.cleanup()

    def test_removes_all_artifacts(self) -> None:
        """Researcher clone, target clone, tv-* clones, chroma dirs, and reports are removed."""
        # Create artifacts that _remove_variant_clones should clean
        researcher_clone = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        researcher_clone.mkdir(parents=True, exist_ok=True)

        target_clone = Path(f"{self.target}--{self.variant_id}")
        target_clone.mkdir(parents=True, exist_ok=True)

        tv_clone = Path(f"{self.target}--{self.variant_id}-tv-1")
        tv_clone.mkdir(parents=True, exist_ok=True)

        chroma_dir = Path(f"/tmp/fluxapi-chroma--{self.variant_id}")
        chroma_dir.mkdir(parents=True, exist_ok=True)

        report_file = Path(f"/tmp/rag-eval-report--{self.variant_id}.json")
        report_file.write_text("{}")

        # Run cleanup
        _remove_variant_clones(self.supervised, self.target, self.variant_id)

        # Verify all artifacts are gone
        self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed")
        self.assertFalse(target_clone.exists(), "Target clone should be removed")
        self.assertFalse(tv_clone.exists(), "tv-* clone should be removed")
        self.assertFalse(chroma_dir.exists(), "Chroma dir should be removed")
        self.assertFalse(report_file.exists(), "Report file should be removed")


class TestCloneFromCanonicalIsIndependent(unittest.TestCase):
    """A clone created from a canonical repo is fully independent."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)
        self.canonical = self.tmpdir / "sar-research-loop"
        _init_repo(self.canonical)
        self.variant_id = "test-independence"
        clone_path = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)

    def tearDown(self) -> None:
        clone_path = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        if clone_path.exists():
            import shutil
            shutil.rmtree(clone_path)
        self._tmpdir.cleanup()

    def test_canonical_commits_dont_appear_in_clone(self) -> None:
        """Commits in canonical after cloning do not appear in the clone."""
        clone = _create_variant_clone(self.canonical, self.variant_id)
        clone_head_before = _git_head(clone)

        # Make a new commit in canonical
        _commit_file(self.canonical, "new.txt", "new-content", "post-clone commit")
        canonical_head = _git_head(self.canonical)

        # Clone should still be at its original HEAD
        clone_head_after = _git_head(clone)
        self.assertEqual(clone_head_before, clone_head_after,
                         "Clone HEAD should not change when canonical gets new commits")
        self.assertNotEqual(canonical_head, clone_head_after,
                            "Clone and canonical should diverge after canonical commit")


if __name__ == "__main__":
    unittest.main()
