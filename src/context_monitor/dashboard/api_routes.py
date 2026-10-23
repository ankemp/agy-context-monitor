from pathlib import Path
import sqlite3
import math
import os
import json
import re
from datetime import datetime, timedelta, tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from context_monitor.db import get_quota_state, ensure_rollups_synced
from context_monitor.pricing import get_pricing_key, get_model_context_limit
from context_monitor.quota import (
    _get_default_capacity,
    _to_utc_datetime,
    calculate_quota_metrics,
    get_hourly_activity_profile,
    sync_and_get_quota_state,
)
from context_monitor.timings import calculate_force_multiplier
from context_monitor.velocity import compute_active_velocity, query_rolling_velocity

CHARS_PER_TOKEN = 3.8


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


class QuotaPercentages(NamedTuple):
    weekly_quota_pct: float
    five_hour_quota_pct: float


def _extract_single_capacity(
    qs_dict: dict[str, Any],
    *key_candidates: str,
    default_capacity: float,
) -> float:
    for k in key_candidates:
        if k in qs_dict:
            val = qs_dict[k]
            if isinstance(val, (int, float)):
                try:
                    f = float(val)
                    if f > 0:
                        return f
                except (ValueError, TypeError):
                    pass
            elif isinstance(val, dict):
                c = (
                    val.get("calibrated_capacity")
                    or val.get("calibratedCapacity")
                    or val.get("capacity")
                    or val.get("cap")
                )
                if c is not None:
                    try:
                        f = float(c)
                        if f > 0:
                            return f
                    except (ValueError, TypeError):
                        pass
            elif isinstance(val, str):
                try:
                    f = float(val)
                    if f > 0:
                        return f
                except (ValueError, TypeError):
                    pass
    return default_capacity


def extract_bucket_capacities(
    quota_state: list[dict] | dict[str, Any] | tuple | None,
) -> dict[str, float]:
    """Extracts calibrated capacities as a dictionary with standard bucket IDs."""
    if (
        isinstance(quota_state, (tuple, list))
        and len(quota_state) == 4
        and all(isinstance(x, (int, float)) for x in quota_state)
    ):
        return {
            "gemini-weekly": float(quota_state[0]),
            "gemini-5h": float(quota_state[1]),
            "3p-weekly": float(quota_state[2]),
            "3p-5h": float(quota_state[3]),
        }

    qs_dict: dict[str, Any] = {}
    if isinstance(quota_state, dict):
        qs_dict = quota_state
        if "buckets" in qs_dict and isinstance(qs_dict["buckets"], dict):
            for k, v in qs_dict["buckets"].items():
                qs_dict[str(k).lower()] = v
        for k, v in list(qs_dict.items()):
            qs_dict[str(k).lower()] = v
    elif isinstance(quota_state, (list, tuple)):
        for item in quota_state:
            if isinstance(item, dict):
                bid = (
                    item.get("bucket_id")
                    or item.get("bucketId")
                    or item.get("bucket")
                    or ""
                ).lower()
                if bid:
                    qs_dict[bid] = item
            elif hasattr(item, "keys"):
                bid = str(item.get("bucket_id") or item.get("bucketId") or "").lower()
                if bid:
                    qs_dict[bid] = dict(item)

    gw_cap = _extract_single_capacity(
        qs_dict,
        "gemini-weekly",
        "gemini_weekly",
        "gw",
        "gw_cap",
        default_capacity=float(_get_default_capacity("gemini-weekly") or 672.0),
    )
    g5_cap = _extract_single_capacity(
        qs_dict,
        "gemini-5h",
        "gemini_5h",
        "g5",
        "g5_cap",
        default_capacity=float(_get_default_capacity("gemini-5h") or 236.7),
    )
    pw_cap = _extract_single_capacity(
        qs_dict,
        "3p-weekly",
        "3p_weekly",
        "pw",
        "pw_cap",
        default_capacity=float(_get_default_capacity("3p-weekly") or 200.0),
    )
    p5_cap = _extract_single_capacity(
        qs_dict,
        "3p-5h",
        "3p_5h",
        "p5",
        "p5_cap",
        default_capacity=float(_get_default_capacity("3p-5h") or 70.0),
    )
    return {
        "gemini-weekly": gw_cap,
        "gemini-5h": g5_cap,
        "3p-weekly": pw_cap,
        "3p-5h": p5_cap,
    }


def _extract_bucket_capacities(
    quota_state: list[dict] | dict[str, Any] | tuple | None,
) -> tuple[float, float, float, float]:
    """Extracts calibrated capacities (gw, g5, pw, p5) from quota state."""
    caps = extract_bucket_capacities(quota_state)
    return (
        caps["gemini-weekly"],
        caps["gemini-5h"],
        caps["3p-weekly"],
        caps["3p-5h"],
    )


def compute_quota_pcts(
    billed_cost: float | None = 0.0,
    model_name: str | None = None,
    capacities: (
        dict[str, float] | tuple[float, float, float, float] | float | None
    ) = None,
    g5_cap: float | None = None,
    pw_cap: float | None = None,
    p5_cap: float | None = None,
    *,
    cost: float | None = None,
) -> QuotaPercentages:
    """Computes (weekly_quota_pct, five_hour_quota_pct) in O(1) time without pacing simulations.

    Interfaces supported:
    1. compute_quota_pcts(billed_cost: float, model_name: str, capacities: dict[str, float])
    2. compute_quota_pcts(cost, model_name, gw_cap, g5_cap, pw_cap, p5_cap)
    3. compute_quota_pcts(cost, model_name, capacities_tuple)
    """
    eff_cost = (
        cost if cost is not None else (billed_cost if billed_cost is not None else 0.0)
    )
    try:
        c = float(eff_cost)
        if math.isnan(c) or math.isinf(c):
            c = 0.0
    except (ValueError, TypeError):
        c = 0.0

    if isinstance(capacities, (dict, list)) or (
        isinstance(capacities, tuple) and len(capacities) != 4
    ):
        caps = extract_bucket_capacities(capacities)
        gw = caps["gemini-weekly"]
        g5 = caps["gemini-5h"]
        pw = caps["3p-weekly"]
        p5 = caps["3p-5h"]
    elif isinstance(capacities, tuple) and len(capacities) == 4:
        gw, g5, pw, p5 = capacities
    elif isinstance(capacities, (int, float)):
        gw = float(capacities)
        g5 = (
            float(g5_cap)
            if g5_cap is not None
            else float(_get_default_capacity("gemini-5h") or 236.7)
        )
        pw = (
            float(pw_cap)
            if pw_cap is not None
            else float(_get_default_capacity("3p-weekly") or 200.0)
        )
        p5 = (
            float(p5_cap)
            if p5_cap is not None
            else float(_get_default_capacity("3p-5h") or 70.0)
        )
    else:
        caps = extract_bucket_capacities(None)
        gw = caps["gemini-weekly"]
        g5 = caps["gemini-5h"]
        pw = caps["3p-weekly"]
        p5 = caps["3p-5h"]

    pkey = get_pricing_key(model_name or "Gemini")
    is_3p = pkey in ("claude", "gpt")
    cap_w = pw if is_3p else gw
    cap_5 = p5 if is_3p else g5

    w_pct = round((c / cap_w) * 100.0, 4) if cap_w > 0 else 0.0
    f_pct = round((c / cap_5) * 100.0, 4) if cap_5 > 0 else 0.0

    return QuotaPercentages(weekly_quota_pct=w_pct, five_hour_quota_pct=f_pct)


_compute_quota_pcts = compute_quota_pcts


def get_target_tz(tz_name=None):
    if tz_name:
        if isinstance(tz_name, (ZoneInfo, tzinfo)) or hasattr(tz_name, "utcoffset"):
            return tz_name
        try:
            return ZoneInfo(str(tz_name).strip())
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


def query_quota_data(
    conn, model_name: str | None = None, billed_cost: float = 0.0
) -> dict:
    quota_state = sync_and_get_quota_state(conn)
    activity_profile = get_hourly_activity_profile(conn) if conn is not None else None
    metrics = calculate_quota_metrics(
        billed_cost=billed_cost,
        model_name=model_name or "Gemini 3.8 Flash (High)",
        quota_state=quota_state,
        activity_profile=activity_profile,
    )
    res = dict(metrics)
    res["quota_state"] = [dict(r) for r in quota_state]
    res["gemini_weekly"] = metrics.get("gemini-weekly")
    res["gemini_5h"] = metrics.get("gemini-5h")
    res["threep_weekly"] = metrics.get("3p-weekly")
    res["threep_5h"] = metrics.get("3p-5h")
    res["buckets"] = {
        "gemini-weekly": metrics.get("gemini-weekly"),
        "gemini-5h": metrics.get("gemini-5h"),
        "3p-weekly": metrics.get("3p-weekly"),
        "3p-5h": metrics.get("3p-5h"),
    }
    return res


def query_summary_data(conn, tz=None, project=None):
    cur = conn.cursor()

    where_conv = ""
    params_conv = ()
    where_tool = ""
    where_step_and = ""
    if project:
        if project == "(unassigned)":
            where_conv = "WHERE (project IS NULL OR project = '')"
            where_tool = "AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
            where_step_and = "AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_conv = "WHERE project = ?"
            where_tool = "AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            where_step_and = "AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params_conv = (project,)

    row = cur.execute(
        f"""
        SELECT 
            COUNT(*) as total_convs,
            COUNT(CASE WHEN parent_id IS NULL THEN 1 END) as root_convs,
            COUNT(CASE WHEN parent_id IS NOT NULL THEN 1 END) as subagent_convs,
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
    """,
        params_conv,
    ).fetchone()

    total_prompts = row["total_prompts"] or 0
    total_tools = row["total_tools"] or 0
    autonomy_multiplier = round(total_tools / max(1, total_prompts), 1)
    total_code_lines = (row["total_written"] or 0) + (row["total_edited"] or 0)
    total_fails = row["total_fails"] or 0
    resilience_rate = round(
        ((total_tools - total_fails) / max(1, total_tools)) * 100, 1
    )

    # Fast tool execution counts using covering index
    tool_name_filter = (
        "('view_file', 'grep_search', 'find_by_name', 'list_dir', 'run_command', "
        "'write_to_file', 'replace_file_content', 'multi_replace_file_content')"
    )
    tool_rows = cur.execute(
        f"""
        SELECT tool_name, COUNT(*) as cnt
        FROM tool_executions
        WHERE tool_name IN {tool_name_filter} {where_tool}
        GROUP BY tool_name
    """,
        params_conv,
    ).fetchall()

    tc_map = {r["tool_name"]: r["cnt"] for r in tool_rows}
    total_views = tc_map.get("view_file", 0)
    reads_cnt = (
        tc_map.get("view_file", 0)
        + tc_map.get("grep_search", 0)
        + tc_map.get("find_by_name", 0)
        + tc_map.get("list_dir", 0)
    )
    cmds_cnt = tc_map.get("run_command", 0)
    edits_cnt = (
        tc_map.get("write_to_file", 0)
        + tc_map.get("replace_file_content", 0)
        + tc_map.get("multi_replace_file_content", 0)
    )

    total_re_reads = row["total_re_reads"] or 0
    re_read_rate = round((total_re_reads / max(1, total_views)) * 100, 1)
    re_read_waste_tokens = total_re_reads * 750
    saved_seconds = (reads_cnt * 30) + (cmds_cnt * 45) + (edits_cnt * 120)
    saved_hours = round(saved_seconds / 3600)
    total_pairing_work = max(0, row["total_pairing_work"] or 0)
    total_agent_work = max(0, row["total_agent_work"] or 0)
    fm, eff_sec, _ = calculate_force_multiplier(
        total_pairing_work, total_agent_work, saved_seconds
    )
    force_multiplier = round(fm, 1)
    effective_output_hours = round(eff_sec / 3600.0, 1)

    # Phase breakdown
    phase_rows = cur.execute(
        f"""
        SELECT phase, COUNT(*) as steps, SUM(est_tokens) as tokens
        FROM steps
        WHERE phase IS NOT NULL {where_step_and}
        GROUP BY phase
    """,
        params_conv,
    ).fetchall()
    phases = [
        {"phase": r["phase"], "steps": r["steps"], "tokens": r["tokens"] or 0}
        for r in phase_rows
    ]
    phases.sort(key=lambda x: x["tokens"], reverse=True)

    # Top bloat steps
    bloat_rows = cur.execute(
        f"""
        SELECT step_index, phase, tool_or_source, est_tokens, detail
        FROM steps
        WHERE est_tokens > 2000 {where_step_and}
        ORDER BY est_tokens DESC
        LIMIT 8
    """,
        params_conv,
    ).fetchall()
    bloat_steps = [
        {
            "step_index": r["step_index"],
            "phase": r["phase"] or "other",
            "tool_or_source": r["tool_or_source"],
            "est_tokens": r["est_tokens"],
            "detail": r["detail"],
        }
        for r in bloat_rows
    ]

    total_billed_cost = row["total_billed_cost"] or 0.0
    total_billed_input_tokens = row["total_billed_input_tokens"] or 0
    total_billed_output_tokens = row["total_billed_output_tokens"] or 0

    models = []
    model_data: dict[str, dict[str, Any]] = {}
    if project:
        if project == "(unassigned)":
            model_sql = """
                SELECT s.model_name as mname,
                       COUNT(*) as steps,
                       SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
                FROM steps s
                WHERE s.conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')
                GROUP BY s.model_name
            """
            model_params = ()
        else:
            model_sql = """
                SELECT s.model_name as mname,
                       COUNT(*) as steps,
                       SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
                FROM steps s
                WHERE s.conversation_id IN (SELECT id FROM conversations WHERE project = ?)
                GROUP BY s.model_name
            """
            model_params = (project,)
    else:
        model_sql = """
            SELECT model_name as mname,
                   COUNT(*) as steps,
                   SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) as tokens
            FROM steps
            GROUP BY model_name
        """
        model_params = ()

    model_rows = cur.execute(model_sql, model_params).fetchall()
    has_resolved_project_nulls = False
    for r in model_rows:
        name = r["mname"]
        if not name:
            if project:
                if not has_resolved_project_nulls:
                    has_resolved_project_nulls = True
                    if project == "(unassigned)":
                        null_sql = """
                            SELECT COALESCE(c.model_name, 'Gemini Flash') as cname,
                                   COUNT(*) as steps,
                                   SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
                            FROM steps s
                            JOIN conversations c ON s.conversation_id = c.id
                            WHERE (s.model_name IS NULL OR s.model_name = '')
                              AND (c.project IS NULL OR c.project = '')
                            GROUP BY cname
                        """
                        null_params = ()
                    else:
                        null_sql = """
                            SELECT COALESCE(c.model_name, 'Gemini Flash') as cname,
                                   COUNT(*) as steps,
                                   SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)) as tokens
                            FROM steps s
                            JOIN conversations c ON s.conversation_id = c.id
                            WHERE (s.model_name IS NULL OR s.model_name = '')
                              AND c.project = ?
                            GROUP BY cname
                        """
                        null_params = (project,)
                    null_rows = cur.execute(null_sql, null_params).fetchall()
                    for null_row in null_rows:
                        cname = null_row["cname"] or "Gemini Flash"
                        c_steps = null_row["steps"] or 0
                        c_tokens = null_row["tokens"] or 0
                        if cname in model_data:
                            model_data[cname]["steps"] += c_steps
                            model_data[cname]["tokens"] += c_tokens
                        else:
                            model_data[cname] = {
                                "model_name": cname,
                                "steps": c_steps,
                                "tokens": c_tokens,
                            }
                continue
            else:
                name = "Gemini Flash"

        tokens = r["tokens"] or 0
        steps = r["steps"] or 0
        if name in model_data:
            model_data[name]["steps"] += steps
            model_data[name]["tokens"] += tokens
        else:
            model_data[name] = {
                "model_name": name,
                "steps": steps,
                "tokens": tokens,
            }

    models = sorted(model_data.values(), key=lambda x: x["tokens"], reverse=True)

    category_rows = cur.execute(
        f"SELECT category, SUM(est_tokens) as tokens FROM steps WHERE category IS NOT NULL {where_step_and} GROUP BY category",
        params_conv,
    ).fetchall()
    categories = [
        {"category": r["category"], "tokens": r["tokens"] or 0} for r in category_rows
    ]
    categories.sort(key=lambda x: x["tokens"], reverse=True)
    categories = categories[:10]

    earliest_local = parse_to_local_dt(row["earliest"], tz)
    latest_local = parse_to_local_dt(row["latest"], tz)

    tz_info = get_target_tz(tz)
    tz_label = datetime.now(tz_info).strftime("%Z (%z)")

    q_data = query_quota_data(conn, billed_cost=total_billed_cost)

    active_vel = compute_active_velocity(
        est_tokens=row["total_tokens"] or 0,
        billed_tokens=total_billed_input_tokens + total_billed_output_tokens,
        active_agent_seconds=total_agent_work,
        active_pairing_seconds=total_pairing_work,
    )
    rolling_vel = query_rolling_velocity(conn)
    token_velocity = {
        "active_context_tpm": active_vel["active_context_tpm"],
        "active_billed_tpm": active_vel["active_billed_tpm"],
        "pairing_context_tph": active_vel["pairing_context_tph"],
        "pairing_billed_tph": active_vel["pairing_billed_tph"],
        "rolling_5m_context_tpm": rolling_vel["rolling_5m_context_tpm"],
        "rolling_5m_billed_tpm": rolling_vel["rolling_5m_billed_tpm"],
        "rolling_60m_context_tph": rolling_vel["rolling_60m_context_tph"],
        "rolling_60m_billed_tph": rolling_vel["rolling_60m_billed_tph"],
        "peak_billed_tpm": rolling_vel["peak_billed_tpm"],
        "peak_context_tpm": rolling_vel["peak_context_tpm"],
        "peak_billed_tph": rolling_vel["peak_billed_tph"],
        "gauge_pct": rolling_vel["gauge_pct"],
        "context_gauge_pct": rolling_vel["context_gauge_pct"],
    }
    if project:
        proj_filter = (
            "(c.project IS NULL OR c.project = '')"
            if project == "(unassigned)"
            else "c.project = ?"
        )
        proj_params = () if project == "(unassigned)" else (project,)
        try:
            cur_series = conn.execute(
                f"""
                SELECT
                    h.hour_key,
                    SUM(h.step_count) AS step_count,
                    SUM(h.est_tokens) AS est_tokens,
                    SUM(h.billed_tokens) AS billed_tokens,
                    SUM(h.billed_cost) AS billed_cost
                FROM hourly_step_rollups h
                JOIN conversations c ON h.conversation_id = c.id
                WHERE h.hour_key IS NOT NULL AND h.hour_key != ''
                  AND {proj_filter}
                GROUP BY h.hour_key
                ORDER BY h.hour_key DESC
                LIMIT 24
                """,
                proj_params,
            )
            p_rows = cur_series.fetchall()
            if not p_rows:
                cur_series = conn.execute(
                    f"""
                    SELECT
                        substr(replace(s.created_at, ' ', 'T'), 1, 13) AS hour_key,
                        COUNT(*) AS step_count,
                        COALESCE(SUM(s.est_tokens), 0) AS est_tokens,
                        COALESCE(SUM(COALESCE(s.billed_input_tokens, 0) + COALESCE(s.billed_output_tokens, 0)), 0) AS billed_tokens,
                        COALESCE(SUM(COALESCE(s.billed_cost, 0.0)), 0.0) AS billed_cost
                    FROM steps s
                    JOIN conversations c ON s.conversation_id = c.id
                    WHERE s.created_at IS NOT NULL AND s.created_at != ''
                      AND {proj_filter}
                    GROUP BY hour_key
                    ORDER BY hour_key DESC
                    LIMIT 24
                    """,
                    proj_params,
                )
                p_rows = cur_series.fetchall()
            p_series = []
            for pr in reversed(p_rows):
                h_k = str(pr[0])
                s_c = int(pr[1] or 0)
                e_t = int(pr[2] or 0)
                b_t = int(pr[3] or 0)
                b_c = round(float(pr[4] or 0.0), 4)
                p_series.append(
                    {
                        "hour_key": h_k,
                        "step_count": s_c,
                        "est_tokens": e_t,
                        "billed_tokens": b_t,
                        "billed_cost": b_c,
                        "est_tpm": round(e_t / 60.0, 2),
                        "billed_tpm": round(b_t / 60.0, 2),
                    }
                )
            hourly_series = p_series
        except Exception:
            hourly_series = []
    else:
        hourly_series = rolling_vel.get("hourly_series", [])

    return {
        "total_convs": row["total_convs"] or 0,
        "root_convs": row["root_convs"] or (row["total_convs"] or 0),
        "subagent_convs": row["subagent_convs"] or 0,
        "total_steps": row["total_steps"] or 0,
        "total_tokens": row["total_tokens"] or 0,
        "total_chars": row["total_chars"] or 0,
        "total_duration": row["total_duration"] or 0,
        "total_agent_work": total_agent_work,
        "total_pairing_work": total_pairing_work,
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
        "saved_seconds": saved_seconds,
        "saved_hours": saved_hours,
        "force_multiplier": force_multiplier,
        "effective_output_hours": effective_output_hours,
        "saved_reads": reads_cnt,
        "saved_cmds": cmds_cnt,
        "saved_edits": edits_cnt,
        "phases": phases,
        "bloat_steps": bloat_steps,
        "total_billed_cost": round(total_billed_cost, 2),
        "total_billed_input_tokens": total_billed_input_tokens,
        "total_billed_output_tokens": total_billed_output_tokens,
        "avg_steps": round(row["avg_steps"] or 0, 1),
        "avg_tokens": int(row["avg_tokens"] or 0),
        "earliest": earliest_local.strftime("%Y-%m-%d %H:%M")
        if earliest_local
        else None,
        "latest": latest_local.strftime("%Y-%m-%d %H:%M") if latest_local else None,
        "timezone": str(tz_info),
        "timezone_label": tz_label,
        "models": models,
        "categories": categories,
        "quota": q_data,
        "quota_summary": q_data,
        "token_velocity": token_velocity,
        "hourly_series": hourly_series,
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
        if project and (
            p_name == project
            or (project == "(unassigned)" and p_name == "(unassigned)")
        ):
            filtered_project_cost = cost
        act_tokens = (r["billed_input_tokens"] or 0) + (r["billed_output_tokens"] or 0)
        projects.append(
            {
                "project": p_name,
                "raw_project": None if p_name == "(unassigned)" else p_name,
                "cost": cost,
                "percentage": pct,
                "sessions": r["session_count"],
                "tokens": act_tokens,
                "disk_tokens": r["total_tokens"] or 0,
                "steps": r["total_steps"] or 0,
                "billed_input_tokens": r["billed_input_tokens"] or 0,
                "billed_output_tokens": r["billed_output_tokens"] or 0,
            }
        )

    # Query top expensive sessions grouped by Campaign (narrowed to project if filtered)
    where_sess = ""
    params_sess = ()
    if project and project != "all":
        if project == "(unassigned)":
            where_sess = "WHERE (c.project IS NULL OR c.project = '')"
        else:
            where_sess = "WHERE c.project = ?"
            params_sess = (project,)

    try:
        sess_rows = cur.execute(
            f"""
            WITH campaign_rollups AS (
                SELECT 
                    COALESCE(NULLIF(root_id, ''), id) AS campaign_id,
                    SUM(COALESCE(billed_cost, 0.0)) AS billed_cost,
                    SUM(CASE WHEN id = COALESCE(NULLIF(root_id, ''), id) THEN COALESCE(billed_cost, 0.0) ELSE 0.0 END) AS local_cost,
                    COUNT(CASE WHEN id != COALESCE(NULLIF(root_id, ''), id) THEN 1 END) AS subagent_count,
                    SUM(COALESCE(billed_input_tokens, 0)) AS billed_input_tokens,
                    SUM(COALESCE(billed_output_tokens, 0)) AS billed_output_tokens,
                    SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                    SUM(COALESCE(total_steps, 0)) AS total_steps,
                    SUM(COALESCE(duration_seconds, 0)) AS duration_seconds,
                    SUM(COALESCE(active_agent_seconds, 0)) AS active_agent_seconds,
                    MAX(created_at) AS max_created_at
                FROM conversations
                GROUP BY COALESCE(NULLIF(root_id, ''), id)
            )
            SELECT 
                cr.campaign_id AS id,
                COALESCE(c.title, 'Session ' || substr(cr.campaign_id, 1, 8)) AS title,
                c.project AS project,
                c.model_name AS model_name,
                c.models_used AS models_used,
                COALESCE(c.created_at, cr.max_created_at) AS created_at,
                cr.total_steps AS total_steps,
                cr.total_tokens AS total_tokens,
                cr.duration_seconds AS duration_seconds,
                cr.active_agent_seconds AS active_agent_seconds,
                cr.billed_cost AS billed_cost,
                cr.local_cost AS local_cost,
                cr.subagent_count AS subagent_count,
                cr.billed_input_tokens AS billed_input_tokens,
                cr.billed_output_tokens AS billed_output_tokens
            FROM campaign_rollups cr
            LEFT JOIN conversations c ON c.id = cr.campaign_id
            {where_sess}
            ORDER BY cr.billed_cost DESC, (COALESCE(cr.billed_input_tokens, 0) + COALESCE(cr.billed_output_tokens, 0)) DESC, cr.total_tokens DESC
            LIMIT ?
        """,
            [*params_sess, limit_sessions],
        ).fetchall()
    except Exception:
        try:
            sess_rows = cur.execute(
                f"""
                WITH campaign_rollups AS (
                    SELECT 
                        COALESCE(NULLIF(root_id, ''), id) AS campaign_id,
                        SUM(COALESCE(billed_cost, 0.0)) AS billed_cost,
                        SUM(CASE WHEN id = COALESCE(NULLIF(root_id, ''), id) THEN COALESCE(billed_cost, 0.0) ELSE 0.0 END) AS local_cost,
                        COUNT(CASE WHEN id != COALESCE(NULLIF(root_id, ''), id) THEN 1 END) AS subagent_count,
                        SUM(COALESCE(billed_input_tokens, 0)) AS billed_input_tokens,
                        SUM(COALESCE(billed_output_tokens, 0)) AS billed_output_tokens,
                        SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                        SUM(COALESCE(total_steps, 0)) AS total_steps,
                        SUM(COALESCE(duration_seconds, 0)) AS duration_seconds,
                        SUM(COALESCE(active_agent_seconds, 0)) AS active_agent_seconds,
                        MAX(created_at) AS max_created_at
                    FROM conversations
                    GROUP BY COALESCE(NULLIF(root_id, ''), id)
                )
                SELECT 
                    cr.campaign_id AS id,
                    COALESCE(c.title, 'Session ' || substr(cr.campaign_id, 1, 8)) AS title,
                    c.project AS project,
                    c.model_name AS model_name,
                    COALESCE(c.created_at, cr.max_created_at) AS created_at,
                    cr.total_steps AS total_steps,
                    cr.total_tokens AS total_tokens,
                    cr.duration_seconds AS duration_seconds,
                    cr.active_agent_seconds AS active_agent_seconds,
                    cr.billed_cost AS billed_cost,
                    cr.local_cost AS local_cost,
                    cr.subagent_count AS subagent_count,
                    cr.billed_input_tokens AS billed_input_tokens,
                    cr.billed_output_tokens AS billed_output_tokens
                FROM campaign_rollups cr
                LEFT JOIN conversations c ON c.id = cr.campaign_id
                {where_sess}
                ORDER BY cr.billed_cost DESC, (COALESCE(cr.billed_input_tokens, 0) + COALESCE(cr.billed_output_tokens, 0)) DESC, cr.total_tokens DESC
                LIMIT ?
            """,
                [*params_sess, limit_sessions],
            ).fetchall()
        except Exception:
            where_plain = where_sess.replace("c.project", "project")
            sess_rows = cur.execute(
                f"""
                SELECT id, title, project, model_name, created_at, total_steps, total_tokens, duration_seconds, active_agent_seconds, billed_cost, billed_input_tokens, billed_output_tokens
                FROM conversations
                {where_plain}
                ORDER BY billed_cost DESC, (COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) DESC, total_tokens DESC
                LIMIT ?
            """,
                [*params_sess, limit_sessions],
            ).fetchall()

    top_sessions = []
    target_tz = get_target_tz(tz)
    for sr in sess_rows:
        dt = parse_to_local_dt(sr["created_at"], target_tz)
        act_tok = (sr["billed_input_tokens"] or 0) + (sr["billed_output_tokens"] or 0)
        models_used_val = sr["model_name"] or "Gemini"
        try:
            if "models_used" in sr.keys() and sr["models_used"]:
                models_used_val = sr["models_used"]
        except Exception:
            pass
        local_cost_val = (
            round(sr["local_cost"] or 0.0, 2)
            if "local_cost" in sr.keys() and sr["local_cost"] is not None
            else round(sr["billed_cost"] or 0.0, 2)
        )
        subagent_count_val = (
            int(sr["subagent_count"] or 0)
            if "subagent_count" in sr.keys() and sr["subagent_count"] is not None
            else 0
        )
        top_sessions.append(
            {
                "id": sr["id"],
                "title": sr["title"] or f"Session {sr['id'][:8]}",
                "project": sr["project"] or "(unassigned)",
                "model_name": sr["model_name"] or "Gemini",
                "models_used": models_used_val,
                "created_at": dt.strftime("%Y-%m-%d %H:%M")
                if dt
                else (sr["created_at"] or "--"),
                "total_steps": sr["total_steps"] or 0,
                "tokens": act_tok,
                "total_tokens": act_tok,
                "disk_tokens": sr["total_tokens"] or 0,
                "duration_seconds": sr["duration_seconds"] or 0,
                "active_agent_seconds": sr["active_agent_seconds"] or 0,
                "billed_cost": round(sr["billed_cost"] or 0.0, 2),
                "fleet_cost": round(sr["billed_cost"] or 0.0, 2),
                "local_cost": local_cost_val,
                "subagent_count": subagent_count_val,
            }
        )

    return {
        "total_cost": round(total_cost, 2),
        "projects": projects,
        "top_sessions": top_sessions,
        "filtered_project": project if (project and project != "all") else None,
        "filtered_project_cost": filtered_project_cost,
    }


def _make_timeline_item(
    day: str,
    label: str | None = None,
    is_start: bool = False,
    is_reset: bool = False,
    is_future: bool = False,
    sessions: int = 0,
    steps: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    projects: dict[str, int] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "day": day,
        "sessions": sessions,
        "steps": steps,
        "tokens": tokens,
        "cost": round(cost, 2),
        "projects": projects if projects is not None else {},
    }
    if label is not None:
        item["label"] = label
        item["is_start"] = is_start
        item["is_reset"] = is_reset
        item["is_future"] = is_future
    return item


def query_timeline_data(conn, days=None, tz=None, project=None):
    is_cycle_mode = (
        str(days).lower().strip() in ("cycle", "reset", "since_reset")
        if (days is not None and not isinstance(days, bool))
        else False
    )

    num_days = None
    if not is_cycle_mode and days is not None and not isinstance(days, bool):
        try:
            val = int(float(days)) if isinstance(days, str) else int(days)
            if val <= 0:
                return []
            if val > 100_000:
                num_days = None
            else:
                num_days = val
        except (ValueError, TypeError, OverflowError):
            num_days = None

    try:
        ensure_rollups_synced(conn)
    except Exception:
        pass

    cur = conn.cursor()
    target_tz = get_target_tz(tz)
    today = datetime.now(target_tz).date()

    conv_proj = dict(
        cur.execute(
            'SELECT id, COALESCE(NULLIF(project, ""), "(unassigned)") FROM conversations'
        ).fetchall()
    )

    has_valid_reset = False
    start_dt_utc = None
    reset_dt_utc = None
    start_dt_local = None
    reset_dt_local = None

    if is_cycle_mode:
        where_conditions = ["hour_key IS NOT NULL", "hour_key != ''"]
        params = []
        if project and project != "all":
            if project == "(unassigned)":
                where_conditions.append(
                    "conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
                )
            else:
                where_conditions.append(
                    "conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
                )
                params.append(project)

        try:
            quota_rows = get_quota_state(conn)
        except Exception:
            quota_rows = []

        gw_bucket = None
        for b in quota_rows:
            bid = str(b.get("bucket_id") or b.get("bucketId") or "").lower()
            if bid in ("gemini-weekly", "gemini_weekly"):
                gw_bucket = b
                break

        if not (gw_bucket and gw_bucket.get("reset_time")):
            for b in quota_rows:
                bid = str(b.get("bucket_id") or b.get("bucketId") or "").lower()
                win = str(b.get("window") or "").lower()
                if ("weekly" in bid or win == "weekly") and b.get("reset_time"):
                    gw_bucket = b
                    break

        reset_time = gw_bucket.get("reset_time") if gw_bucket else None
        if reset_time:
            reset_dt_utc = _to_utc_datetime(reset_time)
            if reset_dt_utc is not None:
                reset_dt_utc = reset_dt_utc.replace(microsecond=0)
                start_dt_utc = reset_dt_utc - timedelta(days=7)
                start_dt_local = start_dt_utc.astimezone(target_tz)
                reset_dt_local = reset_dt_utc.astimezone(target_tz)
                if reset_dt_local.date() >= today:
                    has_valid_reset = True

        if has_valid_reset:
            start_dt_utc_clean = start_dt_utc.replace(microsecond=0)
            reset_dt_utc_clean = reset_dt_utc.replace(microsecond=0)
            start_hour = start_dt_utc_clean.strftime("%Y-%m-%dT%H")
            end_hour = reset_dt_utc_clean.strftime("%Y-%m-%dT%H")
            where_conditions.append("hour_key >= ?")
            params.append(start_hour)
            where_conditions.append("hour_key < ?")
            params.append(end_hour)
            start_date = start_dt_local.date()
            end_date = reset_dt_local.date()
        else:
            start_date = today - timedelta(days=6)
            end_date = today
            cutoff_date = today - timedelta(days=8)
            where_conditions.append("hour_key >= ?")
            params.append(cutoff_date.isoformat())

        where_clause = "WHERE " + " AND ".join(where_conditions)
        try:
            rows = cur.execute(
                f"""
                SELECT 
                    hour_key,
                    conversation_id,
                    step_count,
                    billed_tokens as tokens,
                    billed_cost as cost
                FROM hourly_step_rollups
                {where_clause}
            """,
                tuple(params),
            ).fetchall()

            if has_valid_reset and (
                start_dt_utc.minute > 0
                or start_dt_utc.second > 0
                or start_dt_utc.microsecond > 0
            ):
                boundary_where = ["created_at IS NOT NULL", "created_at != ''"]
                boundary_params = []
                if project and project != "all":
                    if project == "(unassigned)":
                        boundary_where.append(
                            "conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
                        )
                    else:
                        boundary_where.append(
                            "conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
                        )
                        boundary_params.append(project)

                start_hour_space = start_hour.replace("T", " ")
                next_hour_utc = start_dt_utc_clean.replace(
                    minute=0, second=0
                ) + timedelta(hours=1)
                next_hour_space = next_hour_utc.strftime("%Y-%m-%d %H")
                next_hour_t = next_hour_utc.strftime("%Y-%m-%dT%H")

                boundary_where.append(
                    "((created_at >= ? AND created_at < ?) OR (created_at >= ? AND created_at < ?))"
                )
                boundary_params.extend(
                    [start_hour_space, next_hour_space, start_hour, next_hour_t]
                )
                boundary_where.append("replace(created_at, ' ', 'T') >= ?")
                boundary_params.append(start_dt_utc_clean.strftime("%Y-%m-%dT%H:%M:%S"))
                boundary_where.append(
                    "substr(replace(created_at, ' ', 'T'), 1, 13) = ?"
                )
                boundary_params.append(start_hour)

                b_clause = "WHERE " + " AND ".join(boundary_where)
                b_rows = cur.execute(
                    f"""
                    SELECT 
                        substr(replace(created_at, ' ', 'T'), 1, 13) as hour_key,
                        conversation_id,
                        COUNT(*) as step_count,
                        SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) as tokens,
                        SUM(billed_cost) as cost
                    FROM steps
                    {b_clause}
                    GROUP BY hour_key, conversation_id
                """,
                    tuple(boundary_params),
                ).fetchall()
                rows = [r for r in rows if r["hour_key"] != start_hour] + list(b_rows)
        except (sqlite3.OperationalError, Exception):
            legacy_where = where_clause.replace(
                "hour_key", "substr(replace(created_at, ' ', 'T'), 1, 13)"
            )
            rows = cur.execute(
                f"""
                SELECT 
                    substr(replace(created_at, ' ', 'T'), 1, 13) as hour_key,
                    conversation_id,
                    COUNT(*) as step_count,
                    SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) as tokens,
                    SUM(billed_cost) as cost
                FROM steps
                {legacy_where}
                GROUP BY hour_key, conversation_id
            """,
                tuple(params),
            ).fetchall()
    else:
        where_conditions = ["hour_key IS NOT NULL", "hour_key != ''"]
        params = []
        if project and project != "all":
            if project == "(unassigned)":
                where_conditions.append(
                    "conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
                )
            else:
                where_conditions.append(
                    "conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
                )
                params.append(project)

        if num_days is not None:
            try:
                cutoff_date = today - timedelta(days=num_days + 1)
                where_conditions.append("hour_key >= ?")
                params.append(cutoff_date.isoformat())
            except OverflowError:
                num_days = None

        where_clause = "WHERE " + " AND ".join(where_conditions)
        try:
            rows = cur.execute(
                f"""
                SELECT 
                    hour_key,
                    conversation_id,
                    step_count,
                    billed_tokens as tokens,
                    billed_cost as cost
                FROM hourly_step_rollups
                {where_clause}
            """,
                tuple(params),
            ).fetchall()
        except (sqlite3.OperationalError, Exception):
            legacy_where = where_clause.replace("hour_key", "created_at")
            rows = cur.execute(
                f"""
                SELECT 
                    substr(created_at, 1, 13) as hour_key,
                    conversation_id,
                    COUNT(*) as step_count,
                    SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) as tokens,
                    SUM(billed_cost) as cost
                FROM steps
                {legacy_where}
                GROUP BY hour_key, conversation_id
            """,
                tuple(params),
            ).fetchall()

    daily_map = {}
    day_sessions = {}
    hour_cache = {}

    for r in rows:
        hour_key = r["hour_key"]
        cid = r["conversation_id"]
        step_cnt = r["step_count"]
        tokens = r["tokens"] or 0
        cost = r["cost"] or 0.0

        cached_day = hour_cache.get(hour_key)
        if cached_day is None:
            try:
                dt = datetime.fromisoformat(hour_key + ":00:00+00:00").astimezone(
                    target_tz
                )
                cached_day = dt.strftime("%Y-%m-%d")
                hour_cache[hour_key] = cached_day
            except (ValueError, TypeError):
                continue
        day_str = cached_day

        if day_str not in daily_map:
            daily_map[day_str] = {
                "day": day_str,
                "sessions": 0,
                "steps": 0,
                "tokens": 0,
                "cost": 0.0,
                "projects": {},
            }
            day_sessions[day_str] = set()

        daily_map[day_str]["steps"] += step_cnt
        daily_map[day_str]["tokens"] += tokens
        daily_map[day_str]["cost"] += cost
        day_sessions[day_str].add(cid)

        proj = conv_proj.get(cid, "(unassigned)")
        daily_map[day_str]["projects"][proj] = (
            daily_map[day_str]["projects"].get(proj, 0) + tokens
        )

    for day_str, s_set in day_sessions.items():
        daily_map[day_str]["sessions"] = len(s_set)

    if is_cycle_mode:
        pass
    elif num_days is not None:
        start_date = today - timedelta(days=num_days - 1)
        end_date = today
    else:
        if daily_map:
            min_date = min(
                datetime.strptime(d, "%Y-%m-%d").date() for d in daily_map.keys()
            )
            max_date = max(
                datetime.strptime(d, "%Y-%m-%d").date() for d in daily_map.keys()
            )
            start_date = min(min_date, today - timedelta(days=29))
            end_date = max(max_date, today)
        else:
            start_date = today - timedelta(days=29)
            end_date = today

    timeline = []
    curr = start_date
    while curr <= end_date:
        d_str = curr.strftime("%Y-%m-%d")
        if is_cycle_mode:
            is_start = False
            is_reset = False
            if has_valid_reset:
                if curr == start_date:
                    is_start = True
                    label = start_dt_local.strftime("%a %H:%M")
                elif curr == end_date:
                    is_reset = True
                    label = reset_dt_local.strftime("%a %H:%M")
                else:
                    label = d_str
            else:
                label = d_str

            is_future = curr > today
            if is_future:
                timeline.append(
                    _make_timeline_item(
                        d_str,
                        label=label,
                        is_start=is_start,
                        is_reset=is_reset,
                        is_future=True,
                    )
                )
            else:
                if d_str in daily_map:
                    item = daily_map[d_str]
                    timeline.append(
                        _make_timeline_item(
                            d_str,
                            label=label,
                            is_start=is_start,
                            is_reset=is_reset,
                            is_future=False,
                            sessions=item["sessions"],
                            steps=item["steps"],
                            tokens=item["tokens"],
                            cost=item["cost"],
                            projects=item["projects"],
                        )
                    )
                else:
                    timeline.append(
                        _make_timeline_item(
                            d_str,
                            label=label,
                            is_start=is_start,
                            is_reset=is_reset,
                            is_future=False,
                        )
                    )
        else:
            if d_str in daily_map:
                item = daily_map[d_str]
                timeline.append(
                    _make_timeline_item(
                        d_str,
                        sessions=item["sessions"],
                        steps=item["steps"],
                        tokens=item["tokens"],
                        cost=item["cost"],
                        projects=item["projects"],
                    )
                )
            else:
                timeline.append(_make_timeline_item(d_str))
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

    rows = cur.execute(
        f"""
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
    """,
        params,
    ).fetchall()

    tools = []
    for r in rows:
        tools.append(
            {
                "tool_name": r["tool_name"],
                "mcp_server": r["mcp_server"],
                "is_mcp": 1 if r["mcp_server"] else 0,
                "total_calls": r["total_calls"],
                "distinct_sessions": r["distinct_sessions"],
            }
        )
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

    server_rows = cur.execute(
        f"""
        SELECT 
            mcp_server,
            COUNT(*) as total_calls,
            COUNT(DISTINCT conversation_id) as sessions,
            AVG(CASE WHEN is_error = 1 THEN 1.0 ELSE 0.0 END) * 100 as error_rate
        FROM tool_executions
        {where_clause}
        GROUP BY mcp_server
        ORDER BY total_calls DESC
    """,
        params,
    ).fetchall()

    servers = []
    for s in server_rows:
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
            (s["mcp_server"],),
        ).fetchall()
        servers.append(
            {
                "server": s["mcp_server"],
                "calls": s["total_calls"],
                "sessions": s["sessions"],
                "error_rate": round(s["error_rate"] or 0, 1),
                "tools": [
                    {
                        "tool": tr["mcp_tool"],
                        "calls": tr["calls"],
                        "sessions": tr["sessions"],
                        "error_rate": round(tr["error_rate"] or 0, 1),
                    }
                    for tr in tool_rows
                ],
            }
        )
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

    rows = cur.execute(
        f"""
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
    """,
        params,
    ).fetchall()

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

        items.append(
            {
                "name": name,
                "path": raw_path,
                "type": type_label,
                "is_dir": is_dir,
                "touches": touches,
                "views": views,
                "edits": edits,
                "searches": searches,
                "sessions": r["sessions"],
            }
        )
    return items


def query_activity_matrix(conn, tz=None, project=None):
    try:
        ensure_rollups_synced(conn)
    except Exception:
        pass

    cur = conn.cursor()
    target_tz = get_target_tz(tz)

    where_clause = "WHERE hour_key IS NOT NULL AND hour_key != ''"
    params = ()
    if project:
        if project == "(unassigned)":
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project IS NULL OR project = '')"
        else:
            where_clause += " AND conversation_id IN (SELECT id FROM conversations WHERE project = ?)"
            params = (project,)

    try:
        rows = cur.execute(
            f"""
            SELECT 
                hour_key,
                conversation_id,
                step_count,
                est_tokens as tokens
            FROM hourly_step_rollups
            {where_clause}
        """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        legacy_where = where_clause.replace("hour_key", "created_at")
        rows = cur.execute(
            f"""
            SELECT 
                substr(created_at, 1, 13) as hour_key,
                conversation_id,
                COUNT(*) as step_count,
                SUM(est_tokens) as tokens
            FROM steps
            {legacy_where}
            GROUP BY hour_key, conversation_id
        """,
            params,
        ).fetchall()

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
    hour_cache = {}

    for r in rows:
        hour_key = r["hour_key"]
        cid = r["conversation_id"]
        step_cnt = r["step_count"]
        tokens = r["tokens"] or 0

        cached = hour_cache.get(hour_key)
        if cached is None:
            dt = datetime.fromisoformat(hour_key + ":00:00+00:00").astimezone(target_tz)
            cached = (dt.strftime("%Y-%m-%d"), dt.weekday(), dt.hour)
            hour_cache[hour_key] = cached
        d_str, weekday, hour = cached

        hourly_aggregate[weekday][hour] += step_cnt

        if d_str in past_30_days_hourly:
            past_30_days_hourly[d_str][hour] += step_cnt
        if d_str in past_7_days_hourly:
            past_7_days_hourly[d_str][hour] += step_cnt

        if d_str not in daily_map:
            daily_map[d_str] = {"steps": 0, "tokens": 0, "sessions": 0}
            day_sessions[d_str] = set()

        daily_map[d_str]["steps"] += step_cnt
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
            "total_active_days": len(active_days),
        },
    }


def query_conversations_list(
    conn, limit=200, tz=None, project=None, view_mode="hierarchy"
):
    cur = conn.cursor()
    target_tz = get_target_tz(tz)
    where_parts = []
    params = []
    if project:
        if project == "(unassigned)":
            where_parts.append("(project IS NULL OR project = '')")
        else:
            where_parts.append("project = ?")
            params.append(project)

    if view_mode == "hierarchy":
        where_parts.append("parent_id IS NULL")

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    try:
        rows = cur.execute(
            f"""
            SELECT id, title, project, model_name, models_used, created_at, updated_at, total_steps, total_tokens,
                   duration_seconds, active_agent_seconds, active_pairing_seconds,
                   billed_cost, billed_input_tokens, billed_output_tokens,
                   parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                   compaction_count, pruned_tokens
            FROM conversations
            {where_clause}
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT {limit}
        """,
            tuple(params),
        ).fetchall()
    except Exception:
        rows = cur.execute(
            f"""
            SELECT id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
                   duration_seconds, active_agent_seconds, active_pairing_seconds,
                   billed_cost, billed_input_tokens, billed_output_tokens,
                   parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                   compaction_count, pruned_tokens
            FROM conversations
            {where_clause}
            ORDER BY COALESCE(updated_at, created_at) DESC
            LIMIT {limit}
        """,
            tuple(params),
        ).fetchall()

    quota_state = sync_and_get_quota_state(conn)
    bucket_caps = _extract_bucket_capacities(quota_state)

    convs = []
    root_ids = [r["id"] for r in rows]

    descendants_by_root = {}
    if view_mode == "hierarchy" and root_ids:
        placeholders = ",".join("?" for _ in root_ids)
        try:
            desc_rows = cur.execute(
                f"""
                SELECT id, title, project, model_name, models_used, created_at, updated_at, total_steps, total_tokens,
                       duration_seconds, active_agent_seconds, active_pairing_seconds,
                       billed_cost, billed_input_tokens, billed_output_tokens,
                       parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                       compaction_count, pruned_tokens
                FROM conversations
                WHERE root_id IN ({placeholders}) AND id != root_id
                ORDER BY depth ASC, invoking_step_index ASC, created_at ASC
            """,
                tuple(root_ids),
            ).fetchall()
        except Exception:
            desc_rows = cur.execute(
                f"""
                SELECT id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
                       duration_seconds, active_agent_seconds, active_pairing_seconds,
                       billed_cost, billed_input_tokens, billed_output_tokens,
                       parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                       compaction_count, pruned_tokens
                FROM conversations
                WHERE root_id IN ({placeholders}) AND id != root_id
                ORDER BY depth ASC, invoking_step_index ASC, created_at ASC
            """,
                tuple(root_ids),
            ).fetchall()

        for dr in desc_rows:
            rid = dr["root_id"]
            if rid not in descendants_by_root:
                descendants_by_root[rid] = []
            d_dt = parse_to_local_dt(dr["created_at"], target_tz)
            d_udt = (
                parse_to_local_dt(dr["updated_at"], target_tz)
                if ("updated_at" in dr.keys() and dr["updated_at"])
                else None
            )
            d_act = (dr["billed_input_tokens"] or 0) + (dr["billed_output_tokens"] or 0)
            dr_m_used = dr["model_name"] or "Gemini"
            try:
                if "models_used" in dr.keys() and dr["models_used"]:
                    dr_m_used = dr["models_used"]
            except Exception:
                pass
            d_cost = round(dr["billed_cost"] or 0.0, 2)
            d_w_pct, d_5h_pct = _compute_quota_pcts(d_cost, dr_m_used, bucket_caps)
            d_vel = compute_active_velocity(
                est_tokens=dr["total_tokens"] or 0,
                billed_tokens=d_act,
                active_agent_seconds=dr["active_agent_seconds"] or 0,
                active_pairing_seconds=dr["active_pairing_seconds"] or 0,
            )
            descendants_by_root[rid].append(
                {
                    "id": dr["id"],
                    "title": dr["title"] or f"Subagent {dr['id'][:8]}",
                    "project": dr["project"] or "(unassigned)",
                    "model_name": dr["model_name"] or "Gemini",
                    "models_used": dr_m_used,
                    "created_at": d_dt.strftime("%Y-%m-%d %H:%M")
                    if d_dt
                    else dr["created_at"],
                    "updated_at": d_udt.strftime("%Y-%m-%d %H:%M")
                    if d_udt
                    else (
                        dr["updated_at"]
                        if "updated_at" in dr.keys() and dr["updated_at"]
                        else dr["created_at"]
                    ),
                    "total_steps": dr["total_steps"] or 0,
                    "tokens": d_act,
                    "total_tokens": d_act,
                    "disk_tokens": dr["total_tokens"] or 0,
                    "duration_seconds": dr["duration_seconds"] or 0,
                    "active_agent_seconds": dr["active_agent_seconds"] or 0,
                    "fleet_agent_seconds": round(
                        float(dr["active_agent_seconds"] or 0.0), 1
                    ),
                    "active_pairing_seconds": dr["active_pairing_seconds"] or 0,
                    "active_context_tpm": d_vel["active_context_tpm"],
                    "active_billed_tpm": d_vel["active_billed_tpm"],
                    "pairing_billed_tph": d_vel["pairing_billed_tph"],
                    "velocity": d_vel,
                    "billed_cost": d_cost,
                    "weekly_quota_pct": d_w_pct,
                    "five_hour_quota_pct": d_5h_pct,
                    "parent_id": dr["parent_id"],
                    "root_id": dr["root_id"],
                    "depth": dr["depth"] or 1,
                    "subagent_role": dr["subagent_role"] or "Worker",
                    "subagent_type": dr["subagent_type"] or "self",
                    "invoking_step_index": dr["invoking_step_index"],
                    "compaction_count": dr["compaction_count"] or 0,
                    "pruned_tokens": dr["pruned_tokens"] or 0,
                }
            )

        # Compute nested descendant agent rollups for subagents with children
        for rid, descs in descendants_by_root.items():
            children_by_parent: dict[str, list[dict]] = {}
            for d in descs:
                p_id = d.get("parent_id")
                if p_id:
                    children_by_parent.setdefault(p_id, []).append(d)

            def _get_sub_descendant_agent_sec(node_id: str) -> float:
                tot = 0.0
                for child in children_by_parent.get(node_id, []):
                    tot += float(
                        child.get("active_agent_seconds") or 0.0
                    ) + _get_sub_descendant_agent_sec(child["id"])
                return tot

            for d in descs:
                sub_desc_sec = _get_sub_descendant_agent_sec(d["id"])
                d["fleet_agent_seconds"] = round(
                    float(d.get("active_agent_seconds") or 0.0) + sub_desc_sec, 1
                )

    for r in rows:
        dt = parse_to_local_dt(r["created_at"], target_tz)
        udt = (
            parse_to_local_dt(r["updated_at"], target_tz)
            if ("updated_at" in r.keys() and r["updated_at"])
            else None
        )
        act_tok = (r["billed_input_tokens"] or 0) + (r["billed_output_tokens"] or 0)
        c_cost = round(r["billed_cost"] or 0.0, 2)
        c_steps = r["total_steps"] or 0
        c_id = r["id"]
        c_compaction = r["compaction_count"] or 0
        c_pruned = r["pruned_tokens"] or 0

        all_desc = descendants_by_root.get(c_id, [])
        fleet_cost = round(c_cost + sum(d["billed_cost"] for d in all_desc), 2)
        fleet_tokens = act_tok + sum(d["tokens"] for d in all_desc)
        fleet_steps = c_steps + sum(d["total_steps"] for d in all_desc)
        fleet_compaction = c_compaction + sum(
            d.get("compaction_count", 0) for d in all_desc
        )
        fleet_pruned = c_pruned + sum(d.get("pruned_tokens", 0) for d in all_desc)
        subagent_count = len(all_desc)
        descendant_agent_seconds = sum(
            float(d.get("active_agent_seconds") or 0.0) for d in all_desc
        )
        fleet_agent_seconds = round(
            float(r["active_agent_seconds"] or 0.0) + descendant_agent_seconds, 1
        )

        r_m_used = r["model_name"] or "Gemini"
        try:
            if "models_used" in r.keys() and r["models_used"]:
                r_m_used = r["models_used"]
        except Exception:
            pass

        c_w_pct, c_5h_pct = _compute_quota_pcts(c_cost, r_m_used, bucket_caps)
        f_w_pct, f_5h_pct = _compute_quota_pcts(fleet_cost, r_m_used, bucket_caps)

        conv_vel = compute_active_velocity(
            est_tokens=r["total_tokens"] or 0,
            billed_tokens=act_tok,
            active_agent_seconds=r["active_agent_seconds"] or 0,
            active_pairing_seconds=r["active_pairing_seconds"] or 0,
        )

        convs.append(
            {
                "id": c_id,
                "title": r["title"] or f"Conversation {r['id'][:8]}",
                "project": r["project"] or "(unassigned)",
                "model_name": r["model_name"] or "Gemini",
                "models_used": r_m_used,
                "created_at": dt.strftime("%Y-%m-%d %H:%M") if dt else r["created_at"],
                "updated_at": udt.strftime("%Y-%m-%d %H:%M")
                if udt
                else (
                    r["updated_at"]
                    if "updated_at" in r.keys() and r["updated_at"]
                    else r["created_at"]
                ),
                "total_steps": c_steps,
                "tokens": act_tok,
                "total_tokens": act_tok,
                "disk_tokens": r["total_tokens"] or 0,
                "duration_seconds": r["duration_seconds"] or 0,
                "active_agent_seconds": r["active_agent_seconds"] or 0,
                "fleet_agent_seconds": fleet_agent_seconds,
                "active_pairing_seconds": r["active_pairing_seconds"] or 0,
                "active_context_tpm": conv_vel["active_context_tpm"],
                "active_billed_tpm": conv_vel["active_billed_tpm"],
                "pairing_billed_tph": conv_vel["pairing_billed_tph"],
                "velocity": conv_vel,
                "billed_cost": c_cost,
                "weekly_quota_pct": c_w_pct,
                "five_hour_quota_pct": c_5h_pct,
                "parent_id": r["parent_id"],
                "root_id": r["root_id"],
                "depth": r["depth"] or 0,
                "subagent_role": r["subagent_role"],
                "subagent_type": r["subagent_type"],
                "subagent_count": subagent_count,
                "fleet_cost": fleet_cost,
                "fleet_tokens": fleet_tokens,
                "fleet_steps": fleet_steps,
                "fleet_weekly_quota_pct": f_w_pct,
                "fleet_5h_quota_pct": f_5h_pct,
                "compaction_count": c_compaction,
                "pruned_tokens": c_pruned,
                "fleet_compaction_count": fleet_compaction,
                "fleet_pruned_tokens": fleet_pruned,
                "subagents": all_desc,
            }
        )
    return convs


def query_session_details(conn, cid, tz=None):
    cur = conn.cursor()
    conv = cur.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if not conv:
        return None

    steps_rows = cur.execute(
        """
        SELECT step_index, created_at, source, activity, tool_or_source, category, est_tokens, thinking_tokens, phase, model_name, detail,
               billed_cost, billed_input_tokens, billed_output_tokens
        FROM steps
        WHERE conversation_id = ?
        ORDER BY step_index ASC
    """,
        (cid,),
    ).fetchall()

    # 1. Fetch tool executions for tool name mapping
    tool_rows = cur.execute(
        """
        SELECT step_index, tool_name, mcp_server, mcp_tool, target_path, command_line, is_error
        FROM tool_executions
        WHERE conversation_id = ?
        ORDER BY id ASC
    """,
        (cid,),
    ).fetchall()

    tool_execs_by_step = {}
    for tr in tool_rows:
        s_idx = tr["step_index"]
        if s_idx not in tool_execs_by_step:
            tool_execs_by_step[s_idx] = []
        tool_execs_by_step[s_idx].append(dict(tr))

    # 2. Fetch transcript blocks
    blocks_rows = cur.execute(
        """
        SELECT step_index, block_type, content, call_id
        FROM transcript_blocks
        WHERE conversation_id = ?
        ORDER BY step_index ASC, id ASC
    """,
        (cid,),
    ).fetchall()

    TOOL_PALETTE = [
        "#f87171",
        "#fb923c",
        "#fbbf24",
        "#a3e635",
        "#34d399",
        "#22d3ee",
        "#38bdf8",
        "#818cf8",
        "#a78bfa",
        "#e879f9",
        "#f43f5e",
        "#2dd4bf",
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
            current_tool_idx = sum(
                1
                for prev_b in blocks_by_step[idx]
                if prev_b.get("block_type") == "tool_use"
            )
            exec_list = tool_execs_by_step.get(idx, [])
            matched_exec = (
                exec_list[current_tool_idx]
                if current_tool_idx < len(exec_list)
                else None
            )

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
                    raw_content = (b_dict.get("content") or "").strip()
                    parsed_args = {}
                    if raw_content:
                        try:
                            parsed_args = json.loads(raw_content)
                            if isinstance(parsed_args, str):
                                parsed_args = json.loads(parsed_args)
                        except Exception:
                            parsed_args = {}
                    if not isinstance(parsed_args, dict):
                        parsed_args = {}

                    extracted_subagents = []
                    subagents_raw = parsed_args.get("Subagents") or parsed_args.get(
                        "subagents"
                    )
                    if subagents_raw:
                        if isinstance(subagents_raw, str):
                            s = _clean_arg_str(subagents_raw)
                            try:
                                parsed = json.loads(s)
                                if isinstance(parsed, str):
                                    parsed = json.loads(parsed)
                                if isinstance(parsed, list):
                                    extracted_subagents = parsed
                            except Exception:
                                pass
                        elif isinstance(subagents_raw, list):
                            extracted_subagents = subagents_raw

                    if not extracted_subagents:
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
                        if any(k in parsed_args for k in flat_keys):
                            role = _clean_arg_str(
                                parsed_args.get("Role") or parsed_args.get("role") or ""
                            )
                            sub_type = _clean_arg_str(
                                parsed_args.get("TypeName")
                                or parsed_args.get("type")
                                or parsed_args.get("subagent_type")
                                or parsed_args.get("subagentType")
                                or ""
                            )
                            model = _clean_arg_str(
                                parsed_args.get("Model")
                                or parsed_args.get("model")
                                or ""
                            )
                            prompt = _clean_arg_str(
                                parsed_args.get("Prompt")
                                or parsed_args.get("prompt")
                                or ""
                            )
                            extracted_subagents = [
                                {
                                    "role": role or "Autonomous Worker",
                                    "type": sub_type or "self",
                                    "model": model or "inherit",
                                    "prompt": prompt,
                                    "Role": role or "Autonomous Worker",
                                    "TypeName": sub_type or "self",
                                    "Model": model or "inherit",
                                    "Prompt": prompt,
                                }
                            ]
                    b_dict["subagents"] = extracted_subagents
                    b_dict["tool_action"] = parsed_args.get("toolAction")
                    b_dict["tool_summary"] = parsed_args.get("toolSummary")
                except Exception:
                    b_dict["subagents"] = []

            # Skill detection
            raw_c = b_dict.get("content") or ""
            if target_path and ("skills/" in target_path or "SKILL.md" in target_path):
                b_dict["is_skill"] = True
                m = re.search(r"skills/([^/]+)/SKILL\.md", target_path)
                if m:
                    b_dict["skill_name"] = m.group(1)
            elif "skills/" in raw_c:
                m = re.search(r"skills/([^/]+)/SKILL\.md", raw_c)
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
                    "use_block": b_dict,
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
            content_str = b_dict.get("content") or ""
            cid_match = re.search(
                r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
                content_str,
            )
            if cid_match:
                b_dict["subagent_conversation_id"] = cid_match.group(1)

        blocks_by_step[idx].append(b_dict)

    target_tz = get_target_tz(tz)
    quota_state = sync_and_get_quota_state(conn)
    bucket_caps = _extract_bucket_capacities(quota_state)
    activity_profile = get_hourly_activity_profile(conn) if conn is not None else None
    conv_model = conv["model_name"] or "Gemini"

    steps = []
    for r in steps_rows:
        d = dict(r)
        dt = parse_to_local_dt(d["created_at"], target_tz)
        if dt:
            d["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        d["blocks"] = blocks_by_step.get(d["step_index"], [])
        s_cost = d.get("billed_cost") or 0.0
        s_m = d.get("model_name") or conv_model
        s_w_pct, s_5h_pct = _compute_quota_pcts(s_cost, s_m, bucket_caps)
        d["weekly_quota_pct"] = s_w_pct
        d["five_hour_quota_pct"] = s_5h_pct
        steps.append(d)

    cat_rows = cur.execute(
        """
        SELECT category, SUM(est_tokens) as tokens
        FROM steps
        WHERE conversation_id = ?
        GROUP BY category
        ORDER BY tokens DESC
    """,
        (cid,),
    ).fetchall()
    categories = [dict(r) for r in cat_rows]

    phase_rows = cur.execute(
        """
        SELECT phase, SUM(est_tokens) as tokens
        FROM steps
        WHERE conversation_id = ? AND phase IS NOT NULL
        GROUP BY phase
        ORDER BY tokens DESC
    """,
        (cid,),
    ).fetchall()
    session_phases = [dict(r) for r in phase_rows]

    top_consumers = sorted(steps, key=lambda s: s["est_tokens"], reverse=True)[:8]

    conv_dict = dict(conv)
    dt_c = parse_to_local_dt(conv_dict.get("created_at"), target_tz)
    if dt_c:
        conv_dict["created_at"] = dt_c.strftime("%Y-%m-%d %H:%M:%S")

    c_act = (conv_dict.get("billed_input_tokens") or 0) + (
        conv_dict.get("billed_output_tokens") or 0
    )
    conv_dict["tokens"] = c_act if c_act > 0 else (conv_dict.get("total_tokens") or 0)
    conv_dict["total_tokens"] = conv_dict.get("total_tokens") or c_act or 0
    conv_dict["total_steps"] = conv_dict.get("total_steps") or len(steps)
    conv_dict["compaction_count"] = conv_dict.get("compaction_count") or 0
    conv_dict["pruned_tokens"] = conv_dict.get("pruned_tokens") or 0

    c_billed_cost = conv_dict.get("billed_cost") or 0.0
    conv_quota = calculate_quota_metrics(
        billed_cost=c_billed_cost,
        model_name=conv_model,
        quota_state=quota_state,
        activity_profile=activity_profile,
    )
    conv_dict["weekly_quota_pct"] = conv_quota["weekly_pct"]
    conv_dict["five_hour_quota_pct"] = conv_quota["five_hour_pct"]
    conv_dict["quota"] = conv_quota

    s_vel = compute_active_velocity(
        est_tokens=conv_dict.get("total_tokens") or 0,
        billed_tokens=c_act,
        active_agent_seconds=conv_dict.get("active_agent_seconds") or 0,
        active_pairing_seconds=conv_dict.get("active_pairing_seconds") or 0,
    )
    conv_dict["active_context_tpm"] = s_vel["active_context_tpm"]
    conv_dict["active_billed_tpm"] = s_vel["active_billed_tpm"]
    conv_dict["pairing_billed_tph"] = s_vel["pairing_billed_tph"]
    conv_dict["velocity"] = s_vel

    compaction_meta = {
        "count": conv_dict["compaction_count"],
        "pruned_tokens": conv_dict["pruned_tokens"],
        "has_compaction": bool(
            conv_dict["compaction_count"] > 0 or conv_dict["pruned_tokens"] > 0
        ),
    }

    # Ancestor lineage trail
    ancestors = []
    curr_pid = conv_dict.get("parent_id")
    visited_ancestors = set()
    while curr_pid and curr_pid not in visited_ancestors:
        visited_ancestors.add(curr_pid)
        a_row = cur.execute(
            "SELECT id, title, project, model_name, subagent_role, depth, parent_id FROM conversations WHERE id = ?",
            (curr_pid,),
        ).fetchone()
        if a_row:
            a_dict = dict(a_row)
            ancestors.insert(
                0,
                {
                    "id": a_dict["id"],
                    "title": a_dict["title"] or f"Session {a_dict['id'][:8]}",
                    "role": a_dict["subagent_role"] or "Parent",
                    "depth": a_dict["depth"] or 0,
                },
            )
            curr_pid = a_dict.get("parent_id")
        else:
            break

    # Direct children
    ch_rows = cur.execute(
        """
        SELECT id, title, project, model_name, created_at, total_steps, total_tokens,
               billed_cost, billed_input_tokens, billed_output_tokens,
               depth, subagent_role, subagent_type, invoking_step_index
        FROM conversations
        WHERE parent_id = ?
        ORDER BY invoking_step_index ASC, created_at ASC
    """,
        (cid,),
    ).fetchall()
    children = []
    for cr in ch_rows:
        c_dt = parse_to_local_dt(cr["created_at"], target_tz)
        c_act = (cr["billed_input_tokens"] or 0) + (cr["billed_output_tokens"] or 0)
        ch_cost = round(cr["billed_cost"] or 0.0, 2)
        ch_m = cr["model_name"] or "Gemini"
        ch_w_pct, ch_5h_pct = _compute_quota_pcts(ch_cost, ch_m, bucket_caps)
        children.append(
            {
                "id": cr["id"],
                "title": cr["title"] or f"Sub-Agent {cr['id'][:8]}",
                "role": cr["subagent_role"] or "Worker",
                "type": cr["subagent_type"] or "self",
                "depth": cr["depth"] or 1,
                "step_index": cr["invoking_step_index"],
                "steps": cr["total_steps"] or 0,
                "tokens": c_act,
                "cost": ch_cost,
                "weekly_quota_pct": ch_w_pct,
                "five_hour_quota_pct": ch_5h_pct,
                "created_at": c_dt.strftime("%Y-%m-%d %H:%M")
                if c_dt
                else cr["created_at"],
            }
        )

    # Swarm fleet rollup for whole tree
    effective_root = conv_dict.get("root_id") or cid
    try:
        swarm_rows = cur.execute(
            """
            SELECT id, title, parent_id, root_id, depth, subagent_role, subagent_type,
                   total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                   duration_seconds, active_agent_seconds, active_pairing_seconds, model_name, compaction_count, pruned_tokens
            FROM conversations
            WHERE root_id = ? OR id = ?
            ORDER BY depth ASC, created_at ASC
        """,
            (effective_root, effective_root),
        ).fetchall()
    except Exception:
        swarm_rows = cur.execute(
            """
            SELECT id, title, parent_id, root_id, depth, subagent_role, subagent_type,
                   total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                   duration_seconds, active_agent_seconds, active_pairing_seconds, model_name
            FROM conversations
            WHERE root_id = ? OR id = ?
            ORDER BY depth ASC, created_at ASC
        """,
            (effective_root, effective_root),
        ).fetchall()

    swarm_nodes = [dict(sr) for sr in swarm_rows]
    subagents_list = [n for n in swarm_nodes if n.get("id") != conv_dict.get("id")]
    total_swarm_cost = sum(n["billed_cost"] or 0.0 for n in swarm_nodes)
    total_swarm_tokens = sum(
        (n["billed_input_tokens"] or 0) + (n["billed_output_tokens"] or 0)
        for n in swarm_nodes
    )
    total_swarm_steps = sum(n["total_steps"] or 0 for n in swarm_nodes)

    fleet_q = calculate_quota_metrics(
        billed_cost=total_swarm_cost,
        model_name=conv_model,
        quota_state=quota_state,
        activity_profile=activity_profile,
    )
    fleet_vel = compute_active_velocity(
        est_tokens=sum(n.get("total_tokens") or 0 for n in swarm_nodes),
        billed_tokens=total_swarm_tokens,
        active_agent_seconds=round(
            float(conv_dict.get("active_agent_seconds") or 0)
            + sum(float(s.get("active_agent_seconds") or 0) for s in subagents_list),
            1,
        ),
        active_pairing_seconds=round(
            float(conv_dict.get("active_pairing_seconds") or 0)
            + sum(float(s.get("active_pairing_seconds") or 0) for s in subagents_list),
            1,
        ),
    )
    fleet_summary = {
        "root_id": effective_root,
        "is_subagent": bool(conv_dict.get("parent_id")),
        "subagent_count": max(0, len(swarm_nodes) - 1),
        "total_cost": round(total_swarm_cost, 2),
        "total_tokens": total_swarm_tokens,
        "total_steps": total_swarm_steps,
        "total_agent_seconds": round(
            float(conv_dict.get("active_agent_seconds") or 0)
            + sum(float(s.get("active_agent_seconds") or 0) for s in subagents_list),
            1,
        ),
        "total_pairing_seconds": round(
            float(conv_dict.get("active_pairing_seconds") or 0)
            + sum(float(s.get("active_pairing_seconds") or 0) for s in subagents_list),
            1,
        ),
        "weekly_quota_pct": fleet_q["weekly_pct"],
        "five_hour_quota_pct": fleet_q["five_hour_pct"],
        "active_context_tpm": fleet_vel["active_context_tpm"],
        "active_billed_tpm": fleet_vel["active_billed_tpm"],
        "pairing_billed_tph": fleet_vel["pairing_billed_tph"],
        "velocity": fleet_vel,
    }

    # Build nested tree topology for visualization
    nodes_by_id = {}
    for n in swarm_nodes:
        node_id = n["id"]
        node_active_tokens = (n["billed_input_tokens"] or 0) + (
            n["billed_output_tokens"] or 0
        )
        node_cost = round(n["billed_cost"] or 0.0, 2)
        node_model = n.get("model_name") or conv_model
        node_weekly_quota_pct, node_5h_quota_pct = _compute_quota_pcts(
            node_cost, node_model, bucket_caps
        )

        max_tokens = get_model_context_limit(node_model)
        active_tokens = n.get("total_tokens") or 0
        context_pct = (
            round((active_tokens / max_tokens) * 100, 1) if max_tokens else 0.0
        )
        cost_share_pct = (
            round((node_cost / total_swarm_cost * 100), 1)
            if total_swarm_cost > 0
            else 0.0
        )
        token_share_pct = (
            round((node_active_tokens / total_swarm_tokens * 100), 1)
            if total_swarm_tokens > 0
            else 0.0
        )
        n_vel = compute_active_velocity(
            est_tokens=active_tokens,
            billed_tokens=node_active_tokens,
            active_agent_seconds=n.get("active_agent_seconds") or 0.0,
            active_pairing_seconds=n.get("active_pairing_seconds") or 0.0,
        )

        nodes_by_id[node_id] = {
            "id": node_id,
            "title": n["title"] or f"Session {node_id[:8]}",
            "role": n["subagent_role"]
            or ("Root Coordinator" if (n.get("depth") or 0) == 0 else "Worker"),
            "type": n["subagent_type"] or "self",
            "depth": n.get("depth") or 0,
            "steps": n["total_steps"] or 0,
            "tokens": node_active_tokens,
            "total_tokens": active_tokens,
            "cost": node_cost,
            "model_name": node_model,
            "compaction_count": n.get("compaction_count") or 0,
            "pruned_tokens": n.get("pruned_tokens") or 0,
            "active_agent_seconds": round(n.get("active_agent_seconds") or 0.0, 1),
            "active_pairing_seconds": round(n.get("active_pairing_seconds") or 0.0, 1),
            "active_context_tpm": n_vel["active_context_tpm"],
            "active_billed_tpm": n_vel["active_billed_tpm"],
            "pairing_billed_tph": n_vel["pairing_billed_tph"],
            "velocity": n_vel,
            "max_tokens": max_tokens,
            "active_tokens": active_tokens,
            "context_pct": context_pct,
            "weekly_quota_pct": node_weekly_quota_pct,
            "five_hour_quota_pct": node_5h_quota_pct,
            "cost_share_pct": cost_share_pct,
            "token_share_pct": token_share_pct,
            "duration": round(n["duration_seconds"] or 0.0),
            "is_current": (node_id == cid),
            "children": [],
        }

    root_node = nodes_by_id.get(effective_root)
    for n in swarm_nodes:
        n_id = n["id"]
        p_id = n["parent_id"]
        if p_id and p_id in nodes_by_id and n_id != p_id:
            nodes_by_id[p_id]["children"].append(nodes_by_id[n_id])

    fleet_nodes = sorted(
        nodes_by_id.values(),
        key=lambda x: (x.get("cost") or 0.0, x.get("tokens") or 0),
        reverse=True,
    )

    return {
        "conversation": conv_dict,
        "active_context_tpm": s_vel["active_context_tpm"],
        "active_billed_tpm": s_vel["active_billed_tpm"],
        "pairing_billed_tph": s_vel["pairing_billed_tph"],
        "velocity": s_vel,
        "steps": steps,
        "categories": categories,
        "phases": session_phases,
        "top_consumers": top_consumers,
        "ancestors": ancestors,
        "children": children,
        "fleet_summary": fleet_summary,
        "fleet_tree": root_node,
        "fleet_nodes": fleet_nodes,
        "compaction": compaction_meta,
        "quota": conv_quota,
        "weekly_quota_pct": conv_quota["weekly_pct"],
        "five_hour_quota_pct": conv_quota["five_hour_pct"],
    }


def query_turn_diff(
    conn, conv_id: str, step_a: int, step_b: int, cumulative: bool = True, tz=None
):
    from context_monitor.diff import get_turn_diff_from_db

    cur = conn.cursor()
    conv = cur.execute(
        "SELECT id, title, model_name FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv:
        return None

    diff_data = get_turn_diff_from_db(
        conn, conv_id, step_a, step_b, cumulative=cumulative
    )
    target_tz = get_target_tz(tz)
    for step_key in ("step_a", "step_b"):
        if diff_data.get(step_key) and diff_data[step_key].get("created_at"):
            dt = parse_to_local_dt(diff_data[step_key]["created_at"], target_tz)
            if dt:
                diff_data[step_key]["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    diff_data["conversation_title"] = conv["title"] or f"Session {conv['id'][:8]}"
    diff_data["model_name"] = conv["model_name"] or "Gemini"
    return diff_data


def get_analytics_data(
    conn: sqlite3.Connection | None = None,
    db_path: Path | str | None = None,
    tz: Any = None,
    project: str | None = None,
) -> dict[str, Any]:
    """
    Returns aggregated analytics payload including summary metrics,
    dual TPM/TPH token velocity, hourly rollup series, and recent conversations.
    """
    if conn is None:
        from context_monitor.db import get_db_connection

        if db_path is not None:
            with get_db_connection(db_path) as c:
                return get_analytics_data(c, None, tz=tz, project=project)
        else:
            with get_db_connection() as c:
                return get_analytics_data(c, None, tz=tz, project=project)

    target_tz = get_target_tz(tz)
    summary = query_summary_data(conn, tz=target_tz, project=project)
    convs = query_conversations_list(conn, limit=50, tz=target_tz, project=project)
    hourly_series = summary.get("hourly_series", [])

    return {
        "summary": summary,
        "recent_conversations": convs,
        "conversations": convs,
        "hourly_series": hourly_series,
        "token_velocity": summary.get("token_velocity", {}),
    }


def get_conversation_details(
    cid_or_conn: Any,
    cid: str | None = None,
    db_path: Path | str | None = None,
    tz: Any = None,
) -> dict[str, Any] | None:
    """
    Queries session deep-dive details enriched with step quota impacts,
    ancestor lineage, subagent trees, and active TPM/TPH throughput.
    """
    if isinstance(cid_or_conn, sqlite3.Connection):
        conn = cid_or_conn
        target_cid = cid
    else:
        conn = None
        target_cid = str(cid_or_conn) if cid_or_conn is not None else cid

    if target_cid is None:
        return None

    if conn is None:
        from context_monitor.db import get_db_connection

        if db_path is not None:
            with get_db_connection(db_path) as c:
                return get_conversation_details(c, target_cid, None, tz=tz)
        else:
            with get_db_connection() as c:
                return get_conversation_details(c, target_cid, None, tz=tz)

    return query_session_details(conn, target_cid, tz=tz)

