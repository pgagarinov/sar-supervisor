from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


def load_harness_config(workspace: Path) -> dict[str, Any]:
    """Load and parse harness.toml from the workspace root."""
    config_path = workspace / "harness.toml"
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _resolve_path_template(template: str, project_name: str) -> str:
    """Replace {tmp} and {name} placeholders in path templates."""
    return template.replace("{tmp}", "/tmp").replace("{name}", project_name)


def my_profile_index(config_dirs: tuple[Path, ...]) -> int:
    """Find current profile's index in the list from CLAUDE_CONFIG_DIR env var."""
    current = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not current:
        return 0
    current_path = Path(current).expanduser().resolve()
    for i, d in enumerate(config_dirs):
        if d.expanduser().resolve() == current_path:
            return i
    return 0


def next_profile(config_dirs: tuple[Path, ...], *, offset: int = 1) -> Path:
    """Return profile at (my_index + offset) % len.

    Single-profile list always returns that profile (no rotation).
    """
    if len(config_dirs) <= 1:
        return config_dirs[0]
    base = my_profile_index(config_dirs)
    return config_dirs[(base + offset) % len(config_dirs)]


@dataclass(slots=True, frozen=True)
class RepoPaths:
    workspace_root: Path
    supervised_repo: Path
    claude_dir: Path
    skill_name: str
    agent_names: tuple[str, ...]
    skill_path: Path
    agent_paths: dict[str, Path]
    log_path: Path
    state_dir: Path
    snapshots_dir: Path
    pid_path: Path
    state_path: Path
    latest_snapshot_path: Path
    history_path: Path
    report_paths: tuple[Path, ...]
    report_map: dict[str, Path]
    config_dirs: tuple[Path, ...]
    config: dict[str, Any]

    @classmethod
    def discover(
        cls,
        workspace_root: Path | None = None,
        supervised_repo: Path | None = None,
        log_path: Path | None = None,
        experiment_id: str | None = None,
    ) -> "RepoPaths":
        workspace = (workspace_root or Path.cwd()).expanduser().resolve()
        config = load_harness_config(workspace)

        project_name = config.get("project", {}).get("name", "supervisor")
        supervised_cfg = config.get("supervised", {})

        # Resolve supervised repo path
        if supervised_repo:
            supervised = supervised_repo.expanduser().resolve()
        else:
            raw = supervised_cfg.get("repo", "../supervised-project")
            raw_path = Path(raw)
            if raw_path.is_absolute():
                supervised = raw_path.expanduser().resolve()
            else:
                supervised = (workspace / raw_path).resolve()

        claude_dir = supervised / ".claude"
        skill_name = supervised_cfg.get("skill_name", "default")
        agent_names = tuple(supervised_cfg.get("agents", []))

        # Config dirs — from CLAUDE_CONFIG_DIRS env var (colon-separated, required)
        config_dirs_env = os.environ.get("CLAUDE_CONFIG_DIRS", "")
        if not config_dirs_env:
            raise RuntimeError(
                "CLAUDE_CONFIG_DIRS env var is not set. "
                "Run /setup-env in the integration hub or set it manually "
                "(colon-separated list of ~/.claude-* directories)."
            )
        config_dirs = tuple(Path(d).expanduser() for d in config_dirs_env.split(":") if d)

        # Build report paths from config
        reports_cfg = config.get("reports", {})
        report_map: dict[str, Path] = {}
        report_paths_list: list[Path] = []
        for key, template in reports_cfg.items():
            if key == "metric":
                continue
            if isinstance(template, str):
                resolved = Path(_resolve_path_template(template, project_name))
                report_map[key] = resolved
                report_paths_list.append(resolved)

        # Log path
        log_cfg = config.get("log", {})
        log_template = log_cfg.get("path", "{tmp}/cc-{name}.log")
        if experiment_id and not log_path:
            resolved_log = Path(
                _resolve_path_template(log_template, project_name)
                .replace(".log", f"--{experiment_id}.log")
            )
        else:
            resolved_log = log_path or Path(
                _resolve_path_template(log_template, project_name)
            )

        state_dir = workspace / ".supervisor"

        # When experiment_id is set, namespace PID and state paths
        pid_name = f"{skill_name}--{experiment_id}" if experiment_id else skill_name

        return cls(
            workspace_root=workspace,
            supervised_repo=supervised,
            claude_dir=claude_dir,
            skill_name=skill_name,
            agent_names=agent_names,
            skill_path=claude_dir / "skills" / skill_name / "SKILL.md",
            agent_paths={
                name: claude_dir / "agents" / f"{name}.md" for name in agent_names
            },
            log_path=resolved_log.expanduser(),
            state_dir=state_dir,
            snapshots_dir=state_dir / "snapshots",
            pid_path=state_dir / f"{pid_name}.pid",
            state_path=state_dir / f"{pid_name}-state.json",
            latest_snapshot_path=state_dir / "latest_snapshot.json",
            history_path=state_dir / "history.jsonl",
            report_paths=tuple(report_paths_list),
            report_map=report_map,
            config_dirs=config_dirs,
            config=config,
        )

    def clean_targets(self, include_log: bool) -> tuple[Path, ...]:
        targets = list(self.report_paths)
        if include_log:
            targets.append(self.log_path)
        targets.extend((self.pid_path, self.state_path))
        return tuple(targets)


@dataclass(slots=True, frozen=True)
class LaunchSpec:
    command: str
    cwd: Path
    log_path: Path
    prompt: str


def build_launch_spec(
    paths: RepoPaths,
    *,
    prompt: str | None = None,
    claude_bin: str | None = None,
    pixi_bin: str | None = None,
    config_dir: Path | None = None,
    enable_lsp_tool: bool = True,
    experiment_id: str | None = None,
    base_branch: str | None = None,
) -> LaunchSpec:
    resolved_claude = claude_bin or shutil.which("claude") or "claude"
    resolved_pixi = pixi_bin or shutil.which("pixi") or "pixi"
    default_prompt = paths.config.get("supervised", {}).get(
        "default_prompt", "/default"
    )
    resolved_prompt = prompt or default_prompt
    resolved_config_dir = (config_dir or next_profile(paths.config_dirs, offset=1)).expanduser()
    target_config_dir = next_profile(paths.config_dirs, offset=2).expanduser()
    config_dirs_str = os.environ.get("CLAUDE_CONFIG_DIRS", "")
    cleared_pixi_env = (
        "unset PIXI_ENVIRONMENT_NAME PIXI_ENVIRONMENT_PLATFORMS PIXI_EXE "
        "PIXI_IN_SHELL PIXI_PROJECT_MANIFEST PIXI_PROJECT_NAME "
        "PIXI_PROJECT_ROOT PIXI_PROJECT_VERSION PIXI_PROMPT"
    )

    env_prefix = ""
    if enable_lsp_tool:
        env_prefix += "ENABLE_LSP_TOOL=1 "
    env_prefix += f"CLAUDE_CONFIG_DIR={shlex.quote(str(resolved_config_dir))} "
    env_prefix += f"CLAUDE_CONFIG_DIRS={shlex.quote(config_dirs_str)} "
    env_prefix += f"TARGET_CLAUDE_CONFIG_DIR={shlex.quote(str(target_config_dir))} "
    if experiment_id:
        env_prefix += f"EXPERIMENT_ID={shlex.quote(experiment_id)} "
    if base_branch:
        env_prefix += f"BASE_BRANCH={shlex.quote(base_branch)} "

    command = (
        f"{cleared_pixi_env} && "
        f"cd {shlex.quote(str(paths.supervised_repo))} && "
        f'eval "$({shlex.quote(resolved_pixi)} shell-hook -e dev)" && '
        f"{env_prefix}"
        f"{shlex.quote(str(resolved_claude))} "
        f"-p {shlex.quote(resolved_prompt)} "
        "--dangerously-skip-permissions "
        "--output-format stream-json "
        "--verbose"
    )
    return LaunchSpec(
        command=command,
        cwd=paths.supervised_repo,
        log_path=paths.log_path,
        prompt=resolved_prompt,
    )


def save_state(paths: RepoPaths, *, pid: int, launch_spec: LaunchSpec) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_path.write_text(f"{pid}\n", encoding="utf-8")
    # Extract config_dir from launch command for state tracking
    import re
    config_dir_match = re.search(r"CLAUDE_CONFIG_DIR=(\S+)", launch_spec.command)
    config_dir_used = (
        config_dir_match.group(1).strip("'\"") if config_dir_match else None
    )
    state = {
        "pid": pid,
        "prompt": launch_spec.prompt,
        "command": launch_spec.command,
        "cwd": str(launch_spec.cwd),
        "log_path": str(launch_spec.log_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": config_dir_used,
    }
    paths.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_state(paths: RepoPaths) -> dict[str, Any] | None:
    if not paths.state_path.exists():
        return None
    return json.loads(paths.state_path.read_text(encoding="utf-8"))
