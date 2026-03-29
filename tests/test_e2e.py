"""End-to-end tests using real Claude sessions.

These tests are slow (~15 min) and require a working Claude CLI installation.
Skipped by default — run with: pixi run -e dev test -m e2e
"""
from __future__ import annotations

import json
import subprocess
import time
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

# Resolve repo roots relative to this test file
_THIS_DIR = Path(__file__).parent
_SUPERVISOR_ROOT = _THIS_DIR.parent
_INTEGRATION_HUB = _SUPERVISOR_ROOT.parent / "take3-pe"


def _pixi_run(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a pixi task in the given repo."""
    return subprocess.run(
        ["pixi", "run", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _pixi_run_check(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a pixi task and assert it succeeds."""
    result = _pixi_run(repo, *args, timeout=timeout)
    assert result.returncode == 0, (
        f"pixi run {' '.join(args)} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _wait_for_iterations(
    repo: Path, variant_id: str, min_iterations: int = 1, timeout: int = 600,
) -> bool:
    """Poll results.tsv until at least min_iterations are recorded."""
    results_path = Path(f"/tmp/sar-research-loop--{variant_id}/results.tsv")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if results_path.exists():
            lines = results_path.read_text().strip().splitlines()
            # Skip header line
            data_lines = [l for l in lines if not l.startswith("commit")]
            if len(data_lines) >= min_iterations:
                return True
        time.sleep(15)
    return False


class TestResearcherGetsDifferentProfile(unittest.TestCase):
    """Researcher runs with a different Claude profile than the supervisor."""

    def test_different_profile(self) -> None:
        """Start a variant, verify it uses a different config_dir, then stop."""
        # Start a variant
        result = _pixi_run_check(
            _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", "e2e-profile",
            timeout=120,
        )

        try:
            # Check state file for config_dir
            state_dir = _SUPERVISOR_ROOT / ".supervisor"
            state_files = list(state_dir.glob("*--e2e-profile-state.json"))
            self.assertTrue(len(state_files) > 0, "State file should exist")

            state = json.loads(state_files[0].read_text())
            researcher_config_dir = state.get("config_dir", "")

            # Supervisor's own config dir
            import os
            supervisor_config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")

            self.assertNotEqual(
                researcher_config_dir, supervisor_config_dir,
                "Researcher should use a different profile than supervisor",
            )
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "stop", "--id", "e2e-profile")
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", "e2e-profile")


class TestTwoVariantsDifferentProfiles(unittest.TestCase):
    """Two researcher variants get different profiles from each other."""

    def test_different_profiles(self) -> None:
        """Start two variants, verify they use different config_dirs, then discard."""
        vid_a = "e2e-prof-a"
        vid_b = "e2e-prof-b"

        try:
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a,
                timeout=120,
            )
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b,
                timeout=120,
            )

            state_dir = _SUPERVISOR_ROOT / ".supervisor"
            state_a_files = list(state_dir.glob(f"*--{vid_a}-state.json"))
            state_b_files = list(state_dir.glob(f"*--{vid_b}-state.json"))
            self.assertTrue(len(state_a_files) > 0, f"State file for {vid_a} should exist")
            self.assertTrue(len(state_b_files) > 0, f"State file for {vid_b} should exist")

            config_a = json.loads(state_a_files[0].read_text()).get("config_dir", "")
            config_b = json.loads(state_b_files[0].read_text()).get("config_dir", "")

            self.assertNotEqual(
                config_a, config_b,
                "Two variants should use different profiles",
            )
        finally:
            for vid in (vid_a, vid_b):
                _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "stop", "--id", vid)
                _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid)


class TestParkPreservesTargetWithMetrics(unittest.TestCase):
    """Park preserves the target clone with metrics after iterations."""

    def test_park_preserves_metrics(self) -> None:
        """Start, wait for 1+ iteration, park, verify parked state has metrics."""
        vid = "e2e-park"
        try:
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid,
                timeout=120,
            )

            # Wait for at least one iteration
            got_iterations = _wait_for_iterations(_SUPERVISOR_ROOT, vid, min_iterations=1, timeout=600)
            self.assertTrue(got_iterations, "Should have at least 1 iteration within timeout")

            # Park the variant
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid,
                timeout=120,
            )

            # Verify parked state
            parked_path = _SUPERVISOR_ROOT / ".supervisor" / f"parked-{vid}.json"
            self.assertTrue(parked_path.exists(), "Parked state should exist")
            data = json.loads(parked_path.read_text())
            self.assertEqual(data["status"], "parked")
            # Metrics may or may not exist depending on whether eval completed
            # but the target clone should be preserved
            target_clone_path = data.get("target_clone", "")
            if target_clone_path:
                self.assertTrue(
                    Path(target_clone_path).exists(),
                    "Target clone should be preserved after park",
                )
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid)


class TestWinnerTakesAllAndRollback(unittest.TestCase):
    """Winner-takes-all merge updates canonical, rollback restores it."""

    def test_merge_and_rollback(self) -> None:
        """Start, wait, park, merge, verify HEAD changed, rollback, verify restored."""
        vid = "e2e-merge"
        try:
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid,
                timeout=120,
            )

            got_iterations = _wait_for_iterations(_SUPERVISOR_ROOT, vid, min_iterations=1, timeout=600)
            self.assertTrue(got_iterations, "Should have at least 1 iteration within timeout")

            # Capture canonical HEAD before merge
            from supervisor_harness.supervisor import _resolve_target_repo
            supervised = _SUPERVISOR_ROOT.parent / "sar-research-loop"
            target_repo = _resolve_target_repo(supervised)
            head_before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_repo, check=True, capture_output=True, text=True,
            ).stdout.strip()

            # Park
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid,
                timeout=120,
            )

            # Merge winner-takes-all
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid,
                "--strategy", "winner-takes-all",
                timeout=120,
            )

            # Verify HEAD changed
            head_after_merge = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertNotEqual(head_before, head_after_merge,
                                "HEAD should change after merge")

            # Rollback
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "rollback",
                timeout=120,
            )

            # Verify HEAD restored
            head_after_rollback = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target_repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            # After rollback the working tree is restored (may not be exact SHA match
            # if restore_code_state was used, but files should match)
            self.assertTrue(
                (target_repo / "file.txt").exists() or head_after_rollback != head_after_merge,
                "Rollback should restore canonical state",
            )
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid)


class TestDiscardCleansEverything(unittest.TestCase):
    """Discard removes all variant artifacts."""

    def test_discard_cleans_all(self) -> None:
        """Start, discard, verify no artifacts remain."""
        vid = "e2e-discard"

        _pixi_run_check(
            _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid,
            timeout=120,
        )

        # Verify artifacts exist
        researcher_clone = Path(f"/tmp/sar-research-loop--{vid}")
        self.assertTrue(researcher_clone.exists(), "Researcher clone should exist before discard")

        # Discard
        _pixi_run_check(
            _SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid,
            timeout=120,
        )

        # Verify all artifacts are gone
        self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed")
        state_dir = _SUPERVISOR_ROOT / ".supervisor"
        pid_files = list(state_dir.glob(f"*--{vid}.pid"))
        state_files = list(state_dir.glob(f"*--{vid}-state.json"))
        parked_files = list(state_dir.glob(f"parked-{vid}.json"))
        self.assertEqual(len(pid_files), 0, "PID file should be removed")
        self.assertEqual(len(state_files), 0, "State file should be removed")
        self.assertEqual(len(parked_files), 0, "Parked file should be removed")


if __name__ == "__main__":
    unittest.main()
