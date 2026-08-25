#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Web-Based Analytics GUI Dashboard Server & Privacy-Safe HTML Exporter
--------------------------------------------------------------------------------
Launches a modern, interactive web-based dashboard on http://localhost:8765
that directly queries ~/.gemini/antigravity/analytics.db to visualize token usage,
daily velocity, model distribution, tool analytics, codebase hotspots,
authentic GitHub-style square contribution heatmaps across ALL zoom levels (Year / 3M / 30D Hourly / 7D Hourly / 24x7 Agg),
real multi-turn API billing calculations, local timezone intelligence, and deep-dive conversation replays.

Exporting:
  uv run dashboard.py --export-html report.html               # Privacy-Safe Export (Excludes Convo Explorer & Live Controls)
  uv run dashboard.py --export-html report.html --include-conversations  # Full Export with Convo Explorer
"""

import os
import sys
import re
import json
import time
import queue
import socket
import threading
import argparse
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

try:
    from db import get_db_connection, DEFAULT_DB_PATH
except ImportError:
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir))
    from db import get_db_connection, DEFAULT_DB_PATH

# ---------------------------------------------------------------------------
# Server-Sent Events (SSE) Broadcaster & SQLite File Watcher
# ---------------------------------------------------------------------------
SSE_CLIENTS = set()
SSE_LOCK = threading.Lock()

def broadcast_sse(event_name: str, data: dict):
    """Pushes an SSE event to all connected browser subscriber queues."""
    with SSE_LOCK:
        dead = set()
        for q in SSE_CLIENTS:
            try:
                q.put_nowait({"event": event_name, "data": data})
            except Exception:
                dead.add(q)
        SSE_CLIENTS.difference_update(dead)

class DbChangeWatcher(threading.Thread):
    """Monitors SQLite database and WAL files for modifications to push instant SSE events."""
    def __init__(self, db_path: Path, check_interval: float = 0.5):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.wal_path = Path(str(db_path) + "-wal")
        self.check_interval = check_interval
        self.last_state = self._get_state()

    def _get_state(self):
        db_mtime = self.db_path.stat().st_mtime if self.db_path.exists() else 0
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        wal_mtime = self.wal_path.stat().st_mtime if self.wal_path.exists() else 0
        wal_size = self.wal_path.stat().st_size if self.wal_path.exists() else 0
        return (db_mtime, db_size, wal_mtime, wal_size)

    def run(self):
        while True:
            time.sleep(self.check_interval)
            try:
                cur_state = self._get_state()
                if cur_state != self.last_state:
                    self.last_state = cur_state
                    # Short debounce to let multi-step batch writes settle
                    time.sleep(0.3)
                    self.last_state = self._get_state()
                    broadcast_sse("db_update", {"timestamp": datetime.now().isoformat()})
            except Exception:
                pass

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

def query_summary_data(conn, tz=None):
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

    total_prompts = row["total_prompts"] or 0
    total_tools = row["total_tools"] or 0
    autonomy_multiplier = round(total_tools / max(1, total_prompts), 1)
    total_code_lines = (row["total_written"] or 0) + (row["total_edited"] or 0)
    total_fails = row["total_fails"] or 0
    resilience_rate = round(((total_tools - total_fails) / max(1, total_tools)) * 100, 1)

    view_row = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'view_file'").fetchone()
    total_views = view_row[0] if view_row else 0
    total_re_reads = row["total_re_reads"] or 0
    re_read_rate = round((total_re_reads / max(1, total_views)) * 100, 1)
    re_read_waste_tokens = total_re_reads * 750

    reads_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('view_file', 'grep_search', 'find_by_name', 'list_dir')").fetchone()[0]
    cmds_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name = 'run_command'").fetchone()[0]
    edits_cnt = cur.execute("SELECT COUNT(*) FROM tool_executions WHERE tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content')").fetchone()[0]
    saved_hours = round(((reads_cnt * 30) + (cmds_cnt * 45) + (edits_cnt * 120)) / 3600)

    # Phase breakdown
    phase_rows = cur.execute("""
        SELECT phase, COUNT(*) as steps, SUM(est_tokens) as tokens
        FROM steps
        WHERE phase IS NOT NULL
        GROUP BY phase
        ORDER BY tokens DESC
    """).fetchall()
    phases = [{"phase": r["phase"], "steps": r["steps"], "tokens": r["tokens"] or 0} for r in phase_rows]

    # Top bloat steps
    bloat_rows = cur.execute("""
        SELECT step_index, phase, tool_or_source, est_tokens, detail
        FROM steps
        WHERE est_tokens > 2000
        ORDER BY est_tokens DESC
        LIMIT 8
    """).fetchall()
    bloat_steps = [{"step_index": r["step_index"], "phase": r["phase"] or "other", "tool_or_source": r["tool_or_source"], "est_tokens": r["est_tokens"], "detail": r["detail"]} for r in bloat_rows]

    cur.execute("""
        SELECT s.conversation_id, COALESCE(s.model_name, c.model_name, 'Gemini Flash'), s.step_type, s.est_tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        ORDER BY s.conversation_id, s.step_index ASC
    """)

    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    cur_conv = None
    running_ctx = 0

    for cid, model_name, s_type, tokens in cur.fetchall():
        if cid != cur_conv:
            cur_conv = cid
            running_ctx = 0
            
        cur_pkey = get_pricing_key(model_name)
        if s_type == "PLANNER_RESPONSE":
            in_rate_low, in_rate_high, out_rate = PRICING[cur_pkey]
            in_rate = in_rate_high if running_ctx > 128_000 else in_rate_low
            total_billed_cost += (running_ctx / 1_000_000 * in_rate) + (tokens / 1_000_000 * out_rate)
            total_billed_input_tokens += running_ctx
            total_billed_output_tokens += tokens
            running_ctx += tokens
        else:
            running_ctx += tokens

    models = []
    for r in cur.execute("""
        SELECT COALESCE(s.model_name, c.model_name, 'Gemini Flash') as mname,
               COUNT(*) as steps,
               SUM(s.est_tokens) as tokens
        FROM steps s
        JOIN conversations c ON s.conversation_id = c.id
        GROUP BY mname
        ORDER BY tokens DESC
    """):
        models.append({
            "model_name": r["mname"] or "Unknown",
            "steps": r["steps"],
            "tokens": r["tokens"] or 0
        })

    categories = []
    for r in cur.execute("SELECT category, SUM(est_tokens) as tokens FROM steps WHERE category IS NOT NULL GROUP BY category ORDER BY tokens DESC LIMIT 10"):
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

def query_timeline_data(conn, days=None, tz=None):
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT created_at, total_steps, total_tokens
        FROM conversations
        WHERE created_at IS NOT NULL AND created_at != ''
    """).fetchall()

    daily_map = {}
    target_tz = get_target_tz(tz)

    for r in rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        if not dt:
            continue
        day_str = dt.strftime("%Y-%m-%d")
        if day_str not in daily_map:
            daily_map[day_str] = {"day": day_str, "sessions": 0, "steps": 0, "tokens": 0}
        daily_map[day_str]["sessions"] += 1
        daily_map[day_str]["steps"] += r["total_steps"] or 0
        daily_map[day_str]["tokens"] += r["total_tokens"] or 0

    sorted_days = sorted(daily_map.keys())
    timeline = [daily_map[d] for d in sorted_days]

    if days and len(timeline) > int(days):
        return timeline[-int(days):]
    return timeline

def query_tools_data(conn):
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT 
            tool_name, 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as distinct_sessions
        FROM tool_executions
        GROUP BY tool_name
        ORDER BY total_calls DESC
        LIMIT 50
    """).fetchall()

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

def query_mcp_data(conn):
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

def query_hot_files_data(conn):
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT 
            target_path,
            COUNT(*) as total_touches,
            COUNT(CASE WHEN tool_name = 'view_file' THEN 1 END) as views,
            COUNT(CASE WHEN tool_name IN ('write_to_file', 'replace_file_content', 'multi_replace_file_content') THEN 1 END) as edits,
            COUNT(CASE WHEN tool_name IN ('grep_search', 'find_by_name', 'list_dir') THEN 1 END) as searches,
            COUNT(DISTINCT conversation_id) as sessions
        FROM tool_executions
        WHERE target_path IS NOT NULL AND target_path != ''
        GROUP BY target_path
        ORDER BY total_touches DESC
        LIMIT 150
    """).fetchall()

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

def query_activity_matrix(conn, tz=None):
    cur = conn.cursor()
    target_tz = get_target_tz(tz)
    
    step_rows = cur.execute("""
        SELECT created_at, conversation_id, est_tokens
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
    """).fetchall()

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

def query_conversations_list(conn, limit=200, tz=None):
    cur = conn.cursor()
    rows = cur.execute(f"""
        SELECT id, title, model_name, created_at, total_steps, total_tokens, duration_seconds, active_agent_seconds, active_pairing_seconds
        FROM conversations
        ORDER BY created_at DESC
        LIMIT {limit}
    """).fetchall()

    convs = []
    target_tz = get_target_tz(tz)

    for r in rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        convs.append({
            "id": r["id"],
            "title": r["title"] or f"Conversation {r['id'][:8]}",
            "model_name": r["model_name"] or "Gemini",
            "created_at": dt.strftime("%Y-%m-%d %H:%M") if dt else r["created_at"],
            "total_steps": r["total_steps"] or 0,
            "total_tokens": r["total_tokens"] or 0,
            "duration_seconds": r["duration_seconds"] or 0,
            "active_agent_seconds": r["active_agent_seconds"] or 0,
            "active_pairing_seconds": r["active_pairing_seconds"] or 0
        })
    return convs

def query_session_details(conn, cid, tz=None):
    cur = conn.cursor()
    conv = cur.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if not conv:
        return None

    steps_rows = cur.execute("""
        SELECT step_index, created_at, source, activity, tool_or_source, category, est_tokens, thinking_tokens, phase, model_name, detail
        FROM steps
        WHERE conversation_id = ?
        ORDER BY step_index ASC
    """, (cid,)).fetchall()

    target_tz = get_target_tz(tz)
    steps = []
    for r in steps_rows:
        d = dict(r)
        dt = parse_to_local_dt(d["created_at"], target_tz)
        if dt:
            d["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100' fill='none'%3E%3Crect width='100' height='100' rx='22' fill='%230b0f19'/%3E%3Cpolygon points='50,15 20,82 34,82 50,42' fill='%234f46e5'/%3E%3Cpolygon points='50,15 20,82 14,75 44,12' fill='%230284c7' opacity='0.85'/%3E%3Cpolygon points='50,15 80,82 66,82 50,42' fill='%2306b6d4'/%3E%3Cpolygon points='50,15 80,82 86,75 56,12' fill='%230891b2' opacity='0.85'/%3E%3Cpolygon points='50,22 36,60 64,60' fill='%23a855f7' opacity='0.9'/%3E%3Cpolygon points='50,12 44,18 50,22 56,18' fill='%23ffffff'/%3E%3Cline x1='27' y1='62' x2='73' y2='62' stroke='%2338bdf8' stroke-width='4' stroke-linecap='round'/%3E%3Cline x1='31' y1='62' x2='69' y2='62' stroke='%23ffffff' stroke-width='1.5' stroke-linecap='round'/%3E%3Cpolyline points='20,82 50,15 80,82' stroke='%2338bdf8' stroke-width='1.5' stroke-linejoin='round' fill='none'/%3E%3C/svg%3E">
  <!-- Tailwind CSS CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Chart.js CDN -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <!-- Lucide Icons -->
  <script src="https://unpkg.com/lucide@latest"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#eef2ff',
              100: '#e0e7ff',
              400: '#818cf8',
              500: '#6366f1',
              600: '#4f46e5',
              700: '#4338ca',
            },
            dark: {
              950: '#070a10',
              900: '#0b0f19',
              850: '#0f1422',
              800: '#111827',
              750: '#161f33',
              700: '#1f2937',
              600: '#374151',
            }
          }
        }
      }
    }
  </script>
  <style>
    body { background-color: #0b0f19; color: #f3f4f6; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
    .card { background-color: #111827; border: 1px solid #1f2937; border-radius: 0.75rem; }
    .glass { background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(12px); }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0b0f19; }
    ::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #4b5563; }

    /* Disable all layout and form animations to prevent flashing on rapid live SSE updates */
    *, *::before, *::after {
      animation-duration: 0s !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0s !important;
    }
    button:hover, .commit-square:hover {
      transition-duration: 0.1s !important;
    }
    
    /* Strict GitHub Square Commit Tile */
    .commit-square {
      width: 13px;
      height: 13px;
      min-width: 13px;
      min-height: 13px;
      max-width: 13px;
      max-height: 13px;
      border-radius: 2px;
      outline: 1px solid rgba(255,255,255,0.04);
      outline-offset: -1px;
      transition: transform 0.12s ease, filter 0.12s ease, box-shadow 0.12s ease;
      cursor: pointer;
      flex-shrink: 0;
    }
    .commit-square:hover {
      transform: scale(1.4);
      z-index: 30;
      filter: brightness(1.35);
      box-shadow: 0 0 8px rgba(99, 102, 241, 0.5);
    }
    .commit-square-sm {
      width: 11px;
      height: 11px;
      min-width: 11px;
      min-height: 11px;
      max-width: 11px;
      max-height: 11px;
      border-radius: 2px;
      outline: 1px solid rgba(255,255,255,0.04);
      outline-offset: -1px;
      transition: transform 0.12s ease, filter 0.12s ease, box-shadow 0.12s ease;
      cursor: pointer;
      flex-shrink: 0;
    }
    .commit-square-sm:hover {
      transform: scale(1.4);
      z-index: 30;
      filter: brightness(1.35);
      box-shadow: 0 0 8px rgba(99, 102, 241, 0.5);
    }
  </style>
</head>
<body class="min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white">

  <!-- Top Navigation Bar -->
  <header class="sticky top-0 z-40 glass border-b border-dark-700 px-6 py-3.5 flex items-center justify-between">
    <div class="flex items-center space-x-3">
      <div class="w-10 h-10 rounded-xl bg-dark-900 border border-indigo-500/30 flex items-center justify-center shadow-lg shadow-indigo-500/20 p-1 flex-shrink-0">
        <svg viewBox="0 0 100 100" fill="none" class="w-full h-full drop-shadow-[0_0_8px_rgba(99,102,241,0.65)]">
          <defs>
            <filter id="nav-neon-glow" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3" result="blur"/>
              <feMerge>
                <feMergeNode in="blur"/>
                <feMergeNode in="SourceGraphic"/>
              </feMerge>
            </filter>
            <linearGradient id="nav-orbit" x1="10" y1="30" x2="90" y2="70" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.95"/>
              <stop offset="50%" stop-color="#818cf8" stop-opacity="0.3"/>
              <stop offset="100%" stop-color="#c084fc" stop-opacity="0.95"/>
            </linearGradient>
            <linearGradient id="nav-left" x1="20" y1="15" x2="45" y2="85" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#38bdf8"/>
              <stop offset="55%" stop-color="#4f46e5"/>
              <stop offset="100%" stop-color="#1e1b4b"/>
            </linearGradient>
            <linearGradient id="nav-right" x1="50" y1="15" x2="80" y2="85" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#67e8f9"/>
              <stop offset="50%" stop-color="#6366f1"/>
              <stop offset="100%" stop-color="#312e81"/>
            </linearGradient>
            <linearGradient id="nav-center" x1="50" y1="20" x2="50" y2="62" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#e0e7ff" stop-opacity="0.95"/>
              <stop offset="50%" stop-color="#a855f7" stop-opacity="0.8"/>
              <stop offset="100%" stop-color="#4338ca" stop-opacity="0.95"/>
            </linearGradient>
            <linearGradient id="nav-top" x1="44" y1="10" x2="56" y2="22" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#ffffff"/>
              <stop offset="100%" stop-color="#38bdf8"/>
            </linearGradient>
            <linearGradient id="nav-bar" x1="25" y1="62" x2="75" y2="62" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stop-color="#22d3ee"/>
              <stop offset="50%" stop-color="#818cf8"/>
              <stop offset="100%" stop-color="#c084fc"/>
            </linearGradient>
          </defs>
          <circle cx="50" cy="50" r="36" fill="#6366f1" opacity="0.12" filter="url(#nav-neon-glow)"/>
          <path d="M16 64 C12 50, 26 32, 54 28 C76 25, 88 34, 84 46 C80 58, 66 69, 42 73 C26 76, 17 72, 16 64 Z" stroke="url(#nav-orbit)" stroke-width="1.8" stroke-dasharray="3 3" opacity="0.75" fill="none"/>
          <polygon points="50,15 20,82 34,82 50,42" fill="url(#nav-left)"/>
          <polygon points="50,15 20,82 14,75 44,12" fill="#0284c7" opacity="0.85"/>
          <polygon points="50,15 80,82 66,82 50,42" fill="url(#nav-right)"/>
          <polygon points="50,15 80,82 86,75 56,12" fill="#0891b2" opacity="0.85"/>
          <polygon points="50,22 36,60 64,60" fill="url(#nav-center)"/>
          <polygon points="50,12 44,18 50,22 56,18" fill="url(#nav-top)"/>
          <line x1="27" y1="62" x2="73" y2="62" stroke="url(#nav-bar)" stroke-width="3.6" stroke-linecap="round" filter="url(#nav-neon-glow)"/>
          <line x1="31" y1="62" x2="69" y2="62" stroke="#ffffff" stroke-width="1.4" stroke-linecap="round"/>
          <polyline points="20,82 50,15 80,82" stroke="#38bdf8" stroke-width="1.4" stroke-linejoin="round" fill="none" opacity="0.95"/>
          <circle cx="22" cy="36" r="1.6" fill="#38bdf8" filter="url(#nav-neon-glow)"/>
          <circle cx="78" cy="30" r="1.4" fill="#c084fc" filter="url(#nav-neon-glow)"/>
          <circle cx="50" cy="8" r="2.2" fill="#ffffff" filter="url(#nav-neon-glow)"/>
        </svg>
      </div>
      <div>
        <h1 class="text-lg font-bold bg-gradient-to-r from-white via-gray-200 to-gray-400 bg-clip-text text-transparent">Antigravity Analytics Studio</h1>
        <div id="liveSubheader" class="flex items-center space-x-2 text-xs text-gray-400">
          <span class="inline-block w-2 h-2 rounded-full bg-emerald-400"></span>
          <span id="liveStatus">Analytics Active</span>
          <span>•</span>
          <span id="tzBadge" class="text-gray-300 font-mono text-[11px] flex items-center space-x-1">
            <i data-lucide="globe" class="w-3 h-3 text-brand-400 inline"></i>
            <span id="tzLabel">Local Time</span>
          </span>
        </div>
      </div>
    </div>

    <!-- Header Actions (Live Controls) -->
    <div id="headerActions" class="flex items-center space-x-3">
      <button id="refreshBtn" onclick="fetchDashboardData()" title="Manual Refresh" class="px-3 py-1.5 rounded-md bg-dark-700 hover:bg-dark-600 text-xs font-medium text-gray-200 flex items-center space-x-1.5 transition">
        <i data-lucide="refresh-cw" class="w-3.5 h-3.5"></i>
        <span>Refresh</span>
      </button>

      <div id="liveSyncBadge" class="px-3 py-1.5 rounded-md bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 text-xs font-medium flex items-center space-x-1.5 select-none">
        <i data-lucide="radio" class="w-3.5 h-3.5"></i>
        <span id="syncText">Live Sync: SSE Active</span>
      </div>
    </div>
  </header>

  <!-- Main Content Container -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">

    <!-- 1. Hero KPI Metric Cards -->
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
      <div class="card p-4 flex flex-col justify-between relative overflow-hidden">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Context Tokens</span>
          <i data-lucide="sparkles" class="w-4 h-4 text-indigo-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiTokens" class="text-2xl font-bold text-white tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5" id="kpiChars">Disk footprint</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-indigo-500/5 rounded-full blur-xl"></div>
      </div>

      <div class="card p-4 flex flex-col justify-between relative overflow-hidden">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Conversations</span>
          <i data-lucide="messages-square" class="w-4 h-4 text-blue-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiConvs" class="text-2xl font-bold text-white tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5" id="kpiDateRange">Across all repos</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-blue-500/5 rounded-full blur-xl"></div>
      </div>

      <div class="card p-4 flex flex-col justify-between relative overflow-hidden">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Logged Steps</span>
          <i data-lucide="footprints" class="w-4 h-4 text-purple-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiSteps" class="text-2xl font-bold text-white tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5" id="kpiAvgSteps">-- avg/session</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-purple-500/5 rounded-full blur-xl"></div>
      </div>

      <div class="card p-4 flex flex-col justify-between relative overflow-hidden">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Tool Calls</span>
          <i data-lucide="wrench" class="w-4 h-4 text-amber-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiTools" class="text-2xl font-bold text-white tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5">Automated operations</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-amber-500/5 rounded-full blur-xl"></div>
      </div>

      <div id="cardActiveTime" class="card p-4 flex flex-col justify-between relative overflow-hidden cursor-help" title="Pure Agent Execution Time: Time spent actively generating tokens, reading files, executing bash & MCP tools. Active Pairing: Time in continuous iterations with gaps <= 5m. Wall-Clock Span: Total open lifecycle.">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Agent Work Time</span>
          <i data-lucide="clock" class="w-4 h-4 text-emerald-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiDuration" class="text-2xl font-bold text-white tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5" id="kpiPairingSub">-- pairing • -- span</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-emerald-500/5 rounded-full blur-xl"></div>
      </div>

      <div class="card p-4 flex flex-col justify-between relative overflow-hidden cursor-help" title="Calculated as the true multi-turn cumulative context area-under-the-curve (re-sending history on each response) at standard Gemini Pro ($1.25-$2.50/M) and Flash ($0.075-$0.15/M) rates.">
        <div class="flex justify-between items-start text-gray-400">
          <span class="text-xs font-medium uppercase tracking-wider">Est. API Value</span>
          <i data-lucide="calculator" class="w-4 h-4 text-rose-400"></i>
        </div>
        <div class="mt-2">
          <div id="kpiCost" class="text-2xl font-bold text-rose-400 tracking-tight">--</div>
          <div class="text-[11px] text-gray-400 mt-0.5" id="kpiBilledTokens">-- billed in/out</div>
        </div>
        <div class="absolute -right-3 -bottom-3 w-14 h-14 bg-rose-500/5 rounded-full blur-xl"></div>
      </div>
    </div>

    <!-- 1b. Developer Velocity & Autonomy ROI Banner -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="card p-3.5 flex items-center space-x-3.5 border-l-4 border-l-indigo-500">
        <div class="p-2.5 rounded-lg bg-indigo-500/10 text-indigo-400">
          <i data-lucide="zap" class="w-5 h-5"></i>
        </div>
        <div>
          <div class="text-[10px] font-medium text-gray-400 uppercase tracking-wider">Autonomy Leverage</div>
          <div id="kpiAutonomy" class="text-xl font-bold text-white tracking-tight">--</div>
          <div class="text-[10px] text-gray-400" id="kpiAutonomySub">-- ops / prompt</div>
        </div>
      </div>

      <div class="card p-3.5 flex items-center space-x-3.5 border-l-4 border-l-purple-500">
        <div class="p-2.5 rounded-lg bg-purple-500/10 text-purple-400">
          <i data-lucide="brain-circuit" class="w-5 h-5"></i>
        </div>
        <div>
          <div class="text-[10px] font-medium text-gray-400 uppercase tracking-wider">Deep Reasoning</div>
          <div id="kpiThinking" class="text-xl font-bold text-white tracking-tight">--</div>
          <div class="text-[10px] text-gray-400">Thinking tokens</div>
        </div>
      </div>

      <div class="card p-3.5 flex items-center space-x-3.5 border-l-4 border-l-emerald-500">
        <div class="p-2.5 rounded-lg bg-emerald-500/10 text-emerald-400">
          <i data-lucide="code-2" class="w-5 h-5"></i>
        </div>
        <div>
          <div class="text-[10px] font-medium text-gray-400 uppercase tracking-wider">Code Production</div>
          <div id="kpiCodeLines" class="text-xl font-bold text-white tracking-tight">--</div>
          <div class="text-[10px] text-gray-400" id="kpiCodeSub">-- lines authored</div>
        </div>
      </div>

      <div class="card p-3.5 flex items-center space-x-3.5 border-l-4 border-l-amber-500">
        <div class="p-2.5 rounded-lg bg-amber-500/10 text-amber-400">
          <i data-lucide="hourglass" class="w-5 h-5"></i>
        </div>
        <div>
          <div class="text-[10px] font-medium text-gray-400 uppercase tracking-wider">Dev Time Saved</div>
          <div id="kpiTimeSaved" class="text-xl font-bold text-white tracking-tight">--</div>
          <div class="text-[10px] text-gray-400">Engineering hours</div>
        </div>
      </div>
    </div>

    <!-- 2. Main Visual Charts (Token Velocity & Model Shares) -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div class="card p-5 lg:col-span-2 flex flex-col">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
              <i data-lucide="trending-up" class="w-4 h-4 text-indigo-400"></i>
              <span>Token Consumption Velocity</span>
            </h2>
            <p class="text-xs text-gray-400">Daily tokens consumed and session counts over time</p>
          </div>
          <div class="flex items-center space-x-1 bg-dark-800 border border-dark-700 rounded-md p-0.5 text-xs">
            <button onclick="setTimelineRange(7)" id="btn7d" class="px-2.5 py-1 rounded text-gray-400 hover:text-white transition">7D</button>
            <button onclick="setTimelineRange(14)" id="btn14d" class="px-2.5 py-1 rounded text-gray-400 hover:text-white transition">14D</button>
            <button onclick="setTimelineRange(30)" id="btn30d" class="px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition">30D</button>
            <button onclick="setTimelineRange(null)" id="btnAll" class="px-2.5 py-1 rounded text-gray-400 hover:text-white transition">All</button>
          </div>
        </div>
        <div class="flex-1 relative min-h-[260px]">
          <canvas id="timelineChart"></canvas>
        </div>
      </div>

      <div class="card p-5 flex flex-col">
        <div class="mb-4">
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="cpu" class="w-4 h-4 text-purple-400"></i>
            <span>Model Utilization & Share</span>
          </h2>
          <p class="text-xs text-gray-400">Token distribution across Gemini Pro, Flash, & external models</p>
        </div>
        <div class="flex-1 relative min-h-[220px] flex items-center justify-center">
          <canvas id="modelsChart"></canvas>
        </div>
      </div>
    </div>

    <!-- 3. Tool Execution & Context Distribution -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="card p-5 flex flex-col">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
              <i data-lucide="terminal" class="w-4 h-4 text-amber-400"></i>
              <span>Top Tool Executions</span>
            </h2>
            <p class="text-xs text-gray-400">Most frequently invoked built-in and MCP tools</p>
          </div>
          <div class="flex items-center space-x-1 bg-dark-800 border border-dark-700 rounded-md p-0.5 text-xs">
            <button onclick="setToolFilter('all')" id="toolBtnAll" class="px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition">All</button>
            <button onclick="setToolFilter('native')" id="toolBtnNative" class="px-2.5 py-1 rounded text-gray-400 hover:text-white transition">Native</button>
            <button onclick="setToolFilter('mcp')" id="toolBtnMcp" class="px-2.5 py-1 rounded text-gray-400 hover:text-white transition">MCP</button>
          </div>
        </div>
        <div class="flex-1 relative min-h-[260px]">
          <canvas id="toolsChart"></canvas>
        </div>
      </div>

      <div class="card p-5 flex flex-col">
        <div class="mb-4">
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="pie-chart" class="w-4 h-4 text-emerald-400"></i>
            <span>Context Consumption Breakdown</span>
          </h2>
          <p class="text-xs text-gray-400">Where tokens are spent (File views, Agent thinking, Shell, Prompts)</p>
        </div>
        <div class="flex-1 relative min-h-[260px] flex items-center justify-center">
          <canvas id="categoryChart"></canvas>
        </div>
      </div>
    </div>

    <!-- 3b. Context Health & Phase Efficiency Diagnostics -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Phase Breakdown Chart -->
      <div class="card p-5 flex flex-col">
        <div class="mb-3">
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="layers" class="w-4 h-4 text-brand-400"></i>
            <span>Context by Phase</span>
          </h2>
          <p class="text-xs text-gray-400">Token distribution across development stages</p>
        </div>
        <div class="relative flex-1 min-h-[200px] flex items-center justify-center">
          <canvas id="phaseChart"></canvas>
        </div>
        <div id="phaseLegend" class="grid grid-cols-2 gap-2 mt-3 pt-3 border-t border-dark-700 text-xs">
        </div>
      </div>

      <!-- Context Hygiene & Re-Read Waste Diagnostics -->
      <div class="card p-5 flex flex-col justify-between">
        <div>
          <div class="mb-3">
            <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
              <i data-lucide="shield-alert" class="w-4 h-4 text-amber-400"></i>
              <span>Context Hygiene Diagnostics</span>
            </h2>
            <p class="text-xs text-gray-400">Redundant reads and error recovery efficiency</p>
          </div>

          <div class="space-y-3.5 mt-3">
            <div class="bg-dark-800/80 p-3 rounded-lg border border-dark-700">
              <div class="flex justify-between items-center text-xs mb-1.5">
                <span class="text-gray-300 font-medium">Redundant File Re-Reads</span>
                <span id="diagReReadRate" class="font-mono font-bold text-amber-400">--%</span>
              </div>
              <div class="w-full bg-dark-900 rounded-full h-2 overflow-hidden">
                <div id="diagReReadBar" class="bg-amber-500 h-2 rounded-full" style="width: 0%"></div>
              </div>
              <div class="text-[11px] text-gray-400 mt-1.5 flex justify-between font-mono">
                <span id="diagReReadCount">-- repeated views</span>
                <span id="diagReReadWaste" class="text-amber-300/90">~-- tokens waste</span>
              </div>
            </div>

            <div class="bg-dark-800/80 p-3 rounded-lg border border-dark-700">
              <div class="flex justify-between items-center text-xs mb-1.5">
                <span class="text-gray-300 font-medium">Tool Execution Resilience</span>
                <span id="diagResilienceRate" class="font-mono font-bold text-emerald-400">--%</span>
              </div>
              <div class="w-full bg-dark-900 rounded-full h-2 overflow-hidden">
                <div id="diagResilienceBar" class="bg-emerald-500 h-2 rounded-full" style="width: 0%"></div>
              </div>
              <div class="text-[11px] text-gray-400 mt-1.5 flex justify-between font-mono">
                <span>Autonomous self-correction</span>
                <span id="diagErrorCount" class="text-emerald-300/90">-- errors resolved</span>
              </div>
            </div>
          </div>
        </div>

        <div class="mt-3 p-2.5 bg-dark-900/60 rounded-lg border border-dark-700/60 text-[11px] text-gray-400 flex items-start space-x-2">
          <i data-lucide="lightbulb" class="w-3.5 h-3.5 text-indigo-400 shrink-0 mt-0.5"></i>
          <span><strong>Optimization:</strong> High re-read rates indicate context saturation. Subagent delegation isolates sweeps and eliminates re-read waste.</span>
        </div>
      </div>

      <!-- Top Bloat Injections Table -->
      <div class="card p-5 flex flex-col">
        <div class="mb-3">
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="flame" class="w-4 h-4 text-rose-400"></i>
            <span>Context Bloat Culprits</span>
          </h2>
          <p class="text-xs text-gray-400">Largest single-step token injections</p>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs text-gray-300">
            <thead class="text-[10px] text-gray-400 uppercase bg-dark-800 border-b border-dark-700 sticky top-0">
              <tr>
                <th class="py-1.5 px-2">Step</th>
                <th class="py-1.5 px-2">Phase</th>
                <th class="py-1.5 px-2 text-right">Tokens</th>
                <th class="py-1.5 px-2">Detail</th>
              </tr>
            </thead>
            <tbody id="bloatTableBody" class="divide-y divide-dark-700">
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 3c. Model Context Protocol (MCP) Ecosystem Analytics -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- MCP Server Invocations Chart -->
      <div class="card p-5 flex flex-col">
        <div class="mb-3">
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="plug" class="w-4 h-4 text-cyan-400"></i>
            <span>MCP Server Ecosystem</span>
          </h2>
          <p class="text-xs text-gray-400">External Model Context Protocol server share</p>
        </div>
        <div class="relative flex-1 min-h-[200px] flex items-center justify-center">
          <canvas id="mcpServerChart"></canvas>
        </div>
        <div id="mcpServerLegend" class="grid grid-cols-2 gap-2 mt-3 pt-3 border-t border-dark-700 text-xs">
        </div>
      </div>

      <!-- Top MCP Tools Breakdown Table -->
      <div class="card p-5 lg:col-span-2 flex flex-col">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
          <div>
            <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
              <i data-lucide="boxes" class="w-4 h-4 text-cyan-400"></i>
              <span>MCP Tools Breakdown</span>
            </h2>
            <p class="text-xs text-gray-400">Operations executed through connected MCP servers</p>
          </div>
          <div class="flex items-center space-x-2">
            <label class="text-xs text-gray-400">Filter Server:</label>
            <select id="mcpServerSelect" onchange="filterMcpToolsTable()" class="bg-dark-800 border border-dark-700 rounded-md px-2.5 py-1 text-xs text-gray-200 focus:outline-none focus:border-brand-500">
              <option value="all">All Servers</option>
            </select>
          </div>
        </div>
        <div class="overflow-x-auto max-h-[260px] overflow-y-auto">
          <table class="w-full text-left text-xs text-gray-300">
            <thead class="text-[10px] text-gray-400 uppercase bg-dark-800 border-b border-dark-700 sticky top-0">
              <tr>
                <th class="py-1.5 px-3">Tool Name</th>
                <th class="py-1.5 px-3">MCP Server</th>
                <th class="py-1.5 px-3 text-right">Invocations</th>
                <th class="py-1.5 px-3 text-right">Sessions</th>
                <th class="py-1.5 px-3 text-right">Error Rate</th>
              </tr>
            </thead>
            <tbody id="mcpToolsTableBody" class="divide-y divide-dark-700">
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 4. Agent Activity Heatmap (Hourly and Calendar Commit Grids) -->
    <div class="card p-6 bg-dark-800 border-dark-700">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-4">
        <div>
          <div class="flex items-center space-x-3">
            <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
              <i data-lucide="git-commit" class="w-4 h-4 text-brand-400"></i>
              <span>Agent Activity Matrix</span>
            </h2>
            <span id="heatmapOpsCount" class="text-xs px-2.5 py-0.5 rounded-full bg-brand-500/15 border border-brand-500/30 text-brand-300 font-medium">
              -- operations
            </span>
          </div>
          <div class="flex items-center space-x-4 text-xs text-gray-400 mt-1">
            <span>Current Streak: <strong id="streakCurrent" class="text-white">--</strong></span>
            <span>•</span>
            <span>Longest Streak: <strong id="streakLongest" class="text-white">--</strong></span>
            <span>•</span>
            <span>Active Days: <strong id="activeDaysCount" class="text-white">--</strong></span>
          </div>
        </div>

        <!-- Zoom Tabs -->
        <div class="flex items-center space-x-1 bg-dark-900 border border-dark-700 rounded-lg p-1 text-xs">
          <button onclick="setHeatmapMode('year')" id="hmBtnYear" class="px-3 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap">
            1 Year (52W)
          </button>
          <button onclick="setHeatmapMode('quarter')" id="hmBtnQuarter" class="px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
            3 Months
          </button>
          <button onclick="setHeatmapMode('month')" id="hmBtnMonth" class="px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
            Past 30 Days (Hourly)
          </button>
          <button onclick="setHeatmapMode('past7hourly')" id="hmBtnPast7" class="px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
            Past 7 Days (Hourly)
          </button>
          <button onclick="setHeatmapMode('hourly')" id="hmBtnHourly" class="px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
            24x7 Aggregate
          </button>
        </div>
      </div>

      <!-- Viewports (ALL strictly formatted as GitHub commit square matrices) -->
      <div class="mt-4 overflow-x-auto pb-2 flex justify-start sm:justify-center">
        
        <!-- View 1: 52-Week Year Matrix (53 cols x 7 rows of 11px squares) -->
        <div id="ghYearView" class="flex flex-col w-fit">
          <div id="ghMonthLabels" class="flex text-[10px] text-gray-400 h-4 pl-7 mb-1 select-none"></div>
          <div class="flex">
            <div class="w-7 flex flex-col justify-between text-[9px] text-gray-400 select-none pr-2 py-0.5 h-[98px]">
              <span></span>
              <span>Mon</span>
              <span></span>
              <span>Wed</span>
              <span></span>
              <span>Fri</span>
              <span></span>
            </div>
            <div id="ghGrid" class="flex space-x-[3px]"></div>
          </div>
        </div>

        <!-- View 2: 3 Months (14 Weeks) Matrix (14 cols x 7 rows of 13px squares) -->
        <div id="ghQuarterView" class="hidden flex-col w-fit">
          <div id="ghQuarterMonthLabels" class="flex text-xs text-gray-300 h-5 pl-8 mb-2 font-medium"></div>
          <div class="flex">
            <div class="w-8 flex flex-col justify-between text-[10px] text-gray-400 select-none pr-2 py-1 h-[112px]">
              <span>Sun</span>
              <span>Mon</span>
              <span>Tue</span>
              <span>Wed</span>
              <span>Thu</span>
              <span>Fri</span>
              <span>Sat</span>
            </div>
            <div id="ghQuarterGrid" class="flex space-x-1"></div>
          </div>
        </div>

        <!-- View 3: Past 30 Days (Hourly Matrix: 24 cols x 30 rows of 13px squares) -->
        <div id="ghMonthView" class="hidden flex-col w-fit">
          <div class="flex items-center mb-1 pr-2">
            <div class="w-16"></div>
            <div id="monthHourHeader" class="flex space-x-[3px] text-[9px] text-gray-400 font-mono text-center"></div>
          </div>
          <div class="max-h-[360px] overflow-y-auto pr-2">
            <div id="ghMonthGrid" class="flex flex-col space-y-[3px]"></div>
          </div>
        </div>

        <!-- View 4: Past 7 Days Hourly Matrix (24 cols x 7 rows of 13px squares) -->
        <div id="ghPast7View" class="hidden flex-col w-fit">
          <div class="flex items-center mb-1">
            <div class="w-16"></div>
            <div id="past7HourHeader" class="flex space-x-[3px] text-[9px] text-gray-400 font-mono text-center"></div>
          </div>
          <div id="ghPast7Grid" class="flex flex-col space-y-[3px]"></div>
        </div>

        <!-- View 5: 24x7 Aggregate Matrix (24 cols x 7 rows of 13px squares) -->
        <div id="ghHourlyView" class="hidden flex-col w-fit">
          <div class="flex items-center mb-1">
            <div class="w-10"></div>
            <div id="hourlyHourHeader" class="flex space-x-[3px] text-[9px] text-gray-400 font-mono text-center"></div>
          </div>
          <div id="ghHourlyGrid" class="flex flex-col space-y-[3px]"></div>
        </div>

      </div>

      <!-- Footer Tooltip & Color Legend -->
      <div class="flex items-center justify-between pt-3 border-t border-dark-700 text-xs text-gray-400 mt-3">
        <div id="ghTooltipText" class="font-medium text-gray-300">Hover over any commit square to see operations</div>
        <div class="flex items-center space-x-1.5 select-none">
          <span class="text-[11px] text-gray-400">Less</span>
          <div class="w-3 h-3 rounded-[2px] bg-[#151d2f] border border-white/5"></div>
          <div class="w-3 h-3 rounded-[2px] bg-[#312e81]"></div>
          <div class="w-3 h-3 rounded-[2px] bg-[#4338ca]"></div>
          <div class="w-3 h-3 rounded-[2px] bg-[#6366f1]"></div>
          <div class="w-3 h-3 rounded-[2px] bg-[#a5b4fc]"></div>
          <span class="text-[11px] text-gray-400">More</span>
        </div>
      </div>
    </div>

    <!-- 5. Codebase Hotspots Table -->
    <div class="card p-5">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="flame" class="w-4 h-4 text-rose-500"></i>
            <span>Codebase Hotspots</span>
          </h2>
          <p class="text-xs text-gray-400">Most touched files and searched repository directories</p>
        </div>

        <div class="flex items-center space-x-3 w-full sm:w-auto">
          <div class="flex items-center space-x-1 bg-dark-900 border border-dark-700 rounded-lg p-1 text-xs">
            <button onclick="setHotFilesTypeFilter('files')" id="hfBtnFiles" class="px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap">
              📄 Files Only
            </button>
            <button onclick="setHotFilesTypeFilter('dirs')" id="hfBtnDirs" class="px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
              📁 Directories
            </button>
            <button onclick="setHotFilesTypeFilter('all')" id="hfBtnAll" class="px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap">
              All Paths
            </button>
          </div>

          <div class="relative max-w-xs w-full">
            <i data-lucide="search" class="w-4 h-4 text-gray-400 absolute left-3 top-2.5"></i>
            <input type="text" id="hotFilesSearch" oninput="filterHotFiles()" placeholder="Filter by name / path..." class="w-full pl-9 pr-3 py-1.5 bg-dark-800 border border-dark-700 rounded-md text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-brand-500 transition">
          </div>
        </div>
      </div>

      <div class="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table class="w-full text-left text-xs text-gray-300">
          <thead class="text-[11px] text-gray-400 uppercase bg-dark-800/80 sticky top-0 border-b border-dark-700">
            <tr>
              <th class="py-2.5 px-3">Path / Resource</th>
              <th class="py-2.5 px-3">Type</th>
              <th class="py-2.5 px-3 text-right">Total Touches</th>
              <th class="py-2.5 px-3 text-right" id="hfColViews">Views</th>
              <th class="py-2.5 px-3 text-right" id="hfColEdits">Edits</th>
              <th class="py-2.5 px-3 text-right">Distinct Sessions</th>
            </tr>
          </thead>
          <tbody id="hotFilesTableBody" class="divide-y divide-dark-700">
          </tbody>
        </table>
      </div>
    </div>

    <!-- 6. Searchable Conversation Explorer (SECTION_CONVERSATION_EXPLORER) -->
    <div id="convExplorerSection" class="card p-5">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
        <div>
          <h2 class="text-sm font-semibold text-white flex items-center space-x-2">
            <i data-lucide="folder-search" class="w-4 h-4 text-indigo-400"></i>
            <span>Conversation Explorer</span>
          </h2>
          <p class="text-xs text-gray-400">Browse and inspect step-by-step waterfalls for any session</p>
        </div>
        <div class="flex items-center space-x-2 max-w-sm w-full">
          <div class="relative flex-1">
            <i data-lucide="search" class="w-4 h-4 text-gray-400 absolute left-3 top-2.5"></i>
            <input type="text" id="convSearch" oninput="filterConversations()" placeholder="Search conversation title / ID..." class="w-full pl-9 pr-3 py-1.5 bg-dark-800 border border-dark-700 rounded-md text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-brand-500 transition">
          </div>
        </div>
      </div>

      <div class="overflow-x-auto max-h-[420px] overflow-y-auto">
        <table class="w-full text-left text-xs text-gray-300">
          <thead class="text-[11px] text-gray-400 uppercase bg-dark-800/80 sticky top-0 border-b border-dark-700">
            <tr>
              <th class="py-2.5 px-3">Date (Local)</th>
              <th class="py-2.5 px-3">Title / Initial Prompt</th>
              <th class="py-2.5 px-3">Model</th>
              <th class="py-2.5 px-3 text-right">Steps</th>
              <th class="py-2.5 px-3 text-right">Tokens</th>
              <th class="py-2.5 px-3 text-right">Active Time (Span)</th>
              <th class="py-2.5 px-3 text-center">Action</th>
            </tr>
          </thead>
          <tbody id="convTableBody" class="divide-y divide-dark-700">
          </tbody>
        </table>
      </div>
    </div>

  </main>

  <!-- Session Deep-Dive Modal (MODAL_CONVERSATION_DEEPDIVE) -->
  <div id="sessionModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm hidden items-center justify-center p-4">
    <div class="card max-w-4xl w-full max-h-[90vh] flex flex-col overflow-hidden shadow-2xl border border-dark-600">
      <div class="p-5 border-b border-dark-700 flex items-center justify-between bg-dark-800/50">
        <div>
          <h3 id="modalTitle" class="text-base font-bold text-white tracking-tight">Conversation Deep-Dive</h3>
          <p id="modalSub" class="text-xs text-gray-400 mt-0.5">Session inspection</p>
        </div>
        <button onclick="closeModal()" class="p-1 rounded-md text-gray-400 hover:text-white hover:bg-dark-700 transition">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <div class="flex-1 overflow-y-auto p-6 space-y-6">
        <div class="grid grid-cols-4 gap-3 text-center">
          <div class="bg-dark-800 p-3 rounded-lg border border-dark-700">
            <div class="text-[11px] text-gray-400">Total Tokens</div>
            <div id="modalTokens" class="text-lg font-bold text-indigo-400 mt-0.5">--</div>
          </div>
          <div class="bg-dark-800 p-3 rounded-lg border border-dark-700">
            <div class="text-[11px] text-gray-400">Total Steps</div>
            <div id="modalSteps" class="text-lg font-bold text-white mt-0.5">--</div>
          </div>
          <div class="bg-dark-800 p-3 rounded-lg border border-dark-700">
            <div class="text-[11px] text-gray-400">Model</div>
            <div id="modalModel" class="text-xs font-semibold text-purple-400 mt-1 truncate">--</div>
          </div>
          <div class="bg-dark-800 p-3 rounded-lg border border-dark-700 cursor-help" title="Active working time vs total wall-clock duration of this session">
            <div class="text-[11px] text-gray-400">Agent Work Time</div>
            <div id="modalDuration" class="text-lg font-bold text-emerald-400 mt-0.5">--</div>
            <div id="modalWallSpan" class="text-[10px] text-gray-400 mt-0.5">-- span</div>
          </div>
        </div>

        <!-- Session Efficiency Scorecard -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 bg-dark-800/50 p-3 rounded-lg border border-dark-700 text-xs">
          <div class="p-2 rounded bg-dark-900/60 border border-dark-700/50">
            <span class="text-gray-400 block text-[10px] uppercase font-medium">Autonomy Leverage</span>
            <span id="modalAutonomy" class="font-semibold text-white font-mono mt-0.5 block">--</span>
          </div>
          <div class="p-2 rounded bg-dark-900/60 border border-dark-700/50">
            <span class="text-gray-400 block text-[10px] uppercase font-medium">Deep Reasoning</span>
            <span id="modalThinking" class="font-semibold text-purple-400 font-mono mt-0.5 block">--</span>
          </div>
          <div class="p-2 rounded bg-dark-900/60 border border-dark-700/50">
            <span class="text-gray-400 block text-[10px] uppercase font-medium">Re-Read Health</span>
            <span id="modalReReads" class="font-semibold text-amber-400 font-mono mt-0.5 block">--</span>
          </div>
          <div class="p-2 rounded bg-dark-900/60 border border-dark-700/50">
            <span class="text-gray-400 block text-[10px] uppercase font-medium">Code Authored</span>
            <span id="modalCodeLines" class="font-semibold text-emerald-400 font-mono mt-0.5 block">--</span>
          </div>
        </div>

        <div>
          <h4 class="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-2 flex items-center space-x-1.5">
            <i data-lucide="layers" class="w-3.5 h-3.5 text-brand-500"></i>
            <span>Top Context Consumers in this Session</span>
          </h4>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs text-gray-300">
              <thead class="text-[10px] text-gray-400 uppercase bg-dark-800 border-b border-dark-700">
                <tr>
                  <th class="py-2 px-2.5">Step #</th>
                  <th class="py-2 px-2.5">Activity</th>
                  <th class="py-2 px-2.5">Tool / Source</th>
                  <th class="py-2 px-2.5 text-right">Est. Tokens</th>
                  <th class="py-2 px-2.5">Details</th>
                </tr>
              </thead>
              <tbody id="modalTopConsumers" class="divide-y divide-dark-700">
              </tbody>
            </table>
          </div>
        </div>

        <div>
          <h4 class="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-2 flex items-center space-x-1.5">
            <i data-lucide="list-ordered" class="w-3.5 h-3.5 text-brand-500"></i>
            <span>Step-by-Step Operations Log</span>
          </h4>
          <div class="overflow-x-auto max-h-[300px] overflow-y-auto">
            <table class="w-full text-left text-xs text-gray-300">
              <thead class="text-[10px] text-gray-400 uppercase bg-dark-800 border-b border-dark-700 sticky top-0">
                <tr>
                  <th class="py-2 px-2.5">#</th>
                  <th class="py-2 px-2.5">Type</th>
                  <th class="py-2 px-2.5">Tool / Source</th>
                  <th class="py-2 px-2.5 text-right">Tokens</th>
                  <th class="py-2 px-2.5">Details</th>
                </tr>
              </thead>
              <tbody id="modalStepList" class="divide-y divide-dark-700">
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>
  </div>

  <script>
    // Completely disable animations globally to prevent spamming animations on live SSE updates
    if (typeof Chart !== 'undefined') {
      Chart.defaults.animation = false;
      Chart.defaults.animations = false;
      Chart.defaults.transitions = {
        active: { animation: { duration: 0 } },
        resize: { animation: { duration: 0 } }
      };
    }

    let timelineChart = null;
    let modelsChart = null;
    let toolsChart = null;
    let categoryChart = null;
    let phaseChart = null;
    let mcpServerChart = null;

    let allHotFiles = [];
    let allConversations = [];
    let allTimelineData = [];
    let allToolsData = [];
    let allMcpData = [];
    let currentTimelineDays = 30;
    let currentToolFilter = 'all';
    let currentHotFilesFilter = 'files';
    let cachedActivityMatrix = null;
    let currentHeatmapMode = 'year';
    let localTimezoneAbbr = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';

    const COLOR_PALETTE = {
      lvl0: '#151d2f',
      lvl1: '#312e81',
      lvl2: '#4338ca',
      lvl3: '#6366f1',
      lvl4: '#a5b4fc'
    };

    function formatTokens(n) {
      if (!n) return "0";
      if (n >= 1000000000) return (n / 1000000000).toFixed(2) + "B";
      if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
      if (n >= 1000) return (n / 1000).toFixed(1) + "k";
      return n.toLocaleString();
    }

    function formatDuration(sec) {
      if (!sec) return "0m";
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (h > 0) return `${h}h ${m}m`;
      return `${m}m`;
    }

    async function fetchDashboardData() {
      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        const tzParam = clientTz ? `?tz=${encodeURIComponent(clientTz)}` : '';

        const [summaryRes, timelineRes, toolsRes, mcpRes, hotFilesRes, matrixRes, convsRes] = await Promise.all([
          fetch(`/api/summary${tzParam}`).then(r => r.json()),
          fetch(`/api/timeline${tzParam}`).then(r => r.json()),
          fetch('/api/tools').then(r => r.json()),
          fetch('/api/mcp').then(r => r.json()),
          fetch('/api/hot-files').then(r => r.json()),
          fetch(`/api/activity-matrix${tzParam}`).then(r => r.json()),
          fetch(`/api/conversations${tzParam}`).then(r => r.json())
        ]);

        allTimelineData = timelineRes || [];
        allToolsData = toolsRes || [];
        allMcpData = mcpRes || [];
        cachedActivityMatrix = matrixRes;

        if (summaryRes.timezone_label) {
          document.getElementById('tzLabel').textContent = summaryRes.timezone_label;
        } else if (matrixRes.timezone_abbr) {
          document.getElementById('tzLabel').textContent = matrixRes.timezone_abbr;
        }
        if (matrixRes.timezone_abbr) {
          localTimezoneAbbr = matrixRes.timezone_abbr;
        }

        renderKPIs(summaryRes);
        setTimelineRange(currentTimelineDays);
        renderModelsChart(summaryRes.models);
        renderToolsChart(allToolsData);
        renderMcpSection(allMcpData);
        renderCategoryChart(summaryRes.categories);
        renderPhaseChart(summaryRes.phases);
        renderHeatmapAllViews(matrixRes);
        renderHotFiles(hotFilesRes);
        if (convsRes && convsRes.length > 0) {
          renderConversations(convsRes);
        }
        
        lucide.createIcons();
      } catch (err) {
        console.error("Failed to load dashboard data", err);
      }
    }

    function renderKPIs(data) {
      document.getElementById('kpiTokens').textContent = formatTokens(data.total_tokens);
      document.getElementById('kpiChars').textContent = (data.total_chars || 0).toLocaleString() + ' chars';
      document.getElementById('kpiConvs').textContent = (data.total_convs || 0).toLocaleString();
      document.getElementById('kpiSteps').textContent = (data.total_steps || 0).toLocaleString();
      document.getElementById('kpiAvgSteps').textContent = data.avg_steps + ' avg/session';
      document.getElementById('kpiTools').textContent = (data.total_tools || 0).toLocaleString();
      
      const agentWork = data.total_agent_work || 0;
      const pairingWork = data.total_pairing_work || 0;
      const wallSpan = data.total_duration || 0;
      
      document.getElementById('kpiDuration').textContent = formatDuration(agentWork);
      document.getElementById('kpiPairingSub').textContent = `~${formatDuration(pairingWork)} pairing • ${formatDuration(wallSpan)} span`;
      
      const cardEl = document.getElementById('cardActiveTime');
      if (cardEl) {
        cardEl.title = `Pure Agent Execution Time: ${formatDuration(agentWork)} (active LLM reasoning, code generation, tool runs).\\nActive Pairing Time: ${formatDuration(pairingWork)} (continuous interactions <= 5m gaps).\\nTotal Wall-Clock Span: ${formatDuration(wallSpan)} (across all open sessions).`;
      }
      
      document.getElementById('kpiCost').textContent = '$' + (data.total_billed_cost || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
      document.getElementById('kpiBilledTokens').textContent = `~${formatTokens(data.total_billed_input_tokens)} in / ${formatTokens(data.total_billed_output_tokens)} out`;

      // 1b. Developer Velocity Strip
      const elAutonomy = document.getElementById('kpiAutonomy');
      if (elAutonomy) elAutonomy.textContent = `${data.autonomy_multiplier || 1}x`;
      const elAutonomySub = document.getElementById('kpiAutonomySub');
      if (elAutonomySub) elAutonomySub.textContent = `${(data.total_tools || 0).toLocaleString()} ops / ${(data.total_prompts || 0).toLocaleString()} prompts`;

      const elThinking = document.getElementById('kpiThinking');
      if (elThinking) elThinking.textContent = formatTokens(data.total_thinking || 0);

      const elCodeLines = document.getElementById('kpiCodeLines');
      if (elCodeLines) elCodeLines.textContent = (data.total_code_lines || 0).toLocaleString();
      const elCodeSub = document.getElementById('kpiCodeSub');
      if (elCodeSub) elCodeSub.textContent = `+${(data.lines_written || 0).toLocaleString()} / ~${(data.lines_edited || 0).toLocaleString()}`;

      const elTimeSaved = document.getElementById('kpiTimeSaved');
      if (elTimeSaved) elTimeSaved.textContent = `~${(data.saved_hours || 0).toLocaleString()}h`;

      // Context Hygiene Diagnostics
      const elReReadRate = document.getElementById('diagReReadRate');
      if (elReReadRate) elReReadRate.textContent = `${data.re_read_rate || 0}%`;
      const elReReadBar = document.getElementById('diagReReadBar');
      if (elReReadBar) elReReadBar.style.width = `${Math.min(data.re_read_rate || 0, 100)}%`;
      const elReReadCount = document.getElementById('diagReReadCount');
      if (elReReadCount) elReReadCount.textContent = `${(data.total_re_reads || 0).toLocaleString()} repeated views`;
      const elReReadWaste = document.getElementById('diagReReadWaste');
      if (elReReadWaste) elReReadWaste.textContent = `~${formatTokens(data.re_read_waste_tokens || 0)} waste`;

      const elResilienceRate = document.getElementById('diagResilienceRate');
      if (elResilienceRate) elResilienceRate.textContent = `${data.resilience_rate || 100}%`;
      const elResilienceBar = document.getElementById('diagResilienceBar');
      if (elResilienceBar) elResilienceBar.style.width = `${Math.min(data.resilience_rate || 100, 100)}%`;
      const elErrorCount = document.getElementById('diagErrorCount');
      if (elErrorCount) elErrorCount.textContent = `${(data.failed_tool_calls || 0).toLocaleString()} errors resolved`;

      // Bloat Table
      const bloatTbody = document.getElementById('bloatTableBody');
      if (bloatTbody && data.bloat_steps) {
        bloatTbody.innerHTML = '';
        const phaseBadges = {
          'discovery': 'bg-amber-500/10 text-amber-400 border-amber-500/20',
          'reasoning': 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
          'execution': 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
          'verification': 'bg-purple-500/10 text-purple-400 border-purple-500/20'
        };
        for (const b of data.bloat_steps) {
          const tr = document.createElement('tr');
          tr.className = 'hover:bg-dark-800/60 transition';
          const p = (b.phase || 'other').toLowerCase();
          const badgeCls = phaseBadges[p] || 'bg-gray-500/10 text-gray-400 border-gray-500/20';
          tr.innerHTML = `
            <td class="py-1.5 px-2 font-mono text-[10px] text-gray-400">#${b.step_index}</td>
            <td class="py-1.5 px-2"><span class="px-1.5 py-0.5 rounded text-[9px] uppercase font-semibold border ${badgeCls}">${b.phase}</span></td>
            <td class="py-1.5 px-2 text-right font-mono font-bold text-rose-400 text-[11px]">${formatTokens(b.est_tokens)}</td>
            <td class="py-1.5 px-2 text-[10px] text-gray-300 max-w-[120px] truncate" title="${b.detail || ''}">${b.detail || '--'}</td>
          `;
          bloatTbody.appendChild(tr);
        }
      }
    }

    function getCommitColor(steps) {
      if (!steps || steps <= 0) return COLOR_PALETTE.lvl0;
      if (steps <= 50) return COLOR_PALETTE.lvl1;
      if (steps <= 250) return COLOR_PALETTE.lvl2;
      if (steps <= 1000) return COLOR_PALETTE.lvl3;
      return COLOR_PALETTE.lvl4;
    }

    function renderHeatmapAllViews(data) {
      if (!data) return;
      const daily = data.daily || {};
      const streak = data.streak || {};

      document.getElementById('streakCurrent').textContent = `${streak.current} days`;
      document.getElementById('streakLongest').textContent = `${streak.longest} days`;
      document.getElementById('activeDaysCount').textContent = `${streak.total_active_days} days`;

      let totalOps = 0;
      for (const k in daily) totalOps += daily[k].steps || 0;
      document.getElementById('heatmapOpsCount').textContent = `${totalOps.toLocaleString()} operations in 2026`;

      // View 1: 52-Week Year Matrix
      renderYearGrid(daily);
      // View 2: 3 Months (14 Weeks) Matrix
      renderQuarterGrid(daily);
      // View 3: Past 30 Days (Hourly Matrix: 24 cols x 30 rows)
      renderMonthHourlyGrid(data.past_30_days_hourly || {});
      // View 4: Past 7 Days (Hourly 24x7 Commit Squares)
      renderPast7HourlyGrid(data.past_7_days_hourly || {});
      // View 5: 24x7 Aggregate (Hourly 24x7 Commit Squares)
      renderHourlyAggregateGrid(data.hourly_aggregate || []);
    }

    // --- Mode 1: 52-Week Grid ---
    function renderYearGrid(daily) {
      const gridContainer = document.getElementById('ghGrid');
      const monthLabelsContainer = document.getElementById('ghMonthLabels');
      gridContainer.innerHTML = '';
      monthLabelsContainer.innerHTML = '';

      const today = new Date();
      const dayOfWeek = today.getDay();
      const totalDays = 52 * 7 + dayOfWeek + 1;
      
      const startDate = new Date(today);
      startDate.setDate(today.getDate() - totalDays + 1);

      let currentDate = new Date(startDate);
      let monthCols = {};
      let colIdx = 0;

      while (currentDate <= today) {
        const col = document.createElement('div');
        col.className = 'flex flex-col space-y-[3px]';

        for (let row = 0; row < 7; row++) {
          if (currentDate > today) {
            const emptyCell = document.createElement('div');
            emptyCell.className = 'commit-square-sm opacity-0 pointer-events-none';
            col.appendChild(emptyCell);
            continue;
          }

          const dateStr = currentDate.toISOString().substring(0, 10);
          const monthName = currentDate.toLocaleString('default', { month: 'short' });
          const dayNum = currentDate.getDate();

          if (row === 0 && dayNum <= 7 && !monthCols[monthName]) {
            monthCols[monthName] = colIdx;
          }

          const dayData = daily[dateStr] || { steps: 0, tokens: 0, sessions: 0 };
          const cell = document.createElement('div');
          cell.className = 'commit-square-sm';
          cell.style.backgroundColor = getCommitColor(dayData.steps);

          const formattedDate = currentDate.toLocaleDateString('default', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
          const tooltipMsg = dayData.steps > 0 
            ? `${dayData.steps.toLocaleString()} operations (${formatTokens(dayData.tokens)} tokens across ${dayData.sessions} sessions) on ${formattedDate}`
            : `No operations on ${formattedDate}`;

          cell.addEventListener('mouseenter', () => {
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          col.appendChild(cell);
          currentDate.setDate(currentDate.getDate() + 1);
        }

        gridContainer.appendChild(col);
        colIdx++;
      }

      for (let c = 0; c < colIdx; c++) {
        const span = document.createElement('span');
        span.style.width = '14px';
        span.style.display = 'inline-block';
        for (const [mName, mCol] of Object.entries(monthCols)) {
          if (mCol === c) { span.textContent = mName; break; }
        }
        monthLabelsContainer.appendChild(span);
      }
    }

    // --- Mode 2: 3 Months (14 Weeks) Matrix ---
    function renderQuarterGrid(daily) {
      const container = document.getElementById('ghQuarterGrid');
      const labelContainer = document.getElementById('ghQuarterMonthLabels');
      container.innerHTML = '';
      labelContainer.innerHTML = '';

      const today = new Date();
      const totalDays = 14 * 7;
      const startDate = new Date(today);
      startDate.setDate(today.getDate() - totalDays + (6 - today.getDay()));

      let currentDate = new Date(startDate);
      let colIdx = 0;
      let monthCols = {};

      while (currentDate <= today) {
        const col = document.createElement('div');
        col.className = 'flex flex-col space-y-[3px]';

        for (let row = 0; row < 7; row++) {
          if (currentDate > today) {
            const emptyCell = document.createElement('div');
            emptyCell.className = 'commit-square opacity-0 pointer-events-none';
            col.appendChild(emptyCell);
            continue;
          }

          const dateStr = currentDate.toISOString().substring(0, 10);
          const monthName = currentDate.toLocaleString('default', { month: 'short' });
          if (row === 0 && !monthCols[monthName]) {
            monthCols[monthName] = colIdx;
          }

          const dayData = daily[dateStr] || { steps: 0, tokens: 0, sessions: 0 };
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(dayData.steps);

          const formattedDate = currentDate.toLocaleDateString('default', { weekday: 'short', month: 'short', day: 'numeric' });
          const tooltipMsg = `${dayData.steps.toLocaleString()} ops (${formatTokens(dayData.tokens)} tokens) on ${formattedDate}`;

          cell.addEventListener('mouseenter', () => { document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`; });
          cell.addEventListener('mouseleave', () => { document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations'; });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          col.appendChild(cell);
          currentDate.setDate(currentDate.getDate() + 1);
        }
        container.appendChild(col);
        colIdx++;
      }

      for (let c = 0; c < colIdx; c++) {
        const span = document.createElement('span');
        span.style.width = '16px';
        span.style.display = 'inline-block';
        for (const [mName, mCol] of Object.entries(monthCols)) {
          if (mCol === c) { span.textContent = mName; break; }
        }
        labelContainer.appendChild(span);
      }
    }

    // --- Mode 3: Past 30 Days (Hourly Matrix: 24 cols x 30 rows) ---
    function renderMonthHourlyGrid(data) {
      const headerContainer = document.getElementById('monthHourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghMonthGrid');
      if (!container) return;
      container.innerHTML = '';

      const sortedDates = Object.keys(data).sort().reverse();

      for (const dateStr of sortedDates) {
        const hours = data[dateStr];
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';

        const dObj = new Date(dateStr + 'T00:00:00');
        const label = document.createElement('div');
        label.className = 'w-16 text-[10px] font-medium text-gray-400 whitespace-nowrap text-right pr-2 select-none';
        label.textContent = dObj.toLocaleDateString('default', { month: 'short', day: 'numeric' });
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        hours.forEach((val, h) => {
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${dateStr} @ ${timeFormatted} — ${val.toLocaleString()} operations`;

          cell.addEventListener('mouseenter', () => {
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          squaresContainer.appendChild(cell);
        });

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    // --- Mode 4: Past 7 Days (Hourly 24x7 Commit Squares) ---
    function renderPast7HourlyGrid(data) {
      const headerContainer = document.getElementById('past7HourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghPast7Grid');
      if (!container) return;
      container.innerHTML = '';

      for (const [dateStr, hours] of Object.entries(data)) {
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';

        const dObj = new Date(dateStr + 'T00:00:00');
        const label = document.createElement('div');
        label.className = 'w-16 text-[10px] font-medium text-gray-400 whitespace-nowrap text-right pr-2 select-none';
        label.textContent = dObj.toLocaleDateString('default', { weekday: 'short', day: 'numeric' });
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        hours.forEach((val, h) => {
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${dateStr} @ ${timeFormatted} — ${val.toLocaleString()} operations`;

          cell.addEventListener('mouseenter', () => {
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });
          cell.addEventListener('click', () => {
            const el = document.getElementById('convSearch');
            if (el) {
              el.value = dateStr;
              filterConversations();
              el.scrollIntoView({ behavior: 'smooth' });
            }
          });

          squaresContainer.appendChild(cell);
        });

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    // --- Mode 5: 24x7 Aggregate (Hourly 24x7 Commit Squares) ---
    function renderHourlyAggregateGrid(matrix) {
      const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      const headerContainer = document.getElementById('hourlyHourHeader');
      if (!headerContainer) return;
      headerContainer.innerHTML = '';
      for (let h = 0; h < 24; h++) {
        const span = document.createElement('span');
        span.className = 'w-[13px] inline-block text-center';
        span.textContent = h % 4 === 0 ? (h < 10 ? '0' + h : h) : '';
        headerContainer.appendChild(span);
      }

      const container = document.getElementById('ghHourlyGrid');
      if (!container) return;
      container.innerHTML = '';

      for (let d = 0; d < 7; d++) {
        const row = document.createElement('div');
        row.className = 'flex items-center space-x-[3px]';
        
        const label = document.createElement('div');
        label.className = 'w-10 text-[10px] font-medium text-gray-400 text-right pr-2 select-none';
        label.textContent = days[d];
        row.appendChild(label);

        const squaresContainer = document.createElement('div');
        squaresContainer.className = 'flex space-x-[3px]';

        for (let h = 0; h < 24; h++) {
          const val = (matrix[d] && matrix[d][h]) || 0;
          const cell = document.createElement('div');
          cell.className = 'commit-square';
          cell.style.backgroundColor = getCommitColor(val);

          const timeFormatted = `${h < 10 ? '0' + h : h}:00`;
          const tooltipMsg = `${days[d]} at ${timeFormatted} (${localTimezoneAbbr}) — ${val.toLocaleString()} operations across all history`;

          cell.addEventListener('mouseenter', () => {
            document.getElementById('ghTooltipText').innerHTML = `<strong>${tooltipMsg}</strong>`;
          });
          cell.addEventListener('mouseleave', () => {
            document.getElementById('ghTooltipText').textContent = 'Hover over any commit square to see operations';
          });

          squaresContainer.appendChild(cell);
        }

        row.appendChild(squaresContainer);
        container.appendChild(row);
      }
    }

    function setHeatmapMode(mode) {
      currentHeatmapMode = mode;
      document.getElementById('ghYearView').className = mode === 'year' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghQuarterView').className = mode === 'quarter' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghMonthView').className = mode === 'month' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghPast7View').className = mode === 'past7hourly' ? 'flex flex-col w-fit' : 'hidden';
      document.getElementById('ghHourlyView').className = mode === 'hourly' ? 'flex flex-col w-fit' : 'hidden';

      const btnClasses = {
        active: 'px-3 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap',
        inactive: 'px-3 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap'
      };

      document.getElementById('hmBtnYear').className = mode === 'year' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnQuarter').className = mode === 'quarter' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnMonth').className = mode === 'month' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnPast7').className = mode === 'past7hourly' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hmBtnHourly').className = mode === 'hourly' ? btnClasses.active : btnClasses.inactive;
    }

    // ==========================================
    // CHARTS & TABLES
    // ==========================================
    function setTimelineRange(days) {
      currentTimelineDays = days;
      const btn7d = document.getElementById('btn7d');
      const btn14d = document.getElementById('btn14d');
      const btn30d = document.getElementById('btn30d');
      const btnAll = document.getElementById('btnAll');

      const btnClasses = {
        active: 'px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition',
        inactive: 'px-2.5 py-1 rounded text-gray-400 hover:text-white transition'
      };

      if (btn7d) btn7d.className = days === 7 ? btnClasses.active : btnClasses.inactive;
      if (btn14d) btn14d.className = days === 14 ? btnClasses.active : btnClasses.inactive;
      if (btn30d) btn30d.className = days === 30 ? btnClasses.active : btnClasses.inactive;
      if (btnAll) btnAll.className = days === null ? btnClasses.active : btnClasses.inactive;
      
      const sliced = (days && allTimelineData.length > days) ? allTimelineData.slice(-days) : allTimelineData;
      renderTimelineChart(sliced);
    }

    function renderTimelineChart(data) {
      const ctx = document.getElementById('timelineChart').getContext('2d');
      const labels = data.map(d => d.day);
      const tokens = data.map(d => d.tokens);
      const sessions = data.map(d => d.sessions);

      if (timelineChart) timelineChart.destroy();

      timelineChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Tokens Consumed',
              data: tokens,
              backgroundColor: 'rgba(99, 102, 241, 0.65)',
              borderColor: '#6366f1',
              borderWidth: 1,
              borderRadius: 4,
              yAxisID: 'y'
            },
            {
              label: 'Sessions',
              data: sessions,
              type: 'line',
              borderColor: '#38bdf8',
              backgroundColor: '#38bdf8',
              borderWidth: 2,
              tension: 0.3,
              pointRadius: 2,
              yAxisID: 'y1'
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#9ca3af', font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => ctx.datasetIndex === 0 ? `Tokens: ${formatTokens(ctx.raw)}` : `Sessions: ${ctx.raw}`
              }
            }
          },
          scales: {
            x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', maxTicksLimit: 12, font: { size: 10 } } },
            y: {
              grid: { color: '#1f2937' },
              ticks: { color: '#9ca3af', callback: v => formatTokens(v), font: { size: 10 } },
              title: { display: true, text: 'Tokens', color: '#6b7280', font: { size: 10 } }
            },
            y1: {
              position: 'right',
              grid: { drawOnChartArea: false },
              ticks: { color: '#38bdf8', font: { size: 10 } },
              title: { display: true, text: 'Sessions', color: '#38bdf8', font: { size: 10 } }
            }
          }
        }
      });
    }

    function renderModelsChart(models) {
      const ctx = document.getElementById('modelsChart').getContext('2d');
      const labels = models.map(m => m.model_name);
      const data = models.map(m => m.tokens);
      const colors = ['#6366f1', '#a855f7', '#ec4899', '#3b82f6', '#10b981', '#f59e0b'];

      if (modelsChart) modelsChart.destroy();

      modelsChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.slice(0, labels.length),
            borderColor: '#111827',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { color: '#9ca3af', boxWidth: 12, font: { size: 10 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          cutout: '65%'
        }
      });
    }

    function setToolFilter(filter) {
      currentToolFilter = filter;
      const btnAll = document.getElementById('toolBtnAll');
      const btnNative = document.getElementById('toolBtnNative');
      const btnMcp = document.getElementById('toolBtnMcp');
      const btnClasses = {
        active: 'px-2.5 py-1 rounded bg-brand-600 text-white font-medium shadow transition',
        inactive: 'px-2.5 py-1 rounded text-gray-400 hover:text-white transition'
      };
      if (btnAll) btnAll.className = filter === 'all' ? btnClasses.active : btnClasses.inactive;
      if (btnNative) btnNative.className = filter === 'native' ? btnClasses.active : btnClasses.inactive;
      if (btnMcp) btnMcp.className = filter === 'mcp' ? btnClasses.active : btnClasses.inactive;
      renderToolsChart(allToolsData);
    }

    function renderToolsChart(tools) {
      const ctx = document.getElementById('toolsChart').getContext('2d');
      let filtered = tools || [];
      if (currentToolFilter === 'native') {
        filtered = filtered.filter(t => !t.is_mcp);
      } else if (currentToolFilter === 'mcp') {
        filtered = filtered.filter(t => t.is_mcp);
      }

      const top10 = filtered.slice(0, 10);
      const labels = top10.map(t => t.tool_name);
      const data = top10.map(t => t.total_calls);

      if (toolsChart) toolsChart.destroy();

      const barBg = currentToolFilter === 'mcp' ? 'rgba(6, 182, 212, 0.75)' : 'rgba(245, 158, 11, 0.75)';
      const barBorder = currentToolFilter === 'mcp' ? '#06b6d4' : '#f59e0b';

      toolsChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: 'Calls',
            data: data,
            backgroundColor: barBg,
            borderColor: barBorder,
            borderWidth: 1,
            borderRadius: 4
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { color: '#1f2937' }, ticks: { color: '#9ca3af', font: { size: 10 } } },
            y: { grid: { display: false }, ticks: { color: '#e5e7eb', font: { size: 11, family: 'monospace' } } }
          }
        }
      });
    }

    function renderMcpSection(mcpData) {
      allMcpData = mcpData || [];
      const ctx = document.getElementById('mcpServerChart').getContext('2d');
      if (!allMcpData || allMcpData.length === 0) return;

      const labels = allMcpData.map(s => s.server);
      const data = allMcpData.map(s => s.calls);
      const colors = ['#06b6d4', '#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#a855f7', '#64748b'];

      if (mcpServerChart) mcpServerChart.destroy();

      mcpServerChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.slice(0, labels.length),
            borderColor: '#111827',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${ctx.raw.toLocaleString()} calls`
              }
            }
          },
          cutout: '68%'
        }
      });

      const leg = document.getElementById('mcpServerLegend');
      if (leg) {
        leg.innerHTML = '';
        const total = data.reduce((a, b) => a + b, 0) || 1;
        allMcpData.forEach((s, idx) => {
          const pct = ((s.calls / total) * 100).toFixed(1);
          const color = colors[idx % colors.length];
          leg.innerHTML += `
            <div class="flex items-center space-x-1.5">
              <div class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color: ${color}"></div>
              <span class="text-gray-300 font-medium truncate max-w-[90px]" title="${s.server}">${s.server}</span>
              <span class="text-gray-400 ml-auto font-mono text-[11px]">${pct}%</span>
            </div>
          `;
        });
      }

      const select = document.getElementById('mcpServerSelect');
      if (select) {
        select.innerHTML = '<option value="all">All Servers</option>';
        allMcpData.forEach(s => {
          select.innerHTML += `<option value="${s.server}">${s.server} (${s.calls})</option>`;
        });
      }

      filterMcpToolsTable();
    }

    function filterMcpToolsTable() {
      const select = document.getElementById('mcpServerSelect');
      const selectedServer = select ? select.value : 'all';
      const tbody = document.getElementById('mcpToolsTableBody');
      if (!tbody) return;
      tbody.innerHTML = '';

      let toolsList = [];
      allMcpData.forEach(s => {
        if (selectedServer === 'all' || selectedServer === s.server) {
          (s.tools || []).forEach(t => {
            toolsList.push({
              server: s.server,
              tool: t.tool,
              calls: t.calls,
              sessions: t.sessions,
              error_rate: t.error_rate
            });
          });
        }
      });

      toolsList.sort((a, b) => b.calls - a.calls);

      if (toolsList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-gray-500">No MCP tool invocations found</td></tr>';
        return;
      }

      toolsList.forEach(t => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/50 transition';
        tr.innerHTML = `
          <td class="py-1.5 px-3 font-mono text-cyan-300 font-medium">${t.tool}</td>
          <td class="py-1.5 px-3"><span class="px-2 py-0.5 rounded text-[10px] font-mono bg-dark-700 text-gray-300 border border-dark-600">${t.server}</span></td>
          <td class="py-1.5 px-3 text-right font-semibold text-white">${t.calls.toLocaleString()}</td>
          <td class="py-1.5 px-3 text-right text-gray-400 font-mono">${t.sessions.toLocaleString()}</td>
          <td class="py-1.5 px-3 text-right font-mono ${t.error_rate > 0 ? 'text-rose-400 font-bold' : 'text-emerald-400'}">${t.error_rate.toFixed(1)}%</td>
        `;
        tbody.appendChild(tr);
      });
    }

    function renderCategoryChart(categories) {
      const ctx = document.getElementById('categoryChart').getContext('2d');
      const top7 = categories.slice(0, 7);
      const labels = top7.map(c => c.category);
      const data = top7.map(c => c.tokens);
      const colors = ['#10b981', '#6366f1', '#3b82f6', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b'];

      if (categoryChart) categoryChart.destroy();

      categoryChart = new Chart(ctx, {
        type: 'polarArea',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: colors.map(c => c + 'aa'),
            borderColor: '#111827',
            borderWidth: 1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'right', labels: { color: '#9ca3af', boxWidth: 10, font: { size: 9.5 } } },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          scales: {
            r: { grid: { color: '#1f2937' }, ticks: { display: false } }
          }
        }
      });
    }

    function renderPhaseChart(phases) {
      const ctx = document.getElementById('phaseChart').getContext('2d');
      if (!phases || phases.length === 0) return;

      const labels = phases.map(p => p.phase.charAt(0).toUpperCase() + p.phase.slice(1));
      const data = phases.map(p => p.tokens);
      const colors = {
        'Reasoning': '#6366f1',
        'Discovery': '#f59e0b',
        'Execution': '#10b981',
        'Verification': '#a855f7',
        'Other': '#6b7280'
      };
      const bgColors = labels.map(l => colors[l] || '#6366f1');

      if (phaseChart) phaseChart.destroy();

      phaseChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: bgColors,
            borderColor: '#111827',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => ` ${ctx.label}: ${formatTokens(ctx.raw)} tokens`
              }
            }
          },
          cutout: '68%'
        }
      });

      const leg = document.getElementById('phaseLegend');
      if (leg) {
        leg.innerHTML = '';
        const total = data.reduce((a, b) => a + b, 0) || 1;
        phases.forEach(p => {
          const name = p.phase.charAt(0).toUpperCase() + p.phase.slice(1);
          const pct = ((p.tokens / total) * 100).toFixed(1);
          const color = colors[name] || '#6366f1';
          leg.innerHTML += `
            <div class="flex items-center space-x-1.5">
              <div class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color: ${color}"></div>
              <span class="text-gray-300 font-medium">${name}</span>
              <span class="text-gray-400 ml-auto font-mono text-[11px]">${pct}%</span>
            </div>
          `;
        });
      }
    }

    function renderHotFiles(files) {
      allHotFiles = files;
      filterHotFiles();
    }

    function setHotFilesTypeFilter(filterType) {
      currentHotFilesFilter = filterType;
      
      const btnClasses = {
        active: 'px-2.5 py-1 rounded-md bg-brand-600 text-white font-medium shadow transition whitespace-nowrap',
        inactive: 'px-2.5 py-1 rounded-md text-gray-400 hover:text-white transition whitespace-nowrap'
      };

      document.getElementById('hfBtnFiles').className = filterType === 'files' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hfBtnDirs').className = filterType === 'dirs' ? btnClasses.active : btnClasses.inactive;
      document.getElementById('hfBtnAll').className = filterType === 'all' ? btnClasses.active : btnClasses.inactive;

      if (filterType === 'dirs') {
        document.getElementById('hfColViews').textContent = 'Searches';
        document.getElementById('hfColEdits').textContent = 'Listings';
      } else {
        document.getElementById('hfColViews').textContent = 'Views';
        document.getElementById('hfColEdits').textContent = 'Edits';
      }

      filterHotFiles();
    }

    function filterHotFiles() {
      const q = document.getElementById('hotFilesSearch').value.toLowerCase();
      const tbody = document.getElementById('hotFilesTableBody');
      tbody.innerHTML = '';

      let filtered = allHotFiles.filter(f => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));

      if (currentHotFilesFilter === 'files') {
        filtered = filtered.filter(f => !f.is_dir);
      } else if (currentHotFilesFilter === 'dirs') {
        filtered = filtered.filter(f => f.is_dir);
      }

      for (const f of filtered.slice(0, 40)) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/60 transition';
        
        let typeBadge = '';
        if (f.is_dir) {
          typeBadge = '<span class="px-2 py-0.5 rounded text-[10px] font-mono bg-amber-950/60 border border-amber-500/30 text-amber-300 flex items-center w-fit space-x-1"><i data-lucide="folder" class="w-2.5 h-2.5 inline mr-1"></i>dir</span>';
        } else {
          const extBadgeClass = f.type === '.ts' ? 'bg-blue-900/60 text-blue-300' :
                               f.type === '.java' ? 'bg-amber-900/60 text-amber-300' :
                               f.type === '.html' ? 'bg-orange-900/60 text-orange-300' :
                               f.type === '.json' ? 'bg-emerald-900/60 text-emerald-300' :
                               f.type === '.md' ? 'bg-purple-900/60 text-purple-300' :
                               'bg-gray-800 text-gray-300';
          typeBadge = `<span class="px-2 py-0.5 rounded text-[10px] font-mono ${extBadgeClass}">${f.type || 'file'}</span>`;
        }

        const col4Val = f.is_dir ? (f.searches || 0).toLocaleString() : (f.views || 0).toLocaleString();
        const col5Val = f.is_dir ? '-' : (f.edits || 0).toLocaleString();
        const col5Class = f.is_dir ? 'text-gray-500' : 'text-indigo-300 font-semibold';

        tr.innerHTML = `
          <td class="py-2.5 px-3 font-mono font-medium text-white max-w-xs truncate" title="${f.path}">
            ${f.is_dir ? '📁 ' : '📄 '}${f.name}
          </td>
          <td class="py-2.5 px-3">${typeBadge}</td>
          <td class="py-2.5 px-3 text-right font-semibold text-white">${f.touches.toLocaleString()}</td>
          <td class="py-2.5 px-3 text-right text-gray-400">${col4Val}</td>
          <td class="py-2.5 px-3 text-right ${col5Class}">${col5Val}</td>
          <td class="py-2.5 px-3 text-right text-gray-400">${f.sessions.toLocaleString()}</td>
        `;
        tbody.appendChild(tr);
      }
      lucide.createIcons();
    }

    function renderConversations(convs) {
      allConversations = convs;
      filterConversations();
    }

    function filterConversations() {
      const el = document.getElementById('convSearch');
      if (!el) return;
      const q = el.value.toLowerCase();
      const tbody = document.getElementById('convTableBody');
      if (!tbody) return;
      tbody.innerHTML = '';

      const filtered = allConversations.filter(c => c.title.toLowerCase().includes(q) || c.id.toLowerCase().includes(q) || (c.created_at && c.created_at.includes(q)));

      for (const c of filtered.slice(0, 50)) {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-dark-800/60 transition';
        
        const dateStr = c.created_at || '--';
        const activeSec = c.active_agent_seconds || c.active_pairing_seconds || c.duration_seconds || 0;
        const wallSec = c.duration_seconds || 0;
        const timeTitle = `Agent Work: ${formatDuration(c.active_agent_seconds || 0)} | Active Pairing: ${formatDuration(c.active_pairing_seconds || 0)} | Total Span: ${formatDuration(wallSec)}`;

        tr.innerHTML = `
          <td class="py-2.5 px-3 text-gray-400 whitespace-nowrap font-mono text-[11px]">${dateStr}</td>
          <td class="py-2.5 px-3 font-medium text-white max-w-sm truncate" title="${c.title}">${c.title}</td>
          <td class="py-2.5 px-3 text-purple-300 whitespace-nowrap">${c.model_name}</td>
          <td class="py-2.5 px-3 text-right text-gray-300">${c.total_steps}</td>
          <td class="py-2.5 px-3 text-right font-semibold text-indigo-400">${formatTokens(c.total_tokens)}</td>
          <td class="py-2.5 px-3 text-right text-gray-300 whitespace-nowrap" title="${timeTitle}">
            <span class="font-medium text-emerald-400">${formatDuration(activeSec)}</span>
            <span class="text-[10px] text-gray-500 block">(${formatDuration(wallSec)})</span>
          </td>
          <td class="py-2.5 px-3 text-center">
            <button onclick="inspectSession('${c.id}')" class="px-2.5 py-1 rounded bg-brand-600/80 hover:bg-brand-600 text-[11px] font-medium text-white transition">
              Inspect
            </button>
          </td>
        `;
        tbody.appendChild(tr);
      }
    }

    async function inspectSession(cid) {
      try {
        const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        const res = await fetch(`/api/session/${cid}?tz=${encodeURIComponent(clientTz)}`);
        const data = await res.json();
        if (!data) return;

        const conv = data.conversation;
        document.getElementById('modalTitle').textContent = conv.title || `Session ${conv.id.substring(0, 8)}`;
        document.getElementById('modalSub').textContent = `ID: ${conv.id} • Started: ${conv.created_at || '--'}`;
        
        const distinctModels = [...new Set(data.steps.map(s => s.model_name).filter(Boolean))];
        const modelLabel = distinctModels.length > 1 ? distinctModels.join(' ➔ ') : (conv.model_name || 'Gemini');
        document.getElementById('modalModel').textContent = modelLabel;
        
        const activeSec = conv.active_agent_seconds || conv.active_pairing_seconds || conv.duration_seconds || 0;
        document.getElementById('modalDuration').textContent = formatDuration(activeSec);
        const modalWallSpanEl = document.getElementById('modalWallSpan');
        if (modalWallSpanEl) {
          modalWallSpanEl.textContent = `Span: ${formatDuration(conv.duration_seconds || 0)}`;
        }

        // Populate Scorecard
        const toolsCnt = conv.tool_calls_count || 0;
        const promptsCnt = conv.user_prompts || 1;
        const autonomyRatio = (toolsCnt / Math.max(1, promptsCnt)).toFixed(1);
        const elAutonomy = document.getElementById('modalAutonomy');
        if (elAutonomy) elAutonomy.textContent = `${autonomyRatio}x (${toolsCnt} tools)`;

        const elThinking = document.getElementById('modalThinking');
        if (elThinking) elThinking.textContent = formatTokens(conv.thinking_tokens || 0);

        const elReReads = document.getElementById('modalReReads');
        const reReads = conv.redundant_file_reads || 0;
        if (elReReads) {
          elReReads.textContent = reReads === 0 ? '✓ 0 re-reads' : `⚠️ ${reReads} repeated`;
          elReReads.className = reReads === 0 ? 'font-semibold text-emerald-400 font-mono mt-0.5 block' : 'font-semibold text-amber-400 font-mono mt-0.5 block';
        }

        const elCodeLines = document.getElementById('modalCodeLines');
        if (elCodeLines) elCodeLines.textContent = `+${(conv.lines_written || 0).toLocaleString()} / ~${(conv.lines_edited || 0).toLocaleString()}`;

        function formatToolBadge(toolOrSource, activity) {
          const isMcp = (activity && activity.startsWith('MCP')) || (toolOrSource && (toolOrSource.includes(':') || toolOrSource.startsWith('`mcp')));
          if (isMcp) {
            return `<span class="px-1.5 py-0.5 rounded text-[11px] font-mono bg-cyan-950/60 text-cyan-300 border border-cyan-500/30">${toolOrSource}</span>`;
          }
          return `<span class="text-indigo-300 font-mono">${toolOrSource}</span>`;
        }

        const tcBody = document.getElementById('modalTopConsumers');
        tcBody.innerHTML = '';
        for (const s of data.top_consumers) {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-2 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-2 px-2.5 font-medium text-white">${s.activity}</td>
            <td class="py-2 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-2 px-2.5 text-right font-semibold text-white">${formatTokens(s.est_tokens)}</td>
            <td class="py-2 px-2.5 text-gray-400 max-w-xs truncate" title="${s.detail || ''}">${s.detail || '--'}</td>
          `;
          tcBody.appendChild(tr);
        }

        const sBody = document.getElementById('modalStepList');
        sBody.innerHTML = '';
        let lastModel = null;
        for (const s of data.steps) {
          const isModelSwitch = lastModel && s.model_name && s.model_name !== lastModel;
          lastModel = s.model_name || lastModel;

          if (isModelSwitch) {
            const switchTr = document.createElement('tr');
            switchTr.className = 'bg-brand-950/60 border-y border-brand-500/30';
            switchTr.innerHTML = `
              <td colspan="5" class="py-1 px-2.5 text-center text-xs font-semibold text-brand-300">
                🔄 Model Switched to: <span class="text-white">${s.model_name}</span>
              </td>
            `;
            sBody.appendChild(switchTr);
          }

          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td class="py-1.5 px-2.5 text-gray-400 font-mono">#${s.step_index}</td>
            <td class="py-1.5 px-2.5 text-gray-300">${s.activity}</td>
            <td class="py-1.5 px-2.5">${formatToolBadge(s.tool_or_source, s.activity)}</td>
            <td class="py-1.5 px-2.5 text-right text-gray-400">${formatTokens(s.est_tokens)}</td>
            <td class="py-1.5 px-2.5 text-gray-400 max-w-md truncate" title="${s.detail || ''}">${s.detail || '--'}</td>
          `;
          sBody.appendChild(tr);
        }

        document.getElementById('sessionModal').classList.remove('hidden');
        document.getElementById('sessionModal').classList.add('flex');
        lucide.createIcons();
      } catch (err) {
        console.error("Failed to fetch session", err);
      }
    }

    function closeModal() {
      const modal = document.getElementById('sessionModal');
      if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
      }
    }

    function initLiveSyncSSE() {
      let evtSource = null;

      function connect() {
        evtSource = new EventSource('/api/events');

        evtSource.addEventListener('connected', () => {
          const badge = document.getElementById('liveSyncBadge');
          const text = document.getElementById('syncText');
          if (badge) badge.className = 'px-3 py-1.5 rounded-md bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 text-xs font-medium flex items-center space-x-1.5 transition select-none';
          if (text) text.textContent = 'Live Sync: SSE Active';
        });

        evtSource.addEventListener('db_update', (event) => {
          console.log("⚡ SQLite change detected via SSE. Refreshing UI...");
          fetchDashboardData();
        });

        evtSource.onerror = () => {
          const badge = document.getElementById('liveSyncBadge');
          const text = document.getElementById('syncText');
          if (badge) badge.className = 'px-3 py-1.5 rounded-md bg-amber-950/60 border border-amber-500/30 text-amber-300 text-xs font-medium flex items-center space-x-1.5 transition select-none';
          if (text) text.textContent = 'Live Sync: Reconnecting...';
          try { evtSource.close(); } catch (e) {}
          setTimeout(connect, 3000);
        };
      }

      connect();
    }

    document.addEventListener('DOMContentLoaded', () => {
      fetchDashboardData();
      initLiveSyncSSE();
    });
  </script>
</body>
</html>
"""

class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        cls, err, tb = sys.exc_info()
        if cls in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, socket.error):
            return
        super().handle_error(request, client_address)

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, socket.error):
            pass

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, socket.error):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        client_tz = qs.get("tz", [None])[0]

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            client_q = queue.Queue(maxsize=50)
            with SSE_LOCK:
                SSE_CLIENTS.add(client_q)

            try:
                init_msg = f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
                self.wfile.write(init_msg.encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        msg_obj = client_q.get(timeout=15.0)
                        evt = msg_obj.get("event", "message")
                        data_str = json.dumps(msg_obj.get("data", {}))
                        payload = f"event: {evt}\ndata: {data_str}\n\n"
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.error):
                pass
            finally:
                with SSE_LOCK:
                    SSE_CLIENTS.discard(client_q)
            return

        if not DEFAULT_DB_PATH.exists():
            self.send_error(500, "analytics.db not found. Run backfill_analytics.py first.")
            return

        conn = get_db_connection()
        try:
            if path == "/api/summary":
                data = query_summary_data(conn, client_tz)
                self.send_json(data)
            elif path == "/api/timeline":
                days = qs.get("days", [None])[0]
                data = query_timeline_data(conn, days, client_tz)
                self.send_json(data)
            elif path == "/api/tools":
                data = query_tools_data(conn)
                self.send_json(data)
            elif path == "/api/mcp":
                data = query_mcp_data(conn)
                self.send_json(data)
            elif path == "/api/hot-files":
                data = query_hot_files_data(conn)
                self.send_json(data)
            elif path == "/api/activity-matrix":
                data = query_activity_matrix(conn, client_tz)
                self.send_json(data)
            elif path == "/api/conversations":
                limit = int(qs.get("limit", [200])[0])
                data = query_conversations_list(conn, limit, client_tz)
                self.send_json(data)
            elif path.startswith("/api/session/"):
                cid = path.split("/")[-1]
                data = query_session_details(conn, cid, client_tz)
                self.send_json(data or {})
            else:
                self.send_error(404, "Endpoint not found")
        finally:
            conn.close()

    def send_json(self, data):
        try:
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

def export_standalone_html(output_path: Path, include_conversations: bool = False, tz: str = None):
    conn = get_db_connection()
    target_tz = get_target_tz(tz)
    try:
        summary = query_summary_data(conn, target_tz)
        timeline = query_timeline_data(conn, None, target_tz)
        tools = query_tools_data(conn)
        mcp = query_mcp_data(conn)
        hot_files = query_hot_files_data(conn)
        activity_matrix = query_activity_matrix(conn, target_tz)
        conversations = query_conversations_list(conn, 200, target_tz) if include_conversations else []
    finally:
        conn.close()

    embedded_js = f"""
    <script>
      const IS_STATIC_EXPORT = true;
      const STATIC_DATA = {{
        summary: {json.dumps(summary)},
        timeline: {json.dumps(timeline)},
        tools: {json.dumps(tools)},
        mcp: {json.dumps(mcp)},
        hot_files: {json.dumps(hot_files)},
        activity_matrix: {json.dumps(activity_matrix)},
        conversations: {json.dumps(conversations)}
      }};
      
      window.fetch = async function(url) {{
        if (url.includes('/api/summary')) return {{ json: async () => STATIC_DATA.summary }};
        if (url.includes('/api/timeline')) return {{ json: async () => STATIC_DATA.timeline }};
        if (url.includes('/api/tools')) return {{ json: async () => STATIC_DATA.tools }};
        if (url.includes('/api/mcp')) return {{ json: async () => STATIC_DATA.mcp }};
        if (url.includes('/api/hot-files')) return {{ json: async () => STATIC_DATA.hot_files }};
        if (url.includes('/api/activity-matrix')) return {{ json: async () => STATIC_DATA.activity_matrix }};
        if (url.includes('/api/conversations')) return {{ json: async () => STATIC_DATA.conversations }};
        return {{ json: async () => ({{}}) }};
      }};
    </script>
    """

    html_content = HTML_TEMPLATE.replace("</head>", f"{embedded_js}\n</head>")

    # Replace live controls in header with clean static timestamp badge
    gen_time_str = datetime.now(target_tz).strftime('%b %d, %Y %H:%M %Z')
    static_header_badge = f'''<div class="flex items-center space-x-2 text-xs text-gray-300 bg-dark-800 border border-dark-700 px-3 py-1.5 rounded-lg shadow-sm">
      <i data-lucide="calendar" class="w-3.5 h-3.5 text-brand-400"></i>
      <span>Snapshot: {gen_time_str}</span>
    </div>'''
    
    html_content = re.sub(r'<!-- Header Actions.*?-->\s*<div id="headerActions".*?</div>\s*</header>', f'{static_header_badge}\n  </header>', html_content, flags=re.DOTALL)
    html_content = html_content.replace('<span id="liveStatus">Analytics Active</span>', '<span id="liveStatus">Snapshot Archive</span>')
    html_content = html_content.replace('animate-pulse', '')

    # Strip initLiveSyncSSE() function and call completely from exported standalone HTML
    html_content = re.sub(r'function initLiveSyncSSE\(\)\s*\{.*?\}\s*document\.addEventListener', 'document.addEventListener', html_content, flags=re.DOTALL)
    html_content = html_content.replace('initLiveSyncSSE();', '')

    if not include_conversations:
        # Strip Conversation Explorer section from HTML
        html_content = re.sub(r'<!-- 6\. Searchable Conversation Explorer.*?-->\s*<div id="convExplorerSection".*?</div>\s*</div>\s*</main>', '</main>', html_content, flags=re.DOTALL)
        # Strip Session Deep-Dive Modal from HTML
        html_content = re.sub(r'<!-- Session Deep-Dive Modal.*?-->.*?<script>', '<script>', html_content, flags=re.DOTALL)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    privacy_badge = "[With Convo Explorer]" if include_conversations else "[Privacy-Safe: Convo Explorer Omitted]"
    print(f"✅ Standalone HTML report exported to: {output_path} (Timezone: {target_tz}) {privacy_badge}")

def main():
    parser = argparse.ArgumentParser(description="Antigravity Web Analytics Dashboard & HTML Exporter")
    parser.add_argument("--port", type=int, default=8765, help="HTTP server port (default: 8765)")
    parser.add_argument("--open", action="store_true", help="Open dashboard in browser automatically")
    parser.add_argument("--export-html", type=str, help="Export standalone offline HTML file")
    parser.add_argument("--include-conversations", action="store_true", help="Include conversation explorer & session details in exported HTML (default: False / privacy-safe)")
    parser.add_argument("--timezone", "-tz", type=str, default=None, help="Explicit timezone name (e.g. 'America/New_York', 'US/Eastern', 'America/Chicago')")

    args = parser.parse_args()

    if args.export_html:
        export_standalone_html(Path(args.export_html).resolve(), include_conversations=args.include_conversations, tz=args.timezone)
        sys.exit(0)

    # Start background SQLite change watcher
    watcher = DbChangeWatcher(DEFAULT_DB_PATH)
    watcher.start()

    server = DashboardServer(("127.0.0.1", args.port), DashboardHTTPHandler)
    url = f"http://localhost:{args.port}"
    tz_info = get_target_tz(args.timezone)
    print(f"\n" + "=" * 65)
    print(f" 🚀 Antigravity Analytics Studio Web GUI is LIVE!")
    print(f" Access URL : {url}")
    print(f" Timezone   : {tz_info}")
    print(f" Database   : {DEFAULT_DB_PATH}")
    print(f" Live Sync  : SSE Push Enabled (watching SQLite changes)")
    print(f"=" * 65 + "\n")

    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping analytics dashboard server...")
        server.server_close()

if __name__ == "__main__":
    main()
