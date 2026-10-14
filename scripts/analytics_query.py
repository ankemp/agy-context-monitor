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

_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import get_db_connection, DEFAULT_DB_PATH  # noqa: E402
from context_monitor.pricing import (  # noqa: E402
    calculate_step_cost,
)
from context_monitor.quota import (  # noqa: E402
    calculate_quota_metrics,
    sync_and_get_quota_state,
)
from context_monitor.settings import (  # noqa: E402
    DEFAULT_3P_5H_CAPACITY,
    DEFAULT_3P_WEEKLY_CAPACITY,
    DEFAULT_GEMINI_5H_CAPACITY,
    DEFAULT_GEMINI_WEEKLY_CAPACITY,
)


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
        row = cur.execute(
            "SELECT SUM(billed_cost), SUM(billed_input_tokens), SUM(billed_output_tokens) FROM steps"
        ).fetchone()
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

        if s_type == "CHECKPOINT":
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            step_cost, step_in, step_out = calculate_step_cost(
                running_ctx, current_ephemeral, tokens, model_name
            )
            total_cost += step_cost
            total_in += step_in
            total_out += step_out
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

    multi_row = cur.execute(
        "SELECT COUNT(*) FROM conversations WHERE models_used LIKE '%,%' OR model_name LIKE '%→%'"
    ).fetchone()
    multi_convs = multi_row[0] if multi_row else 0

    mcp_row = cur.execute(
        "SELECT COUNT(*) as mcp_calls, COUNT(DISTINCT mcp_server) as servers FROM tool_executions WHERE mcp_server IS NOT NULL"
    ).fetchone()
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

    print(
        f" Total Conversations Tracked : {total_convs:,} ({multi_convs:,} multi-model)"
    )
    print(
        f" Total Logged Steps          : {total_steps:,} (avg {avg_steps:.1f} steps/session)"
    )
    print(
        f" Static Disk Token Footprint : {format_tokens(total_tokens)} (~{total_chars:,} chars)"
    )
    print(
        f" Cumulative Multi-Turn Input : {format_tokens(total_in)} tokens (area under curve)"
    )
    print(f" Cumulative Output / Thinking: {format_tokens(total_out)} tokens")
    print(f" True Equivalent API Value   : ${total_cost:,.2f}")
    print(
        f" Pure Agent Execution Time   : {format_duration(total_agent_work)} (active LLM reasoning & tool calls)"
    )
    print(
        f" Active Collaborative Time   : {format_duration(total_pairing_work)} (continuous interaction <= 5m gaps)"
    )
    print(
        f" Total Wall-Clock Span       : {format_duration(total_duration)} (across all open sessions)"
    )
    print(
        f" Autonomy Leverage Ratio     : {autonomy_ratio:.1f}x automated actions per prompt ({total_tools:,} tools / {total_prompts:,} prompts)"
    )
    print(
        f" Deep Reasoning Tokens       : {format_tokens(total_thinking)} thinking tokens"
    )
    print(
        f" Total Code Authored/Edited  : {total_written + total_edited:,} lines (+{total_written:,} written / ~{total_edited:,} edited)"
    )
    print(
        f" MCP Tool Invocations        : {total_mcp_calls:,} calls across {total_mcp_servers} servers"
    )
    print(
        f" Redundant File Re-Reads     : {total_re_reads:,} repeated reads across active sessions"
    )
    quota_state = sync_and_get_quota_state(conn)
    q_metrics = calculate_quota_metrics(billed_cost=total_cost, quota_state=quota_state)
    gw = q_metrics.get("gemini-weekly", {})
    g5 = q_metrics.get("gemini-5h", {})
    tpw = q_metrics.get("3p-weekly", {})
    tp5 = q_metrics.get("3p-5h", {})
    print(
        f" Gemini Weekly Quota         : {gw.get('quota_remaining_pct', 100):.1f}% remaining (resets in {gw.get('reset_countdown') or 'n/a'}) [Cap: ${gw.get('calibrated_capacity', DEFAULT_GEMINI_WEEKLY_CAPACITY):.1f}]"
    )
    print(
        f" Gemini 5-Hour Quota         : {g5.get('quota_remaining_pct', 100):.1f}% remaining (resets in {g5.get('reset_countdown') or 'n/a'}) [Cap: ${g5.get('calibrated_capacity', DEFAULT_GEMINI_5H_CAPACITY):.1f}]"
    )
    print(
        f" Claude/3P Weekly Quota      : {tpw.get('quota_remaining_pct', 100):.1f}% remaining (resets in {tpw.get('reset_countdown') or 'n/a'}) [Cap: ${tpw.get('calibrated_capacity', DEFAULT_3P_WEEKLY_CAPACITY):.1f}]"
    )
    print(
        f" Claude/3P 5-Hour Quota      : {tp5.get('quota_remaining_pct', 100):.1f}% remaining (resets in {tp5.get('reset_countdown') or 'n/a'}) [Cap: ${tp5.get('calibrated_capacity', DEFAULT_3P_5H_CAPACITY):.1f}]"
    )
    if row["earliest"] and row["latest"]:
        try:
            d_min = (
                datetime.fromisoformat(row["earliest"].replace("Z", "+00:00"))
                .astimezone()
                .strftime("%Y-%m-%d")
            )
            d_max = (
                datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
                .astimezone()
                .strftime("%Y-%m-%d")
            )
            print(
                f" Tracking Date Range         : {d_min} to {d_max} ({datetime.now().astimezone().strftime('%Z')})"
            )
        except Exception:
            print(
                f" Tracking Date Range         : {row['earliest'][:10]} to {row['latest'][:10]}"
            )
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
               SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        GROUP BY mname
        ORDER BY tokens DESC
    """).fetchall()
    tot_model_tokens = sum(r["tokens"] or 0 for r in model_rows)
    for r in model_rows:
        toks = r["tokens"] or 0
        share = (toks / tot_model_tokens * 100) if tot_model_tokens else 0.0
        print(
            f"  {(r['mname'] or 'Unknown'):<30} | {r['step_count']:<10,}| {format_tokens(toks):<12} | {share:5.1f}%"
        )

    print("\n[4] Cost & Context by Project:")
    print(
        f"  {'Project':<28} | {'Cost':<10} | {'Share':<7} | {'Sessions':<10} | {'Tokens':<10}"
    )
    print("  " + "-" * 72)
    proj_rows = cur.execute("""
        SELECT 
            COALESCE(NULLIF(project, ''), '(unassigned)') as project_name,
            COUNT(*) as session_count,
            SUM(total_tokens) as total_tokens,
            SUM(billed_cost) as billed_cost,
            SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) as actual_tokens
        FROM conversations
        GROUP BY project_name
        ORDER BY billed_cost DESC, actual_tokens DESC
    """).fetchall()
    tot_proj_cost = sum(r["billed_cost"] or 0.0 for r in proj_rows)
    for r in proj_rows:
        c = r["billed_cost"] or 0.0
        pct = (c / tot_proj_cost * 100) if tot_proj_cost else 0.0
        act_tok = r["actual_tokens"] or 0
        print(
            f"  {(r['project_name']):<28} | ${c:<9.2f}| {pct:5.1f}% | {r['session_count']:<10,}| {format_tokens(act_tok):<10}"
        )
    print("=" * 70 + "\n")


def print_quota(conn, cid: str | None = None):
    quota_state = sync_and_get_quota_state(conn)
    metrics_ref = calculate_quota_metrics(billed_cost=0.0, quota_state=quota_state)
    gw = metrics_ref.get("gemini-weekly", {})
    g5 = metrics_ref.get("gemini-5h", {})
    tpw = metrics_ref.get("3p-weekly", {})
    tp5 = metrics_ref.get("3p-5h", {})

    if not cid:
        print("\n" + "=" * 75)
        print(" ANTIGRAVITY SUBSCRIPTION QUOTA & RATE LIMIT STATUS")
        print("=" * 75)
        print(
            f" Gemini Weekly Quota    : {gw.get('quota_remaining_pct', 100):.1f}% remaining | Reset: {gw.get('reset_countdown') or 'n/a'} | Cap: ${gw.get('calibrated_capacity', DEFAULT_GEMINI_WEEKLY_CAPACITY):.1f}"
        )
        print(
            f" Gemini 5-Hour Quota    : {g5.get('quota_remaining_pct', 100):.1f}% remaining | Reset: {g5.get('reset_countdown') or 'n/a'} | Cap: ${g5.get('calibrated_capacity', DEFAULT_GEMINI_5H_CAPACITY):.1f}"
        )
        print(
            f" Claude/3P Weekly Quota : {tpw.get('quota_remaining_pct', 100):.1f}% remaining | Reset: {tpw.get('reset_countdown') or 'n/a'} | Cap: ${tpw.get('calibrated_capacity', DEFAULT_3P_WEEKLY_CAPACITY):.1f}"
        )
        print(
            f" Claude/3P 5-Hour Quota : {tp5.get('quota_remaining_pct', 100):.1f}% remaining | Reset: {tp5.get('reset_countdown') or 'n/a'} | Cap: ${tp5.get('calibrated_capacity', DEFAULT_3P_5H_CAPACITY):.1f}"
        )
        print("-" * 75)

        cur = conn.cursor()
        tot_cost, _, _ = calculate_billed_cost(conn)
        print(f" Total Cumulative API Billing  : ${tot_cost:,.2f}")
        print("\n Top 5 Sessions by Quota Impact:")
        print(
            f"  {'Session ID':<36} | {'Model':<20} | {'Cost':<9} | {'% Wk Quota':<10}"
        )
        print("  " + "-" * 79)
        top_convs = cur.execute("""
            SELECT id, model_name, billed_cost
            FROM conversations
            ORDER BY billed_cost DESC
            LIMIT 5
        """).fetchall()
        for tc in top_convs:
            c_cost = tc["billed_cost"] or 0.0
            m_name = tc["model_name"] or "Gemini"
            q_m = calculate_quota_metrics(
                billed_cost=c_cost, model_name=m_name, quota_state=quota_state
            )
            print(
                f"  {tc['id']:<36} | {m_name[:20]:<20} | ${c_cost:<8.2f} | {q_m['weekly_pct']:>8.4f}%"
            )
        print("=" * 75 + "\n")
        return

    cur = conn.cursor()
    conv = cur.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if not conv:
        conv = cur.execute(
            "SELECT * FROM conversations WHERE id LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"{cid}%",),
        ).fetchone()
    if not conv:
        print(f"Conversation '{cid}' not found in database.")
        return

    full_cid = conv["id"]
    model_name = conv["model_name"] or "Gemini"
    sess_cost = conv["billed_cost"] or 0.0
    sess_steps = conv["total_steps"] or 0
    sess_q = calculate_quota_metrics(
        billed_cost=sess_cost, model_name=model_name, quota_state=quota_state
    )

    subagent_rows = cur.execute(
        """
        SELECT id, subagent_role, subagent_type, model_name, billed_cost, total_steps
        FROM conversations
        WHERE (parent_id = ? OR root_id = ?) AND id != ?
        ORDER BY billed_cost DESC
    """,
        (full_cid, full_cid, full_cid),
    ).fetchall()

    subagents_cost = sum(r["billed_cost"] or 0.0 for r in subagent_rows)
    fleet_cost = sess_cost + subagents_cost
    fleet_q = calculate_quota_metrics(
        billed_cost=fleet_cost, model_name=model_name, quota_state=quota_state
    )
    subagents_q = calculate_quota_metrics(
        billed_cost=subagents_cost, model_name=model_name, quota_state=quota_state
    )

    step_rows = cur.execute(
        """
        SELECT step_index, step_type, activity, tool_or_source, billed_cost, est_tokens
        FROM steps
        WHERE conversation_id = ?
        ORDER BY billed_cost DESC
    """,
        (full_cid,),
    ).fetchall()

    max_turn_cost = 0.0
    max_turn_pct = 0.0
    max_step_idx = 0
    avg_turn_cost = 0.0
    avg_turn_pct = 0.0

    if step_rows:
        max_r = step_rows[0]
        max_turn_cost = max_r["billed_cost"] or 0.0
        max_q = calculate_quota_metrics(
            billed_cost=max_turn_cost, model_name=model_name, quota_state=quota_state
        )
        max_turn_pct = max_q["weekly_pct"]
        max_step_idx = max_r["step_index"]

        total_step_cost = sum(sr["billed_cost"] or 0.0 for sr in step_rows)
        avg_turn_cost = total_step_cost / len(step_rows)
        avg_q = calculate_quota_metrics(
            billed_cost=avg_turn_cost, model_name=model_name, quota_state=quota_state
        )
        avg_turn_pct = avg_q["weekly_pct"]

    print("\n" + "=" * 75)
    print(f" CONVERSATION QUOTA BREAKDOWN: {full_cid}")
    print("=" * 75)
    print(f" Title       : {conv['title'] or '(untitled)'}")
    print(f" Project     : {conv['project'] or '(unassigned)'}")
    print(f" Model       : {model_name}")
    print(f" Steps       : {sess_steps:,}")
    print("-" * 75)
    print(" QUOTA IMPACT SUMMARY:")
    print(
        f"  Session Weekly Quota Used  : {sess_q['weekly_pct']:>8.4f}% (${sess_cost:.2f})"
    )
    print(f"  Session 5-Hour Quota Used  : {sess_q['five_hour_pct']:>8.4f}%")
    print(
        f"  Subagent Fleet Impact      : {subagents_q['weekly_pct']:>8.4f}% (${subagents_cost:.2f} across {len(subagent_rows)} subagents)"
    )
    print(
        f"  Total Fleet Weekly Quota   : {fleet_q['weekly_pct']:>8.4f}% (${fleet_cost:.2f})"
    )
    print(
        f"  Max Single Turn Impact     : {max_turn_pct:>8.4f}% (${max_turn_cost:.4f} at step #{max_step_idx})"
    )
    print(
        f"  Avg Turn Impact            : {avg_turn_pct:>8.4f}% (${avg_turn_cost:.4f}/turn)"
    )
    print("-" * 75)
    print(" LIVE SUBSCRIPTION BUCKET STATUS:")
    b_pre = sess_q["bucket_prefix"]
    b_wk = metrics_ref.get(f"{b_pre}-weekly", {})
    b_5h = metrics_ref.get(f"{b_pre}-5h", {})
    def_wk = (
        DEFAULT_3P_WEEKLY_CAPACITY if b_pre == "3p" else DEFAULT_GEMINI_WEEKLY_CAPACITY
    )
    def_5h = DEFAULT_3P_5H_CAPACITY if b_pre == "3p" else DEFAULT_GEMINI_5H_CAPACITY
    print(
        f"  {b_pre.capitalize()} Weekly Remaining : {b_wk.get('quota_remaining_pct', 100):.1f}% (reset in {b_wk.get('reset_countdown') or 'n/a'}) [Cap: ${b_wk.get('calibrated_capacity', def_wk):.1f}]"
    )
    print(
        f"  {b_pre.capitalize()} 5-Hour Remaining : {b_5h.get('quota_remaining_pct', 100):.1f}% (reset in {b_5h.get('reset_countdown') or 'n/a'}) [Cap: ${b_5h.get('calibrated_capacity', def_5h):.1f}]"
    )

    if subagent_rows:
        print("\n SUBAGENT BREAKDOWN:")
        print(
            f"  {'Subagent ID':<36} | {'Role':<15} | {'Steps':<6} | {'Cost':<8} | {'% Wk Quota':<10}"
        )
        print("  " + "-" * 81)
        for sa in subagent_rows:
            sa_cost = sa["billed_cost"] or 0.0
            sa_m = sa["model_name"] or model_name
            sa_q = calculate_quota_metrics(
                billed_cost=sa_cost, model_name=sa_m, quota_state=quota_state
            )
            role = sa["subagent_role"] or sa["subagent_type"] or "subagent"
            print(
                f"  {sa['id']:<36} | {role[:15]:<15} | {sa['total_steps']:<6} | ${sa_cost:<7.2f} | {sa_q['weekly_pct']:>8.4f}%"
            )

    if step_rows:
        print("\n TOP 5 EXPENSIVE TURNS:")
        print(
            f"  {'Step':<6} | {'Type/Activity':<28} | {'Cost':<9} | {'% Wk Quota':<10}"
        )
        print("  " + "-" * 59)
        for st in step_rows[:5]:
            st_cost = st["billed_cost"] or 0.0
            st_q = calculate_quota_metrics(
                billed_cost=st_cost, model_name=model_name, quota_state=quota_state
            )
            act = st["activity"] or st["step_type"] or "step"
            print(
                f"  #{st['step_index']:<5} | {act[:28]:<28} | ${st_cost:<8.4f} | {st_q['weekly_pct']:>8.4f}%"
            )

    print("=" * 75 + "\n")


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

    sess_row = cur.execute(
        "SELECT COUNT(DISTINCT conversation_id) FROM tool_executions WHERE mcp_server IS NOT NULL"
    ).fetchone()
    mcp_sessions = sess_row[0] if sess_row else 0

    print(f" Total MCP Invocations Tracked : {total_mcp_calls:,}")
    print(f" Connected MCP Servers Active  : {distinct_servers}")
    print(f" Sessions Utilizing MCP Tools  : {mcp_sessions:,}")
    print("-" * 78)

    print("\n[1] MCP Server Invocations Leaderboard:")
    print(
        f"  {'MCP Server':<28} | {'Calls':<8} | {'Share':<6} | {'Sessions':<10} | {'Error Rate':<10}"
    )
    print("  " + "-" * 72)
    for r in server_rows:
        share = (r["total_calls"] / total_mcp_calls * 100) if total_mcp_calls else 0.0
        print(
            f"  {r['mcp_server']:<28} | {r['total_calls']:<8,}| {share:5.1f}% | {r['sessions']:<10,}| {r['error_rate']:5.1f}%"
        )

    print("\n[2] Detailed Tool Invocations by MCP Server:")
    for s_row in server_rows:
        server = s_row["mcp_server"]
        tool_rows = cur.execute(
            """
            SELECT 
                mcp_tool,
                COUNT(*) as calls,
                COUNT(DISTINCT conversation_id) as sessions,
                AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
            FROM tool_executions
            WHERE mcp_server = ? AND mcp_tool IS NOT NULL
            GROUP BY mcp_tool
            ORDER BY calls DESC
        """,
            (server,),
        ).fetchall()

        print(
            f"\n  📦 Server: {server.upper()} ({s_row['total_calls']:,} calls across {s_row['sessions']} sessions)"
        )
        print(
            f"     {'Specific Tool Name':<36} | {'Calls':<8} | {'Sessions':<10} | {'Error Rate':<10}"
        )
        print("     " + "-" * 70)
        for tr in tool_rows:
            t_name = tr["mcp_tool"] or "generic"
            print(
                f"     {t_name:<36} | {tr['calls']:<8,}| {tr['sessions']:<10,}| {tr['error_rate']:5.1f}%"
            )

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

    view_row = cur.execute(
        "SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'view_file'"
    ).fetchone()
    total_views = view_row[0] if view_row else 0
    re_read_rate = (total_re_reads / max(1, total_views)) * 100
    wasted_tokens = total_re_reads * 750

    print(f" Total File Views Tracked    : {total_views:,}")
    print(
        f" Redundant File Re-Reads     : {total_re_reads:,} ({re_read_rate:.1f}% re-read rate)"
    )
    print(
        f" Estimated Context Waste     : ~{format_tokens(wasted_tokens)} tokens spent re-reading existing context"
    )
    print("-" * 75)

    print("\n[1] Context Consumption by Development Phase:")
    print(
        f"  {'Phase':<16} | {'Logged Steps':<14} | {'Est. Tokens':<14} | {'Share':<6}"
    )
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
        print(
            f"  {p_name:<16} | {p['steps']:<14,}| {format_tokens(tokens):<14} | {share:5.1f}%"
        )

    print("\n[2] Top 8 Largest Single-Step Context Injections (Bloat Culprits):")
    print(
        f"  {'Step #':<8} | {'Phase':<12} | {'Tool / Source':<20} | {'Tokens':<10} | {'Detail':<20}"
    )
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
        print(
            f"  #{r['step_index']:<7} | {(r['phase'] or 'other'):<12} | {r['tool_or_source']:<20} | ~{format_tokens(r['est_tokens']):<9} | {detail_snippet:<20}"
        )
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

    reads_cnt = cur.execute(
        "SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('view_file', 'grep_search', 'find_by_name', 'list_dir')"
    ).fetchone()[0]
    cmds_cnt = cur.execute(
        "SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'run_command'"
    ).fetchone()[0]
    edits_cnt = cur.execute(
        "SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content')"
    ).fetchone()[0]

    saved_seconds = (reads_cnt * 30) + (cmds_cnt * 45) + (edits_cnt * 120)
    saved_hours = saved_seconds / 3600

    print(
        f" Autonomy Multiplier       : {autonomy:.1f}x ({total_tools:,} tool actions across {total_prompts:,} user prompts)"
    )
    print(
        f" Deep Reasoning Volume     : {format_tokens(total_thinking)} thinking tokens (internal chain-of-thought)"
    )
    print(
        f" Codebase Production Lines : {total_written + total_edited:,} lines (+{total_written:,} written / ~{total_edited:,} modified)"
    )
    print(
        f" Tool Success & Resilience : {success_rate:.1f}% first-attempt execution success ({total_fails:,} errors)"
    )
    print(
        f" Active Agent Work Time    : {format_duration(total_agent_sec)} of pure LLM execution & tool inference"
    )
    print(
        f" Estimated Dev Time Saved  : ~{saved_hours:,.0f} engineering hours saved to date"
    )
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

    print(
        f"  {'Tool Name':<34} | {'Server':<14} | {'Calls':<8} | {'Sessions':<10} | {'Error Rate':<10}"
    )
    print("  " + "-" * 75)
    for r in rows:
        srv = r["mcp_server"] or "native"
        print(
            f"  {r['tool_name']:<34} | {srv:<14} | {r['total_calls']:<8,}| {r['distinct_sessions']:<10,}| {r['error_rate']:5.1f}%"
        )
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

    print(
        f"  {'File / Directory Path':<42} | {'Touches':<8} | {'Views':<6} | {'Edits':<6} | {'Sessions':<8}"
    )
    print("  " + "-" * 78)
    for r in rows:
        p = Path(r["target_path"].strip("\"'")).name or r["target_path"]
        if len(p) > 40:
            p = p[:37] + "..."
        print(
            f"  {p:<42} | {r['total_touches']:<8,}| {r['views']:<6,}| {r['edits']:<6,}| {r['sessions']:<8,}"
        )
    print("=" * 85 + "\n")


def print_timeline(conn, days: int = 14):
    local_tz = datetime.now().astimezone().tzinfo
    tz_abbr = datetime.now().astimezone().strftime("%Z")
    print("\n" + "=" * 75)
    print(f" 📅 ACTIVITY TIMELINE (LAST {days} DAYS — {tz_abbr})")
    print("=" * 75)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT created_at, conversation_id, billed_input_tokens, billed_output_tokens, billed_cost
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
    """).fetchall()

    daily_map = {}
    day_sessions = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")
            ).astimezone(local_tz)
            d_str = dt.strftime("%Y-%m-%d")
            if d_str not in daily_map:
                daily_map[d_str] = {"sessions": 0, "steps": 0, "tokens": 0, "cost": 0.0}
                day_sessions[d_str] = set()
            daily_map[d_str]["steps"] += 1
            daily_map[d_str]["tokens"] += (r["billed_input_tokens"] or 0) + (
                r["billed_output_tokens"] or 0
            )
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

    print(
        f"  {'Date (' + tz_abbr + ')':<14} | {'Sessions':<10} | {'Steps Logged':<14} | {'Tokens Consumed':<16} | {'Est API Value':<14}"
    )
    print("  " + "-" * 75)
    for day in timeline_days:
        info = daily_map.get(day, {"sessions": 0, "steps": 0, "tokens": 0, "cost": 0.0})
        print(
            f"  {day:<14} | {info['sessions']:<10,}| {info['steps']:<14,}| {format_tokens(info['tokens']):<16} | ${info['cost']:<13,.2f}"
        )
    print("=" * 75 + "\n")


def generate_global_report(
    conn,
    output_path: Path = Path(
        os.path.expanduser("~/.gemini/antigravity/global_analytics.md")
    ),
):
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
    total_convs = totals["total_convs"] or 0
    total_steps = totals["total_steps"] or 0
    total_re_reads = totals["total_re_reads"] or 0
    total_tokens = totals["total_tokens"] or 0
    autonomy = (totals["total_tools"] or 0) / max(1, totals["total_prompts"] or 1)

    mcp_row = cur.execute(
        "SELECT COUNT(*) as mcp_calls, COUNT(DISTINCT mcp_server) as servers FROM tool_executions WHERE mcp_server IS NOT NULL"
    ).fetchone()
    total_mcp_calls = mcp_row["mcp_calls"] or 0
    total_mcp_servers = mcp_row["servers"] or 0

    lines = [
        "# Antigravity Global Analytics & Intelligence Report",
        f"\n> **Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> **Total Sessions**: {total_convs:,} conversations  ",
        f"> **Total Operations**: {total_steps:,} logged steps  ",
        f"> **Autonomy Multiplier**: **{autonomy:.1f}x actions per prompt**  ",
        f"> **Deep Reasoning Tokens**: **{format_tokens(totals['total_thinking'] or 0)}**  ",
        f"> **Code Authored & Refactored**: **{(totals['total_written'] or 0) + (totals['total_edited'] or 0):,} lines**  ",
        f"> **MCP Invocations**: **{total_mcp_calls:,} calls** across {total_mcp_servers} servers  ",
        f"> **Redundant File Reads**: {total_re_reads:,}  ",
        f"> **Cumulative Multi-Turn Billed Input**: **{format_tokens(total_in)} tokens**  ",
        f"> **True Equivalent API Billing Value**: **${total_cost:,.2f}**  ",
        f"> **Agent Working Time**: {format_duration(totals['total_agent_sec'] or 0)}\n",
        "## 1. Context Consumption by Activity Phase\n",
        "| Development Phase | Estimated Tokens | Share |",
        "| :--- | :--- | :--- |",
    ]

    for r in cur.execute(
        "SELECT phase, SUM(est_tokens) as tokens FROM steps WHERE phase IS NOT NULL GROUP BY phase ORDER BY tokens DESC"
    ):
        share = (r["tokens"] / total_tokens * 100) if total_tokens else 0.0
        lines.append(
            f"| {r['phase'].capitalize()} | {format_tokens(r['tokens'])} | {share:.1f}% |"
        )

    lines.extend(
        [
            "\n## 2. Top Tool Invocations\n",
            "| Tool Name | Total Invocations | Distinct Sessions |",
            "| :--- | :--- | :--- |",
        ]
    )

    for r in cur.execute(
        "SELECT tool_name, COUNT(*) as calls, COUNT(DISTINCT conversation_id) as sess FROM tool_executions GROUP BY tool_name ORDER BY calls DESC LIMIT 15"
    ):
        lines.append(f"| `{r['tool_name']}` | {r['calls']:,} | {r['sess']:,} |")

    lines.extend(
        [
            "\n## 3. Model Context Protocol (MCP) Ecosystem\n",
            "| MCP Server | Total Invocations | Distinct Sessions | Top Specific Tool |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )

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

    for ms in mcp_servers_list:
        top_tool_row = cur.execute(
            "SELECT mcp_tool FROM tool_executions WHERE mcp_server = ? AND mcp_tool IS NOT NULL GROUP BY mcp_tool ORDER BY COUNT(*) DESC LIMIT 1",
            (ms["mcp_server"],),
        ).fetchone()
        top_tool = top_tool_row[0] if top_tool_row and top_tool_row[0] else "generic"
        lines.append(
            f"| `{ms['mcp_server']}` | {ms['calls']:,} | {ms['sess']:,} | `{top_tool}` |"
        )

    lines.extend(
        [
            "\n## 4. Model Ecosystem Distribution (Step-Level Exact)\n",
            "| Model Name | Logged Steps | API Tokens Consumed | Share |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )

    model_rows = cur.execute("""
        SELECT COALESCE(s.model_name, c.model_name, 'Gemini Flash') as mname,
               COUNT(*) as step_count,
               SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        GROUP BY mname
        ORDER BY tokens DESC
    """).fetchall()
    tot_model_tokens = sum(r["tokens"] or 0 for r in model_rows)
    for r in model_rows:
        toks = r["tokens"] or 0
        share = (toks / tot_model_tokens * 100) if tot_model_tokens else 0.0
        lines.append(
            f"| `{r['mname'] or 'Unknown'}` | {r['step_count']:,} | {format_tokens(toks)} | {share:.1f}% |"
        )

    quota_state = sync_and_get_quota_state(conn)
    q_metrics = calculate_quota_metrics(billed_cost=total_cost, quota_state=quota_state)
    gw = q_metrics.get("gemini-weekly", {})
    g5 = q_metrics.get("gemini-5h", {})
    tpw = q_metrics.get("3p-weekly", {})
    tp5 = q_metrics.get("3p-5h", {})

    lines.extend(
        [
            "\n## 5. Subscription Quotas & Rate Limits\n",
            "| Quota Bucket | Window | Remaining % | Reset Countdown | Calibrated Capacity |",
            "| :--- | :--- | :--- | :--- | :--- |",
            f"| Gemini Models | Weekly | {gw.get('quota_remaining_pct', 100):.1f}% | {gw.get('reset_countdown') or 'n/a'} | ${gw.get('calibrated_capacity', DEFAULT_GEMINI_WEEKLY_CAPACITY):.2f} |",
            f"| Gemini Models | 5-Hour | {g5.get('quota_remaining_pct', 100):.1f}% | {g5.get('reset_countdown') or 'n/a'} | ${g5.get('calibrated_capacity', DEFAULT_GEMINI_5H_CAPACITY):.2f} |",
            f"| Claude / 3P Models | Weekly | {tpw.get('quota_remaining_pct', 100):.1f}% | {tpw.get('reset_countdown') or 'n/a'} | ${tpw.get('calibrated_capacity', DEFAULT_3P_WEEKLY_CAPACITY):.2f} |",
            f"| Claude / 3P Models | 5-Hour | {tp5.get('quota_remaining_pct', 100):.1f}% | {tp5.get('reset_countdown') or 'n/a'} | ${tp5.get('calibrated_capacity', DEFAULT_3P_5H_CAPACITY):.2f} |",
        ]
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\nGlobal Analytics Markdown Report written to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Antigravity Global Analytics Query Tool"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Display overall system summary"
    )
    parser.add_argument(
        "--quota",
        action="store_true",
        help="Display subscription quota and rate limit status",
    )
    parser.add_argument(
        "--cid",
        type=str,
        default=None,
        help="Filter quota breakdown by conversation ID",
    )
    parser.add_argument(
        "--conversation", type=str, default=None, help="Alias for --cid"
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Display MCP server and tool ecosystem analytics",
    )
    parser.add_argument(
        "--efficiency",
        action="store_true",
        help="Display context health, redundant reads, and bloat diagnostics",
    )
    parser.add_argument(
        "--velocity",
        action="store_true",
        help="Display developer velocity, autonomy multiplier, and ROI metrics",
    )
    parser.add_argument(
        "--tools", action="store_true", help="Display detailed tool analytics"
    )
    parser.add_argument(
        "--mcp-only", action="store_true", help="Filter tool rankings to MCP tools only"
    )
    parser.add_argument(
        "--native-only",
        action="store_true",
        help="Filter tool rankings to native tools only",
    )
    parser.add_argument(
        "--hot-files", action="store_true", help="Display top touched files"
    )
    parser.add_argument(
        "--timeline",
        type=int,
        nargs="?",
        const=14,
        help="Display activity timeline for N days (default: 14)",
    )
    parser.add_argument(
        "--models", action="store_true", help="Display model comparison"
    )
    parser.add_argument(
        "--report", action="store_true", help="Generate global_analytics.md artifact"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output summary as raw JSON"
    )

    args = parser.parse_args()

    if not DEFAULT_DB_PATH.exists():
        print(
            f"Database not found at {DEFAULT_DB_PATH}. Please run backfill_analytics.py first."
        )
        sys.exit(1)

    conn = get_db_connection()
    try:
        if args.quota or args.cid or args.conversation:
            print_quota(conn, cid=args.cid or args.conversation)
        elif args.mcp:
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
            totals = cur.execute(
                "SELECT COUNT(*) as convs, SUM(total_steps) as steps, SUM(total_tokens) as tokens FROM conversations"
            ).fetchone()
            print(json.dumps(dict(totals), indent=2))
        else:
            print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
