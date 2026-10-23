"""
Token Velocity (TPM / TPH) Telemetry Engine
--------------------------------------------
Computes real-time Tokens-Per-Minute (TPM) and Tokens-Per-Hour (TPH) metrics
across live rolling windows (5m / 60m), active agent/pairing sessions, and
historical 24h/7d hourly rollups with p95 peak calibration.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "DEFAULT_PEAK_BILLED_TPM",
    "DEFAULT_PEAK_CONTEXT_TPM",
    "DEFAULT_PEAK_BILLED_TPH",
    "DEFAULT_PEAK_CONTEXT_TPH",
    "compute_active_velocity",
    "query_rolling_velocity",
]

DEFAULT_PEAK_BILLED_TPM: float = 100_000.0
DEFAULT_PEAK_CONTEXT_TPM: float = 10_000.0
DEFAULT_PEAK_BILLED_TPH: float = 2_500_000.0
DEFAULT_PEAK_CONTEXT_TPH: float = 250_000.0


def compute_active_velocity(
    est_tokens: int,
    billed_tokens: int,
    active_agent_seconds: int | float,
    active_pairing_seconds: int | float,
) -> dict[str, float]:
    """
    Computes active agent and pairing velocity (TPM and TPH) for net context and billed tokens.

    Clamps positive durations under 60s to a 60.0s denominator to prevent inflated
    spikes or ZeroDivisionError. Returns 0.0 for non-positive or non-finite inputs.
    """
    def _clean_tokens(val: Any) -> int:
        if val is None:
            return 0
        try:
            f = float(val)
            if math.isnan(f) or math.isinf(f) or f <= 0:
                return 0
            return max(0, int(f))
        except (ValueError, TypeError, OverflowError):
            return 0

    est_tok = _clean_tokens(est_tokens)
    billed_tok = _clean_tokens(billed_tokens)

    def _calc_rates(tokens: int, sec: int | float) -> tuple[float, float]:
        if tokens <= 0 or sec is None:
            return 0.0, 0.0
        try:
            s = float(sec)
            if math.isnan(s) or math.isinf(s) or s <= 0.0:
                return 0.0, 0.0
        except (ValueError, TypeError, OverflowError):
            return 0.0, 0.0

        eff_sec = max(s, 60.0)
        tpm = round((tokens / eff_sec) * 60.0, 2)
        tph = round((tokens / eff_sec) * 3600.0, 2)
        return tpm, tph

    act_ctx_tpm, act_ctx_tph = _calc_rates(est_tok, active_agent_seconds)
    act_bld_tpm, act_bld_tph = _calc_rates(billed_tok, active_agent_seconds)
    pr_ctx_tpm, pr_ctx_tph = _calc_rates(est_tok, active_pairing_seconds)
    pr_bld_tpm, pr_bld_tph = _calc_rates(billed_tok, active_pairing_seconds)

    return {
        "active_context_tpm": act_ctx_tpm,
        "active_context_tph": act_ctx_tph,
        "active_billed_tpm": act_bld_tpm,
        "active_billed_tph": act_bld_tph,
        "pairing_context_tpm": pr_ctx_tpm,
        "pairing_context_tph": pr_ctx_tph,
        "pairing_billed_tpm": pr_bld_tpm,
        "pairing_billed_tph": pr_bld_tph,
    }


def _calc_p95(vals: list[float]) -> float:
    """Calculates linear-interpolated 95th percentile from a list of floats."""
    if not vals:
        return 0.0
    k = (len(vals) - 1) * 0.95
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(vals[int(k)])
    d0 = vals[int(f)] * (c - k)
    d1 = vals[int(c)] * (k - f)
    return float(d0 + d1)


def _resolve_fleet_ids(conn: sqlite3.Connection, root_id: str) -> list[str]:
    """Resolves all conversation IDs in the swarm fleet hierarchy."""
    try:
        cur = conn.execute(
            """
            WITH RECURSIVE fleet(id) AS (
                SELECT id FROM conversations WHERE id = ? OR root_id = ? OR parent_id = ?
                UNION
                SELECT c.id FROM conversations c JOIN fleet f ON c.parent_id = f.id OR c.root_id = f.id
            )
            SELECT DISTINCT id FROM fleet
            """,
            (root_id, root_id, root_id),
        )
        ids = [str(row[0]) for row in cur.fetchall() if row[0]]
        if ids:
            return ids
    except sqlite3.OperationalError:
        pass

    try:
        cur = conn.execute(
            "SELECT DISTINCT id FROM conversations WHERE root_id = ? OR id = ?",
            (root_id, root_id),
        )
        ids = [str(row[0]) for row in cur.fetchall() if row[0]]
        if ids:
            return ids
    except Exception:
        pass

    return [root_id]


def query_rolling_velocity(
    conn: sqlite3.Connection,
    conversation_id: str | None = None,
    root_id: str | None = None,
    now: datetime | None = None,
    hourly_limit: int = 24,
) -> dict[str, Any]:
    """
    Queries live rolling window (5m / 60m) token throughput, gauges, and hourly rollups.

    Supports scoping by conversation_id or root_id (fleet aggregation across recursive hierarchy),
    calibrates peak capacities against historical hourly rollups (falling back to defaults
    under cold start), bounds queries strictly to elapsed timestamps up to `now`,
    and returns static gauge percentages clamped to [0.0, 100.0].
    """
    try:
        cur_chk = conn.execute(
            "SELECT 1 FROM steps WHERE created_at IS NOT NULL AND created_at != '' LIMIT 1"
        )
        if cur_chk.fetchone():
            from context_monitor.db import ensure_rollups_synced

            ensure_rollups_synced(conn)
    except Exception:
        pass

    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now_dt = now.replace(tzinfo=timezone.utc)
    else:
        now_dt = now.astimezone(timezone.utc)

    t_5m = now_dt - timedelta(minutes=5)
    t_60m = now_dt - timedelta(minutes=60)

    iso_now = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    iso_5m = t_5m.strftime("%Y-%m-%dT%H:%M:%S")
    iso_60m = t_60m.strftime("%Y-%m-%dT%H:%M:%S")
    now_hour = now_dt.strftime("%Y-%m-%dT%H")

    # Build scoping clause for steps and rollups
    scope_steps_sql = ""
    scope_steps_params: list[Any] = []
    scope_rollups_sql = ""
    scope_rollups_params: list[Any] = []

    if conversation_id:
        scope_steps_sql = " AND conversation_id = ?"
        scope_steps_params.append(conversation_id)
        scope_rollups_sql = " AND conversation_id = ?"
        scope_rollups_params.append(conversation_id)
    elif root_id:
        fleet_ids = _resolve_fleet_ids(conn, root_id)
        placeholders = ",".join("?" for _ in fleet_ids)
        scope_steps_sql = f" AND conversation_id IN ({placeholders})"
        scope_steps_params.extend(fleet_ids)
        scope_rollups_sql = f" AND conversation_id IN ({placeholders})"
        scope_rollups_params.extend(fleet_ids)

    # 1. Query live rolling 5m and 60m token metrics from steps (strictly bounded <= iso_now)
    rolling_5m_context = 0
    rolling_5m_billed = 0
    rolling_60m_context = 0
    rolling_60m_billed = 0

    try:
        rolling_sql = f"""
            SELECT
                COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN est_tokens ELSE 0 END), 0) AS c_5m,
                COALESCE(SUM(CASE WHEN substr(replace(created_at, ' ', 'T'), 1, 19) >= ? THEN (COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)) ELSE 0 END), 0) AS b_5m,
                COALESCE(SUM(est_tokens), 0) AS c_60m,
                COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS b_60m
            FROM steps
            WHERE created_at IS NOT NULL AND created_at != ''
              AND substr(replace(created_at, ' ', 'T'), 1, 19) >= ?
              AND substr(replace(created_at, ' ', 'T'), 1, 19) <= ?
              {scope_steps_sql}
        """
        params = [iso_5m, iso_5m, iso_60m, iso_now] + scope_steps_params
        cur = conn.execute(rolling_sql, params)
        row = cur.fetchone()
        if row:
            rolling_5m_context = int(row[0] or 0)
            rolling_5m_billed = int(row[1] or 0)
            rolling_60m_context = int(row[2] or 0)
            rolling_60m_billed = int(row[3] or 0)
    except Exception:
        pass

    # Rates
    rolling_5m_context_tpm = round(rolling_5m_context / 5.0, 2)
    rolling_5m_billed_tpm = round(rolling_5m_billed / 5.0, 2)
    rolling_60m_context_tph = round(float(rolling_60m_context), 2)
    rolling_60m_billed_tph = round(float(rolling_60m_billed), 2)
    rolling_60m_context_tpm = round(rolling_60m_context / 60.0, 2)
    rolling_60m_billed_tpm = round(rolling_60m_billed / 60.0, 2)

    # 2. Query peak calibration from hourly_step_rollups (across entire DB up to now_hour)
    distinct_hours: list[tuple[float, float]] = []
    try:
        cur = conn.execute(
            """
            SELECT
                SUM(est_tokens) AS est_tok,
                SUM(billed_tokens) AS bld_tok
            FROM hourly_step_rollups
            WHERE hour_key IS NOT NULL AND hour_key != ''
              AND hour_key <= ?
            GROUP BY hour_key
            """,
            (now_hour,),
        )
        distinct_hours = [(float(r[0] or 0.0), float(r[1] or 0.0)) for r in cur.fetchall()]
    except Exception:
        distinct_hours = []

    # Fallback to steps if hourly_step_rollups has fewer than 3 hours
    if len(distinct_hours) < 3:
        try:
            cur = conn.execute(
                """
                SELECT
                    COALESCE(SUM(est_tokens), 0) AS est_tok,
                    COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS bld_tok
                FROM steps
                WHERE created_at IS NOT NULL AND created_at != ''
                  AND substr(replace(created_at, ' ', 'T'), 1, 13) <= ?
                GROUP BY substr(replace(created_at, ' ', 'T'), 1, 13)
                """,
                (now_hour,),
            )
            distinct_hours = [(float(r[0] or 0.0), float(r[1] or 0.0)) for r in cur.fetchall()]
        except Exception:
            distinct_hours = []

    if len(distinct_hours) < 3:
        peak_billed_tpm = DEFAULT_PEAK_BILLED_TPM
        peak_context_tpm = DEFAULT_PEAK_CONTEXT_TPM
        peak_billed_tph = DEFAULT_PEAK_BILLED_TPH
        peak_context_tph = DEFAULT_PEAK_CONTEXT_TPH
    else:
        billed_hourly = sorted(r[1] for r in distinct_hours)
        context_hourly = sorted(r[0] for r in distinct_hours)
        p95_bld = _calc_p95(billed_hourly)
        p95_ctx = _calc_p95(context_hourly)

        peak_billed_tph = round(p95_bld, 2) if p95_bld > 0.0 else DEFAULT_PEAK_BILLED_TPH
        peak_billed_tpm = round(peak_billed_tph / 60.0, 2) if p95_bld > 0.0 else DEFAULT_PEAK_BILLED_TPM

        peak_context_tph = round(p95_ctx, 2) if p95_ctx > 0.0 else DEFAULT_PEAK_CONTEXT_TPH
        peak_context_tpm = round(peak_context_tph / 60.0, 2) if p95_ctx > 0.0 else DEFAULT_PEAK_CONTEXT_TPM

    # Gauges (clamped to [0.0, 100.0])
    if peak_billed_tpm > 0.0:
        gauge_pct = round(max(0.0, min(100.0, (rolling_5m_billed_tpm / peak_billed_tpm) * 100.0)), 2)
    else:
        gauge_pct = 0.0

    if peak_context_tpm > 0.0:
        context_gauge_pct = round(
            max(0.0, min(100.0, (rolling_5m_context_tpm / peak_context_tpm) * 100.0)), 2
        )
    else:
        context_gauge_pct = 0.0

    # 3. Query hourly_series (scoped, chronologically ordered ASC up to hourly_limit and now_hour)
    hourly_series: list[dict[str, Any]] = []
    eff_limit = max(1, int(hourly_limit or 24))
    try:
        series_sql = f"""
            SELECT
                hour_key,
                SUM(step_count) AS step_count,
                SUM(est_tokens) AS est_tokens,
                SUM(billed_tokens) AS billed_tokens,
                SUM(billed_cost) AS billed_cost
            FROM hourly_step_rollups
            WHERE hour_key IS NOT NULL AND hour_key != ''
              AND hour_key <= ?
              {scope_rollups_sql}
            GROUP BY hour_key
            ORDER BY hour_key DESC
            LIMIT ?
        """
        cur = conn.execute(series_sql, [now_hour] + scope_rollups_params + [eff_limit])
        rows = cur.fetchall()
        for r in reversed(rows):
            h_key = str(r[0])
            s_cnt = int(r[1] or 0)
            e_tok = int(r[2] or 0)
            b_tok = int(r[3] or 0)
            b_cst = round(float(r[4] or 0.0), 4)
            hourly_series.append(
                {
                    "hour_key": h_key,
                    "step_count": s_cnt,
                    "est_tokens": e_tok,
                    "billed_tokens": b_tok,
                    "billed_cost": b_cst,
                    "est_tpm": round(e_tok / 60.0, 2),
                    "billed_tpm": round(b_tok / 60.0, 2),
                }
            )
    except Exception:
        hourly_series = []

    # Fallback to steps if hourly_series is empty (e.g. read-only DB or unrolled conversation)
    if not hourly_series:
        try:
            steps_series_sql = f"""
                SELECT
                    substr(replace(created_at, ' ', 'T'), 1, 13) AS hour_key,
                    COUNT(*) AS step_count,
                    COALESCE(SUM(est_tokens), 0) AS est_tokens,
                    COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS billed_tokens,
                    COALESCE(SUM(COALESCE(billed_cost, 0.0)), 0.0) AS billed_cost
                FROM steps
                WHERE created_at IS NOT NULL AND created_at != ''
                  AND substr(replace(created_at, ' ', 'T'), 1, 13) <= ?
                  {scope_steps_sql}
                GROUP BY hour_key
                ORDER BY hour_key DESC
                LIMIT ?
            """
            cur = conn.execute(steps_series_sql, [now_hour] + scope_steps_params + [eff_limit])
            rows = cur.fetchall()
            for r in reversed(rows):
                h_key = str(r[0])
                s_cnt = int(r[1] or 0)
                e_tok = int(r[2] or 0)
                b_tok = int(r[3] or 0)
                b_cst = round(float(r[4] or 0.0), 4)
                hourly_series.append(
                    {
                        "hour_key": h_key,
                        "step_count": s_cnt,
                        "est_tokens": e_tok,
                        "billed_tokens": b_tok,
                        "billed_cost": b_cst,
                        "est_tpm": round(e_tok / 60.0, 2),
                        "billed_tpm": round(b_tok / 60.0, 2),
                    }
                )
        except Exception:
            hourly_series = []

    return {
        "rolling_5m_context_tokens": rolling_5m_context,
        "rolling_5m_billed_tokens": rolling_5m_billed,
        "rolling_5m_context_tpm": rolling_5m_context_tpm,
        "rolling_5m_billed_tpm": rolling_5m_billed_tpm,
        "rolling_60m_context_tokens": rolling_60m_context,
        "rolling_60m_billed_tokens": rolling_60m_billed,
        "rolling_60m_context_tph": rolling_60m_context_tph,
        "rolling_60m_billed_tph": rolling_60m_billed_tph,
        "rolling_60m_context_tpm": rolling_60m_context_tpm,
        "rolling_60m_billed_tpm": rolling_60m_billed_tpm,
        "peak_billed_tpm": peak_billed_tpm,
        "peak_context_tpm": peak_context_tpm,
        "peak_billed_tph": peak_billed_tph,
        "peak_context_tph": peak_context_tph,
        "gauge_pct": gauge_pct,
        "context_gauge_pct": context_gauge_pct,
        "hourly_series": hourly_series,
    }
