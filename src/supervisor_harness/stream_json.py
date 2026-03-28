from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


def _scope_for(parent_tool_use_id: str | None) -> str:
    return "subagent" if parent_tool_use_id else "orchestrator"


@dataclass(slots=True, frozen=True)
class ParseError:
    line_no: int
    error: str
    raw_line: str


@dataclass(slots=True, frozen=True)
class TextBlock:
    line_no: int
    scope: str
    block_type: str
    text: str


@dataclass(slots=True, frozen=True)
class ToolUse:
    line_no: int
    scope: str
    parent_tool_use_id: str | None
    tool_id: str | None
    name: str
    input: dict[str, Any]

    @property
    def haystack(self) -> str:
        return json.dumps(self.input, ensure_ascii=False, sort_keys=True).lower()


@dataclass(slots=True)
class Transcript:
    events: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[ParseError] = field(default_factory=list)
    text_blocks: list[TextBlock] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def session_ids(self) -> list[str]:
        seen: list[str] = []
        for event in self.events:
            session_id = event.get("session_id")
            if session_id and session_id not in seen:
                seen.append(session_id)
        return seen

    def counts_by_tool(self, scope: str | None = None) -> dict[str, int]:
        counter: Counter[str] = Counter()
        for tool_use in self.tool_uses:
            if scope is None or tool_use.scope == scope:
                counter[tool_use.name] += 1
        return dict(counter)

    def latest_text(self, scope: str | None = None) -> str | None:
        for block in reversed(self.text_blocks):
            if scope is None or block.scope == scope:
                return block.text
        return None


def parse_stream_log(path: Path) -> Transcript:
    transcript = Transcript()
    if not path.exists():
        return transcript

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                transcript.parse_errors.append(
                    ParseError(line_no=line_no, error=str(exc), raw_line=raw_line.rstrip("\n"))
                )
                continue

            transcript.events.append(event)
            scope = _scope_for(event.get("parent_tool_use_id"))
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
                if block_type in {"text", "thinking"}:
                    transcript.text_blocks.append(
                        TextBlock(
                            line_no=line_no,
                            scope=scope,
                            block_type=block_type,
                            text=str(block.get(block_type, block.get("text", ""))),
                        )
                    )
                    continue
                if block_type == "tool_use":
                    transcript.tool_uses.append(
                        ToolUse(
                            line_no=line_no,
                            scope=scope,
                            parent_tool_use_id=event.get("parent_tool_use_id"),
                            tool_id=block.get("id"),
                            name=str(block.get("name", "")),
                            input=block.get("input", {}) if isinstance(block.get("input"), dict) else {},
                        )
                    )
    return transcript
