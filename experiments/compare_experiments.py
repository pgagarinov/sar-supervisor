#!/usr/bin/env python3
"""Compare experiment results from snapshots.

Usage:
    python experiments/compare_experiments.py

Reads history.jsonl and finds all experiment-labeled snapshots.
Prints a comparison table sorted by the primary metric.
"""

import json
from pathlib import Path


def main():
    history_path = Path(".supervisor/history.jsonl")
    if not history_path.exists():
        print("No history.jsonl found")
        return

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
            "metric": metric,
            "code_files": code_files,
            "timestamp": entry.get("created_at", ""),
            "path": snap_path,
        })

    if not experiments:
        print("No experiment snapshots found (labels must start with 'exp-')")
        return

    # Sort by metric (ascending = best first for minimize)
    experiments.sort(key=lambda x: (x["metric"] if x["metric"] is not None else 9999))

    print(f"{'Variant':<30} {'Metric':>10} {'Modified':>10} {'Timestamp'}")
    print("-" * 75)
    for exp in experiments:
        metric = str(exp["metric"]) if exp["metric"] is not None else "n/a"
        ts = exp["timestamp"][:19] if exp["timestamp"] else ""
        print(f"{exp['variant']:<30} {metric:>10} {exp['code_files']:>10} {ts}")

    if len(experiments) > 1:
        best = experiments[0]
        print(f"\nBest: {best['variant']} (metric={best['metric']})")
        print(f"Snapshot: {best['path']}")


if __name__ == "__main__":
    main()
