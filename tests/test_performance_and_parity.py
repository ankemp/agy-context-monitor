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
