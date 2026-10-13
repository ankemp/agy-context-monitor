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


