from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any

from harness_core.checkpoint import (
    _sha1,
    capture_code_state,
    restore_code_state,
    resolve_snapshot as _resolve_snapshot,
    DEFAULT_REVERT_PATHS,
)
from harness_core.git_utils import git_command, git_status, commit_claude_changes
from harness_core.metrics import report_summary, extract_metric

from .config import LaunchSpec, RepoPaths, build_launch_spec, load_state, save_state
from .stream_json import ToolUse, parse_stream_log


@dataclass(slots=True, frozen=True)
class Dispatch:
    line_no: int
    tool_name: str
    agent_kind: str | None
    raw_input: dict[str, Any]


@dataclass(slots=True)
class AnalysisReport:
    log_path: Path
    session_ids: list[str]
    event_count: int
    parse_error_count: int
    tool_counts: dict[str, dict[str, int]]
    dispatches: list[Dispatch] = field(default_factory=list)
    latest_text: str | None = None
    latest_thinking: str | None = None
    report_summaries: dict[str, Any] = field(default_factory=dict)
    prompt_assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    repo_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_path": str(self.log_path),
            "session_ids": self.session_ids,
            "event_count": self.event_count,
            "parse_error_count": self.parse_error_count,
            "tool_counts": self.tool_counts,
            "dispatches": [
                {
                    "line_no": dispatch.line_no,
                    "tool_name": dispatch.tool_name,
                    "agent_kind": dispatch.agent_kind,
                }
                for dispatch in self.dispatches
            ],
            "latest_text": self.latest_text,
            "latest_thinking": self.latest_thinking,
            "report_summaries": self.report_summaries,
            "prompt_assets": self.prompt_assets,
            "repo_status": self.repo_status,
        }


def _prompt_assets(paths: RepoPaths) -> dict[str, dict[str, Any]]:
    assets: dict[str, Path] = {paths.skill_name: paths.skill_path}
    assets.update(paths.agent_paths)
    summary: dict[str, dict[str, Any]] = {}
    for name, path in assets.items():
        if not path.exists():
            continue
        stat = path.stat()
        summary[name] = {
            "path": str(path),
            "sha1": _sha1(path),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size_bytes": stat.st_size,
        }
    return summary


def _tool_input_text(tool_use: ToolUse) -> str:
    return json.dumps(tool_use.input, ensure_ascii=False, sort_keys=True).lower()


def _guess_agent_kind(tool_use: ToolUse, agent_names: tuple[str, ...]) -> str | None:
    haystack = _tool_input_text(tool_use)
    for name in agent_names:
        if name in haystack:
            return name
    return None


def _is_dispatch(tool_use: ToolUse) -> bool:
    return tool_use.name in {"Task", "Agent"}


def _load_json_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"parse_error": True}


def _collect_report_summaries(paths: RepoPaths) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for report_path in paths.report_paths:
        if report_path.suffix != ".json":
            summaries[report_path.name] = (
                {
                    "exists": True,
                    "size_bytes": report_path.stat().st_size,
                }
                if report_path.exists()
                else None
            )
            continue
        summaries[report_path.name] = report_summary(report_path)
    return summaries


def extract_primary_metric(
    paths: RepoPaths, report_summaries: dict[str, Any]
) -> Any:
    """Extract the primary metric value from report summaries using config."""
    metric_cfg = paths.config.get("reports", {}).get("metric", {})
    report_key = metric_cfg.get("report", "primary")
    metric_field = metric_cfg.get("field", "failed")

    report_path = paths.report_map.get(report_key)
    if report_path is None:
        return None

    report_data = report_summaries.get(report_path.name)
    if not isinstance(report_data, dict):
        return None

    return report_data.get(metric_field)


def _collect_dispatches(paths: RepoPaths) -> list[Dispatch]:
    transcript = parse_stream_log(paths.log_path)
    dispatches: list[Dispatch] = []
    for tool_use in transcript.tool_uses:
        if tool_use.scope != "orchestrator" or not _is_dispatch(tool_use):
            continue
        dispatches.append(
            Dispatch(
                line_no=tool_use.line_no,
                tool_name=tool_use.name,
                agent_kind=_guess_agent_kind(tool_use, paths.agent_names),
                raw_input=tool_use.input,
            )
        )
    return dispatches


def analyze_log(paths: RepoPaths) -> AnalysisReport:
    transcript = parse_stream_log(paths.log_path)
    grouped_tool_counts: dict[str, dict[str, int]] = defaultdict(dict)
    grouped_tool_counts["orchestrator"] = transcript.counts_by_tool(scope="orchestrator")
    grouped_tool_counts["subagent"] = transcript.counts_by_tool(scope="subagent")
    latest_thinking = None
    for block in reversed(transcript.text_blocks):
        if block.scope == "orchestrator" and block.block_type == "thinking":
            latest_thinking = block.text
            break
    return AnalysisReport(
        log_path=paths.log_path,
        session_ids=transcript.session_ids,
        event_count=transcript.event_count,
        parse_error_count=len(transcript.parse_errors),
        tool_counts=dict(grouped_tool_counts),
        dispatches=_collect_dispatches(paths),
        latest_text=transcript.latest_text(scope="orchestrator"),
        latest_thinking=latest_thinking,
        report_summaries=_collect_report_summaries(paths),
        prompt_assets=_prompt_assets(paths),
        repo_status=git_status(paths.supervised_repo),
    )


def _copy_if_exists(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(source, target)
    return True


def _snapshot_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def resolve_snapshot(paths: RepoPaths, identifier: str) -> Path:
    """Resolve a snapshot identifier to a directory path."""
    metric_cfg = paths.config.get("reports", {}).get("metric", {})
    direction = metric_cfg.get("direction", "minimize")
    return _resolve_snapshot(
        snapshots_dir=paths.snapshots_dir,
        history_path=paths.history_path,
        identifier=identifier,
        direction=direction,
    )


def restore_code_state_for_paths(paths: RepoPaths, snapshot_dir: Path) -> dict[str, Any]:
    """Restore the supervised repo to the state captured in a snapshot."""
    return restore_code_state(paths.supervised_repo, snapshot_dir)


def safe_revert(
    paths: RepoPaths,
    *,
    label: str | None = None,
    revert_paths: tuple[str, ...] | None = None,
    full: bool = False,
) -> Path:
    """Checkpoint current state, then revert production code in the supervised repo."""
    if revert_paths is None:
        config_revert = paths.config.get("revert", {}).get("paths")
        revert_paths = tuple(config_revert) if config_revert else DEFAULT_REVERT_PATHS

    # Commit .claude/ changes first so they survive the revert
    commit_claude_changes(paths.supervised_repo)

    snapshot_dir = write_snapshot(paths, label=label or "pre-revert")

    if full:
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=paths.supervised_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=paths.supervised_repo,
            check=True,
            capture_output=True,
        )
    else:
        for rp in revert_paths:
            subprocess.run(
                ["git", "checkout", "--", rp],
                cwd=paths.supervised_repo,
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "clean", "-fd", "--", rp],
                cwd=paths.supervised_repo,
                check=False,
                capture_output=True,
            )

    return snapshot_dir


def write_snapshot(paths: RepoPaths, *, label: str | None = None) -> Path:
    snapshot_id = _snapshot_id()
    suffix = f"-{label}" if label else ""
    snapshot_dir = paths.snapshots_dir / f"{snapshot_id}{suffix}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    report = analyze_log(paths).to_dict()
    state = load_state(paths)
    pid = read_pid(paths)
    running = bool(pid and process_running(pid))
    payload = {
        "snapshot_id": snapshot_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_state": {
            "pid": pid,
            "running": running,
            "state": state,
        },
        "analysis": report,
    }

    copied_files: list[str] = []
    if _copy_if_exists(paths.log_path, snapshot_dir / "artifacts" / paths.log_path.name):
        copied_files.append(paths.log_path.name)
    for report_path in paths.report_paths:
        if _copy_if_exists(report_path, snapshot_dir / "artifacts" / report_path.name):
            copied_files.append(report_path.name)
    _copy_if_exists(
        paths.skill_path,
        snapshot_dir / "prompt-assets" / paths.skill_name / "SKILL.md",
    )
    for agent_name, agent_path in paths.agent_paths.items():
        _copy_if_exists(
            agent_path,
            snapshot_dir / "prompt-assets" / "agents" / f"{agent_name}.md",
        )

    code_state = capture_code_state(paths.supervised_repo, snapshot_dir)
    payload["code_state"] = code_state
    payload["copied_files"] = copied_files
    snapshot_json = snapshot_dir / "snapshot.json"
    snapshot_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.latest_snapshot_path.write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    primary_metric = extract_primary_metric(paths, report["report_summaries"])
    history_entry = {
        "snapshot_id": snapshot_id,
        "label": label,
        "path": str(snapshot_dir),
        "created_at": payload["created_at"],
        "pid": pid,
        "running": running,
        "primary_metric": primary_metric,
        "session_id": report["session_ids"][-1] if report["session_ids"] else None,
    }
    with paths.history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_entry) + "\n")
    return snapshot_dir


def start_run(
    paths: RepoPaths,
    *,
    prompt: str,
    clean_first: bool,
    claude_bin: str | None = None,
    pixi_bin: str | None = None,
    config_dir: Path | None = None,
) -> tuple[LaunchSpec, int]:
    if clean_first:
        clean_temp_files(paths, include_log=True)

    # Reset Haiku offset for fresh log analysis
    haiku_offset = paths.state_dir / "haiku-offset"
    if haiku_offset.exists():
        haiku_offset.unlink()

    launch_spec = build_launch_spec(
        paths,
        prompt=prompt,
        claude_bin=claude_bin,
        pixi_bin=pixi_bin,
        config_dir=config_dir,
    )
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    launch_spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    with launch_spec.log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", launch_spec.command],
            cwd=launch_spec.cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    save_state(paths, pid=process.pid, launch_spec=launch_spec)
    return launch_spec, process.pid


def read_pid(paths: RepoPaths) -> int | None:
    if not paths.pid_path.exists():
        return None
    try:
        return int(paths.pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def process_running(pid: int) -> bool:
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return False
    state = probe.stdout.strip()
    if not state:
        return False
    return "Z" not in state


def stop_run(paths: RepoPaths, *, force: bool = False) -> bool:
    pid = read_pid(paths)
    if pid is None:
        return False
    if not process_running(pid):
        cleanup_state(paths)
        return False
    sig = signal.SIGKILL if force else signal.SIGTERM
    os.kill(pid, sig)
    deadline = time.time() + (2 if force else 10)
    while time.time() < deadline:
        if not process_running(pid):
            cleanup_state(paths)
            return True
        time.sleep(0.1)
    if not force:
        return stop_run(paths, force=True)
    cleanup_state(paths)
    return True


def cleanup_state(paths: RepoPaths) -> None:
    for path in (paths.pid_path, paths.state_path):
        if path.exists():
            path.unlink()


def clean_temp_files(paths: RepoPaths, *, include_log: bool) -> list[Path]:
    removed: list[Path] = []
    for path in paths.clean_targets(include_log=include_log):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def restart_run(
    paths: RepoPaths,
    *,
    prompt: str | None = None,
    claude_bin: str | None = None,
    pixi_bin: str | None = None,
    config_dir: Path | None = None,
) -> tuple[LaunchSpec, int]:
    prior_state = load_state(paths) or {}
    stop_run(paths)
    default_prompt = paths.config.get("supervised", {}).get(
        "default_prompt", "/default"
    )
    chosen_prompt = prompt or str(prior_state.get("prompt") or default_prompt)
    return start_run(
        paths,
        prompt=chosen_prompt,
        clean_first=True,
        claude_bin=claude_bin,
        pixi_bin=pixi_bin,
        config_dir=config_dir,
    )
