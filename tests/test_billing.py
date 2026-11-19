import sqlite3

import pytest

from context_monitor.dashboard.api_routes import query_timeline_data
from context_monitor.db import get_db_connection, init_db
from context_monitor.pricing import PRICING


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
    indexes = [
        r[1]
        for r in cur.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    ]
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
        {
            "step_type": "EPHEMERAL_MESSAGE",
            "est_tokens": 500,
            "model_name": "Gemini Flash",
        },
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 200,
            "model_name": "Gemini Flash",
        },
        {"step_type": "GENERIC", "est_tokens": 300, "model_name": "Gemini Flash"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 100,
            "model_name": "Gemini Flash",
        },
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
            step_cost = (actual_in / 1_000_000 * in_rate) + (
                tokens / 1_000_000 * out_rate
            )
            calculated_steps.append(
                {"in_tokens": actual_in, "out_tokens": tokens, "cost": step_cost}
            )
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

    # Turn 1
    assert calculated_steps[0]["in_tokens"] == 1500  # 1000 user + 500 ephemeral
    assert calculated_steps[0]["out_tokens"] == 200

    # Turn 2
    assert (
        calculated_steps[1]["in_tokens"] == 1500
    )  # 1000 user + 200 turn1_out + 300 tool_result (ephemeral discarded)
    assert calculated_steps[1]["out_tokens"] == 100


def test_checkpoint_resets_truncated_context():
    """
    Verifies that CHECKPOINT steps reset running_ctx to the summary token size,
    preventing truncated prior history from being charged repeatedly.
    """
    steps = [
        {"step_type": "USER_INPUT", "est_tokens": 50000, "model_name": "Gemini Flash"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 5000,
            "model_name": "Gemini Flash",
        },
        {"step_type": "CHECKPOINT", "est_tokens": 4000, "model_name": "Gemini Flash"},
        {"step_type": "USER_INPUT", "est_tokens": 500, "model_name": "Gemini Flash"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 200,
            "model_name": "Gemini Flash",
        },
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

    assert turn_inputs[0] == 50000  # Pre-truncation turn
    assert (
        turn_inputs[1] == 4500
    )  # Post-truncation turn: 4000 checkpoint + 500 new prompt (55,000 old tokens truncated)


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
        ("conv-1", 1, "2026-08-20T10:00:00Z", "USER", "USER_INPUT", 500, 0.0),
        ("conv-1", 2, "2026-08-20T10:01:00Z", "MODEL", "PLANNER_RESPONSE", 200, 0.01),
        ("conv-1", 3, "2026-08-21T14:00:00Z", "MODEL", "PLANNER_RESPONSE", 300, 0.02),
        ("conv-1", 4, "2026-08-22T15:00:00Z", "MODEL", "PLANNER_RESPONSE", 400, 0.02),
    ]

    conn.executemany(
        """
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        steps,
    )
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
        (
            "conv-gap",
            1,
            "2026-08-01T10:00:00Z",
            "MODEL",
            "PLANNER_RESPONSE",
            500,
            0.01,
            450,
            50,
        ),
        (
            "conv-gap",
            2,
            "2026-08-05T10:00:00Z",
            "MODEL",
            "PLANNER_RESPONSE",
            500,
            0.01,
            450,
            50,
        ),
    ]

    conn.executemany(
        """
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        steps,
    )
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
        (
            "conv-proj-1",
            1,
            "2026-08-10T10:00:00Z",
            "MODEL",
            "PLANNER_RESPONSE",
            500,
            0.01,
            400,
            100,
        ),
        (
            "conv-proj-1",
            2,
            "2026-08-10T11:30:00Z",
            "MODEL",
            "PLANNER_RESPONSE",
            300,
            0.01,
            200,
            100,
        ),
        (
            "conv-proj-2",
            1,
            "2026-08-10T12:00:00Z",
            "MODEL",
            "PLANNER_RESPONSE",
            400,
            0.01,
            350,
            50,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        steps,
    )
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
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES ('conv-no-proj', 1, '2026-08-12T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 600, 0.01, 500, 100)
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
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES ('c1', 1, '2026-08-15T10:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 500, 0.01, 450, 50)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES ('c2', 1, '2026-08-15T12:00:00Z', 'MODEL', 'PLANNER_RESPONSE', 700, 0.01, 600, 100)
    """)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC", project="target-proj")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    assert date_map["2026-08-15"]["tokens"] == 500
    assert date_map["2026-08-15"]["projects"]["target-proj"] == 500
    assert "other-proj" not in date_map["2026-08-15"]["projects"]


def test_timeline_actual_consumption_multi_turn(tmp_path):
    """
    Verifies that query_timeline_data reflects cumulative actual consumption
    (billed_input_tokens + billed_output_tokens) across multi-turn sessions rather than est_tokens.
    """
    db_file = tmp_path / "test_actual_consumption.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute("""
        INSERT INTO conversations (id, title, project, created_at, updated_at, total_steps, total_tokens)
        VALUES ('conv-mt', 'Multi-turn Conv', 'my-repo', '2026-08-20T10:00:00Z', '2026-08-20T10:30:00Z', 3, 300)
    """)

    # In this session:
    # Step 1: User prompt (on-disk 100 tokens, 0 billed tokens)
    # Step 2: Planner response (on-disk 100 tokens, prompt in: 1,000, completion out: 100 -> 1,100 actual tokens)
    # Step 3: Tool response + Planner response (on-disk 100 tokens, prompt in: 1,500, completion out: 200 -> 1,700 actual tokens)
    # Total on disk est_tokens = 300
    # Total actual consumption = 1,100 + 1,700 = 2,800
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES 
        ('conv-mt', 1, '2026-08-20T10:05:00Z', 'USER_INPUT', 'USER_INPUT', 100, 0.0, 0, 0),
        ('conv-mt', 2, '2026-08-20T10:10:00Z', 'MODEL', 'PLANNER_RESPONSE', 100, 0.005, 1000, 100),
        ('conv-mt', 3, '2026-08-20T10:20:00Z', 'MODEL', 'PLANNER_RESPONSE', 100, 0.008, 1500, 200)
    """)
    conn.commit()

    timeline = query_timeline_data(conn, tz="UTC")
    conn.close()

    date_map = {item["day"]: item for item in timeline}
    assert "2026-08-20" in date_map
    day_item = date_map["2026-08-20"]
    # Verify that tokens reflect actual API consumption (2,800), NOT on-disk est_tokens (300)
    assert day_item["tokens"] == 2800
    assert day_item["projects"]["my-repo"] == 2800


def test_argon_pricing_and_context_limit():
    from context_monitor.pricing import (
        PRICING,
        calculate_step_cost,
        get_model_context_limit,
        get_pricing_key,
    )

    assert "argon" in PRICING
    assert PRICING["argon"] == (2.00, 2.00, 10.00)

    for name in [
        "Gemini 4 Argon (High)",
        "Gemini 4 Argon (Medium)",
        "Gemini 4 Argon (Low)",
        "gemini-4-argon-high",
        "Gemini 4 Argo (High)",
    ]:
        assert get_pricing_key(name) == "argon"
        assert get_model_context_limit(name) == 1_000_000

    # 100k input tokens + 2k output tokens on Argon:
    # (100_000 / 1M * 2.00) + (2_000 / 1M * 10.00) = 0.20 + 0.02 = 0.22
    cost, in_tok, out_tok = calculate_step_cost(
        100_000, 0, 2_000, "Gemini 4 Argon (High)"
    )
    assert cost == pytest.approx(0.22)
    assert in_tok == 100_000
    assert out_tok == 2_000


def test_models_used_column_migration(tmp_path):
    from context_monitor.db import get_db_connection, init_db

    db_file = tmp_path / "test_migration.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        cols = [
            c[1] for c in conn.execute("PRAGMA table_info(conversations)").fetchall()
        ]
        assert "models_used" in cols


def test_tool_output_does_not_trigger_model_switch(tmp_path):
    """Verifies that viewing a file or running a shell command containing 'Model Selection' does NOT alter the model."""
    import json

    from scripts.ingest import analyze_transcript

    t_file = tmp_path / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 3.8 Flash (High).\n</USER_SETTINGS_CHANGE>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "",
            "tool_calls": [
                {"name": "view_file", "args": {"AbsolutePath": "/some/log.jsonl"}}
            ],
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "content": 'File Path: /some/log.jsonl\n{"Model Selection": "Gemini 4 Argon (High)"}',
        },
        {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Done viewing file."},
    ]
    with open(t_file, "w") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)

    res = analyze_transcript(t_file)
    assert res["latest_model"] == "Gemini 3.8 Flash (High)"
    assert res["models_used"] == ["Gemini 3.8 Flash (High)"]
    assert res["steps"][3]["model_name"] == "Gemini 3.8 Flash (High)"


def test_legitimate_multi_model_thread_pricing(tmp_path):
    """Verifies that a real mid-session model switch prices each step under its active model and records all models used."""
    import json

    from scripts.ingest import analyze_transcript

    t_file = tmp_path / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "Argon response",
            "est_tokens": 500,
        },
        {
            "step_index": 2,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 4 Argon (High) to Gemini 3.8 Flash (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "content": "Flash response",
            "est_tokens": 500,
        },
    ]
    with open(t_file, "w") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)

    res = analyze_transcript(t_file)
    assert res["models_used"] == ["Gemini 4 Argon (High)", "Gemini 3.8 Flash (High)"]
    assert res["steps"][1]["model_name"] == "Gemini 4 Argon (High)"
    assert res["steps"][3]["model_name"] == "Gemini 3.8 Flash (High)"
    # Step 1 (Argon): input 1000 @ $2.00/M, output 500 @ $10.00/M = 0.002 + 0.005 = 0.007
    assert res["steps"][1]["billed_cost"] == pytest.approx(0.007)
    # Step 3 (Flash): input (1000+500+1000=2500) @ $0.075/M, output 500 @ $0.60/M = 0.0001875 + 0.0003 = 0.0004875
    assert res["steps"][3]["billed_cost"] == pytest.approx(0.0004875)


def test_extract_model_switch_variations():
    from context_monitor.pricing import extract_model_switch

    assert (
        extract_model_switch(
            "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (High). No need to comment on this change if the user doesn't ask about it.\n</USER_SETTINGS_CHANGE>"
        )
        == "Gemini 4 Argon (High)"
    )
    assert (
        extract_model_switch(
            "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 3.8 Flash (High)</USER_SETTINGS_CHANGE>"
        )
        == "Gemini 3.8 Flash (High)"
    )
    assert (
        extract_model_switch(
            "The user changed setting Model Selection from None to Gemini 4 Argon (Medium)."
        )
        == "Gemini 4 Argon (Medium)"
    )
    assert (
        extract_model_switch(
            "The user changed setting `Model Selection` from None to 'Gemini 4 Argo (Low)'."
        )
        == "Gemini 4 Argo (Low)"
    )
    assert extract_model_switch('{"Model Selection": "Gemini 4 Argon (High)"}') is None
    assert extract_model_switch("") is None

    # Prompt text in USER_REQUEST should NOT override settings change
    prompt_with_quote = """<USER_REQUEST>
Why did "The user changed setting `Model Selection` from None to Gemini 4 Argon (High)" fail?
</USER_REQUEST>
<USER_SETTINGS_CHANGE>
The user changed setting `Model Selection` from None to Gemini 3.8 Flash (High). No need to comment...
</USER_SETTINGS_CHANGE>"""
    assert extract_model_switch(prompt_with_quote) == "Gemini 3.8 Flash (High)"


def test_leading_system_message_does_not_poison_models_used(tmp_path):
    """Verifies that transcripts starting with SYSTEM_MESSAGE (e.g. subagents) do not falsely prepend default model."""
    import json

    from scripts.ingest import analyze_transcript

    t_file = tmp_path / "subagent_transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "SYSTEM_MESSAGE",
            "content": "Initializing subagent environment task.",
        },
        {
            "step_index": 1,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "content": "I am working on Argon",
            "est_tokens": 500,
        },
    ]
    with open(t_file, "w") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)

    res = analyze_transcript(t_file)
    assert res["latest_model"] == "Gemini 4 Argon (High)"
    assert res["models_used"] == ["Gemini 4 Argon (High)"]
    assert res["steps"][0]["model_name"] == "Gemini 4 Argon (High)"
    assert res["steps"][2]["model_name"] == "Gemini 4 Argon (High)"


def test_multi_model_transitions_sequence_and_switch_back(tmp_path):
    """Verifies that switching from Argon -> Flash -> Argon is accurately tracked in model_transitions and model_name."""
    import json

    from context_monitor.db import get_db_connection, init_db
    from scripts.ingest import analyze_transcript, sync_to_sqlite

    t_file = tmp_path / "multi_switch.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Argon (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "Argon phase 1",
            "est_tokens": 500,
        },
        {
            "step_index": 2,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 4 Argon (High) to Gemini 3.8 Flash (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "content": "Flash phase",
            "est_tokens": 500,
        },
        {
            "step_index": 4,
            "type": "USER_INPUT",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 3.8 Flash (High) to Gemini 4 Argon (High).\n</USER_SETTINGS_CHANGE>",
            "est_tokens": 1000,
        },
        {
            "step_index": 5,
            "type": "PLANNER_RESPONSE",
            "content": "Argon phase 2",
            "est_tokens": 500,
        },
    ]
    with open(t_file, "w") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)

    res = analyze_transcript(t_file)
    assert res["latest_model"] == "Gemini 4 Argon (High)"
    assert res["models_used"] == ["Gemini 4 Argon (High)", "Gemini 3.8 Flash (High)"]
    assert res["model_transitions"] == [
        "Gemini 4 Argon (High)",
        "Gemini 3.8 Flash (High)",
        "Gemini 4 Argon (High)",
    ]

    # Test sync_to_sqlite
    test_db = tmp_path / "test_multi.db"
    init_db(test_db)
    import scripts.ingest as ingest_mod

    orig_db = ingest_mod.DEFAULT_DB_PATH
    try:
        ingest_mod.DEFAULT_DB_PATH = test_db
        sync_to_sqlite("conv-123", res["latest_model"], res)
    finally:
        ingest_mod.DEFAULT_DB_PATH = orig_db

    with get_db_connection(test_db) as conn:
        row = conn.execute(
            "SELECT model_name, models_used FROM conversations WHERE id = 'conv-123'"
        ).fetchone()
        assert (
            row["model_name"]
            == "Gemini 4 Argon (High) → Gemini 3.8 Flash (High) → Gemini 4 Argon (High)"
        )
        assert row["models_used"] == "Gemini 4 Argon (High), Gemini 3.8 Flash (High)"


def test_models_used_backfill_migration_in_init_db(tmp_path):
    """Verifies that init_db() automatically populates models_used for existing unmigrated conversations."""
    db_file = tmp_path / "test_legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT,
            model_name TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE steps (
            conversation_id TEXT,
            step_index INTEGER,
            model_name TEXT,
            PRIMARY KEY(conversation_id, step_index)
        );
    """)
    conn.execute("INSERT INTO conversations VALUES ('c1', 'Conv 1', 'Gemini Flash');")
    conn.execute(
        "INSERT INTO conversations VALUES ('c2', 'Conv 2', 'Gemini 3.8 Flash (High)');"
    )
    conn.execute("INSERT INTO steps VALUES ('c1', 0, 'Gemini Flash');")
    conn.execute("INSERT INTO steps VALUES ('c2', 0, 'Gemini 4 Argon (High)');")
    conn.execute("INSERT INTO steps VALUES ('c2', 1, 'Gemini 3.8 Flash (High)');")
    conn.commit()
    conn.close()

    # Run init_db on the legacy DB
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        r1 = conn.execute(
            "SELECT model_name, models_used FROM conversations WHERE id = 'c1'"
        ).fetchone()
        assert r1["models_used"] == "Gemini Flash"

        r2 = conn.execute(
            "SELECT model_name, models_used FROM conversations WHERE id = 'c2'"
        ).fetchone()
        assert "Gemini 4 Argon (High)" in r2["models_used"]
        assert "Gemini 3.8 Flash (High)" in r2["models_used"]


def test_carbon_hydrogen_pricing_and_context_limits():
    from context_monitor.pricing import (
        CACHED_INPUT_PRICING,
        PRICING,
        calculate_step_cost,
        get_model_context_limit,
        get_pricing_key,
        normalize_native_model_name,
        resolve_subagent_model,
    )

    assert PRICING["carbon"] == (2.00, 2.00, 10.00)
    assert PRICING["argon"] == (2.00, 2.00, 10.00)
    assert PRICING["hydrogen"] == (1.50, 1.50, 7.50)
    assert CACHED_INPUT_PRICING["carbon"] == pytest.approx(0.10)
    assert CACHED_INPUT_PRICING["argon"] == pytest.approx(0.10)
    assert CACHED_INPUT_PRICING["hydrogen"] == pytest.approx(0.075)

    for name in [
        "Gemini 4 Carbon (Medium)",
        "Gemini 4 Carbon (Low)",
        "gemini-4-carbon",
        "Orchestrator (Carbon)",
        "gemini-pro-orchestrator",
    ]:
        assert get_pricing_key(name) == "carbon"
        assert get_model_context_limit(name) == 1_000_000

    for name in [
        "Gemini 4 Hydrogen (High)",
        "Gemini 4 Hydrogen (Medium)",
        "Gemini 4 Hydrogen (XHigh)",
        "gemini-4-hydrogen",
    ]:
        assert get_pricing_key(name) == "hydrogen"
        assert get_model_context_limit(name) == 1_000_000

    # Hydrogen: 100k uncached + 200k cached + 10k output (flat across 1M context)
    # 100k * 1.50/M (0.15) + 200k * 0.075/M (0.015) + 10k * 7.50/M (0.075) = 0.24
    cost_h, in_h, out_h = calculate_step_cost(
        300_000,
        0,
        10_000,
        "Gemini 4 Hydrogen (High)",
        cached_tokens=200_000,
    )
    assert cost_h == pytest.approx(0.24)
    assert in_h == 300_000
    assert out_h == 10_000

    # Carbon introductory vs post-Dec 1, 2026 standard pricing
    cost_c_intro, _, _ = calculate_step_cost(
        100_000,
        0,
        10_000,
        "Gemini 4 Carbon (Medium)",
        cached_tokens=50_000,
        created_at="2026-11-16T10:00:00Z",
    )
    # 50k * 2.00/M (0.10) + 50k * 0.10/M (0.005) + 10k * 10.00/M (0.10) = 0.205
    assert cost_c_intro == pytest.approx(0.205)

    cost_c_std, _, _ = calculate_step_cost(
        100_000,
        0,
        10_000,
        "Gemini 4 Carbon (Medium)",
        cached_tokens=50_000,
        created_at="2026-12-01T00:00:00Z",
    )
    # 50k * 4.00/M (0.20) + 50k * 0.20/M (0.010) + 10k * 20.00/M (0.20) = 0.410
    assert cost_c_std == pytest.approx(0.410)

    # Native placeholder & model ID normalization + subagent tiers
    assert (
        normalize_native_model_name("gemini-4-carbon", "", "MODEL_PLACEHOLDER_M438")
        == "Gemini 4 Carbon (Low)"
    )
    assert (
        normalize_native_model_name("gemini-4-hydrogen", "", "MODEL_PLACEHOLDER_M528")
        == "Gemini 4 Hydrogen (XHigh)"
    )
    assert (
        normalize_native_model_name("gemini-pro-orchestrator", "", "MODEL_PLACEHOLDER_M667")
        == "Orchestrator (Carbon)"
    )
    assert resolve_subagent_model("hydrogen", "Gemini 4 Carbon (Medium)") == "Gemini 4 Hydrogen (High)"
    assert resolve_subagent_model("carbon", "Gemini 4 Hydrogen (High)") == "Gemini 4 Carbon (Medium)"


def test_neon_pricing_and_context_limits():
    from context_monitor.pricing import (
        CACHED_INPUT_PRICING,
        PRICING,
        calculate_step_cost,
        get_model_context_limit,
        get_pricing_key,
        normalize_native_model_name,
        recompute_step_costs,
        resolve_subagent_model,
    )

    assert PRICING["neon"] == (3.00, 3.00, 15.00)
    assert CACHED_INPUT_PRICING["neon"] == pytest.approx(0.15)

    for name in [
        "Gemini 4 Neon (High)",
        "Gemini 4 Neon (Medium)",
        "Gemini 4 Neon (Low)",
        "Gemini 4 Neon (XHigh)",
        "gemini-4-neon",
    ]:
        assert get_pricing_key(name) == "neon"
        assert get_model_context_limit(name) == 1_000_000

    # Neon: 100k uncached + 200k cached + 10k output (flat across 1M context)
    # 100k * 3.00/M (0.30) + 200k * 0.15/M (0.03) + 10k * 15.00/M (0.15) = 0.48
    cost_n, in_n, out_n = calculate_step_cost(
        300_000,
        0,
        10_000,
        "Gemini 4 Neon (High)",
        cached_tokens=200_000,
        created_at="2026-12-15T10:00:00Z",
    )
    assert cost_n == pytest.approx(0.48)
    assert in_n == 300_000
    assert out_n == 10_000

    assert normalize_native_model_name("gemini-4-neon", "", "") == "Gemini 4 Neon (High)"
    assert (
        normalize_native_model_name("gemini-4-neon", "Gemini 4 Neon (Medium)", "")
        == "Gemini 4 Neon (Medium)"
    )
    assert resolve_subagent_model("neon", "Gemini 4 Carbon (Medium)") == "Gemini 4 Neon (High)"

    steps = [
        {"step_type": "USER_INPUT", "est_tokens": 10_000, "created_at": "2026-11-18T15:00:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Neon (High)",
            "created_at": "2026-11-18T15:00:10Z",
        },
        {"step_type": "GENERIC", "est_tokens": 2_000, "created_at": "2026-11-18T15:00:20Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Neon (High)",
            "created_at": "2026-11-18T15:01:00Z",  # 50s later -> cache warm (11k cached, 2k uncached)
        },
    ]
    recompute_step_costs(steps, "Gemini 4 Neon (High)")
    # Step 1: 10k * 3.00/M (0.03) + 1k * 15.00/M (0.015) = 0.045
    assert steps[1]["billed_cost"] == pytest.approx(0.045)
    # Step 3 (warm): 11k * 0.15/M (0.00165) + 2k * 3.00/M (0.006) + 1k * 15.00/M (0.015) = 0.02265
    assert steps[3]["billed_cost"] == pytest.approx(0.02265)


def test_orchestrator_neon_and_hydrogen_pricing_and_cache_boundary():
    from context_monitor.pricing import (
        get_pricing_key,
        normalize_native_model_name,
        recompute_step_costs,
    )

    assert get_pricing_key("Orchestrator (Neon)") == "neon"
    assert get_pricing_key("gemini-pro-orchestrator-neon") == "neon"
    assert get_pricing_key("Orchestrator (Argon)") == "argon"
    assert get_pricing_key("gemini-pro-orchestrator-argon") == "argon"
    assert get_pricing_key("Gemini 4 Hydrogen (Orchestrator)") == "hydrogen"
    assert get_pricing_key("Orchestrator (Carbon)") == "carbon"
    assert get_pricing_key("gemini-pro-orchestrator") == "carbon"
    assert get_pricing_key("Orchestrator") == "carbon"

    assert (
        normalize_native_model_name("gemini-pro-orchestrator-neon", "", "MODEL_PLACEHOLDER_M537")
        == "Orchestrator (Neon)"
    )
    assert (
        normalize_native_model_name("gemini-4-neon", "", "MODEL_PLACEHOLDER_M533")
        == "Gemini 4 Neon (Medium)"
    )
    assert (
        normalize_native_model_name("gemini-pro-orchestrator-argon", "", "")
        == "Orchestrator (Argon)"
    )
    assert (
        normalize_native_model_name(
            "gemini-flash-worker-agy",
            "Gemini 4 Hydrogen (Orchestrator)",
            "MODEL_PLACEHOLDER_M669",
        )
        == "Gemini 4 Hydrogen (Orchestrator)"
    )
    assert (
        normalize_native_model_name("gemini-flash-worker-agy", "", "MODEL_PLACEHOLDER_M669")
        == "Gemini Flash"
    )

    # Fallback (non-native token) Neon -> Hydrogen switch within 300s must NOT apply cached discount across models
    steps = [
        {"step_type": "USER_INPUT", "est_tokens": 100_000, "created_at": "2026-11-18T17:00:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Orchestrator (Neon)",
            "created_at": "2026-11-18T17:00:10Z",
        },
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Hydrogen (Orchestrator)",
            "created_at": "2026-11-18T17:00:20Z",
        },
    ]
    res = recompute_step_costs(steps, "Orchestrator (Neon)")
    # Step 1 (Neon): 100k * $3/M ($0.30) + 1k * $15/M ($0.015) = $0.315
    # Step 2 (Hydrogen, uncached because pkey switched neon -> hydrogen): 101k * $1.50/M ($0.1515) + 1k * $7.50/M ($0.0075) = $0.159
    assert steps[1]["billed_cost"] == pytest.approx(0.315)
    assert steps[2]["billed_cost"] == pytest.approx(0.159)
    assert res["billed_cost"] == pytest.approx(0.474)

    # Empty/None step model_name must fall back to active_model rather than resolving to flash
    fallback_steps = [
        {"step_type": "USER_INPUT", "est_tokens": 100_000, "created_at": "2026-11-18T17:00:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": None,
            "created_at": "2026-11-18T17:00:10Z",
        },
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "",
            "created_at": "2026-11-18T17:00:20Z",
        },
    ]
    fallback_res = recompute_step_costs(fallback_steps, "Orchestrator (Neon)")
    assert fallback_steps[1]["billed_cost"] == pytest.approx(0.315)
    # Step 2 (warm cache on Neon): 101k * $0.15/M ($0.01515) + 1k * $15/M ($0.015) = $0.03015
    assert fallback_steps[2]["billed_cost"] == pytest.approx(0.03015)
    assert fallback_res["billed_cost"] == pytest.approx(0.34515)


def test_recompute_step_costs_native_tokens_and_implicit_cache_ttl():
    from context_monitor.pricing import recompute_step_costs

    # 1. Native token telemetry (input_tokens, cache_read_tokens, output_tokens) + trailing step
    native_steps = [
        {"step_type": "USER_INPUT", "est_tokens": 500, "created_at": "2026-11-16T10:00:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 400,
            "input_tokens": 20_000,
            "cache_read_tokens": 0,
            "output_tokens": 1_000,
            "model_name": "Gemini 4 Hydrogen (High)",
            "created_at": "2026-11-16T10:00:05Z",
        },
        {"step_type": "GENERIC", "est_tokens": 1_500, "created_at": "2026-11-16T10:00:10Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 300,
            "input_tokens": 2_000,
            "cache_read_tokens": 21_000,
            "output_tokens": 500,
            "model_name": "Gemini 4 Hydrogen (High)",
            "created_at": "2026-11-16T10:00:15Z",
        },
        {"step_type": "GENERIC", "est_tokens": 200, "created_at": "2026-11-16T10:00:20Z"},
    ]
    res_native = recompute_step_costs(native_steps, "Gemini 4 Hydrogen (High)")
    # Step 1: 20k * 1.50/M (0.03) + 1k * 7.50/M (0.0075) = 0.0375
    assert native_steps[1]["billed_cost"] == pytest.approx(0.0375)
    assert native_steps[1]["billed_input_tokens"] == 20_000
    assert native_steps[1]["billed_output_tokens"] == 1_000
    # Step 3: 2k * 1.50/M (0.003) + 21k * 0.075/M (0.001575) + 500 * 7.50/M (0.00375) = 0.008325
    assert native_steps[3]["billed_cost"] == pytest.approx(0.008325)
    assert native_steps[3]["billed_input_tokens"] == 23_000
    assert native_steps[3]["billed_output_tokens"] == 500
    # Active context: (2000 + 21000 + 500) + trailing 200 = 23,700
    assert res_native["active_context_tokens"] == 23_700

    # 2. Fallback implicit KV cache estimation (warm within 300s, cold after >300s, reset on CHECKPOINT)
    fallback_steps = [
        {"step_type": "USER_INPUT", "est_tokens": 10_000, "created_at": "2026-11-16T10:00:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Carbon (Low)",
            "created_at": "2026-11-16T10:00:10Z",
        },
        {"step_type": "GENERIC", "est_tokens": 2_000, "created_at": "2026-11-16T10:00:20Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Carbon (Low)",
            "created_at": "2026-11-16T10:01:00Z",  # 50s later -> cache warm (11k cached, 2k fresh)
        },
        {"step_type": "GENERIC", "est_tokens": 1_000, "created_at": "2026-11-16T10:10:00Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 1_000,
            "model_name": "Gemini 4 Carbon (Low)",
            "created_at": "2026-11-16T10:10:10Z",  # >300s after 10:01:00 -> cache expired (15k uncached)
        },
        {"step_type": "CHECKPOINT", "est_tokens": 5_000, "created_at": "2026-11-16T10:10:20Z"},
        {
            "step_type": "PLANNER_RESPONSE",
            "est_tokens": 500,
            "model_name": "Gemini 4 Carbon (Low)",
            "created_at": "2026-11-16T10:10:30Z",  # right after CHECKPOINT -> cache invalidated (5k uncached)
        },
    ]
    recompute_step_costs(fallback_steps, "Gemini 4 Carbon (Low)")
    # Step 1: 10k * 2.00/M (0.02) + 1k * 10.00/M (0.01) = 0.03
    assert fallback_steps[1]["billed_cost"] == pytest.approx(0.03)
    # Step 3 (warm): 11k * 0.10/M (0.0011) + 2k * 2.00/M (0.004) + 1k * 10.00/M (0.01) = 0.0151
    assert fallback_steps[3]["billed_cost"] == pytest.approx(0.0151)
    # Step 5 (expired >300s): 15k * 2.00/M (0.03) + 1k * 10.00/M (0.01) = 0.04
    assert fallback_steps[5]["billed_cost"] == pytest.approx(0.04)
    # Step 7 (post-checkpoint): 5k * 2.00/M (0.01) + 500 * 10.00/M (0.005) = 0.015
    assert fallback_steps[7]["billed_cost"] == pytest.approx(0.015)


def test_ingest_and_backfill_parity_with_native_gemini4_tokens(tmp_path):
    import json
    from scripts import backfill_analytics, ingest

    t_dir = tmp_path / "conv-g4"
    logs_dir = t_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    tx_path = logs_dir / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "source": "USER",
            "created_at": "2026-11-16T10:00:00Z",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Hydrogen (High).\n</USER_SETTINGS_CHANGE>\nHello",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "created_at": "2026-11-16T10:00:05Z",
            "content": "Hi!",
            "input_tokens": 20_000,
            "cache_read_tokens": 0,
            "output_tokens": 1_000,
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "source": "SYSTEM",
            "created_at": "2026-11-16T10:00:08Z",
            "content": "Tool output here",
        },
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "created_at": "2026-11-16T10:00:12Z",
            "content": "Done.",
            "input_tokens": 2_000,
            "cache_read_tokens": 21_000,
            "output_tokens": 500,
        },
    ]
    tx_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    ing = ingest.analyze_transcript(t_dir)
    bf = backfill_analytics.parse_conversation(t_dir)
    assert bf is not None

    assert ing["latest_model"] == "Gemini 4 Hydrogen (High)"
    assert ing["latest_max_tokens"] == 1_000_000
    assert ing["active_context_tokens"] == 23_500
    assert ing["billed_cost"] == pytest.approx(0.0375 + 0.008325)
    assert bf["conversation"]["billed_cost"] == pytest.approx(ing["billed_cost"])
    assert bf["conversation"]["billed_input_tokens"] == ing["billed_input_tokens"]
    assert bf["conversation"]["billed_output_tokens"] == ing["billed_output_tokens"]
    assert bf["conversation"]["active_context_tokens"] == ing["active_context_tokens"]
