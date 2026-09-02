import pytest
import sqlite3
from pathlib import Path

from context_monitor.db import get_db_connection, init_db
from context_monitor.dashboard.api_routes import query_cost_by_project, query_summary_data

def test_query_cost_by_project_empty(tmp_path):
    db_file = tmp_path / "test_empty.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    
    res = query_cost_by_project(conn)
    conn.close()
    
    assert res["total_cost"] == 0.0
    assert res["projects"] == []

def test_query_cost_by_project_aggregation(tmp_path):
    db_file = tmp_path / "test_cost_proj.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    
    # Insert conversations with various projects and costs
    convs = [
        ('c1', 'Conv 1', 'agy-context-monitor', 'Gemini Flash', '2026-08-20T10:00:00Z', 10, 50000, 1.50, 40000, 10000),
        ('c2', 'Conv 2', 'agy-context-monitor', 'Gemini Pro',   '2026-08-21T10:00:00Z', 5,  25000, 2.50, 20000, 5000),
        ('c3', 'Conv 3', 'ledger',              'Gemini Flash', '2026-08-22T10:00:00Z', 20, 100000, 6.00, 80000, 20000),
        ('c4', 'Conv 4', None,                  'Gemini Flash', '2026-08-23T10:00:00Z', 2,  10000, 0.50, 8000, 2000),
        ('c5', 'Conv 5', '',                    'Gemini Flash', '2026-08-24T10:00:00Z', 2,  10000, 0.50, 8000, 2000),
    ]
    
    conn.executemany("""
        INSERT INTO conversations (
            id, title, project, model_name, created_at, total_steps, total_tokens,
            billed_cost, billed_input_tokens, billed_output_tokens
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, convs)
    conn.commit()
    
    res = query_cost_by_project(conn)
    conn.close()
    
    assert res["total_cost"] == 11.0
    projects = res["projects"]
    assert len(projects) == 3  # ledger, agy-context-monitor, (unassigned)
    
    # Should be sorted by cost DESC
    assert projects[0]["project"] == "ledger"
    assert projects[0]["cost"] == 6.0
    assert projects[0]["sessions"] == 1
    assert projects[0]["percentage"] == round(6.0 / 11.0 * 100, 1)
    
    assert projects[1]["project"] == "agy-context-monitor"
    assert projects[1]["cost"] == 4.0
    assert projects[1]["sessions"] == 2
    assert projects[1]["tokens"] == 75000
    assert projects[1]["percentage"] == round(4.0 / 11.0 * 100, 1)
    
    assert projects[2]["project"] == "(unassigned)"
    assert projects[2]["cost"] == 1.0
    assert projects[2]["sessions"] == 2
    assert projects[2]["percentage"] == round(1.0 / 11.0 * 100, 1)

    # Top sessions globally
    top_sessions = res["top_sessions"]
    assert len(top_sessions) == 5
    assert top_sessions[0]["id"] == "c3"
    assert top_sessions[0]["billed_cost"] == 6.0
    assert top_sessions[0]["project"] == "ledger"
    assert top_sessions[1]["id"] == "c2"
    assert top_sessions[1]["billed_cost"] == 2.50

def test_query_cost_by_project_top_sessions_filtered(tmp_path):
    db_file = tmp_path / "test_cost_proj_filter.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    
    convs = [
        ('c1', 'Conv 1', 'agy-context-monitor', 'Gemini Flash', '2026-08-20T10:00:00Z', 10, 50000, 1.50, 40000, 10000),
        ('c2', 'Conv 2', 'agy-context-monitor', 'Gemini Pro',   '2026-08-21T10:00:00Z', 5,  25000, 2.50, 20000, 5000),
        ('c3', 'Conv 3', 'ledger',              'Gemini Flash', '2026-08-22T10:00:00Z', 20, 100000, 6.00, 80000, 20000),
        ('c4', 'Conv 4', None,                  'Gemini Flash', '2026-08-23T10:00:00Z', 2,  10000, 0.50, 8000, 2000),
    ]
    conn.executemany("""
        INSERT INTO conversations (
            id, title, project, model_name, created_at, total_steps, total_tokens,
            billed_cost, billed_input_tokens, billed_output_tokens
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, convs)
    conn.commit()

    # When filtered to agy-context-monitor
    res_agy = query_cost_by_project(conn, project="agy-context-monitor")
    assert len(res_agy["projects"]) == 3  # All projects still returned for the chart!
    assert len(res_agy["top_sessions"]) == 2  # Only agy-context-monitor sessions!
    assert res_agy["top_sessions"][0]["id"] == "c2"
    assert res_agy["top_sessions"][1]["id"] == "c1"

    # When filtered to (unassigned)
    res_unassigned = query_cost_by_project(conn, project="(unassigned)")
    assert len(res_unassigned["top_sessions"]) == 1
    assert res_unassigned["top_sessions"][0]["id"] == "c4"
    conn.close()


def test_query_summary_data_with_unassigned_filter(tmp_path):
    db_file = tmp_path / "test_unassigned_filter.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    
    conn.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens, billed_cost)
        VALUES ('c1', 'Conv 1', 'ledger', 'Gemini Flash', '2026-08-20T10:00:00Z', 10, 50000, 5.00)
    """)
    conn.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens, billed_cost)
        VALUES ('c2', 'Conv 2', NULL, 'Gemini Flash', '2026-08-21T10:00:00Z', 4, 20000, 1.00)
    """)
    conn.commit()
    
    summary_all = query_summary_data(conn)
    summary_ledger = query_summary_data(conn, project="ledger")
    summary_unassigned = query_summary_data(conn, project="(unassigned)")
    conn.close()
    
    assert summary_all["total_convs"] == 2
    assert summary_all["total_billed_cost"] == 6.0
    
    assert summary_ledger["total_convs"] == 1
    assert summary_ledger["total_billed_cost"] == 5.0
    
    assert summary_unassigned["total_convs"] == 1
    assert summary_unassigned["total_billed_cost"] == 1.0

def test_export_standalone_html_includes_cost_by_project(tmp_path, monkeypatch):
    from context_monitor.dashboard.server import export_standalone_html
    db_file = tmp_path / "test_export.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute("""
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens, billed_cost)
        VALUES ('c1', 'Conv 1', 'agy-context-monitor', 'Gemini Flash', '2026-08-20T10:00:00Z', 10, 50000, 3.50)
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr("context_monitor.dashboard.server.get_db_connection", lambda: get_db_connection(db_file))
    out_html = tmp_path / "report.html"
    export_standalone_html(out_html)

    assert out_html.exists()
    content = out_html.read_text(encoding="utf-8")
    assert "cost_by_project" in content
    assert "agy-context-monitor" in content
    assert "/api/cost-by-project" in content

