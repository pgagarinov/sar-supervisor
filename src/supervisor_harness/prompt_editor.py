"""Read, edit, and diff .claude prompt assets in the supervised repo.

Used by the /edit-prompts skill. All mutations go through this module so that
every change is logged, diffed, and optionally snapshotted.

Thin wrappers around harness_core.prompt_editor, adapting the RepoPaths
interface to the generic function signatures.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_core.prompt_editor import (
    list_assets as _list_assets,
    read_asset as _read_asset,
    edit_asset as _edit_asset,
    sed_asset as _sed_asset,
    diff_text,
    edit_history as _edit_history,
    resolve_asset as _resolve_asset,
)

from .config import RepoPaths


def resolve_asset(paths: RepoPaths, name: str) -> Path:
    """Resolve an asset name to its absolute path in the supervised repo."""
    return _resolve_asset(
        paths.claude_dir, paths.skill_name, list(paths.agent_names), name
    )


def list_assets(paths: RepoPaths) -> list[dict[str, Any]]:
    """List all known prompt assets with metadata."""
    return _list_assets(
        paths.claude_dir, paths.skill_name, list(paths.agent_names)
    )


def read_asset(paths: RepoPaths, name: str) -> str:
    """Read an asset's full contents."""
    return _read_asset(
        paths.claude_dir, paths.skill_name, list(paths.agent_names), name
    )


def edit_asset(
    paths: RepoPaths,
    name: str,
    new_content: str,
    *,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Write new content to an asset, returning a change record with diff."""
    return _edit_asset(
        claude_dir=paths.claude_dir,
        repo_path=paths.supervised_repo,
        skill_name=paths.skill_name,
        agent_names=list(paths.agent_names),
        name=name,
        new_content=new_content,
        log_dir=log_dir or paths.state_dir,
    )


def sed_asset(
    paths: RepoPaths,
    name: str,
    pattern: str,
    *,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply a sed-like substitution to an asset. Logged, diffed, auto-committed."""
    return _sed_asset(
        claude_dir=paths.claude_dir,
        repo_path=paths.supervised_repo,
        skill_name=paths.skill_name,
        agent_names=list(paths.agent_names),
        name=name,
        pattern=pattern,
        log_dir=log_dir or paths.state_dir,
    )


def edit_history(paths: RepoPaths, limit: int = 20) -> list[dict[str, Any]]:
    """Read recent prompt edit history."""
    return _edit_history(paths.state_dir, limit=limit)


__all__ = [
    "resolve_asset",
    "list_assets",
    "read_asset",
    "edit_asset",
    "sed_asset",
    "diff_text",
    "edit_history",
]
