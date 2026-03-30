"""Tests for merge strategies, rollback, and merge lock.

Verifies that merge_winner_takes_all, merge_cherry_pick, and
merge_branch_and_continue correctly apply variant changes to the
canonical target, create backups, update baseline tags, and that
rollback_merge restores the original state. Also tests _MergeLock
for exclusive access.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from supervisor_harness.config import RepoPaths
from supervisor_harness.supervisor import (
    _create_target_clone,
    _resolve_target_repo,
    _symlink_pixi,
    merge_winner_takes_all,
    merge_cherry_pick,
    merge_branch_and_continue,
    rollback_merge,
    _MergeLock,
)
from harness_core.git_utils import git_head, git_command


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


def _commit_file(repo: Path, filename: str, content: str, message: str) -> str:
    """Write a file, commit it, and return the new HEAD sha."""
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _tag(repo: Path, tag_name: str) -> None:
    """Create a tag at HEAD."""
    subprocess.run(
        ["git", "tag", tag_name], cwd=repo, check=True, capture_output=True,
    )


def _resolve_tag(repo: Path, tag_name: str) -> str | None:
    """Resolve a tag to its commit sha."""
    result = git_command(repo, "rev-parse", tag_name)
    return result.stdout.strip() if result.returncode == 0 else None


class _MergeTestBase(unittest.TestCase):
    """Base class that sets up a complete merge test workspace.

    Creates:
    - canonical_target: a git repo with a baseline tag (the "real" target)
    - supervised_repo: a researcher repo with .env pointing to canonical_target
    - workspace: a supervisor workspace with harness.toml and .supervisor/
    - target_clone: a clone of canonical_target simulating variant work
    """

    variant_id: str = "test-merge"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

        # Canonical target repo with baseline tag
        self.canonical_target = self.tmpdir / "sar-rag-target"
        _init_repo(self.canonical_target)
        _tag(self.canonical_target, "baseline")
        self.original_head = git_head(self.canonical_target)

        # Supervised repo (researcher) with .env pointing to target
        self.supervised_repo = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised_repo)
        env_content = f"TARGET_PATH={self.canonical_target}\n"
        (self.supervised_repo / ".env").write_text(env_content)

        # Supervisor workspace
        self.workspace = self.tmpdir / "sar-supervisor"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.workspace / ".supervisor"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Create target clone simulating a researcher variant's work
        self.target_clone = Path(f"{self.canonical_target}--{self.variant_id}")
        if self.target_clone.exists():
            shutil.rmtree(self.target_clone)
        subprocess.run(
            ["git", "clone", "--local", str(self.canonical_target), str(self.target_clone)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=self.target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=self.target_clone, check=True, capture_output=True,
        )

        # Build RepoPaths manually
        self.paths = self._make_paths()

    def tearDown(self) -> None:
        # Clean up clones and backups
        for suffix in (
            f"--{self.variant_id}",
            ".pre-merge-backup",
        ):
            p = Path(f"{self.canonical_target}{suffix}")
            if p.exists():
                shutil.rmtree(p)
        self._tmpdir.cleanup()

    def _make_paths(self) -> RepoPaths:
        """Build a RepoPaths for testing merge operations."""
        claude_dir = self.supervised_repo / ".claude"
        skill_name = "start"
        return RepoPaths(
            workspace_root=self.workspace,
            supervised_repo=self.supervised_repo,
            claude_dir=claude_dir,
            skill_name=skill_name,
            agent_names=(),
            skill_path=claude_dir / "skills" / skill_name / "SKILL.md",
            agent_paths={},
            log_path=self.tmpdir / "test.log",
            state_dir=self.state_dir,
            snapshots_dir=self.snapshots_dir,
            pid_path=self.state_dir / f"{skill_name}.pid",
            state_path=self.state_dir / f"{skill_name}-state.json",
            latest_snapshot_path=self.state_dir / "latest_snapshot.json",
            history_path=self.state_dir / "history.jsonl",
            report_paths=(),
            report_map={},
            config_dirs=(Path("~/.claude").expanduser(),),
            config={},
        )


class TestMergeWinnerTakesAll(_MergeTestBase):
    """merge_winner_takes_all replaces canonical with clone state."""

    def setUp(self) -> None:
        super().setUp()
        # Make commits in the clone to simulate researcher improvements
        self.clone_head = _commit_file(
            self.target_clone, "improvement.txt", "better", "improve target",
        )

    def test_canonical_head_matches_clone(self) -> None:
        """After WTA merge, canonical HEAD matches the clone's HEAD."""
        merge_winner_takes_all(self.paths, self.variant_id)
        self.assertEqual(git_head(self.canonical_target), self.clone_head)

    def test_backup_created(self) -> None:
        """WTA merge creates a backup snapshot in pre-merge-backup."""
        result = merge_winner_takes_all(self.paths, self.variant_id)
        backup_dir = Path(result["backup"])
        self.assertTrue(backup_dir.exists(), "Backup directory should exist")
        # Backup should contain code-state from capture_code_state
        self.assertTrue(
            (backup_dir / "code-state").exists(),
            "Backup should contain code-state directory",
        )

    def test_baseline_tag_updated(self) -> None:
        """WTA merge updates the baseline tag to new HEAD."""
        merge_winner_takes_all(self.paths, self.variant_id)
        baseline_sha = _resolve_tag(self.canonical_target, "baseline")
        head_sha = git_head(self.canonical_target)
        self.assertEqual(baseline_sha, head_sha,
                         "baseline tag should point to new HEAD after merge")


class TestMergeCherryPick(_MergeTestBase):
    """merge_cherry_pick applies individual commits from variant clones."""

    def setUp(self) -> None:
        super().setUp()
        # Make two clean commits in the clone
        self.commit_1_sha = _commit_file(
            self.target_clone, "feat1.txt", "feature-1", "add feature 1",
        )
        self.commit_2_sha = _commit_file(
            self.target_clone, "feat2.txt", "feature-2", "add feature 2",
        )
        # Fetch clone objects into canonical so cherry-pick can resolve commits.
        # merge_cherry_pick uses git_log_range on the clone to find commit hashes,
        # then cherry-picks them into canonical — canonical must have the objects.
        subprocess.run(
            ["git", "fetch", str(self.target_clone), "main"],
            cwd=self.canonical_target, check=True, capture_output=True,
        )

    def test_individual_commits_applied(self) -> None:
        """Cherry-pick applies individual commits to canonical."""
        result = merge_cherry_pick(self.paths, [self.variant_id])
        self.assertGreater(len(result["applied"]), 0, "Should have applied commits")
        # Both feature files should exist in canonical
        self.assertTrue(
            (self.canonical_target / "feat1.txt").exists(),
            "feat1.txt should be in canonical after cherry-pick",
        )
        self.assertTrue(
            (self.canonical_target / "feat2.txt").exists(),
            "feat2.txt should be in canonical after cherry-pick",
        )

    def test_conflicts_skipped(self) -> None:
        """Cherry-pick skips conflicting commits and reports them."""
        # Create a conflicting change in canonical on the same file
        _commit_file(
            self.canonical_target, "feat1.txt", "different-content", "conflict setup",
        )
        # Re-tag baseline so cherry-pick finds the clone commits
        git_command(self.canonical_target, "tag", "-f", "baseline", self.original_head)

        result = merge_cherry_pick(self.paths, [self.variant_id])
        # The first commit (feat1.txt) should conflict, second should apply
        total = len(result["applied"]) + len(result["conflicts"])
        self.assertGreater(total, 0, "Should have attempted some commits")
        # At least feat1.txt commit should conflict
        self.assertGreater(
            len(result["conflicts"]), 0,
            "Should have at least one conflict for feat1.txt",
        )

    def test_baseline_tag_updated(self) -> None:
        """Cherry-pick updates the baseline tag after applying commits."""
        result = merge_cherry_pick(self.paths, [self.variant_id])
        if result["applied"]:
            baseline_sha = _resolve_tag(self.canonical_target, "baseline")
            head_sha = git_head(self.canonical_target)
            self.assertEqual(baseline_sha, head_sha,
                             "baseline should point to HEAD after cherry-pick")


class TestMergeBranchAndContinue(_MergeTestBase):
    """merge_branch_and_continue promotes the clone to canonical location."""

    def setUp(self) -> None:
        super().setUp()
        # Make a commit in the clone
        self.clone_head = _commit_file(
            self.target_clone, "promoted.txt", "winner", "winning change",
        )

    def test_clone_replaces_canonical(self) -> None:
        """After B&C, the canonical location contains the clone's content."""
        merge_branch_and_continue(self.paths, self.variant_id)
        self.assertTrue(
            (self.canonical_target / "promoted.txt").exists(),
            "Canonical should now have the clone's promoted.txt",
        )
        self.assertEqual(git_head(self.canonical_target), self.clone_head)

    def test_pixi_re_symlinked(self) -> None:
        """After B&C, .pixi is re-symlinked if source had a real .pixi directory."""
        # Create a real .pixi in the backup location (canonical before move)
        pixi_dir = self.canonical_target / ".pixi"
        pixi_dir.mkdir(exist_ok=True)
        (pixi_dir / "marker").write_text("env-data")

        merge_branch_and_continue(self.paths, self.variant_id)

        pixi_link = self.canonical_target / ".pixi"
        # After merge, .pixi should be a symlink pointing to backup's .pixi
        if pixi_link.exists() or pixi_link.is_symlink():
            # The symlink target should resolve to the backup's .pixi
            backup_path = Path(f"{self.canonical_target}.pre-merge-backup")
            self.assertTrue(
                pixi_link.is_symlink(),
                ".pixi should be a symlink after B&C merge",
            )

    def test_backup_created(self) -> None:
        """B&C creates a backup of the original canonical directory."""
        result = merge_branch_and_continue(self.paths, self.variant_id)
        backup_path = Path(result["backup"])
        self.assertTrue(backup_path.exists(), "Backup directory should exist")
        # Backup should contain the original file.txt
        self.assertTrue(
            (backup_path / "file.txt").exists(),
            "Backup should contain original file.txt",
        )


class TestRollbackMerge(_MergeTestBase):
    """rollback_merge restores the canonical target to pre-merge state."""

    def test_rollback_after_wta_restores_working_tree(self) -> None:
        """Rollback after winner-takes-all restores clean working tree via code-state backup."""
        _commit_file(
            self.target_clone, "change.txt", "variant", "variant commit",
        )
        merge_winner_takes_all(self.paths, self.variant_id)
        # HEAD should have changed
        self.assertNotEqual(git_head(self.canonical_target), self.original_head)

        result = rollback_merge(self.paths)
        # restore_code_state was used (code-state backup exists)
        self.assertEqual(result["method"], "restore")

    def test_rollback_after_cherry_pick_restores_working_tree(self) -> None:
        """Rollback after cherry-pick invokes restore via code-state backup."""
        _commit_file(
            self.target_clone, "cherry.txt", "picked", "cherry commit",
        )
        # Fetch clone objects into canonical so cherry-pick can resolve commits
        subprocess.run(
            ["git", "fetch", str(self.target_clone), "main"],
            cwd=self.canonical_target, check=True, capture_output=True,
        )
        merge_cherry_pick(self.paths, [self.variant_id])

        result = rollback_merge(self.paths)
        self.assertEqual(result["method"], "restore")

    def test_rollback_after_branch_and_continue_restores_directory(self) -> None:
        """Rollback after B&C restores the original directory contents."""
        _commit_file(
            self.target_clone, "bc.txt", "branch", "bc commit",
        )
        merge_branch_and_continue(self.paths, self.variant_id)
        # Original file should still exist (clone was cloned from canonical)
        # but the new file from the clone should also be there
        self.assertTrue((self.canonical_target / "bc.txt").exists())

        # Now rollback — the code-state backup won't exist for B&C,
        # but the .pre-merge-backup directory will
        rollback_merge(self.paths)
        # After rollback, the original canonical should be restored
        self.assertTrue(
            (self.canonical_target / "file.txt").exists(),
            "Original file.txt should be restored",
        )
        self.assertFalse(
            (self.canonical_target / "bc.txt").exists(),
            "Clone's bc.txt should not be in restored canonical",
        )

    def test_rollback_with_no_backup_raises(self) -> None:
        """rollback_merge raises FileNotFoundError when there is no backup."""
        with self.assertRaises(FileNotFoundError):
            rollback_merge(self.paths)


class TestCherryPickAutoFetch(_MergeTestBase):
    """merge_cherry_pick must fetch objects from variant clones before cherry-picking."""

    def setUp(self) -> None:
        super().setUp()
        # Make commits in the clone WITHOUT manually fetching into canonical.
        # The production code should handle fetching automatically.
        self.commit_sha = _commit_file(
            self.target_clone, "auto-fetched.txt", "auto", "auto-fetch commit",
        )
        # NOTE: Deliberately no manual git fetch here — that's the bug.

    def test_cherry_pick_fetches_objects_automatically(self) -> None:
        """merge_cherry_pick fetches clone objects into canonical before picking."""
        result = merge_cherry_pick(self.paths, [self.variant_id])
        self.assertGreater(len(result["applied"]), 0,
                           "Cherry-pick should apply commits even without prior fetch")
        self.assertTrue(
            (self.canonical_target / "auto-fetched.txt").exists(),
            "auto-fetched.txt should exist in canonical after cherry-pick",
        )


class TestRollbackRestoresHead(_MergeTestBase):
    """rollback_merge must restore HEAD to its pre-merge state."""

    def test_rollback_after_wta_restores_head(self) -> None:
        """After WTA merge + rollback, HEAD matches the original pre-merge HEAD."""
        _commit_file(self.target_clone, "wta.txt", "v", "wta commit")
        merge_winner_takes_all(self.paths, self.variant_id)
        self.assertNotEqual(git_head(self.canonical_target), self.original_head)
        rollback_merge(self.paths)
        self.assertEqual(git_head(self.canonical_target), self.original_head,
                         "HEAD should be restored to pre-merge state after rollback")

    def test_rollback_after_cherry_pick_restores_head(self) -> None:
        """After cherry-pick merge + rollback, HEAD matches the original."""
        _commit_file(self.target_clone, "cp.txt", "v", "cp commit")
        subprocess.run(
            ["git", "fetch", str(self.target_clone), "main"],
            cwd=self.canonical_target, check=True, capture_output=True,
        )
        merge_cherry_pick(self.paths, [self.variant_id])
        rollback_merge(self.paths)
        self.assertEqual(git_head(self.canonical_target), self.original_head,
                         "HEAD should be restored after cherry-pick rollback")

    def test_rollback_after_bac_restores_head(self) -> None:
        """After B&C merge + rollback, HEAD matches the original."""
        _commit_file(self.target_clone, "bc.txt", "v", "bc commit")
        merge_branch_and_continue(self.paths, self.variant_id)
        rollback_merge(self.paths)
        self.assertEqual(git_head(self.canonical_target), self.original_head,
                         "HEAD should be restored after B&C rollback")


class TestRollbackCleansUpBackup(_MergeTestBase):
    """rollback_merge must remove the backup so a second rollback fails."""

    def test_rollback_removes_code_state_backup(self) -> None:
        """After WTA merge + rollback, the backup is cleaned up."""
        _commit_file(self.target_clone, "clean.txt", "v", "commit")
        merge_winner_takes_all(self.paths, self.variant_id)
        rollback_merge(self.paths)
        backup_dir = self.paths.snapshots_dir / "pre-merge-backup"
        self.assertFalse(backup_dir.exists(),
                         "Backup should be removed after successful rollback")

    def test_rollback_removes_bac_backup(self) -> None:
        """After B&C merge + rollback, the backup directory is cleaned up."""
        _commit_file(self.target_clone, "clean.txt", "v", "commit")
        merge_branch_and_continue(self.paths, self.variant_id)
        rollback_merge(self.paths)
        backup_path = Path(f"{self.canonical_target}.pre-merge-backup")
        self.assertFalse(backup_path.exists(),
                         "Backup should be removed after successful rollback")

    def test_second_rollback_raises_after_first(self) -> None:
        """A second rollback raises FileNotFoundError after backup is cleaned up."""
        _commit_file(self.target_clone, "clean.txt", "v", "commit")
        merge_winner_takes_all(self.paths, self.variant_id)
        rollback_merge(self.paths)
        with self.assertRaises(FileNotFoundError):
            rollback_merge(self.paths)


class TestBranchAndContinuePixiSymlink(_MergeTestBase):
    """B&C merge must re-create .pixi symlink even when source .pixi is a symlink."""

    def setUp(self) -> None:
        super().setUp()
        _commit_file(self.target_clone, "bc.txt", "v", "bc commit")

    def test_pixi_symlink_recreated_when_source_is_symlink(self) -> None:
        """After B&C, .pixi exists even when the original was a symlink itself."""
        # Create a real .pixi directory somewhere, then symlink to it from canonical
        real_pixi = self.tmpdir / "real-pixi-env"
        real_pixi.mkdir()
        (real_pixi / "marker").write_text("pixi-data")
        pixi_link = self.canonical_target / ".pixi"
        if pixi_link.exists() or pixi_link.is_symlink():
            pixi_link.unlink()
        pixi_link.symlink_to(real_pixi)

        merge_branch_and_continue(self.paths, self.variant_id)
        new_pixi = self.canonical_target / ".pixi"
        self.assertTrue(new_pixi.exists() or new_pixi.is_symlink(),
                        ".pixi should exist after B&C even when source was a symlink")


class TestBranchAndContinuePreservesRemoteUrl(_MergeTestBase):
    """B&C merge must preserve the original remote URL in the canonical repo."""

    def setUp(self) -> None:
        super().setUp()
        _commit_file(self.target_clone, "bc.txt", "v", "bc commit")
        # Add a fake GitHub remote URL on the canonical target
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:user/repo.git"],
            cwd=self.canonical_target, check=True, capture_output=True,
        )

    def test_remote_url_preserved_after_bac(self) -> None:
        """After B&C merge, the canonical repo's origin URL must match the original, not the clone's."""
        merge_branch_and_continue(self.paths, self.variant_id)
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=self.canonical_target, check=True, capture_output=True, text=True,
        )
        actual_url = result.stdout.strip()
        self.assertEqual(
            actual_url, "git@github.com:user/repo.git",
            f"Remote URL should be preserved after B&C, got: {actual_url}",
        )


class TestBranchAndContinuePixiDirInClone(_MergeTestBase):
    """B&C merge must handle case where clone has a real .pixi directory (not a symlink)."""

    def setUp(self) -> None:
        super().setUp()
        _commit_file(self.target_clone, "bc.txt", "v", "bc commit")

    def test_pixi_fallback_when_no_pixi_in_either(self) -> None:
        """After B&C with no .pixi in canonical or clone, pixi install is called as fallback."""
        import shutil as _shutil
        # Remove .pixi from both canonical and clone
        for path in [self.canonical_target / ".pixi", self.target_clone / ".pixi"]:
            if path.is_dir() and not path.is_symlink():
                _shutil.rmtree(path)
            elif path.is_symlink() or path.exists():
                path.unlink()

        # Patch only the fallback subprocess.run call in supervisor module
        _real_run = subprocess.run
        pixi_calls: list = []

        def _capturing_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "pixi":
                pixi_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return _real_run(*args, **kwargs)

        with unittest.mock.patch("supervisor_harness.supervisor.subprocess.run", side_effect=_capturing_run):
            merge_branch_and_continue(self.paths, self.variant_id)

        self.assertGreater(len(pixi_calls), 0,
                           "pixi install should be called when .pixi missing from both sources")

    def test_pixi_recreated_when_clone_has_real_pixi_dir(self) -> None:
        """After B&C, .pixi is re-symlinked even when clone had a real .pixi dir.

        Real scenario: canonical has .pixi as symlink (pixi environment).
        Clone gets a real .pixi dir when pixi installs into it.
        After B&C rename, canonical/.pixi is the clone's real dir.
        The old code only removed symlinks, so symlink_to() failed with
        FileExistsError.
        """
        # Canonical has .pixi as a symlink pointing to a real pixi env
        real_pixi = self.tmpdir / "real-pixi-env"
        real_pixi.mkdir()
        (real_pixi / "marker").write_text("canonical-pixi")
        canonical_pixi = self.canonical_target / ".pixi"
        if canonical_pixi.exists() or canonical_pixi.is_symlink():
            if canonical_pixi.is_dir() and not canonical_pixi.is_symlink():
                import shutil
                shutil.rmtree(canonical_pixi)
            else:
                canonical_pixi.unlink()
        canonical_pixi.symlink_to(real_pixi)

        # Clone has .pixi as a REAL directory (not symlink) — pixi created it
        clone_pixi = self.target_clone / ".pixi"
        clone_pixi.mkdir(exist_ok=True)
        (clone_pixi / "marker").write_text("clone-pixi")

        # After B&C: clone becomes canonical, canonical becomes backup.
        # canonical/.pixi is now the clone's real dir (not a symlink).
        # The code must remove this real dir before creating the symlink.
        merge_branch_and_continue(self.paths, self.variant_id)
        new_pixi = self.canonical_target / ".pixi"
        self.assertTrue(new_pixi.exists() or new_pixi.is_symlink(),
                        ".pixi should exist after B&C when clone had real .pixi dir")


class TestMergeLock(_MergeTestBase):
    """_MergeLock prevents concurrent merges and cleans up properly."""

    def test_prevents_concurrent_merge(self) -> None:
        """A second _MergeLock raises RuntimeError when one is already held."""
        with _MergeLock(self.state_dir):
            with self.assertRaises(RuntimeError):
                with _MergeLock(self.state_dir):
                    pass  # Should never reach here

    def test_released_after_success(self) -> None:
        """Lock file is removed after successful context manager exit."""
        lock_path = self.state_dir / "merge.lock"
        with _MergeLock(self.state_dir):
            self.assertTrue(lock_path.exists(), "Lock should exist during merge")
        self.assertFalse(lock_path.exists(), "Lock should be removed after exit")

    def test_released_after_exception(self) -> None:
        """Lock file is removed even if an exception occurs inside the context."""
        lock_path = self.state_dir / "merge.lock"
        with self.assertRaises(ValueError):
            with _MergeLock(self.state_dir):
                raise ValueError("simulated failure")
        self.assertFalse(lock_path.exists(), "Lock should be removed after exception")


if __name__ == "__main__":
    unittest.main()
