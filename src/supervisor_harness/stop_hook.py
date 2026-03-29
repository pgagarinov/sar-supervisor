"""Stop hook decision support module.

Produces structured analysis for the Claude Code stop hook.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness_core.metrics import metric_trend, trend_direction

from .config import RepoPaths, load_state
from .supervisor import analyze_log, extract_primary_metric, process_running, read_pid


AUTONOMY_DIRECTIVE = (
    "YOU ARE AUTONOMOUS. Do not ask the user questions. "
    "Analyze the situation, decide your next action, and execute it. "
    "Refer to CLAUDE.md 'Autonomous Operation' for guidance.\n\n"
    "RESEARCHER CHECK: Are you just reporting numbers, or are you actively thinking about "
    "WHY the metric isn't improving and WHAT to do differently? "
    "If stalled, analyze the remaining issues NOW -- categorize errors, form a hypothesis, "
    "design a new approach. The user should never have to ask you for ideas."
)


def _count_iterations(
    dispatches: list[dict], agent_names: tuple[str, ...]
) -> int:
    """Count dispatches of the first agent as a proxy for iteration count."""
    if not agent_names:
        return sum(1 for d in dispatches if d.get("agent_kind"))
    first_agent = agent_names[0]
    return sum(1 for d in dispatches if d.get("agent_kind") == first_agent)


HAIKU_PROMPT = """\
Analyze this log from an autonomous inner loop. The main agent dispatches subagents via the Agent tool and reads their JSON reports.

Detect anti-patterns:
1. Main agent doing work itself instead of dispatching (reading source files, editing code, analyzing in text blocks)
2. Main agent using TodoWrite (should not track tasks, just dispatch)
3. Dispatching the wrong subagent_type for the current phase (compare against the pattern visible in the log)
4. Main agent rephrasing or synthesizing instead of forwarding agent outputs verbatim
5. Missing expected dispatches (e.g., analysis happened but no action dispatch followed)

Here are the recent events from the main agent (JSON array):

{events_json}

Return ONLY a JSON object (no markdown, no explanation):
{{"deviations": ["description1", ...], "phase": "current phase description", "summary": "one line status"}}
If no deviations: {{"deviations": [], "phase": "...", "summary": "..."}}
"""


def _load_haiku_config(state_dir: Path) -> dict[str, Any]:
    config_path = state_dir / "haiku-config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"max_events": 50}


def _read_log_chunk(
    log_path: Path, offset_path: Path, max_events: int = 50
) -> tuple[list[dict[str, Any]], int]:
    """Read new orchestrator events from the log since last offset."""
    if not log_path.exists():
        return [], 0

    file_size = log_path.stat().st_size

    # Read stored offset
    offset = 0
    if offset_path.exists():
        try:
            offset = int(offset_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            offset = 0

    # Log was replaced (new run) -- reset
    if offset > file_size:
        offset = 0

    # Nothing new
    if offset >= file_size:
        return [], offset

    # Read from offset to EOF
    summaries: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only orchestrator-scope events
            if event.get("parent_tool_use_id") is not None:
                continue

            message = event.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_input = block.get("input", {})
                    summary: dict[str, Any] = {
                        "tool": block.get("name", ""),
                    }
                    if "subagent_type" in tool_input:
                        summary["subagent_type"] = tool_input["subagent_type"]
                    if "prompt" in tool_input:
                        summary["prompt_preview"] = str(tool_input["prompt"])[:200]
                    if "file_path" in tool_input:
                        summary["file_path"] = tool_input["file_path"]
                    summaries.append(summary)
                elif block_type == "text":
                    text = str(block.get("text", ""))
                    if len(text) > 20:
                        summaries.append({
                            "text_preview": text[:200],
                        })

    # Cap to last max_events
    if len(summaries) > max_events:
        summaries = summaries[-max_events:]

    return summaries, file_size


def _haiku_analyze(
    summaries: list[dict[str, Any]],
    claude_bin: str | None = None,
) -> dict[str, Any] | None:
    """Call Haiku via claude CLI to analyze orchestrator events."""
    if not summaries:
        return {"deviations": [], "phase": "unknown", "summary": "no events to analyze"}

    resolved_claude = claude_bin or shutil.which("claude") or "claude"
    events_json = json.dumps(summaries, indent=None, ensure_ascii=False)
    prompt = HAIKU_PROMPT.format(events_json=events_json)

    try:
        result = subprocess.run(
            [resolved_claude, "--dangerously-skip-permissions",
             "--model", "haiku", "-p", prompt,
             "--output-format", "json", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/tmp",
        )
        if result.returncode != 0:
            return None

        # Parse the JSON response -- claude --output-format json wraps in {"result": "..."}
        outer = json.loads(result.stdout)
        # The actual response text may be in "result" field
        response_text = outer.get("result", result.stdout) if isinstance(outer, dict) else result.stdout
        if isinstance(response_text, str):
            # Try to parse as JSON directly
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                # Try to extract JSON from markdown code block
                if "```" in response_text:
                    for block in response_text.split("```"):
                        block = block.strip()
                        if block.startswith("json"):
                            block = block[4:].strip()
                        try:
                            return json.loads(block)
                        except json.JSONDecodeError:
                            continue
                return None
        return response_text if isinstance(response_text, dict) else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _save_offset(offset_path: Path, offset: int) -> None:
    offset_path.parent.mkdir(parents=True, exist_ok=True)
    offset_path.write_text(str(offset), encoding="utf-8")


def _detect_deviations_heuristic(
    report: dict, agent_names: tuple[str, ...] = ()
) -> list[str]:
    """Check for common anti-patterns."""
    deviations = []
    orch_tools = report.get("tool_counts", {}).get("orchestrator", {})

    if orch_tools.get("TodoWrite", 0) > 0:
        deviations.append(f"orchestrator used TodoWrite {orch_tools['TodoWrite']}x")

    dispatches = report.get("dispatches", [])
    kinds = [d.get("agent_kind") for d in dispatches]

    # Check for missing agents in a complete cycle
    if len(kinds) >= 5 and len(agent_names) >= 2:
        present = set(kinds) - {None}
        missing = [a for a in agent_names if a not in present]
        dispatched = [a for a in agent_names if a in present]
        if missing and dispatched:
            deviations.append(
                f"agents {missing} not dispatched despite {dispatched} running"
            )

    # Check orchestrator doing too much analysis
    orch_reads = orch_tools.get("Read", 0) + orch_tools.get("Grep", 0)
    dispatch_count = sum(1 for d in dispatches if d.get("agent_kind"))
    if orch_reads > 20 and dispatch_count < 3:
        deviations.append(
            f"orchestrator Read/Grep={orch_reads} but only {dispatch_count} dispatches"
        )

    return deviations


def _current_phase(
    report: dict, phases_config: dict | None = None
) -> str:
    """Detect current phase from latest_text."""
    lt = report.get("latest_text") or ""
    if phases_config:
        markers = phases_config.get("markers", {})
        labels = phases_config.get("labels", {})
        # Check in reverse order so more specific markers match first
        for key in reversed(list(markers.keys())):
            marker = markers[key]
            if marker in lt:
                label = labels.get(key, key)
                for line in lt.split("\n"):
                    if marker in line:
                        return line.strip()[:120]
                return f"{key} ({label})"
    return "unknown"


def _action_guidance(
    running: bool,
    trend: list[int | float],
    direction: str,
    deviations: list[str],
    iterations: int,
) -> str:
    if not running:
        return (
            "STOPPED. Snapshot the final state. Then: read the report, "
            "categorize remaining issues by type, "
            "and design a new approach targeting the dominant error class. "
            "Don't just restart with the same skill."
        )
    if deviations:
        return (
            f"DEVIATION: {'; '.join(deviations)}. "
            "Assess severity: is the inner loop still making progress despite the deviation? "
            "If yes, note it and continue. If no, stop, fix the specific prompt that caused the deviation, restart."
        )
    if direction == "regressing":
        current = trend[-1] if trend else "?"
        prev = trend[-2] if len(trend) >= 2 else "?"
        if iterations <= 1:
            return f"REGRESSING ({prev} -> {current}) but iteration {iterations} -- wait, the worker may self-correct."
        return (
            f"REGRESSING ({prev} -> {current}). The current changes are making things worse. "
            "Stop, analyze WHAT changed, revert if needed, adjust approach."
        )
    if direction == "stalled":
        val = trend[-1] if trend else "?"
        if iterations < 3:
            return f"Stalled at {val} but only {iterations} iterations -- wait, but start analyzing issue patterns NOW."
        if iterations < 6:
            return (
                f"STALLED at {val} for {iterations} iterations. "
                "Read the report NOW. What error patterns remain? Are they the same class the current skill targets? "
                "If not, the skill design is wrong for these issues -- design a new variant."
            )
        return (
            f"STALLED at {val} for {iterations} iterations. Current approach is definitively not working. "
            "STOP the run. Analyze issue patterns. Write a new variant in researcher_variants/. "
            "The remaining issues likely need a fundamentally different strategy."
        )
    if direction == "improving":
        return "IMPROVING. Let it run. But think ahead: what will you do when it stalls?"
    return "Early stage. Let it run, but start analyzing the issue distribution."


def generate_stop_hook_output(paths: RepoPaths | None = None) -> str:
    """Generate the structured stop hook output."""
    if paths is None:
        paths = RepoPaths.discover()

    pid = read_pid(paths)
    running = bool(pid and process_running(pid))
    state = load_state(paths) or {}
    report = analyze_log(paths).to_dict()

    # Elapsed time
    elapsed = ""
    started = state.get("started_at", "")
    if started:
        try:
            t0 = datetime.fromisoformat(started)
            dt = datetime.now(timezone.utc) - t0
            mins = int(dt.total_seconds() // 60)
            elapsed = f" ({mins}m elapsed)"
        except (ValueError, TypeError):
            pass

    # Metrics
    events = report.get("event_count", 0)
    dispatches = report.get("dispatches", [])
    iterations = _count_iterations(dispatches, paths.agent_names)
    trend = metric_trend(paths.history_path)
    direction = trend_direction(trend)

    metric_cfg = paths.config.get("reports", {}).get("metric", {})
    metric_field = metric_cfg.get("field", "metric")
    metric_direction = metric_cfg.get("direction", "minimize")

    if metric_direction == "minimize":
        best_ever = min(trend) if trend else None
    else:
        best_ever = max(trend) if trend else None

    # Extract current metric value
    metric_value = extract_primary_metric(
        paths, report.get("report_summaries", {})
    )

    # Haiku-based deviation detection with heuristic fallback
    haiku_config = _load_haiku_config(paths.state_dir)
    max_events = haiku_config.get("max_events", 50)
    offset_path = paths.state_dir / "haiku-offset"

    haiku_result = None
    summaries, new_offset = _read_log_chunk(
        paths.log_path, offset_path, max_events=max_events
    )
    if summaries:
        haiku_result = _haiku_analyze(summaries)
        if haiku_result is not None:
            _save_offset(offset_path, new_offset)

    phases_config = paths.config.get("phases")
    if haiku_result is not None:
        deviations = haiku_result.get("deviations", [])
        phase = haiku_result.get("phase", "unknown")
        haiku_summary = haiku_result.get("summary", "")
    else:
        # Fallback to heuristics
        deviations = _detect_deviations_heuristic(report, paths.agent_names)
        phase = _current_phase(report, phases_config)
        haiku_summary = ""

    # Build output
    parts = []
    parts.append(
        f"running={running} pid={pid} events={events}{elapsed} iteration={iterations}"
    )
    parts.append(phase)

    if metric_value is not None:
        parts.append(f"{metric_field}: {metric_value}")

    if trend:
        trend_str = " -> ".join(str(t) for t in trend[-6:])
        best_str = f", best={best_ever}" if best_ever is not None else ""
        parts.append(f"trend: {trend_str} ({direction}{best_str})")

    if deviations:
        parts.append(f"deviations: {'; '.join(deviations)}")
    else:
        parts.append("deviations: none")

    if haiku_summary:
        parts.append(f"haiku: {haiku_summary}")

    parts.append("")
    guidance = _action_guidance(running, trend, direction, deviations, iterations)
    parts.append(guidance)
    parts.append("")
    parts.append(AUTONOMY_DIRECTIVE)

    summary = "\n".join(parts)
    return json.dumps({"decision": "block", "reason": summary})


def main() -> None:
    print(generate_stop_hook_output())


if __name__ == "__main__":
    main()
