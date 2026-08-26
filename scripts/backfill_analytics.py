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

# Import db layer
import sys
from pathlib import Path
_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import get_db_connection, init_db, DEFAULT_DB_PATH
from context_monitor.formats import BlockParser

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
        line.strip() for line in content.splitlines()
        if line.strip() and not line.strip().startswith("<")
        and not line.strip().startswith("The following")
        and not line.strip().startswith("[Message]")
    ]
    snippet = lines[0][:40] if lines else ""
    return f"{sender} {code} {snippet}".strip()

def detect_model_from_transcript(transcript_path: Path) -> tuple[str, str, int]:
    first_model = "Gemini Flash"
    latest_model = "Gemini Flash"
    found_first = False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Model Selection" in line:
                    m = re.search(r"Model Selection\` from (?:.*?) to (.+?)\.\s*(?:No need|$)", line)
                    if m:
                        model = m.group(1).strip()
                        if not found_first:
                            first_model = model
                            found_first = True
                        latest_model = model
    except Exception:
        pass

    lower = latest_model.lower()
    if "pro" in lower:
        max_tokens = 2_000_000
    elif "flash" in lower or "gemini" in lower:
        max_tokens = 1_000_000
    elif "claude" in lower:
        max_tokens = 200_000
    elif "gpt" in lower or "o1" in lower or "o3" in lower:
        max_tokens = 128_000
    else:
        max_tokens = 1_000_000

    return first_model, latest_model, max_tokens

def classify_phase(step_type: str, tool_name: str) -> str:
    """Categorizes a step into one of 4 development phases."""
    s_upper = step_type.upper()
    t_lower = tool_name.lower() if tool_name else ""

    if t_lower in ("view_file", "grep_search", "find_by_name", "list_dir", "search_web", "read_url_content", "read_resource", "list_resources") or \
       s_upper in ("VIEW_FILE", "GREP_SEARCH", "FIND", "LIST_DIRECTORY", "SEARCH_WEB", "READ_URL_CONTENT"):
        return "discovery"
    
    if t_lower in ("write_to_file", "replace_file_content", "multi_replace_file_content", "generate_image", "create_or_update_file", "push_files") or \
       s_upper in ("CODE_ACTION", "GENERATE_IMAGE"):
        return "execution"

    if t_lower in ("run_command", "manage_task", "command_status", "invoke_subagent", "send_message", "manage_subagents") or \
       t_lower.startswith("mcp_") or \
       s_upper in ("RUN_COMMAND", "INVOKE_SUBAGENT", "SYSTEM_MESSAGE"):
        return "verification"

    return "reasoning"

def parse_conversation(conv_dir: Path) -> dict | None:
    transcript_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_path.exists():
        transcript_path = conv_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
    if not transcript_path.exists():
        return None

    cid = conv_dir.name
    first_model, latest_model, _ = detect_model_from_transcript(transcript_path)

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
    cur_model = first_model

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
            is_error = 1 if (step.get("error") or step.get("status") == "ERROR" or (exit_code is not None and exit_code != 0)) else 0

            # Detect mid-session model switch
            if "Model Selection" in content or (step_type == "USER_INPUT" and "<USER_SETTINGS_CHANGE>" in content):
                m = re.search(r"Model Selection\` from (?:.*?) to (.+?)\.\s*(?:No need|$)", content)
                if m:
                    cur_model = m.group(1).strip()

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
            est_tokens = int(step_chars / CHARS_PER_TOKEN)
            step_thinking_tokens = int(thinking_len / CHARS_PER_TOKEN) if thinking_len else 0
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
                        for tc in tool_calls if isinstance(tc, dict)
                    ]
                    tool_names = [tc[0] for tc in pending_tool_calls]
                    primary_tool_name = tool_names[0] if tool_names else ""
                    detail = f"Tools called: {', '.join(tool_names)}"
                    
                    # Record tool executions and volume metrics
                    for tc_name, tc_args in pending_tool_calls:
                        target_p = tc_args.get("TargetFile") or tc_args.get("AbsolutePath") or tc_args.get("SearchPath") or tc_args.get("DirectoryPath") or ""
                        cmd_line = tc_args.get("CommandLine", "")
                        clean_path = str(target_p).strip("\"'") if target_p else None
                        clean_cmd = str(cmd_line).strip("\"'") if cmd_line else None
                        
                        # Check redundant file reads
                        if tc_name == "view_file" and clean_path:
                            norm_path = clean_path.lower()
                            if norm_path in seen_files:
                                redundant_reads += 1
                            else:
                                seen_files.add(norm_path)
                        elif tc_name == "write_to_file":
                            code = tc_args.get("CodeContent", "")
                            lines_written += len(code.splitlines())
                        elif tc_name in ("replace_file_content", "multi_replace_file_content"):
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

                        tool_execs.append({
                            "conversation_id": cid,
                            "step_index": step_idx,
                            "tool_name": stored_tool_name,
                            "mcp_server": mcp_server,
                            "mcp_tool": mcp_tool,
                            "target_path": clean_path,
                            "command_line": clean_cmd,
                            "exit_code": exit_code,
                            "is_error": is_error
                        })
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

                if tool_name == "view_file" or "File Path:" in content:
                    activity = "File Content"
                    tool_or_source = "`view_file`"
                    category = "Tool Output: File Contents (`view_file`)"
                    if "AbsolutePath" in tool_args:
                        target = str(tool_args['AbsolutePath']).strip("\"'")
                        detail = f"File: {Path(target).name}"
                    else:
                        target = extract_file_basename(content)
                        detail = f"File: {target}"
                elif tool_name == "run_command" or "The command exited" in content:
                    activity = "Shell Output"
                    tool_or_source = "`run_command`"
                    category = "Tool Output: Shell Execution (`run_command`)"
                    cmd = str(tool_args.get("CommandLine", "")).strip("\"' ")
                    detail = f"Cmd: {cmd[:40]}..." if len(cmd) > 40 else (f"Cmd: {cmd}" if cmd else "Shell stdout/stderr")
                elif tool_name == "list_dir" or "Summary: This directory contains" in content:
                    activity = "Directory Listing"
                    tool_or_source = "`list_dir`"
                    category = "Tool Output: Directory Listings (`list_dir`)"
                    dir_path = str(tool_args.get("DirectoryPath", "")).strip("\"' ")
                    detail = f"Dir: {Path(dir_path).name or dir_path}" if dir_path else "Directory listing"
                elif tool_name == "find_by_name" or ("Found " in content and "results" in content):
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
                    detail = f"File: {Path(target).name}" if target else "Write confirmation"
                elif tool_name == "replace_file_content" or "The following changes were made" in content:
                    activity = "File Edit"
                    tool_or_source = "`replace_file_content`"
                    category = "Tool Output: `replace_file_content`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"' ")
                    detail = f"Edit: {Path(target).name}" if target else "Edit diff block"
                elif tool_name.startswith("mcp_"):
                    activity = f"MCP Tool ({tool_name})"
                    tool_or_source = f"`{tool_name}`"
                    category = f"Tool Output: MCP Tools"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
                else:
                    activity = f"Tool: {tool_name}"
                    tool_or_source = f"`{tool_name}`"
                    category = f"Tool Output: Other"
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

            steps.append({
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
                "detail": detail
            })

    duration_seconds = 0.0
    active_agent_seconds = 0.0
    active_pairing_seconds = 0.0
    
    start_dt = None
    end_dt = None
    prev_dt = None
    cur_turn_start = None

    for s in steps:
        c_at = s.get("created_at")
        s_type = s.get("step_type")
        if not c_at:
            continue
        try:
            dt = datetime.fromisoformat(c_at.replace("Z", "+00:00"))
        except Exception:
            continue

        if start_dt is None:
            start_dt = dt
        end_dt = dt

        if prev_dt is not None:
            delta = (dt - prev_dt).total_seconds()
            if 0 < delta <= 300:
                active_pairing_seconds += delta

        if s_type == "USER_INPUT":
            if cur_turn_start is not None and prev_dt is not None:
                turn_dur = (prev_dt - cur_turn_start).total_seconds()
                if 0 < turn_dur <= 1800:
                    active_agent_seconds += turn_dur
            cur_turn_start = dt
        elif s_type in ("PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE"):
            if cur_turn_start is None:
                cur_turn_start = dt

        prev_dt = dt

    if cur_turn_start is not None and end_dt is not None:
        turn_dur = (end_dt - cur_turn_start).total_seconds()
        if 0 < turn_dur <= 1800:
            active_agent_seconds += turn_dur

    if start_dt and end_dt:
        duration_seconds = max((end_dt - start_dt).total_seconds(), 0.0)

    total_tokens = int(total_chars / CHARS_PER_TOKEN)

    # Append dynamically loaded tool schemas
    parsed_blocks.extend(block_parser.load_tool_schemas())

    return {
        "conversation": {
            "id": cid,
            "title": title or f"Conversation {cid[:8]}",
            "model_name": cur_model,
            "created_at": start_time or datetime.now().isoformat(),
            "updated_at": end_time or datetime.now().isoformat(),
            "total_steps": len(steps),
            "total_characters": total_chars,
            "total_tokens": total_tokens,
            "duration_seconds": duration_seconds,
            "active_agent_seconds": active_agent_seconds,
            "active_pairing_seconds": active_pairing_seconds,
            "thinking_tokens": total_thinking_tokens,
            "user_prompts": user_prompts,
            "tool_calls_count": len(tool_execs),
            "redundant_file_reads": redundant_reads,
            "lines_written": lines_written,
            "lines_edited": lines_edited,
            "failed_tool_calls": failed_tool_calls
        },
        "steps": steps,
        "tool_executions": tool_execs,
        "parsed_blocks": parsed_blocks
    }

def run_backfill(brain_dir: Path = Path(os.path.expanduser("~/.gemini/antigravity/brain"))):
    init_db()
    
    if not brain_dir.exists():
        print(f"Brain directory not found at {brain_dir}")
        return

    t_start = time.time()
    conv_dirs = [p for p in brain_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    total_convs = len(conv_dirs)
    print(f"\n🚀 Starting Antigravity Analytics Back-Fill (Efficiency & Velocity Edition)...")
    print(f"📁 Source: {brain_dir}")
    print(f"📊 Found:  {total_convs} total conversation transcripts")

    conn = get_db_connection()
    
    processed_convs = 0
    total_steps_inserted = 0
    total_tools_inserted = 0
    total_tokens_computed = 0

    try:
        with conn:
            for i, cdir in enumerate(conv_dirs, 1):
                data = parse_conversation(cdir)
                if not data:
                    continue

                conv = data["conversation"]
                steps = data["steps"]
                tools = data["tool_executions"]

                # Upsert conversation with new metrics
                conn.execute("""
                    INSERT INTO conversations (
                        id, title, model_name, created_at, updated_at, total_steps, total_characters, total_tokens,
                        duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                        user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        model_name = excluded.model_name,
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
                        failed_tool_calls = excluded.failed_tool_calls
                """, (
                    conv["id"], conv["title"], conv["model_name"], conv["created_at"],
                    conv["updated_at"], conv["total_steps"], conv["total_characters"],
                    conv["total_tokens"], conv["duration_seconds"],
                    conv["active_agent_seconds"], conv["active_pairing_seconds"],
                    conv["thinking_tokens"], conv["user_prompts"], conv["tool_calls_count"],
                    conv["redundant_file_reads"], conv["lines_written"], conv["lines_edited"],
                    conv["failed_tool_calls"]
                ))

                # Bulk insert steps (replace existing)
                conn.executemany("""
                    INSERT INTO steps (
                        conversation_id, step_index, created_at, source, step_type, category, activity,
                        tool_or_source, characters, est_tokens, thinking_tokens, phase, model_name, detail
                    )
                    VALUES (:conversation_id, :step_index, :created_at, :source, :step_type, :category, :activity,
                            :tool_or_source, :characters, :est_tokens, :thinking_tokens, :phase, :model_name, :detail)
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
                        detail = excluded.detail
                """, steps)

                # Insert tool executions
                conn.execute("DELETE FROM tool_executions WHERE conversation_id = ?", (conv["id"],))
                conn.executemany("""
                    INSERT INTO tool_executions (conversation_id, step_index, tool_name, mcp_server, mcp_tool, target_path, command_line, exit_code, is_error)
                    VALUES (:conversation_id, :step_index, :tool_name, :mcp_server, :mcp_tool, :target_path, :command_line, :exit_code, :is_error)
                """, tools)

                # Insert transcript blocks
                if data.get("parsed_blocks"):
                    conn.execute("DELETE FROM transcript_blocks WHERE conversation_id = ?", (conv["id"],))
                    block_params = []
                    for b in data["parsed_blocks"]:
                        content_str = b.text
                        if b.arguments is not None:
                            import json
                            content_str += "\n" + json.dumps(b.arguments)

                        block_params.append((
                            conv["id"], b.step_index, b.kind.value, content_str, b.hash, b.call_id
                        ))
                    
                    conn.executemany("""
                        INSERT INTO transcript_blocks (
                            conversation_id, step_index, block_type, content, block_hash, call_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, block_params)

                processed_convs += 1
                total_steps_inserted += len(steps)
                total_tools_inserted += len(tools)
                total_tokens_computed += conv["total_tokens"]

                if i % 25 == 0 or i == total_convs:
                    pct = (i / total_convs) * 100
                    print(f"  [{pct:5.1f}%] Processed {i}/{total_convs} conversations ({total_steps_inserted:,} steps)...")

    finally:
        conn.close()

    elapsed = time.time() - t_start
    print(f"\n✅ Back-fill complete in {elapsed:.2f}s!")
    print(f"-" * 50)
    print(f"  Total Conversations Stored : {processed_convs:,}")
    print(f"  Total Steps Logged         : {total_steps_inserted:,}")
    print(f"  Total Tool Calls Tracked   : {total_tools_inserted:,}")
    print(f"  Total Estimated Tokens     : {total_tokens_computed:,}")
    print(f"  Database Location          : {DEFAULT_DB_PATH}")
    print(f"-" * 50 + "\n")

if __name__ == "__main__":
    run_backfill()
