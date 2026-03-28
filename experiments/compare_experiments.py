#!/usr/bin/env python3
"""Compare experiment results from snapshots and live experiment state.

Usage:
    python experiments/compare_experiments.py [--live]

Without --live: reads history.jsonl for snapshot-based experiments.
With --live: also reads .supervisor/ for active experiment PIDs.
"""

import argparse
import json
from pathlib import Path


def _snapshot_experiments() -> list[dict]:
    """Read experiments from history.jsonl snapshot labels."""
    history_path = Path(".supervisor/history.jsonl")
    if not history_path.exists():
        return []

    experiments = []
    for line in history_path.read_text().splitlines():
        entry = json.loads(line)
        label = entry.get("label", "")
        if not label or not label.startswith("exp-"):
            continue

        variant = label.removeprefix("exp-")
        metric = entry.get("primary_metric")
        snap_path = entry.get("path", "")

        # Try to read the snapshot for more detail
        snap_json = Path(snap_path) / "snapshot.json" if snap_path else None
        code_files = 0
        if snap_json and snap_json.exists():
            snap = json.loads(snap_json.read_text())
            analysis = snap.get("analysis", {})
            repo_status = analysis.get("repo_status", {})
            code_files = len(repo_status.get("status_lines", []))

        experiments.append({
            "variant": variant,
            "source": "snapshot",
            "metric": metric,
            "code_files": code_files,
            "timestamp": entry.get("created_at", ""),
            "path": snap_path,
        })
    return experiments


def _live_experiments() -> list[dict]:
    """Read active experiments from .supervisor/ PID files."""
    state_dir = Path(".supervisor")
    if not state_dir.exists():
        return []

    experiments = []
    for pid_file in sorted(state_dir.glob("start--*.pid")):
        name = pid_file.stem
        exp_id = name.removeprefix("start--")

        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None

        state_file = state_dir / f"{name}-state.json"
        state = None
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        experiments.append({
            "variant": exp_id,
            "source": "live",
            "metric": None,
            "code_files": 0,
            "timestamp": state.get("started_at", "") if state else "",
            "path": state.get("log_path", "") if state else "",
            "pid": pid,
        })
    return experiments


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare SAR experiments")
    parser.add_argument("--live", action="store_true", help="Include live experiments from PID files")
    args = parser.parse_args()

    experiments = _snapshot_experiments()
    if args.live:
        experiments.extend(_live_experiments())

    if not experiments:
        msg = "No experiments found"
        if not args.live:
            msg += " (use --live to include active experiments)"
        print(msg)
        return

    # Sort by metric (descending for maximize)
    experiments.sort(
        key=lambda x: (x["metric"] if x["metric"] is not None else -9999),
        reverse=True,
    )

    print(f"{'Variant':<40s} {'Source':<10s} {'Metric':>10} {'Modified':>10} {'Timestamp'}")
    print("-" * 95)
    for exp in experiments:
        metric = str(exp["metric"]) if exp["metric"] is not None else "n/a"
        ts = exp["timestamp"][:19] if exp["timestamp"] else ""
        print(f"{exp['variant']:<40s} {exp['source']:<10s} {metric:>10} {exp['code_files']:>10} {ts}")

    snapshot_exps = [e for e in experiments if e["source"] == "snapshot" and e["metric"] is not None]
    if len(snapshot_exps) > 1:
        best = snapshot_exps[0]
        print(f"\nBest: {best['variant']} (metric={best['metric']})")
        print(f"Snapshot: {best['path']}")


if __name__ == "__main__":
    main()
