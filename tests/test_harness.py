from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from supervisor_harness.cli import main
from supervisor_harness.config import RepoPaths, build_launch_spec
from supervisor_harness.stream_json import parse_stream_log
from supervisor_harness.supervisor import analyze_log, write_snapshot


FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Minimal config that mirrors harness.toml structure
MINIMAL_CONFIG = {
    "project": {"name": "test-project"},
    "supervised": {
        "repo": "../supervised-project",
        "default_prompt": "/my-skill",
        "skill_name": "my-skill",
        "agents": ["agent-a", "agent-b"],
        "config_dirs": ["~/.claude"],
    },
    "reports": {
        "primary": "{tmp}/primary-report.json",
        "metric": {
            "report": "primary",
            "field": "failed",
            "direction": "minimize",
        },
    },
    "log": {"path": "{tmp}/cc-test-project.log"},
    "revert": {"paths": ["src/", "tests/", "lib/"]},
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_paths(
    root: Path,
    workspace: Path,
    supervised_repo: Path,
    log_path: Path,
) -> RepoPaths:
    """Build a RepoPaths for testing with the generic config structure."""
    claude_dir = supervised_repo / ".claude"
    skill_name = "my-skill"
    agent_names = ("agent-a", "agent-b")
    state_dir = workspace / ".supervisor"
    report_primary = root / "primary-report.json"
    return RepoPaths(
        workspace_root=workspace,
        supervised_repo=supervised_repo,
        claude_dir=claude_dir,
        skill_name=skill_name,
        agent_names=agent_names,
        skill_path=claude_dir / "skills" / skill_name / "SKILL.md",
        agent_paths={
            "agent-a": claude_dir / "agents" / "agent-a.md",
            "agent-b": claude_dir / "agents" / "agent-b.md",
        },
        log_path=log_path,
        state_dir=state_dir,
        snapshots_dir=state_dir / "snapshots",
        pid_path=state_dir / f"{skill_name}.pid",
        state_path=state_dir / f"{skill_name}-state.json",
        latest_snapshot_path=state_dir / "latest_snapshot.json",
        history_path=state_dir / "history.jsonl",
        report_paths=(report_primary,),
        report_map={"primary": report_primary},
        config_dirs=(Path("~/.claude").expanduser(),),
        config=MINIMAL_CONFIG,
        project_id="test-harness",
        project_dir=root / "project",
        clone_dir=root / "clones",
    )


class HarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.supervised_repo = self.root / "supervised-project"
        self.workspace = self.root / "supervisor-harness"
        self.workspace.mkdir()

        claude_dir = self.supervised_repo / ".claude"
        _write(claude_dir / "skills" / "my-skill" / "SKILL.md", "# skill\n")
        for agent in ("agent-a", "agent-b"):
            _write(claude_dir / "agents" / f"{agent}.md", f"# {agent}\n")

        self.log_path = self.root / "sample_stream.jsonl"
        self.log_path.write_text(
            (FIXTURE_DIR / "sample_stream.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        self.paths = _make_paths(
            self.root, self.workspace, self.supervised_repo, self.log_path
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_parse_stream_log_extracts_scope_and_tool_uses(self) -> None:
        transcript = parse_stream_log(self.log_path)

        self.assertEqual(transcript.event_count, 9)
        self.assertEqual(transcript.session_ids, ["session-1"])
        self.assertEqual(transcript.counts_by_tool(scope="orchestrator")["Task"], 3)
        self.assertEqual(transcript.counts_by_tool(scope="orchestrator")["TodoWrite"], 1)
        self.assertEqual(transcript.counts_by_tool(scope="subagent")["Read"], 1)

    def test_analyze_log_reports_observations(self) -> None:
        _write(
            self.root / "primary-report.json",
            json.dumps({"status": "failures_found", "failed": 12, "passed": 120}),
        )

        report = analyze_log(self.paths).to_dict()

        # Dispatches may or may not match agent names (fixture uses old names)
        self.assertIn("dispatches", report)
        self.assertIn("report_summaries", report)
        self.assertEqual(report["report_summaries"]["primary-report.json"]["failed"], 12)
        self.assertIn("my-skill", report["prompt_assets"])
        self.assertIn("repo_status", report)

    def test_build_launch_spec_matches_expected_command_shape(self) -> None:
        launch_spec = build_launch_spec(
            self.paths,
            prompt="/my-skill zone=src/module",
            claude_bin="/opt/bin/claude",
            pixi_bin="/opt/bin/pixi",
        )

        self.assertIn("ENABLE_LSP_TOOL=1", launch_spec.command)
        self.assertIn("CLAUDE_CONFIG_DIR=", launch_spec.command)
        self.assertIn("/opt/bin/claude", launch_spec.command)
        self.assertIn("/opt/bin/pixi shell-hook -e dev", launch_spec.command)
        self.assertIn("-p '/my-skill zone=src/module'", launch_spec.command)

    def test_build_launch_spec_uses_config_default_prompt(self) -> None:
        launch_spec = build_launch_spec(
            self.paths,
            claude_bin="/opt/bin/claude",
            pixi_bin="/opt/bin/pixi",
        )
        self.assertIn("-p /my-skill", launch_spec.command)
        self.assertEqual(launch_spec.prompt, "/my-skill")

    def test_write_snapshot_archives_current_state(self) -> None:
        _write(self.root / "primary-report.json", json.dumps({"status": "failures_found", "failed": 9}))

        snapshot_dir = write_snapshot(self.paths, label="manual")

        self.assertTrue((snapshot_dir / "snapshot.json").exists())
        self.assertTrue((snapshot_dir / "artifacts" / self.log_path.name).exists())
        self.assertTrue((snapshot_dir / "artifacts" / "primary-report.json").exists())
        self.assertTrue((snapshot_dir / "prompt-assets" / "my-skill" / "SKILL.md").exists())
        self.assertTrue(self.paths.latest_snapshot_path.exists())
        history_lines = self.paths.history_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(history_lines), 1)
        history_entry = json.loads(history_lines[0])
        self.assertIn(str(snapshot_dir), history_entry["path"])
        self.assertEqual(history_entry["primary_metric"], 9)

    def test_loop_once_starts_and_archives(self) -> None:
        class FakeReport:
            def to_dict(self) -> dict:
                return {
                    "log_path": str(self.log_path),
                    "session_ids": ["session-1"],
                    "event_count": 1,
                    "parse_error_count": 0,
                    "tool_counts": {"orchestrator": {}, "subagent": {}},
                    "dispatches": [],
                    "latest_text": "Iteration 1: Phase T",
                    "latest_thinking": None,
                    "report_summaries": {
                        "primary-report.json": {"status": "failures_found", "failed": 7}
                    },
                    "prompt_assets": {},
                    "repo_status": {"branch": "main", "head": "abc123", "status_lines": []},
                }

            def __init__(self, log_path: Path) -> None:
                self.log_path = log_path

        launch_spec = build_launch_spec(
            self.paths,
            prompt="/my-skill",
            claude_bin="/opt/bin/claude",
            pixi_bin="/opt/bin/pixi",
        )

        fake_config_dir = self.root / ".claude-test"
        fake_config_dir.mkdir(exist_ok=True)

        with (
            patch.dict(os.environ, {"CLAUDE_CONFIG_DIRS": str(fake_config_dir)}),
            patch("supervisor_harness.cli.read_pid", return_value=None),
            patch("supervisor_harness.cli.process_running", return_value=False),
            patch(
                "supervisor_harness.cli.start_run",
                return_value=(launch_spec, 4321),
            ) as start_run_mock,
            patch(
                "supervisor_harness.cli.analyze_log",
                return_value=FakeReport(self.paths.log_path),
            ),
            patch(
                "supervisor_harness.cli.write_snapshot",
                return_value=self.paths.snapshots_dir / "example",
            ) as snapshot_mock,
            patch("builtins.print") as print_mock,
        ):
            exit_code = main(
                [
                    "--workspace-root",
                    str(self.workspace),
                    "--supervised-repo",
                    str(self.supervised_repo),
                    "--log-path",
                    str(self.log_path),
                    "loop",
                    "--once",
                    "--prompt",
                    "/my-skill",
                ]
            )

        self.assertEqual(exit_code, 0)
        start_run_mock.assert_called_once()
        snapshot_mock.assert_called_once()
        rendered = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("started pid 4321", rendered)
        self.assertIn("snapshot:", rendered)

    def test_watch_status_once_waits_first_and_logs_check_request(self) -> None:
        with (
            patch("supervisor_harness.cli.RepoPaths.discover", return_value=self.paths),
            patch("supervisor_harness.cli.read_pid", return_value=4321),
            patch("supervisor_harness.cli.process_running", return_value=True),
            patch("supervisor_harness.cli.time.sleep") as sleep_mock,
            patch("builtins.print") as print_mock,
        ):
            exit_code = main(
                [
                    "watch-status",
                    "--interval-seconds",
                    "30",
                    "--once",
                ]
            )

        self.assertEqual(exit_code, 0)
        sleep_mock.assert_called_once_with(30.0)
        status_log = self.paths.state_dir / "status.jsonl"
        self.assertTrue(status_log.exists())
        payload = json.loads(status_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["pid"], 4321)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["hook"]["action"], "check_loop_status")
        rendered = "\n".join(" ".join(str(arg) for arg in call.args) for call in print_mock.call_args_list)
        self.assertIn("action=check_loop_status", rendered)
        self.assertIn("events=9", rendered)


if __name__ == "__main__":
    unittest.main()
