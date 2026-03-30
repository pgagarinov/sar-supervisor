"""Tests for the full variant lifecycle: start, stop, park, discard, list.

Unit tests using temp repos and mocked subprocess calls — no real Claude sessions.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from supervisor_harness.config import RepoPaths, save_state, LaunchSpec
from supervisor_harness.supervisor import (
    _create_target_clone,
    _create_variant_clone,
    _remove_variant_clones,
    discard_researcher_variant,
    list_parked_variants,
    list_researcher_variants,
    park_researcher_variant,
    start_researcher_variant,
    stop_researcher_variant,
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


def _git_head(path: Path) -> str:
    """Return HEAD sha for the repo at path."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class _VariantTestBase(unittest.TestCase):
    """Base class that sets up a workspace with supervised + target repos.

    Sets CLAUDE_CONFIG_DIRS in the environment for the entire test so that
    internal calls to RepoPaths.discover() inside production code work.
    """

    variant_id: str = ""  # Auto-set from class name in setUp

    def setUp(self) -> None:
        # Unique variant_id per test class to avoid parallel collisions
        self.variant_id = f"rv-{self.__class__.__name__.lower()}"
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

        # Supervised repo (researcher)
        self.supervised = self.tmpdir / "sar-research-loop"
        _init_repo(self.supervised)
        # Create .claude/skills/start/SKILL.md so discover works
        skill_dir = self.supervised / ".claude" / "skills" / "start"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Start skill\n")
        _commit_file(self.supervised, ".claude/skills/start/SKILL.md", "# Start skill\n", "add skill")

        # Target repo
        self.target = self.tmpdir / "sar-rag-target"
        _init_repo(self.target)
        (self.target / ".pixi").mkdir(exist_ok=True)
        (self.target / ".pixi" / "marker").write_text("pixi-env")

        # Point supervised .env at target
        (self.supervised / ".env").write_text(f"TARGET_PATH={self.target}\n")

        # Workspace (supervisor)
        self.workspace = self.tmpdir / "sar-supervisor"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.workspace / ".supervisor"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Write minimal harness.toml
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

        # Set CLAUDE_CONFIG_DIRS for entire test duration so internal
        # RepoPaths.discover() calls work
        self._config_dirs_str = f"{self.profile_a}:{self.profile_b}"
        self._env_patcher = patch.dict(
            "os.environ", {"CLAUDE_CONFIG_DIRS": self._config_dirs_str},
        )
        self._env_patcher.start()

        self.paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=self.supervised,
        )

    def tearDown(self) -> None:
        self._env_patcher.stop()
        # Clean up any clones created in /tmp
        for p in Path("/tmp").glob(f"sar-research-loop--{self.variant_id}*"):
            if p.exists():
                shutil.rmtree(p)
        target_clone = Path(f"{self.target}--{self.variant_id}")
        if target_clone.exists():
            shutil.rmtree(target_clone)
        self._tmpdir.cleanup()


class TestStartResearcherVariant(_VariantTestBase):
    """start_researcher_variant creates clones and spawns a process."""

    def _mock_popen(self) -> MagicMock:
        """Create a Popen mock that returns a fake process with working communicate()."""
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.__enter__ = MagicMock(return_value=mock_process)
        mock_process.__exit__ = MagicMock(return_value=False)
        mock_process.communicate = MagicMock(return_value=(b"", b""))
        mock_process.returncode = 0
        mock_process.stdout = b""
        mock_process.stderr = b""
        return mock_process

    def test_creates_researcher_and_target_clones(self) -> None:
        """start_researcher_variant creates both researcher clone and target clone."""
        _real_popen = subprocess.Popen
        mock_process = self._mock_popen()

        def _popen_side_effect(*args: Any, **kwargs: Any) -> Any:
            # Let git commands through, mock the claude launch
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd[0] == "/bin/bash":
                return mock_process
            return _real_popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            launch_spec, pid, vid = start_researcher_variant(
                self.paths, self.variant_id, clean_first=False,
            )

        researcher_clone = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        target_clone = Path(f"{self.target}--{self.variant_id}")
        self.assertTrue(researcher_clone.exists(), "Researcher clone should exist")
        self.assertTrue(target_clone.exists(), "Target clone should exist")

    def test_applies_variant_skill_to_clone(self) -> None:
        """start_researcher_variant applies variant SKILL.md to the researcher clone."""
        variant_skill = self.tmpdir / "variant_skill.md"
        variant_skill.write_text("# Custom variant skill\nDo something different.\n")

        _real_popen = subprocess.Popen
        mock_process = self._mock_popen()

        def _popen_side_effect(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd[0] == "/bin/bash":
                return mock_process
            return _real_popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            launch_spec, pid, vid = start_researcher_variant(
                self.paths, self.variant_id,
                variant_path=variant_skill,
                clean_first=False,
            )

        researcher_clone = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        skill_path = researcher_clone / ".claude" / "skills" / "start" / "SKILL.md"
        self.assertTrue(skill_path.exists(), "SKILL.md should exist in clone")
        content = skill_path.read_text()
        self.assertIn("Custom variant skill", content)


class TestStopResearcherVariant(_VariantTestBase):
    """stop_researcher_variant stops process but preserves clones."""

    def test_stop_preserves_clones(self) -> None:
        """stop_researcher_variant stops the process but keeps clones intact."""
        # Create clones manually
        researcher_clone = _create_variant_clone(self.supervised, self.variant_id)
        target_clone = _create_target_clone(self.target, self.variant_id)

        # Write a PID file so stop_run finds it
        var_paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=researcher_clone,
            variant_id=self.variant_id,
        )
        var_paths.state_dir.mkdir(parents=True, exist_ok=True)
        var_paths.pid_path.write_text("99999\n")

        # stop_run will check process_running which returns False for a fake PID
        stop_researcher_variant(self.paths, self.variant_id)

        # Clones should still exist
        self.assertTrue(researcher_clone.exists(), "Researcher clone should be preserved")
        self.assertTrue(target_clone.exists(), "Target clone should be preserved")


class TestParkResearcherVariant(_VariantTestBase):
    """park_researcher_variant preserves target clone with metrics."""

    def _setup_parked(self) -> Path:
        """Create a target clone with commits and a fake eval report. Returns target clone path."""
        target_clone = _create_target_clone(self.target, self.variant_id)

        # Configure git user in clone
        subprocess.run(
            ["git", "config", "user.email", "t@t"],
            cwd=target_clone, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=target_clone, check=True, capture_output=True,
        )

        # Make some commits
        _commit_file(target_clone, "improvement.py", "# better code", "improve target")

        # Write a fake eval report
        report_path = Path(f"/tmp/rag-eval-report--{self.variant_id}.json")
        report_path.write_text(json.dumps({
            "total": 20, "passed": 18, "failed": 2,
        }))

        # Create researcher clone (park will try to stop it)
        researcher_clone = _create_variant_clone(self.supervised, self.variant_id)

        # Write variant state file so stop_run can find the PID
        var_paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=researcher_clone,
            variant_id=self.variant_id,
        )
        var_paths.state_dir.mkdir(parents=True, exist_ok=True)
        var_paths.pid_path.write_text("99999\n")

        return target_clone

    def test_creates_parked_json_with_metrics(self) -> None:
        """park_researcher_variant creates parked-{id}.json with metrics."""
        self._setup_parked()
        result = park_researcher_variant(self.paths, self.variant_id)

        parked_path = self.state_dir / f"parked-{self.variant_id}.json"
        self.assertTrue(parked_path.exists(), "parked JSON should be created")
        data = json.loads(parked_path.read_text())
        self.assertEqual(data["variant_id"], self.variant_id)
        self.assertEqual(data["status"], "parked")
        self.assertIsNotNone(data["metrics"])

        # Cleanup report
        report_path = Path(f"/tmp/rag-eval-report--{self.variant_id}.json")
        if report_path.exists():
            report_path.unlink()

    def test_auto_commits_uncommitted_target_changes(self) -> None:
        """park_researcher_variant auto-commits uncommitted changes in target clone."""
        target_clone = self._setup_parked()

        # Add an uncommitted file to the target clone
        (target_clone / "dirty.txt").write_text("uncommitted change")
        subprocess.run(["git", "add", "-A"], cwd=target_clone, check=True, capture_output=True)

        park_researcher_variant(self.paths, self.variant_id)

        # Target clone should still exist (parked, not discarded)
        self.assertTrue(target_clone.exists(), "Target clone should be preserved")

        # The dirty file should be committed (git status clean)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_clone, check=True, capture_output=True, text=True,
        )
        self.assertEqual(status.stdout.strip(), "", "Target clone should have clean status")

        # Cleanup
        report_path = Path(f"/tmp/rag-eval-report--{self.variant_id}.json")
        if report_path.exists():
            report_path.unlink()

    def test_removes_researcher_clone_keeps_target(self) -> None:
        """park_researcher_variant removes researcher clone but keeps target clone."""
        target_clone = self._setup_parked()
        researcher_clone = Path(f"/tmp/sar-research-loop--{self.variant_id}")
        self.assertTrue(researcher_clone.exists(), "Researcher clone should exist before park")

        park_researcher_variant(self.paths, self.variant_id)

        self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed after park")
        self.assertTrue(target_clone.exists(), "Target clone should be preserved after park")

        # Cleanup
        report_path = Path(f"/tmp/rag-eval-report--{self.variant_id}.json")
        if report_path.exists():
            report_path.unlink()


class TestDiscardResearcherVariant(_VariantTestBase):
    """discard_researcher_variant removes all clones and parked state."""

    def test_removes_all_clones_and_parked_state(self) -> None:
        """discard_researcher_variant removes all clones and parked state."""
        # Create clones
        researcher_clone = _create_variant_clone(self.supervised, self.variant_id)
        target_clone = _create_target_clone(self.target, self.variant_id)

        # Write a PID file
        var_paths = RepoPaths.discover(
            workspace_root=self.workspace,
            supervised_repo=researcher_clone,
            variant_id=self.variant_id,
        )
        var_paths.state_dir.mkdir(parents=True, exist_ok=True)
        var_paths.pid_path.write_text("99999\n")

        # Write parked state
        parked_path = self.state_dir / f"parked-{self.variant_id}.json"
        parked_path.write_text(json.dumps({"variant_id": self.variant_id}))

        discard_researcher_variant(self.paths, self.variant_id)

        self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed")
        self.assertFalse(target_clone.exists(), "Target clone should be removed")
        self.assertFalse(parked_path.exists(), "Parked state should be removed")


class TestListResearcherVariants(_VariantTestBase):
    """list_researcher_variants returns running and stopped variants."""

    def test_returns_running_and_stopped_variants(self) -> None:
        """list_researcher_variants returns variants with correct running status."""
        skill_name = self.paths.skill_name

        # Create PID files for two variants
        vid_a = "rv-list-a"
        vid_b = "rv-list-b"
        pid_a = self.state_dir / f"{skill_name}--{vid_a}.pid"
        pid_b = self.state_dir / f"{skill_name}--{vid_b}.pid"
        pid_a.write_text("99998\n")  # Non-existent PID → stopped
        pid_b.write_text("99997\n")  # Non-existent PID → stopped

        # Write state files
        state_a = self.state_dir / f"{skill_name}--{vid_a}-state.json"
        state_a.write_text(json.dumps({
            "started_at": "2026-01-01T00:00:00Z",
            "prompt": "/start",
            "log_path": "/tmp/log-a.log",
            "config_dir": str(self.profile_a),
        }))

        variants = list_researcher_variants(self.paths)
        variant_ids = [v["variant_id"] for v in variants]
        self.assertIn(vid_a, variant_ids)
        self.assertIn(vid_b, variant_ids)

        # Both should be stopped (fake PIDs)
        for v in variants:
            self.assertFalse(v["running"], f"Variant {v['variant_id']} should be stopped")


class TestListResearcherVariantsIncludesParked(_VariantTestBase):
    """list_researcher_variants must include parked variants (not just PID-based ones)."""

    def test_parked_variants_included_in_list(self) -> None:
        """Parked variants (with parked-*.json but no PID file) appear in list."""
        # Write a parked state file (no PID file exists for this variant)
        parked = {
            "variant_id": "rv-parked-only",
            "status": "parked",
            "parked_at": "2026-01-01T00:00:00Z",
            "metrics": {"total": 20, "passed": 18, "failed": 2},
        }
        (self.state_dir / "parked-rv-parked-only.json").write_text(json.dumps(parked))

        variants = list_researcher_variants(self.paths)
        variant_ids = [v["variant_id"] for v in variants]
        self.assertIn("rv-parked-only", variant_ids,
                      "Parked variants should be included in list_researcher_variants")


class TestListParkedVariants(_VariantTestBase):
    """list_parked_variants returns parked variants with metrics."""

    def test_returns_parked_variants_with_metrics(self) -> None:
        """list_parked_variants returns parked variants with their metrics."""
        # Write two parked state files
        parked_a = {
            "variant_id": "rv-park-a",
            "status": "parked",
            "metrics": {"total": 20, "passed": 18, "failed": 2},
        }
        parked_b = {
            "variant_id": "rv-park-b",
            "status": "parked",
            "metrics": {"total": 20, "passed": 15, "failed": 5},
        }
        (self.state_dir / "parked-rv-park-a.json").write_text(json.dumps(parked_a))
        (self.state_dir / "parked-rv-park-b.json").write_text(json.dumps(parked_b))

        result = list_parked_variants(self.paths)
        self.assertEqual(len(result), 2)
        variant_ids = [r["variant_id"] for r in result]
        self.assertIn("rv-park-a", variant_ids)
        self.assertIn("rv-park-b", variant_ids)

        # Check metrics are present
        for item in result:
            self.assertIn("metrics", item)
            self.assertIsNotNone(item["metrics"])


class TestLaunchSpecIncludesTargetRepo(_VariantTestBase):
    """start_researcher_variant must set TARGET_REPO to the target clone path in the launch command."""

    def test_launch_command_contains_target_repo_pointing_to_clone(self) -> None:
        """TARGET_REPO env var in launch command must point to the target CLONE, not canonical."""
        _real_popen = subprocess.Popen
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.__enter__ = MagicMock(return_value=mock_process)
        mock_process.__exit__ = MagicMock(return_value=False)
        mock_process.communicate = MagicMock(return_value=(b"", b""))
        mock_process.returncode = 0
        mock_process.stdout = b""
        mock_process.stderr = b""

        def _popen_side_effect(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd[0] == "/bin/bash":
                return mock_process
            return _real_popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            launch_spec, pid, vid = start_researcher_variant(
                self.paths, self.variant_id, clean_first=False,
            )

        # The launch command must contain TARGET_REPO pointing to the clone
        target_clone_path = f"{self.target}--{self.variant_id}"
        self.assertIn("TARGET_REPO=", launch_spec.command,
                       "Launch command must set TARGET_REPO env var")
        self.assertIn(target_clone_path, launch_spec.command,
                       f"TARGET_REPO must point to clone {target_clone_path}, not canonical {self.target}")

    def test_launch_command_contains_canonical_target(self) -> None:
        """CANONICAL_TARGET env var must point to the original (non-clone) target."""
        _real_popen = subprocess.Popen
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.__enter__ = MagicMock(return_value=mock_process)
        mock_process.__exit__ = MagicMock(return_value=False)
        mock_process.communicate = MagicMock(return_value=(b"", b""))
        mock_process.returncode = 0
        mock_process.stdout = b""
        mock_process.stderr = b""

        def _popen_side_effect(*args: Any, **kwargs: Any) -> Any:
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd[0] == "/bin/bash":
                return mock_process
            return _real_popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=_popen_side_effect):
            launch_spec, pid, vid = start_researcher_variant(
                self.paths, self.variant_id, clean_first=False,
            )

        self.assertIn("CANONICAL_TARGET=", launch_spec.command,
                       "Launch command must set CANONICAL_TARGET env var")
        self.assertIn(str(self.target), launch_spec.command,
                       f"CANONICAL_TARGET must contain canonical path {self.target}")


class TestCLIVariantStartKwargsMatch(unittest.TestCase):
    """Regression: CLI must only pass kwargs that start_researcher_variant accepts."""

    def test_cli_does_not_pass_unknown_kwargs(self):
        """Every kwarg the CLI passes to start_researcher_variant must exist in its signature."""
        import inspect
        import re

        from supervisor_harness.supervisor import start_researcher_variant

        # Get accepted params from function signature
        sig = inspect.signature(start_researcher_variant)
        accepted = set(sig.parameters.keys())

        # Parse the CLI source to find what kwargs it passes
        cli_path = Path(__file__).parent.parent / "src" / "supervisor_harness" / "cli.py"
        source = cli_path.read_text()

        # Find the call to start_researcher_variant
        call_match = re.search(
            r"start_researcher_variant\((.*?)\)", source, re.DOTALL
        )
        self.assertIsNotNone(call_match, "CLI should call start_researcher_variant")

        call_text = call_match.group(1)
        kwarg_names = set(re.findall(r"(\w+)\s*=", call_text))
        kwarg_names.discard("paths")  # positional

        unknown = kwarg_names - accepted
        self.assertEqual(
            unknown, set(),
            f"CLI passes kwargs not accepted by start_researcher_variant: {unknown}",
        )


if __name__ == "__main__":
    unittest.main()
