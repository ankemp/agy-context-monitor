import json
import subprocess
import sqlite3
from pathlib import Path


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
        cwd=Path.cwd(),
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
    assert "quota" in global_data
    assert "weekly_quota_pct" in global_data

    # Quota payload in session
    assert "quota" in session
    assert "weekly_quota_pct" in session
    assert "five_hour_quota_pct" in session
    assert "quota_remaining_weekly_pct" in session
    assert "quota_remaining_5h_pct" in session
    assert session["quota"]["calibrated_weekly_capacity"] == 672.0
    assert "weekly_quota_pct" in session["top_steps"][0]


def test_sidecar_quota_integration(tmp_path):
    db_path = tmp_path / "test_analytics_quota.db"
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
        total_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        compaction_count INTEGER DEFAULT 0,
        pruned_tokens INTEGER DEFAULT 0
    );

    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        created_at TEXT,
        step_type TEXT,
        category TEXT,
        activity TEXT,
        tool_or_source TEXT,
        est_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        detail TEXT
    );

    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY,
        group_name TEXT NOT NULL,
        window TEXT NOT NULL,
        remaining_fraction REAL NOT NULL,
        reset_time TEXT NOT NULL,
        calibrated_capacity REAL NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """)

    cur.executemany(
        """
    INSERT INTO quota_state (bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        [
            (
                "gemini-weekly",
                "Gemini Models",
                "weekly",
                0.85,
                "2026-10-21T02:27:22Z",
                672.0,
                "2026-10-14T10:00:00Z",
            ),
            (
                "gemini-5h",
                "Gemini Models",
                "5h",
                0.90,
                "2026-10-14T18:00:00Z",
                236.7,
                "2026-10-14T10:00:00Z",
            ),
            (
                "3p-weekly",
                "Claude and GPT models",
                "weekly",
                0.95,
                "2026-10-21T02:27:22Z",
                200.0,
                "2026-10-14T10:00:00Z",
            ),
            (
                "3p-5h",
                "Claude and GPT models",
                "5h",
                0.98,
                "2026-10-14T18:00:00Z",
                70.0,
                "2026-10-14T10:00:00Z",
            ),
        ],
    )

    # Insert Gemini conversation with billed_cost = $6.72 (exactly 1.0% of $672)
    cur.execute("""
    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, updated_at)
    VALUES ('conv-gemini', 'Gemini Chat', 'Gemini 3.8 Flash', 6.72, 50000, '2026-10-14T11:00:00Z')
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, activity, est_tokens, billed_cost)
    VALUES ('conv-gemini', 1, 'USER_INPUT', 'Prompt', 1000, 1.344)
    """)

    # Insert Claude conversation with billed_cost = $2.00 (exactly 1.0% of $200)
    cur.execute("""
    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, updated_at)
    VALUES ('conv-claude', 'Claude Chat', 'Claude 3.7 Sonnet', 2.00, 30000, '2026-10-14T11:05:00Z')
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData, getQuotaSummary, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const geminiSession = getSessionData(db, 'conv-gemini');
    const claudeSession = getSessionData(db, 'conv-claude');
    const quotaGemini = getQuotaSummary(db, 'Gemini 3.8 Flash', 6.72);
    const quotaClaude = getQuotaSummary(db, 'Claude 3.7 Sonnet', 2.00);
    const globalData = getGlobalData(db);
    console.log(JSON.stringify({{ geminiSession, claudeSession, quotaGemini, quotaClaude, globalData }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    gemini_s = data["geminiSession"]
    assert abs(gemini_s["weekly_quota_pct"] - 1.0) < 0.01
    assert abs(gemini_s["quota_remaining_weekly_pct"] - 85.0) < 0.1
    assert abs(gemini_s["quota_remaining_5h_pct"] - 90.0) < 0.1
    assert gemini_s["quota"]["prefix"] == "gemini"
    assert gemini_s["quota"]["calibrated_weekly_capacity"] == 672.0
    assert gemini_s["quota"]["weekly_reset_countdown"] != ""
    assert gemini_s["top_steps"][0]["billed_cost"] == 1.344
    assert abs(gemini_s["top_steps"][0]["weekly_quota_pct"] - 0.2) < 0.01

    claude_s = data["claudeSession"]
    assert abs(claude_s["weekly_quota_pct"] - 1.0) < 0.01
    assert abs(claude_s["quota_remaining_weekly_pct"] - 95.0) < 0.1
    assert abs(claude_s["quota_remaining_5h_pct"] - 98.0) < 0.1
    assert claude_s["quota"]["prefix"] == "3p"
    assert claude_s["quota"]["calibrated_weekly_capacity"] == 200.0

    quota_g = data["quotaGemini"]
    assert quota_g["is_3p"] is False
    assert quota_g["buckets"]["gemini-weekly"]["remaining_pct"] == 85.0
    assert quota_g["buckets"]["3p-weekly"]["remaining_pct"] == 95.0
    # Gemini conversation should have 0% impact on 3P bucket and 1% on Gemini bucket
    assert quota_g["buckets"]["3p-weekly"]["session_impact_pct"] == 0.0
    assert abs(quota_g["buckets"]["gemini-weekly"]["session_impact_pct"] - 1.0) < 0.01

    quota_c = data["quotaClaude"]
    assert quota_c["is_3p"] is True
    assert quota_c["calibrated_weekly_capacity"] == 200.0
    # Claude conversation should have 0% impact on Gemini bucket and 1% on 3P bucket
    assert quota_c["buckets"]["gemini-weekly"]["session_impact_pct"] == 0.0
    assert abs(quota_c["buckets"]["3p-weekly"]["session_impact_pct"] - 1.0) < 0.01


def test_sidecar_quota_missing_table_fallback(tmp_path):
    # Tests that query.mjs falls back gracefully when quota_state table does not exist
    db_path = tmp_path / "test_analytics_no_quota_table.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens)
    VALUES ('c1', 'Test', 'Gemini 3.7 Flash', 3.36, 1000);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData, getQuotaSummary }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'c1');
    const quota = getQuotaSummary(db, 'Gemini 3.7 Flash', 3.36);
    console.log(JSON.stringify({{ session, quota }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)
    session = data["session"]
    assert session["quota"]["calibrated_weekly_capacity"] == 672.0
    assert session["quota_remaining_weekly_pct"] == 100.0
    assert abs(session["weekly_quota_pct"] - 0.5) < 0.01


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
        cwd=Path.cwd(),
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
        ["node", "--check", str(main_mjs)], capture_output=True, text=True
    )
    assert res.returncode == 0, f"Syntax check failed: {res.stderr}"


def test_sidecar_quota_indexing_session_uses_cached_quota(tmp_path):
    # Verifies that when a conversation is indexing, getSessionData reads cached quota_state
    db_path = tmp_path / "test_analytics_indexing.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.50, '2026-10-21T02:27:22Z', 672.0, '2026-10-14T10:00:00Z');
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'indexing-conv-id');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    session = json.loads(res.stdout)
    assert session["cid"] == "indexing-conv-id"
    assert session["quota_remaining_weekly_pct"] == 50.0
    assert session["quota"]["weekly_remaining_pct"] == 50.0


def test_sidecar_model_detection_and_naive_utc_countdown():
    # Verifies model context limit / prefix detection and UTC naive countdown
    node_code = """
    import { getModelContextLimit, getModelQuotaPrefix, formatResetCountdown } from './sidecars/panel/query.mjs';
    const sonnetLimit = getModelContextLimit('Sonnet 3.7');
    const sonnetPrefix = getModelQuotaPrefix('Sonnet 3.7');
    const opusLimit = getModelContextLimit('Claude 3 Opus');
    const opusPrefix = getModelQuotaPrefix('Claude 3 Opus');
    const argonLimit = getModelContextLimit('Gemini 4 Argon');
    const argonPrefix = getModelQuotaPrefix('Gemini 4 Argon');

    const now = new Date('2026-10-14T12:00:00Z');
    const utcZ = formatResetCountdown('2026-10-14T14:00:00Z', now);
    const naive = formatResetCountdown('2026-10-14T14:00:00', now);

    console.log(JSON.stringify({ sonnetLimit, sonnetPrefix, opusLimit, opusPrefix, argonLimit, argonPrefix, utcZ, naive }));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)
    assert data["sonnetLimit"] == 200000
    assert data["sonnetPrefix"] == "3p"
    assert data["opusLimit"] == 200000
    assert data["opusPrefix"] == "3p"
    assert data["argonLimit"] == 1000000
    assert data["argonPrefix"] == "gemini"
    assert data["utcZ"] == "2h 0m"
    assert data["naive"] == "2h 0m"


def test_sidecar_global_cost_breakdown_by_model_family(tmp_path):
    db_path = tmp_path / "test_analytics_global_family.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 672.0, '2026-10-14T10:00:00Z'),
        ('3p-weekly', 'Claude Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 200.0, '2026-10-14T10:00:00Z');
    INSERT INTO conversations VALUES
        ('c-gem', 'Gemini', 'p', 'Gemini 3.8 Flash', '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z', 1, 1000, 6.72),
        ('c-3p', 'Claude', 'p', 'Claude 3.7 Sonnet', '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z', 1, 1000, 2.00);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const g = getGlobalData(db);
    console.log(JSON.stringify(g));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)
    assert abs(data["total_cost"] - 8.72) < 0.01
    # Gemini bucket should calculate against Gemini cost ($6.72 / $672 = 1.0%)
    assert (
        abs(data["quota"]["buckets"]["gemini-weekly"]["session_impact_pct"] - 1.0)
        < 0.01
    )
    # 3P bucket should calculate against 3P cost ($2.00 / $200 = 1.0%)
    assert abs(data["quota"]["buckets"]["3p-weekly"]["session_impact_pct"] - 1.0) < 0.01


def test_sidecar_missing_conversations_table_graceful(tmp_path):
    db_path = tmp_path / "test_analytics_no_conversations.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    )
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const s = getSessionData(db, 'any-id');
    const g = getGlobalData(db);
    console.log(JSON.stringify({{ s, g }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)
    assert data["s"]["cid"] == "any-id"
    assert data["g"]["total_conversations"] == 0


def test_sidecar_get_global_data_filters_older_conversations(tmp_path):
    db_path = tmp_path / "test_analytics_window.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 672.0, '2026-10-14T10:00:00Z'),
        ('3p-weekly', 'Claude Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 200.0, '2026-10-14T10:00:00Z');
    INSERT INTO conversations VALUES
        ('c-recent-gem', 'Recent Gemini', 'p', 'Gemini 3.8 Flash', '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z', 1, 1000, 6.72),
        ('c-recent-3p', 'Recent Claude', 'p', 'Claude 3.7 Sonnet', '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z', 1, 1000, 2.00),
        ('c-sameday-gem', 'Same Day Space Gemini', 'p', 'Gemini 3.8 Flash', '2026-10-14 05:00:00', '2026-10-14 05:00:00', 1, 1000, 3.36),
        ('c-micro-3p', 'Microsecond Claude', 'p', 'Claude 3.7 Sonnet', '2026-10-14T03:00:00.123456', '2026-10-14T03:00:00.123456', 1, 1000, 1.00),
        ('c-tz-gem', 'Timezone Offset Gemini', 'p', 'Gemini 3.8 Flash', '2026-10-14T01:00:00-04:00', '2026-10-14T01:00:00-04:00', 1, 1000, 3.36),
        ('c-old-gem', 'Old Gemini', 'p', 'Gemini 3.8 Flash', '2026-09-01T10:00:00Z', '2026-09-01T10:00:00Z', 1, 1000, 50.00);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const g = getGlobalData(db);
    console.log(JSON.stringify(g));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # All-time total includes the old conversation ($50 + $6.72 + $2.00 + $3.36 + $1.00 + $3.36 = $66.44)
    assert abs(data["total_cost"] - 66.44) < 0.01
    assert data["total_conversations"] == 6

    # Weekly cost includes recent conversations ($6.72 + $2.00 + $3.36 + $1.00 + $3.36 = $16.44)
    assert abs(data["weekly_cost"] - 16.44) < 0.01
    # Gemini weekly cost = $6.72 + $3.36 + $3.36 = $13.44 => $13.44 / $672 = 2.0%
    assert (
        abs(data["quota"]["buckets"]["gemini-weekly"]["session_impact_pct"] - 2.0)
        < 0.01
    )
    # 3P weekly cost = $2.00 + $1.00 = $3.00 => $3.00 / $200 = 1.5%
    assert abs(data["quota"]["buckets"]["3p-weekly"]["session_impact_pct"] - 1.5) < 0.01


def test_sidecar_backend_header_meta_badges(tmp_path):
    """Verifies that backend query data provides required fields for header cost and quota badges."""
    db_path = tmp_path / "test_header_badges.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, project TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        category TEXT, activity TEXT, tool_or_source TEXT, est_tokens INTEGER DEFAULT 0,
        detail TEXT, billed_cost REAL DEFAULT 0.0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 672.0, '2026-10-14T10:00:00Z');
    INSERT INTO conversations VALUES
        ('conv-badge-1', 'Badge Test', 'p', 'Gemini 3.8 Flash', '2026-10-14T10:00:00Z', '2026-10-14T10:00:00Z', 1, 1000, 3.36);
    INSERT INTO steps VALUES
        (1, 'conv-badge-1', 1, 'General', 'Prompt', 'User', 1000, 'Test', 3.36);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const s = getSessionData(db, 'conv-badge-1');
    const g = getGlobalData(db);
    console.log(JSON.stringify({{ session: s, global: g }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # Session mode header checks: cost $3.36, quota ~0.50% wk
    session = data["session"]
    assert abs(session["billed_cost"] - 3.36) < 0.01
    assert abs(session["weekly_quota_pct"] - 0.50) < 0.01
    assert session["quota"]["calibrated_weekly_capacity"] == 672.0

    # Global mode header checks: cost $3.36, quota ~0.50% fleet
    global_data = data["global"]
    assert abs(global_data["total_cost"] - 3.36) < 0.01
    assert abs(global_data["weekly_quota_pct"] - 0.50) < 0.01
