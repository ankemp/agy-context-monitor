from pathlib import Path
import os
import json
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo


CHARS_PER_TOKEN = 3.8

PRICING = {
    "pro": (1.25, 2.50, 10.00),     # Gemini Pro / 3.1 Pro / 2.5 Pro
    "flash": (0.075, 0.15, 0.60),   # Gemini Flash / 3.6 / 3.7 Flash
    "claude": (3.00, 3.00, 15.00),  # Claude 3.5 / 3.7 Sonnet
    "gpt": (2.50, 2.50, 10.00),     # GPT-4o / o1
}

def get_pricing_key(model_name):
    m = (model_name or "").lower()
    if "claude" in m: return "claude"
    if "gpt" in m or "o1" in m: return "gpt"
    if "pro" in m: return "pro"
    return "flash"

def get_target_tz(tz_name=None):
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    env_tz = os.environ.get("TZ")
    if env_tz:
        try:
            return ZoneInfo(env_tz)
        except Exception:
            pass
    # Default to system local timezone
    return datetime.now().astimezone().tzinfo

def parse_to_local_dt(iso_str, tz=None):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        target_tz = tz or get_target_tz()
        return dt.astimezone(target_tz)
    except Exception:
        return None

def query_summary_data(conn, tz=None, project=None):
    cur = conn.cursor()
    
    where_conv = ""
    params_conv = ()
    where_tool = ""
    where_step_and = ""
    where_step_join = ""
    if project:
        if project == "(unassigned)":
            where_conv = "WHERE (project IS NULL OR project = '')"
            where_tool = "AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
            where_step_and = "AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
            where_step_join = "AND (c.project IS NULL OR c.project = '')"
        else:
            where_conv = "WHERE project = ?"
            where_tool = "AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            where_step_and = "AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            where_step_join = "AND c.project = ?"
            params_conv = (project,)
        
    row = cur.execute(f"""
        SELECT 
            COUNT(*) as total_convs,
            SUM(total_steps) as total_steps,
            SUM(total_tokens) as total_tokens,
            SUM(total_characters) as total_chars,
            SUM(duration_seconds) as total_duration,
            SUM(active_agent_seconds) as total_agent_work,
            SUM(active_pairing_seconds) as total_pairing_work,
            SUM(thinking_tokens) as total_thinking,
            SUM(user_prompts) as total_prompts,
            SUM(tool_calls_count) as total_tools,
            SUM(redundant_file_reads) as total_re_reads,
            SUM(lines_written) as total_written,
            SUM(lines_edited) as total_edited,
            SUM(failed_tool_calls) as total_fails,
            SUM(billed_cost) as total_billed_cost,
            SUM(billed_input_tokens) as total_billed_input_tokens,
            SUM(billed_output_tokens) as total_billed_output_tokens,
            AVG(total_steps) as avg_steps,
            AVG(total_tokens) as avg_tokens,
            MIN(created_at) as earliest,
            MAX(updated_at) as latest
        FROM conversations
        {where_conv}
    """, params_conv).fetchone()

    total_prompts = row["total_prompts"] or 0
    total_tools = row["total_tools"] or 0
    autonomy_multiplier = round(total_tools / max(1, total_prompts), 1)
    total_code_lines = (row["total_written"] or 0) + (row["total_edited"] or 0)
    total_fails = row["total_fails"] or 0
    resilience_rate = round(((total_tools - total_fails) / max(1, total_tools)) * 100, 1)

    view_row = cur.execute(f"SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'view_file' {where_tool}", params_conv).fetchone()
    total_views = view_row[0] if view_row else 0
    total_re_reads = row["total_re_reads"] or 0
    re_read_rate = round((total_re_reads / max(1, total_views)) * 100, 1)
    re_read_waste_tokens = total_re_reads * 750

    reads_cnt = cur.execute(f"SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('view_file', 'grep_search', 'find_by_name', 'list_dir') {where_tool}", params_conv).fetchone()[0]
    cmds_cnt = cur.execute(f"SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'run_command' {where_tool}", params_conv).fetchone()[0]
    edits_cnt = cur.execute(f"SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content') {where_tool}", params_conv).fetchone()[0]
    saved_hours = round(((reads_cnt * 30) + (cmds_cnt * 45) + (edits_cnt * 120)) / 3600)

    # Phase breakdown
    phase_rows = cur.execute(f"""
        SELECT phase, COUNT(*) as steps, SUM(est_tokens) as tokens
        FROM steps
        WHERE phase IS NOT NULL {where_step_and}
        GROUP BY phase
        ORDER BY tokens DESC
    """, params_conv).fetchall()
    phases = [{"phase": r["phase"], "steps": r["steps"], "tokens": r["tokens"] or 0} for r in phase_rows]

    # Top bloat steps
    bloat_rows = cur.execute(f"""
        SELECT step_index, phase, tool_or_source, est_tokens, detail
        FROM steps
        WHERE est_tokens > 2000 {where_step_and}
        ORDER BY est_tokens DESC
        LIMIT 8
    """, params_conv).fetchall()
    bloat_steps = [{"step_index": r["step_index"], "phase": r["phase"] or "other", "tool_or_source": r["tool_or_source"], "est_tokens": r["est_tokens"], "detail": r["detail"]} for r in bloat_rows]

    total_billed_cost = row["total_billed_cost"] or 0.0
    total_billed_input_tokens = row["total_billed_input_tokens"] or 0
    total_billed_output_tokens = row["total_billed_output_tokens"] or 0

    models = []
    for r in cur.execute(f"""
        SELECT COALESCE(s.model_name, c.model_name, 'Gemini Flash') as mname,
               COUNT(*) as steps,
               SUM(s.est_tokens) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        WHERE 1=1 {where_step_join}
        GROUP BY mname
        ORDER BY tokens DESC
    """, params_conv):
        models.append({
            "model_name": r["mname"] or "Unknown",
            "steps": r["steps"],
            "tokens": r["tokens"] or 0
        })

    categories = []
    for r in cur.execute(f"SELECT category, SUM(est_tokens) as tokens FROM steps WHERE category IS NOT NULL {where_step_and} GROUP BY category ORDER BY tokens DESC LIMIT 10", params_conv):
        categories.append({
            "category": r["category"],
            "tokens": r["tokens"] or 0
        })

    earliest_local = parse_to_local_dt(row["earliest"], tz)
    latest_local = parse_to_local_dt(row["latest"], tz)

    tz_info = get_target_tz(tz)
    tz_label = datetime.now(tz_info).strftime("%Z (%z)")

    return {
        "total_convs": row["total_convs"] or 0,
        "total_steps": row["total_steps"] or 0,
        "total_tokens": row["total_tokens"] or 0,
        "total_chars": row["total_chars"] or 0,
        "total_duration": row["total_duration"] or 0,
        "total_agent_work": row["total_agent_work"] or 0,
        "total_pairing_work": row["total_pairing_work"] or 0,
        "total_thinking": row["total_thinking"] or 0,
        "total_prompts": total_prompts,
        "total_tools": total_tools,
        "autonomy_multiplier": autonomy_multiplier,
        "total_code_lines": total_code_lines,
        "lines_written": row["total_written"] or 0,
        "lines_edited": row["total_edited"] or 0,
        "total_re_reads": total_re_reads,
        "re_read_rate": re_read_rate,
        "re_read_waste_tokens": re_read_waste_tokens,
        "resilience_rate": resilience_rate,
        "failed_tool_calls": total_fails,
        "saved_hours": saved_hours,
        "phases": phases,
        "bloat_steps": bloat_steps,
        "total_billed_cost": round(total_billed_cost, 2),
        "total_billed_input_tokens": total_billed_input_tokens,
        "total_billed_output_tokens": total_billed_output_tokens,
        "avg_steps": round(row["avg_steps"] or 0, 1),
        "avg_tokens": int(row["avg_tokens"] or 0),
        "earliest": earliest_local.strftime("%Y-%m-%d %H:%M") if earliest_local else None,
        "latest": latest_local.strftime("%Y-%m-%d %H:%M") if latest_local else None,
        "timezone": str(tz_info),
        "timezone_label": tz_label,
        "models": models,
        "categories": categories
    }

def query_cost_by_project(conn, tz=None, project=None, limit_sessions=6):
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT 
            COALESCE(NULLIF(project, ''), '(unassigned)') as project_name,
            COUNT(*) as session_count,
            SUM(total_steps) as total_steps,
            SUM(total_tokens) as total_tokens,
            SUM(billed_cost) as billed_cost,
            SUM(billed_input_tokens) as billed_input_tokens,
            SUM(billed_output_tokens) as billed_output_tokens
        FROM conversations
        GROUP BY project_name
        ORDER BY billed_cost DESC, total_tokens DESC
    """).fetchall()

    total_cost = sum(r["billed_cost"] or 0.0 for r in rows)

    projects = []
    filtered_project_cost = None
    for r in rows:
        cost = round(r["billed_cost"] or 0.0, 2)
        pct = round((cost / total_cost * 100), 1) if total_cost > 0 else 0.0
        p_name = r["project_name"]
        if project and (p_name == project or (project == "(unassigned)" and p_name == "(unassigned)")):
            filtered_project_cost = cost
        projects.append({
            "project": p_name,
            "raw_project": None if p_name == "(unassigned)" else p_name,
            "cost": cost,
            "percentage": pct,
            "sessions": r["session_count"],
            "tokens": r["total_tokens"] or 0,
            "steps": r["total_steps"] or 0,
            "billed_input_tokens": r["billed_input_tokens"] or 0,
            "billed_output_tokens": r["billed_output_tokens"] or 0
        })

    # Query top expensive sessions (narrowed to project if filtered)
    where_sess = ""
    params_sess = ()
    if project and project != "all":
        if project == "(unassigned)":
            where_sess = "WHERE (project IS NULL OR project = '')"
        else:
            where_sess = "WHERE project = ?"
            params_sess = (project,)

    sess_rows = cur.execute(f"""
        SELECT id, title, project, model_name, created_at, total_steps, total_tokens, duration_seconds, active_agent_seconds, billed_cost
        FROM conversations
        {where_sess}
        ORDER BY billed_cost DESC, total_tokens DESC
        LIMIT {limit_sessions}
    """, params_sess).fetchall()

    top_sessions = []
    target_tz = get_target_tz(tz)
    for sr in sess_rows:
        dt = parse_to_local_dt(sr["created_at"], target_tz)
        top_sessions.append({
            "id": sr["id"],
            "title": sr["title"] or f"Session {sr['id'][:8]}",
            "project": sr["project"] or "(unassigned)",
            "model_name": sr["model_name"] or "Gemini",
            "created_at": dt.strftime("%Y-%m-%d %H:%M") if dt else (sr["created_at"] or "--"),
            "total_steps": sr["total_steps"] or 0,
            "total_tokens": sr["total_tokens"] or 0,
            "duration_seconds": sr["duration_seconds"] or 0,
            "active_agent_seconds": sr["active_agent_seconds"] or 0,
            "billed_cost": round(sr["billed_cost"] or 0.0, 2)
        })

    return {
        "total_cost": round(total_cost, 2),
        "projects": projects,
        "top_sessions": top_sessions,
        "filtered_project": project if (project and project != "all") else None,
        "filtered_project_cost": filtered_project_cost
    }

def query_timeline_data(conn, days=None, tz=None, project=None):
    cur = conn.cursor()

    where_clause = "WHERE created_at IS NOT NULL AND created_at != ''"
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)

    rows = cur.execute(f"""
        SELECT created_at, conversation_id, est_tokens, billed_cost
        FROM steps
        {where_clause}
    """, params).fetchall()

    daily_map = {}
    day_sessions = {}
    target_tz = get_target_tz(tz)

    for r in rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        if not dt:
            continue
        day_str = dt.strftime("%Y-%m-%d")
        if day_str not in daily_map:
            daily_map[day_str] = {"day": day_str, "sessions": 0, "steps": 0, "tokens": 0, "cost": 0.0}
            day_sessions[day_str] = set()
        daily_map[day_str]["steps"] += 1
        daily_map[day_str]["tokens"] += r["est_tokens"] or 0
        daily_map[day_str]["cost"] += r["billed_cost"] or 0.0
        day_sessions[day_str].add(r["conversation_id"])

    for day_str, s_set in day_sessions.items():
        daily_map[day_str]["sessions"] = len(s_set)

    today = datetime.now(target_tz).date()

    if days is not None:
        num_days = int(days)
        start_date = today - timedelta(days=num_days - 1)
        end_date = today
    else:
        if daily_map:
            min_date = min(datetime.strptime(d, "%Y-%m-%d").date() for d in daily_map.keys())
            max_date = max(datetime.strptime(d, "%Y-%m-%d").date() for d in daily_map.keys())
            start_date = min(min_date, today - timedelta(days=29))
            end_date = max(max_date, today)
        else:
            start_date = today - timedelta(days=29)
            end_date = today

    timeline = []
    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        if d_str in daily_map:
            item = daily_map[d_str]
            item["cost"] = round(item["cost"], 2)
            timeline.append(item)
        else:
            timeline.append({
                "day": d_str,
                "sessions": 0,
                "steps": 0,
                "tokens": 0,
                "cost": 0.0
            })
        curr += timedelta(days=1)

    return timeline

def query_tools_data(conn, project=None):
    cur = conn.cursor()

    where_clause = ""
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause = "WHERE conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause = "WHERE conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)

    rows = cur.execute(f"""
        SELECT 
            tool_name, 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as distinct_sessions
        FROM tool_executions
        {where_clause}
        GROUP BY tool_name
        ORDER BY total_calls DESC
        LIMIT 50
    """, params).fetchall()

    tools = []
    for r in rows:
        tools.append({
            "tool_name": r["tool_name"],
            "mcp_server": r["mcp_server"],
            "is_mcp": 1 if r["mcp_server"] else 0,
            "total_calls": r["total_calls"],
            "distinct_sessions": r["distinct_sessions"]
        })
    return tools

def query_mcp_data(conn, project=None):
    cur = conn.cursor()

    where_clause = "WHERE mcp_server IS NOT NULL"
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)

    server_rows = cur.execute(f"""
        SELECT 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as sessions,
            AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
        FROM tool_executions
        {where_clause}
        GROUP BY mcp_server
        ORDER BY total_calls DESC
    """, params).fetchall()

    servers = []
    for s in server_rows:
        tool_rows = cur.execute("""
            SELECT 
                mcp_tool,
                COUNT(*) as calls,
                COUNT(DISTINCT conversation_id) as sessions,
                AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
            FROM tool_executions
            WHERE mcp_server = ? AND mcp_tool IS NOT NULL
            GROUP BY mcp_tool
            ORDER BY calls DESC
        """, (s["mcp_server"],)).fetchall()
        servers.append({
            "server": s["mcp_server"],
            "calls": s["total_calls"],
            "sessions": s["sessions"],
            "error_rate": round(s["error_rate"] or 0, 1),
            "tools": [{"tool": tr["mcp_tool"], "calls": tr["calls"], "sessions": tr["sessions"], "error_rate": round(tr["error_rate"] or 0, 1)} for tr in tool_rows]
        })
    return servers

def query_hot_files_data(conn, project=None):
    cur = conn.cursor()
    
    where_clause = "WHERE target_path IS NOT NULL AND target_path != ''"
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)

    rows = cur.execute(f"""
        SELECT 
            target_path,
            COUNT(*) as total_touches,
            COUNT(CASE WHEN tool_name = 'view_file' THEN 1 END) as views,
            COUNT(CASE WHEN tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content') THEN 1 END) as edits,
            COUNT(CASE WHEN tool_name IN ('grep_search', 'find_by_name', 'list_dir') THEN 1 END) as searches,
            COUNT(DISTINCT conversation_id) as sessions
        FROM tool_executions
        {where_clause}
        GROUP BY target_path
        ORDER BY total_touches DESC
        LIMIT 150
    """, params).fetchall()

    items = []
    for r in rows:
        raw_path = r["target_path"].strip("\"' ")
        p_obj = Path(raw_path)
        name = p_obj.name or raw_path
        ext = p_obj.suffix.lower()

        views = r["views"]
        edits = r["edits"]
        searches = r["searches"]
        touches = r["total_touches"]

        if views == 0 and edits == 0 and (not ext or searches > 0):
            is_dir = True
            type_label = "dir"
        elif ext:
            is_dir = False
            type_label = ext
        else:
            is_dir = False
            type_label = "file"

        items.append({
            "name": name,
            "path": raw_path,
            "type": type_label,
            "is_dir": is_dir,
            "touches": touches,
            "views": views,
            "edits": edits,
            "searches": searches,
            "sessions": r["sessions"]
        })
    return items

def query_activity_matrix(conn, tz=None, project=None):
    cur = conn.cursor()
    target_tz = get_target_tz(tz)
    
    where_clause = "WHERE created_at IS NOT NULL AND created_at != ''"
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)
        
    step_rows = cur.execute(f"""
        SELECT created_at, conversation_id, est_tokens
        FROM steps
        {where_clause}
    """, params).fetchall()

    daily_map = {}
    hourly_aggregate = [[0 for _ in range(24)] for _ in range(7)]
    past_30_days_hourly = {}
    
    today = datetime.now(target_tz).date()
    for i in range(30):
        d_str = (today - timedelta(days=29 - i)).isoformat()
        past_30_days_hourly[d_str] = [0 for _ in range(24)]

    past_7_days_hourly = {}
    for i in range(7):
        d_str = (today - timedelta(days=6 - i)).isoformat()
        past_7_days_hourly[d_str] = [0 for _ in range(24)]

    day_sessions = {}

    for r in step_rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        if not dt:
            continue
        
        weekday = dt.weekday()
        hour = dt.hour
        d_str = dt.strftime("%Y-%m-%d")
        tokens = r["est_tokens"] or 0
        cid = r["conversation_id"]

        hourly_aggregate[weekday][hour] += 1
        
        if d_str in past_30_days_hourly:
            past_30_days_hourly[d_str][hour] += 1
        if d_str in past_7_days_hourly:
            past_7_days_hourly[d_str][hour] += 1

        if d_str not in daily_map:
            daily_map[d_str] = {"steps": 0, "tokens": 0, "sessions": 0}
            day_sessions[d_str] = set()
            
        daily_map[d_str]["steps"] += 1
        daily_map[d_str]["tokens"] += tokens
        day_sessions[d_str].add(cid)

    for d_str, s_set in day_sessions.items():
        daily_map[d_str]["sessions"] = len(s_set)

    active_days = set(daily_map.keys())
    cur_streak = 0
    d = today
    while d.isoformat() in active_days:
        cur_streak += 1
        d -= timedelta(days=1)

    sorted_days = sorted([datetime.strptime(d, "%Y-%m-%d").date() for d in active_days])
    longest_streak = 0
    temp_streak = 0
    prev_date = None
    for day_obj in sorted_days:
        if prev_date and (day_obj - prev_date).days == 1:
            temp_streak += 1
        else:
            temp_streak = 1
        if temp_streak > longest_streak:
            longest_streak = temp_streak
        prev_date = day_obj

    tz_info = get_target_tz(tz)
    tz_abbr = datetime.now(tz_info).strftime("%Z")

    return {
        "daily": daily_map,
        "hourly_aggregate": hourly_aggregate,
        "past_7_days_hourly": past_7_days_hourly,
        "past_30_days_hourly": past_30_days_hourly,
        "timezone": str(tz_info),
        "timezone_abbr": tz_abbr,
        "streak": {
            "current": cur_streak,
            "longest": max(longest_streak, cur_streak),
            "total_active_days": len(active_days)
        }
    }

def query_conversations_list(conn, limit=200, tz=None, project=None):
    cur = conn.cursor()
    where_clause = ""
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause = "WHERE (project IS NULL OR project = '')"
        else:
            where_clause = "WHERE project = ?"
            params = (project,)
        
    rows = cur.execute(f"""
        SELECT id, title, project, model_name, created_at, total_steps, total_tokens, duration_seconds, active_agent_seconds, active_pairing_seconds, billed_cost
        FROM conversations
        {where_clause}
        ORDER BY created_at DESC
        LIMIT {limit}
    """, params).fetchall()

    convs = []
    target_tz = get_target_tz(tz)

    for r in rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        convs.append({
            "id": r["id"],
            "title": r["title"] or f"Conversation {r['id'][:8]}",
            "project": r["project"] or "(unassigned)",
            "model_name": r["model_name"] or "Gemini",
            "created_at": dt.strftime("%Y-%m-%d %H:%M") if dt else r["created_at"],
            "total_steps": r["total_steps"] or 0,
            "total_tokens": r["total_tokens"] or 0,
            "duration_seconds": r["duration_seconds"] or 0,
            "active_agent_seconds": r["active_agent_seconds"] or 0,
            "active_pairing_seconds": r["active_pairing_seconds"] or 0,
            "billed_cost": round(r["billed_cost"] or 0.0, 2)
        })
    return convs

def query_session_details(conn, cid, tz=None):
    cur = conn.cursor()
    conv = cur.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if not conv:
        return None

    steps_rows = cur.execute("""
        SELECT step_index, created_at, source, activity, tool_or_source, category, est_tokens, thinking_tokens, phase, model_name, detail,
               billed_cost, billed_input_tokens, billed_output_tokens
        FROM steps
        WHERE conversation_id = ?
        ORDER BY step_index ASC
    """, (cid,)).fetchall()

    # 1. Fetch tool executions for tool name mapping
    tool_rows = cur.execute("""
        SELECT step_index, tool_name, mcp_server, mcp_tool, target_path, command_line, is_error
        FROM tool_executions
        WHERE conversation_id = ?
        ORDER BY id ASC
    """, (cid,)).fetchall()

    tool_execs_by_step = {}
    for tr in tool_rows:
        s_idx = tr["step_index"]
        if s_idx not in tool_execs_by_step:
            tool_execs_by_step[s_idx] = []
        tool_execs_by_step[s_idx].append(dict(tr))

    # 2. Fetch transcript blocks
    blocks_rows = cur.execute("""
        SELECT step_index, block_type, content, call_id
        FROM transcript_blocks
        WHERE conversation_id = ?
        ORDER BY step_index ASC, id ASC
    """, (cid,)).fetchall()
    
    TOOL_PALETTE = [
        "#f87171", "#fb923c", "#fbbf24", "#a3e635", "#34d399",
        "#22d3ee", "#38bdf8", "#818cf8", "#a78bfa", "#e879f9",
        "#f43f5e", "#2dd4bf"
    ]

    import hashlib
    def get_color(cid_str):
        if not cid_str:
            return "#6b7280"
        val = int(hashlib.md5(cid_str.encode("utf-8")).hexdigest(), 16)
        return TOOL_PALETTE[val % len(TOOL_PALETTE)]

    tool_calls_map = {}
    blocks_by_step = {}
    for b in blocks_rows:
        idx = b["step_index"]
        if idx not in blocks_by_step:
            blocks_by_step[idx] = []
        b_dict = dict(b)
        b_type = b_dict.get("block_type")
        call_id = b_dict.get("call_id")

        if call_id:
            b_dict["color"] = get_color(call_id)

        if b_type == "tool_use":
            current_tool_idx = sum(1 for prev_b in blocks_by_step[idx] if prev_b.get("block_type") == "tool_use")
            exec_list = tool_execs_by_step.get(idx, [])
            matched_exec = exec_list[current_tool_idx] if current_tool_idx < len(exec_list) else None

            tool_name = matched_exec["tool_name"] if matched_exec else "Tool"
            target_path = matched_exec["target_path"] if matched_exec else ""
            mcp_server = matched_exec["mcp_server"] if matched_exec else None
            mcp_tool = matched_exec["mcp_tool"] if matched_exec else None

            b_dict["tool_name"] = tool_name
            b_dict["target_path"] = target_path
            b_dict["mcp_server"] = mcp_server
            b_dict["mcp_tool"] = mcp_tool

            # Sub-agent detection
            if tool_name == "invoke_subagent":
                b_dict["is_subagent"] = True
                try:
                    raw_content = b_dict.get("content", "").strip()
                    parsed_args = json.loads(raw_content) if raw_content.startswith("{") else {}
                    subagents_raw = parsed_args.get("Subagents", "")
                    if isinstance(subagents_raw, str) and subagents_raw.startswith("["):
                        subagents_list = json.loads(subagents_raw)
                    elif isinstance(subagents_raw, list):
                        subagents_list = subagents_raw
                    else:
                        subagents_list = []
                    b_dict["subagents"] = subagents_list
                    b_dict["tool_action"] = parsed_args.get("toolAction")
                    b_dict["tool_summary"] = parsed_args.get("toolSummary")
                except Exception:
                    b_dict["subagents"] = []

            # Skill detection
            if target_path and ("skills/" in target_path or "SKILL.md" in target_path):
                b_dict["is_skill"] = True
                m = re.search(r"skills/([^/]+)/SKILL\.md", target_path)
                if m:
                    b_dict["skill_name"] = m.group(1)
            elif "skills/" in b_dict.get("content", ""):
                m = re.search(r"skills/([^/]+)/SKILL\.md", b_dict.get("content", ""))
                if m:
                    b_dict["is_skill"] = True
                    b_dict["skill_name"] = m.group(1)

            if call_id:
                tool_calls_map[call_id] = {
                    "tool_name": tool_name,
                    "target_path": target_path,
                    "step_index": idx,
                    "is_subagent": b_dict.get("is_subagent", False),
                    "is_skill": b_dict.get("is_skill", False),
                    "skill_name": b_dict.get("skill_name"),
                    "subagents": b_dict.get("subagents"),
                    "color": b_dict.get("color"),
                    "use_block": b_dict
                }

        elif b_type == "tool_result":
            if call_id and call_id in tool_calls_map:
                paired_use = tool_calls_map[call_id]
                b_dict["paired_tool_name"] = paired_use.get("tool_name", "Tool")
                b_dict["paired_step_index"] = paired_use.get("step_index")
                b_dict["is_subagent"] = paired_use.get("is_subagent", False)
                b_dict["is_skill"] = paired_use.get("is_skill", False)
                b_dict["skill_name"] = paired_use.get("skill_name")
                b_dict["subagents"] = paired_use.get("subagents")
                if "color" not in b_dict or not b_dict["color"]:
                    b_dict["color"] = paired_use.get("color")
                if "use_block" in paired_use:
                    paired_use["use_block"]["result_step_index"] = idx

            # Extract subagent conversation ID from result content
            content_str = b_dict.get("content", "")
            cid_match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", content_str)
            if cid_match:
                b_dict["subagent_conversation_id"] = cid_match.group(1)

        blocks_by_step[idx].append(b_dict)

    target_tz = get_target_tz(tz)
    steps = []
    for r in steps_rows:
        d = dict(r)
        dt = parse_to_local_dt(d["created_at"], target_tz)
        if dt:
            d["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        d["blocks"] = blocks_by_step.get(d["step_index"], [])
        steps.append(d)

    cat_rows = cur.execute("""
        SELECT category, SUM(est_tokens) as tokens
        FROM steps
        WHERE conversation_id = ?
        GROUP BY category
        ORDER BY tokens DESC
    """, (cid,)).fetchall()
    categories = [dict(r) for r in cat_rows]

    phase_rows = cur.execute("""
        SELECT phase, SUM(est_tokens) as tokens
        FROM steps
        WHERE conversation_id = ? AND phase IS NOT NULL
        GROUP BY phase
        ORDER BY tokens DESC
    """, (cid,)).fetchall()
    session_phases = [dict(r) for r in phase_rows]

    top_consumers = sorted(steps, key=lambda s: s["est_tokens"], reverse=True)[:8]

    conv_dict = dict(conv)
    dt_c = parse_to_local_dt(conv_dict.get("created_at"), target_tz)
    if dt_c:
        conv_dict["created_at"] = dt_c.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "conversation": conv_dict,
        "steps": steps,
        "categories": categories,
        "phases": session_phases,
        "top_consumers": top_consumers
    }

def query_turn_diff(conn, conv_id: str, step_a: int, step_b: int, cumulative: bool = True, tz=None):
    from context_monitor.diff import get_turn_diff_from_db
    cur = conn.cursor()
    conv = cur.execute("SELECT id, title, model_name FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    if not conv:
        return None

    diff_data = get_turn_diff_from_db(conn, conv_id, step_a, step_b, cumulative=cumulative)
    target_tz = get_target_tz(tz)
    for step_key in ("step_a", "step_b"):
        if diff_data.get(step_key) and diff_data[step_key].get("created_at"):
            dt = parse_to_local_dt(diff_data[step_key]["created_at"], target_tz)
            if dt:
                diff_data[step_key]["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    diff_data["conversation_title"] = conv["title"] or f"Session {conv['id'][:8]}"
    diff_data["model_name"] = conv["model_name"] or "Gemini"
    return diff_data


