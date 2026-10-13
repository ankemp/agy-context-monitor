#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Context Monitor & Intelligence Engine
-------------------------------------------------
Lifecycle hook script triggered via PostInvocation.
Performs lightweight, zero-overhead transcript analysis:
1. Updates offline clean artifact ~/.gemini/antigravity/brain/<id>/context_usage.md
2. Syncs structured session, efficiency, MCP, and velocity metrics to centralized SQLite DB (~/.gemini/antigravity/analytics.db)
3. Returns empty JSON {} to keep conversation context 100% clean.
"""

import sys
import json
import os
import re
from pathlib import Path
from datetime import datetime

_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import get_db_connection, init_db, DEFAULT_DB_PATH
from context_monitor.formats import BlockParser
from context_monitor.diff import diff_block_lists, DiffResult
from context_monitor.html_report import generate_html_report

CHARS_PER_TOKEN = 3.8

def format_tokens(n: int | float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))

def detect_model(transcript_path: Path) -> tuple[str, str, int]:
    first_model = "Gemini 3.7 Flash"
    latest_model = "Gemini 3.7 Flash"
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

    max_tokens = get_model_context_limit(latest_model)
    return first_model, latest_model, max_tokens

def get_model_context_limit(model_name: str) -> int:
    lower = (model_name or "").lower()
    if "pro" in lower:
        return 2_000_000
    elif "flash" in lower or "gemini" in lower:
        return 1_000_000
    elif "claude" in lower:
        return 200_000
    elif "gpt" in lower or "o1" in lower or "o3" in lower:
        return 128_000
    return 1_000_000

def extract_file_basename(content: str) -> str:
    m = re.search(r"File Path:\s*`?(?:file://)?([^`\r\n]+)`?", content)
    if m:
        path_str = m.group(1).strip()
        return Path(path_str).name
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

def classify_phase(step_type: str, tool_name: str) -> str:
    s_upper = step_type.upper()
    t_lower = tool_name.lower() if tool_name else ""

    if t_lower in ("view_file", "grep_search", "find_by_name", "list_dir", "search_web", "read_url_content", "read_resource", "list_resources") or \
       s_upper in ("VIEW_FILE", "GREP_SEARCH", "FIND", "LIST_DIRECTORY", "SEARCH_WEB", "READ_URL_CONTENT"):
        return "discovery"
    
    if t_lower in ("write_to_file", "replace_file_content", "multi_replace_file_content", "generate_image", "create_or_update_file", "push_files") or \
       s_upper in ("CODE_ACTION", "GENERATE_IMAGE"):
        return "execution"

    if t_lower in ("run_command", "manage_task", "command_status", "invoke_subagent", "send_message", "manage_subagents") or \
       t_lower.startswith("mcp") or \
       s_upper in ("RUN_COMMAND", "INVOKE_SUBAGENT", "SYSTEM_MESSAGE"):
        return "verification"

    return "reasoning"

def analyze_transcript(transcript_path: Path, initial_model: str = "Gemini 3.7 Flash") -> dict:
    if transcript_path.is_dir():
        t_file = transcript_path / ".system_generated" / "logs" / "transcript.jsonl"
        if not t_file.exists():
            t_file = transcript_path / "transcript.jsonl"
        transcript_path = t_file

    steps = []
    category_stats = {}
    total_chars = 0
    total_thinking_tokens = 0
    user_prompts = 0
    redundant_reads = 0
    lines_written = 0
    lines_edited = 0
    failed_tool_calls = 0
    seen_files = set()
    pending_tool_calls = []
    tool_execs = []
    parsed_blocks = []
    block_parser = BlockParser()
    title = ""
    start_time = None
    end_time = None
    cur_model = initial_model
    project = None
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

            step_idx = step.get("step_index", len(steps) + 1)
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

            if not project and step_type in ("USER_INPUT", "CHECKPOINT") and "<user_information>" in content:
                m = re.search(r"(?:/Users/[^\s]+)\s*->\s*[^\s]+/([^\s]+)", content)
                if m:
                    project = m.group(1)

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
                    for tc_name, tc_args in pending_tool_calls:
                        target_p = tc_args.get("TargetFile") or tc_args.get("AbsolutePath") or tc_args.get("SearchPath") or tc_args.get("DirectoryPath") or ""
                        proj_path = target_p or tc_args.get("Cwd") or ""
                        if not project and proj_path and "/Users/akemp/Workspace/" in proj_path:
                            m = re.search(r"/Users/akemp/Workspace/([^/\"\'\s]+)", proj_path)
                            if m:
                                project = m.group(1)
                        clean_path = str(target_p).strip("\"'") if target_p else None
                        
                        if tc_name == "invoke_subagent":
                            sub_raw = tc_args.get("Subagents", "")
                            if isinstance(sub_raw, str):
                                try:
                                    pending_subagent_specs = json.loads(sub_raw) if sub_raw.strip().startswith("[") else []
                                except Exception:
                                    pending_subagent_specs = []
                            elif isinstance(sub_raw, list):
                                pending_subagent_specs = sub_raw
                            else:
                                pending_subagent_specs = []
                        elif tc_name == "view_file" and clean_path:
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
                            "step_index": step_idx,
                            "tool_name": stored_tool_name,
                            "mcp_server": mcp_server,
                            "mcp_tool": mcp_tool,
                            "target_path": clean_path,
                            "command_line": tc_args.get("CommandLine"),
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
                if "<USER_REQUEST>" in clean and "</USER_REQUEST>" in clean:
                    s = clean.find("<USER_REQUEST>") + len("<USER_REQUEST>")
                    e = clean.find("</USER_REQUEST>")
                    clean = clean[s:e].strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")
                if not title and clean:
                    title = clean[:120]

            elif step_type == "CHECKPOINT":
                activity = "Session Summary"
                tool_or_source = "System"
                category = "Session Summaries (Checkpoint)"
                pending_tool_calls = []
                detail = "Context truncation summary"

            elif step_type == "SYSTEM_MESSAGE":
                activity = "Background Task Output"
                tool_or_source = "System Task"
                category = "System: Background Task Outputs"
                pending_tool_calls = []
                detail = extract_system_message_detail(content)
                if step_idx <= 2 and not detected_parent_id:
                    m_snd = re.search(r"\[Message\]\s+timestamp=[^\s]+\s+sender=([a-zA-Z0-9_\-]+)", content)
                    if m_snd:
                        detected_parent_id = m_snd.group(1)

            elif step_type == "EPHEMERAL_MESSAGE":
                activity = "System Ephemeral Status"
                tool_or_source = "Hook / System"
                category = "System: Ephemeral Injections"
                pending_tool_calls = []
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")

            elif step_type == "GENERIC":
                tool_info = pending_tool_calls.pop(0) if pending_tool_calls else ("", {})
                tool_name = tool_info[0]
                tool_args = tool_info[1]
                primary_tool_name = tool_name
                
                if (tool_name == "invoke_subagent" or ("Created the following subagents:" in content and pending_subagent_specs)) and tool_name not in ("run_command", "view_file", "search_web", "manage_task"):
                    activity = "Subagent Dispatched"
                    tool_or_source = "`invoke_subagent`"
                    category = "Tool Output: `invoke_subagent`"
                    found_cids = re.findall(r'conversationId["\':\s]+([a-zA-Z0-9_\-]+)', content)
                    if not found_cids:
                        found_cids = re.findall(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', content)
                    for c_idx, ch_id in enumerate(found_cids):
                        spec = pending_subagent_specs[c_idx] if c_idx < len(pending_subagent_specs) else {}
                        dispatched_subagents.append({
                            "id": ch_id,
                            "role": spec.get("Role") or spec.get("role") or "Autonomous Worker",
                            "type": spec.get("TypeName") or spec.get("type") or "self",
                            "model": spec.get("Model") or spec.get("model") or "inherit",
                            "step_index": step_idx
                        })
                    pending_subagent_specs = []
                    detail = f"Dispatched {len(found_cids)} subagent(s)" if found_cids else "Subagent dispatch"
                elif tool_name == "view_file" or "File Path:" in content:
                    activity = "File Content"
                    tool_or_source = "`view_file`"
                    category = "Tool Output: File Contents (`view_file`)"
                    if "AbsolutePath" in tool_args:
                        detail = f"File: {Path(tool_args['AbsolutePath']).name}"
                    else:
                        detail = f"File: {extract_file_basename(content)}"
                elif tool_name == "run_command" or "The command exited" in content:
                    activity = "Shell Output"
                    tool_or_source = "`run_command`"
                    category = "Tool Output: Shell Execution (`run_command`)"
                    cmd = tool_args.get("CommandLine", "").strip()
                    detail = f"Cmd: {cmd[:40]}..." if len(cmd) > 40 else (f"Cmd: {cmd}" if cmd else "Shell stdout/stderr")
                elif tool_name == "list_dir" or "Summary: This directory contains" in content:
                    activity = "Directory Listing"
                    tool_or_source = "`list_dir`"
                    category = "Tool Output: Directory Listings (`list_dir`)"
                    dir_path = tool_args.get("DirectoryPath", "")
                    detail = f"Dir: {Path(dir_path).name or dir_path}" if dir_path else "Directory listing"
                elif tool_name == "find_by_name" or ("Found " in content and "results" in content):
                    activity = "File Search"
                    tool_or_source = "`find_by_name`"
                    category = "Tool Output: File Search (`find_by_name`)"
                    pattern = tool_args.get("Pattern", "")
                    detail = f"Pattern: {pattern}" if pattern else "File search"
                elif tool_name == "grep_search" or "LineNumber" in content:
                    activity = "Pattern Search"
                    tool_or_source = "`grep_search`"
                    category = "Tool Output: Pattern Search (`grep_search`)"
                    query = tool_args.get("Query", "")
                    detail = f"Grep: {query}" if query else "Ripgrep search"
                elif tool_name == "write_to_file" or "Created file" in content:
                    activity = "File Write"
                    tool_or_source = "`write_to_file`"
                    category = "Tool Output: `write_to_file`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"'")
                    detail = f"File: {Path(target).name}" if target else "Write confirmation"
                elif tool_name == "replace_file_content" or "The following changes were made" in content:
                    activity = "File Edit"
                    tool_or_source = "`replace_file_content`"
                    category = "Tool Output: `replace_file_content`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"'")
                    detail = f"Edit: {Path(target).name}" if target else "Edit diff block"
                elif tool_name == "call_mcp_tool" or tool_name.startswith("mcp_"):
                    srv = tool_args.get("ServerName", "")
                    t_n = tool_args.get("ToolName", tool_name)
                    activity = f"MCP: {srv}/{t_n}" if srv else f"MCP: {t_n}"
                    tool_or_source = f"`{t_n}`"
                    category = "Tool Output: MCP Tools"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
                else:
                    activity = f"Tool: {tool_name}" if tool_name else "Generic Output"
                    tool_or_source = f"`{tool_name}`" if tool_name else "Tool"
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
            category_stats[category] = category_stats.get(category, 0) + step_chars

            steps.append({
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

    total_tokens = int(total_chars / CHARS_PER_TOKEN)
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

    # Append dynamically loaded tool schemas
    parsed_blocks.extend(block_parser.load_tool_schemas())

    # Calculate billed cost based on steps
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0
    
    # We need pricing for the cost calculation
    PRICING = {
        "pro": (1.25, 2.50, 10.00),     # Gemini Pro / 3.1 Pro / 2.5 Pro
        "flash": (0.075, 0.15, 0.60),   # Gemini Flash / 3.6 / 3.7 Flash
        "claude": (3.00, 3.00, 15.00),  # Claude 3.5 / 3.7 Sonnet
        "gpt": (2.50, 2.50, 10.00),     # GPT-4o / o1
    }
    
    def get_pricing_key(m_name):
        m = (m_name or "").lower()
        if "claude" in m: return "claude"
        if "gpt" in m or "o1" in m: return "gpt"
        if "pro" in m: return "pro"
        return "flash"

    for s in steps:
        m_name = s.get("model_name", cur_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)
        
        pkey = get_pricing_key(m_name)
        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
            # Context truncation: reset running_ctx to checkpoint tokens
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            # Transient injection for immediate turn only
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            in_rate_low, in_rate_high, out_rate = PRICING[pkey]
            actual_in = running_ctx + current_ephemeral
            in_rate = in_rate_high if actual_in > 128_000 else in_rate_low
            step_cost = (actual_in / 1_000_000 * in_rate) + (tokens / 1_000_000 * out_rate)
            step_in_tokens = actual_in
            step_out_tokens = tokens
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

    return {
        "title": title or "Active Session",
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "active_agent_seconds": active_agent_seconds,
        "active_pairing_seconds": active_pairing_seconds,
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
        "latest_model": cur_model,
        "latest_max_tokens": get_model_context_limit(cur_model),
        "project": project,
        "steps": steps,
        "total_chars": total_chars,
        "total_tokens": total_tokens,
        "active_context_tokens": running_ctx,
        "category_stats": category_stats,
        "tool_executions": tool_execs,
        "parsed_blocks": parsed_blocks,
        "parent_id": detected_parent_id,
        "dispatched_subagents": dispatched_subagents
    }

def sync_to_sqlite(cid: str, model_name: str, analysis: dict):
    """Safely and incrementally syncs session data to SQLite in WAL mode and computes turn diff."""
    diff_res = None
    try:
        if not DEFAULT_DB_PATH:
            return None
        conn = get_db_connection(DEFAULT_DB_PATH, timeout=5.0)
        with conn:
            cur = conn.cursor()

            # Query existing blocks BEFORE deleting them to compute turn diff
            prev_blocks = cur.execute("""
                SELECT step_index, block_type, content, block_hash, call_id
                FROM transcript_blocks
                WHERE conversation_id = ?
                ORDER BY step_index ASC, id ASC
            """, (cid,)).fetchall()

            curr_blocks = analysis.get("parsed_blocks", [])
            if curr_blocks and prev_blocks:
                diff_res = diff_block_lists(prev_blocks, curr_blocks)
            elif curr_blocks:
                diff_res = diff_block_lists([], curr_blocks)

            active_model = analysis.get("latest_model", model_name)
            parent_id = analysis.get("parent_id")
            subagent_role = analysis.get("subagent_role")
            subagent_type = analysis.get("subagent_type")
            invoking_step = analysis.get("invoking_step_index")
            root_id = cid
            depth = 0

            # Check if an existing row already has lineage from a parent's dispatch
            existing = cur.execute(
                "SELECT parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index, project, title, compaction_count, pruned_tokens FROM conversations WHERE id = ?",
                (cid,)
            ).fetchone()

            comp_count = 0
            pruned_toks = 0
            if existing:
                try:
                    comp_count = existing["compaction_count"] or 0
                except (IndexError, KeyError):
                    comp_count = 0
                try:
                    pruned_toks = existing["pruned_tokens"] or 0
                except (IndexError, KeyError):
                    pruned_toks = 0
                if not parent_id and existing["parent_id"]:
                    parent_id = existing["parent_id"]
                if not subagent_role and existing["subagent_role"]:
                    subagent_role = existing["subagent_role"]
                if not subagent_type and existing["subagent_type"]:
                    subagent_type = existing["subagent_type"]
                if invoking_step is None and existing["invoking_step_index"] is not None:
                    invoking_step = existing["invoking_step_index"]

            if diff_res and diff_res.compaction_detected:
                comp_count += 1
                pruned_toks += diff_res.removed_tokens

            if parent_id:
                p_row = cur.execute("SELECT id, root_id, project, depth, title FROM conversations WHERE id = ?", (parent_id,)).fetchone()
                if p_row:
                    root_id = p_row["root_id"] or p_row["id"]
                    depth = (p_row["depth"] or 0) + 1
                    if not analysis.get("project") and p_row["project"]:
                        analysis["project"] = p_row["project"]
                    if (not analysis.get("title") or analysis["title"] in ("Active Session", "")) and p_row["title"]:
                        role_label = subagent_role or "Worker"
                        clean_p_title = p_row["title"]
                        m_sub = re.match(r"^\[.+?\]\s*(.+)", clean_p_title)
                        if m_sub:
                            clean_p_title = m_sub.group(1)
                        analysis["title"] = f"[{role_label}] {clean_p_title[:90]}"
                else:
                    root_id = parent_id
                    depth = 1

            # Upsert conversation record with hierarchy and compaction columns
            conn.execute("""
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at, total_steps, total_characters, total_tokens,
                    duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                    user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                    billed_cost, billed_input_tokens, billed_output_tokens,
                    parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                    compaction_count, pruned_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    project = COALESCE(excluded.project, conversations.project),
                    model_name = excluded.model_name,
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
                    parent_id = COALESCE(excluded.parent_id, conversations.parent_id),
                    root_id = COALESCE(excluded.root_id, conversations.root_id),
                    depth = CASE WHEN excluded.parent_id IS NOT NULL THEN excluded.depth ELSE conversations.depth END,
                    subagent_role = COALESCE(excluded.subagent_role, conversations.subagent_role),
                    subagent_type = COALESCE(excluded.subagent_type, conversations.subagent_type),
                    invoking_step_index = COALESCE(excluded.invoking_step_index, conversations.invoking_step_index),
                    compaction_count = excluded.compaction_count,
                    pruned_tokens = excluded.pruned_tokens
            """, (
                cid, analysis["title"], analysis.get("project"), active_model, analysis["start_time"] or datetime.now().isoformat(),
                analysis["end_time"] or datetime.now().isoformat(), len(analysis["steps"]),
                analysis["total_chars"], analysis["total_tokens"], analysis["duration_seconds"],
                analysis.get("active_agent_seconds", 0.0), analysis.get("active_pairing_seconds", 0.0),
                analysis.get("thinking_tokens", 0), analysis.get("user_prompts", 0),
                analysis.get("tool_calls_count", 0), analysis.get("redundant_file_reads", 0),
                analysis.get("lines_written", 0), analysis.get("lines_edited", 0),
                analysis.get("failed_tool_calls", 0),
                analysis.get("billed_cost", 0.0),
                analysis.get("billed_input_tokens", 0),
                analysis.get("billed_output_tokens", 0),
                parent_id, root_id, depth, subagent_role, subagent_type, invoking_step,
                comp_count, pruned_toks
            ))

            # Upsert dispatched subagents so child rows have lineage right away
            for child in analysis.get("dispatched_subagents", []):
                ch_id = child["id"]
                ch_role = child.get("role")
                ch_type = child.get("type")
                ch_step = child.get("step_index")
                clean_t = analysis["title"]
                m_sub = re.match(r"^\[.+?\]\s*(.+)", clean_t)
                if m_sub:
                    clean_t = m_sub.group(1)
                ch_title = f"[{ch_role}] {clean_t[:90]}"

                conn.execute("""
                    INSERT INTO conversations (
                        id, title, project, parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        parent_id = excluded.parent_id,
                        root_id = excluded.root_id,
                        depth = excluded.depth,
                        subagent_role = COALESCE(excluded.subagent_role, conversations.subagent_role),
                        subagent_type = COALESCE(excluded.subagent_type, conversations.subagent_type),
                        invoking_step_index = COALESCE(excluded.invoking_step_index, conversations.invoking_step_index),
                        project = COALESCE(conversations.project, excluded.project)
                """, (
                    ch_id,
                    ch_title,
                    analysis.get("project"),
                    cid,
                    root_id,
                    depth + 1,
                    ch_role,
                    ch_type,
                    ch_step
                ))

            # Upsert steps with step-level model_name
            step_params = [
                (cid, s["step_index"], s["created_at"], s["source"], s["step_type"], s["category"], s["activity"],
                 s["tool_or_source"], s["characters"], s["est_tokens"], s.get("thinking_tokens", 0), s.get("phase", "reasoning"),
                 s.get("model_name", active_model), s["detail"], s.get("billed_cost", 0.0), s.get("billed_input_tokens", 0),
                 s.get("billed_output_tokens", 0))
                for s in analysis["steps"]
            ]
            conn.executemany("""
                INSERT INTO steps (
                    conversation_id, step_index, created_at, source, step_type, category, activity,
                    tool_or_source, characters, est_tokens, thinking_tokens, phase, model_name, detail,
                    billed_cost, billed_input_tokens, billed_output_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, step_index) DO UPDATE SET
                    characters = excluded.characters,
                    est_tokens = excluded.est_tokens,
                    thinking_tokens = excluded.thinking_tokens,
                    phase = excluded.phase,
                    model_name = excluded.model_name,
                    detail = excluded.detail,
                    billed_cost = excluded.billed_cost,
                    billed_input_tokens = excluded.billed_input_tokens,
                    billed_output_tokens = excluded.billed_output_tokens
            """, step_params)

            # Sync tool executions
            if analysis.get("tool_executions"):
                conn.execute("DELETE FROM tool_executions WHERE conversation_id = ?", (cid,))
                tool_params = [
                    (cid, te["step_index"], te["tool_name"], te.get("mcp_server"), te.get("mcp_tool"),
                     te.get("target_path"), te.get("command_line"), te.get("exit_code"), te.get("is_error", 0))
                    for te in analysis["tool_executions"]
                ]
                conn.executemany("""
                    INSERT INTO tool_executions (
                        conversation_id, step_index, tool_name, mcp_server, mcp_tool,
                        target_path, command_line, exit_code, is_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, tool_params)

            # Sync transcript blocks
            if analysis.get("parsed_blocks"):
                conn.execute("DELETE FROM transcript_blocks WHERE conversation_id = ?", (cid,))
                block_params = []
                for b in analysis["parsed_blocks"]:
                    content_str = b.text
                    if b.arguments is not None:
                        import json
                        content_str += "\n" + json.dumps(b.arguments)

                    block_params.append((
                        cid, b.step_index, b.kind.value, content_str, b.hash, b.call_id
                    ))
                
                conn.executemany("""
                    INSERT INTO transcript_blocks (
                        conversation_id, step_index, block_type, content, block_hash, call_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, block_params)

        conn.close()
        return diff_res
    except Exception:
        # Never break hook execution on db errors
        return diff_res

def generate_artifact_report(cid: str, model_name: str, max_tokens: int, analysis: dict, artifact_dir: Path, diff_res: Any = None):
    try:
        report_file = artifact_dir / "context_usage.md"
        html_file = artifact_dir / "context_usage.html"
        active_model = analysis.get("latest_model", model_name)
        active_max = analysis.get("latest_max_tokens", max_tokens)
        active_tokens = analysis.get("active_context_tokens", analysis["total_tokens"])
        used_tokens = analysis["total_tokens"]
        active_pct = (active_tokens / active_max * 100) if active_max else 0.0
        cost = analysis.get("billed_cost", 0.0)
        in_toks = analysis.get("billed_input_tokens", 0)
        out_toks = analysis.get("billed_output_tokens", 0)
        
        top_steps = sorted(analysis["steps"], key=lambda s: s["characters"], reverse=True)[:10]

        # Check database for lineage and swarm fleet info
        parent_info = None
        fleet_children = []
        try:
            if DEFAULT_DB_PATH and DEFAULT_DB_PATH.exists():
                c_conn = get_db_connection(DEFAULT_DB_PATH, timeout=2.0)
                with c_conn:
                    cur_row = c_conn.execute("SELECT parent_id, root_id, depth, subagent_role FROM conversations WHERE id = ?", (cid,)).fetchone()
                    if cur_row and cur_row["parent_id"]:
                        p_row = c_conn.execute("SELECT id, title, project FROM conversations WHERE id = ?", (cur_row["parent_id"],)).fetchone()
                        if p_row:
                            parent_info = {
                                "id": p_row["id"],
                                "title": p_row["title"] or f"Parent ({p_row['id'][:8]})",
                                "role": cur_row["subagent_role"] or "Worker",
                                "depth": cur_row["depth"] or 1
                            }

                    # Fetch children / descendants
                    ch_rows = c_conn.execute("""
                        SELECT id, title, subagent_role, depth, total_steps, total_tokens, billed_cost
                        FROM conversations
                        WHERE (parent_id = ? OR (root_id = ? AND id != ?))
                        ORDER BY depth ASC, created_at ASC
                    """, (cid, cid, cid)).fetchall()
                    fleet_children = [dict(r) for r in ch_rows]
                c_conn.close()
        except Exception:
            pass

        lines = [
            "# Live Context Window Usage Report",
            f"\n> **Conversation**: `{cid}`  ",
            f"> **Model**: `{active_model}` ({format_tokens(active_max)} token window limit)  ",
        ]
        if parent_info:
            lines.append(f"> **Lineage**: Sub-Agent (Role: `{parent_info['role']}`, Tier: {parent_info['depth']}) spawned by [{parent_info['title']}](conversation://{parent_info['id']})  ")
        lines.extend([
            f"> **Active Context Window**: **{active_pct:.1f}%** ({format_tokens(active_tokens)} / {format_tokens(active_max)} estimated tokens)  ",
            f"> **Cumulative Disk Footprint**: **{format_tokens(used_tokens)} tokens** across {len(analysis['steps'])} logged steps  ",
            f"> **Session API Value**: **${cost:,.4f}** (~{format_tokens(in_toks)} in / {format_tokens(out_toks)} out)\n",
            "## 1. Granular Context Breakdown by Category\n",
            "| Category | Characters | Est. Tokens | Share |",
            "| :--- | :--- | :--- | :--- |"
        ])

        for cat, chars in sorted(analysis["category_stats"].items(), key=lambda x: x[1], reverse=True):
            toks = int(chars / CHARS_PER_TOKEN)
            cat_pct = (chars / analysis["total_chars"] * 100) if analysis["total_chars"] else 0.0
            lines.append(f"| {cat} | {chars:,} | ~{format_tokens(toks)} | {cat_pct:.1f}% |")

        lines.extend([
            "\n## 2. Top Context-Consuming Steps\n",
            "| Step # | Activity | Source / Tool | Est. Tokens | Detail |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ])

        for s in top_steps:
            lines.append(f"| #{s['step_index']} | {s['activity']} | {s['tool_or_source']} | ~{format_tokens(s['est_tokens'])} | {s['detail']} |")

        sec_num = 3
        if diff_res and getattr(diff_res, "compaction_detected", False):
            pruned_indices = getattr(diff_res, "pruned_step_indices", [])
            p_min = min(pruned_indices) if pruned_indices else 0
            p_max = max(pruned_indices) if pruned_indices else 0
            removed_toks = getattr(diff_res, "removed_tokens", 0)
            steps_list = analysis.get("steps", [])
            act_min = steps_list[0]["step_index"] if steps_list else 0
            act_max = steps_list[-1]["step_index"] if steps_list else 0

            lines.extend([
                f"\n## {sec_num}. Active Context Memory Boundary\n",
                f"> **Active Memory Window**: Steps #{act_min} to #{act_max} (currently visible to model)  ",
                f"> **Context Compaction**: {len(pruned_indices)} historical steps pruned (~{format_tokens(removed_toks)} freed across {len(diff_res.removed)} blocks)  ",
                f"> **Pruned Step Range**: Steps #{p_min} to #{p_max} (older history trimmed to respect context limits)\n",
            ])
            sec_num += 1

        if fleet_children:
            total_fleet_toks = sum(c["total_tokens"] or 0 for c in fleet_children)
            total_fleet_cost = sum(c["billed_cost"] or 0.0 for c in fleet_children)
            total_fleet_steps = sum(c["total_steps"] or 0 for c in fleet_children)

            lines.extend([
                f"\n## {sec_num}. Delegated Swarm Fleet (Context Shielding)\n",
                f"> **Dispatched Workers**: {len(fleet_children)} sub-agents  ",
                f"> **Delegated Fleet Work**: ~{format_tokens(total_fleet_toks)} tokens (${total_fleet_cost:,.4f}) across {total_fleet_steps} steps  ",
                f"> **Context Shielding Benefit**: Delegating tasks avoided injecting ~{format_tokens(total_fleet_toks)} tokens of verbose sub-task output into this active context window.\n",
                "| Sub-Agent | Role | Tier | Steps | Tokens | Cost |",
                "| :--- | :--- | :--- | :--- | :--- | :--- |"
            ])
            for ch in fleet_children:
                ch_title = ch.get("title") or f"Sub-Agent {ch['id'][:8]}"
                ch_role = ch.get("subagent_role") or "Worker"
                ch_depth = ch.get("depth") or 1
                ch_steps = ch.get("total_steps") or 0
                ch_toks = ch.get("total_tokens") or 0
                ch_c = ch.get("billed_cost") or 0.0
                lines.append(f"| [{ch_title}](conversation://{ch['id']}) | {ch_role} | Tier {ch_depth} | {ch_steps} | ~{format_tokens(ch_toks)} | ${ch_c:,.2f} |")

        lines.extend([
            "\n---\n*Report generated automatically by the `context-monitor` plugin.* · [Context Monitor](sidecar://context-monitor/panel/)"
        ])

        report_file.parent.mkdir(parents=True, exist_ok=True)
        with open(report_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # Generate companion HTML report (Generative UI card)
        try:
            html_content = generate_html_report(cid, active_model, active_max, analysis, diff_res=diff_res)
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception:
            pass
    except Exception:
        pass

def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}

    transcript_path_str = input_data.get("transcriptPath")
    conversation_id = input_data.get("conversationId", "unknown")

    if not transcript_path_str or not os.path.exists(transcript_path_str):
        print(json.dumps({}))
        return

    transcript_path = Path(transcript_path_str)
    first_model, latest_model, max_tokens = detect_model(transcript_path)
    analysis = analyze_transcript(transcript_path, initial_model=first_model)

    diff_res = sync_to_sqlite(conversation_id, latest_model, analysis)

    artifact_dir = Path(os.path.expanduser(f"~/.gemini/antigravity/brain/{conversation_id}"))
    if artifact_dir.exists():
        generate_artifact_report(conversation_id, latest_model, max_tokens, analysis, artifact_dir, diff_res=diff_res)

    # Return transient ephemeral message for Antigravity 2.0 UI
    # Invariant: Zero context pollution - ephemeralMessage is never written to transcript.jsonl
    active_tokens = analysis.get("active_context_tokens", analysis.get("total_tokens", 0))
    active_pct = (active_tokens / max_tokens * 100) if max_tokens else 0.0
    turn_cost = analysis.get("billed_cost", 0.0)

    if diff_res and getattr(diff_res, "compaction_detected", False):
        pruned_steps = getattr(diff_res, "pruned_step_indices", [])
        p_min = min(pruned_steps) if pruned_steps else 0
        p_max = max(pruned_steps) if pruned_steps else 0
        rem_toks = getattr(diff_res, "removed_tokens", 0)
        ephemeral_msg = (
            f"Context Compaction Detected: Pruned steps #{p_min}–#{p_max} "
            f"(~{format_tokens(rem_toks)} tokens freed across {len(diff_res.removed)} blocks). "
            f"Active window: {active_pct:.1f}% ({format_tokens(active_tokens)}/{format_tokens(max_tokens)})."
        )
    elif active_pct >= 80.0:
        ephemeral_msg = (
            f"Context Warning: Window at {active_pct:.1f}% capacity "
            f"({format_tokens(active_tokens)} / {format_tokens(max_tokens)} tokens). "
            f"Consider summarizing or resetting conversation."
        )
    else:
        added_tok = getattr(diff_res, "added_tokens", 0) if diff_res else 0
        delta_str = f" · +{format_tokens(added_tok)} tok this turn" if added_tok > 0 else ""
        ephemeral_msg = (
            f"Context: {active_pct:.1f}% ({format_tokens(active_tokens)} / {format_tokens(max_tokens)} tokens)"
            f"{delta_str} · Session Value: ${turn_cost:,.2f}"
        )

    output_payload = {
        "injectSteps": [
            {
                "ephemeralMessage": ephemeral_msg
            }
        ]
    }
    print(json.dumps(output_payload))


if __name__ == "__main__":
    main()

