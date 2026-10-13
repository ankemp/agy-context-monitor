import json
import subprocess
import sqlite3
from pathlib import Path
import pytest

def test_sidecar_query_logic(tmp_path):
    # Create a temporary SQLite database mimicking analytics.db
    db_path = tmp_path / "test_analytics.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        project TEXT,
        model_name TEXT,
        created_at TEXT,
        updated_at TEXT,
        total_steps INTEGER DEFAULT 0,
        total_characters INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        duration_seconds REAL DEFAULT 0.0,
        active_agent_seconds REAL DEFAULT 0.0,
        active_pairing_seconds REAL DEFAULT 0.0,
        thinking_tokens INTEGER DEFAULT 0,
        user_prompts INTEGER DEFAULT 0,
        tool_calls_count INTEGER DEFAULT 0,
        redundant_file_reads INTEGER DEFAULT 0,
        lines_written INTEGER DEFAULT 0,
        lines_edited INTEGER DEFAULT 0,
        failed_tool_calls INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        parent_id TEXT,
        root_id TEXT,
        depth INTEGER DEFAULT 0,
        subagent_role TEXT,
        subagent_type TEXT,
        invoking_step_index INTEGER,
        compaction_count INTEGER DEFAULT 0,
        pruned_tokens INTEGER DEFAULT 0
    );

    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        created_at TEXT,
        source TEXT,
        step_type TEXT,
        category TEXT,
        activity TEXT,
        tool_or_source TEXT,
        characters INTEGER DEFAULT 0,
        est_tokens INTEGER DEFAULT 0,
        thinking_tokens INTEGER DEFAULT 0,
        phase TEXT,
        model_name TEXT,
        detail TEXT,
        billed_cost REAL DEFAULT 0.0,
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        UNIQUE(conversation_id, step_index)
    );
    """)

    cur.execute("""
    INSERT INTO conversations (
        id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
        billed_cost, compaction_count, pruned_tokens
    ) VALUES (
        'conv-123', 'Test Conversation', 'test-proj', 'Gemini 3.7 Flash',
        '2026-10-09T10:00:00Z', '2026-10-09T10:05:00Z', 5, 25000, 0.42, 1, 10000
    )
    """)

    # Older step before checkpoint
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, detail)
    VALUES ('conv-123', 1, 'USER_INPUT', 'User Requests', 'User Prompt', 'User', 1000, 'Init prompt')
    """)

    # Checkpoint step at index 2
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, detail)
    VALUES ('conv-123', 2, 'CHECKPOINT', 'Session Summaries (Checkpoint)', 'Session Summary', 'System', 2000, 'Compaction summary')
    """)

    # Active steps after checkpoint
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, detail)
    VALUES ('conv-123', 3, 'PLANNER_RESPONSE', 'Agent Reasoning & Text', 'Reasoning', 'Planner', 3000, 'Agent thought')
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, detail)
    VALUES ('conv-123', 4, 'TOOL_RESULT', 'Tool Output: File Contents (`view_file`)', 'File Content', 'view_file', 5000, 'File: main.py')
    """)
    conn.commit()
    conn.close()

    # Invoke node script importing query.mjs
    node_code = f"""
    import {{ openDatabase, getSessionData, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'conv-123');
    const globalData = getGlobalData(db);
    console.log(JSON.stringify({{ session, globalData }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    session = data["session"]
    assert session["cid"] == "conv-123"
    assert session["model_name"] == "Gemini 3.7 Flash"
    assert session["max_tokens"] == 1000000
    # Active tokens should start from checkpoint (2000 + 3000 + 5000 = 10000)
    assert session["active_tokens"] == 10000
    assert session["compaction_count"] == 1
    assert session["pruned_tokens"] == 10000
    assert session["compaction_detected"] is True
    assert len(session["top_steps"]) > 0
    assert session["top_steps"][0]["tool_or_source"] == "view_file"

    global_data = data["globalData"]
    assert global_data["total_conversations"] == 1
    assert global_data["total_tokens"] == 25000
    assert global_data["total_compactions"] == 1

def test_sidecar_no_cross_conversation_leakage(tmp_path):
    # If a specific cid is requested that is not yet in the DB, it must NOT leak conv-existing
    db_path = tmp_path / "test_analytics2.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_characters INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        duration_seconds REAL DEFAULT 0.0, active_agent_seconds REAL DEFAULT 0.0,
        active_pairing_seconds REAL DEFAULT 0.0, thinking_tokens INTEGER DEFAULT 0,
        user_prompts INTEGER DEFAULT 0, tool_calls_count INTEGER DEFAULT 0,
        redundant_file_reads INTEGER DEFAULT 0, lines_written INTEGER DEFAULT 0,
        lines_edited INTEGER DEFAULT 0, failed_tool_calls INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0, billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0, parent_id TEXT, root_id TEXT,
        depth INTEGER DEFAULT 0, subagent_role TEXT, subagent_type TEXT,
        invoking_step_index INTEGER, compaction_count INTEGER DEFAULT 0,
        pruned_tokens INTEGER DEFAULT 0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        step_index INTEGER NOT NULL, created_at TEXT, source TEXT,
        step_type TEXT, category TEXT, activity TEXT, tool_or_source TEXT,
        characters INTEGER DEFAULT 0, est_tokens INTEGER DEFAULT 0,
        thinking_tokens INTEGER DEFAULT 0, phase TEXT, model_name TEXT,
        detail TEXT, billed_cost REAL DEFAULT 0.0, billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0, UNIQUE(conversation_id, step_index)
    );
    """)
    cur.execute("""
    INSERT INTO conversations (id, title, project, model_name, updated_at, total_tokens)
    VALUES ('conv-existing', 'Secret Conversation', 'proj', 'Gemini 3.7 Flash', '2026-10-09T10:00:00Z', 99999)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'conv-new-unknown');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    session = json.loads(res.stdout)
    assert session["cid"] == "conv-new-unknown"
    assert session["active_tokens"] == 0
    assert "Secret" not in session["title"]

def test_main_mjs_exists_and_syntax():
    main_mjs = Path("sidecars/panel/main.mjs")
    assert main_mjs.exists(), "sidecars/panel/main.mjs must exist"
    res = subprocess.run(
        ["node", "--check", str(main_mjs)],
        capture_output=True,
        text=True
    )
    assert res.returncode == 0, f"Syntax check failed: {res.stderr}"
