"""
Tests for Dashboard Quota Integration, API Routes, SSE Stream, and Frontend Invariants
--------------------------------------------------------------------------------------
Covers /api/quota, /events, /api/stream, /api/conversations/<id>, summary & session quota metrics,
zero polling invariant, static charts invariant, and standalone HTML export.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from context_monitor.dashboard import api_routes, server
from context_monitor.db import get_db_connection, init_db, upsert_quota_state


@pytest.fixture
def test_db(tmp_path: Path):
    """Creates a temporary SQLite test database initialized with full schema and fresh quota state."""
    db_file = tmp_path / "test_dashboard.db"
    init_db(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_file) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.85,
                    "reset_time": "2026-10-21T02:27:22Z",
                    "calibrated_capacity": 700.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "gemini-5h",
                    "group_name": "Gemini Models",
                    "window": "5h",
                    "remaining_fraction": 0.90,
                    "reset_time": "2026-10-14T18:00:00Z",
                    "calibrated_capacity": 250.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "3p-weekly",
                    "group_name": "Claude and GPT models",
                    "window": "weekly",
                    "remaining_fraction": 0.95,
                    "reset_time": "2026-10-21T02:27:22Z",
                    "calibrated_capacity": 200.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "3p-5h",
                    "group_name": "Claude and GPT models",
                    "window": "5h",
                    "remaining_fraction": 0.98,
                    "reset_time": "2026-10-14T18:00:00Z",
                    "calibrated_capacity": 70.0,
                    "fetched_at": now_iso,
                },
            ],
        )

        # Seed conversations: 1 root thread and 1 subagent
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'root-conv-1', 'Root Task Conversation', 'test-proj', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:00:00Z', '2026-10-14T10:30:00Z',
                10, 50000, 7.00, 40000, 10000,
                NULL, 'root-conv-1', 0, NULL, NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'sub-conv-1', 'Subagent Worker 1', 'test-proj', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:05:00Z', '2026-10-14T10:25:00Z',
                5, 20000, 3.50, 15000, 5000,
                'root-conv-1', 'root-conv-1', 1, 'code-reviewer', 'subagent'
            )
            """
        )

        # Seed steps for root conversation
        for idx in range(1, 4):
            conn.execute(
                """
                INSERT INTO steps (
                    conversation_id, step_index, created_at, source, step_type,
                    category, activity, tool_or_source, est_tokens, thinking_tokens,
                    phase, model_name, billed_cost, billed_input_tokens, billed_output_tokens
                ) VALUES (?, ?, '2026-10-14T10:00:00Z', 'assistant', 'PLANNER_RESPONSE',
                          'Agent Reasoning & Text', 'Reasoning', 'Planner', 5000, 500,
                          'execution', 'Gemini 3.8 Flash (High)', ?, 4000, 1000)
                """,
                ("root-conv-1", idx, 2.0 * idx),
            )
        conn.commit()
    return db_file


def test_query_quota_data(test_db: Path):
    """Verifies query_quota_data returns valid bucket structures and model-routed metrics."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(test_db) as conn:
            q_data = api_routes.query_quota_data(
                conn, model_name="Gemini 3.8 Flash (High)", billed_cost=7.0
            )

    assert "gemini-weekly" in q_data
    assert "gemini-5h" in q_data
    assert "3p-weekly" in q_data
    assert "3p-5h" in q_data

    # Gemini weekly capacity calibrated to 700.0 in test_db
    assert q_data["gemini-weekly"]["calibrated_capacity"] == 700.0
    assert q_data["gemini-weekly"]["quota_remaining_pct"] == 85.0
    assert q_data["gemini-weekly"]["turn_pct"] == 1.0  # 7.0 / 700.0 * 100

    # 3P model routing check
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(test_db) as conn:
            q_claude = api_routes.query_quota_data(
                conn, model_name="Claude 3.7 Sonnet", billed_cost=4.0
            )
    assert q_claude["pricing_key"] == "claude"
    assert q_claude["bucket_prefix"] == "3p"
    assert q_claude["weekly_pct"] == 2.0  # 4.0 / 200.0 * 100


def test_query_summary_data_quota_enrichment(test_db: Path):
    """Verifies query_summary_data enriches summary payload with quota metrics."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(test_db) as conn:
            summary = api_routes.query_summary_data(conn)

    assert "quota" in summary
    assert "quota_summary" in summary

    quota = summary["quota"]
    assert "gemini-weekly" in quota
    assert quota["gemini-weekly"]["calibrated_capacity"] == 700.0

    qs = summary["quota_summary"]
    assert qs["gemini-weekly"]["quota_remaining_pct"] == 85.0
    assert qs["gemini-5h"]["quota_remaining_pct"] == 90.0
    assert qs["3p-weekly"]["quota_remaining_pct"] == 95.0
    assert qs["3p-5h"]["quota_remaining_pct"] == 98.0


def test_query_conversations_list_quota_enrichment(test_db: Path):
    """Verifies query_conversations_list includes session and fleet quota percentages."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(test_db) as conn:
            convs = api_routes.query_conversations_list(conn, view_mode="hierarchy")

    assert len(convs) >= 1
    root = next(c for c in convs if c["id"] == "root-conv-1")

    # Root session billed_cost = 7.0, capacity = 700.0 -> weekly_quota_pct = 1.0%
    assert root["weekly_quota_pct"] == 1.0
    assert root["five_hour_quota_pct"] == 2.8  # 7.0 / 250.0 * 100

    # Fleet includes subagent (7.0 + 3.5 = 10.5) -> fleet_weekly_quota_pct = 1.5%
    assert root["fleet_cost"] == 10.50
    assert root["fleet_weekly_quota_pct"] == 1.5
    assert root["fleet_5h_quota_pct"] == 4.2  # 10.5 / 250.0 * 100

    # Subagent in root subagents list
    assert len(root["subagents"]) == 1
    sub = root["subagents"][0]
    assert sub["id"] == "sub-conv-1"
    assert sub["weekly_quota_pct"] == 0.5  # 3.5 / 700.0 * 100


def test_query_session_details_quota_enrichment(test_db: Path):
    """Verifies query_session_details enriches steps, subagents, and top-level quota."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(test_db) as conn:
            details = api_routes.query_session_details(conn, "root-conv-1")

    assert details is not None
    assert "quota" in details
    assert details["quota"]["weekly_pct"] == 1.0

    # Step quota percentages
    steps = details["steps"]
    assert len(steps) == 3
    # Step 1: cost 2.0 -> 2.0 / 700.0 * 100 = 0.2857%
    assert round(steps[0]["weekly_quota_pct"], 4) == 0.2857
    assert round(steps[0]["five_hour_quota_pct"], 4) == 0.8

    # Subagent node quota percentages
    children = details["children"]
    assert len(children) == 1
    assert children[0]["weekly_quota_pct"] == 0.5

    # Fleet summary
    fleet = details["fleet_summary"]
    assert fleet["total_cost"] == 10.50
    assert fleet["weekly_quota_pct"] == 1.5


def test_server_http_and_sse_endpoints(test_db: Path):
    """Verifies server routes /api/quota, /api/conversations/<id>, and SSE /events /api/stream."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    # Patch get_db_connection in server, and discover_language_server_credentials
    with (
        patch(
            "context_monitor.dashboard.server.get_db_connection",
            lambda *args, **kwargs: get_db_connection(test_db),
        ),
        patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ),
    ):
        httpd = server.ThreadingHTTPServer(
            ("127.0.0.1", port), server.DashboardHTTPHandler
        )
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. Test /api/quota
            req = urllib.request.Request(f"{base_url}/api/quota")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert "gemini-weekly" in data
                assert data["gemini-weekly"]["calibrated_capacity"] == 700.0

            # 2. Test /api/quota with model param
            req = urllib.request.Request(
                f"{base_url}/api/quota?model=Claude%203.7%20Sonnet"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["bucket_prefix"] == "3p"

            # 2b. Test /api/quota with cost and model param
            req = urllib.request.Request(
                f"{base_url}/api/quota?model=Gemini%203.8%20Flash%20(High)&cost=14.0"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["weekly_pct"] == 2.0
                assert data["gemini-weekly"]["turn_pct"] == 2.0

            # 3. Test /api/conversations/<id> alias for session details
            req = urllib.request.Request(f"{base_url}/api/conversations/root-conv-1")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["conversation"]["id"] == "root-conv-1"
                assert "quota" in data
                assert data["weekly_quota_pct"] == 1.0

            # 3b. Test /api/conversations/<id>/ with trailing slash
            req = urllib.request.Request(f"{base_url}/api/conversations/root-conv-1/")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["conversation"]["id"] == "root-conv-1"
                assert "quota" in data
                assert data["weekly_quota_pct"] == 1.0

            # 4. Test SSE endpoints: /events and /api/stream receive initial connected event with quota
            for sse_path in ("/events", "/api/stream", "/api/events"):
                req = urllib.request.Request(f"{base_url}{sse_path}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    assert resp.status == 200
                    line1 = resp.readline().decode("utf-8")
                    line2 = resp.readline().decode("utf-8")
                    assert "event: connected" in line1
                    assert "data: " in line2
                    event_data = json.loads(line2.replace("data: ", "").strip())
                    assert "quota" in event_data
                    assert (
                        event_data["quota"]["gemini-weekly"]["calibrated_capacity"]
                        == 700.0
                    )

        finally:
            httpd.shutdown()
            server_thread.join(timeout=2)


def test_dashboard_frontend_invariants():
    """
    Verifies critical frontend architectural invariants:
    - Zero polling: No setInterval/setTimeout loops fetching data in client JS.
    - Static charts: Chart animations disabled and transitions none.
    - Subscription Quota UI elements present in templates/index.html.
    """
    dashboard_dir = (
        Path(__file__).resolve().parent.parent / "src" / "context_monitor" / "dashboard"
    )
    html_file = dashboard_dir / "templates" / "index.html"
    js_file = dashboard_dir / "static" / "script.js"

    html_content = html_file.read_text(encoding="utf-8")
    js_content = js_file.read_text(encoding="utf-8")

    # Invariant 1: Zero polling
    assert "setInterval(" not in js_content, (
        "Client JS must not use setInterval polling loops"
    )
    assert "setInterval(" not in html_content, (
        "HTML must not use setInterval polling loops"
    )

    # Invariant 2: Static charts (Chart animations disabled)
    assert (
        "Chart.defaults.animation = false" in js_content
        or "animation: false" in js_content
        or "duration: 0" in js_content
    ), "Static charts invariant violated: chart animations must be disabled"

    # Quota UI elements in index.html
    assert (
        "Subscription Quotas &amp; Rate Limits" in html_content
        or "Subscription Quotas & Rate Limits" in html_content
    )
    assert "quotaSection" in html_content
    assert "quotaGeminiWeeklyRem" in html_content
    assert "quotaGemini5hRem" in html_content
    assert "quota3pWeeklyRem" in html_content
    assert "quota3p5hRem" in html_content
    assert "sessionQuotaImpactBanner" in html_content
    assert "Cost (% Quota)" in html_content

    # Turn Inspector quota in script.js
    assert "weekly_quota_pct" in js_content
    assert "stepCostStr" in js_content

    # SSE listeners in script.js
    assert "connected" in js_content
    assert "db_update" in js_content
    assert "quota_update" in js_content
    assert "renderQuotaSection" in js_content

    # Token Consumption Velocity % Quota Integration
    assert "% weekly quota capacity" in html_content
    assert "Est. API Value ($ / % wk)" in js_content
    assert "currentWeeklyQuotaCapacity" in js_content
    assert "% of weekly quota" in js_content
    assert "wk quota" in js_content


def test_standalone_html_export_quota(test_db: Path, tmp_path: Path):
    """Verifies standalone HTML export embeds quota in STATIC_DATA and includes quota UI."""
    out_file = tmp_path / "standalone_export.html"
    with (
        patch(
            "context_monitor.dashboard.server.get_db_connection",
            lambda *args, **kwargs: get_db_connection(test_db),
        ),
        patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ),
    ):
        server.export_standalone_html(out_file)

    assert out_file.exists()
    html = out_file.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "STATIC_DATA" in html
    assert '"quota":' in html
    assert "quotaGeminiWeeklyRem" in html
    assert "Subscription Quotas" in html


def test_dashboard_velocity_chart_quota_execution():
    """Deeply verifies velocity chart quota capacity, yCost dual axis ticks, and tooltips via Node execution."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    let capturedConfig = null;
    let chartUpdated = false;
    let updateMode = null;
    const mockCanvas = { getContext: () => ({}) };
    const mockDocument = {
      getElementById: (id) => {
        if (id === 'timelineChart') return mockCanvas;
        return { textContent: '', className: '', style: {}, addEventListener: () => {} };
      },
      querySelectorAll: () => [],
      addEventListener: () => {}
    };

    const mockWindow = {
      location: { search: '' },
      addEventListener: () => {},
      history: { pushState: () => {} }
    };

    const mockChart = {
      defaults: { font: {}, elements: {}, plugins: {} },
      register: () => {}
    };

    const fn = new Function('window', 'document', 'Chart', 'lucide', `
      let capturedConfig = null;
      let chartUpdated = false;
      let updateMode = null;
      ${code.replace(
        'function upsertChart(existingChart, canvas, config) {',
        'function upsertChart(existingChart, canvas, config) { capturedConfig = config; return { update: (m) => { chartUpdated = true; updateMode = m; } };'
      )}
      return {
        updateWeeklyQuotaCapacity,
        renderTimelineChart,
        getWeeklyCapacity: () => currentWeeklyQuotaCapacity,
        getConfig: () => capturedConfig,
        isChartUpdated: () => chartUpdated,
        setStacked: () => { currentTimelineViewMode = 'stacked'; currentProject = 'all'; }
      };
    `);

    const api = fn(mockWindow, mockDocument, mockChart, { createIcons: () => {} });

    // Initial capacity default check
    const defaultCap = api.getWeeklyCapacity();

    // Render unstacked chart
    api.renderTimelineChart([
      { day: '2026-10-14', tokens: 100000, cost: 43.8, sessions: 5 }
    ]);
    const cfg = api.getConfig();
    const tickFn = cfg.options.scales.yCost.ticks.callback;
    const labelFn = cfg.options.plugins.tooltip.callbacks.label;

    const ticks = {
      zero: tickFn(0),
      thirty: tickFn(30),
      sixty: tickFn(60),
      ninety: tickFn(90),
      fractionalSmall: tickFn(2.5),
      fractionalMed: tickFn(12.5),
      nearInt: tickFn(29.999999999999996),
      kilo: tickFn(1500)
    };

    const costHover = labelFn({ dataset: { label: 'Est. API Value ($)' }, raw: 43.8, dataIndex: 0 });
    const unstackedBarHover = labelFn({ dataset: { label: 'API Tokens Consumed' }, raw: 100000, dataIndex: 0 });

    // Dynamic quota update triggers timelineChart update
    api.updateWeeklyQuotaCapacity({ buckets: { 'gemini-weekly': { calibrated_capacity: 500.0 } } });
    const updatedCap = api.getWeeklyCapacity();
    const tickThirtyAfterCapUpdate = tickFn(30);
    const wasUpdated = api.isChartUpdated();

    // Render stacked chart
    api.setStacked();
    api.renderTimelineChart([
      { day: '2026-10-14', tokens: 100000, cost: 40.0, projects: { 'projA': 50000, 'projB': 50000 } }
    ]);
    const stackedCfg = api.getConfig();
    const stackedLabelFn = stackedCfg.options.plugins.tooltip.callbacks.label;
    const stackedFooterFn = stackedCfg.options.plugins.tooltip.callbacks.footer;

    const stackedBarItemHover = stackedLabelFn({
      dataset: { label: 'projA', stack: 'tokens_stack' },
      raw: 50000,
      dataIndex: 0
    });
    const stackedFooter = stackedFooterFn([{ dataIndex: 0 }]);

    console.log(JSON.stringify({
      defaultCap,
      updatedCap,
      wasUpdated,
      axisTitle: cfg.options.scales.yCost.title.text,
      ticks,
      tickThirtyAfterCapUpdate,
      costHover,
      unstackedBarHover,
      stackedBarItemHover,
      stackedFooter
    }));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["defaultCap"] == 672.0
    assert data["updatedCap"] == 500.0
    assert data["wasUpdated"] is True
    assert data["axisTitle"] == "Est. API Value ($ / % wk)"

    # Ticks formatting checks
    assert data["ticks"]["zero"] == "$0 (0.0%)"
    assert data["ticks"]["thirty"] == "$30 (4.5%)"
    assert data["ticks"]["sixty"] == "$60 (8.9%)"
    assert data["ticks"]["ninety"] == "$90 (13.4%)"
    assert data["ticks"]["fractionalSmall"] == "$2.50 (0.4%)"
    assert data["ticks"]["fractionalMed"] == "$12.50 (1.9%)"
    assert data["ticks"]["nearInt"] == "$30 (4.5%)"
    assert data["ticks"]["kilo"] == "$1.5k (223.2%)"
    assert data["tickThirtyAfterCapUpdate"] == "$30 (6.0%)"

    # Tooltips checks
    assert data["costHover"] == "Est. API Value: $43.80 (6.52% of weekly quota)"
    assert "of weekly quota" in data["unstackedBarHover"]
    assert "50.0% · 4.00% wk quota" in data["stackedBarItemHover"]
    assert "Total API Tokens: 100.0k (8.00% of weekly quota)" in data["stackedFooter"]
