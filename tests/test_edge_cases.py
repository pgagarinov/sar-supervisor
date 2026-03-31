"""Edge case tests for variant lifecycle and merge operations.

Verifies graceful handling of stopped variants, dirty targets,
non-existent variants, diverged canonicals, empty commit ranges,
and missing .pixi directories.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supervisor_harness.config import RepoPaths
from supervisor_harness.supervisor import (
    _create_target_clone,
    _create_variant_clone,
    _remove_variant_clones,
    discard_researcher_variant,
    merge_branch_and_continue,
    merge_cherry_pick,
    merge_winner_takes_all,
    park_researcher_variant,
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


class _EdgeCaseTestBase(unittest.TestCase):
    """Base class with a complete workspace for edge case tests."""

    variant_id: str = ""

    def setUp(self) -> None:
        self.variant_id = f"rv-{self.__class__.__name__.lower()}"
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

        # Supervised repo (researcher)
        self.supervised = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised)
        skill_dir = self.supervised / ".claude" / "skills" / "start"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Start\n")
        _commit_file(self.supervised, ".claude/skills/start/SKILL.md", "# Start\n", "add skill")

        # Target repo
        self.target = self.tmpdir / "sar-rag-target"
        _init_repo(self.target)
        _tag(self.target, "baseline")
        (self.target / ".pixi").mkdir(exist_ok=True)
        (self.target / ".pixi" / "marker").write_text("pixi-env")

        # Point supervised .env at target
        (self.supervised / ".env").write_text(f"TARGET_PATH={self.target}\n")

        # Workspace (supervisor)
        self.workspace = self.tmpdir / "sar-supervisor"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # Project dirs — match what RepoPaths.discover() will compute
        self._project_id = f"test-{self.__class__.__name__.lower()}"
        project_dir = self.tmpdir / "projects" / self._project_id
        self.state_dir = project_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Write harness.toml
        harness_toml = self.workspace / "harness.toml"
        harness_toml.write_text(
            '[project]\nname = "test"\n'
            '[supervised]\n'
            f'repo = "{self.supervised}"\n'
            'skill_name = "start"\n'
            'default_prompt = "/start"\n'
            'agents = []\n'
            '[reports]\n'
            f'primary = "{self.tmpdir}/rag-eval-report.json"\n'
            '[reports.metric]\nreport = "primary"\nfield = "failed"\n'
            '[log]\n'
            f'path = "{self.tmpdir}/cc-test.log"\n'
        )

        # Profile dirs
        self.profile_a = self.tmpdir / ".claude-a"
        self.profile_b = self.tmpdir / ".claude-b"
        self.profile_a.mkdir()
        self.profile_b.mkdir()

        # Set env vars for entire test duration
        self._config_dirs_str = f"{self.profile_a}:{self.profile_b}"
        self._env_patcher = patch.dict(
            "os.environ", {
                "CLAUDE_CONFIG_DIRS": self._config_dirs_str,
                "SAR_PROJECTS_ROOT": str(self.tmpdir / "projects"),
                "SAR_PROJECT_ID": self._project_id,
            },
        )
        self._env_patcher.start()

        self.paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=self.supervised,
        )

    def tearDown(self) -> None:
        self._env_patcher.stop()
        # Clean up any pre-merge-backup alongside the target
        backup = Path(f"{self.target}.pre-merge-backup")
        if backup.exists():
            shutil.rmtree(backup)
        self._tmpdir.cleanup()


class TestParkAlreadyStopped(_EdgeCaseTestBase):
    """Park a variant whose process is already dead."""

    def test_park_already_stopped_variant(self) -> None:
        """Parking a variant with a non-existent PID still works."""
        # Create target clone with a commit
        target_clone = _create_target_clone(self.target, self.variant_id)
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )
        _commit_file(target_clone, "work.py", "# code", "do work")

        # Write a PID file with a dead PID — no researcher clone exists
        var_paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=self.supervised,
            variant_id=self.variant_id,
        )
        var_paths.state_dir.mkdir(parents=True, exist_ok=True)
        var_paths.pid_path.write_text("99999\n")

        # Park should succeed even though process is already dead
        result = park_researcher_variant(self.paths, self.variant_id)
        self.assertEqual(result["status"], "parked")
        self.assertEqual(result["variant_id"], self.variant_id)


class TestParkDirtyTarget(_EdgeCaseTestBase):
    """Park a variant with uncommitted target files."""

    def test_park_with_dirty_target(self) -> None:
        """Parking auto-commits uncommitted files in target clone."""
        target_clone = _create_target_clone(self.target, self.variant_id, clone_base=self.paths.clone_dir)
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )
        _commit_file(target_clone, "clean.py", "# clean", "clean commit")

        # Leave dirty files
        (target_clone / "dirty.txt").write_text("not committed")

        # Write PID file
        var_paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=self.supervised,
            variant_id=self.variant_id,
        )
        var_paths.state_dir.mkdir(parents=True, exist_ok=True)
        var_paths.pid_path.write_text("99999\n")

        park_researcher_variant(self.paths, self.variant_id)

        # dirty.txt should now be committed
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_clone, check=True, capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "",
                         "Target clone should have clean status after park auto-commit")


class TestMergeNonExistentVariant(_EdgeCaseTestBase):
    """Merge a variant whose target clone doesn't exist."""

    def test_merge_non_existent_raises(self) -> None:
        """merge_winner_takes_all raises FileNotFoundError for missing variant."""
        with self.assertRaises(FileNotFoundError) as ctx:
            merge_winner_takes_all(self.paths, "rv-does-not-exist")
        self.assertIn("not found", str(ctx.exception).lower())


class TestDiscardNonExistent(_EdgeCaseTestBase):
    """Discard a non-existent variant."""

    def test_discard_non_existent_no_crash(self) -> None:
        """discard_researcher_variant on a non-existent variant does not crash."""
        # Should not raise any exceptions
        discard_researcher_variant(self.paths, "rv-never-existed")


class TestMergeAfterCanonicalDiverged(_EdgeCaseTestBase):
    """Merge after the canonical target has diverged with new commits."""

    def test_backup_includes_diverged_state(self) -> None:
        """B&C backup includes the canonical's diverged commits."""
        # Remove .pixi from canonical so the merge won't try to re-symlink
        pixi_dir = self.target / ".pixi"
        if pixi_dir.exists():
            shutil.rmtree(pixi_dir)

        # Make a commit in canonical after baseline
        diverged_head = _commit_file(
            self.target, "canonical-new.txt", "new canonical work", "diverge canonical",
        )

        # Create target clone from canonical (includes diverged commit)
        target_clone = _create_target_clone(self.target, self.variant_id)
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )
        _commit_file(target_clone, "variant-work.txt", "variant improvement", "variant commit")

        result = merge_branch_and_continue(self.paths, self.variant_id)

        # Backup should exist and contain the diverged canonical state
        backup_path = Path(result["backup"])
        self.assertTrue(backup_path.exists(), "Backup should exist")
        self.assertTrue(
            (backup_path / "canonical-new.txt").exists(),
            "Backup should contain the diverged canonical file",
        )


class TestRollbackAfterDivergedCanonical(_EdgeCaseTestBase):
    """Rollback after WTA merge on a diverged canonical restores the diverged HEAD."""

    def test_rollback_restores_diverged_head(self) -> None:
        """After WTA merge + rollback, HEAD matches the pre-merge diverged state."""
        from supervisor_harness.supervisor import merge_winner_takes_all, rollback_merge

        # Diverge canonical with a manual commit
        diverged_head = _commit_file(
            self.target, "manual.txt", "manual change", "manual divergence",
        )

        # Create target clone and make a variant commit
        target_clone = _create_target_clone(self.target, self.variant_id)
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )
        _commit_file(target_clone, "variant.txt", "variant", "variant commit")

        # WTA merge
        merge_winner_takes_all(self.paths, self.variant_id)

        # Rollback should restore to the diverged HEAD (pre-merge)
        rollback_merge(self.paths)
        from harness_core.git_utils import git_head
        restored_head = git_head(self.target)
        self.assertEqual(diverged_head, restored_head,
                         "Rollback should restore the diverged pre-merge HEAD")


class TestCherryPickEmptyRange(_EdgeCaseTestBase):
    """Cherry-pick with no commits since baseline."""

    def test_empty_commit_range(self) -> None:
        """Cherry-pick with no new commits returns empty applied list."""
        # Create a target clone with no new commits beyond baseline
        target_clone = _create_target_clone(self.target, self.variant_id)

        # Fetch clone into canonical so cherry-pick can work
        subprocess.run(
            ["git", "fetch", str(target_clone), "main"],
            cwd=self.target, check=True, capture_output=True,
        )

        result = merge_cherry_pick(self.paths, [self.variant_id])
        self.assertEqual(result["applied"], [],
                         "No commits should be applied when range is empty")


class TestBranchAndContinueMissingPixi(_EdgeCaseTestBase):
    """B&C merge when the clone has no .pixi directory."""

    def test_still_works_without_pixi(self) -> None:
        """merge_branch_and_continue succeeds even if clone has no .pixi."""
        # Create target clone
        target_clone = _create_target_clone(self.target, self.variant_id)
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )
        _commit_file(target_clone, "work.txt", "work", "do work")

        # Remove .pixi from clone (simulating it was never symlinked or removed)
        pixi_link = target_clone / ".pixi"
        if pixi_link.is_symlink() or pixi_link.exists():
            if pixi_link.is_symlink():
                pixi_link.unlink()
            elif pixi_link.is_dir():
                shutil.rmtree(pixi_link)

        result = merge_branch_and_continue(self.paths, self.variant_id)
        self.assertEqual(result["strategy"], "branch_and_continue")
        # The merged canonical should have the work file
        self.assertTrue(
            (self.target / "work.txt").exists(),
            "Canonical should contain clone's work.txt after merge",
        )


if __name__ == "__main__":
    unittest.main()
