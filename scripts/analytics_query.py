#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Cross-Conversation Analytics Query Tool
---------------------------------------------------
Queries the centralized SQLite analytics database to generate rich CLI summaries,
MCP ecosystem metrics, context efficiency diagnostics, developer velocity metrics, tool rankings,
hot file lists, activity timelines, accurate multi-turn billing, and markdown reports.

Usage:
  uv run analytics_query.py --summary
  uv run analytics_query.py --mcp
  uv run analytics_query.py --efficiency
  uv run analytics_query.py --velocity
  uv run analytics_query.py --tools
  uv run analytics_query.py --hot-files
  uv run analytics_query.py --timeline 14
  uv run analytics_query.py --report
  uv run analytics_query.py --json
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import sys
from pathlib import Path
_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import get_db_connection, DEFAULT_DB_PATH

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

def format_tokens(n: int) -> str:
    if not n:
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    elif n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

def format_duration(seconds: float) -> str:
    if not seconds:
        return "0m"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"

def calculate_billed_cost(conn):
    cur = conn.cursor()
    # Check if steps have populated billed_cost
    try:
        row = cur.execute("SELECT SUM(billed_cost), SUM(billed_input_tokens), SUM(billed_output_tokens) FROM steps").fetchone()
        if row and row[0] is not None and row[0] > 0:
            return row[0] or 0.0, row[1] or 0, row[2] or 0
    except Exception:
        pass

    # Fallback to accurate multi-turn calculation
    cur.execute("""
        SELECT s.conversation_id, COALESCE(s.model_name, c.model_name, 'Gemini Flash'), s.step_type, s.est_tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        ORDER BY s.conversation_id, s.step_index ASC
    """)

    total_cost = 0.0
    total_in = 0
    total_out = 0
    cur_conv = None
    running_ctx = 0
    current_ephemeral = 0

    for cid, model_name, s_type, tokens in cur.fetchall():
        if cid != cur_conv:
            cur_conv = cid
            running_ctx = 0
            current_ephemeral = 0
            
        cur_pkey = get_pricing_key(model_name)
        if s_type == "CHECKPOINT":
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            in_rate_low, in_rate_high, out_rate = PRICING[cur_pkey]
            actual_in = running_ctx + current_ephemeral
            in_rate = in_rate_high if actual_in > 128_000 else in_rate_low
            total_cost += (actual_in / 1_000_000 * in_rate) + (tokens / 1_000_000 * out_rate)
            total_in += actual_in
            total_out += tokens
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

    return total_cost, total_in, total_out

def print_summary(conn):
    print("\n" + "=" * 70)
    print(" 🚀 GOOGLE ANTIGRAVITY — GLOBAL AGGREGATE ANALYTICS")
    print("=" * 70)

    cur = conn.cursor()
    row = cur.execute("""
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
            AVG(total_steps) as avg_steps,
            AVG(total_tokens) as avg_tokens,
            MIN(created_at) as earliest,
            MAX(updated_at) as latest
        FROM conversations
    """).fetchone()

    mcp_row = cur.execute("SELECT COUNT(*) as mcp_calls, COUNT(DISTINCT mcp_server) as servers FROM tool_executions WHERE mcp_server IS NOT NULL").fetchone()
    total_mcp_calls = mcp_row["mcp_calls"] or 0
    total_mcp_servers = mcp_row["servers"] or 0

    total_cost, total_in, total_out = calculate_billed_cost(conn)

    total_convs = row["total_convs"] or 0
    total_steps = row["total_steps"] or 0
    total_tokens = row["total_tokens"] or 0
    total_chars = row["total_chars"] or 0
    total_duration = row["total_duration"] or 0
    total_agent_work = row["total_agent_work"] or 0
    total_pairing_work = row["total_pairing_work"] or 0
    total_thinking = row["total_thinking"] or 0
    total_prompts = row["total_prompts"] or 0
    total_tools = row["total_tools"] or 0
    total_re_reads = row["total_re_reads"] or 0
    total_written = row["total_written"] or 0
    total_edited = row["total_edited"] or 0
    avg_steps = row["avg_steps"] or 0

    autonomy_ratio = total_tools / max(1, total_prompts)

    print(f" Total Conversations Tracked : {total_convs:,}")
    print(f" Total Logged Steps          : {total_steps:,} (avg {avg_steps:.1f} steps/session)")
    print(f" Static Disk Token Footprint : {format_tokens(total_tokens)} (~{total_chars:,} chars)")
    print(f" Cumulative Multi-Turn Input : {format_tokens(total_in)} tokens (area under curve)")
    print(f" Cumulative Output / Thinking: {format_tokens(total_out)} tokens")
    print(f" True Equivalent API Value   : ${total_cost:,.2f}")
    print(f" Pure Agent Execution Time   : {format_duration(total_agent_work)} (active LLM reasoning & tool calls)")
    print(f" Active Collaborative Time   : {format_duration(total_pairing_work)} (continuous interaction <= 5m gaps)")
    print(f" Total Wall-Clock Span       : {format_duration(total_duration)} (across all open sessions)")
    print(f" Autonomy Leverage Ratio     : {autonomy_ratio:.1f}x automated actions per prompt ({total_tools:,} tools / {total_prompts:,} prompts)")
    print(f" Deep Reasoning Tokens       : {format_tokens(total_thinking)} thinking tokens")
    print(f" Total Code Authored/Edited  : {total_written + total_edited:,} lines (+{total_written:,} written / ~{total_edited:,} edited)")
    print(f" MCP Tool Invocations        : {total_mcp_calls:,} calls across {total_mcp_servers} servers")
    print(f" Redundant File Re-Reads     : {total_re_reads:,} repeated reads across active sessions")
    if row["earliest"] and row["latest"]:
        try:
            d_min = datetime.fromisoformat(row["earliest"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
            d_max = datetime.fromisoformat(row["latest"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
            print(f" Tracking Date Range         : {d_min} to {d_max} ({datetime.now().astimezone().strftime('%Z')})")
        except Exception:
            print(f" Tracking Date Range         : {row['earliest'][:10]} to {row['latest'][:10]}")
    print("-" * 70)

    print("\n[1] Context Consumption by Activity Category:")
    print(f"  {'Category':<42} | {'Est. Tokens':<12} | {'Share':<6}")
    print("  " + "-" * 64)
    cat_rows = cur.execute("""
        SELECT category, SUM(est_tokens) as tokens
        FROM steps
        WHERE category IS NOT NULL
        GROUP BY category
        ORDER BY tokens DESC
        LIMIT 8
    """).fetchall()
    for r in cat_rows:
        tokens = r["tokens"] or 0
        share = (tokens / total_tokens * 100) if total_tokens else 0.0
        print(f"  {r['category']:<42} | {format_tokens(tokens):<12} | {share:5.1f}%")

    print("\n[2] Top 10 Most Frequently Called Tools:")
    print(f"  {'Tool Name':<30} | {'Call Count':<12} | {'Share':<6}")
    print("  " + "-" * 52)
    tool_rows = cur.execute("""
        SELECT tool_name, COUNT(*) as calls
        FROM tool_executions
        GROUP BY tool_name
        ORDER BY calls DESC
        LIMIT 10
    """).fetchall()
    tot_tools = sum(r["calls"] for r in tool_rows)
    for r in tool_rows:
        calls = r["calls"]
        share = (calls / tot_tools * 100) if tot_tools else 0.0
        print(f"  {r['tool_name']:<30} | {calls:<12,}| {share:5.1f}%")

    print("\n[3] Model Distribution (Step-Level Exact):")
    print(f"  {'Model Name':<30} | {'Steps':<10} | {'Tokens':<12} | {'Share':<6}")
    print("  " + "-" * 66)
    model_rows = cur.execute("""
        SELECT COALESCE(s.model_name, c.model_name, 'Gemini Flash') as mname,
               COUNT(*) as step_count,
               SUM(s.est_tokens) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        GROUP BY mname
        ORDER BY tokens DESC
    """).fetchall()
    tot_model_tokens = sum(r["tokens"] or 0 for r in model_rows)
    for r in model_rows:
        toks = r["tokens"] or 0
        share = (toks / tot_model_tokens * 100) if tot_model_tokens else 0.0
        print(f"  {(r['mname'] or 'Unknown'):<30} | {r['step_count']:<10,}| {format_tokens(toks):<12} | {share:5.1f}%")

    print("\n[4] Cost & Context by Project:")
    print(f"  {'Project':<28} | {'Cost':<10} | {'Share':<7} | {'Sessions':<10} | {'Tokens':<10}")
    print("  " + "-" * 72)
    proj_rows = cur.execute("""
        SELECT 
            COALESCE(NULLIF(project, ''), '(unassigned)') as project_name,
            COUNT(*) as session_count,
            SUM(total_tokens) as total_tokens,
            SUM(billed_cost) as billed_cost
        FROM conversations
        GROUP BY project_name
        ORDER BY billed_cost DESC, total_tokens DESC
    """).fetchall()
    tot_proj_cost = sum(r["billed_cost"] or 0.0 for r in proj_rows)
    for r in proj_rows:
        c = r["billed_cost"] or 0.0
        pct = (c / tot_proj_cost * 100) if tot_proj_cost else 0.0
        print(f"  {(r['project_name']):<28} | ${c:<9.2f}| {pct:5.1f}% | {r['session_count']:<10,}| {format_tokens(r['total_tokens'] or 0):<10}")
    print("=" * 70 + "\n")

def print_mcp(conn):
    print("\n" + "=" * 78)
    print(" 🔌 MCP (MODEL CONTEXT PROTOCOL) SERVER & TOOL ECOSYSTEM ANALYTICS")
    print("=" * 78)

    cur = conn.cursor()
    server_rows = cur.execute("""
        SELECT 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as sessions,
            AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
        FROM tool_executions
        WHERE mcp_server IS NOT NULL
        GROUP BY mcp_server
        ORDER BY total_calls DESC
    """).fetchall()

    total_mcp_calls = sum(r["total_calls"] for r in server_rows)
    distinct_servers = len(server_rows)

    sess_row = cur.execute("SELECT COUNT(DISTINCT conversation_id) FROM tool_executions WHERE mcp_server IS NOT NULL").fetchone()
    mcp_sessions = sess_row[0] if sess_row else 0

    print(f" Total MCP Invocations Tracked : {total_mcp_calls:,}")
    print(f" Connected MCP Servers Active  : {distinct_servers}")
    print(f" Sessions Utilizing MCP Tools  : {mcp_sessions:,}")
    print("-" * 78)

    print("\n[1] MCP Server Invocations Leaderboard:")
    print(f"  {'MCP Server':<28} | {'Calls':<8} | {'Share':<6} | {'Sessions':<10} | {'Error Rate':<10}")
    print("  " + "-" * 72)
    for r in server_rows:
        share = (r["total_calls"] / total_mcp_calls * 100) if total_mcp_calls else 0.0
        print(f"  {r['mcp_server']:<28} | {r['total_calls']:<8,}| {share:5.1f}% | {r['sessions']:<10,}| {r['error_rate']:5.1f}%")

    print("\n[2] Detailed Tool Invocations by MCP Server:")
    for s_row in server_rows:
        server = s_row["mcp_server"]
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
        """, (server,)).fetchall()

        print(f"\n  📦 Server: {server.upper()} ({s_row['total_calls']:,} calls across {s_row['sessions']} sessions)")
        print(f"     {'Specific Tool Name':<36} | {'Calls':<8} | {'Sessions':<10} | {'Error Rate':<10}")
        print("     " + "-" * 70)
        for tr in tool_rows:
            t_name = tr["mcp_tool"] or "generic"
            print(f"     {t_name:<36} | {tr['calls']:<8,}| {tr['sessions']:<10,}| {tr['error_rate']:5.1f}%")

    print("=" * 78 + "\n")

def print_efficiency(conn):
    print("\n" + "=" * 75)
    print(" 🔍 ANTIGRAVITY CONTEXT HEALTH & EFFICIENCY DIAGNOSTICS")
    print("=" * 75)

    cur = conn.cursor()
    row = cur.execute("""
        SELECT 
            COUNT(*) as convs,
            SUM(total_tokens) as total_tokens,
            SUM(redundant_file_reads) as total_re_reads
        FROM conversations
    """).fetchone()

    total_tokens = row["total_tokens"] or 1
    total_re_reads = row["total_re_reads"] or 0

    view_row = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'view_file'").fetchone()
    total_views = view_row[0] if view_row else 0
    re_read_rate = (total_re_reads / max(1, total_views)) * 100
    wasted_tokens = total_re_reads * 750

    print(f" Total File Views Tracked    : {total_views:,}")
    print(f" Redundant File Re-Reads     : {total_re_reads:,} ({re_read_rate:.1f}% re-read rate)")
    print(f" Estimated Context Waste     : ~{format_tokens(wasted_tokens)} tokens spent re-reading existing context")
    print("-" * 75)

    print("\n[1] Context Consumption by Development Phase:")
    print(f"  {'Phase':<16} | {'Logged Steps':<14} | {'Est. Tokens':<14} | {'Share':<6}")
    print("  " + "-" * 56)
    phases = cur.execute("""
        SELECT phase, COUNT(*) as steps, SUM(est_tokens) as tokens
        FROM steps
        WHERE phase IS NOT NULL
        GROUP BY phase
        ORDER BY tokens DESC
    """).fetchall()
    for p in phases:
        tokens = p["tokens"] or 0
        share = (tokens / total_tokens * 100) if total_tokens else 0.0
        p_name = p["phase"].capitalize()
        print(f"  {p_name:<16} | {p['steps']:<14,}| {format_tokens(tokens):<14} | {share:5.1f}%")

    print("\n[2] Top 8 Largest Single-Step Context Injections (Bloat Culprits):")
    print(f"  {'Step #':<8} | {'Phase':<12} | {'Tool / Source':<20} | {'Tokens':<10} | {'Detail':<20}")
    print("  " + "-" * 75)
    bloat_rows = cur.execute("""
        SELECT step_index, phase, tool_or_source, est_tokens, detail
        FROM steps
        WHERE est_tokens > 2000
        ORDER BY est_tokens DESC
        LIMIT 8
    """).fetchall()
    for r in bloat_rows:
        detail_snippet = (r["detail"] or "")[:20]
        print(f"  #{r['step_index']:<7} | {(r['phase'] or 'other'):<12} | {r['tool_or_source']:<20} | ~{format_tokens(r['est_tokens']):<9} | {detail_snippet:<20}")
    print("=" * 75 + "\n")

def print_velocity(conn):
    print("\n" + "=" * 75)
    print(" ⚡ DEVELOPER VELOCITY & AUTONOMOUS ROI METRICS")
    print("=" * 75)

    cur = conn.cursor()
    row = cur.execute("""
        SELECT 
            COUNT(*) as total_convs,
            SUM(total_steps) as total_steps,
            SUM(user_prompts) as total_prompts,
            SUM(tool_calls_count) as total_tools,
            SUM(thinking_tokens) as total_thinking,
            SUM(lines_written) as total_written,
            SUM(lines_edited) as total_edited,
            SUM(failed_tool_calls) as total_fails,
            SUM(active_agent_seconds) as total_agent_sec
        FROM conversations
    """).fetchone()

    total_prompts = row["total_prompts"] or 0
    total_tools = row["total_tools"] or 0
    total_thinking = row["total_thinking"] or 0
    total_written = row["total_written"] or 0
    total_edited = row["total_edited"] or 0
    total_fails = row["total_fails"] or 0
    total_agent_sec = row["total_agent_sec"] or 0

    autonomy = total_tools / max(1, total_prompts)
    success_rate = ((total_tools - total_fails) / max(1, total_tools)) * 100

    reads_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('view_file', 'grep_search', 'find_by_name', 'list_dir')").fetchone()[0]
    cmds_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'run_command'").fetchone()[0]
    edits_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content')").fetchone()[0]
    
    saved_seconds = (reads_cnt * 30) + (cmds_cnt * 45) + (edits_cnt * 120)
    saved_hours = saved_seconds / 3600

    print(f" Autonomy Multiplier       : {autonomy:.1f}x ({total_tools:,} tool actions across {total_prompts:,} user prompts)")
    print(f" Deep Reasoning Volume     : {format_tokens(total_thinking)} thinking tokens (internal chain-of-thought)")
    print(f" Codebase Production Lines : {total_written + total_edited:,} lines (+{total_written:,} written / ~{total_edited:,} modified)")
    print(f" Tool Success & Resilience : {success_rate:.1f}% first-attempt execution success ({total_fails:,} errors)")
    print(f" Active Agent Work Time    : {format_duration(total_agent_sec)} of pure LLM execution & tool inference")
    print(f" Estimated Dev Time Saved  : ~{saved_hours:,.0f} engineering hours saved to date")
    print("=" * 75 + "\n")

def print_tools(conn, mcp_only=False, native_only=False):
    title = "TOOL INVOCATION ANALYTICS & RANKINGS"
    if mcp_only:
        title = "MCP TOOL INVOCATIONS & RANKINGS"
    elif native_only:
        title = "NATIVE BUILT-IN TOOL INVOCATIONS & RANKINGS"

    print("\n" + "=" * 75)
    print(f" 🛠️  {title}")
    print("=" * 75)
    cur = conn.cursor()

    where_clause = ""
    if mcp_only:
        where_clause = "WHERE mcp_server IS NOT NULL"
    elif native_only:
        where_clause = "WHERE mcp_server IS NULL"

    rows = cur.execute(f"""
        SELECT 
            tool_name, 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as distinct_sessions,
            AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
        FROM tool_executions
        {where_clause}
        GROUP BY tool_name
        ORDER BY total_calls DESC
    """).fetchall()

    print(f"  {'Tool Name':<34} | {'Server':<14} | {'Calls':<8} | {'Sessions':<10} | {'Error Rate':<10}")
    print("  " + "-" * 75)
    for r in rows:
        srv = r["mcp_server"] or "native"
        print(f"  {r['tool_name']:<34} | {srv:<14} | {r['total_calls']:<8,}| {r['distinct_sessions']:<10,}| {r['error_rate']:5.1f}%")
    print("=" * 75 + "\n")

def print_hot_files(conn):
    print("\n" + "=" * 85)
    print(" 🔥 TOP ACCESSED & MODIFIED REPOSITORY HOTSPOTS")
    print("=" * 85)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT 
            target_path,
            COUNT(*) as total_touches,
            COUNT(CASE WHEN tool_name = 'view_file' THEN 1 END) as views,
            COUNT(CASE WHEN tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content') THEN 1 END) as edits,
            COUNT(DISTINCT conversation_id) as sessions
        FROM tool_executions
        WHERE target_path IS NOT NULL AND target_path != ''
        GROUP BY target_path
        ORDER BY total_touches DESC
        LIMIT 20
    """).fetchall()

    print(f"  {'File / Directory Path':<42} | {'Touches':<8} | {'Views':<6} | {'Edits':<6} | {'Sessions':<8}")
    print("  " + "-" * 78)
    for r in rows:
        p = Path(r["target_path"].strip('\"\'')).name or r["target_path"]
        if len(p) > 40: p = p[:37] + "..."
        print(f"  {p:<42} | {r['total_touches']:<8,}| {r['views']:<6,}| {r['edits']:<6,}| {r['sessions']:<8,}")
    print("=" * 85 + "\n")

def print_timeline(conn, days: int = 14):
    local_tz = datetime.now().astimezone().tzinfo
    tz_abbr = datetime.now().astimezone().strftime('%Z')
    print("\n" + "=" * 75)
    print(f" 📅 ACTIVITY TIMELINE (LAST {days} DAYS — {tz_abbr})")
    print("=" * 75)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT created_at, conversation_id, est_tokens, billed_cost
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
    """).fetchall()

    daily_map = {}
    day_sessions = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(local_tz)
            d_str = dt.strftime("%Y-%m-%d")
            if d_str not in daily_map:
                daily_map[d_str] = {"sessions": 0, "steps": 0, "tokens": 0, "cost": 0.0}
                day_sessions[d_str] = set()
            daily_map[d_str]["steps"] += 1
            daily_map[d_str]["tokens"] += r["est_tokens"] or 0
            daily_map[d_str]["cost"] += r["billed_cost"] or 0.0
            day_sessions[d_str].add(r["conversation_id"])
        except Exception:
            pass

    for d_str, s_set in day_sessions.items():
        daily_map[d_str]["sessions"] = len(s_set)

    today = datetime.now(local_tz).date()
    timeline_days = []
    for i in range(days):
        d_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        timeline_days.append(d_str)

    print(f"  {'Date (' + tz_abbr + ')':<14} | {'Sessions':<10} | {'Steps Logged':<14} | {'Tokens Consumed':<16} | {'Est API Value':<14}")
    print("  " + "-" * 75)
    for day in timeline_days:
        info = daily_map.get(day, {"sessions": 0, "steps": 0, "tokens": 0, "cost": 0.0})
        print(f"  {day:<14} | {info['sessions']:<10,}| {info['steps']:<14,}| {format_tokens(info['tokens']):<16} | ${info['cost']:<13,.2f}")
    print("=" * 75 + "\n")

def generate_global_report(conn, output_path: Path = Path(os.path.expanduser("~/.gemini/antigravity/global_analytics.md"))):
    cur = conn.cursor()
    totals = cur.execute("""
        SELECT 
            COUNT(*) as total_convs,
            SUM(total_steps) as total_steps,
            SUM(total_tokens) as total_tokens,
            SUM(duration_seconds) as total_duration,
            SUM(active_agent_seconds) as total_agent_sec,
            SUM(thinking_tokens) as total_thinking,
            SUM(user_prompts) as total_prompts,
            SUM(tool_calls_count) as total_tools,
            SUM(redundant_file_reads) as total_re_reads,
            SUM(lines_written) as total_written,
            SUM(lines_edited) as total_edited,
            MIN(created_at) as earliest,
            MAX(updated_at) as latest
        FROM conversations
    """).fetchone()

    total_cost, total_in, total_out = calculate_billed_cost(conn)
    total_tokens = totals["total_tokens"] or 0
    autonomy = (totals["total_tools"] or 0) / max(1, totals["total_prompts"] or 1)

    mcp_row = cur.execute("SELECT COUNT(*) as mcp_calls, COUNT(DISTINCT mcp_server) as servers FROM tool_executions WHERE mcp_server IS NOT NULL").fetchone()
    total_mcp_calls = mcp_row["mcp_calls"] or 0
    total_mcp_servers = mcp_row["servers"] or 0

    lines = [
        "# Antigravity Global Analytics & Intelligence Report",
        f"\n> **Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> **Total Sessions**: {totals['total_convs']:,} conversations  ",
        f"> **Total Operations**: {totals['total_steps']:,} logged steps  ",
        f"> **Autonomy Multiplier**: **{autonomy:.1f}x actions per prompt**  ",
        f"> **Deep Reasoning Tokens**: **{format_tokens(totals['total_thinking'] or 0)}**  ",
        f"> **Code Authored & Refactored**: **{(totals['total_written'] or 0) + (totals['total_edited'] or 0):,} lines**  ",
        f"> **MCP Invocations**: **{total_mcp_calls:,} calls** across {total_mcp_servers} servers  ",
        f"> **Redundant File Reads**: {totals['total_re_reads']:,}  ",
        f"> **Cumulative Multi-Turn Billed Input**: **{format_tokens(total_in)} tokens**  ",
        f"> **True Equivalent API Billing Value**: **${total_cost:,.2f}**  ",
        f"> **Agent Working Time**: {format_duration(totals['total_agent_sec'] or 0)}\n",
        "## 1. Context Consumption by Activity Phase\n",
        "| Development Phase | Estimated Tokens | Share |",
        "| :--- | :--- | :--- |"
    ]

    for r in cur.execute("SELECT phase, SUM(est_tokens) as tokens FROM steps WHERE phase IS NOT NULL GROUP BY phase ORDER BY tokens DESC"):
        share = (r["tokens"] / total_tokens * 100) if total_tokens else 0.0
        lines.append(f"| {r['phase'].capitalize()} | {format_tokens(r['tokens'])} | {share:.1f}% |")

    lines.extend([
        "\n## 2. Top Tool Invocations\n",
        "| Tool Name | Total Invocations | Distinct Sessions |",
        "| :--- | :--- | :--- |"
    ])

    for r in cur.execute("SELECT tool_name, COUNT(*) as calls, COUNT(DISTINCT conversation_id) as sess FROM tool_executions GROUP BY tool_name ORDER BY calls DESC LIMIT 15"):
        lines.append(f"| `{r['tool_name']}` | {r['calls']:,} | {r['sess']:,} |")

    lines.extend([
        "\n## 3. Model Context Protocol (MCP) Ecosystem\n",
        "| MCP Server | Total Invocations | Distinct Sessions | Top Specific Tool |",
        "| :--- | :--- | :--- | :--- |"
    ])

    mcp_servers_list = cur.execute("""
        SELECT 
            mcp_server,
            COUNT(*) as calls,
            COUNT(DISTINCT conversation_id) as sess
        FROM tool_executions
        WHERE mcp_server IS NOT NULL
        GROUP BY mcp_server
        ORDER BY calls DESC
    """).fetchall()

    lines.extend([
        "\n## 4. Model Ecosystem Distribution (Step-Level Exact)\n",
        "| Model Name | Logged Steps | Estimated Tokens | Share |",
        "| :--- | :--- | :--- | :--- |"
    ])

    model_rows = cur.execute("""
        SELECT COALESCE(s.model_name, c.model_name, 'Gemini Flash') as mname,
               COUNT(*) as step_count,
               SUM(s.est_tokens) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        GROUP BY mname
        ORDER BY tokens DESC
    """).fetchall()
    tot_model_tokens = sum(r["tokens"] or 0 for r in model_rows)
    for r in model_rows:
        toks = r["tokens"] or 0
        share = (toks / tot_model_tokens * 100) if tot_model_tokens else 0.0
        lines.append(f"| `{r['mname'] or 'Unknown'}` | {r['step_count']:,} | {format_tokens(toks)} | {share:.1f}% |")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n📄 Global Analytics Markdown Report written to: {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Antigravity Global Analytics Query Tool")
    parser.add_argument("--summary", action="store_true", help="Display overall system summary")
    parser.add_argument("--mcp", action="store_true", help="Display MCP server and tool ecosystem analytics")
    parser.add_argument("--efficiency", action="store_true", help="Display context health, redundant reads, and bloat diagnostics")
    parser.add_argument("--velocity", action="store_true", help="Display developer velocity, autonomy multiplier, and ROI metrics")
    parser.add_argument("--tools", action="store_true", help="Display detailed tool analytics")
    parser.add_argument("--mcp-only", action="store_true", help="Filter tool rankings to MCP tools only")
    parser.add_argument("--native-only", action="store_true", help="Filter tool rankings to native tools only")
    parser.add_argument("--hot-files", action="store_true", help="Display top touched files")
    parser.add_argument("--timeline", type=int, nargs="?", const=14, help="Display activity timeline for N days (default: 14)")
    parser.add_argument("--models", action="store_true", help="Display model comparison")
    parser.add_argument("--report", action="store_true", help="Generate global_analytics.md artifact")
    parser.add_argument("--json", action="store_true", help="Output summary as raw JSON")

    args = parser.parse_args()

    if not DEFAULT_DB_PATH.exists():
        print(f"Database not found at {DEFAULT_DB_PATH}. Please run backfill_analytics.py first.")
        sys.exit(1)

    conn = get_db_connection()
    try:
        if args.mcp:
            print_mcp(conn)
        elif args.efficiency:
            print_efficiency(conn)
        elif args.velocity:
            print_velocity(conn)
        elif args.tools:
            print_tools(conn, mcp_only=args.mcp_only, native_only=args.native_only)
        elif args.hot_files:
            print_hot_files(conn)
        elif args.timeline is not None:
            print_timeline(conn, args.timeline)
        elif args.report:
            generate_global_report(conn)
        elif args.json:
            cur = conn.cursor()
            totals = cur.execute("SELECT COUNT(*) as convs, SUM(total_steps) as steps, SUM(total_tokens) as tokens FROM conversations").fetchone()
            print(json.dumps(dict(totals), indent=2))
        else:
            print_summary(conn)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
