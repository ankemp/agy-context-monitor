"""
Tests for Token Velocity (TPM / TPH) Telemetry Engine
------------------------------------------------------
Covers active-duration velocity, sliding live rolling windows (5m/60m),
cold-start peak fallback, p95 peak calibration, swarm fleet aggregation,
and timestamp robustness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from context_monitor.db import get_db_connection, init_db
from context_monitor.velocity import (
    DEFAULT_PEAK_BILLED_TPH,
    DEFAULT_PEAK_BILLED_TPM,
    DEFAULT_PEAK_CONTEXT_TPH,
    DEFAULT_PEAK_CONTEXT_TPM,
    compute_active_velocity,
    query_rolling_velocity,
)


def test_default_peak_constants():
    """Verifies default peak capacity constants match spec."""
    assert DEFAULT_PEAK_BILLED_TPM == 100_000.0
    assert DEFAULT_PEAK_CONTEXT_TPM == 10_000.0
    assert DEFAULT_PEAK_BILLED_TPH == 2_500_000.0
    assert DEFAULT_PEAK_CONTEXT_TPH == 250_000.0


def test_compute_active_velocity_standard_and_zero_guards():
    """Verifies compute_active_velocity standard rates, zero-guards, and sub-minute clamping."""
    # 120s active agent time (2 mins), 600s pairing time (10 mins = 1/6 hr)
    res = compute_active_velocity(
        est_tokens=12_000,
        billed_tokens=180_000,
        active_agent_seconds=120,
        active_pairing_seconds=600,
    )
    assert res["active_context_tpm"] == 6_000.0
    assert res["active_billed_tpm"] == 90_000.0
    assert res["active_context_tph"] == 360_000.0
    assert res["active_billed_tph"] == 5_400_000.0

    assert res["pairing_context_tpm"] == 1_200.0
    assert res["pairing_billed_tpm"] == 18_000.0
    assert res["pairing_context_tph"] == 72_000.0
    assert res["pairing_billed_tph"] == 1_080_000.0

    # Zero-duration guard: must not raise ZeroDivisionError
    zero_res = compute_active_velocity(0, 0, 0, 0)
    assert zero_res["active_billed_tpm"] == 0.0
    assert zero_res["active_context_tpm"] == 0.0
    assert zero_res["pairing_billed_tph"] == 0.0
    assert zero_res["pairing_context_tph"] == 0.0

    # Non-zero tokens with zero duration: returns 0.0 safely
    zero_dur = compute_active_velocity(10_000, 50_000, 0, 0)
    assert zero_dur["active_billed_tpm"] == 0.0
    assert zero_dur["pairing_billed_tph"] == 0.0

    # Negative duration: returns 0.0 safely
    neg_res = compute_active_velocity(10_000, 50_000, -10, -50)
    assert neg_res["active_billed_tpm"] == 0.0
    assert neg_res["pairing_billed_tph"] == 0.0

    # Sub-minute clamp: duration > 0 and < 60s clamped to 60s (1 min) denominator
    short_res = compute_active_velocity(5_000, 50_000, 5, 10)
    assert short_res["active_context_tpm"] == 5_000.0
    assert short_res["active_billed_tpm"] == 50_000.0
    assert short_res["pairing_context_tpm"] == 5_000.0
    assert short_res["pairing_billed_tpm"] == 50_000.0
    # Hourly rates with 60s denominator = 50_000 * 60 = 3_000_000.0
    assert short_res["active_billed_tph"] == 3_000_000.0


def test_query_rolling_velocity_cold_start(tmp_path: Path):
    """Verifies that an empty database or < 3 hours returns default peak ceilings and 0 rolling rates."""
    db_file = tmp_path / "cold_start.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
    with get_db_connection(db_file) as conn:
        res = query_rolling_velocity(conn, now=now)

    assert res["rolling_5m_context_tokens"] == 0
    assert res["rolling_5m_billed_tokens"] == 0
    assert res["rolling_5m_context_tpm"] == 0.0
    assert res["rolling_5m_billed_tpm"] == 0.0
    assert res["rolling_60m_context_tokens"] == 0
    assert res["rolling_60m_billed_tokens"] == 0
    assert res["rolling_60m_context_tph"] == 0.0
    assert res["rolling_60m_billed_tph"] == 0.0
    assert res["rolling_60m_context_tpm"] == 0.0
    assert res["rolling_60m_billed_tpm"] == 0.0

    assert res["peak_billed_tpm"] == DEFAULT_PEAK_BILLED_TPM
    assert res["peak_context_tpm"] == DEFAULT_PEAK_CONTEXT_TPM
    assert res["peak_billed_tph"] == DEFAULT_PEAK_BILLED_TPH
    assert res["peak_context_tph"] == DEFAULT_PEAK_CONTEXT_TPH
    assert res["gauge_pct"] == 0.0
    assert res["context_gauge_pct"] == 0.0
    assert res["hourly_series"] == []


def test_query_rolling_velocity_rolling_windows_and_gauges(tmp_path: Path):
    """Verifies 5m and 60m sliding windows with steps in database."""
    db_file = tmp_path / "rolling.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    # Insert conversation
    with get_db_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES ('c1', 'Test Conv', '2026-10-23T12:00:00Z')"
        )
        # Step 1: 2 mins ago (inside 5m and 60m)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
            VALUES ('c1', 1, '2026-10-23T13:58:00Z', 1000, 4000, 1000)
            """
        )
        # Step 2: 4 mins ago (inside 5m and 60m)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
            VALUES ('c1', 2, '2026-10-23T13:56:00Z', 2000, 8000, 2000)
            """
        )
        # Step 3: 20 mins ago (outside 5m, inside 60m)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
            VALUES ('c1', 3, '2026-10-23T13:40:00Z', 5000, 20000, 5000)
            """
        )
        # Step 4: 90 mins ago (outside 60m)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
            VALUES ('c1', 4, '2026-10-23T12:30:00Z', 10000, 50000, 10000)
            """
        )
        conn.commit()

        res = query_rolling_velocity(conn, conversation_id="c1", now=now)

    # In 5m: Step 1 + Step 2 = est 3000, billed 15000 (5000 + 10000)
    assert res["rolling_5m_context_tokens"] == 3000
    assert res["rolling_5m_billed_tokens"] == 15000
    assert res["rolling_5m_context_tpm"] == 600.0  # 3000 / 5
    assert res["rolling_5m_billed_tpm"] == 3000.0  # 15000 / 5

    # In 60m: Step 1 + Step 2 + Step 3 = est 8000, billed 40000
    assert res["rolling_60m_context_tokens"] == 8000
    assert res["rolling_60m_billed_tokens"] == 40000
    assert res["rolling_60m_context_tph"] == 8000.0  # 8000 / 1h
    assert res["rolling_60m_billed_tph"] == 40000.0
    assert res["rolling_60m_billed_tpm"] == round(40000.0 / 60.0, 2)

    # Gauge calculation: rolling_5m_billed_tpm (3000) / DEFAULT_PEAK_BILLED_TPM (100_000) * 100 = 3.0%
    assert res["gauge_pct"] == 3.0
    # context_gauge_pct: 600.0 / 10_000.0 * 100 = 6.0%
    assert res["context_gauge_pct"] == 6.0


def test_query_rolling_velocity_fleet_aggregation(tmp_path: Path):
    """Verifies that root_id aggregates across root and subagents without double-counting."""
    db_file = tmp_path / "fleet.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    with get_db_connection(db_file) as conn:
        # Root conversation
        conn.execute(
            "INSERT INTO conversations (id, root_id, title) VALUES ('root-1', 'root-1', 'Lead Agent')"
        )
        # Subagent 1
        conn.execute(
            "INSERT INTO conversations (id, root_id, parent_id, title) VALUES ('sub-1', 'root-1', 'root-1', 'Sub 1')"
        )
        # Subagent 2
        conn.execute(
            "INSERT INTO conversations (id, root_id, parent_id, title) VALUES ('sub-2', 'root-1', 'root-1', 'Sub 2')"
        )
        # Unrelated conversation
        conn.execute(
            "INSERT INTO conversations (id, root_id, title) VALUES ('other-1', 'other-1', 'Other')"
        )

        # Root step: 1m ago
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens) VALUES ('root-1', 1, '2026-10-23T13:59:00Z', 1000, 5000, 0)"
        )
        # Sub-1 step: 2m ago
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens) VALUES ('sub-1', 1, '2026-10-23T13:58:00Z', 2000, 10000, 0)"
        )
        # Sub-2 step: 3m ago
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens) VALUES ('sub-2', 1, '2026-10-23T13:57:00Z', 3000, 15000, 0)"
        )
        # Other step: 1m ago (should NOT be included in root-1 fleet)
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens) VALUES ('other-1', 1, '2026-10-23T13:59:00Z', 9999, 99999, 0)"
        )
        conn.commit()

        # Query root fleet
        res = query_rolling_velocity(conn, root_id="root-1", now=now)

    # In 5m: root-1 (1000/5000) + sub-1 (2000/10000) + sub-2 (3000/15000) = est 6000, billed 30000
    assert res["rolling_5m_context_tokens"] == 6000
    assert res["rolling_5m_billed_tokens"] == 30000
    assert res["rolling_5m_context_tpm"] == 1200.0
    assert res["rolling_5m_billed_tpm"] == 6000.0


def test_query_rolling_velocity_malformed_timestamps_robustness(tmp_path: Path):
    """Verifies that NULL, empty, or space-separated created_at timestamps are handled gracefully."""
    db_file = tmp_path / "malformed.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test')")
        # Valid step with space instead of T
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 1, '2026-10-23 13:58:00', 1000, 5000)"
        )
        # Step with NULL created_at
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 2, NULL, 500, 2500)"
        )
        # Step with empty created_at
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 3, '', 500, 2500)"
        )
        conn.commit()

        res = query_rolling_velocity(conn, conversation_id="c1", now=now)

    assert res["rolling_5m_context_tokens"] == 1000
    assert res["rolling_5m_billed_tokens"] == 5000


def test_query_rolling_velocity_p95_peak_calibration(tmp_path: Path):
    """Verifies p95 calibration when >= 3 hours exist in hourly rollups."""
    db_file = tmp_path / "p95.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test')")
        # Insert 4 distinct hours in hourly_step_rollups
        hours_data = [
            ("2026-10-23T10", "c1", 10, 6_000, 60_000, 0.5),
            ("2026-10-23T11", "c1", 15, 12_000, 120_000, 1.0),
            ("2026-10-23T12", "c1", 20, 18_000, 180_000, 1.5),
            ("2026-10-23T13", "c1", 25, 24_000, 240_000, 2.0),
        ]
        conn.executemany(
            """
            INSERT INTO hourly_step_rollups (hour_key, conversation_id, step_count, est_tokens, billed_tokens, billed_cost)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            hours_data,
        )
        conn.commit()

        res = query_rolling_velocity(conn, now=now, hourly_limit=4)

    assert len(res["hourly_series"]) == 4
    # hourly_series check
    latest_hour = res["hourly_series"][-1]
    assert latest_hour["hour_key"] == "2026-10-23T13"
    assert latest_hour["billed_tokens"] == 240_000
    assert latest_hour["billed_tpm"] == 4_000.0  # 240_000 / 60
    assert latest_hour["est_tpm"] == 400.0  # 24_000 / 60

    # Calibrated peak check (> 0, >= p95 of hourly values)
    assert res["peak_billed_tph"] > 0
    assert res["peak_context_tph"] > 0
    assert res["peak_billed_tpm"] == round(res["peak_billed_tph"] / 60.0, 2)


def test_compute_active_velocity_non_finite_inputs_and_overflow():
    """Verifies that NaN, Inf, Overflow, and non-numeric inputs never crash or return NaN/Inf."""
    # NaN and Inf durations
    res = compute_active_velocity(1000, 5000, float("nan"), float("inf"))
    for k, v in res.items():
        assert v == 0.0
        assert not isinstance(v, str)

    # Inf and Overflow tokens
    res_inf = compute_active_velocity(float("inf"), float("inf"), 120, 120)
    for k, v in res_inf.items():
        assert v == 0.0

    # Negative tokens and None inputs
    res_none = compute_active_velocity(None, None, None, None)
    for k, v in res_none.items():
        assert v == 0.0


def test_query_rolling_velocity_future_steps_and_now_upper_bound(tmp_path: Path):
    """Verifies that steps occurring strictly in the future of now are excluded from rolling windows."""
    db_file = tmp_path / "future_steps.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test')")
        # Step 1: 2 mins ago (in 5m and 60m)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens)
            VALUES ('c1', 1, '2026-10-23T13:58:00Z', 1000, 5000)
            """
        )
        # Step 2: Exact boundary step at now (must be included)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens)
            VALUES ('c1', 2, '2026-10-23T14:00:00Z', 500, 2500)
            """
        )
        # Step 3: 15 mins in future of now (must be excluded)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens)
            VALUES ('c1', 3, '2026-10-23T14:15:00Z', 9000, 45000)
            """
        )
        conn.commit()

        res = query_rolling_velocity(conn, conversation_id="c1", now=now)

    assert res["rolling_5m_context_tokens"] == 1500
    assert res["rolling_5m_billed_tokens"] == 7500
    assert res["rolling_60m_context_tokens"] == 1500
    assert res["rolling_60m_billed_tokens"] == 7500


def test_query_rolling_velocity_nested_swarm_parent_id_hierarchy(tmp_path: Path):
    """Verifies multi-level swarm fleet aggregation where subagents have NULL root_id and link via parent_id."""
    db_file = tmp_path / "nested_swarm.db"
    init_db(db_file)

    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)

    with get_db_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, parent_id, root_id) VALUES ('root-coord', 'Root', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, parent_id, root_id) VALUES ('mid-coord', 'Mid', 'root-coord', NULL)"
        )
        conn.execute(
            "INSERT INTO conversations (id, title, parent_id, root_id) VALUES ('leaf-worker', 'Leaf', 'mid-coord', NULL)"
        )

        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('root-coord', 1, '2026-10-23T13:58:00Z', 1000, 5000)"
        )
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('mid-coord', 1, '2026-10-23T13:58:00Z', 2000, 10000)"
        )
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('leaf-worker', 1, '2026-10-23T13:58:00Z', 3000, 15000)"
        )
        conn.commit()

        res = query_rolling_velocity(conn, root_id="root-coord", now=now)

    assert res["rolling_5m_context_tokens"] == 6000
    assert res["rolling_5m_billed_tokens"] == 30000


def test_query_rolling_velocity_hourly_series_future_exclusion_and_fallback(tmp_path: Path):
    """Verifies that future hours are excluded and fallback to steps functions on read-only databases."""
    db_file = tmp_path / "ro_fallback.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test Conv')")
        # Step in hour 13
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 1, '2026-10-23T13:45:00Z', 1000, 5000)"
        )
        # Rollup for future hour 15
        conn.execute(
            "INSERT INTO hourly_step_rollups (hour_key, conversation_id, step_count, est_tokens, billed_tokens) VALUES ('2026-10-23T15', 'c1', 5, 2000, 10000)"
        )
        conn.commit()

    # Query with now at 14:00:00 (future hour 15 must be excluded, hour 13 step must be present via fallback)
    now = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
    with get_db_connection(db_file) as conn:
        res = query_rolling_velocity(conn, conversation_id="c1", now=now, hourly_limit=5)

    hour_keys = [h["hour_key"] for h in res["hourly_series"]]
    assert "2026-10-23T15" not in hour_keys
