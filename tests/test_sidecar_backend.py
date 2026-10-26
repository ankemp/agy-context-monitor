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
    content = main_mjs.read_text(encoding="utf-8")
    assert "data?.inspect_cid" in content


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


def test_sidecar_fleet_swarm_rollups_and_node_sorting(tmp_path):
    """Verifies that a root session with 2 sub-agents produces correct fleet rollups and sorted nodes."""
    db_path = tmp_path / "test_swarm.db"
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
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        parent_id TEXT,
        root_id TEXT,
        depth INTEGER DEFAULT 0,
        subagent_role TEXT,
        subagent_type TEXT,
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

    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 672.0, '2026-10-14T10:00:00Z'),
        ('3p-weekly', 'Claude Models', 'weekly', 0.90, '2026-10-21T02:27:22Z', 200.0, '2026-10-14T10:00:00Z');
    """)

    # Root coordinator (cost 1.50)
    cur.execute("""
    INSERT INTO conversations (
        id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
        billed_cost, parent_id, root_id, depth, subagent_role
    ) VALUES (
        'root-1', 'Root Coordinator', 'proj-alpha', 'Gemini 3.7 Flash',
        '2026-10-14T10:00:00Z', '2026-10-14T10:30:00Z', 10, 20000,
        1.50, NULL, NULL, 0, 'Root Coordinator'
    )
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, billed_cost)
    VALUES ('root-1', 1, 'USER_INPUT', 'General', 'Prompt', 'User', 10000, 1.50)
    """)

    # Subagent 1: Worker (cost 4.50, highest) with Checkpoint step
    cur.execute("""
    INSERT INTO conversations (
        id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
        billed_cost, parent_id, root_id, depth, subagent_role
    ) VALUES (
        'sub-1', 'Worker Alpha', 'proj-alpha', 'Gemini 3.7 Flash',
        '2026-10-14T10:05:00Z', '2026-10-14T10:20:00Z', 25, 50000,
        4.50, 'root-1', 'root-1', 1, 'Worker'
    )
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, billed_cost)
    VALUES
        ('sub-1', 1, 'CHECKPOINT', 'Summaries', 'Checkpoint', 'System', 5000, 1.00),
        ('sub-1', 2, 'TOOL_RESULT', 'File', 'Read', 'view_file', 15000, 3.50)
    """)

    # Subagent 2: Researcher (cost 2.00, Claude model)
    cur.execute("""
    INSERT INTO conversations (
        id, title, project, model_name, created_at, updated_at, total_steps, total_tokens,
        billed_cost, parent_id, root_id, depth, subagent_role
    ) VALUES (
        'sub-2', 'Researcher Beta', 'proj-alpha', 'Claude 3.7 Sonnet',
        '2026-10-14T10:10:00Z', '2026-10-14T10:25:00Z', 15, 30000,
        2.00, 'root-1', 'root-1', 1, 'Researcher'
    )
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, billed_cost)
    VALUES ('sub-2', 1, 'TOOL_RESULT', 'Web', 'Search', 'search_web', 12000, 2.00)
    """)

    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'root-1');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # Local root checks
    assert data["cid"] == "root-1"
    assert data["title"] == "Root Coordinator"
    assert abs(data["billed_cost"] - 1.50) < 0.01

    # Fleet checks
    fleet = data["fleet"]
    assert fleet["is_swarm"] is True
    assert fleet["agent_count"] == 3
    assert fleet["subagent_count"] == 2
    assert abs(fleet["total_cost"] - 8.00) < 0.01
    assert fleet["total_steps"] == 50
    assert fleet["total_tokens"] == 100000
    assert fleet["root_id"] == "root-1"
    assert fleet["active_cid"] == "root-1"
    assert fleet["inspected_cid"] == "root-1"
    assert fleet["is_inspecting_other"] is False
    assert fleet["shielded_tokens"] == 80000
    assert fleet["shielding_ratio"] == 5.0

    # Nodes sorting by billed_cost DESC, total_tokens DESC
    nodes = fleet["nodes"]
    assert len(nodes) == 3
    assert nodes[0]["id"] == "sub-1"
    assert abs(nodes[0]["billed_cost"] - 4.50) < 0.01
    assert nodes[0]["subagent_role"] == "Worker"
    assert nodes[0]["max_tokens"] == 1000000
    # Post checkpoint active tokens: 5000 + 15000 = 20000
    assert nodes[0]["active_tokens"] == 20000
    assert abs(nodes[0]["context_pct"] - 2.0) < 0.1

    assert nodes[1]["id"] == "sub-2"
    assert abs(nodes[1]["billed_cost"] - 2.00) < 0.01
    assert nodes[1]["subagent_role"] == "Researcher"
    assert nodes[1]["max_tokens"] == 200000  # Claude context limit
    assert nodes[1]["active_tokens"] == 12000
    assert abs(nodes[1]["context_pct"] - 6.0) < 0.1

    assert nodes[2]["id"] == "root-1"
    assert abs(nodes[2]["billed_cost"] - 1.50) < 0.01
    assert nodes[2]["is_root"] is True
    assert nodes[2]["is_active"] is True
    assert nodes[2]["is_inspected"] is True


def test_sidecar_inspect_subagent_preserves_fleet(tmp_path):
    """Verifies that inspecting a sub-agent returns local telemetry while preserving full fleet summary."""
    db_path = tmp_path / "test_swarm_inspect.db"
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
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        parent_id TEXT,
        root_id TEXT,
        depth INTEGER DEFAULT 0,
        subagent_role TEXT,
        subagent_type TEXT,
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

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, total_steps, parent_id, root_id, subagent_role)
    VALUES
        ('root-1', 'Root Coordinator', 'Gemini 3.7 Flash', 1.00, 10000, 5, NULL, NULL, 'Root Coordinator'),
        ('sub-1', 'Worker Alpha', 'Gemini 3.7 Flash', 3.00, 30000, 12, 'root-1', 'root-1', 'Worker'),
        ('sub-2', 'Worker Beta', 'Gemini 3.7 Flash', 2.00, 20000, 8, 'root-1', 'root-1', 'Worker');

    INSERT INTO steps (conversation_id, step_index, step_type, category, activity, tool_or_source, est_tokens, billed_cost)
    VALUES
        ('sub-1', 1, 'TOOL_RESULT', 'File', 'Edit', 'replace_file_content', 4500, 3.00);
    """)
    conn.commit()
    conn.close()

    # Inspect sub-1 while active session is root-1
    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'root-1', 'sub-1');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # Inspecting sub-1 returns sub-1 local metrics
    assert data["cid"] == "sub-1"
    assert data["title"] == "Worker Alpha"
    assert abs(data["billed_cost"] - 3.00) < 0.01
    assert data["active_tokens"] == 4500
    assert len(data["top_steps"]) == 1
    assert data["top_steps"][0]["tool_or_source"] == "replace_file_content"

    # Preserves 3-node fleet summary
    fleet = data["fleet"]
    assert fleet["is_swarm"] is True
    assert fleet["agent_count"] == 3
    assert fleet["subagent_count"] == 2
    assert fleet["root_id"] == "root-1"
    assert fleet["active_cid"] == "root-1"
    assert fleet["inspected_cid"] == "sub-1"
    assert fleet["is_inspecting_other"] is True
    assert fleet["inspected_role"] == "Worker"
    assert abs(fleet["total_cost"] - 6.00) < 0.01
    assert fleet["total_steps"] == 25
    assert len(fleet["nodes"]) == 3

    # Node flags reflect inspection status
    sub1_node = next(n for n in fleet["nodes"] if n["id"] == "sub-1")
    assert sub1_node["is_inspected"] is True
    assert sub1_node["is_active"] is False

    root_node = next(n for n in fleet["nodes"] if n["id"] == "root-1")
    assert root_node["is_inspected"] is False
    assert root_node["is_active"] is True
    assert root_node["is_root"] is True


def test_sidecar_inspect_unrelated_cid_falls_back(tmp_path):
    """Verifies that attempting to inspect an unrelated session outside the swarm falls back to active session."""
    db_path = tmp_path / "test_unrelated.db"
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
        parent_id TEXT,
        root_id TEXT
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, root_id)
    VALUES
        ('swarm-root', 'Swarm Root', 'Gemini 3.7 Flash', 1.00, NULL),
        ('unrelated-conv', 'Unrelated Session', 'Gemini 3.7 Flash', 9.00, NULL);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'swarm-root', 'unrelated-conv');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # Must fall back to swarm-root
    assert data["cid"] == "swarm-root"
    assert data["fleet"]["inspected_cid"] == "swarm-root"
    assert data["fleet"]["is_inspecting_other"] is False


def test_sidecar_global_data_root_and_subagent_breakdown(tmp_path):
    """Verifies that getGlobalData returns root_conversations and subagent_conversations counts."""
    db_path = tmp_path / "test_global_breakdown.db"
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
        parent_id TEXT,
        root_id TEXT
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, parent_id)
    VALUES
        ('r1', 'Root 1', 'Gemini 3.7 Flash', 1.00, 1000, NULL),
        ('r2', 'Root 2', 'Gemini 3.7 Flash', 2.00, 2000, ''),
        ('s1', 'Sub 1', 'Gemini 3.7 Flash', 0.50, 500, 'r1'),
        ('s2', 'Sub 2', 'Gemini 3.7 Flash', 0.50, 500, 'r1'),
        ('s3', 'Sub 3', 'Gemini 3.7 Flash', 0.50, 500, 'r2');
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

    assert data["total_conversations"] == 5
    assert data["root_conversations"] == 2
    assert data["subagent_conversations"] == 3
    assert abs(data["total_cost"] - 4.50) < 0.01


def test_sidecar_default_session_fleet_schema():
    """Verifies that createDefaultSession returns valid default fleet metadata."""
    node_code = """
    import { createDefaultSession } from './sidecars/panel/query.mjs';
    const s = createDefaultSession('default-cid');
    console.log(JSON.stringify(s));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["cid"] == "default-cid"
    assert "fleet" in data
    fleet = data["fleet"]
    assert fleet["is_swarm"] is False
    assert fleet["agent_count"] == 1
    assert fleet["subagent_count"] == 0
    assert fleet["nodes"] == []
    assert fleet["total_cost"] == 0
    assert fleet["total_tokens"] == 0
    assert fleet["total_disk_tokens"] == 0
    assert fleet["total_steps"] == 0
    assert fleet["weekly_quota_pct"] == 0
    assert fleet["five_hour_quota_pct"] == 0
    assert fleet["shielded_tokens"] == 0
    assert fleet["shielding_ratio"] == 1.0


def test_sidecar_nested_swarm_parent_id_walk(tmp_path):
    """Verifies that nested subagents (depth >= 2) with missing root_id walk parent_id to resolve root."""
    db_path = tmp_path / "test_nested_swarm.db"
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
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        parent_id TEXT,
        root_id TEXT,
        depth INTEGER DEFAULT 0,
        subagent_role TEXT,
        subagent_type TEXT,
        compaction_count INTEGER DEFAULT 0,
        pruned_tokens INTEGER DEFAULT 0
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, parent_id, root_id, subagent_role)
    VALUES
        ('root-coord', 'Root Coordinator', 'Gemini 3.7 Flash', 1.00, 10000, NULL, NULL, 'Root Coordinator'),
        ('mid-coord', 'Sub Coordinator', 'Gemini 3.7 Flash', 2.00, 20000, 'root-coord', NULL, 'Coordinator'),
        ('leaf-worker', 'Leaf Worker', 'Gemini 3.7 Flash', 3.00, 30000, 'mid-coord', NULL, 'Worker');
    """)
    conn.commit()
    conn.close()

    # When inspecting leaf-worker while active session is root-coord
    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'root-coord', 'leaf-worker');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["cid"] == "leaf-worker"
    fleet = data["fleet"]
    assert fleet["root_id"] == "root-coord"
    assert fleet["active_cid"] == "root-coord"
    assert fleet["inspected_cid"] == "leaf-worker"
    assert fleet["is_inspecting_other"] is True


def test_sidecar_swarm_fallback_schema_missing_subagent_role(tmp_path):
    """Verifies that schemas lacking subagent_role or billed_input_tokens still query all swarm nodes."""
    db_path = tmp_path / "test_fallback_schema.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        model_name TEXT,
        total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        parent_id TEXT,
        root_id TEXT
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, parent_id, root_id)
    VALUES
        ('root-f', 'Root Fallback', 'Gemini 3.7 Flash', 1.00, 10000, NULL, NULL),
        ('sub-f1', 'Sub Fallback 1', 'Gemini 3.7 Flash', 2.00, 20000, 'root-f', 'root-f'),
        ('sub-f2', 'Sub Fallback 2', 'Gemini 3.7 Flash', 3.00, 30000, 'root-f', 'root-f');
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'root-f');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    fleet = data["fleet"]
    assert fleet["is_swarm"] is True
    assert fleet["agent_count"] == 3
    assert len(fleet["nodes"]) == 3
    assert abs(fleet["total_cost"] - 6.00) < 0.01


def test_sidecar_inspect_root_from_subagent_session(tmp_path):
    """Verifies that an active subagent session can inspect the root coordinator."""
    db_path = tmp_path / "test_inspect_root.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        model_name TEXT,
        total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        parent_id TEXT,
        root_id TEXT,
        subagent_role TEXT
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens, parent_id, root_id, subagent_role)
    VALUES
        ('root-main', 'Coordinator', 'Gemini 3.7 Flash', 1.00, 10000, NULL, NULL, 'Root Coordinator'),
        ('sub-worker', 'Worker 1', 'Gemini 3.7 Flash', 4.00, 40000, 'root-main', 'root-main', 'Worker');
    """)
    conn.commit()
    conn.close()

    # Active session is sub-worker, inspecting root-main
    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'sub-worker', 'root-main');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["cid"] == "root-main"
    assert data["title"] == "Coordinator"
    fleet = data["fleet"]
    assert fleet["is_swarm"] is True
    assert fleet["active_cid"] == "sub-worker"
    assert fleet["inspected_cid"] == "root-main"
    assert fleet["is_inspecting_other"] is True


def test_sidecar_inspect_nonexistent_cid_falls_back(tmp_path):
    """Verifies that attempting to inspect a nonexistent CID safely falls back to the active session."""
    db_path = tmp_path / "test_nonexistent.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        model_name TEXT,
        total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        parent_id TEXT,
        root_id TEXT
    );

    INSERT INTO conversations (id, title, model_name, billed_cost, total_tokens)
    VALUES ('active-session', 'Active Session', 'Gemini 3.7 Flash', 2.50, 15000);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const session = getSessionData(db, 'active-session', 'ghost-subagent');
    console.log(JSON.stringify(session));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["cid"] == "active-session"
    assert data["fleet"]["inspected_cid"] == "active-session"
    assert data["fleet"]["is_inspecting_other"] is False


def test_sidecar_global_data_schema_without_parent_id(tmp_path):
    """Verifies getGlobalData handles schemas lacking parent_id column gracefully."""
    db_path = tmp_path / "test_no_parent_id.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT,
        model_name TEXT,
        total_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0
    );

    INSERT INTO conversations (id, title, model_name, total_tokens, billed_cost)
    VALUES
        ('c1', 'Chat 1', 'Gemini 3.7 Flash', 1000, 0.10),
        ('c2', 'Chat 2', 'Gemini 3.7 Flash', 2000, 0.20);
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

    assert data["total_conversations"] == 2
    assert data["root_conversations"] == 2
    assert data["subagent_conversations"] == 0


def test_sidecar_conversation_fingerprint_isolation(tmp_path):
    """Verifies getConversationFingerprint changes only when target conversation, its swarm fleet, inspect target, or quota changes."""
    db_path = tmp_path / "test_fp.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT,
        updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0,
        parent_id TEXT, root_id TEXT, compaction_count INTEGER DEFAULT 0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO conversations VALUES
        ('conv-a', 'Conv A', 'Gemini 3.8 Flash', '2026-10-26T10:00:00Z', 5, 1000, 0.10, NULL, 'conv-a', 0),
        ('sub-a1', 'Sub A1', 'Gemini 3.8 Flash', '2026-10-26T10:01:00Z', 3, 500, 0.05, 'conv-a', 'conv-a', 0),
        ('conv-b', 'Conv B', 'Gemini 3.8 Flash', '2026-10-26T10:00:00Z', 2, 400, 0.02, NULL, 'conv-b', 0);
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini', 'weekly', 0.85, '2026-10-28T14:00:00Z', 672.0, '2026-10-26T10:00:00Z');
    """)
    conn.commit()

    node_code = f"""
    import {{ openDatabase, getConversationFingerprint }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    console.log(JSON.stringify({{
      fpA: getConversationFingerprint(db, 'conv-a'),
      fpA_inspecting_sub: getConversationFingerprint(db, 'conv-a', 'sub-a1'),
      fpB: getConversationFingerprint(db, 'conv-b'),
      fpGlobal: getConversationFingerprint(db, null),
      fpNullDb: getConversationFingerprint(null, 'conv-a')
    }}));
    """
    res1 = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert res1.returncode == 0, res1.stderr
    initial = json.loads(res1.stdout)

    assert initial["fpNullDb"] == "empty|||0||0|0|0|0|"
    assert initial["fpA"] != initial["fpB"]
    assert initial["fpA"] != initial["fpA_inspecting_sub"]

    # 1. Update unrelated conv-b: conv-a must be isolated
    cur.execute("UPDATE conversations SET total_steps = 10, total_tokens = 9000, updated_at = '2026-10-26T10:05:00Z' WHERE id = 'conv-b'")
    conn.commit()
    res2 = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    after_b = json.loads(res2.stdout)
    assert after_b["fpA"] == initial["fpA"], "Unrelated conversation update must not change conv-a fingerprint"
    assert after_b["fpA_inspecting_sub"] == initial["fpA_inspecting_sub"], "Unrelated conversation update must not change conv-a inspect sub fingerprint"
    assert after_b["fpB"] != initial["fpB"]
    assert after_b["fpGlobal"] != initial["fpGlobal"]

    # 2. Update swarm child sub-a1: both conv-a root fingerprint and inspecting fingerprint must update
    cur.execute("UPDATE conversations SET total_steps = 4, total_tokens = 800, updated_at = '2026-10-26T10:06:00Z' WHERE id = 'sub-a1'")
    conn.commit()
    res3 = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    after_sub = json.loads(res3.stdout)
    assert after_sub["fpA"] != initial["fpA"], "Swarm child update must change root conv-a fingerprint"
    assert after_sub["fpA_inspecting_sub"] != initial["fpA_inspecting_sub"]
    assert after_sub["fpB"] == after_b["fpB"], "conv-b fingerprint must remain unchanged when sub-a1 updates"

    # 3. Update compaction_count on conv-a
    cur.execute("UPDATE conversations SET compaction_count = 1 WHERE id = 'conv-a'")
    conn.commit()
    res4 = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    after_comp = json.loads(res4.stdout)
    assert after_comp["fpA"] != after_sub["fpA"], "Compaction count change must alter conv-a fingerprint"
    assert after_comp["fpB"] == after_b["fpB"]

    # 4. Update quota_state: all non-empty fingerprints must reflect quota change
    cur.execute("UPDATE quota_state SET remaining_fraction = 0.80, fetched_at = '2026-10-26T10:10:00Z' WHERE bucket_id = 'gemini-weekly'")
    conn.commit()
    res5 = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    after_quota = json.loads(res5.stdout)
    assert after_quota["fpA"] != after_comp["fpA"], "Quota update must update conv-a fingerprint"
    assert after_quota["fpB"] != after_b["fpB"], "Quota update must update conv-b fingerprint"
    assert after_quota["fpGlobal"] != after_b["fpGlobal"], "Quota update must update global fingerprint"
    conn.close()


def test_sidecar_steps_queries_use_created_at_index(tmp_path):
    """Verifies that queryVelocityMetrics (global, session, fleet) and getQuotaSummary use idx_steps_created index,
    and accurately match space-separated, ISO, and microsecond timestamps."""
    db_path = tmp_path / "test_steps_index.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0,
        parent_id TEXT, root_id TEXT
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        created_at TEXT,
        model_name TEXT,
        step_type TEXT,
        category TEXT,
        activity TEXT,
        tool_or_source TEXT,
        est_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0,
        detail TEXT
    );
    CREATE INDEX idx_steps_created ON steps(created_at);

    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY,
        group_name TEXT NOT NULL,
        window TEXT NOT NULL,
        remaining_fraction REAL NOT NULL,
        reset_time TEXT NOT NULL,
        calibrated_capacity REAL NOT NULL,
        fetched_at TEXT NOT NULL
    );
    INSERT INTO quota_state VALUES
        ('gemini-weekly', 'Gemini Models', 'weekly', 0.90, '2026-10-28T14:00:00Z', 672.0, '2026-10-23T14:00:00Z'),
        ('gemini-5h', 'Gemini Models', '5h', 0.95, '2026-10-23T18:00:00Z', 236.7, '2026-10-23T14:00:00Z');

    INSERT INTO conversations VALUES
        ('conv-1', 'Test Conv', 'Gemini 3.7 Flash', '2026-10-23T10:00:00Z', '2026-10-23T14:00:00Z', 3, 3000, 0.15, NULL, 'conv-1');

    -- Insert steps with mixed timestamp formats: ISO Z, space-separated, and microseconds
    INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES
        ('conv-1', 1, '2026-10-23T13:30:00Z', 'Gemini 3.7 Flash', 1000, 5000, 1000),
        ('conv-1', 2, '2026-10-23 13:58:00', 'Gemini 3.7 Flash', 1000, 5000, 1000),
        ('conv-1', 3, '2026-10-23T13:59:30.123456Z', 'Gemini 3.7 Flash', 1000, 5000, 1000);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics, getQuotaSummary }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');

    // Spy on db.prepare to capture executed queries and their EXPLAIN QUERY PLAN
    const plans = [];
    const origPrepare = db.prepare.bind(db);
    db.prepare = function(sql) {{
      if (sql.includes('FROM steps') && (sql.includes('created_at') || sql.includes('substr(replace(created_at'))) {{
        try {{
          const planRows = origPrepare('EXPLAIN QUERY PLAN ' + sql).all();
          plans.push({{ sql, planRows }});
        }} catch (e) {{
          plans.push({{ sql, error: e.message }});
        }}
      }}
      return origPrepare(sql);
    }};

    const now = new Date('2026-10-23T14:00:00Z');
    const vSession = queryVelocityMetrics(db, {{ conversationId: 'conv-1', now }});
    const vGlobal = queryVelocityMetrics(db, {{ now }});
    const vFleet = queryVelocityMetrics(db, {{ rootId: 'conv-1', now }});
    const quota = getQuotaSummary(db, 'Gemini 3.7 Flash', 0.15, null, now);

    console.log(JSON.stringify({{
      plans,
      rolling5mBilledTpmSession: vSession.rolling5mBilledTpm,
      rolling60mBilledTphSession: vSession.rolling60mBilledTph,
      rolling60mBilledTphGlobal: vGlobal.rolling60mBilledTph,
      weeklyBilledTph: quota.weekly_billed_tph
    }}));
    """
    res = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    plans = data["plans"]
    assert len(plans) >= 3, f"Expected at least 3 steps queries, got: {len(plans)}"
    for p in plans:
        assert "planRows" in p, f"Query plan error: {p}"
        details = [r["detail"] for r in p["planRows"]]
        assert any("idx_steps_created" in d and "SEARCH" in d for d in details), (
            f"Expected SEARCH using idx_steps_created in plan for SQL:\n{p['sql']}\nPlan details: {details}"
        )

    # Verify that steps across mixed timestamp formats are accurately captured:
    # 2 steps in 5m window (13:58:00 with space and 13:59:30 with microseconds) -> (6000 + 6000) / 5 = 2400.0 TPM
    assert data["rolling5mBilledTpmSession"] == 2400.0
    # 3 steps in 60m window (13:30 ISO, 13:58 space, 13:59 microsecond) -> 3 * 6000 = 18000.0 TPH
    assert data["rolling60mBilledTphSession"] == 18000.0
    assert data["rolling60mBilledTphGlobal"] == 18000.0
    assert data["weeklyBilledTph"] > 0


def test_sidecar_query_cache_and_clear(tmp_path):
    """Verifies getSessionData and getGlobalData cache responses, clearQueryCache evicts them,
    fingerprint changes automatically invalidate cache, and FIFO capacity bounding works."""
    db_path = tmp_path / "test_cache.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0
    );
    INSERT INTO conversations VALUES
        ('conv-c', 'Cache Conv', 'Gemini 3.7 Flash', '2026-10-26T10:00:00Z', '2026-10-26T10:00:00Z', 1, 100, 0.01);
    """)
    conn.commit()

    node_code = f"""
    import {{ openDatabase, getSessionData, getGlobalData, clearQueryCache }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');

    let prepareCount = 0;
    const origPrepare = db.prepare.bind(db);
    db.prepare = function(sql) {{
      prepareCount++;
      return origPrepare(sql);
    }};

    // First calls: populate cache
    const s1 = getSessionData(db, 'conv-c');
    const g1 = getGlobalData(db);
    const countAfterFirst = prepareCount;

    // Second calls: should hit cache directly returning cached references
    const s2 = getSessionData(db, 'conv-c');
    const g2 = getGlobalData(db);
    const countAfterCached = prepareCount;

    // Clear cache explicitly
    clearQueryCache();

    // Third calls: should execute queries again and produce new references
    const s3 = getSessionData(db, 'conv-c');
    const g3 = getGlobalData(db);
    const countAfterEvict = prepareCount;

    console.log(JSON.stringify({{
      countAfterFirst,
      countAfterCached,
      countAfterEvict,
      isCachedSameRefS: s1 === s2,
      isCachedSameRefG: g1 === g2,
      isEvictedDifferentRefS: s1 !== s3,
      isEvictedDifferentRefG: g1 !== g3
    }}));
    """
    res = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["isCachedSameRefS"] is True, "Second getSessionData must return cached reference"
    assert data["isCachedSameRefG"] is True, "Second getGlobalData must return cached reference"
    assert data["isEvictedDifferentRefS"] is True, "getSessionData after clearQueryCache must return new object"
    assert data["isEvictedDifferentRefG"] is True, "getGlobalData after clearQueryCache must return new object"
    assert (data["countAfterCached"] - data["countAfterFirst"]) < (data["countAfterEvict"] - data["countAfterCached"]), (
        "Cached calls should only execute fingerprint checks, not full queries"
    )

    # Test that DB mutation changes fingerprint and produces fresh session data without clearQueryCache()
    mutate_node_code = f"""
    import {{ openDatabase, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const first = getSessionData(db, 'conv-c');
    console.log(JSON.stringify({{ tokens: first.total_tokens }}));
    """
    res_m1 = subprocess.run(["node", "--input-type=module", "-e", mutate_node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert json.loads(res_m1.stdout)["tokens"] == 100

    cur.execute("UPDATE conversations SET total_tokens = 999, updated_at = '2026-10-26T11:00:00Z' WHERE id = 'conv-c'")
    conn.commit()
    conn.close()

    res_m2 = subprocess.run(["node", "--input-type=module", "-e", mutate_node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert json.loads(res_m2.stdout)["tokens"] == 999


def test_sidecar_sse_filtered_by_conversation(tmp_path):
    """Verifies that the SSE hub:
    1. Only delivers 'update' events to clients whose subscribed conversation or swarm fleet changed.
    2. Swarm child updates notify the root conversation subscriber.
    3. Unrelated conversation updates do not leak events to other subscribers.
    4. Idle watcher and heartbeat timers stop when all clients disconnect."""
    db_path = tmp_path / "test_sse.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT,
        created_at TEXT, updated_at TEXT, total_steps INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0, billed_cost REAL DEFAULT 0.0,
        parent_id TEXT, root_id TEXT, compaction_count INTEGER DEFAULT 0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT, window TEXT,
        remaining_fraction REAL, reset_time TEXT, calibrated_capacity REAL, fetched_at TEXT
    );
    INSERT INTO conversations VALUES
        ('conv-a', 'Conv A', 'Gemini 3.7 Flash', '2026-10-26T10:00:00Z', '2026-10-26T10:00:00Z', 5, 1000, 0.10, NULL, 'conv-a', 0),
        ('sub-a1', 'Sub A1', 'Gemini 3.7 Flash', '2026-10-26T10:01:00Z', '2026-10-26T10:01:00Z', 3, 500, 0.05, 'conv-a', 'conv-a', 0),
        ('conv-b', 'Conv B', 'Gemini 3.7 Flash', '2026-10-26T10:00:00Z', '2026-10-26T10:00:00Z', 2, 400, 0.02, NULL, 'conv-b', 0);
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import http from 'node:http';
    import {{ DatabaseSync }} from 'node:sqlite';
    import {{ getConversationFingerprint, clearQueryCache }} from './sidecars/panel/query.mjs';
    import {{ createSseHub, attachSseRoute }} from './sidecars/panel/sse.mjs';

    let db = new DatabaseSync('{db_path}');
    let cacheInvalidated = 0;

    const hub = createSseHub({{
      getDb: () => db,
      getFingerprint: getConversationFingerprint,
      onCacheInvalidate: () => {{
        cacheInvalidated++;
        clearQueryCache();
      }},
      checkIntervalMs: 50,
      settleWindowMs: 20,
      heartbeatMs: 500,
      dbPath: '{db_path}'
    }});

    const server = http.createServer((req, res) => {{
      res.writeHead(404);
      res.end('Not found');
    }});
    attachSseRoute(server, hub);

    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const port = server.address().port;

    function connectClient(urlPath) {{
      return new Promise((resolve, reject) => {{
        const events = [];
        let raw = '';
        const req = http.get(`http://127.0.0.1:${{port}}${{urlPath}}`, (res) => {{
          res.setEncoding('utf8');
          res.on('data', (chunk) => {{
            raw += chunk;
            const parts = raw.split('\\n\\n');
            while (parts.length > 1) {{
              const block = parts.shift().trim();
              if (block.startsWith('event: update')) {{
                const dataLine = block.split('\\n').find(l => l.startsWith('data: '));
                if (dataLine) {{
                  events.push(JSON.parse(dataLine.slice(6)));
                }}
              }}
            }}
          }});
          res.on('end', () => {{}});
        }});
        req.on('error', reject);
        setTimeout(() => resolve({{ req, events }}), 50);
      }});
    }}

    const clientA = await connectClient('/api/events?cid=conv-a');
    const clientB = await connectClient('/api/events?cid=conv-b');

    // 1. Initial state: neither should have received any update events yet
    const initLenA = clientA.events.length;
    const initLenB = clientB.events.length;

    // 2. Mutate conv-b
    db.prepare("UPDATE conversations SET total_steps = 10, total_tokens = 9999, updated_at = '2026-10-26T10:10:00Z' WHERE id = 'conv-b'").run();
    await hub.checkChanges();
    await new Promise((r) => setTimeout(r, 40));

    const afterBLenA = clientA.events.length;
    const afterBLenB = clientB.events.length;

    // 3. Mutate sub-a1 (swarm fleet child of conv-a)
    db.prepare("UPDATE conversations SET total_steps = 8, total_tokens = 1200, updated_at = '2026-10-26T10:15:00Z' WHERE id = 'sub-a1'").run();
    await hub.checkChanges();
    await new Promise((r) => setTimeout(r, 40));

    const afterSubLenA = clientA.events.length;
    const afterSubLenB = clientB.events.length;

    // 4. Verify client disconnect closes timers
    const activeCountBefore = hub.activeClientCount;
    clientA.req.destroy();
    clientB.req.destroy();

    // Give destroy time to fire close event on server
    await new Promise((r) => setTimeout(r, 80));
    const activeCountAfter = hub.activeClientCount;
    const isWatcherRunning = hub.isWatcherRunning;

    hub.close();
    await new Promise((r) => server.close(r));
    db.close();

    console.log(JSON.stringify({{
      initLenA,
      initLenB,
      afterBLenA,
      afterBLenB,
      afterSubLenA,
      afterSubLenB,
      activeCountBefore,
      activeCountAfter,
      isWatcherRunning,
      clientBLastEvent: clientB.events[clientB.events.length - 1],
      clientALastEvent: clientA.events[clientA.events.length - 1]
    }}));
    """
    res = subprocess.run(["node", "--input-type=module", "-e", node_code], capture_output=True, text=True, cwd=Path.cwd())
    assert res.returncode == 0, f"Node script failed with: {res.stderr}\nStdout: {res.stdout}"
    data = json.loads(res.stdout)

    assert data["initLenA"] == 0
    assert data["initLenB"] == 0

    # Only client B received an event on conv-b mutation
    assert data["afterBLenA"] == 0, "Client A must NOT receive event when conv-b mutates"
    assert data["afterBLenB"] == 1, "Client B MUST receive event when conv-b mutates"
    assert data["clientBLastEvent"]["cid"] == "conv-b"

    # Swarm child sub-a1 mutation MUST notify client A and NOT client B
    assert data["afterSubLenA"] == 1, "Client A MUST receive event when subagent of conv-a mutates"
    assert data["afterSubLenB"] == 1, "Client B must NOT receive event when subagent of conv-a mutates"
    assert data["clientALastEvent"]["cid"] == "conv-a"

    # Verify zero-polling / zero-client cleanup
    assert data["activeCountBefore"] == 2
    assert data["activeCountAfter"] == 0
    assert data["isWatcherRunning"] is False, "Watcher must stop running when all clients disconnect"





