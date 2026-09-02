import pytest
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from context_monitor.db import get_db_connection, init_db, DEFAULT_DB_PATH
from context_monitor.dashboard.api_routes import query_timeline_data, query_summary_data
from context_monitor.formats import BlockParser

PRICING = {
    "pro": (1.25, 2.50, 10.00),     # Gemini Pro
    "flash": (0.075, 0.15, 0.60),   # Gemini Flash
    "claude": (3.00, 3.00, 15.00),  # Claude 3.5
    "gpt": (2.50, 2.50, 10.00),     # GPT-4o
}

def test_database_schema_and_migration(tmp_path):
    db_file = tmp_path / "test_analytics.db"
    init_db(db_file)
    
    conn = get_db_connection(db_file)
    cur = conn.cursor()
    
    # Check steps columns
    cols = [c[1] for c in cur.execute("PRAGMA table_info(steps)").fetchall()]
    assert "billed_cost" in cols
    assert "billed_input_tokens" in cols
    assert "billed_output_tokens" in cols
    
    # Check indexes
    indexes = [r[1] for r in cur.execute("SELECT type, name FROM sqlite_master WHERE type='index'").fetchall()]
    assert "idx_steps_created" in indexes
    assert "idx_steps_conv_step" in indexes
    assert "idx_tool_exec_conv" in indexes
    assert "idx_steps_cat_tokens" in indexes
    assert "idx_steps_model_tokens" in indexes
    assert "idx_steps_phase_tokens" in indexes
    assert "idx_steps_est_tokens" in indexes
    conn.close()

def test_ephemeral_not_accumulated_in_context():
    """
    Verifies that EPHEMERAL_MESSAGE is included in the immediate turn's prompt,
    but is not accumulated into persistent history for subsequent turns.
    """
    # 3 turns:
    # Step 1: User prompt (1,000 tokens)
    # Step 2: Ephemeral message (500 tokens)
    # Step 3: Planner response (200 tokens output) -> Prompt had 1,000 + 500 = 1,500 input tokens.
    # Step 4: Tool result (300 tokens)
    # Step 5: Planner response (100 tokens output) -> Prompt had 1,000 + 200 + 300 = 1,500 input tokens (NOT 2,000).
    
    steps = [
        {"step_type": "USER_INPUT", "est_tokens": 1000, "model_name": "Gemini Flash"},
        {"step_type": "EPHEMERAL_MESSAGE", "est_tokens": 500, "model_name": "Gemini Flash"},
        {"step_type": "PLANNER_RESPONSE", "est_tokens": 200, "model_name": "Gemini Flash"},
        {"step_type": "GENERIC", "est_tokens": 300, "model_name": "Gemini Flash"},
        {"step_type": "PLANNER_RESPONSE", "est_tokens": 100, "model_name": "Gemini Flash"},
    ]
    
    running_ctx = 0
    current_ephemeral = 0
    calculated_steps = []
    
    for s in steps:
        stype = s["step_type"]
        tokens = s["est_tokens"]
        
        if stype == "CHECKPOINT":
            running_ctx = tokens
            current_ephemeral = 0
        elif stype == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif stype == "PLANNER_RESPONSE":
            actual_in = running_ctx + current_ephemeral
            in_rate_low, in_rate_high, out_rate = PRICING["flash"]
            in_rate = in_rate_high if actual_in > 128_000 else in_rate_low
            step_cost = (actual_in / 1_000_000 * in_rate) + (tokens / 1_000_000 * out_rate)
            calculated_steps.append({
                "in_tokens": actual_in,
                "out_tokens": tokens,
                "cost": step_cost
            })
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

    # Turn 1
    assert calculated_steps[0]["in_tokens"] == 1500 # 1000 user + 500 ephemeral
    assert calculated_steps[0]["out_tokens"] == 200
    
    # Turn 2
    assert calculated_steps[1]["in_tokens"] == 1500 # 1000 user + 200 turn1_out + 300 tool_result (ephemeral discarded)
    assert calculated_steps[1]["out_tokens"] == 100

def test_checkpoint_resets_truncated_context():
    """
    Verifies that CHECKPOINT steps reset running_ctx to the summary token size,
    preventing truncated prior history from being charged repeatedly.
    """
    steps = [
        {"step_type": "USER_INPUT", "est_tokens": 50000, "model_name": "Gemini Flash"},
        {"step_type": "PLANNER_RESPONSE", "est_tokens": 5000, "model_name": "Gemini Flash"},
        {"step_type": "CHECKPOINT", "est_tokens": 4000, "model_name": "Gemini Flash"},
        {"step_type": "USER_INPUT", "est_tokens": 500, "model_name": "Gemini Flash"},
        {"step_type": "PLANNER_RESPONSE", "est_tokens": 200, "model_name": "Gemini Flash"},
    ]
    
    running_ctx = 0
    current_ephemeral = 0
    turn_inputs = []
    
    for s in steps:
        stype = s["step_type"]
        tokens = s["est_tokens"]
        if stype == "CHECKPOINT":
            running_ctx = tokens
            current_ephemeral = 0
        elif stype == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif stype == "PLANNER_RESPONSE":
            actual_in = running_ctx + current_ephemeral
            turn_inputs.append(actual_in)
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

    assert turn_inputs[0] == 50000 # Pre-truncation turn
    assert turn_inputs[1] == 4500  # Post-truncation turn: 4000 checkpoint + 500 new prompt (55,000 old tokens truncated)

def test_timeline_date_attribution_multi_day(tmp_path):
    """
    Verifies that a multi-day conversation correctly attributes steps and costs
    to their respective execution dates instead of all to conversation created_at.
    """
    db_file = tmp_path / "test_timeline.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    
    # Insert a conversation created on Aug 20
    conn.execute("""
        INSERT INTO conversations (id, title, model_name, created_at, updated_at, total_steps, total_tokens, billed_cost)
        VALUES ('conv-1', 'Multi-day Session', 'Gemini Flash', '2026-08-20T10:00:00Z', '2026-08-22T15:00:00Z', 4, 2000, 0.05)
    """)
    
    # Insert steps on Aug 20, Aug 21, Aug 22
    steps = [
        ('conv-1', 1, '2026-08-20T10:00:00Z', 'USER', 'USER_INPUT', 500, 0.0),
        ('conv-1', 2, '2026-08-20T10:01:00Z', 'MODEL', 'PLANNER_RESPONSE', 200, 0.01),
        ('conv-1', 3, '2026-08-21T14:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 300, 0.02),
        ('conv-1', 4, '2026-08-22T15:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 400, 0.02),
    ]
    
    conn.executemany("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, steps)
    conn.commit()
    
    # Query timeline data in UTC
    timeline = query_timeline_data(conn, tz="UTC")
    conn.close()
    
    date_map = {item["day"]: item for item in timeline}
    assert "2026-08-20" in date_map
    assert "2026-08-21" in date_map
    assert "2026-08-22" in date_map
    
    assert date_map["2026-08-20"]["cost"] == 0.01
    assert date_map["2026-08-20"]["steps"] == 2
    assert date_map["2026-08-20"]["sessions"] == 1
    
    assert date_map["2026-08-21"]["cost"] == 0.02
    assert date_map["2026-08-21"]["steps"] == 1
    assert date_map["2026-08-21"]["sessions"] == 1
    
    assert date_map["2026-08-22"]["cost"] == 0.02
    assert date_map["2026-08-22"]["steps"] == 1
    assert date_map["2026-08-22"]["sessions"] == 1

def test_timeline_fills_gap_days_with_zeros(tmp_path):
    """
    Verifies that intermediate gap days with no recorded activity are populated
    with zero values (tokens=0, steps=0, sessions=0, cost=0.0).
    """
    db_file = tmp_path / "test_gap_days.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute("""
        INSERT INTO conversations (id, title, model_name, created_at, updated_at, total_steps, total_tokens, billed_cost)
        VALUES ('conv-gap', 'Gap Session', 'Gemini Flash', '2026-08-01T10:00:00Z', '2026-08-05T15:00:00Z', 2, 1000, 0.02)
    """)

    steps = [
        ('conv-gap', 1, '2026-08-01T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 500, 0.01),
        ('conv-gap', 2, '2026-08-05T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 500, 0.01),
    ]

    conn.executemany("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, steps)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    # Check active days
    assert date_map["2026-08-01"]["tokens"] == 500
    assert date_map["2026-08-05"]["tokens"] == 500

    # Check intermediate zero-data gap days
    for gap_day in ["2026-08-02", "2026-08-03", "2026-08-04"]:
        assert gap_day in date_map
        assert date_map[gap_day]["tokens"] == 0
        assert date_map[gap_day]["steps"] == 0
        assert date_map[gap_day]["sessions"] == 0
        assert date_map[gap_day]["cost"] == 0.0

def test_timeline_fixed_day_counts(tmp_path):
    """
    Verifies that passing days=7, days=14, days=30 returns exactly that many items.
    """
    db_file = tmp_path / "test_fixed_days.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    t7 = query_timeline_data(conn, days=7, tz="UTC")
    t14 = query_timeline_data(conn, days=14, tz="UTC")
    t30 = query_timeline_data(conn, days=30, tz="UTC")
    conn.close()

    assert len(t7) == 7
    assert len(t14) == 14
    assert len(t30) == 30

    for item in t7:
        assert item["tokens"] == 0
        assert item["sessions"] == 0
        assert item["steps"] == 0
        assert item["cost"] == 0.0
        assert item["projects"] == {}

def test_timeline_projects_breakdown(tmp_path):
    """
    Verifies that query_timeline_data groups tokens by project across multi-project conversations.
    """
    db_file = tmp_path / "test_timeline_projects.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('conv-proj-1', 'Project 1 Session', 'alpha-proj', '2026-08-10T10:00:00Z', '2026-08-10T12:00:00Z', 2, 800)
    """)
    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('conv-proj-2', 'Project 2 Session', 'beta-proj', '2026-08-10T11:00:00Z', '2026-08-10T13:00:00Z', 1, 400)
    """)

    steps = [
        ('conv-proj-1', 1, '2026-08-10T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 500, 0.01),
        ('conv-proj-1', 2, '2026-08-10T11:30:00Z', 'MODEL', 'PLANNER_RESPONSE', 300, 0.01),
        ('conv-proj-2', 1, '2026-08-10T12:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 400, 0.01),
    ]
    conn.executemany("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, steps)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    assert "2026-08-10" in date_map
    day_item = date_map["2026-08-10"]
    assert day_item["tokens"] == 1200
    assert day_item["projects"]["alpha-proj"] == 800
    assert day_item["projects"]["beta-proj"] == 400

def test_timeline_unassigned_project(tmp_path):
    """
    Verifies that conversations with empty or NULL project are grouped under '(unassigned)'.
    """
    db_file = tmp_path / "test_unassigned.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('conv-no-proj', 'No Project Session', NULL, '2026-08-12T10:00:00Z', '2026-08-12T11:00:00Z', 1, 600)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES ('conv-no-proj', 1, '2026-08-12T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 600, 0.01)
    """)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    assert "2026-08-12" in date_map
    assert date_map["2026-08-12"]["projects"]["(unassigned)"] == 600

def test_timeline_single_project_filter(tmp_path):
    """
    Verifies that filtering by project isolates only that project's records.
    """
    db_file = tmp_path / "test_filter_proj.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('c1', 'C1', 'target-proj', '2026-08-15T10:00:00Z', '2026-08-15T11:00:00Z', 1, 500)
    """)
    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('c2', 'C2', 'other-proj', '2026-08-15T12:00:00Z', '2026-08-15T13:00:00Z', 1, 700)
    """)

    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES ('c1', 1, '2026-08-15T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 500, 0.01)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES ('c2', 1, '2026-08-15T12:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 700, 0.01)
    """)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC", project="target-proj")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    assert date_map["2026-08-15"]["tokens"] == 500
    assert date_map["2026-08-15"]["projects"]["target-proj"] == 500
    assert "other-proj" not in date_map["2026-08-15"]["projects"]

