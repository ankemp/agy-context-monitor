import sqlite3
import json
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
        INSERT INTO steps (conversation_id, step_index, created_at, source, est_tokens, phase, model_name, category, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES 
        ('conv-1', 1, '{today_str}T10:15:00Z', 'USER_INPUT', 500, 'discovery', 'Gemini Pro', 'input', 0.001, 0, 0),
        ('conv-1', 2, '{today_str}T10:30:00Z', 'MODEL', 600, 'execution', 'Gemini Pro', 'code', 0.002, 500, 100),
        ('conv-1', 3, '{today_str}T11:05:00Z', 'MODEL', 400, 'execution', 'Gemini Pro', 'code', 0.001, 800, 100)
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
    assert summary["models"][0]["tokens"] == 1500

    # 2. Test query_timeline_data
    timeline = api_routes.query_timeline_data(conn, days=7, tz="UTC")
    assert len(timeline) == 7
    active_days = [d for d in timeline if d["steps"] > 0]
    assert len(active_days) == 1
    assert active_days[0]["steps"] == 3
    assert active_days[0]["tokens"] == 1500
    assert active_days[0]["sessions"] == 1
    assert "test-project" in active_days[0]["projects"]

    # 3. Test query_activity_matrix
    matrix = api_routes.query_activity_matrix(conn, tz="UTC")
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


def test_project_filter_url_sync_and_chip_removal(tmp_path):
    from jinja2 import Environment, FileSystemLoader
    from context_monitor.dashboard.server import DASHBOARD_DIR, export_standalone_html

    # 1. Verify template rendering
    env = Environment(loader=FileSystemLoader(str(DASHBOARD_DIR / "templates")))
    template = env.get_template("index.html")
    html = template.render(IS_STATIC_EXPORT=False)

    assert "costProjectFilterBadge" not in html
    assert "costProjectFilterText" not in html
    assert "Cost by Project" in html
    assert "costProjectSub" in html
    assert 'id="liveSyncBadge"' in html
    assert 'id="syncText"' in html
    assert 'id="liveStatus2"' not in html

    # 2. Verify script.js contents
    with open(DASHBOARD_DIR / "static" / "script.js", "r", encoding="utf-8") as f:
        js = f.read()
    assert "costProjectFilterBadge" not in js
    assert "getProjectFromUrl" in js
    assert "syncProjectToUrl" in js
    assert "popstate" in js

    # 3. Verify static export generation
    out_html = tmp_path / "export.html"
    export_standalone_html(out_html, include_conversations=False, tz="UTC")
    assert out_html.exists()
    content = out_html.read_text(encoding="utf-8")
    assert "costProjectFilterBadge" not in content
    assert "Cost by Project" in content
    assert "getProjectFromUrl" in content


def test_dashboard_compaction_and_modal_metrics(tmp_path):
    from context_monitor.dashboard.server import DASHBOARD_DIR
    from jinja2 import Environment, FileSystemLoader

    db_file = tmp_path / "test_compaction.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    # Insert root conversation with compaction
    cur.execute("""
        INSERT INTO conversations (
            id, title, project, model_name, created_at, total_steps, total_tokens,
            billed_cost, billed_input_tokens, billed_output_tokens, compaction_count, pruned_tokens
        )
        VALUES ('conv-root', 'Root Campaign', 'my-proj', 'Gemini Pro', '2026-10-07T10:00:00Z', 5, 25000, 0.05, 20000, 5000, 2, 45000)
    """)

    # Insert subagent conversation
    cur.execute("""
        INSERT INTO conversations (
            id, title, project, model_name, created_at, total_steps, total_tokens,
            billed_cost, billed_input_tokens, billed_output_tokens, parent_id, root_id, depth,
            subagent_role, subagent_type, invoking_step_index, compaction_count, pruned_tokens
        )
        VALUES ('conv-child', 'Sub Worker', 'my-proj', 'Gemini Flash', '2026-10-07T10:05:00Z', 3, 10000, 0.01, 8000, 2000, 'conv-root', 'conv-root', 1, 'Coder', 'self', 2, 1, 15000)
    """)

    # Insert sample steps
    cur.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, est_tokens, phase, model_name, category, billed_cost)
        VALUES
        ('conv-root', 1, '2026-10-07T10:01:00Z', 'USER_INPUT', 500, 'discovery', 'Gemini Pro', 'input', 0.001),
        ('conv-root', 2, '2026-10-07T10:02:00Z', 'SYSTEM', 20000, 'execution', 'Gemini Pro', 'System: Ephemeral Injections', 0.04),
        ('conv-root', 3, '2026-10-07T10:03:00Z', 'SYSTEM', 500, 'reasoning', 'Gemini Pro', 'System: Checkpoint', 0.001)
    """)
    conn.commit()

    # 1. Verify query_conversations_list returns compaction fields and fleet rollups
    convs = api_routes.query_conversations_list(conn, tz="UTC", view_mode="hierarchy")
    assert len(convs) == 1
    root = convs[0]
    assert root["id"] == "conv-root"
    assert root["compaction_count"] == 2
    assert root["pruned_tokens"] == 45000
    assert root["fleet_compaction_count"] == 3  # 2 root + 1 child
    assert root["fleet_pruned_tokens"] == 60000  # 45k root + 15k child
    assert len(root["subagents"]) == 1
    assert root["subagents"][0]["compaction_count"] == 1
    assert root["subagents"][0]["pruned_tokens"] == 15000

    # 2. Verify query_session_details returns tokens, total_steps, and compaction metadata
    details = api_routes.query_session_details(conn, "conv-root", tz="UTC")
    assert details is not None
    assert details["conversation"]["tokens"] == 25000
    assert details["conversation"]["total_steps"] == 5
    assert details["conversation"]["compaction_count"] == 2
    assert details["conversation"]["pruned_tokens"] == 45000
    assert details["compaction"]["has_compaction"] is True
    assert details["compaction"]["count"] == 2
    assert details["compaction"]["pruned_tokens"] == 45000

    # 3. Verify index.html template has modalCompactionBanner and modalCompactionCard
    env = Environment(loader=FileSystemLoader(str(DASHBOARD_DIR / "templates")))
    template = env.get_template("index.html")
    html_rendered = template.render(IS_STATIC_EXPORT=False)
    assert 'id="modalCompactionBanner"' in html_rendered
    assert 'id="modalCompactionCard"' in html_rendered
    assert 'id="modalCompactionScore"' in html_rendered

    # 4. Verify script.js handles modalTokens, modalSteps, and compaction
    with open(DASHBOARD_DIR / "static" / "script.js", "r", encoding="utf-8") as f:
        js_code = f.read()
    assert "modalTokensEl" in js_code
    assert "modalStepsEl" in js_code
    assert "modalCompactionBanner" in js_code
    assert "modalCompactionScore" in js_code

    conn.close()


def test_cache_invalidation_across_separate_request_connections(tmp_path):
    db_file = tmp_path / "test_multi_conn.db"
    init_db(db_file)

    conn_w1 = get_db_connection(db_file)
    conn_w1.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c1', 'T1', '2026-08-01T12:00:00Z', 1, 100)"
    )
    conn_w1.commit()
    conn_w1.close()

    call_count = 0

    def compute(c):
        nonlocal call_count
        call_count += 1
        return api_routes.query_summary_data(c)

    # Request 1: Fresh connection
    conn_r1 = get_db_connection(db_file)
    res1 = get_cached_api_response(conn_r1, "/api/summary", lambda: compute(conn_r1))
    conn_r1.close()
    data1 = json.loads(res1.decode("utf-8"))
    assert data1["total_convs"] == 1
    assert call_count == 1

    # Request 1.5: Another fresh connection WITHOUT external write -> must hit cache!
    conn_r1b = get_db_connection(db_file)
    res1b = get_cached_api_response(conn_r1b, "/api/summary", lambda: compute(conn_r1b))
    conn_r1b.close()
    assert res1b == res1
    assert call_count == 1, (
        "Fresh connection without DB writes must be served from cache!"
    )

    # External write (e.g. ingest.py)
    conn_w2 = get_db_connection(db_file)
    conn_w2.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c2', 'T2', '2026-08-01T13:00:00Z', 1, 200)"
    )
    conn_w2.commit()
    conn_w2.close()

    # Request 2: Another fresh connection (simulating HTTP request) -> must invalidate and recompute!
    conn_r2 = get_db_connection(db_file)
    res2 = get_cached_api_response(conn_r2, "/api/summary", lambda: compute(conn_r2))
    conn_r2.close()
    data2 = json.loads(res2.decode("utf-8"))
    assert data2["total_convs"] == 2
    assert call_count == 2, (
        "Fresh connection after DB write must invalidate cache and recompute!"
    )


def test_broadcast_sse_clears_api_cache(tmp_path):
    from context_monitor.dashboard.sse_stream import broadcast_sse

    db_file = tmp_path / "test_sse_cache.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    res = get_cached_api_response(conn, "/api/summary", lambda: {"total_convs": 42})
    assert b"42" in res
    assert "/api/summary" in _API_CACHE

    broadcast_sse("db_update", {"timestamp": "2026-10-14T00:00:00Z"})
    assert "/api/summary" not in _API_CACHE
    conn.close()


def test_clear_api_cache_directly(tmp_path):
    from context_monitor.dashboard.server import clear_api_cache

    db_file = tmp_path / "test_clear_cache.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    res = get_cached_api_response(conn, "/api/test", lambda: {"status": "ok"})
    assert b"ok" in res
    assert "/api/test" in _API_CACHE

    clear_api_cache()
    assert len(_API_CACHE) == 0
    conn.close()


def test_multithreaded_cache_access(tmp_path):
    import concurrent.futures

    db_file = tmp_path / "test_threaded_cache.db"
    init_db(db_file)

    def worker(i):
        conn = get_db_connection(db_file)
        res = get_cached_api_response(
            conn, "/api/summary", lambda: api_routes.query_summary_data(conn)
        )
        conn.close()
        return json.loads(res.decode("utf-8"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            assert res["total_convs"] == 0

    # Write from another connection
    conn_w = get_db_connection(db_file)
    conn_w.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c1', 'T1', '2026-08-01T12:00:00Z', 1, 100)"
    )
    conn_w.commit()
    conn_w.close()

    # Re-query across threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            assert res["total_convs"] == 1


def test_cache_with_in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x int);")

    count = 0

    def compute():
        nonlocal count
        count += 1
        return {"count": count}

    res1 = get_cached_api_response(conn, "/api/mem", compute)
    assert count == 1

    res2 = get_cached_api_response(conn, "/api/mem", compute)
    assert count == 1
    assert res1 == res2

    # In-memory database modification must invalidate cache
    conn.execute("INSERT INTO t VALUES (42);")
    conn.commit()
    res3 = get_cached_api_response(conn, "/api/mem", compute)
    assert count == 2
    assert res3 != res2

    # In-memory schema change must also invalidate cache
    conn.execute("CREATE TABLE t2 (y text);")
    conn.commit()
    res4 = get_cached_api_response(conn, "/api/mem", compute)
    assert count == 3
    assert res4 != res3

    conn.close()


def test_db_change_watcher_string_path_and_lifecycle(tmp_path):
    from context_monitor.dashboard.sse_stream import (
        DbChangeWatcher,
        register_change_callback,
        unregister_change_callback,
    )
    import time

    db_file = tmp_path / "test_watcher.db"
    init_db(db_file)

    # Verify string path does not raise AttributeError
    watcher = DbChangeWatcher(str(db_file), check_interval=0.1)
    triggered = []

    def on_change():
        triggered.append(time.time())

    register_change_callback(on_change)
    try:
        watcher.start()
        time.sleep(0.15)

        # Modify database
        conn = get_db_connection(db_file)
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('cw1', 'W', '2026-08-01T12:00:00Z', 1, 10)"
        )
        conn.commit()
        conn.close()

        # Wait for watcher to detect change
        deadline = time.time() + 2.0
        while not triggered and time.time() < deadline:
            time.sleep(0.05)

        assert len(triggered) > 0, (
            "DbChangeWatcher must fire callback when DB file changes"
        )
    finally:
        watcher.stop()
        unregister_change_callback(on_change)


def test_monitor_connection_refreshes_on_file_recreation(tmp_path):
    db_file = tmp_path / "test_recreate.db"
    init_db(db_file)

    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c1', 'T1', '2026-08-01T12:00:00Z', 1, 100)"
    )
    conn.commit()
    conn.close()

    conn_r1 = get_db_connection(db_file)
    res1 = get_cached_api_response(
        conn_r1, "/api/summary", lambda: api_routes.query_summary_data(conn_r1)
    )
    conn_r1.close()
    assert json.loads(res1)["total_convs"] == 1

    # Remove file and recreate at exact same path
    db_file.unlink()
    init_db(db_file)
    conn_w = get_db_connection(db_file)
    conn_w.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c2', 'T2', '2026-08-01T13:00:00Z', 1, 200)"
    )
    conn_w.execute(
        "INSERT INTO conversations (id, title, created_at, total_steps, total_tokens) VALUES ('c3', 'T3', '2026-08-01T14:00:00Z', 1, 300)"
    )
    conn_w.commit()
    conn_w.close()

    # Re-querying must detect the replaced inode, refresh monitor, and return fresh count
    conn_r2 = get_db_connection(db_file)
    res2 = get_cached_api_response(
        conn_r2, "/api/summary", lambda: api_routes.query_summary_data(conn_r2)
    )
    conn_r2.close()
    assert json.loads(res2)["total_convs"] == 2


def test_concurrent_multi_db_cache_isolation(tmp_path):
    db1 = tmp_path / "iso1.db"
    db2 = tmp_path / "iso2.db"
    init_db(db1)
    init_db(db2)

    c1 = get_db_connection(db1)
    c2 = get_db_connection(db2)

    def slow_compute_db1():
        # While computing DB1, query DB2
        res_db2 = get_cached_api_response(c2, "/api/iso", lambda: {"db": "db2"})
        assert json.loads(res_db2)["db"] == "db2"
        return {"db": "db1"}

    res_db1 = get_cached_api_response(c1, "/api/iso", slow_compute_db1)
    assert json.loads(res_db1)["db"] == "db1"

    # Query DB2 again — must NOT return DB1's payload
    res_db2_again = get_cached_api_response(c2, "/api/iso", lambda: {"db": "db2_fresh"})
    assert json.loads(res_db2_again)["db"] != "db1", (
        "DB2 must not receive DB1's response"
    )

    c1.close()
    c2.close()


def test_hourly_step_rollups_and_indexes(tmp_path):
    from context_monitor.db import refresh_conversation_rollups, ensure_rollups_synced

    db_file = tmp_path / "test_rollups.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    # 1. Verify table and indexes exist
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "hourly_step_rollups" in tables

    indexes = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
    assert "idx_steps_model_billed" in indexes
    assert "idx_steps_created_cost" in indexes
    assert "idx_rollups_conv" in indexes

    # 2. Insert sample steps across hours
    cur.execute("""
        INSERT INTO conversations (id, title, model_name, created_at, total_steps, total_tokens)
        VALUES ('c1', 'Rollup Conv', 'Gemini Pro', '2026-10-18T10:00:00Z', 3, 1500)
    """)
    cur.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES
        ('c1', 1, '2026-10-18T10:15:00Z', 'Gemini Pro', 500, 0.001, 100, 50),
        ('c1', 2, '2026-10-18T10:45:00Z', 'Gemini Pro', 400, 0.002, 200, 100),
        ('c1', 3, '2026-10-18T11:10:00Z', 'Gemini Pro', 600, 0.003, 300, 150)
    """)
    conn.commit()

    # 3. Test refresh_conversation_rollups
    refresh_conversation_rollups(conn, "c1")
    rows = cur.execute("SELECT hour_key, step_count, est_tokens, billed_tokens, billed_cost FROM hourly_step_rollups WHERE conversation_id = 'c1' ORDER BY hour_key ASC").fetchall()
    assert len(rows) == 2
    assert rows[0]["hour_key"] == "2026-10-18T10"
    assert rows[0]["step_count"] == 2
    assert rows[0]["est_tokens"] == 900
    assert rows[0]["billed_tokens"] == 450
    assert abs(rows[0]["billed_cost"] - 0.003) < 1e-6

    assert rows[1]["hour_key"] == "2026-10-18T11"
    assert rows[1]["step_count"] == 1
    assert rows[1]["est_tokens"] == 600
    assert rows[1]["billed_tokens"] == 450
    assert abs(rows[1]["billed_cost"] - 0.003) < 1e-6

    # 4. Insert steps directly into steps table (simulating direct insert in tests)
    cur.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES ('c1', 4, '2026-10-18T12:00:00Z', 'Gemini Pro', 300, 0.001, 50, 50)
    """)
    conn.commit()

    # Rollups should be out of date
    rollup_cnt = cur.execute("SELECT COALESCE(SUM(step_count), 0) FROM hourly_step_rollups").fetchone()[0]
    step_cnt = cur.execute("SELECT COUNT(*) FROM steps WHERE created_at IS NOT NULL AND created_at != ''").fetchone()[0]
    assert rollup_cnt == 3
    assert step_cnt == 4

    # ensure_rollups_synced brings it back into sync and commits
    ensure_rollups_synced(conn)
    conn.close()

    # Verify a new connection sees the synced rollups (not rolled back on close)
    conn2 = get_db_connection(db_file)
    rollup_cnt2 = conn2.execute("SELECT COALESCE(SUM(step_count), 0) FROM hourly_step_rollups").fetchone()[0]
    assert rollup_cnt2 == 4
    conn2.close()


def test_legacy_tools_blocks_cleanup(tmp_path):
    db_file = tmp_path / "test_cleanup.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Insert fake legacy tools block
    conn.execute("""
        INSERT INTO conversations (id, title) VALUES ('conv-legacy', 'Legacy Conv')
    """)
    conn.execute("""
        INSERT INTO transcript_blocks (conversation_id, step_index, block_type, content, block_hash)
        VALUES ('conv-legacy', 0, 'tools', '{"some": "schema"}', 'hash-123')
    """)
    conn.commit()

    cnt = conn.execute("SELECT COUNT(*) FROM transcript_blocks WHERE block_type = 'tools'").fetchone()[0]
    assert cnt == 1

    # Running init_db must clean up legacy tools blocks
    init_db(conn)
    cnt_after = conn.execute("SELECT COUNT(*) FROM transcript_blocks WHERE block_type = 'tools'").fetchone()[0]
    assert cnt_after == 0

    conn.close()


def test_rollup_query_plans_and_calibrate_capacity(tmp_path):
    from context_monitor.quota import calibrate_capacity, get_hourly_activity_profile
    from context_monitor.dashboard import api_routes

    db_file = tmp_path / "test_rollup_queries.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    cur = conn.cursor()

    # 1. Insert conversations and steps
    cur.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens)
        VALUES ('conv-1', 'Conv 1', 'project-a', 'Gemini Pro', '2026-10-18T10:00:00Z', 2, 1000)
    """)
    cur.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
        VALUES 
        ('conv-1', 1, '2026-10-18T10:15:00Z', 'Gemini Pro', 400, 0.05, 200, 100),
        ('conv-1', 2, '2026-10-18T10:45:00Z', 'Gemini Pro', 600, 0.07, 400, 150)
    """)
    conn.commit()

    # 2. Test calibrate_capacity with GROUP BY model_name
    # Verify EXPLAIN QUERY PLAN uses idx_steps_created_cost
    eqp = cur.execute(
        "EXPLAIN QUERY PLAN SELECT model_name, SUM(billed_cost) FROM steps INDEXED BY idx_steps_created_cost WHERE created_at >= ? GROUP BY model_name",
        ("2026-10-18T00:00:00",)
    ).fetchall()
    eqp_str = " ".join([str(tuple(r)) for r in eqp])
    assert "idx_steps_created_cost" in eqp_str, f"Query plan did not use idx_steps_created_cost: {eqp_str}"

    # Verify calibrate_capacity works with GROUP BY model_name
    # (used_fraction = 1 - 0.90 = 0.10, cost = 0.12, 0.12 / 0.10 = 1.20 -> clamped to min bounds for gemini-weekly: 350.0)
    calibrated = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time="2026-10-21T02:00:00Z",
        fallback_capacity=672.0,
    )
    assert calibrated >= 350.0

    # 3. Test query_timeline_data and query_activity_matrix auto-sync and query hourly_step_rollups
    executed_queries: list[tuple[str, tuple]] = []

    class SpyCursor:
        def __init__(self, r):
            self._r = r

        def execute(self, s, p=()):
            executed_queries.append((s, p))
            return self._r.execute(s, p)

        def fetchall(self):
            return self._r.fetchall()

        def fetchone(self):
            return self._r.fetchone()

        def __getattr__(self, n):
            return getattr(self._r, n)

    class SpyConn:
        def __init__(self, r):
            self._r = r

        def cursor(self):
            return SpyCursor(self._r.cursor())

        def execute(self, s, p=()):
            executed_queries.append((s, p))
            return self._r.execute(s, p)

        def __getattr__(self, n):
            return getattr(self._r, n)

    spy = SpyConn(conn)
    timeline = api_routes.query_timeline_data(spy, days=7, tz="UTC")
    assert any(d["steps"] == 2 for d in timeline)
    assert any("FROM hourly_step_rollups" in s for s, _ in executed_queries)

    # Verify query plan for bounded timeline query uses index
    tl_sql, tl_params = next((s, p) for s, p in executed_queries if "FROM hourly_step_rollups" in s and "hour_key >= ?" in s)
    tl_plan = cur.execute(f"EXPLAIN QUERY PLAN {tl_sql}", tl_params).fetchall()
    assert any("SEARCH" in r["detail"] and "hourly_step_rollups" in r["detail"] for r in tl_plan)

    executed_queries.clear()
    matrix = api_routes.query_activity_matrix(spy, tz="UTC")
    total_steps = sum(sum(r) for r in matrix["hourly_aggregate"])
    assert total_steps == 2
    assert any("FROM hourly_step_rollups" in s for s, _ in executed_queries)

    # 4. Test get_hourly_activity_profile reads from rollups
    executed_queries.clear()
    profile = get_hourly_activity_profile(spy, force_refresh=True)
    assert profile["total_steps"] == 2
    assert any("FROM hourly_step_rollups" in s for s, _ in executed_queries)

    # 5. Test fallback when hourly_step_rollups table is dropped (verifies sqlite3.OperationalError handling & no NameError)
    conn.execute("DROP TABLE hourly_step_rollups")
    conn.commit()

    matrix_fallback = api_routes.query_activity_matrix(conn, tz="UTC")
    total_steps_fallback = sum(sum(r) for r in matrix_fallback["hourly_aggregate"])
    assert total_steps_fallback == 2

    timeline_fallback = api_routes.query_timeline_data(conn, days=7, tz="UTC")
    assert any(d["steps"] == 2 for d in timeline_fallback)

    profile_fallback = get_hourly_activity_profile(conn, force_refresh=True)
    assert profile_fallback["total_steps"] == 2

    conn.close()


def test_single_flight_api_cache(tmp_path):
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db_file = tmp_path / "test_single_flight.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    call_count = 0
    lock = threading.Lock()

    def slow_compute():
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.05)
        return {"result": 42}

    # Launch 8 concurrent threads requesting the exact same cache_key simultaneously
    def worker(worker_id):
        c = get_db_connection(db_file)
        try:
            res = get_cached_api_response(c, "/api/heavy-endpoint", slow_compute)
            data = json.loads(res.decode("utf-8"))
            assert data["result"] == 42
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, i) for i in range(8)]
        for f in futures:
            f.result()

    # With single-flight coalescing, slow_compute must be invoked exactly ONCE
    assert call_count == 1, f"Expected single-flight compute_fn to be called 1 time, but called {call_count} times"

    conn.close()


def test_server_transparent_gzip(tmp_path):
    import io
    import gzip
    from context_monitor.dashboard.server import DashboardHTTPHandler

    db_file = tmp_path / "test_gzip.db"
    init_db(db_file)

    # Subclass handler to capture response without running a real socket server
    class DummyHandler(DashboardHTTPHandler):
        def __init__(self, request_headers):
            self.headers = request_headers
            self.wfile = io.BytesIO()
            self._headers_sent = {}
            self.status_code = None

        def send_response(self, code, message=None):
            self.status_code = code

        def send_header(self, keyword, value):
            self._headers_sent[keyword.lower()] = value

        def end_headers(self):
            pass

    # 1. Payload >= 1024 bytes with Accept-Encoding: gzip -> must compress
    large_payload = b'{"data": "' + (b'x' * 2000) + b'"}'
    handler_gzip = DummyHandler({"Accept-Encoding": "gzip, deflate, br"})
    handler_gzip._send_bytes(large_payload, "application/json")

    assert handler_gzip.status_code == 200
    assert handler_gzip._headers_sent.get("content-encoding") == "gzip"
    compressed_bytes = handler_gzip.wfile.getvalue()
    assert len(compressed_bytes) < len(large_payload)
    decompressed = gzip.decompress(compressed_bytes)
    assert decompressed == large_payload

    # 2. Payload >= 1024 bytes without Accept-Encoding: gzip -> no compression
    handler_nogzip = DummyHandler({})
    handler_nogzip._send_bytes(large_payload, "application/json")
    assert handler_nogzip.status_code == 200
    assert "content-encoding" not in handler_nogzip._headers_sent
    assert handler_nogzip.wfile.getvalue() == large_payload

    # 3. Payload < 1024 bytes even with Accept-Encoding: gzip -> no compression
    small_payload = b'{"status": "ok"}'
    handler_small = DummyHandler({"Accept-Encoding": "gzip"})
    handler_small._send_bytes(small_payload, "application/json")
    assert "content-encoding" not in handler_small._headers_sent
    assert handler_small.wfile.getvalue() == small_payload


def test_quota_write_suppression_and_ls_cache(tmp_path):
    from context_monitor.quota import sync_and_get_quota_state

    db_file = tmp_path / "test_suppression.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    observer = get_db_connection(db_file)

    fake_raw = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.85,
                        "resetTime": "2026-10-25T02:00:00Z",
                    }
                ],
            }
        ]
    }

    # First fetch: empty DB -> must write into DB
    q1 = sync_and_get_quota_state(conn=conn, force_refresh=True, ls_fetcher=lambda: fake_raw)
    assert len(q1) >= 1
    tc1 = conn.total_changes
    dv1 = observer.execute("PRAGMA data_version;").fetchone()[0]

    # Second fetch with identical values and force_refresh=True: must suppress write!
    q2 = sync_and_get_quota_state(conn=conn, force_refresh=True, ls_fetcher=lambda: fake_raw)
    assert len(q2) >= 1
    tc2 = conn.total_changes
    dv2 = observer.execute("PRAGMA data_version;").fetchone()[0]
    # Data version must NOT change because write was suppressed
    assert tc1 == tc2, "Expected conn.total_changes to remain unchanged on identical quota state"
    assert dv1 == dv2, "Expected PRAGMA data_version to remain unchanged on identical quota state"

    # Third fetch with changed remaining_fraction: must write into DB
    fake_changed = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.80,  # Changed!
                        "resetTime": "2026-10-25T02:00:00Z",
                    }
                ],
            }
        ]
    }
    q3 = sync_and_get_quota_state(conn=conn, force_refresh=True, ls_fetcher=lambda: fake_changed)
    assert len(q3) >= 1
    tc3 = conn.total_changes
    dv3 = observer.execute("PRAGMA data_version;").fetchone()[0]
    assert tc3 > tc2, "Expected conn.total_changes to increase when quota state changed"
    assert dv3 > dv2, "Expected PRAGMA data_version to change when quota state changed"

    observer.close()
    conn.close()


def test_cycle_mode_uses_hourly_step_rollups_and_fallback(tmp_path):
    from datetime import datetime, timezone, timedelta
    from context_monitor.dashboard import api_routes
    from context_monitor.db import upsert_quota_state, refresh_conversation_rollups

    db_file = tmp_path / "test_cycle_rollups.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    reset_utc = now_utc + timedelta(days=3)
    start_utc = reset_utc - timedelta(days=7)

    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "remaining_fraction": 0.5,
                "reset_time": reset_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "calibrated_capacity": 672.0,
                "fetched_at": now_utc.isoformat(),
            }
        ],
    )
    conn.execute(
        "INSERT INTO conversations (id, title, model_name, created_at) VALUES ('c1', 'Conv 1', 'Gemini Flash', ?)",
        (now_utc.isoformat(),),
    )
    step_ts = (start_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO steps (
            conversation_id, step_index, phase, category, activity,
            tool_or_source, model_name, est_tokens, detail, created_at,
            billed_cost, billed_input_tokens, billed_output_tokens
        ) VALUES ('c1', 1, 'exec', 'cat', 'act', 't', 'm', 100, '', ?, 0.05, 500, 200)
        """,
        (step_ts,),
    )
    conn.commit()
    refresh_conversation_rollups(conn, "c1")

    executed_queries: list[tuple[str, tuple]] = []

    class SpyCursor:
        def __init__(self, r):
            self._r = r

        def execute(self, s, p=()):
            executed_queries.append((s, p))
            return self._r.execute(s, p)

        def fetchall(self):
            return self._r.fetchall()

        def fetchone(self):
            return self._r.fetchone()

        def __getattr__(self, n):
            return getattr(self._r, n)

    class SpyConn:
        def __init__(self, r):
            self._r = r

        def cursor(self):
            return SpyCursor(self._r.cursor())

        def execute(self, s, p=()):
            executed_queries.append((s, p))
            return self._r.execute(s, p)

        def __getattr__(self, n):
            return getattr(self._r, n)

    spy = SpyConn(conn)
    timeline = api_routes.query_timeline_data(spy, days="cycle", tz="UTC")
    assert len(timeline) > 0

    # 1. Verify hourly_step_rollups was queried with hour_key >= ? and hour_key < ?
    rollup_query = next(
        (s, p) for s, p in executed_queries if "FROM hourly_step_rollups" in s and "hour_key >= ?" in s
    )
    assert "hour_key >= ?" in rollup_query[0]
    assert "hour_key < ?" in rollup_query[0]
    # Check start_hour and end_hour formatting '%Y-%m-%dT%H'
    expected_start_hour = start_utc.strftime("%Y-%m-%dT%H")
    expected_end_hour = reset_utc.strftime("%Y-%m-%dT%H")
    assert expected_start_hour in rollup_query[1]
    assert expected_end_hour in rollup_query[1]

    # 2. Verify fallback to steps on OperationalError
    conn.execute("DROP TABLE hourly_step_rollups")
    conn.commit()
    executed_queries.clear()

    timeline_fb = api_routes.query_timeline_data(spy, days="cycle", tz="UTC")
    assert len(timeline_fb) > 0
    fallback_query = next(
        (s, p) for s, p in executed_queries if "FROM steps" in s and "GROUP BY hour_key" in s
    )
    assert fallback_query is not None

    conn.close()


def test_ensure_rollups_synced_o1_check_and_cache(tmp_path):
    from context_monitor.db import ensure_rollups_synced

    db_file = tmp_path / "test_o1_sync.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Insert a conversation and step
    conn.execute(
        "INSERT INTO conversations (id, title, created_at) VALUES ('c1', 'Conv 1', '2026-10-18T10:00:00Z')"
    )
    conn.execute(
        """
        INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_cost)
        VALUES ('c1', 1, '2026-10-18T10:15:00Z', 'Gemini Pro', 100, 0.01)
        """
    )
    conn.commit()

    # Rollups table is empty initially (simulate new migration with data in steps)
    conn.execute("DELETE FROM hourly_step_rollups")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM hourly_step_rollups").fetchone()[0] == 0

    # ensure_rollups_synced does the initial build
    ensure_rollups_synced(conn)
    assert conn.execute("SELECT COUNT(*) FROM hourly_step_rollups").fetchone()[0] == 1

    # Call it again without any DB changes: must hit the O(1) cache check without queries to steps
    executed_queries = []

    class SpyCursor:
        def __init__(self, r):
            self._r = r

        def execute(self, s, p=()):
            executed_queries.append(s)
            return self._r.execute(s, p)

        def fetchall(self):
            return self._r.fetchall()

        def fetchone(self):
            return self._r.fetchone()

        def __getattr__(self, n):
            return getattr(self._r, n)

    class SpyConn:
        def __init__(self, r):
            self._r = r

        def cursor(self):
            return SpyCursor(self._r.cursor())

        def execute(self, s, p=()):
            executed_queries.append(s)
            return self._r.execute(s, p)

        def __getattr__(self, n):
            return getattr(self._r, n)

    spy = SpyConn(conn)
    ensure_rollups_synced(spy)

    # Verify no full scan of steps table occurred
    assert not any("FROM steps" in q for q in executed_queries)
    conn.close()

    # Verify a brand new connection also hits the O(1) cache check without scanning steps
    conn2 = get_db_connection(db_file)
    executed_queries.clear()
    spy2 = SpyConn(conn2)
    ensure_rollups_synced(spy2)
    assert not any("FROM steps" in q for q in executed_queries)
    conn2.close()


def test_init_db_user_version_avoids_blocks_scan(tmp_path):
    db_file = tmp_path / "test_user_ver.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Insert a block so transcript_blocks is not empty
    conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Conv 1')")
    conn.execute(
        "INSERT INTO transcript_blocks (conversation_id, step_index, block_type, content, block_hash) "
        "VALUES ('c1', 0, 'user', 'hello', 'hash1')"
    )
    conn.commit()

    # Run init_db to set user_version = 2
    init_db(conn)
    ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert ver >= 2

    # Spy on subsequent init_db to verify it skips transcript_blocks scan
    executed_queries = []

    class SpyCursor:
        def __init__(self, r):
            self._r = r

        def execute(self, s, p=()):
            executed_queries.append(s)
            return self._r.execute(s, p)

        def fetchall(self):
            return self._r.fetchall()

        def fetchone(self):
            return self._r.fetchone()

        def __getattr__(self, n):
            return getattr(self._r, n)

    class SpyConn:
        def __init__(self, r):
            self._r = r

        def cursor(self):
            return SpyCursor(self._r.cursor())

        def execute(self, s, p=()):
            executed_queries.append(s)
            return self._r.execute(s, p)

        def __getattr__(self, n):
            return getattr(self._r, n)

    spy = SpyConn(conn)
    init_db(spy)

    # No query should search transcript_blocks for tools
    assert not any("block_type = 'tools'" in q for q in executed_queries)
    conn.close()


def test_clear_api_cache_cleans_inflight_keys(tmp_path):
    import threading
    from context_monitor.dashboard.server import _INFLIGHT_KEYS, _INFLIGHT_LOCK, clear_api_cache

    with _INFLIGHT_LOCK:
        _INFLIGHT_KEYS["dummy:key"] = threading.Lock()
        assert len(_INFLIGHT_KEYS) > 0

    clear_api_cache()

    with _INFLIGHT_LOCK:
        assert len(_INFLIGHT_KEYS) == 0


