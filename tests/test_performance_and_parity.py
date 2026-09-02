import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

from context_monitor.db import get_db_connection, init_db
from context_monitor.dashboard import api_routes
from context_monitor.dashboard.server import get_cached_api_response, _API_CACHE

def test_sqlite_pragmas_configured(tmp_path):
    db_file = tmp_path / "test_pragmas.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    cache_size = cur.execute("PRAGMA cache_size;").fetchone()[0]
    assert cache_size == -64000

    mmap_size = cur.execute("PRAGMA mmap_size;").fetchone()[0]
    assert mmap_size == 268435456

    temp_store = cur.execute("PRAGMA temp_store;").fetchone()[0]
    # 2 corresponds to MEMORY in SQLite
    assert temp_store == 2
    conn.close()

def test_query_parity_and_correctness(tmp_path):
    db_file = tmp_path / "test_parity.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Insert sample conversation
    cur.execute(f"""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens, lines_written, lines_edited)
        VALUES ('conv-1', 'Test Conv', 'test-project', 'Gemini Pro', '{today_str}T10:00:00Z', 3, 1500, 50, 20)
    """)

    # Insert sample steps across two distinct hours
    cur.execute(f"""
        INSERT INTO steps (conversation_id, step_index, created_at, source, est_tokens, phase, model_name, category, billed_cost)
        VALUES 
        ('conv-1', 1, '{today_str}T10:15:00Z', 'USER_INPUT', 500, 'discovery', 'Gemini Pro', 'input', 0.001),
        ('conv-1', 2, '{today_str}T10:30:00Z', 'MODEL', 600, 'execution', 'Gemini Pro', 'code', 0.002),
        ('conv-1', 3, '{today_str}T11:05:00Z', 'MODEL', 400, 'execution', 'Gemini Pro', 'code', 0.001)
    """)

    # Insert sample tool executions
    cur.execute("""
        INSERT INTO tool_executions (conversation_id, step_index, tool_name, target_path)
        VALUES
        ('conv-1', 2, 'view_file', '/test/foo.py'),
        ('conv-1', 3, 'run_command', 'echo hi')
    """)
    conn.commit()

    # 1. Test query_summary_data
    summary = api_routes.query_summary_data(conn)
    assert summary["total_convs"] == 1
    assert summary["total_steps"] == 3
    assert summary["total_tokens"] == 1500
    assert summary["total_code_lines"] == 70
    assert summary["saved_reads"] == 1
    assert summary["saved_cmds"] == 1
    assert len(summary["phases"]) == 2
    assert summary["models"][0]["model_name"] == "Gemini Pro"
    assert summary["models"][0]["steps"] == 3

    # 2. Test query_timeline_data
    timeline = api_routes.query_timeline_data(conn, days=7)
    assert len(timeline) == 7
    active_days = [d for d in timeline if d["steps"] > 0]
    assert len(active_days) == 1
    assert active_days[0]["steps"] == 3
    assert active_days[0]["tokens"] == 1500
    assert active_days[0]["sessions"] == 1
    assert "test-project" in active_days[0]["projects"]

    # 3. Test query_activity_matrix
    matrix = api_routes.query_activity_matrix(conn)
    assert matrix["streak"]["total_active_days"] == 1
    total_matrix_steps = sum(sum(row) for row in matrix["hourly_aggregate"])
    assert total_matrix_steps == 3

    conn.close()

def test_pragma_data_version_cache_invalidation(tmp_path):
    db_file = tmp_path / "test_cache.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens)
        VALUES ('c1', 'Title 1', 'proj', 'Gemini Flash', '2026-08-01T12:00:00Z', 1, 100)
    """)
    conn.commit()

    compute_call_count = 0
    def compute_summary():
        nonlocal compute_call_count
        compute_call_count += 1
        return api_routes.query_summary_data(conn)

    # First call: computes
    res1 = get_cached_api_response(conn, "/api/summary", compute_summary)
    assert compute_call_count == 1
    data1 = json.loads(res1.decode("utf-8"))
    assert data1["total_convs"] == 1

    # Second call without DB modification: served from cache
    res2 = get_cached_api_response(conn, "/api/summary", compute_summary)
    assert compute_call_count == 1
    assert res1 == res2

    # Modify database from another connection (simulating ingest.py commit)
    conn2 = get_db_connection(db_file)
    conn2.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens)
        VALUES ('c2', 'Title 2', 'proj', 'Gemini Flash', '2026-08-01T13:00:00Z', 1, 200)
    """)
    conn2.commit()
    conn2.close()

    # Third call: cache is invalidated automatically via PRAGMA data_version!
    res3 = get_cached_api_response(conn, "/api/summary", compute_summary)
    assert compute_call_count == 2
    data3 = json.loads(res3.decode("utf-8"))
    assert data3["total_convs"] == 2

    conn.close()
