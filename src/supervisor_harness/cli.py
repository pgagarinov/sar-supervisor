from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

from .config import RepoPaths, build_launch_spec, load_state
from .prompt_editor import (
    edit_asset,
    edit_history as prompt_edit_history,
    list_assets,
    read_asset,
)
from .supervisor import (
    analyze_log,
    cleanup_state,
    clean_temp_files,
    extract_primary_metric,
    process_running,
    read_pid,
    resolve_snapshot,
    restart_run,
    restore_code_state,
    safe_revert,
    start_run,
    stop_run,
    write_snapshot,
)


def _paths_from_args(args: argparse.Namespace) -> RepoPaths:
    return RepoPaths.discover(
        workspace_root=Path(args.workspace_root) if args.workspace_root else None,
        supervised_repo=Path(args.supervised_repo) if args.supervised_repo else None,
        log_path=Path(args.log_path) if args.log_path else None,
    )


def _print_analysis(report: dict, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"log: {report['log_path']}")
    session_ids = report["session_ids"]
    if session_ids:
        print(f"session: {session_ids[-1]}")
    print(f"events: {report['event_count']}  parse_errors: {report['parse_error_count']}")

    dispatches = report["dispatches"]
    if dispatches:
        rendered = ", ".join(
            f"{dispatch['agent_kind'] or '?'}@L{dispatch['line_no']}" for dispatch in dispatches
        )
        print(f"dispatches: {rendered}")
    else:
        print("dispatches: none")

    repo_status = report.get("repo_status", {})
    branch = repo_status.get("branch")
    head = repo_status.get("head")
    if branch or head:
        head_short = head[:12] if isinstance(head, str) else None
        print(f"repo: {branch or '?'} {head_short or ''}".rstrip())
    status_lines = repo_status.get("status_lines") or []
    if status_lines:
        print("repo_changes:")
        for line in status_lines[:20]:
            print(f"- {line}")

    if report["latest_text"]:
        print(f"latest_text: {report['latest_text']}")

    report_summaries = report["report_summaries"]
    visible_reports = {name: payload for name, payload in report_summaries.items() if payload}
    if visible_reports:
        print("reports:")
        for name, payload in visible_reports.items():
            print(f"- {name}: {json.dumps(payload, ensure_ascii=False)}")
    return 0


def _latest_marker(report: dict) -> str:
    latest_text = report.get("latest_text")
    if isinstance(latest_text, str) and latest_text.strip():
        return " ".join(latest_text.strip().splitlines())[:140]
    dispatches = report.get("dispatches") or []
    if dispatches:
        last = dispatches[-1]
        return f"{last['agent_kind'] or '?'}@L{last['line_no']}"
    return "no activity yet"


def _heartbeat_line(paths: RepoPaths, report: dict) -> str:
    log_path = Path(report["log_path"])
    log_bytes = log_path.stat().st_size if log_path.exists() else 0
    idle_seconds = max(0.0, time.time() - log_path.stat().st_mtime) if log_path.exists() else 0.0
    session_ids = report.get("session_ids") or []
    session_suffix = session_ids[-1][:8] if session_ids else "none"
    return (
        f"heartbeat: session={session_suffix} "
        f"events={report['event_count']} "
        f"log_bytes={log_bytes} "
        f"idle={idle_seconds:.0f}s "
        f"marker={_latest_marker(report)}"
    )


def _status_payload(paths: RepoPaths) -> dict:
    state = load_state(paths)
    pid = read_pid(paths)
    running = bool(pid and process_running(pid))
    report = analyze_log(paths).to_dict()
    primary_metric = extract_primary_metric(
        paths, report.get("report_summaries", {})
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": pid,
        "running": running,
        "session_id": report["session_ids"][-1] if report["session_ids"] else None,
        "event_count": report["event_count"],
        "dispatches": report["dispatches"],
        "latest_text": report["latest_text"],
        "primary_metric": primary_metric,
        "state": state,
    }


def _status_line(payload: dict) -> str:
    hook = payload.get("hook") or {}
    session_id = payload.get("session_id")
    dispatches = payload.get("dispatches") or []
    dispatch_summary = ",".join(
        dispatch["agent_kind"] or "?" for dispatch in dispatches
    ) or "none"
    primary_metric = payload.get("primary_metric")
    if primary_metric is not None:
        metric = f"metric={primary_metric}"
    else:
        metric = "metric=n/a"
    marker = _latest_marker(
        {
            "latest_text": payload.get("latest_text"),
            "dispatches": dispatches,
        }
    )
    return (
        f"{payload['timestamp']} "
        f"action={hook.get('action') or 'check_loop_status'} "
        f"running={payload['running']} "
        f"pid={payload.get('pid')} "
        f"session={(session_id or 'none')[:8]} "
        f"events={payload['event_count']} "
        f"dispatches={dispatch_summary} "
        f"{metric} "
        f"marker={marker}"
    )


def _has_all_clear(report: dict, paths: RepoPaths) -> bool:
    """Check if the primary report indicates completion."""
    report_path = paths.report_map.get(
        paths.config.get("reports", {}).get("metric", {}).get("report", "primary")
    )
    if report_path is None:
        return False
    primary = report.get("report_summaries", {}).get(report_path.name)
    return isinstance(primary, dict) and primary.get("status") == "all_clear"


def _cmd_start(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    prompt = args.prompt or paths.config.get("supervised", {}).get(
        "default_prompt", "/default"
    )
    if args.dry_run:
        launch_spec = build_launch_spec(paths, prompt=prompt)
        print(launch_spec.command)
        return 0

    config_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else None
    launch_spec, pid = start_run(
        paths,
        prompt=prompt,
        clean_first=not args.no_clean,
        config_dir=config_dir,
    )
    print(f"pid: {pid}")
    print(f"log: {launch_spec.log_path}")
    print(f"prompt: {launch_spec.prompt}")
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)

    def emit() -> int:
        report = analyze_log(paths).to_dict()
        return _print_analysis(report, as_json=args.json)

    if not args.follow:
        return emit()

    last_snapshot = None
    last_heartbeat = 0.0
    sleep_seconds = min(args.interval_seconds, args.heartbeat_seconds)
    while True:
        report = analyze_log(paths).to_dict()
        snapshot = json.dumps(report, sort_keys=True)
        if snapshot != last_snapshot:
            if last_snapshot is not None:
                print()
            _print_analysis(report, as_json=args.json)
            last_snapshot = snapshot
            last_heartbeat = time.monotonic()
        elif not args.json and time.monotonic() - last_heartbeat >= args.heartbeat_seconds:
            print(_heartbeat_line(paths, report))
            last_heartbeat = time.monotonic()
        pid = read_pid(paths)
        if pid is None or not process_running(pid):
            return 0
        time.sleep(sleep_seconds)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    snapshot_dir = write_snapshot(paths, label=args.label)
    print(snapshot_dir)
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    if not paths.history_path.exists():
        print("history: none")
        return 0
    lines = paths.history_path.read_text(encoding="utf-8").splitlines()
    tail = lines[-args.limit :]
    if args.json:
        print(json.dumps([json.loads(line) for line in tail], indent=2))
        return 0
    for line in tail:
        item = json.loads(line)
        metric = item.get("primary_metric")
        metric_str = str(metric) if metric is not None else "n/a"
        print(f"{item['created_at']}  metric={metric_str}  {item['path']}")
    return 0


def _cmd_watch_status(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    status_log_path = paths.state_dir / "status.jsonl"
    if not args.immediate:
        time.sleep(args.interval_seconds)
    while True:
        payload = _status_payload(paths)
        payload["hook"] = {
            "action": "check_loop_status",
            "message": "Supervisor should check the loop status now.",
            "interval_seconds": args.interval_seconds,
        }
        if not args.no_log:
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            with status_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        if args.json:
            print(json.dumps(payload, indent=2), flush=True)
        else:
            print(_status_line(payload), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


def _cmd_clean(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    removed = clean_temp_files(paths, include_log=args.include_log)
    if args.include_snapshots:
        for path in (paths.latest_snapshot_path, paths.history_path):
            if path.exists():
                path.unlink()
                removed.append(path)
        if paths.snapshots_dir.exists():
            shutil.rmtree(paths.snapshots_dir)
            removed.append(paths.snapshots_dir)
    if not removed:
        print("removed: none")
        return 0
    for path in removed:
        print(path)
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    stopped = stop_run(paths)
    print("stopped" if stopped else "not-running")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    launch_spec, pid = restart_run(paths, prompt=args.prompt)
    print(f"pid: {pid}")
    print(f"log: {launch_spec.log_path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    state = load_state(paths)
    pid = read_pid(paths)
    running = bool(pid and process_running(pid))
    if pid and not running:
        cleanup_state(paths)
        pid = None
        state = None
    payload = {
        "pid": pid,
        "running": running if pid else False,
        "state": state,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"pid: {payload['pid']}")
    print(f"running: {payload['running']}")
    if state:
        print(f"prompt: {state.get('prompt')}")
        print(f"log: {state.get('log_path')}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    try:
        snapshot_dir = resolve_snapshot(paths, args.identifier)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"snapshot: {snapshot_dir}")
    code_state_dir = snapshot_dir / "code-state"
    if not code_state_dir.exists():
        print("error: snapshot has no code-state (taken before checkpointing was added)", file=sys.stderr)
        return 1
    if args.dry_run:
        snap = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
        cs = snap.get("code_state", {})
        print(f"  head: {cs.get('head', '?')[:12]}")
        print(f"  tracked_patch_bytes: {cs.get('tracked_patch_bytes', 0)}")
        print(f"  untracked_files: {cs.get('untracked_file_count', 0)}")
        print("dry-run: no changes made")
        return 0
    if not args.no_checkpoint:
        checkpoint = write_snapshot(paths, label="pre-restore")
        print(f"checkpoint: {checkpoint}")
    result = restore_code_state(paths, snapshot_dir)
    print(f"tracked_applied: {result['tracked_applied']}")
    print(f"untracked_extracted: {result['untracked_extracted']}")
    if result.get("tracked_error"):
        print(f"tracked_error: {result['tracked_error']}", file=sys.stderr)
    print(f"working_tree_files: {result['status_lines']}")
    return 0


def _cmd_revert_safe(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    snapshot_dir = safe_revert(
        paths,
        label=args.label,
        full=args.full,
    )
    print(f"checkpoint: {snapshot_dir}")
    print("reverted")
    return 0


def _cmd_prompt_list(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    assets = list_assets(paths)
    if args.json:
        print(json.dumps(assets, indent=2))
        return 0
    for a in assets:
        status = f"{a['lines']}L {a['sha1'][:8]}" if a["exists"] else "MISSING"
        print(f"  {a['name']:20s} {status}")
    return 0


def _cmd_prompt_read(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    content = read_asset(paths, args.name)
    print(content, end="")
    return 0


def _cmd_prompt_edit(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    content = sys.stdin.read()
    if not content:
        print("error: no content on stdin", file=sys.stderr)
        return 1
    record = edit_asset(paths, args.name, content)
    if not record["changed"]:
        print(f"{args.name}: no changes")
        return 0
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"{args.name}: changed ({record['old_lines']}L -> {record['new_lines']}L)")
        if record.get("diff"):
            print(record["diff"], end="")
    return 0


def _cmd_prompt_diff(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    content = read_asset(paths, args.name)
    new_content = sys.stdin.read()
    if not new_content:
        print("error: no content on stdin", file=sys.stderr)
        return 1
    from .prompt_editor import diff_text
    diff = diff_text(content, new_content, label=args.name)
    if diff:
        print(diff, end="")
    else:
        print("no differences")
    return 0


def _cmd_prompt_history(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    history = prompt_edit_history(paths, limit=args.limit)
    if not history:
        print("no edits")
        return 0
    if args.json:
        print(json.dumps(history, indent=2))
        return 0
    for entry in history:
        print(f"{entry['timestamp']}  {entry['name']}  {entry.get('old_lines',0)}L->{entry.get('new_lines',0)}L")
    return 0


def _cmd_loop(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    prompt = args.prompt or paths.config.get("supervised", {}).get(
        "default_prompt", "/default"
    )
    pid = read_pid(paths)
    running = bool(pid and process_running(pid))
    if not running:
        config_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else None
        launch_spec, pid = start_run(
            paths,
            prompt=prompt,
            clean_first=not args.no_clean,
            config_dir=config_dir,
        )
        print(f"started pid {pid} -> {launch_spec.log_path}")

    last_snapshot = None
    last_heartbeat = 0.0
    sleep_seconds = min(args.interval_seconds, args.heartbeat_seconds)
    while True:
        report = analyze_log(paths).to_dict()
        snapshot = json.dumps(
            {
                "session_ids": report["session_ids"],
                "event_count": report["event_count"],
                "dispatches": report["dispatches"],
                "repo_status": report["repo_status"],
                "report_summaries": report["report_summaries"],
            },
            sort_keys=True,
        )
        if snapshot != last_snapshot:
            _print_analysis(report, as_json=args.json)
            if not args.no_archive:
                snapshot_dir = write_snapshot(paths, label="loop")
                print(f"snapshot: {snapshot_dir}")
            last_snapshot = snapshot
            last_heartbeat = time.monotonic()
        elif not args.json and time.monotonic() - last_heartbeat >= args.heartbeat_seconds:
            print(_heartbeat_line(paths, report))
            last_heartbeat = time.monotonic()

        if _has_all_clear(report, paths):
            print("all_clear reported; exiting loop")
            return 0

        if args.once:
            return 0

        pid = read_pid(paths)
        if pid is None or not process_running(pid):
            if not args.no_archive:
                snapshot_dir = write_snapshot(paths, label="final")
                print(f"final_snapshot: {snapshot_dir}")
            return 0
        time.sleep(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="supervisor-harness")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace-root")
    common.add_argument("--supervised-repo")
    common.add_argument("--log-path")
    common.add_argument("--config-dir", help="Claude config dir override")
    parser.add_argument("--workspace-root")
    parser.add_argument("--supervised-repo")
    parser.add_argument("--log-path")
    parser.add_argument("--config-dir")

    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", parents=[common])
    start_parser.add_argument("--prompt", default=None, help="Prompt to pass to claude -p (default: from harness.toml)")
    start_parser.add_argument("--no-clean", action="store_true")
    start_parser.add_argument("--dry-run", action="store_true")
    start_parser.set_defaults(func=_cmd_start)

    monitor_parser = subparsers.add_parser("monitor", parents=[common])
    monitor_parser.add_argument("--json", action="store_true")
    monitor_parser.add_argument("--follow", action="store_true")
    monitor_parser.add_argument("--interval-seconds", type=float, default=10.0)
    monitor_parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    monitor_parser.set_defaults(func=_cmd_monitor)

    snapshot_parser = subparsers.add_parser("snapshot", parents=[common])
    snapshot_parser.add_argument("--label")
    snapshot_parser.set_defaults(func=_cmd_snapshot)

    history_parser = subparsers.add_parser("history", parents=[common])
    history_parser.add_argument("--json", action="store_true")
    history_parser.add_argument("--limit", type=int, default=10)
    history_parser.set_defaults(func=_cmd_history)

    watch_status_parser = subparsers.add_parser("watch-status", parents=[common])
    watch_status_parser.add_argument("--interval-seconds", type=float, default=30.0)
    watch_status_parser.add_argument("--json", action="store_true")
    watch_status_parser.add_argument("--no-log", action="store_true")
    watch_status_parser.add_argument("--immediate", action="store_true")
    watch_status_parser.add_argument("--once", action="store_true")
    watch_status_parser.set_defaults(func=_cmd_watch_status)

    clean_parser = subparsers.add_parser("clean", parents=[common])
    clean_parser.add_argument("--include-log", action="store_true")
    clean_parser.add_argument("--include-snapshots", action="store_true")
    clean_parser.set_defaults(func=_cmd_clean)

    stop_parser = subparsers.add_parser("stop", parents=[common])
    stop_parser.set_defaults(func=_cmd_stop)

    restart_parser = subparsers.add_parser("restart", parents=[common])
    restart_parser.add_argument("--prompt", help="Prompt override (default: from prior run or harness.toml)")
    restart_parser.set_defaults(func=_cmd_restart)

    status_parser = subparsers.add_parser("status", parents=[common])
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=_cmd_status)

    restore_parser = subparsers.add_parser("restore", parents=[common])
    restore_parser.add_argument("identifier", help="Snapshot ID prefix, full path, or 'best'")
    restore_parser.add_argument("--no-checkpoint", action="store_true", help="Skip auto-checkpoint before restore")
    restore_parser.add_argument("--dry-run", action="store_true", help="Show what would be restored without doing it")
    restore_parser.set_defaults(func=_cmd_restore)

    revert_safe_parser = subparsers.add_parser("revert-safe", parents=[common])
    revert_safe_parser.add_argument("--label", help="Label for the auto-checkpoint")
    revert_safe_parser.add_argument("--full", action="store_true", help="Revert entire working tree, not just configured paths")
    revert_safe_parser.set_defaults(func=_cmd_revert_safe)

    prompt_list_parser = subparsers.add_parser("prompt-list", parents=[common])
    prompt_list_parser.add_argument("--json", action="store_true")
    prompt_list_parser.set_defaults(func=_cmd_prompt_list)

    prompt_read_parser = subparsers.add_parser("prompt-read", parents=[common])
    prompt_read_parser.add_argument("name", help="Asset name (use prompt-list to see available) or path relative to .claude/")
    prompt_read_parser.set_defaults(func=_cmd_prompt_read)

    prompt_edit_parser = subparsers.add_parser("prompt-edit", parents=[common])
    prompt_edit_parser.add_argument("name", help="Asset name to edit")
    prompt_edit_parser.add_argument("--json", action="store_true")
    prompt_edit_parser.set_defaults(func=_cmd_prompt_edit)

    prompt_diff_parser = subparsers.add_parser("prompt-diff", parents=[common])
    prompt_diff_parser.add_argument("name", help="Asset name to diff against stdin")
    prompt_diff_parser.set_defaults(func=_cmd_prompt_diff)

    prompt_history_parser = subparsers.add_parser("prompt-history", parents=[common])
    prompt_history_parser.add_argument("--json", action="store_true")
    prompt_history_parser.add_argument("--limit", type=int, default=20)
    prompt_history_parser.set_defaults(func=_cmd_prompt_history)

    loop_parser = subparsers.add_parser("loop", parents=[common])
    loop_parser.add_argument("--prompt", default=None, help="Prompt override (default: from harness.toml)")
    loop_parser.add_argument("--json", action="store_true")
    loop_parser.add_argument("--interval-seconds", type=float, default=10.0)
    loop_parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    loop_parser.add_argument("--no-clean", action="store_true")
    loop_parser.add_argument("--no-archive", action="store_true")
    loop_parser.add_argument("--once", action="store_true")
    loop_parser.set_defaults(func=_cmd_loop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
