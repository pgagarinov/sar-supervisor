from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from supervisor_harness.config import (
    RepoPaths,
    build_launch_spec,
    my_profile_index,
    next_profile,
    save_state,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_paths(root: Path, config_dirs: tuple[Path, ...]) -> RepoPaths:
    """Build a minimal RepoPaths for profile rotation tests."""
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    supervised = root / "supervised"
    claude_dir = supervised / ".claude"
    _write(claude_dir / "skills" / "my-skill" / "SKILL.md", "# skill\n")
    state_dir = workspace / ".supervisor"
    report = root / "report.json"
    log_path = root / "stream.jsonl"
    return RepoPaths(
        workspace_root=workspace,
        supervised_repo=supervised,
        claude_dir=claude_dir,
        skill_name="my-skill",
        agent_names=(),
        skill_path=claude_dir / "skills" / "my-skill" / "SKILL.md",
        agent_paths={},
        log_path=log_path,
        state_dir=state_dir,
        snapshots_dir=state_dir / "snapshots",
        pid_path=state_dir / "my-skill.pid",
        state_path=state_dir / "my-skill-state.json",
        latest_snapshot_path=state_dir / "latest_snapshot.json",
        history_path=state_dir / "history.jsonl",
        report_paths=(report,),
        report_map={"primary": report},
        config_dirs=config_dirs,
        config={
            "supervised": {"default_prompt": "/my-skill", "config_dirs": []},
        },
        project_id="test-profile",
        project_dir=root / "project",
        clone_dir=root / "clones",
    )


class ProfileRotationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile_a = self.root / ".claude-a"
        self.profile_b = self.root / ".claude-b"
        self.profile_c = self.root / ".claude-c"
        for p in (self.profile_a, self.profile_b, self.profile_c):
            p.mkdir()
        self.three_profiles = (self.profile_a, self.profile_b, self.profile_c)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_my_profile_index_finds_correct_index(self) -> None:
        """my_profile_index returns the matching index from CLAUDE_CONFIG_DIR."""
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile_b)}):
            idx = my_profile_index(self.three_profiles)
        self.assertEqual(idx, 1)

    def test_next_profile_returns_correct_offset(self) -> None:
        """next_profile(offset=1) returns the profile after the current one."""
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile_a)}):
            result = next_profile(self.three_profiles, offset=1)
        self.assertEqual(result, self.profile_b)

    def test_next_profile_wraps_around(self) -> None:
        """next_profile wraps around when offset goes past the end."""
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile_c)}):
            result = next_profile(self.three_profiles, offset=1)
        self.assertEqual(result, self.profile_a)

    def test_single_profile_no_rotation(self) -> None:
        """With a single profile, next_profile always returns it regardless of offset."""
        single = (self.profile_a,)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile_a)}):
            result = next_profile(single, offset=1)
        self.assertEqual(result, self.profile_a)

    def test_build_launch_spec_sets_config_dir_to_next_profile(self) -> None:
        """build_launch_spec sets CLAUDE_CONFIG_DIR to profile[I+1]."""
        paths = _make_paths(self.root, self.three_profiles)
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.profile_a),
            "CLAUDE_CONFIG_DIRS": ":".join(str(p) for p in self.three_profiles),
        }):
            spec = build_launch_spec(
                paths, claude_bin="/usr/bin/claude", pixi_bin="/usr/bin/pixi"
            )
        # Child should get profile_b (index 0 + offset 1)
        self.assertIn(f"CLAUDE_CONFIG_DIR={str(self.profile_b)}", spec.command)

    def test_build_launch_spec_passes_target_config_dir_at_offset_2(self) -> None:
        """build_launch_spec passes TARGET_CLAUDE_CONFIG_DIR at offset=2."""
        paths = _make_paths(self.root, self.three_profiles)
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.profile_a),
            "CLAUDE_CONFIG_DIRS": ":".join(str(p) for p in self.three_profiles),
        }):
            spec = build_launch_spec(
                paths, claude_bin="/usr/bin/claude", pixi_bin="/usr/bin/pixi"
            )
        # Target should get profile_c (index 0 + offset 2)
        self.assertIn(f"TARGET_CLAUDE_CONFIG_DIR={str(self.profile_c)}", spec.command)

    def test_build_launch_spec_passes_config_dirs_to_child(self) -> None:
        """build_launch_spec includes CLAUDE_CONFIG_DIRS in the command for the child."""
        paths = _make_paths(self.root, self.three_profiles)
        dirs_str = ":".join(str(p) for p in self.three_profiles)
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.profile_a),
            "CLAUDE_CONFIG_DIRS": dirs_str,
        }):
            spec = build_launch_spec(
                paths, claude_bin="/usr/bin/claude", pixi_bin="/usr/bin/pixi"
            )
        self.assertIn(f"CLAUDE_CONFIG_DIRS={dirs_str}", spec.command)

    def test_per_variant_rotation_with_offset(self) -> None:
        """Per-variant rotation uses offset=1+variant_index to spread profiles."""
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile_a)}):
            variant_0 = next_profile(self.three_profiles, offset=1 + 0)
            variant_1 = next_profile(self.three_profiles, offset=1 + 1)
        # variant 0 gets profile_b (0+1=1), variant 1 gets profile_c (0+2=2)
        self.assertEqual(variant_0, self.profile_b)
        self.assertEqual(variant_1, self.profile_c)

    def test_save_state_records_config_dir(self) -> None:
        """save_state persists the config_dir from the launch command."""
        paths = _make_paths(self.root, self.three_profiles)
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.profile_a),
            "CLAUDE_CONFIG_DIRS": ":".join(str(p) for p in self.three_profiles),
        }):
            spec = build_launch_spec(
                paths, claude_bin="/usr/bin/claude", pixi_bin="/usr/bin/pixi"
            )
            save_state(paths, pid=12345, launch_spec=spec)
        state = json.loads(paths.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["config_dir"], str(self.profile_b))

    def test_cmd_status_shows_profile_in_output(self) -> None:
        """_cmd_status prints the profile line when config_dir is in state."""
        paths = _make_paths(self.root, self.three_profiles)
        with patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.profile_a),
            "CLAUDE_CONFIG_DIRS": ":".join(str(p) for p in self.three_profiles),
        }):
            spec = build_launch_spec(
                paths, claude_bin="/usr/bin/claude", pixi_bin="/usr/bin/pixi"
            )
            save_state(paths, pid=99999, launch_spec=spec)

        from supervisor_harness.cli import _cmd_status
        import argparse

        args = argparse.Namespace(json=False)
        with (
            patch("supervisor_harness.cli._paths_from_args", return_value=paths),
            patch("supervisor_harness.cli.read_pid", return_value=99999),
            patch("supervisor_harness.cli.process_running", return_value=True),
            patch("builtins.print") as print_mock,
        ):
            _cmd_status(args)
        rendered = "\n".join(
            " ".join(str(a) for a in call.args) for call in print_mock.call_args_list
        )
        self.assertIn("profile:", rendered)
        self.assertIn(str(self.profile_b), rendered)


if __name__ == "__main__":
    unittest.main()
