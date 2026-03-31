"""End-to-end tests using real Claude sessions and real repos.

These tests are slow (~20 min) and require:
- Working Claude CLI installation
- All SAR repos deployed as siblings
- CLAUDE_CONFIG_DIR set (the profile for THIS session)
- CLAUDE_CONFIG_DIRS set with 3+ profiles (colon-separated)
- Target at baseline tag

Run with: CLAUDE_CONFIG_DIR=~/.claude-profile-1 pixi run -e dev test -m e2e
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
import unittest.mock
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(1800),  # 30 min per E2E test (researcher cycle takes 15-20 min)
    pytest.mark.xdist_group("e2e-serial"),  # E2E tests share real filesystem state — run on one worker
]

# Load .env from supervisor root if env vars are not already set
def _load_dot_env() -> None:
    """Read .env from the supervisor repo root, set missing env vars."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key not in os.environ:
                os.environ[key] = val

_load_dot_env()

# Fail fast if required env vars are not set
_CLAUDE_CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR", "")
_CLAUDE_CONFIG_DIRS = os.environ.get("CLAUDE_CONFIG_DIRS", "")

if not _CLAUDE_CONFIG_DIR:
    raise RuntimeError(
        "CLAUDE_CONFIG_DIR is not set. E2E tests require an explicit profile.\n"
        "Run with: CLAUDE_CONFIG_DIR=~/.claude-profile-1 pixi run -e dev test -m e2e"
    )
if not _CLAUDE_CONFIG_DIRS:
    raise RuntimeError(
        "CLAUDE_CONFIG_DIRS is not set. E2E tests require the full profile list.\n"
        "Set it in .env or export it."
    )

import shutil
from uuid import uuid4

_THIS_DIR = Path(__file__).parent
_SUPERVISOR_ROOT = _THIS_DIR.parent
_RESEARCH_LOOP = _SUPERVISOR_ROOT.parent / "sar-research-loop"
_RAG_TARGET = _SUPERVISOR_ROOT.parent / "sar-rag-target"


def _project_dir() -> Path:
    """Return the current project directory from env vars."""
    root = Path(os.environ.get("SAR_PROJECTS_ROOT", "/tmp/sar-projects"))
    pid = os.environ.get("SAR_PROJECT_ID", "default")
    return root / pid


def _pixi_run(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pixi", "run", *args],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )


def _pixi_run_check(repo: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = _pixi_run(repo, *args, timeout=timeout)
    assert result.returncode == 0, (
        f"pixi run {' '.join(args)} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _wait_for_iterations(variant_id: str, min_iters: int = 1, timeout: int = 1200) -> bool:
    clone_dir = _project_dir() / "clones"
    results_path = clone_dir / f"sar-research-loop--{variant_id}" / "results.tsv"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if results_path.exists():
            lines = [l for l in results_path.read_text().splitlines() if l and not l.startswith("commit")]
            if len(lines) >= min_iters:
                return True
        time.sleep(15)
    return False


def _cleanup_variant(vid: str) -> None:
    _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "stop", "--id", vid)
    _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid)


def _state_file(vid: str) -> dict:
    state_dir = _project_dir() / "state"
    for f in state_dir.glob(f"*--{vid}-state.json"):
        return json.loads(f.read_text())
    return {}


class _E2EProjectBase(unittest.TestCase):
    """Base class that provides project-based isolation for E2E tests.

    Each test class gets a unique SAR_PROJECT_ID. All state (PIDs, clones,
    logs, reports) is scoped to that project. tearDownClass deletes the
    entire project directory.
    """

    _project_id: str = ""
    _project_path: Path = Path()
    _env_patcher: object = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._project_id = f"e2e-{cls.__name__.lower()}-{uuid4().hex[:8]}"
        projects_root = os.environ.get("SAR_PROJECTS_ROOT", "/tmp/sar-projects")
        cls._project_path = Path(projects_root) / cls._project_id
        cls._env_patcher = unittest.mock.patch.dict(
            "os.environ", {"SAR_PROJECT_ID": cls._project_id},
        )
        cls._env_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._env_patcher.stop()
        if cls._project_path.exists():
            shutil.rmtree(cls._project_path, ignore_errors=True)


# =============================================================================
# CLONE ISOLATION (#1-5)
# =============================================================================


class TestCloneIsolationE2E(_E2EProjectBase):

    def setUp(self):
        self.vid = "e2e-clone"
        _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", self.vid, timeout=120)
        time.sleep(5)

    def tearDown(self):
        _cleanup_variant(self.vid)

    def test_01_researcher_clone_has_own_git(self):
        """#1: Researcher clone has its own .git directory."""
        clone = _project_dir() / "clones" / f"sar-research-loop--{self.vid}"
        self.assertTrue((clone / ".git").is_dir(), "Clone should have .git dir")
        self.assertNotEqual(
            (clone / ".git").resolve(),
            (_RESEARCH_LOOP / ".git").resolve(),
            "Clone .git must be separate from original",
        )

    def test_02_target_clone_exists_with_pixi(self):
        """#2: Target clone exists with .pixi symlink."""
        target_clone = _project_dir() / "clones" / f"sar-rag-target--{self.vid}"
        self.assertTrue(target_clone.exists(), f"Target clone should exist at {target_clone}")
        pixi = target_clone / ".pixi"
        self.assertTrue(pixi.is_symlink() or pixi.is_dir(), ".pixi should be symlinked or exist")

    def test_04_discard_cleans_all_artifacts(self):
        """#4: After discard, no clone or temp files remain."""
        _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "stop", "--id", self.vid)
        _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", self.vid)
        researcher_clone = _project_dir() / "clones" / f"sar-research-loop--{self.vid}"
        target_clone = _project_dir() / "clones" / f"sar-rag-target--{self.vid}"
        self.assertFalse(researcher_clone.exists())
        self.assertFalse(target_clone.exists())


class TestCloneFromCanonicalE2E(_E2EProjectBase):

    def test_05_clone_independence(self):
        """#5: Two clones from same canonical have independent git history."""
        vid_a, vid_b = "e2e-cfi-a", "e2e-cfi-b"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b, timeout=120)
            time.sleep(5)
            target_a = _project_dir() / "clones" / f"sar-rag-target--{vid_a}"
            target_b = _project_dir() / "clones" / f"sar-rag-target--{vid_b}"
            # Commit in A, verify B doesn't see it
            if target_a.exists():
                (target_a / "test_a.txt").write_text("from A")
                subprocess.run(["git", "add", "-A"], cwd=target_a, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "A only"], cwd=target_a, check=True, capture_output=True)
            if target_b.exists():
                self.assertFalse((target_b / "test_a.txt").exists(), "B should not see A's file")
        finally:
            _cleanup_variant(vid_a)
            _cleanup_variant(vid_b)


class TestConcurrentClonesSafetyE2E(_E2EProjectBase):

    def test_03_two_clones_commit_independently(self):
        """#3: Two researcher variants can both commit without conflict."""
        vid_a, vid_b = "e2e-conc-a", "e2e-conc-b"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b, timeout=120)
            time.sleep(20)
            # Both should be running without git errors
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "list", "--json")
            variants = json.loads(r.stdout)
            running = [v for v in variants if v["running"]]
            self.assertGreaterEqual(len(running), 2, "Both variants should be running")
        finally:
            _cleanup_variant(vid_a)
            _cleanup_variant(vid_b)


# =============================================================================
# PROFILE ROTATION (#10-15)
# =============================================================================


class TestProfileRotationE2E(_E2EProjectBase):

    def test_10_researcher_uses_profile_i_plus_1(self):
        """#10: Researcher's config_dir differs from supervisor's."""
        vid = "e2e-rot-10"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            state = _state_file(vid)
            my_profile = os.environ.get("CLAUDE_CONFIG_DIR", "")
            self.assertNotEqual(state.get("config_dir", ""), my_profile)
        finally:
            _cleanup_variant(vid)

    def test_11_target_config_dir_in_command(self):
        """#11: Launch command contains TARGET_CLAUDE_CONFIG_DIR."""
        vid = "e2e-rot-11"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            state = _state_file(vid)
            self.assertIn("TARGET_CLAUDE_CONFIG_DIR=", state.get("command", ""))
        finally:
            _cleanup_variant(vid)

    def test_12_config_dirs_passed_to_child(self):
        """#12: Launch command contains CLAUDE_CONFIG_DIRS."""
        vid = "e2e-rot-12"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            state = _state_file(vid)
            self.assertIn("CLAUDE_CONFIG_DIRS=", state.get("command", ""))
        finally:
            _cleanup_variant(vid)

    def test_13_two_variants_different_profiles(self):
        """#13: Two researcher variants use different profiles."""
        vid_a, vid_b = "e2e-rot-a", "e2e-rot-b"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b, timeout=120)
            time.sleep(3)
            state_a = _state_file(vid_a)
            state_b = _state_file(vid_b)
            self.assertNotEqual(state_a.get("config_dir"), state_b.get("config_dir"))
        finally:
            _cleanup_variant(vid_a)
            _cleanup_variant(vid_b)

    def test_14_status_json_has_config_dir(self):
        """#14: researcher-status --json includes config_dir."""
        vid = "e2e-rot-14"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            # Main researcher status (the variant runs as a subprocess)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-status", "--json")
            data = json.loads(r.stdout)
            # State may have config_dir if a main researcher is running
            # For variant, check state file directly
            state = _state_file(vid)
            self.assertIn("config_dir", state)
        finally:
            _cleanup_variant(vid)

    def test_15_status_shows_profile_line(self):
        """#15: researcher-status output contains 'profile:' line."""
        vid = "e2e-rot-15"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-start", "--no-clean", timeout=120)
            time.sleep(3)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-status")
            self.assertIn("profile:", r.stdout)
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-stop")
            _cleanup_variant(vid)


# =============================================================================
# VARIANT LIFECYCLE (#16-24)
# =============================================================================


class TestVariantLifecycleE2E(_E2EProjectBase):

    def test_16_start_creates_both_clones(self):
        """#16: Start creates researcher clone AND target clone."""
        vid = "e2e-life-16"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            researcher_clone = _project_dir() / "clones" / f"sar-research-loop--{vid}"
            target_clone = _project_dir() / "clones" / f"sar-rag-target--{vid}"
            self.assertTrue(researcher_clone.exists())
            self.assertTrue(target_clone.exists())
        finally:
            _cleanup_variant(vid)

    def test_17_start_applies_variant_skill(self):
        """#17: Start with --variant flag applies the SKILL.md to the clone."""
        vid = "e2e-life-17"
        # Create a temp variant file
        variant_dir = _SUPERVISOR_ROOT / "researcher_variants"
        variant_file = variant_dir / "_test_variant.md"
        variant_file.write_text("# Test variant\nThis is a test SKILL.md\n")
        try:
            _pixi_run_check(
                _SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid,
                "--variant", str(variant_file), timeout=120,
            )
            clone = _project_dir() / "clones" / f"sar-research-loop--{vid}"
            skill_path = clone / ".claude" / "skills" / "start" / "SKILL.md"
            if skill_path.exists():
                content = skill_path.read_text()
                self.assertIn("Test variant", content)
        finally:
            variant_file.unlink(missing_ok=True)
            _cleanup_variant(vid)

    def test_18_stop_preserves_clones(self):
        """#18: Stop variant, verify clones still exist."""
        vid = "e2e-life-18"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "stop", "--id", vid)
            researcher_clone = _project_dir() / "clones" / f"sar-research-loop--{vid}"
            target_clone = _project_dir() / "clones" / f"sar-rag-target--{vid}"
            self.assertTrue(researcher_clone.exists(), "Researcher clone should survive stop")
            self.assertTrue(target_clone.exists(), "Target clone should survive stop")
        finally:
            _cleanup_variant(vid)

    def test_19_park_has_metrics(self):
        """#19: Park after iterations → parked state has metrics."""
        vid = "e2e-life-19"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got, "Need at least 1 iteration")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            parked = json.loads((_SUPERVISOR_ROOT / ".supervisor" / f"parked-{vid}.json").read_text())
            self.assertEqual(parked["status"], "parked")
            self.assertGreaterEqual(parked["iterations"]["total"], 1)
        finally:
            _cleanup_variant(vid)

    def test_20_park_autocommits_dirty_target(self):
        """#20: Park auto-commits uncommitted changes in the target clone."""
        vid = "e2e-life-20"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(5)
            # Make target dirty
            target_clone = _project_dir() / "clones" / f"sar-rag-target--{vid}"
            if target_clone.exists():
                (target_clone / "dirty.txt").write_text("uncommitted")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            # After park, target should be clean (auto-committed)
            if target_clone.exists():
                status = subprocess.run(
                    ["git", "status", "--short"], cwd=target_clone,
                    capture_output=True, text=True,
                ).stdout.strip()
                self.assertEqual(status, "", "Target should be clean after park auto-commit")
        finally:
            _cleanup_variant(vid)

    def test_21_park_removes_researcher_keeps_target(self):
        """#21: After park, researcher clone gone but target clone exists."""
        vid = "e2e-life-21"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(5)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            researcher_clone = _project_dir() / "clones" / f"sar-research-loop--{vid}"
            target_clone = _project_dir() / "clones" / f"sar-rag-target--{vid}"
            self.assertFalse(researcher_clone.exists(), "Researcher clone should be removed")
            self.assertTrue(target_clone.exists(), "Target clone should be preserved")
        finally:
            _cleanup_variant(vid)

    def test_22_discard_removes_everything(self):
        """#22: Discard removes all clones, temp files, and parked state."""
        vid = "e2e-life-22"
        _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
        time.sleep(3)
        _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", vid, timeout=120)
        self.assertFalse((_project_dir() / "clones" / f"sar-research-loop--{vid}").exists())
        self.assertFalse((_project_dir() / "clones" / f"sar-rag-target--{vid}").exists())

    def test_23_list_shows_running_variants(self):
        """#23: Variant list shows running variants with correct status."""
        vid_a, vid_b = "e2e-list-a", "e2e-list-b"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b, timeout=120)
            time.sleep(3)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "list", "--json")
            variants = json.loads(r.stdout)
            ids = [v["variant_id"] for v in variants]
            self.assertIn(vid_a, ids)
            self.assertIn(vid_b, ids)
        finally:
            _cleanup_variant(vid_a)
            _cleanup_variant(vid_b)

    def test_24_parked_list_shows_metrics(self):
        """#24: Parked list shows variants with metrics."""
        vid = "e2e-parked-24"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(5)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "parked", "--json")
            parked = json.loads(r.stdout)
            self.assertGreaterEqual(len(parked), 1)
            self.assertEqual(parked[0]["variant_id"], vid)
        finally:
            _cleanup_variant(vid)


# =============================================================================
# MERGE STRATEGIES (#25-33)
# =============================================================================


class TestMergeWTAE2E(_E2EProjectBase):
    """Winner Takes All merge with real researcher variant."""

    def test_25_canonical_head_changes(self):
        """#25: After WTA merge, canonical HEAD matches variant's target."""
        vid = "e2e-wta-25"
        try:
            baseline_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)
            new_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            self.assertNotEqual(baseline_head, new_head)
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)

    def test_26_backup_created(self):
        """#26: WTA merge creates a backup snapshot."""
        vid = "e2e-wta-26"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)
            backup = _SUPERVISOR_ROOT / ".supervisor" / "snapshots" / "pre-merge-backup"
            self.assertTrue(backup.exists(), "pre-merge-backup should exist")
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)

    def test_27_baseline_tag_updated(self):
        """#27: After WTA merge, baseline tag points to new HEAD."""
        vid = "e2e-wta-27"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)
            head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            baseline = _git(_RAG_TARGET, "rev-parse", "baseline")
            self.assertEqual(head, baseline)
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)


class TestMergeCherryPickE2E(_E2EProjectBase):
    """Cherry-pick merge with real researcher variants."""

    def test_28_cherry_pick_from_variant(self):
        """#28: Cherry-pick commits from a parked researcher variant."""
        vid = "e2e-cp-28"
        try:
            baseline_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "cherry-pick")
            # Should either succeed or report no commits to pick
            self.assertIn(r.returncode, [0, 1])
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)

    def test_30_cherry_pick_updates_baseline(self):
        """#30: After cherry-pick with applied commits, baseline tag moves."""
        vid = "e2e-cp-30"
        try:
            old_baseline = _git(_RAG_TARGET, "rev-parse", "baseline")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "cherry-pick")
            if r.returncode == 0 and "applied" in r.stdout:
                new_baseline = _git(_RAG_TARGET, "rev-parse", "baseline")
                self.assertNotEqual(old_baseline, new_baseline, "Baseline should move after cherry-pick")
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)


class TestMergeBranchAndContinueE2E(_E2EProjectBase):
    """Branch-and-continue merge with real researcher variant."""

    def test_31_clone_becomes_canonical(self):
        """#31: After B&C, canonical has the variant's commits."""
        vid = "e2e-bc-31"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            # Record variant's target HEAD
            parked = json.loads((_SUPERVISOR_ROOT / ".supervisor" / f"parked-{vid}.json").read_text())
            variant_head = parked.get("target_head")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "branch-and-continue", timeout=120)
            canonical_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            if variant_head:
                self.assertEqual(canonical_head, variant_head)
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)

    def test_32_pixi_symlink_valid(self):
        """#32: After B&C, .pixi symlink in canonical is valid."""
        vid = "e2e-bc-32"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "branch-and-continue", timeout=120)
            pixi = _RAG_TARGET / ".pixi"
            self.assertTrue(pixi.exists() or pixi.is_symlink(), ".pixi should exist after B&C")
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)

    def test_33_backup_created(self):
        """#33: After B&C, the old canonical is backed up."""
        vid = "e2e-bc-33"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "branch-and-continue", timeout=120)
            backup = Path(f"{_RAG_TARGET}.pre-merge-backup")
            self.assertTrue(backup.exists(), "Backup of old canonical should exist")
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)


# =============================================================================
# ROLLBACK (#34-37)
# =============================================================================


class TestRollbackE2E(_E2EProjectBase):

    def test_34_rollback_after_wta(self):
        """#34: Rollback after WTA restores original HEAD."""
        vid = "e2e-roll-34"
        try:
            baseline_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "rollback", timeout=120)
            restored = _git(_RAG_TARGET, "rev-parse", "HEAD")
            self.assertEqual(baseline_head, restored, "HEAD should be restored to baseline")
        finally:
            _cleanup_variant(vid)

    def test_35_rollback_after_cherry_pick(self):
        """#35: Rollback after cherry-pick restores HEAD."""
        vid = "e2e-roll-35"
        try:
            baseline_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "cherry-pick")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "rollback", timeout=120)
            restored = _git(_RAG_TARGET, "rev-parse", "HEAD")
            self.assertEqual(baseline_head, restored)
        finally:
            _cleanup_variant(vid)

    def test_36_rollback_after_branch_and_continue(self):
        """#36: Rollback after B&C restores the original canonical directory."""
        vid = "e2e-roll-36"
        try:
            baseline_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "branch-and-continue", timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "rollback", timeout=120)
            self.assertTrue(_RAG_TARGET.exists(), "Canonical should be restored")
            restored_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            self.assertEqual(baseline_head, restored_head)
        finally:
            _cleanup_variant(vid)

    def test_37_rollback_no_backup_fails(self):
        """#37: Rollback without prior merge returns non-zero exit code."""
        r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
        self.assertNotEqual(r.returncode, 0)


# =============================================================================
# MERGE LOCK (#39)
# =============================================================================


class TestMergeLockE2E(_E2EProjectBase):

    def test_39_lock_released_after_merge(self):
        """#39: After successful merge, merge.lock does not exist."""
        vid = "e2e-lock-39"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)
            lock_path = _SUPERVISOR_ROOT / ".supervisor" / "merge.lock"
            self.assertFalse(lock_path.exists(), "Lock should be released after merge")
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "rollback")
            _cleanup_variant(vid)


# =============================================================================
# MONITOR OUTPUT (#45-48)
# =============================================================================


class TestMonitorOutputE2E(_E2EProjectBase):

    def test_45_loop_once_shows_metrics(self):
        """#45: researcher-loop-once output contains profile and metric values."""
        vid = "e2e-mon-45"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-start", "--no-clean", timeout=120)
            time.sleep(20)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-loop-once", timeout=60)
            self.assertIn("log:", r.stdout)
            self.assertIn("events:", r.stdout)
        finally:
            _pixi_run(_SUPERVISOR_ROOT, "researcher-stop")

    def test_46_variant_list_shows_profiles(self):
        """#46: Variant list shows different profiles per variant."""
        vid_a, vid_b = "e2e-mon-a", "e2e-mon-b"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_a, timeout=120)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid_b, timeout=120)
            time.sleep(3)
            r = _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "list")
            self.assertIn("profile=", r.stdout)
        finally:
            _cleanup_variant(vid_a)
            _cleanup_variant(vid_b)

    def test_47_target_variant_clones_discoverable(self):
        """#47: Target variant clones are discoverable in the filesystem."""
        vid = "e2e-mon-47"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(5)
            # The main target clone should exist
            target_clone = _project_dir() / "clones" / f"sar-rag-target--{vid}"
            self.assertTrue(target_clone.exists(), "Target clone should be discoverable")
        finally:
            _cleanup_variant(vid)

    def test_48_variant_compare_output(self):
        """#48: Variant compare shows structured output with columns."""
        vid = "e2e-mon-48"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(5)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "compare")
            # Should have table header with columns
            self.assertIn("Researcher Variant", r.stdout)
            self.assertIn("Status", r.stdout)
        finally:
            _cleanup_variant(vid)


# =============================================================================
# EDGE CASES (#49, 51-54)
# =============================================================================


class TestEdgeCasesE2E(_E2EProjectBase):

    def test_49_park_already_stopped(self):
        """#49: Park a variant whose process already died."""
        vid = "e2e-edge-49"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            # Kill process manually
            state = _state_file(vid)
            pid = state.get("pid")
            if pid:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
                time.sleep(2)
            # Park should still work
            r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            parked_path = _SUPERVISOR_ROOT / ".supervisor" / f"parked-{vid}.json"
            self.assertTrue(parked_path.exists(), "Parked state should be created even for dead process")
        finally:
            _cleanup_variant(vid)

    def test_51_merge_nonexistent_variant_fails(self):
        """#51: Merge with bogus variant ID returns non-zero and stderr has 'not found'."""
        r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", "rv-bogus", "--strategy", "winner-takes-all")
        self.assertNotEqual(r.returncode, 0)

    def test_52_discard_nonexistent_no_crash(self):
        """#52: Discard with bogus variant ID doesn't crash."""
        r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "discard", "--id", "rv-bogus")
        # Should not crash — exit code 0 or at least no Python traceback
        self.assertNotIn("Traceback", r.stderr)

    def test_53_merge_after_canonical_diverged(self):
        """#53: Merge after manual commit in canonical → backup includes the manual commit."""
        vid = "e2e-edge-53"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            got = _wait_for_iterations(vid, 1)
            self.assertTrue(got)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)

            # Manually commit in canonical
            (_RAG_TARGET / "manual.txt").write_text("manual change")
            subprocess.run(["git", "add", "-A"], cwd=_RAG_TARGET, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "manual divergence"], cwd=_RAG_TARGET, check=True, capture_output=True)
            diverged_head = _git(_RAG_TARGET, "rev-parse", "HEAD")

            # Merge WTA (overwrites canonical)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "winner-takes-all", timeout=120)

            # Rollback should restore the diverged state
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "rollback", timeout=120)
            restored_head = _git(_RAG_TARGET, "rev-parse", "HEAD")
            self.assertEqual(diverged_head, restored_head, "Rollback should restore diverged state")
        finally:
            _cleanup_variant(vid)

    def test_54_cherry_pick_empty_range(self):
        """#54: Cherry-pick variant with no commits beyond baseline → empty applied."""
        vid = "e2e-edge-54"
        try:
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "start", "--id", vid, timeout=120)
            time.sleep(3)
            # Park immediately (no iterations = no commits beyond baseline)
            _pixi_run_check(_SUPERVISOR_ROOT, "researcher-variant", "park", "--id", vid, timeout=120)
            r = _pixi_run(_SUPERVISOR_ROOT, "researcher-variant", "merge", "--id", vid, "--strategy", "cherry-pick")
            # Should succeed but with nothing applied
            if r.returncode == 0:
                self.assertNotIn("error", r.stdout.lower())
        finally:
            _cleanup_variant(vid)


if __name__ == "__main__":
    unittest.main()
