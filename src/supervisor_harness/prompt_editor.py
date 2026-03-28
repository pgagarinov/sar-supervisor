"""Read, edit, and diff .claude prompt assets in the supervised repo.

Used by the /edit-prompts skill. All mutations go through this module so that
every change is logged, diffed, and optionally snapshotted.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RepoPaths


def _build_asset_kinds(paths: RepoPaths) -> dict[str, str]:
    """Build asset name -> relative path mapping dynamically from config."""
    kinds: dict[str, str] = {"skill": f"skills/{paths.skill_name}/SKILL.md"}
    for agent_name in paths.agent_names:
        kinds[agent_name] = f"agents/{agent_name}.md"
    return kinds


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def resolve_asset(paths: RepoPaths, name: str) -> Path:
    """Resolve an asset name to its absolute path in the supervised repo."""
    asset_kinds = _build_asset_kinds(paths)
    if name in asset_kinds:
        return paths.claude_dir / asset_kinds[name]
    # Allow direct relative path under .claude/
    candidate = paths.claude_dir / name
    if candidate.exists():
        return candidate
    raise ValueError(
        f"Unknown asset {name!r}. Known: {', '.join(sorted(asset_kinds))}. "
        f"Or pass a path relative to .claude/"
    )


def list_assets(paths: RepoPaths) -> list[dict[str, Any]]:
    """List all known prompt assets with metadata."""
    asset_kinds = _build_asset_kinds(paths)
    result = []
    for name, rel in asset_kinds.items():
        p = paths.claude_dir / rel
        entry: dict[str, Any] = {"name": name, "path": str(p), "exists": p.exists()}
        if p.exists():
            text = p.read_text(encoding="utf-8")
            entry["size_bytes"] = len(text.encode())
            entry["sha1"] = _sha1(text)
            entry["lines"] = text.count("\n")
        result.append(entry)
    # Also list any rules
    rules_dir = paths.claude_dir / "rules"
    if rules_dir.is_dir():
        for rule_file in sorted(rules_dir.glob("*.md")):
            text = rule_file.read_text(encoding="utf-8")
            result.append({
                "name": f"rules/{rule_file.name}",
                "path": str(rule_file),
                "exists": True,
                "size_bytes": len(text.encode()),
                "sha1": _sha1(text),
                "lines": text.count("\n"),
            })
    return result


def read_asset(paths: RepoPaths, name: str) -> str:
    """Read an asset's full contents."""
    p = resolve_asset(paths, name)
    if not p.exists():
        raise FileNotFoundError(f"Asset {name!r} does not exist at {p}")
    return p.read_text(encoding="utf-8")


def diff_text(old: str, new: str, label: str = "asset") -> str:
    """Produce a unified diff between old and new text."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{label}", tofile=f"b/{label}")
    )


def edit_asset(
    paths: RepoPaths,
    name: str,
    new_content: str,
    *,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Write new content to an asset, returning a change record with diff."""
    p = resolve_asset(paths, name)
    old_content = p.read_text(encoding="utf-8") if p.exists() else ""
    diff = diff_text(old_content, new_content, label=name)
    if not diff:
        return {"name": name, "path": str(p), "changed": False}

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_content, encoding="utf-8")

    record = {
        "name": name,
        "path": str(p),
        "changed": True,
        "old_sha1": _sha1(old_content),
        "new_sha1": _sha1(new_content),
        "old_lines": old_content.count("\n"),
        "new_lines": new_content.count("\n"),
        "diff": diff,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Append to edit log
    edit_log = (log_dir or paths.state_dir) / "prompt-edits.jsonl"
    edit_log.parent.mkdir(parents=True, exist_ok=True)
    log_entry = {k: v for k, v in record.items() if k != "diff"}
    log_entry["diff_lines"] = diff.count("\n")
    with edit_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    # Auto-commit the .claude/ change so it survives reverts
    import subprocess
    subprocess.run(
        ["git", "add", str(p)],
        cwd=paths.supervised_repo,
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"prompt-edit: update {name}"],
        cwd=paths.supervised_repo,
        check=False,
        capture_output=True,
    )

    return record


def edit_history(paths: RepoPaths, limit: int = 20) -> list[dict[str, Any]]:
    """Read recent prompt edit history."""
    log_path = paths.state_dir / "prompt-edits.jsonl"
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-limit:]]
