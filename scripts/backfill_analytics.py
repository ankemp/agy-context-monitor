#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Historical Analytics Back-Fill Script
-------------------------------------------------
Scans all past conversation transcripts in ~/.gemini/antigravity/brain/,
extracts structured analytics (including context efficiency, developer velocity,
thinking tokens, and redundant reads), and populates the centralized SQLite database.
"""

import os
import sys
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Any

# Import db layer
_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import (  # noqa: E402
    DEFAULT_DB_PATH,
    ensure_rollups_synced,
    get_db_connection,
    init_db,
    refresh_conversation_rollups,
)
from context_monitor.formats import BlockParser  # noqa: E402
from context_monitor.pricing import (  # noqa: E402
    DEFAULT_MODEL,
    calculate_step_cost,
    extract_model_switch,
    get_model_context_limit,
)
from context_monitor.project import (  # noqa: E402
    backfill_db_projects,
    extract_project_from_text,
    extract_project_from_tool_args,
    normalize_project_name,
)
from context_monitor.quota import sync_and_get_quota_state  # noqa: E402
from context_monitor.timings import compute_session_timings  # noqa: E402
from context_monitor.velocity import compute_active_velocity  # noqa: E402

CHARS_PER_TOKEN = 3.8


def extract_file_basename(content: str) -> str:
    m = re.search(r"File Path:\s*`?(?:file://)?([^`\r\n]+)`?", content)
    if m:
        return Path(m.group(1).strip()).name
    return "File view"


def extract_system_message_detail(content: str) -> str:
    m_task = re.search(r"sender=([^\s]+)", content)
    m_code = re.search(r"exited with code (\d+)", content)
    sender = m_task.group(1).split("/")[-1] if m_task else "Task"
    code = f"(exit {m_code.group(1)})" if m_code else ""
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
        and not line.strip().startswith("<")
        and not line.strip().startswith("The following")
        and not line.strip().startswith("[Message]")
    ]
    snippet = lines[0][:40] if lines else ""
    return f"{sender} {code} {snippet}".strip()


def detect_model_from_transcript(transcript_path: Path) -> tuple[str, str, int]:
    first_model = DEFAULT_MODEL
    latest_model = DEFAULT_MODEL
    found_first = False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                except Exception:
                    continue
                if step.get("type") == "USER_INPUT":
                    content = step.get("content", "")
                    switched_model = extract_model_switch(content)
                    if switched_model:
                        if not found_first:
                            first_model = switched_model
                            found_first = True
                        latest_model = switched_model
    except Exception:
        pass

    max_tokens = get_model_context_limit(latest_model)
    return first_model, latest_model, max_tokens


def _clean_arg_str(val: Any) -> str:
    """Strips whitespace and unquotes double/single-encoded JSON strings."""
    if val is None:
        return ""
    s = str(val).strip()
    while True:
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            try:
                unquoted = json.loads(s)
                if isinstance(unquoted, str):
                    s = unquoted.strip()
                    continue
            except Exception:
                pass
            inner = s[1:-1].strip()
            if not re.search(r'(?<!\\)"', inner):
                s = inner.replace(r"\"", '"').strip()
                continue
        if s.startswith("'") and s.endswith("'") and len(s) >= 2:
            inner = s[1:-1].strip()
            if not re.search(r"(?<!\\)'", inner):
                s = inner.replace(r"\'", "'").strip()
                continue
        break
    return s


def extract_subagent_specs(tc_args: dict[str, Any]) -> list[dict[str, str]]:
    """Extracts subagent specifications from invoke_subagent tool call arguments.

    Handles Antigravity 1.0 (Subagents list or JSON string) and Antigravity 2.0 flat arguments
    (Role, role, TypeName, type, Prompt, prompt, Model, model), cleaning inner quotes.
    """
    if not isinstance(tc_args, dict):
        return []

    sub_raw = tc_args.get("Subagents") or tc_args.get("subagents")
    if sub_raw:
        specs_list = []
        if isinstance(sub_raw, str):
            s = sub_raw.strip()
            if (s.startswith('"') and s.endswith('"')) or (
                s.startswith("'") and s.endswith("'")
            ):
                s = _clean_arg_str(s)
            try:
                parsed = json.loads(s)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                if isinstance(parsed, list):
                    specs_list = parsed
            except Exception:
                pass
        elif isinstance(sub_raw, list):
            specs_list = sub_raw

        if specs_list:
            results = []
            for item in specs_list:
                if isinstance(item, dict):
                    role = _clean_arg_str(item.get("Role") or item.get("role") or "")
                    sub_type = _clean_arg_str(
                        item.get("TypeName")
                        or item.get("type")
                        or item.get("subagent_type")
                        or ""
                    )
                    model = _clean_arg_str(item.get("Model") or item.get("model") or "")
                    results.append(
                        {
                            "role": role or "Autonomous Worker",
                            "type": sub_type or "self",
                            "model": model or "inherit",
                            "Role": role or "Autonomous Worker",
                            "TypeName": sub_type or "self",
                            "Model": model or "inherit",
                        }
                    )
            if results:
                return results

    flat_keys = (
        "Role",
        "role",
        "TypeName",
        "type",
        "subagent_type",
        "subagentType",
        "Prompt",
        "prompt",
        "Model",
        "model",
    )
    if any(k in tc_args for k in flat_keys):
        raw_role = tc_args.get("Role") or tc_args.get("role")
        raw_type = (
            tc_args.get("TypeName")
            or tc_args.get("type")
            or tc_args.get("subagent_type")
            or tc_args.get("subagentType")
        )
        raw_model = tc_args.get("Model") or tc_args.get("model")

        role = _clean_arg_str(raw_role)
        sub_type = _clean_arg_str(raw_type)
        model = _clean_arg_str(raw_model)

        return [
            {
                "role": role if role else "Autonomous Worker",
                "type": sub_type if sub_type else "self",
                "model": model if model else "inherit",
                "Role": role if role else "Autonomous Worker",
                "TypeName": sub_type if sub_type else "self",
                "Model": model if model else "inherit",
            }
        ]

    return []


def parse_manage_subagents_output(content: str) -> list[dict[str, str]]:
    """Parse manage_subagents tool output when active subagent(s): appears."""
    if not content or (
        "active subagent" not in content.lower()
        and "active sub-agent" not in content.lower()
    ):
        return []

    results: list[dict[str, str]] = []

    # 1. Try to find and parse JSON array
    json_match = re.search(r"\[\s*\{.*?\}\s*\]", content, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        cid = _clean_arg_str(
                            item.get("conversationId")
                            or item.get("conversation_id")
                            or item.get("id")
                            or ""
                        )
                        if cid:
                            role = _clean_arg_str(
                                item.get("role") or item.get("Role") or ""
                            )
                            sub_type = _clean_arg_str(
                                item.get("type") or item.get("TypeName") or ""
                            )
                            model = _clean_arg_str(
                                item.get("model") or item.get("Model") or ""
                            )
                            results.append(
                                {
                                    "id": cid,
                                    "conversationId": cid,
                                    "role": role or "Autonomous Worker",
                                    "type": sub_type or "self",
                                    "model": model or "inherit",
                                    "Role": role or "Autonomous Worker",
                                    "TypeName": sub_type or "self",
                                    "Model": model or "inherit",
                                }
                            )
        except Exception:
            pass

    if results:
        return results

    # 2. Line-by-line regex extraction for text or bullet points
    for line in content.splitlines():
        cid_m = re.search(
            r'\b(?:conversation_?id|cid|id)["\':\s]+([a-zA-Z0-9_\-]+)',
            line,
            re.IGNORECASE,
        )
        if not cid_m:
            cid_m = re.search(
                r"\b([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b",
                line,
                re.IGNORECASE,
            )
        if cid_m:
            cid = cid_m.group(1).strip()
            role_m = re.search(r'\brole["\':\s]+([^,\n\)]+)', line, re.IGNORECASE)
            type_m = re.search(
                r'\btype(?:Name)?["\':\s]+([^,\n\)]+)', line, re.IGNORECASE
            )
            model_m = re.search(r'\bmodel["\':\s]+([^,\n\)]+)', line, re.IGNORECASE)

            role = _clean_arg_str(role_m.group(1)) if role_m else ""
            sub_type = _clean_arg_str(type_m.group(1)) if type_m else ""
            model = _clean_arg_str(model_m.group(1)) if model_m else ""
            results.append(
                {
                    "id": cid,
                    "conversationId": cid,
                    "role": role or "Autonomous Worker",
                    "type": sub_type or "self",
                    "model": model or "inherit",
                    "Role": role or "Autonomous Worker",
                    "TypeName": sub_type or "self",
                    "Model": model or "inherit",
                }
            )

    return results


def classify_phase(step_type: str, tool_name: str) -> str:
    """Categorizes a step into one of 4 development phases."""
    s_upper = step_type.upper()
    t_lower = tool_name.lower() if tool_name else ""

    if t_lower in (
        "view_file",
        "grep_search",
        "find_by_name",
        "list_dir",
        "search_web",
        "read_url_content",
        "read_resource",
        "list_resources",
    ) or s_upper in (
        "VIEW_FILE",
        "GREP_SEARCH",
        "FIND",
        "LIST_DIRECTORY",
        "SEARCH_WEB",
        "READ_URL_CONTENT",
    ):
        return "discovery"

    if t_lower in (
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "generate_image",
        "create_or_update_file",
        "push_files",
    ) or s_upper in ("CODE_ACTION", "GENERATE_IMAGE"):
        return "execution"

    if (
        t_lower
        in (
            "run_command",
            "manage_task",
            "command_status",
            "invoke_subagent",
            "send_message",
            "manage_subagents",
        )
        or t_lower.startswith("mcp_")
        or s_upper in ("RUN_COMMAND", "INVOKE_SUBAGENT", "SYSTEM_MESSAGE")
    ):
        return "verification"

    return "reasoning"


def parse_conversation(
    conv_dir: Path,
    *,
    include_tool_schemas: bool = False,
) -> dict | None:
    transcript_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_path.exists():
        transcript_path = (
            conv_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
        )
    if not transcript_path.exists():
        transcript_path = conv_dir / "transcript.jsonl"
    if not transcript_path.exists():
        return None

    cid = conv_dir.name
    first_model = None

    steps = []
    tool_execs = []
    pending_tool_calls = []
    seen_files = set()
    parsed_blocks = []
    block_parser = BlockParser()
    title = ""
    start_time = None
    end_time = None
    total_chars = 0
    total_thinking_tokens = 0
    user_prompts = 0
    redundant_reads = 0
    lines_written = 0
    lines_edited = 0
    failed_tool_calls = 0
    project = None
    cur_model = DEFAULT_MODEL
    models_used = []
    model_transitions = []
    detected_parent_id = None
    dispatched_subagents = []
    pending_subagent_specs = []

    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue

            step_idx = step.get("step_index", len(steps))
            parsed_blocks.extend(block_parser.parse_step(step, step_idx))

            source = step.get("source", "UNKNOWN")
            step_type = step.get("type", "UNKNOWN")
            created_at = step.get("created_at", "")
            content = step.get("content", "")
            thinking = step.get("thinking", "")
            tool_calls = step.get("tool_calls", [])
            exit_code = step.get("exit_code")
            is_error = (
                1
                if (
                    step.get("error")
                    or step.get("status") == "ERROR"
                    or (exit_code is not None and exit_code != 0)
                )
                else 0
            )

            # Detect mid-session model switch
            if step_type == "USER_INPUT":
                switched_model = extract_model_switch(content)
                if switched_model:
                    if first_model is None:
                        first_model = switched_model
                        for s in steps:
                            if s.get("model_name") == DEFAULT_MODEL:
                                s["model_name"] = first_model
                        cur_model = switched_model
                        models_used = [cur_model]
                        model_transitions = [cur_model]
                    else:
                        cur_model = switched_model
                        if not model_transitions or model_transitions[-1] != cur_model:
                            model_transitions.append(cur_model)
                        if cur_model not in models_used:
                            models_used.append(cur_model)

            if not project and content:
                project = extract_project_from_text(content)

            if is_error:
                failed_tool_calls += 1

            if created_at:
                if start_time is None:
                    start_time = created_at
                end_time = created_at

            content_len = len(content)
            thinking_len = len(thinking)
            tool_calls_len = len(json.dumps(tool_calls)) if tool_calls else 0
            step_chars = content_len + thinking_len + tool_calls_len
            est_tokens = (
                step.get("est_tokens")
                if step.get("est_tokens") is not None
                else int(step_chars / CHARS_PER_TOKEN)
            )
            step_thinking_tokens = (
                int(thinking_len / CHARS_PER_TOKEN) if thinking_len else 0
            )
            total_chars += step_chars
            total_thinking_tokens += step_thinking_tokens

            activity = step_type
            tool_or_source = source
            category = step_type
            detail = ""
            primary_tool_name = ""

            if step_type == "PLANNER_RESPONSE":
                activity = "Agent Reasoning & Text"
                tool_or_source = "Agent"
                category = "Agent Reasoning & Text"
                if tool_calls:
                    pending_tool_calls = [
                        (tc.get("name", ""), tc.get("args", {}))
                        for tc in tool_calls
                        if isinstance(tc, dict)
                    ]
                    tool_names = [tc[0] for tc in pending_tool_calls]
                    primary_tool_name = tool_names[0] if tool_names else ""
                    detail = f"Tools called: {', '.join(tool_names)}"

                    # Record tool executions and volume metrics
                    for tc_name, tc_args in pending_tool_calls:
                        target_p = (
                            tc_args.get("Path")
                            or tc_args.get("path")
                            or tc_args.get("TargetFile")
                            or tc_args.get("target_file")
                            or tc_args.get("AbsolutePath")
                            or tc_args.get("SearchPath")
                            or tc_args.get("DirectoryPath")
                            or ""
                        )

                        proj_path = (
                            target_p or tc_args.get("Cwd") or tc_args.get("cwd") or ""
                        )
                        if not proj_path and tc_args.get("CommandLine"):
                            proj_path = tc_args.get("CommandLine")
                        if not project:
                            project = extract_project_from_tool_args(tc_args)

                        cmd_line = tc_args.get("CommandLine", "")
                        clean_path = str(target_p).strip("\"'") if target_p else None
                        clean_cmd = str(cmd_line).strip("\"'") if cmd_line else None

                        # Check redundant file reads
                        if tc_name == "invoke_subagent":
                            specs = extract_subagent_specs(tc_args)
                            if not specs:
                                specs = [
                                    {
                                        "role": "Autonomous Worker",
                                        "type": "self",
                                        "model": "inherit",
                                        "Role": "Autonomous Worker",
                                        "TypeName": "self",
                                        "Model": "inherit",
                                    }
                                ]
                            pending_subagent_specs.extend(specs)
                        elif tc_name == "view_file" and clean_path:
                            norm_path = clean_path.lower()
                            if norm_path in seen_files:
                                redundant_reads += 1
                            else:
                                seen_files.add(norm_path)
                        elif tc_name == "write_to_file":
                            code = tc_args.get("CodeContent", "")
                            lines_written += len(code.splitlines())
                        elif tc_name in (
                            "replace_file_content",
                            "multi_replace_file_content",
                        ):
                            rep = tc_args.get("ReplacementContent", "")
                            lines_edited += len(rep.splitlines())

                        mcp_server = None
                        mcp_tool = None
                        stored_tool_name = tc_name

                        if tc_name == "call_mcp_tool":
                            server = str(tc_args.get("ServerName", "")).strip("\"'")
                            t_name = str(tc_args.get("ToolName", "")).strip("\"'")
                            if server and t_name:
                                mcp_server = server
                                mcp_tool = t_name
                                stored_tool_name = f"mcp:{server}:{t_name}"
                            elif server:
                                mcp_server = server
                        elif tc_name.startswith("mcp_"):
                            parts = tc_name.split("_", 2)
                            mcp_server = parts[1] if len(parts) > 1 else "mcp"
                            mcp_tool = parts[2] if len(parts) > 2 else tc_name
                            stored_tool_name = tc_name

                        tool_execs.append(
                            {
                                "conversation_id": cid,
                                "step_index": step_idx,
                                "tool_name": stored_tool_name,
                                "mcp_server": mcp_server,
                                "mcp_tool": mcp_tool,
                                "target_path": clean_path,
                                "command_line": clean_cmd,
                                "exit_code": exit_code,
                                "is_error": is_error,
                            }
                        )
                else:
                    pending_tool_calls = []
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")

            elif step_type == "USER_INPUT":
                user_prompts += 1
                activity = "User Request"
                tool_or_source = "User"
                category = "User Requests"
                pending_tool_calls = []
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")
                if not title and clean:
                    title = clean[:50] + ("..." if len(clean) > 50 else "")

            elif step_type in ("EPHEMERAL_MESSAGE", "CHECKPOINT"):
                activity = "Context Bar / Checkpoint"
                tool_or_source = "System"
                category = "System: Ephemeral Injections"
                pending_tool_calls = []
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:50] + ("..." if len(clean) > 50 else "")

            elif step_type == "SYSTEM_MESSAGE":
                activity = "Background Task Output"
                tool_or_source = "Task"
                category = "System: Background Task Outputs"
                pending_tool_calls = []
                detail = extract_system_message_detail(content)
                if step_idx <= 2 and not detected_parent_id:
                    m_snd = re.search(
                        r"\[Message\]\s+timestamp=[^\s]+\s+sender=([a-zA-Z0-9_\-]+)",
                        content,
                    )
                    if m_snd:
                        detected_parent_id = m_snd.group(1)

            elif step_type == "CONVERSATION_HISTORY":
                activity = "History Compaction"
                tool_or_source = "System"
                category = "System: Conversation History"
                pending_tool_calls = []
                detail = "Compacted conversation context"

            elif pending_tool_calls:
                tool_info = pending_tool_calls.pop(0)
                tool_name = tool_info[0]
                tool_args = tool_info[1]
                primary_tool_name = tool_name

                if (
                    tool_name == "invoke_subagent"
                    or "Created the following subagent" in content
                    or "Created the following subagents:" in content
                ) and tool_name not in (
                    "run_command",
                    "view_file",
                    "search_web",
                    "manage_task",
                ):
                    activity = "Subagent Dispatched"
                    tool_or_source = "`invoke_subagent`"
                    category = "Tool Output: `invoke_subagent`"
                    found_cids = re.findall(
                        r'conversation(?:_?id)?["\':\s]+([a-zA-Z0-9_\-]+)',
                        content,
                        re.IGNORECASE,
                    )
                    if not found_cids:
                        found_cids = re.findall(
                            r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
                            content,
                        )
                    found_cids = list(dict.fromkeys(found_cids))
                    if not found_cids and is_error and pending_subagent_specs:
                        pending_subagent_specs.pop(0)

                    for ch_id in found_cids:
                        spec = (
                            pending_subagent_specs.pop(0)
                            if pending_subagent_specs
                            else {}
                        )
                        dispatched_subagents.append(
                            {
                                "id": ch_id,
                                "role": spec.get("role")
                                or spec.get("Role")
                                or "Autonomous Worker",
                                "type": spec.get("type")
                                or spec.get("TypeName")
                                or "self",
                                "model": spec.get("model")
                                or spec.get("Model")
                                or "inherit",
                                "step_index": step_idx,
                            }
                        )
                    detail = (
                        f"Dispatched {len(found_cids)} subagent(s)"
                        if found_cids
                        else "Subagent dispatch"
                    )
                elif (
                    tool_name == "manage_subagents"
                    or "active subagent" in content.lower()
                    or "active sub-agent" in content.lower()
                ) and tool_name not in (
                    "run_command",
                    "view_file",
                    "search_web",
                    "manage_task",
                ):
                    activity = "Subagent Status"
                    tool_or_source = "`manage_subagents`"
                    category = "Tool Output: `manage_subagents`"
                    parsed_agents = parse_manage_subagents_output(content)
                    for pa in parsed_agents:
                        cid_found = pa.get("id") or pa.get("conversationId")
                        if not cid_found:
                            continue
                        matched = False
                        for ds in dispatched_subagents:
                            if ds.get("id") == cid_found:
                                if pa.get("role") and (
                                    ds.get("role") in (None, "", "Autonomous Worker")
                                    or pa.get("role") != "Autonomous Worker"
                                ):
                                    ds["role"] = pa["role"]
                                if pa.get("type"):
                                    ds["type"] = pa["type"]
                                if pa.get("model"):
                                    ds["model"] = pa["model"]
                                matched = True
                                break
                        if not matched:
                            dispatched_subagents.append(
                                {
                                    "id": cid_found,
                                    "role": pa.get("role", "Autonomous Worker"),
                                    "type": pa.get("type", "self"),
                                    "model": pa.get("model", "inherit"),
                                    "step_index": step_idx,
                                }
                            )
                    detail = (
                        f"Active subagent(s): {len(parsed_agents)}"
                        if parsed_agents
                        else "Manage subagents"
                    )
                elif tool_name == "view_file" or "File Path:" in content:
                    activity = "File Content"
                    tool_or_source = "`view_file`"
                    category = "Tool Output: File Contents (`view_file`)"
                    if "AbsolutePath" in tool_args:
                        target = str(tool_args["AbsolutePath"]).strip("\"'")
                        detail = f"File: {Path(target).name}"
                    else:
                        target = extract_file_basename(content)
                        detail = f"File: {target}"
                elif tool_name == "run_command" or "The command exited" in content:
                    activity = "Shell Output"
                    tool_or_source = "`run_command`"
                    category = "Tool Output: Shell Execution (`run_command`)"
                    cmd = str(tool_args.get("CommandLine", "")).strip("\"' ")
                    detail = (
                        f"Cmd: {cmd[:40]}..."
                        if len(cmd) > 40
                        else (f"Cmd: {cmd}" if cmd else "Shell stdout/stderr")
                    )
                elif (
                    tool_name == "list_dir"
                    or "Summary: This directory contains" in content
                ):
                    activity = "Directory Listing"
                    tool_or_source = "`list_dir`"
                    category = "Tool Output: Directory Listings (`list_dir`)"
                    dir_path = str(tool_args.get("DirectoryPath", "")).strip("\"' ")
                    detail = (
                        f"Dir: {Path(dir_path).name or dir_path}"
                        if dir_path
                        else "Directory listing"
                    )
                elif tool_name == "find_by_name" or (
                    "Found " in content and "results" in content
                ):
                    activity = "File Search"
                    tool_or_source = "`find_by_name`"
                    category = "Tool Output: File Search (`find_by_name`)"
                    pattern = str(tool_args.get("Pattern", "")).strip("\"' ")
                    detail = f"Pattern: {pattern}" if pattern else "File search"
                elif tool_name == "grep_search" or "LineNumber" in content:
                    activity = "Pattern Search"
                    tool_or_source = "`grep_search`"
                    category = "Tool Output: Pattern Search (`grep_search`)"
                    query = str(tool_args.get("Query", "")).strip("\"' ")
                    detail = f"Grep: {query}" if query else "Ripgrep search"
                elif tool_name == "write_to_file" or "Created file" in content:
                    activity = "File Write"
                    tool_or_source = "`write_to_file`"
                    category = "Tool Output: `write_to_file`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"' ")
                    detail = (
                        f"File: {Path(target).name}" if target else "Write confirmation"
                    )
                elif (
                    tool_name == "replace_file_content"
                    or "The following changes were made" in content
                ):
                    activity = "File Edit"
                    tool_or_source = "`replace_file_content`"
                    category = "Tool Output: `replace_file_content`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"' ")
                    detail = (
                        f"Edit: {Path(target).name}" if target else "Edit diff block"
                    )
                elif tool_name.startswith("mcp_"):
                    activity = f"MCP Tool ({tool_name})"
                    tool_or_source = f"`{tool_name}`"
                    category = "Tool Output: MCP Tools"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
                else:
                    activity = f"Tool: {tool_name}"
                    tool_or_source = f"`{tool_name}`"
                    category = "Tool Output: Other"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
            else:
                pending_tool_calls = []
                activity = step_type
                tool_or_source = source
                category = step_type
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")

            phase = classify_phase(step_type, primary_tool_name)

            steps.append(
                {
                    "conversation_id": cid,
                    "step_index": step_idx,
                    "created_at": created_at,
                    "source": source,
                    "step_type": step_type,
                    "category": category,
                    "activity": activity,
                    "tool_or_source": tool_or_source,
                    "characters": step_chars,
                    "est_tokens": est_tokens,
                    "thinking_tokens": step_thinking_tokens,
                    "phase": phase,
                    "model_name": cur_model,
                    "detail": detail,
                }
            )

    duration_seconds, active_agent_seconds, active_pairing_seconds = (
        compute_session_timings(steps, is_subagent=bool(detected_parent_id))
    )

    total_tokens = int(total_chars / CHARS_PER_TOKEN)

    if first_model is None:
        first_model = cur_model
        if not models_used:
            models_used = [cur_model]
        if not model_transitions:
            model_transitions = [cur_model]

    # Append dynamically loaded tool schemas if explicitly requested
    if include_tool_schemas:
        parsed_blocks.extend(block_parser.load_tool_schemas())

    # Calculate billed cost based on steps
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0

    compaction_count = 0
    pruned_tokens = 0

    for s in steps:
        m_name = s.get("model_name", cur_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)

        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
            # Context truncation: reset running_ctx to checkpoint tokens
            compaction_count += 1
            if running_ctx > tokens:
                pruned_tokens += running_ctx - tokens
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            # Transient injection for immediate turn only
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            step_cost, step_in_tokens, step_out_tokens = calculate_step_cost(
                running_ctx, current_ephemeral, tokens, m_name
            )
            total_billed_cost += step_cost
            total_billed_input_tokens += step_in_tokens
            total_billed_output_tokens += step_out_tokens
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

        s["billed_cost"] = step_cost
        s["billed_input_tokens"] = step_in_tokens
        s["billed_output_tokens"] = step_out_tokens

    if not models_used and cur_model:
        models_used = [cur_model]
    if not model_transitions and cur_model:
        model_transitions = [cur_model]

    if len(model_transitions) > 1:
        if len(model_transitions) <= 3:
            conv_model_name = " → ".join(model_transitions)
        else:
            conv_model_name = f"{model_transitions[0]} → ... → {model_transitions[-1]}"
        models_used_str = ", ".join(models_used)
    elif models_used:
        conv_model_name = models_used[0]
        models_used_str = models_used[0]
    else:
        conv_model_name = cur_model
        models_used_str = cur_model

    billed_tokens = total_billed_input_tokens + total_billed_output_tokens
    est_tok = running_ctx if running_ctx > 0 else total_tokens
    velocity = compute_active_velocity(
        est_tokens=est_tok,
        billed_tokens=billed_tokens,
        active_agent_seconds=active_agent_seconds,
        active_pairing_seconds=active_pairing_seconds,
    )

    return {
        "conversation": {
            "id": cid,
            "title": title or f"Conversation {cid[:8]}",
            "project": project,
            "model_name": conv_model_name,
            "models_used": models_used_str,
            "created_at": start_time or datetime.now().isoformat(),
            "updated_at": end_time or datetime.now().isoformat(),
            "total_steps": len(steps),
            "total_characters": total_chars,
            "total_tokens": total_tokens,
            "active_context_tokens": running_ctx,
            "duration_seconds": duration_seconds,
            "active_agent_seconds": active_agent_seconds,
            "active_pairing_seconds": active_pairing_seconds,
            "velocity": velocity,
            "thinking_tokens": total_thinking_tokens,
            "user_prompts": user_prompts,
            "tool_calls_count": len(tool_execs),
            "redundant_file_reads": redundant_reads,
            "lines_written": lines_written,
            "lines_edited": lines_edited,
            "failed_tool_calls": failed_tool_calls,
            "billed_cost": total_billed_cost,
            "billed_input_tokens": total_billed_input_tokens,
            "billed_output_tokens": total_billed_output_tokens,
            "parent_id": detected_parent_id,
            "dispatched_subagents": dispatched_subagents,
            "compaction_count": compaction_count,
            "pruned_tokens": pruned_tokens,
        },
        "steps": steps,
        "tool_executions": tool_execs,
        "parsed_blocks": parsed_blocks,
    }


def run_backfill(
    brain_dir: Path = Path(os.path.expanduser("~/.gemini/antigravity/brain")),
    conversation_id: str | None = None,
    force: bool = False,
    db_path: Path | None = None,
):
    target_db = Path(db_path) if db_path else DEFAULT_DB_PATH
    init_db(target_db)

    if not brain_dir.exists():
        print(f"Brain directory not found at {brain_dir}")
        return

    t_start = time.time()
    if conversation_id:
        target = brain_dir / conversation_id
        if not target.exists():
            print(f"Conversation directory not found: {target}")
            return
        conv_dirs = [target]
    else:
        conv_dirs = [
            p for p in brain_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        ]

    total_convs = len(conv_dirs)
    print(
        "\n🚀 Starting Antigravity Analytics Back-Fill (Hierarchy & Swarm Edition)...",
        flush=True,
    )
    print(f"📁 Source: {brain_dir}", flush=True)
    print(f"📊 Found:  {total_convs} total conversation transcripts", flush=True)

    # Pass 1: Parse all conversations and build hierarchy maps
    data_list = []
    data_by_id = {}
    parent_map = {}
    subagent_meta = {}

    print(
        "  [Pass 1/2] Parsing transcripts and discovering agent lineage...", flush=True
    )
    for i, cdir in enumerate(conv_dirs, 1):
        data = parse_conversation(cdir)
        if not data:
            continue
        cid = data["conversation"]["id"]
        data_list.append(data)
        data_by_id[cid] = data

        p_id = data["conversation"].get("parent_id")
        if p_id:
            parent_map[cid] = p_id

        for ch in data["conversation"].get("dispatched_subagents", []):
            parent_map[ch["id"]] = cid
            subagent_meta[ch["id"]] = ch

    # Pass 2: Resolve root_id, depth, project inheritance, semantic titles, and commit to SQLite
    total_parsed = len(data_list)
    print(
        f"  [Pass 2/2] Resolving swarm graph lineages, inheriting metadata, and writing to SQLite ({total_parsed} conversations)...",
        flush=True,
    )
    conn = get_db_connection(target_db)
    processed_convs = 0
    total_steps_inserted = 0
    total_tools_inserted = 0
    total_tokens_computed = 0

    try:
        with conn:
            # Pre-resolve swarm root_id and map swarm projects
            swarm_project_map = {}
            for data in data_list:
                c_conv = data["conversation"]
                c_id = c_conv["id"]
                c_curr = c_id
                c_depth = 0
                c_visited = set()
                while c_curr in parent_map and c_curr not in c_visited and c_depth < 25:
                    c_visited.add(c_curr)
                    c_curr = parent_map[c_curr]
                    c_depth += 1
                c_root = c_curr
                c_proj = c_conv.get("project")
                if c_proj:
                    if c_root == c_id:
                        swarm_project_map[c_root] = c_proj
                    elif c_root not in swarm_project_map:
                        swarm_project_map[c_root] = c_proj

            # Fallback to existing DB projects and lineages for roots, swarms, and parent maps
            try:
                db_swarms = conn.execute(
                    """
                    SELECT id, root_id, project
                    FROM conversations
                    WHERE project IS NOT NULL AND project != ''
                      AND lower(project) NOT IN ('(unassigned)', 'unassigned')
                    """
                ).fetchall()
                for r in db_swarms:
                    try:
                        r_row_id = r["id"]
                        r_id = r["root_id"] or r_row_id
                        p_val = normalize_project_name(r["project"])
                    except (IndexError, TypeError):
                        r_row_id = r[0]
                        r_id = r[1] or r[0]
                        p_val = normalize_project_name(r[2])
                    if r_id and p_val:
                        if r_id == r_row_id and (
                            r_id not in swarm_project_map
                            or swarm_project_map.get(r_id) is None
                        ):
                            swarm_project_map[r_id] = p_val
                        elif r_id not in swarm_project_map:
                            swarm_project_map[r_id] = p_val

                db_lineages = conn.execute(
                    """
                    SELECT id, parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
                    FROM conversations
                    WHERE parent_id IS NOT NULL OR subagent_role IS NOT NULL
                    """
                ).fetchall()
                for el in db_lineages:
                    try:
                        e_id = el["id"]
                        e_pid = el["parent_id"]
                        e_role = el["subagent_role"]
                        e_type = el["subagent_type"]
                        e_step = el["invoking_step_index"]
                    except (IndexError, TypeError):
                        e_id = el[0]
                        e_pid = el[1]
                        e_role = el[4]
                        e_type = el[5]
                        e_step = el[6]
                    if e_pid and e_id not in parent_map:
                        parent_map[e_id] = e_pid
                    if e_role and e_id not in subagent_meta:
                        subagent_meta[e_id] = {
                            "role": e_role,
                            "type": e_type,
                            "step_index": e_step,
                        }
            except Exception:
                pass

            for i, data in enumerate(data_list, 1):
                conv = data["conversation"]
                cid = conv["id"]
                steps = data["steps"]
                tools = data["tool_executions"]

                # Walk ancestors to determine root_id and depth
                curr = cid
                depth = 0
                visited = set()
                while curr in parent_map and curr not in visited and depth < 25:
                    visited.add(curr)
                    curr = parent_map[curr]
                    depth += 1

                root_id = curr
                parent_id = parent_map.get(cid)
                conv["parent_id"] = parent_id
                conv["root_id"] = root_id
                conv["depth"] = depth

                meta = subagent_meta.get(cid, {})
                conv["subagent_role"] = meta.get("role") or conv.get("subagent_role")
                conv["subagent_type"] = meta.get("type") or conv.get("subagent_type")
                conv["invoking_step_index"] = meta.get("step_index") or conv.get(
                    "invoking_step_index"
                )

                if (
                    parent_id is not None
                    or depth > 0
                    or conv.get("subagent_role") is not None
                ):
                    conv["active_pairing_seconds"] = 0.0
                    data["active_pairing_seconds"] = 0.0
                    conv["velocity"] = compute_active_velocity(
                        est_tokens=conv.get(
                            "active_context_tokens", conv.get("total_tokens", 0)
                        ),
                        billed_tokens=(
                            conv.get("billed_input_tokens", 0)
                            + conv.get("billed_output_tokens", 0)
                        ),
                        active_agent_seconds=conv.get("active_agent_seconds", 0.0),
                        active_pairing_seconds=0.0,
                    )
                    data["velocity"] = conv["velocity"]
                elif "velocity" not in conv or not conv["velocity"]:
                    conv["velocity"] = compute_active_velocity(
                        est_tokens=conv.get(
                            "active_context_tokens", conv.get("total_tokens", 0)
                        ),
                        billed_tokens=(
                            conv.get("billed_input_tokens", 0)
                            + conv.get("billed_output_tokens", 0)
                        ),
                        active_agent_seconds=conv.get("active_agent_seconds", 0.0),
                        active_pairing_seconds=conv.get("active_pairing_seconds", 0.0),
                    )
                    data["velocity"] = conv["velocity"]

                # Inherit project from swarm or ancestors if missing
                if not conv.get("project"):
                    conv["project"] = swarm_project_map.get(root_id)

                if not conv.get("project") and depth > 0:
                    curr_ancestor = parent_id
                    while curr_ancestor:
                        anc_data = data_by_id.get(curr_ancestor)
                        if anc_data and anc_data["conversation"].get("project"):
                            conv["project"] = anc_data["conversation"]["project"]
                            break
                        curr_ancestor = parent_map.get(curr_ancestor)

                if conv.get("project"):
                    conv["project"] = normalize_project_name(conv["project"])

                # Generate clean semantic title if generic
                role = conv.get("subagent_role") or "Autonomous Worker"
                if depth > 0 and (
                    not conv.get("title")
                    or conv["title"] in ("Active Session", "")
                    or conv["title"].startswith("Conversation ")
                    or (
                        conv["title"].startswith("[Autonomous Worker]")
                        and role != "Autonomous Worker"
                    )
                ):
                    root_title = "Task"
                    if root_id in data_by_id:
                        root_title = (
                            data_by_id[root_id]["conversation"].get("title") or "Task"
                        )
                    elif conv.get("title"):
                        root_title = conv["title"]
                    clean_root_title = re.sub(r"^(\[.+?\]\s*)+", "", root_title).strip()
                    conv["title"] = f"[{role}] {clean_root_title[:90]}"

                # Upsert conversation with hierarchy metrics
                conn.execute(
                    """
                    INSERT INTO conversations (
                        id, title, project, model_name, models_used, created_at, updated_at, total_steps, total_characters, total_tokens,
                        duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                        user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                        billed_cost, billed_input_tokens, billed_output_tokens,
                        parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                        compaction_count, pruned_tokens
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        project = COALESCE(excluded.project, conversations.project),
                        model_name = excluded.model_name,
                        models_used = excluded.models_used,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        total_steps = excluded.total_steps,
                        total_characters = excluded.total_characters,
                        total_tokens = excluded.total_tokens,
                        duration_seconds = excluded.duration_seconds,
                        active_agent_seconds = excluded.active_agent_seconds,
                        active_pairing_seconds = excluded.active_pairing_seconds,
                        thinking_tokens = excluded.thinking_tokens,
                        user_prompts = excluded.user_prompts,
                        tool_calls_count = excluded.tool_calls_count,
                        redundant_file_reads = excluded.redundant_file_reads,
                        lines_written = excluded.lines_written,
                        lines_edited = excluded.lines_edited,
                        failed_tool_calls = excluded.failed_tool_calls,
                        billed_cost = excluded.billed_cost,
                        billed_input_tokens = excluded.billed_input_tokens,
                        billed_output_tokens = excluded.billed_output_tokens,
                        parent_id = excluded.parent_id,
                        root_id = excluded.root_id,
                        depth = excluded.depth,
                        subagent_role = excluded.subagent_role,
                        subagent_type = excluded.subagent_type,
                        invoking_step_index = excluded.invoking_step_index,
                        compaction_count = excluded.compaction_count,
                        pruned_tokens = excluded.pruned_tokens
                """,
                    (
                        conv["id"],
                        conv["title"],
                        conv["project"],
                        conv["model_name"],
                        conv.get("models_used", conv["model_name"]),
                        conv["created_at"],
                        conv["updated_at"],
                        conv["total_steps"],
                        conv["total_characters"],
                        conv["total_tokens"],
                        conv["duration_seconds"],
                        conv["active_agent_seconds"],
                        conv["active_pairing_seconds"],
                        conv["thinking_tokens"],
                        conv["user_prompts"],
                        conv["tool_calls_count"],
                        conv["redundant_file_reads"],
                        conv["lines_written"],
                        conv["lines_edited"],
                        conv["failed_tool_calls"],
                        conv.get("billed_cost", 0.0),
                        conv.get("billed_input_tokens", 0),
                        conv.get("billed_output_tokens", 0),
                        conv["parent_id"],
                        conv["root_id"],
                        conv["depth"],
                        conv["subagent_role"],
                        conv["subagent_type"],
                        conv["invoking_step_index"],
                        conv.get("compaction_count", 0),
                        conv.get("pruned_tokens", 0),
                    ),
                )

                # Bulk insert steps (replace existing)
                conn.execute(
                    "DELETE FROM steps WHERE conversation_id = ?",
                    (conv["id"],),
                )
                conn.executemany(
                    """
                    INSERT INTO steps (
                        conversation_id, step_index, created_at, source, step_type, category, activity,
                        tool_or_source, characters, est_tokens, thinking_tokens, phase, model_name, detail,
                        billed_cost, billed_input_tokens, billed_output_tokens
                    )
                    VALUES (:conversation_id, :step_index, :created_at, :source, :step_type, :category, :activity,
                            :tool_or_source, :characters, :est_tokens, :thinking_tokens, :phase, :model_name, :detail,
                            :billed_cost, :billed_input_tokens, :billed_output_tokens)
                    ON CONFLICT(conversation_id, step_index) DO UPDATE SET
                        created_at = excluded.created_at,
                        source = excluded.source,
                        step_type = excluded.step_type,
                        category = excluded.category,
                        activity = excluded.activity,
                        tool_or_source = excluded.tool_or_source,
                        characters = excluded.characters,
                        est_tokens = excluded.est_tokens,
                        thinking_tokens = excluded.thinking_tokens,
                        phase = excluded.phase,
                        model_name = excluded.model_name,
                        detail = excluded.detail,
                        billed_cost = excluded.billed_cost,
                        billed_input_tokens = excluded.billed_input_tokens,
                        billed_output_tokens = excluded.billed_output_tokens
                """,
                    steps,
                )

                # Insert tool executions
                conn.execute(
                    "DELETE FROM tool_executions WHERE conversation_id = ?",
                    (conv["id"],),
                )
                conn.executemany(
                    """
                    INSERT INTO tool_executions (conversation_id, step_index, tool_name, mcp_server, mcp_tool, target_path, command_line, exit_code, is_error)
                    VALUES (:conversation_id, :step_index, :tool_name, :mcp_server, :mcp_tool, :target_path, :command_line, :exit_code, :is_error)
                """,
                    tools,
                )

                # Insert transcript blocks
                if data.get("parsed_blocks"):
                    conn.execute(
                        "DELETE FROM transcript_blocks WHERE conversation_id = ?",
                        (conv["id"],),
                    )
                    block_params = []
                    for b in data["parsed_blocks"]:
                        content_str = b.text
                        if b.arguments is not None:
                            import json

                            content_str += "\n" + json.dumps(b.arguments)

                        block_params.append(
                            (
                                conv["id"],
                                b.step_index,
                                b.kind.value,
                                content_str,
                                b.hash,
                                b.call_id,
                            )
                        )

                    conn.executemany(
                        """
                        INSERT INTO transcript_blocks (
                            conversation_id, step_index, block_type, content, block_hash, call_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        block_params,
                    )

                refresh_conversation_rollups(conn, conv["id"])
                processed_convs += 1
                total_steps_inserted += len(steps)
                total_tools_inserted += len(tools)
                total_tokens_computed += conv["total_tokens"]

                if i % 25 == 0 or i == total_parsed:
                    conn.commit()
                    pct = (i / total_parsed) * 100 if total_parsed else 100.0
                    print(
                        f"  [{pct:5.1f}%] Processed {i}/{total_parsed} conversations ({total_steps_inserted:,} steps)...",
                        flush=True,
                    )

        # Backfill projects for historical DB conversations and propagate swarm lineages
        try:
            backfill_db_projects(conn)
            conn.commit()
        except Exception:
            pass

        # Ensure all subagent records have active_pairing_seconds zeroed
        try:
            conn.execute(
                """
                UPDATE conversations
                SET active_pairing_seconds = 0.0
                WHERE (parent_id IS NOT NULL OR depth > 0 OR subagent_role IS NOT NULL)
                  AND active_pairing_seconds != 0.0
                """
            )
            conn.commit()
        except Exception:
            pass

        # Ensure all rollups are fully in sync with steps
        try:
            ensure_rollups_synced(conn, force=force)
            conn.commit()
        except Exception:
            pass

        # Calibrate and initialize quota state for historical backfill
        try:
            sync_and_get_quota_state(conn, force_refresh=True)
            conn.commit()
        except Exception:
            pass

        # Checkpoint WAL to truncate WAL file and reclaim disk space
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
    finally:
        conn.close()

    elapsed = time.time() - t_start
    print(f"\n✅ Back-fill complete in {elapsed:.2f}s!")
    print("-" * 50)
    print(f"  Total Conversations Stored : {processed_convs:,}")
    print(f"  Total Steps Logged         : {total_steps_inserted:,}")
    print(f"  Total Tool Calls Tracked   : {total_tools_inserted:,}")
    print(f"  Total Estimated Tokens     : {total_tokens_computed:,}")
    print(f"  Database Location          : {target_db}")
    print("-" * 50 + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Antigravity Historical Analytics Backfill"
    )
    parser.add_argument(
        "--conversation-id",
        type=str,
        default=None,
        help="Backfill a specific conversation ID",
    )
    parser.add_argument("--force", action="store_true", help="Force re-indexing")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Target SQLite database file path",
    )
    args = parser.parse_args()
    run_backfill(
        conversation_id=args.conversation_id, force=args.force, db_path=args.db_path
    )
