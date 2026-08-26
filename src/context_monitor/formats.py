import json
import hashlib
import re
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BlockParser", "ContentBlock", "BlockType"]

class BlockType(str, Enum):
    SYSTEM = "system"
    TOOLS = "tools"
    USER = "user"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ASSISTANT = "assistant"

@dataclass
class ContentBlock:
    kind: BlockType
    text: str = ""
    step_index: int = 0
    call_id: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    hash: str = field(init=False)

    def __post_init__(self):
        self.hash = self.compute_hash()

    def compute_hash(self) -> str:
        content = self.text
        if self.arguments is not None:
            content += json.dumps(self.arguments, sort_keys=True)
        return hashlib.sha1(f"{self.kind.value}:{content}".encode("utf-8")).hexdigest()

class BlockParser:
    def __init__(self):
        self.mcp_dir = Path.home() / ".gemini" / "antigravity" / "mcp"
        self._pending_tool_calls = []

    def parse_step(self, step: dict, step_index: int) -> list[ContentBlock]:
        blocks = []
        step_type = step.get("type", "")
        content = step.get("content", "")

        if step_type == "USER_INPUT":
            blocks.append(ContentBlock(kind=BlockType.USER, text=content, step_index=step_index))
        elif step_type == "PLANNER_RESPONSE":
            thinking = step.get("thinking", "")
            if thinking:
                blocks.append(ContentBlock(kind=BlockType.THINKING, text=thinking, step_index=step_index))
            
            # Check for <think> tags in content if thinking wasn't separated
            if not thinking and "<think>" in content:
                think_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                if think_match:
                    blocks.append(ContentBlock(kind=BlockType.THINKING, text=think_match.group(1).strip(), step_index=step_index))
                    content = content.replace(think_match.group(0), "").strip()
            
            if content.strip():
                blocks.append(ContentBlock(kind=BlockType.ASSISTANT, text=content.strip(), step_index=step_index))

            tool_calls = step.get("tool_calls", [])
            for i, tc in enumerate(tool_calls):
                call_id = f"{step_index}_{i}"
                name = tc.get("name", "")
                args = tc.get("args", {})
                self._pending_tool_calls.append(call_id)
                blocks.append(ContentBlock(
                    kind=BlockType.TOOL_USE,
                    step_index=step_index,
                    call_id=call_id,
                    name=name,
                    arguments=args
                ))
        elif step_type == "GENERIC":
            call_id = self._pending_tool_calls.pop(0) if self._pending_tool_calls else f"{step_index}_unknown"
            blocks.append(ContentBlock(kind=BlockType.TOOL_RESULT, text=content, step_index=step_index, call_id=call_id))
        elif step_type == "SYSTEM_MESSAGE":
            blocks.append(ContentBlock(kind=BlockType.SYSTEM, text=content, step_index=step_index))
            
        return blocks

    def load_tool_schemas(self) -> list[ContentBlock]:
        blocks = []
        if not self.mcp_dir.exists():
            return blocks
            
        for server_dir in self.mcp_dir.iterdir():
            if not server_dir.is_dir():
                continue
            for schema_file in server_dir.glob("*.json"):
                try:
                    with open(schema_file, "r") as f:
                        schema = json.load(f)
                        blocks.append(ContentBlock(
                            kind=BlockType.TOOLS,
                            name=f"mcp_{server_dir.name}_{schema_file.stem}",
                            arguments=schema
                        ))
                except Exception:
                    pass
        return blocks
