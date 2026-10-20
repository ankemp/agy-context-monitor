"""
Tests for Dashboard Quota Integration, API Routes, SSE Stream, and Frontend Invariants
--------------------------------------------------------------------------------------
Covers /api/quota, /events, /api/stream, /api/conversations/<id>, summary & session quota metrics,
zero polling invariant, static charts invariant, and standalone HTML export.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
    assert "sessionQuotaImpactWk" in html_content
    assert "sessionQuotaImpact5h" in html_content
    assert "quotaCalibrationNote" in html_content
    assert "pacingBadge" in html_content
    assert "End-of-Week Projection" in html_content
    assert "5-Hour Cycle Pace" in html_content
    assert "Cost (% Quota)" in html_content

    # Turn Inspector quota in script.js
    assert "weekly_quota_pct" in js_content
    assert "stepCostStr" in js_content

    # SSE listeners in script.js
    assert "connected" in js_content
    assert "db_update" in js_content
    assert "quota_update" in js_content
    assert "renderQuotaSection" in js_content
    assert "pacingBadge" in js_content
    assert "weekly_pacing" in js_content
    assert "five_hour_pacing" in js_content
    assert "projected_weekly_pct" in js_content
    assert "pacing_status" in js_content

    # Token Consumption Velocity % Quota Integration
    assert "% weekly quota capacity" in html_content
    assert "Est. API Value ($ / % wk)" in js_content
    assert "currentWeeklyQuotaCapacity" in js_content
    assert "% of weekly quota" in js_content
    assert "wk quota" in js_content
    assert "geminiWeeklyBucket" in js_content
    assert "weeklyCalibratedCapacity" in js_content


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
    assert '"timeline_cycle":' in html
    assert "quotaGeminiWeeklyRem" in html
    assert "Subscription Quotas" in html
    assert "24x7 Activity Matrix Weighted" in html
    assert "Linear Wall-Clock" in html


def test_build_static_bundle(test_db: Path):
    """Verifies build_static_bundle constructs the full static bundle including timeline_cycle."""
    conn = get_db_connection(test_db)
    try:
        # 1. Default invocation
        bundle = server.build_static_bundle(conn)
        assert isinstance(bundle, dict)
        expected_keys = {
            "summary",
            "timeline",
            "timeline_cycle",
            "cost_by_project",
            "tools",
            "mcp",
            "hot_files",
            "activity_matrix",
            "conversations",
            "quota",
        }
        assert expected_keys.issubset(bundle.keys())
        assert isinstance(bundle["timeline_cycle"], list)
        assert len(bundle["timeline_cycle"]) > 0
        for item in bundle["timeline_cycle"]:
            assert "day" in item
            assert "tokens" in item
            assert "cost" in item
            assert "label" in item
            assert "is_start" in item
            assert "is_reset" in item
            assert "is_future" in item

        # 2. Parameterized invocation with timezone and project
        bundle_proj = server.build_static_bundle(
            conn,
            target_tz="America/New_York",
            project="test-project",
            include_conversations=True,
        )
        assert isinstance(bundle_proj["timeline_cycle"], list)
        assert isinstance(bundle_proj["conversations"], list)
    finally:
        conn.close()


def test_standalone_html_export_cycle_offline_execution(test_db: Path, tmp_path: Path):
    """Verifies static offline HTML export embeds timeline_cycle, and that the actual
    embedded window.fetch router and script.js render cycle data immediately with 0 network requests."""
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
        server.export_standalone_html(out_file, tz="America/Chicago")

    assert out_file.exists()
    html = out_file.read_text(encoding="utf-8")
    assert '"timeline_cycle":' in html
    assert "days=cycle" in html

    # Extract the actual embedded JS block produced by server.py
    embedded_match = re.search(
        r"<script>(\s*const IS_STATIC_EXPORT = true;[\s\S]*?)</script>", html
    )
    assert embedded_match is not None, "Embedded static JS block was not found in exported HTML"
    embedded_js = embedded_match.group(1)

    node_script = f"""
    const fs = require('fs');
    const scriptCode = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    // 1. Initialize browser window mock
    const window = {{
      location: {{ search: '' }},
      addEventListener: () => {{}},
      history: {{ pushState: () => {{}} }}
    }};

    // 2. Evaluate the REAL embedded JS block from the exported HTML file
    const evalEmbedded = new Function('window', `{embedded_js}; return {{ STATIC_DATA, IS_STATIC_EXPORT }};`);
    const {{ STATIC_DATA, IS_STATIC_EXPORT }} = evalEmbedded(window);

    // Verify window.fetch was attached and routes days=cycle to STATIC_DATA.timeline_cycle
    if (typeof window.fetch !== 'function') {{
      throw new Error('window.fetch was not registered by exported embedded JS');
    }}

    async function runStaticOfflineTest() {{
      // Verify direct mock fetch calls on the exported window.fetch
      const cycleFetchRes = await (await window.fetch('/api/timeline?days=cycle&tz=America/Chicago')).json();
      if (!Array.isArray(cycleFetchRes) || cycleFetchRes.length !== STATIC_DATA.timeline_cycle.length) {{
        throw new Error('Exported window.fetch failed to return timeline_cycle on /api/timeline?days=cycle');
      }}

      const standardFetchRes = await (await window.fetch('/api/timeline?days=30')).json();
      if (!Array.isArray(standardFetchRes) || standardFetchRes.length !== STATIC_DATA.timeline.length) {{
        throw new Error('Exported window.fetch failed to return standard timeline on non-cycle request');
      }}

      // Set up spy on window.fetch to count subsequent network requests
      let fetchCalls = [];
      const realExportedFetch = window.fetch;
      window.fetch = async (...args) => {{
        fetchCalls.push(args[0]);
        return realExportedFetch(...args);
      }};

      const mockCanvas = {{ getContext: () => ({{}}) }};
      const elements = {{}};
      const getEl = (id) => {{
        if (id === 'timelineChart') return mockCanvas;
        if (!elements[id]) {{
          elements[id] = {{ textContent: '', className: '', style: {{}}, options: [], classList: {{ remove: () => {{}}, add: () => {{}} }}, appendChild: () => {{}}, addEventListener: () => {{}} }};
        }}
        return elements[id];
      }};
      const mockDocument = {{
        getElementById: getEl,
        querySelectorAll: () => [],
        createElement: () => ({{ value: '', textContent: '' }}),
        addEventListener: () => {{}}
      }};
      const mockChart = {{
        defaults: {{ font: {{}}, elements: {{}}, plugins: {{}} }},
        register: () => {{}}
      }};

      let capturedConfig = null;
      const modCode = scriptCode.replace(
        'function upsertChart(existingChart, canvas, config) {{',
        'function upsertChart(existingChart, canvas, config) {{ capturedConfig = config; return {{ update: () => {{}} }};'
      );

      const fn = new Function('window', 'document', 'Chart', 'lucide', 'fetch', 'STATIC_DATA', 'IS_STATIC_EXPORT', `
        ${{modCode}}
        return {{
          setTimelineRange,
          renderTimelineChart,
          getCycleTimelineData: () => cycleTimelineData,
          getCachedCycleProject: () => cachedCycleProject,
          getConfig: () => capturedConfig,
          getFetchCalls: () => fetchCalls,
          mockFetch: window.fetch
        }};
      `);

      const api = fn(window, mockDocument, mockChart, {{ createIcons: () => {{}} }}, window.fetch, STATIC_DATA, IS_STATIC_EXPORT);

      // Verify cycleTimelineData and cachedCycleProject were initialized synchronously from STATIC_DATA
      const initialCycle = api.getCycleTimelineData();
      if (!initialCycle || initialCycle.length !== STATIC_DATA.timeline_cycle.length) {{
        throw new Error('cycleTimelineData was not pre-initialized from STATIC_DATA.timeline_cycle');
      }}
      if (api.getCachedCycleProject() !== 'all') {{
        throw new Error('cachedCycleProject was not initialized to "all" in static mode');
      }}

      // Simulate clicking #btnCycle (which calls setTimelineRange('cycle'))
      api.setTimelineRange('cycle');

      // Assert that chart was configured with the static cycle data
      const cfg = api.getConfig();
      if (!cfg || !cfg.data || !cfg.data.labels) {{
        throw new Error('Chart was not rendered after setTimelineRange("cycle")');
      }}
      const expectedFirstLabel = STATIC_DATA.timeline_cycle[0].label;
      const expectedLastLabel = STATIC_DATA.timeline_cycle[STATIC_DATA.timeline_cycle.length - 1].label;
      if (cfg.data.labels[0] !== expectedFirstLabel) {{
        throw new Error(`Expected first boundary label '${{expectedFirstLabel}}', got ${{cfg.data.labels[0]}}`);
      }}
      if (cfg.data.labels[cfg.data.labels.length - 1] !== expectedLastLabel) {{
        throw new Error(`Expected last boundary label '${{expectedLastLabel}}', got ${{cfg.data.labels[cfg.data.labels.length - 1]}}`);
      }}

      // Verify that NO external fetch calls were made (zero network requests, instant offline render)
      const zeroFetchCalls = fetchCalls.length === 0;
      if (!zeroFetchCalls) {{
        throw new Error("Expected 0 fetch calls for static offline cycle switch, got " + fetchCalls.length + ": " + JSON.stringify(fetchCalls));
      }}

      console.log(JSON.stringify({{
        status: 'ok',
        cycleInitialized: true,
        zeroFetchCalls: zeroFetchCalls,
        firstLabel: cfg.data.labels[0],
        lastLabel: cfg.data.labels[cfg.data.labels.length - 1]
      }}));
    }}

    runStaticOfflineTest().catch(err => {{
      console.error(err);
      process.exit(1);
    }});
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    json_line = [
        line for line in res.stdout.strip().splitlines() if line.startswith("{")
    ][-1]
    data = json.loads(json_line)
    assert data["status"] == "ok"
    assert data["cycleInitialized"] is True
    assert data["zeroFetchCalls"] is True


def test_standalone_html_export_cycle_offline_empty_quota(tmp_path: Path):
    """Verifies static offline HTML export when quota_state table is completely empty:
    gracefully includes 7-day fallback timeline_cycle and renders without crashing or network requests."""
    empty_db = tmp_path / "empty_quota.db"
    init_db(empty_db)
    out_file = tmp_path / "empty_quota_export.html"
    with (
        patch(
            "context_monitor.dashboard.server.get_db_connection",
            lambda *args, **kwargs: get_db_connection(empty_db),
        ),
        patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ),
    ):
        server.export_standalone_html(out_file, tz="America/Chicago")

    assert out_file.exists()
    html = out_file.read_text(encoding="utf-8")
    assert '"timeline_cycle":' in html

    embedded_match = re.search(
        r"<script>(\s*const IS_STATIC_EXPORT = true;[\s\S]*?)</script>", html
    )
    assert embedded_match is not None
    embedded_js = embedded_match.group(1)

    node_script = f"""
    const fs = require('fs');
    const scriptCode = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    const window = {{
      location: {{ search: '' }},
      addEventListener: () => {{}},
      history: {{ pushState: () => {{}} }}
    }};

    const evalEmbedded = new Function('window', `{embedded_js}; return {{ STATIC_DATA, IS_STATIC_EXPORT }};`);
    const {{ STATIC_DATA, IS_STATIC_EXPORT }} = evalEmbedded(window);

    let fetchCalls = [];
    window.fetch = async (...args) => {{
      fetchCalls.push(args[0]);
      return {{ json: async () => STATIC_DATA.timeline_cycle }};
    }};

    const mockCanvas = {{ getContext: () => ({{}}) }};
    const elements = {{}};
    const getEl = (id) => {{
      if (id === 'timelineChart') return mockCanvas;
      if (!elements[id]) {{
        elements[id] = {{ textContent: '', className: '', style: {{}}, options: [], classList: {{ remove: () => {{}}, add: () => {{}} }}, appendChild: () => {{}}, addEventListener: () => {{}} }};
      }}
      return elements[id];
    }};
    const mockDocument = {{
      getElementById: getEl,
      querySelectorAll: () => [],
      createElement: () => ({{ value: '', textContent: '' }}),
      addEventListener: () => {{}}
    }};
    const mockChart = {{
      defaults: {{ font: {{}}, elements: {{}}, plugins: {{}} }},
      register: () => {{}}
    }};

    let capturedConfig = null;
    const modCode = scriptCode.replace(
      'function upsertChart(existingChart, canvas, config) {{',
      'function upsertChart(existingChart, canvas, config) {{ capturedConfig = config; return {{ update: () => {{}} }};'
    );

    const fn = new Function('window', 'document', 'Chart', 'lucide', 'fetch', 'STATIC_DATA', 'IS_STATIC_EXPORT', `
      ${{modCode}}
      return {{
        setTimelineRange,
        getCycleTimelineData: () => cycleTimelineData,
        getConfig: () => capturedConfig
      }};
    `);

    const api = fn(window, mockDocument, mockChart, {{ createIcons: () => {{}} }}, window.fetch, STATIC_DATA, IS_STATIC_EXPORT);
    if (!api.getCycleTimelineData() || api.getCycleTimelineData().length !== 7) {{
      throw new Error('Expected 7-day fallback timeline_cycle, got: ' + (api.getCycleTimelineData()?.length));
    }}

    api.setTimelineRange('cycle');
    if (fetchCalls.length !== 0) {{
      throw new Error('Expected 0 fetch calls, got: ' + fetchCalls.length);
    }}
    console.log(JSON.stringify({{ status: 'ok', fallbackLen: api.getCycleTimelineData().length }}));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["status"] == "ok"
    assert data["fallbackLen"] == 7


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


def test_dashboard_velocity_chart_cycle_mode_execution():
    """Verifies #btnCycle toggle button in index.html, setTimelineRange('cycle') button classes,
    boundary chart labels (e.g. 'Tue 21:27'), cycle-aware tooltip callbacks, defensive inputs,
    stacked footer formatting, on-demand fetch error safety, and project change cache invalidation."""
    dashboard_dir = (
        Path(__file__).resolve().parent.parent / "src" / "context_monitor" / "dashboard"
    )
    html_file = dashboard_dir / "templates" / "index.html"
    html_content = html_file.read_text(encoding="utf-8")

    # Invariant: #btnCycle exists in index.html with correct attributes and position
    assert 'id="btnCycle"' in html_content, "#btnCycle missing in index.html"
    assert "setTimelineRange('cycle')" in html_content, (
        "setTimelineRange('cycle') onclick missing in index.html"
    )
    cycle_idx = html_content.find('id="btnCycle"')
    btn7d_idx = html_content.find('id="btn7d"')
    assert cycle_idx != -1 and btn7d_idx != -1 and cycle_idx < btn7d_idx, (
        "#btnCycle must appear immediately before #btn7d in index.html"
    )

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    let capturedConfig = null;
    const mockCanvas = { getContext: () => ({}) };
    const elements = {};
    const getEl = (id) => {
      if (id === 'timelineChart') return mockCanvas;
      if (!elements[id]) {
        elements[id] = { textContent: '', className: '', style: {}, options: [], classList: { remove: () => {}, add: () => {} }, appendChild: () => {}, addEventListener: () => {} };
      }
      return elements[id];
    };
    const mockDocument = {
      getElementById: getEl,
      querySelectorAll: () => [],
      createElement: () => ({ value: '', textContent: '' }),
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

    let fetchCount = 0;
    let shouldRejectFetch = false;
    const mockFetch = (url) => {
      fetchCount++;
      if (shouldRejectFetch) {
        return Promise.reject(new Error('Network error'));
      }
      return Promise.resolve({
        json: () => Promise.resolve([
          { day: '2026-10-13', label: 'Tue 21:27', is_start: true, is_reset: false, is_future: false, tokens: 50000, cost: 20.0, sessions: 2 },
          { day: '2026-10-20', label: 'Tue 21:27', is_start: false, is_reset: true, is_future: true, tokens: 0, cost: 0.0, sessions: 0 }
        ])
      });
    };

    const fn = new Function('window', 'document', 'Chart', 'lucide', 'fetch', `
      let capturedConfig = null;
      ${code.replace(
        'function upsertChart(existingChart, canvas, config) {',
        'function upsertChart(existingChart, canvas, config) { capturedConfig = config; return { update: () => {} };'
      )}
      return {
        setTimelineRange,
        renderTimelineChart,
        handleProjectChange: window.handleProjectChange,
        setTimelineViewMode: window.setTimelineViewMode,
        getConfig: () => capturedConfig,
        setCycleTimelineData: (d) => { cycleTimelineData = d; },
        getCycleTimelineData: () => cycleTimelineData,
        getCurrentTimelineDays: () => currentTimelineDays
      };
    `);

    async function runTests() {
      const api = fn(mockWindow, mockDocument, mockChart, { createIcons: () => {} }, mockFetch);

      // Defensive checks: renderTimelineChart with null or undefined does not throw
      api.renderTimelineChart(null);
      api.renderTimelineChart(undefined);
      api.renderTimelineChart([]);

      // Test 1: Button classes when toggling between cycle and 7d, and case-insensitivity
      api.setTimelineRange('cycle');
      const btnCycleClassAfterCycle = getEl('btnCycle').className;
      const btn7dClassAfterCycle = getEl('btn7d').className;
      const btn30dClassAfterCycle = getEl('btn30d').className;

      api.setTimelineRange(7);
      const btnCycleClassAfter7d = getEl('btnCycle').className;
      const btn7dClassAfter7d = getEl('btn7d').className;

      // Case-insensitivity: 'Cycle' activates button and mode
      api.setTimelineRange('Cycle');
      const btnCycleClassReactivated = getEl('btnCycle').className;

      // Test 2: Chart rendering with cycle boundary labels and tooltips
      const mockCycleData = [
        { day: '2026-10-13', label: 'Tue 21:27', is_start: true, is_reset: false, is_future: false, tokens: 120000, cost: 52.4, sessions: 6, projects: { 'proj1': 120000 } },
        { day: '2026-10-14', label: '2026-10-14', is_start: false, is_reset: false, is_future: false, tokens: 80000, cost: 35.0, sessions: 4, projects: { 'proj1': 80000 } },
        { day: '2026-10-15', label: '2026-10-15', is_start: false, is_reset: false, is_future: true, tokens: 0, cost: 0.0, sessions: 0, projects: {} },
        { day: '2026-10-20', label: 'Tue 21:27', is_start: false, is_reset: true, is_future: true, tokens: 0, cost: 0.0, sessions: 0, projects: {} }
      ];

      api.setCycleTimelineData(mockCycleData);
      api.setTimelineRange('cycle');

      const cfg = api.getConfig();
      const labels = cfg.data.labels;
      const labelFn = cfg.options.plugins.tooltip.callbacks.label;
      const titleFn = cfg.options.plugins.tooltip.callbacks.title;

      // Tooltip evaluations
      const startBarHover = labelFn({ dataset: { label: 'API Tokens Consumed' }, raw: 120000, dataIndex: 0 });
      const startCostHover = labelFn({ dataset: { label: 'Est. API Value ($)' }, raw: 52.4, dataIndex: 0 });
      const startTitle = titleFn ? titleFn([{ dataIndex: 0 }]) : null;

      const midBarHover = labelFn({ dataset: { label: 'API Tokens Consumed' }, raw: 80000, dataIndex: 1 });
      const midTitle = titleFn ? titleFn([{ dataIndex: 1 }]) : null;

      const futureBarHover = labelFn({ dataset: { label: 'API Tokens Consumed' }, raw: 0, dataIndex: 2 });
      const futureCostHover = labelFn({ dataset: { label: 'Est. API Value ($)' }, raw: 0, dataIndex: 2 });
      const futureTitle = titleFn ? titleFn([{ dataIndex: 2 }]) : null;

      const resetBarHover = labelFn({ dataset: { label: 'API Tokens Consumed' }, raw: 0, dataIndex: 3 });
      const resetCostHover = labelFn({ dataset: { label: 'Est. API Value ($)' }, raw: 0, dataIndex: 3 });
      const resetTitle = titleFn ? titleFn([{ dataIndex: 3 }]) : null;

      // Test 3: Stacked view mode with cycle data
      api.setTimelineViewMode('stacked');
      const stackedCfg = api.getConfig();
      const stackedFooterFn = stackedCfg.options.plugins.tooltip.callbacks.footer;
      const stackedFutureFooter = stackedFooterFn([{ dataIndex: 2 }]);
      const stackedResetFooter = stackedFooterFn([{ dataIndex: 3 }]);

      // Test 4: On-demand fetch when cycleTimelineData is null & catch-block safety
      api.setCycleTimelineData(null);
      shouldRejectFetch = true;
      api.setTimelineRange('cycle');
      // User quickly switches to 7d before rejection completes
      api.setTimelineRange(7);
      await new Promise(r => setTimeout(r, 20));
      const rangeAfterReject = api.getCurrentTimelineDays();

      // Test 5: Invalidate cycle data on project change
      api.setCycleTimelineData(mockCycleData);
      api.handleProjectChange('projX', false);
      const cycleDataAfterProjChange = api.getCycleTimelineData();

      console.log(JSON.stringify({
        btnCycleClassAfterCycle,
        btn7dClassAfterCycle,
        btn30dClassAfterCycle,
        btnCycleClassAfter7d,
        btn7dClassAfter7d,
        btnCycleClassReactivated,
        labels,
        startBarHover,
        startCostHover,
        startTitle,
        midBarHover,
        midTitle,
        futureBarHover,
        futureCostHover,
        futureTitle,
        resetBarHover,
        resetCostHover,
        resetTitle,
        stackedFutureFooter,
        stackedResetFooter,
        rangeAfterReject,
        cycleDataAfterProjChange
      }));
    }

    runTests();
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # Button classes
    assert "bg-brand-600" in data["btnCycleClassAfterCycle"]
    assert "text-white" in data["btnCycleClassAfterCycle"]
    assert "text-gray-400" in data["btn7dClassAfterCycle"]
    assert "text-gray-400" in data["btn30dClassAfterCycle"]

    assert "text-gray-400" in data["btnCycleClassAfter7d"]
    assert "bg-brand-600" in data["btn7dClassAfter7d"]

    assert "bg-brand-600" in data["btnCycleClassReactivated"]

    # Chart Labels
    assert data["labels"] == ["Tue 21:27", "2026-10-14", "2026-10-15", "Tue 21:27"]

    # Tooltips: Week Start
    assert "Week Start (Tue 21:27)" in data["startBarHover"]
    assert "Week Start (Tue 21:27)" in data["startCostHover"]
    if data["startTitle"]:
        assert "Week Start (Tue 21:27)" in data["startTitle"]

    # Tooltips: Intermediate day
    assert "Week Start" not in data["midBarHover"]
    assert "Reset" not in data["midBarHover"]
    assert "Upcoming" not in data["midBarHover"]

    # Tooltips: Future day
    assert "Upcoming in cycle" in data["futureBarHover"]
    assert "0 tokens" in data["futureBarHover"]
    assert "Upcoming" in data["futureCostHover"]

    # Tooltips: Reset day
    assert "Reset (Tue 21:27)" in data["resetBarHover"]
    assert "Reset (Tue 21:27)" in data["resetCostHover"]
    if data["resetTitle"]:
        assert "Reset (Tue 21:27)" in data["resetTitle"]

    # Stacked footers
    assert "Upcoming in cycle" in data["stackedFutureFooter"]
    assert "0 tokens" in data["stackedFutureFooter"]
    assert "Reset (Tue 21:27)" in data["stackedResetFooter"]

    # On-demand fetch catch safety: active range preserved
    assert data["rangeAfterReject"] == 7

    # Project change invalidation
    assert data["cycleDataAfterProjChange"] is None


def test_model_badge_formatting():
    """Verifies formatModelBadgeHtml shortens model names, assigns tier colors, and wraps transitions."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    const fn = new Function('window', 'document', 'Chart', 'lucide', `
      ${code}
      return {
        formatModelBadgeHtml,
        shortenModelName,
        getModelTierClass
      };
    `);

    const mockWindow = {
      location: { search: '' },
      addEventListener: () => {},
      history: { pushState: () => {} }
    };
    const mockDocument = {
      getElementById: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {}
    };

    const api = fn(mockWindow, mockDocument, { defaults: { font: {}, elements: {}, plugins: {} }, register: () => {} }, { createIcons: () => {} });

    const singleFlash = api.formatModelBadgeHtml('Gemini 3.8 Flash (High)');
    const singleArgon = api.formatModelBadgeHtml('Gemini 4 Argon (Medium)');
    const singlePro = api.formatModelBadgeHtml('Gemini 3.8 Pro (High)');
    const singleClaude = api.formatModelBadgeHtml('Claude 3.7 Sonnet');
    const singleInherit = api.formatModelBadgeHtml('inherit');
    const transitionMulti = api.formatModelBadgeHtml('Gemini 3.8 Flash (High) → Gemini 4 Argon (Medium) → Gemini 3.8 Flash (High)');
    const nullModel = api.formatModelBadgeHtml(null);

    console.log(JSON.stringify({
      singleFlash,
      singleArgon,
      singlePro,
      singleClaude,
      singleInherit,
      transitionMulti,
      nullModel
    }));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # 1. Single model shortening and color checks
    assert ">3.8 Flash (High)<" in data["singleFlash"]
    assert 'title="Gemini 3.8 Flash (High)"' in data["singleFlash"]
    assert "purple" in data["singleFlash"]

    assert ">4 Argon (Med)<" in data["singleArgon"]
    assert 'title="Gemini 4 Argon (Medium)"' in data["singleArgon"]
    assert "cyan" in data["singleArgon"]

    assert "3.8 Pro (High)" in data["singlePro"]
    assert "indigo" in data["singlePro"]

    assert "Claude 3.7 Sonnet" in data["singleClaude"]
    assert "amber" in data["singleClaude"]

    assert "inherit" in data["singleInherit"]
    assert "zinc" in data["singleInherit"] or "gray" in data["singleInherit"]

    # 2. Multi-model transitions
    assert "flex flex-wrap items-center gap-1 max-w-[220px]" in data["transitionMulti"]
    assert "→" in data["transitionMulti"]
    assert "3.8 Flash (High)" in data["transitionMulti"]
    assert "4 Argon (Med)" in data["transitionMulti"]

    # 3. Null / empty handling
    assert data["nullModel"] != ""
    assert "Gemini" in data["nullModel"] or "Unknown" in data["nullModel"]


def test_dashboard_render_quota_section_pacing_execution():
    """Deeply verifies renderQuotaSection pacing rendering, status badges, 5h pace, and tooltips via Node execution."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    const elements = {};
    function getOrCreate(id) {
      if (!elements[id]) {
        elements[id] = { textContent: '', className: '', style: {}, title: '' };
      }
      return elements[id];
    }

    const mockDocument = {
      getElementById: (id) => getOrCreate(id),
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
      ${code}
      return {
        renderQuotaSection,
        getElements: () => elements
      };
    `);

    const api = fn(mockWindow, mockDocument, mockChart, { createIcons: () => {} });

    // 1. Nominal pacing: 96.6% projected ($478), moderate status, 30.2% 5h pace
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 11.5,
        projected_pct: 96.6,
        projected_cost: 478.05,
        burn_rate_pct_per_hour: 0.575,
        elapsed_hours: 20.0,
        remaining_hours: 148.0,
        status: 'moderate',
        hours_until_exhaustion: null,
        is_calibrating: false
      },
      five_hour_pacing: {
        projected_pct: 30.2,
        is_calibrating: false
      },
      projected_weekly_pct: 96.6,
      projected_weekly_cost: 478.05,
      pacing_status: 'moderate',
      projected_5h_pct: 30.2
    });
    const nominal = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      fiveH: elements['sessionQuotaImpact5h']?.textContent,
      bannerTitle: elements['sessionQuotaImpactBanner']?.title
    };

    // 2. On Track pacing
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 5.0,
        projected_pct: 45.0,
        projected_cost: 220.0,
        burn_rate_pct_per_hour: 0.25,
        elapsed_hours: 20.0,
        remaining_hours: 148.0,
        status: 'on_track',
        is_calibrating: false
      },
      projected_weekly_pct: 45.0,
      projected_weekly_cost: 220.0,
      pacing_status: 'on_track',
      projected_5h_pct: 15.0
    });
    const onTrack = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className
    };

    // 3. Exceeding / Pacing Alert with exhaustion risk
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 60.0,
        projected_pct: 140.0,
        projected_cost: 700.0,
        burn_rate_pct_per_hour: 2.5,
        elapsed_hours: 24.0,
        remaining_hours: 144.0,
        status: 'exceeding',
        hours_until_exhaustion: 16.0,
        is_calibrating: false
      },
      projected_weekly_pct: 140.0,
      projected_weekly_cost: 700.0,
      pacing_status: 'exceeding'
    });
    const alert = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      bannerTitle: elements['sessionQuotaImpactBanner']?.title
    };

    // 4. Calibrating guard active
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 1.0,
        projected_pct: 80.0,
        projected_cost: 400.0,
        burn_rate_pct_per_hour: 1.0,
        elapsed_hours: 1.0,
        remaining_hours: 167.0,
        status: 'calibrating',
        is_calibrating: true
      },
      pacing_status: 'calibrating'
    });
    const calibrating = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      bannerTitle: elements['sessionQuotaImpactBanner']?.title
    };

    // 5. Fallback safely when pacing data absent
    api.renderQuotaSection({});
    const fallback = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      fiveH: elements['sessionQuotaImpact5h']?.textContent
    };

    // 6. Claude / 3P pacing buckets
    api.renderQuotaSection({
      '3p-weekly': {
        pacing: {
          used_pct: 15.0,
          projected_pct: 75.0,
          projected_cost: 150.0,
          burn_rate_pct_per_hour: 0.5,
          elapsed_hours: 30.0,
          remaining_hours: 138.0,
          status: 'on_track',
          is_calibrating: false
        }
      },
      '3p-5h': {
        pacing: {
          projected_pct: 20.0,
          is_calibrating: false
        }
      }
    });
    const claude3p = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      fiveH: elements['sessionQuotaImpact5h']?.textContent
    };

    // 7. Top-level projected pct without explicit pacing_status (inferred status)
    api.renderQuotaSection({
      projected_weekly_pct: 65.0,
      projected_weekly_cost: 320.0,
      projected_5h_pct: 18.0
    });
    const inferred = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      fiveH: elements['sessionQuotaImpact5h']?.textContent
    };

    // 8. Large cost formatting with commas and status alias
    api.renderQuotaSection({
      projected_weekly_pct: 125.0,
      projected_weekly_cost: 12500.0,
      pacing_status: 'pacing_alert'
    });
    const largeCost = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className
    };

    // 9. Activity-weighted pacing
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 10.0,
        projected_pct: 88.0,
        projected_cost: 440.0,
        burn_rate_pct_per_hour: 0.5,
        elapsed_hours: 20.0,
        remaining_hours: 148.0,
        status: 'moderate',
        hours_until_exhaustion: null,
        is_calibrating: false,
        weighting_mode: 'activity_weighted',
        elapsed_activity_pct: 18.5,
        linear_projected_pct: 320.0
      },
      projected_weekly_pct: 88.0,
      projected_weekly_cost: 440.0,
      pacing_status: 'moderate'
    });
    const activityWeighted = {
      wk: elements['sessionQuotaImpactWk']?.textContent,
      badgeText: elements['pacingBadge']?.textContent,
      badgeClass: elements['pacingBadge']?.className,
      bannerTitle: elements['sessionQuotaImpactBanner']?.title,
      wkTitle: elements['sessionQuotaImpactWk']?.title
    };

    // 10. Activity-weighted pacing without linear_projected_pct
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 8.0,
        projected_pct: 72.0,
        projected_cost: 360.0,
        burn_rate_pct_per_hour: 0.4,
        elapsed_hours: 15.0,
        remaining_hours: 153.0,
        status: 'on_track',
        hours_until_exhaustion: null,
        is_calibrating: false,
        weighting_mode: 'activity_weighted',
        elapsed_activity_pct: 12.3,
        linear_projected_pct: null
      },
      projected_weekly_pct: 72.0,
      projected_weekly_cost: 360.0,
      pacing_status: 'on_track'
    });
    const activityWeightedNoLinear = {
      bannerTitle: elements['sessionQuotaImpactBanner']?.title,
      wkTitle: elements['sessionQuotaImpactWk']?.title
    };

    // 11. Standalone elSessWk without sessionQuotaImpactBanner
    delete elements['sessionQuotaImpactBanner'];
    api.renderQuotaSection({
      weekly_pacing: {
        used_pct: 15.0,
        projected_pct: 95.0,
        projected_cost: 450.0,
        burn_rate_pct_per_hour: 0.6,
        elapsed_hours: 25.0,
        remaining_hours: 143.0,
        status: 'moderate',
        hours_until_exhaustion: null,
        is_calibrating: false,
        weighting_mode: 'activity_weighted',
        elapsed_activity_pct: 22.0,
        linear_projected_pct: 180.0
      }
    });
    const standaloneWk = {
      wkTitle: elements['sessionQuotaImpactWk']?.title
    };

    console.log(JSON.stringify({ nominal, onTrack, alert, calibrating, fallback, claude3p, inferred, largeCost, activityWeighted, activityWeightedNoLinear, standaloneWk }));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    # 1. Nominal check
    assert data["nominal"]["wk"] == "~96.6% projected (~$478)"
    assert data["nominal"]["badgeText"] == "Near Limit"
    assert "amber" in data["nominal"]["badgeClass"]
    assert data["nominal"]["fiveH"] == "~30.2% projected"
    assert "Weekly Pacing Calculation:" in data["nominal"]["bannerTitle"]
    assert "Burn Rate" in data["nominal"]["bannerTitle"]
    assert "• Pacing Model: Linear Wall-Clock" in data["nominal"]["bannerTitle"]

    # 2. On Track check
    assert data["onTrack"]["wk"] == "~45.0% projected (~$220)"
    assert data["onTrack"]["badgeText"] == "On Track"
    assert "emerald" in data["onTrack"]["badgeClass"]

    # 3. Alert check
    assert data["alert"]["wk"] == "~140.0% projected (~$700)"
    assert data["alert"]["badgeText"] == "Pacing Alert"
    assert "rose" in data["alert"]["badgeClass"]
    assert (
        "Exhaustion Risk: 100% quota reached in ~16.0h" in data["alert"]["bannerTitle"]
    )

    # 4. Calibrating check
    assert data["calibrating"]["wk"] == "Calibrating..."
    assert data["calibrating"]["badgeText"] == "Calibrating"
    assert "cyan" in data["calibrating"]["badgeClass"]
    assert "Early-Cycle Guard" in data["calibrating"]["bannerTitle"]

    # 5. Fallback check
    assert data["fallback"]["wk"] == "Calibrating..."
    assert data["fallback"]["badgeText"] == "Calibrating"
    assert data["fallback"]["fiveH"] == "--% projected"

    # 6. Claude / 3P check
    assert data["claude3p"]["wk"] == "~75.0% projected (~$150)"
    assert data["claude3p"]["badgeText"] == "On Track"
    assert "emerald" in data["claude3p"]["badgeClass"]
    assert data["claude3p"]["fiveH"] == "~20.0% projected"

    # 7. Inferred status check
    assert data["inferred"]["wk"] == "~65.0% projected (~$320)"
    assert data["inferred"]["badgeText"] == "On Track"
    assert "emerald" in data["inferred"]["badgeClass"]
    assert data["inferred"]["fiveH"] == "~18.0% projected"

    # 8. Large cost formatting check
    assert data["largeCost"]["wk"] == "~125.0% projected (~$12,500)"
    assert data["largeCost"]["badgeText"] == "Pacing Alert"
    assert "rose" in data["largeCost"]["badgeClass"]

    # 9. Activity-weighted check
    assert "24x7 Activity Matrix" in data["activityWeighted"]["bannerTitle"]
    assert "18.5% of weekly activity" in data["activityWeighted"]["bannerTitle"]
    assert "320.0% unweighted linear" in data["activityWeighted"]["bannerTitle"]
    assert (
        data["activityWeighted"]["bannerTitle"] == data["activityWeighted"]["wkTitle"]
    )

    # 10. Activity-weighted without linear projection check
    assert "24x7 Activity Matrix" in data["activityWeightedNoLinear"]["bannerTitle"]
    assert "12.3% of weekly activity" in data["activityWeightedNoLinear"]["bannerTitle"]
    assert "unweighted linear" not in data["activityWeightedNoLinear"]["bannerTitle"]
    assert (
        data["activityWeightedNoLinear"]["bannerTitle"]
        == data["activityWeightedNoLinear"]["wkTitle"]
    )

    # 11. Standalone elSessWk without banner
    assert "24x7 Activity Matrix" in data["standaloneWk"]["wkTitle"]
    assert "22.0% of weekly activity" in data["standaloneWk"]["wkTitle"]


def test_query_quota_data_uses_activity_profile_when_steps_exist(tmp_path: Path):
    db_file = tmp_path / "test_dash_profile.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    for i in range(80):
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, step_type, billed_cost) VALUES ('c1', ?, '2026-10-14T14:00:00Z', 'USER_MESSAGE', 0.5)",
            (i + 1,),
        )
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "remaining_fraction": 0.85,
                "reset_time": "2026-10-20T14:00:00Z",
                "calibrated_capacity": 672.0,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    conn.commit()

    q_data = api_routes.query_quota_data(conn)
    assert q_data["weekly_pacing"]["weighting_mode"] == "activity_weighted"
    assert "elapsed_activity_pct" in q_data["weekly_pacing"]
    assert "linear_projected_pct" in q_data["weekly_pacing"]

    # Verify query_summary_data delivers activity-weighted quota in quota & quota_summary
    s_data = api_routes.query_summary_data(conn)
    assert s_data["quota"]["weekly_pacing"]["weighting_mode"] == "activity_weighted"
    assert "elapsed_activity_pct" in s_data["quota"]["weekly_pacing"]
    assert (
        s_data["quota_summary"]["weekly_pacing"]["weighting_mode"]
        == "activity_weighted"
    )

    # Verify query_session_details delivers activity-weighted quota
    sess_detail = api_routes.query_session_details(conn, "c1")
    assert (
        sess_detail["quota"]["weekly_pacing"]["weighting_mode"] == "activity_weighted"
    )
    assert "elapsed_activity_pct" in sess_detail["quota"]["weekly_pacing"]
    conn.close()

    # Verify graceful linear fallback when conn is None or has 0 steps (< MIN_PROFILE_STEPS)
    q_none = api_routes.query_quota_data(None)
    assert q_none["weekly_pacing"]["weighting_mode"] == "linear"

    db_empty = tmp_path / "test_dash_empty.db"
    init_db(db_empty)
    conn_empty = get_db_connection(db_empty)
    q_empty = api_routes.query_quota_data(conn_empty)
    assert q_empty["weekly_pacing"]["weighting_mode"] == "linear"
    conn_empty.close()

    # Verify HTTP endpoints (/api/quota, /api/summary, and SSE /api/events)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    with (
        patch(
            "context_monitor.dashboard.server.get_db_connection",
            lambda *args, **kwargs: get_db_connection(db_file),
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
            # 1. /api/quota
            req = urllib.request.Request(f"{base_url}/api/quota")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                quota_resp = json.loads(resp.read().decode("utf-8"))
                assert (
                    quota_resp["weekly_pacing"]["weighting_mode"] == "activity_weighted"
                )
                assert "elapsed_activity_pct" in quota_resp["weekly_pacing"]
                assert "linear_projected_pct" in quota_resp["weekly_pacing"]

            # 2. /api/summary
            req = urllib.request.Request(f"{base_url}/api/summary")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                summary_resp = json.loads(resp.read().decode("utf-8"))
                assert (
                    summary_resp["quota"]["weekly_pacing"]["weighting_mode"]
                    == "activity_weighted"
                )
                assert "elapsed_activity_pct" in summary_resp["quota"]["weekly_pacing"]
                assert "linear_projected_pct" in summary_resp["quota"]["weekly_pacing"]

            # 3. SSE /api/events
            req = urllib.request.Request(f"{base_url}/api/events")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                l1 = resp.readline().decode("utf-8")
                l2 = resp.readline().decode("utf-8")
                assert "event: connected" in l1
                event_data = json.loads(l2.replace("data: ", "").strip())
                assert "quota" in event_data
                assert (
                    event_data["quota"]["weekly_pacing"]["weighting_mode"]
                    == "activity_weighted"
                )
                assert "elapsed_activity_pct" in event_data["quota"]["weekly_pacing"]
                assert "linear_projected_pct" in event_data["quota"]["weekly_pacing"]
        finally:
            httpd.shutdown()
            server_thread.join(timeout=2)


def test_conversations_list_and_session_details_quota_parity(tmp_path: Path):
    """Verifies that conversation list and session detail quota percent values accurately

    match (billed_cost / calibrated_capacity) * 100.0 for Gemini and 3P models.
    """
    db_file = tmp_path / "test_parity.db"
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

        # 1. Seed Gemini conversation & subagent
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'gemini-root', 'Gemini Coordinator', 'test-proj', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:00:00Z', '2026-10-14T10:30:00Z',
                10, 50000, 7.00, 40000, 10000,
                NULL, 'gemini-root', 0, NULL, NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
            ) VALUES (
                'gemini-sub', 'Gemini Subagent', 'test-proj', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:05:00Z', '2026-10-14T10:25:00Z',
                5, 20000, 3.50, 15000, 5000,
                'gemini-root', 'gemini-root', 1, 'worker', 'subagent', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO steps (
                conversation_id, step_index, created_at, source, step_type,
                category, activity, tool_or_source, est_tokens, thinking_tokens,
                phase, model_name, billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES ('gemini-root', 1, '2026-10-14T10:00:00Z', 'assistant', 'PLANNER_RESPONSE',
                      'Agent Reasoning', 'Reasoning', 'Planner', 5000, 500,
                      'execution', 'Gemini 3.8 Flash (High)', 2.00, 4000, 1000)
            """
        )

        # 2. Seed 3P (Claude) conversation & subagent
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'claude-root', 'Claude Coordinator', 'test-proj', 'Claude 3.7 Sonnet',
                '2026-10-14T11:00:00Z', '2026-10-14T11:30:00Z',
                10, 50000, 4.00, 40000, 10000,
                NULL, 'claude-root', 0, NULL, NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
            ) VALUES (
                'claude-sub', 'Claude Subagent', 'test-proj', 'Claude 3.7 Sonnet',
                '2026-10-14T11:05:00Z', '2026-10-14T11:25:00Z',
                5, 20000, 2.00, 15000, 5000,
                'claude-root', 'claude-root', 1, 'worker', 'subagent', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO steps (
                conversation_id, step_index, created_at, source, step_type,
                category, activity, tool_or_source, est_tokens, thinking_tokens,
                phase, model_name, billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES ('claude-root', 1, '2026-10-14T11:00:00Z', 'assistant', 'PLANNER_RESPONSE',
                      'Agent Reasoning', 'Reasoning', 'Planner', 5000, 500,
                      'execution', 'Claude 3.7 Sonnet', 1.40, 4000, 1000)
            """
        )
        conn.commit()

        with patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ):
            # A. Test query_conversations_list
            convs = api_routes.query_conversations_list(conn, view_mode="hierarchy")
            gemini_c = next(c for c in convs if c["id"] == "gemini-root")
            claude_c = next(c for c in convs if c["id"] == "claude-root")

            # Gemini root (7.00 / 700.0 = 1.0%, 7.00 / 250.0 = 2.8%)
            assert gemini_c["weekly_quota_pct"] == 1.0
            assert gemini_c["five_hour_quota_pct"] == 2.8
            # Gemini fleet (10.50 / 700.0 = 1.5%, 10.50 / 250.0 = 4.2%)
            assert gemini_c["fleet_cost"] == 10.50
            assert gemini_c["fleet_weekly_quota_pct"] == 1.5
            assert gemini_c["fleet_5h_quota_pct"] == 4.2
            # Gemini subagent (3.50 / 700.0 = 0.5%, 3.50 / 250.0 = 1.4%)
            gemini_sub = gemini_c["subagents"][0]
            assert gemini_sub["weekly_quota_pct"] == 0.5
            assert gemini_sub["five_hour_quota_pct"] == 1.4

            # 3P root (4.00 / 200.0 = 2.0%, 4.00 / 70.0 = 5.7143%)
            assert claude_c["weekly_quota_pct"] == 2.0
            assert round(claude_c["five_hour_quota_pct"], 4) == 5.7143
            # 3P fleet (6.00 / 200.0 = 3.0%, 6.00 / 70.0 = 8.5714%)
            assert claude_c["fleet_cost"] == 6.00
            assert claude_c["fleet_weekly_quota_pct"] == 3.0
            assert round(claude_c["fleet_5h_quota_pct"], 4) == 8.5714
            # 3P subagent (2.00 / 200.0 = 1.0%, 2.00 / 70.0 = 2.8571%)
            claude_sub = claude_c["subagents"][0]
            assert claude_sub["weekly_quota_pct"] == 1.0
            assert round(claude_sub["five_hour_quota_pct"], 4) == 2.8571

            # B. Test query_session_details for Gemini
            g_details = api_routes.query_session_details(conn, "gemini-root")
            assert (
                round(g_details["steps"][0]["weekly_quota_pct"], 4) == 0.2857
            )  # 2.0 / 700
            assert (
                round(g_details["steps"][0]["five_hour_quota_pct"], 4) == 0.8
            )  # 2.0 / 250
            assert g_details["children"][0]["weekly_quota_pct"] == 0.5  # 3.5 / 700
            assert g_details["children"][0]["five_hour_quota_pct"] == 1.4  # 3.5 / 250
            # Swarm fleet tree nodes
            g_tree = g_details["fleet_tree"]
            assert g_tree["weekly_quota_pct"] == 1.0
            assert g_tree["five_hour_quota_pct"] == 2.8
            assert g_tree["children"][0]["weekly_quota_pct"] == 0.5
            assert g_tree["children"][0]["five_hour_quota_pct"] == 1.4

            # C. Test query_session_details for 3P (Claude)
            c_details = api_routes.query_session_details(conn, "claude-root")
            assert c_details["steps"][0]["weekly_quota_pct"] == 0.7  # 1.40 / 200
            assert c_details["steps"][0]["five_hour_quota_pct"] == 2.0  # 1.40 / 70
            assert c_details["children"][0]["weekly_quota_pct"] == 1.0  # 2.0 / 200
            assert (
                round(c_details["children"][0]["five_hour_quota_pct"], 4) == 2.8571
            )  # 2.0 / 70
            # Swarm fleet tree nodes
            c_tree = c_details["fleet_tree"]
            assert c_tree["weekly_quota_pct"] == 2.0
            assert round(c_tree["five_hour_quota_pct"], 4) == 5.7143
            assert c_tree["children"][0]["weekly_quota_pct"] == 1.0
            assert round(c_tree["children"][0]["five_hour_quota_pct"], 4) == 2.8571

            # D. Test query_conversations_list in flat mode
            flat_convs = api_routes.query_conversations_list(conn, view_mode="flat")
            flat_gemini = next(c for c in flat_convs if c["id"] == "gemini-root")
            flat_sub = next(c for c in flat_convs if c["id"] == "gemini-sub")
            assert flat_gemini["weekly_quota_pct"] == 1.0
            assert flat_gemini["five_hour_quota_pct"] == 2.8
            assert flat_sub["weekly_quota_pct"] == 0.5
            assert flat_sub["five_hour_quota_pct"] == 1.4


def test_compute_quota_pcts_interface_and_edge_cases():
    """Verifies that compute_quota_pcts strictly adheres to the Task 2 interface:

    Consumes: billed_cost: float, model_name: str, capacities: dict[str, float]
    Produces: weekly_quota_pct: float, five_hour_quota_pct: float
    And safely handles edge cases (NaN, Inf, negative, None, zero capacities).
    """
    caps_dict = {
        "gemini-weekly": 700.0,
        "gemini-5h": 250.0,
        "3p-weekly": 200.0,
        "3p-5h": 70.0,
    }

    # 1. Interface conformance: compute_quota_pcts(billed_cost, model_name, capacities: dict)
    res_gem = api_routes.compute_quota_pcts(
        billed_cost=7.0, model_name="Gemini 3.8 Flash", capacities=caps_dict
    )
    assert res_gem.weekly_quota_pct == 1.0
    assert res_gem.five_hour_quota_pct == 2.8
    # Unpack as 2-tuple
    w, f = res_gem
    assert w == 1.0
    assert f == 2.8

    # 2. 3P Model (Claude) interface conformance
    res_3p = api_routes.compute_quota_pcts(
        billed_cost=4.0, model_name="Claude 3.7 Sonnet", capacities=caps_dict
    )
    assert res_3p.weekly_quota_pct == 2.0
    assert round(res_3p.five_hour_quota_pct, 4) == 5.7143

    # 3. Positional float capacities calling convention: (cost, model_name, gw, g5, pw, p5)
    res_pos = api_routes.compute_quota_pcts(7.0, "Gemini", 700.0, 250.0, 200.0, 70.0)
    assert res_pos.weekly_quota_pct == 1.0
    assert res_pos.five_hour_quota_pct == 2.8

    # 4. Positional 4-tuple capacities calling convention
    res_tup = api_routes.compute_quota_pcts(7.0, "Gemini", (700.0, 250.0, 200.0, 70.0))
    assert res_tup.weekly_quota_pct == 1.0
    assert res_tup.five_hour_quota_pct == 2.8

    # 5. Edge cases: None cost, zero cost, negative cost
    res_none = api_routes.compute_quota_pcts(
        billed_cost=None, model_name="Gemini", capacities=caps_dict
    )
    assert res_none == (0.0, 0.0)

    res_zero = api_routes.compute_quota_pcts(
        billed_cost=0.0, model_name="Gemini", capacities=caps_dict
    )
    assert res_zero == (0.0, 0.0)

    res_neg = api_routes.compute_quota_pcts(
        billed_cost=-7.0, model_name="Gemini", capacities=caps_dict
    )
    assert res_neg.weekly_quota_pct == -1.0
    assert res_neg.five_hour_quota_pct == -2.8

    # 6. Edge cases: NaN, Inf
    res_nan = api_routes.compute_quota_pcts(
        billed_cost=float("nan"), model_name="Gemini", capacities=caps_dict
    )
    assert res_nan == (0.0, 0.0)

    res_inf = api_routes.compute_quota_pcts(
        billed_cost=float("inf"), model_name="Gemini", capacities=caps_dict
    )
    assert res_inf == (0.0, 0.0)

    # 7. Zero and negative capacities (must safely return 0.0 instead of ZeroDivisionError)
    zero_caps = {
        "gemini-weekly": 0.0,
        "gemini-5h": -10.0,
        "3p-weekly": 0.0,
        "3p-5h": -5.0,
    }
    # Note: fallback capacity takes over if dictionary values are <= 0, keeping it safe
    res_zero_cap = api_routes.compute_quota_pcts(
        cost=10.0, model_name="Gemini", capacities=zero_caps
    )
    assert res_zero_cap.weekly_quota_pct > 0.0  # fell back to positive default capacity


def test_extract_bucket_capacities_formats():
    """Verifies that extract_bucket_capacities and _extract_bucket_capacities accept

    various structures (dict of floats, dict of dicts, list of dicts, case variations).
    """
    # 1. Plain dict of floats
    caps1 = api_routes.extract_bucket_capacities(
        {"gemini-weekly": 750.0, "gemini-5h": 260.0, "3p-weekly": 210.0, "3p-5h": 75.0}
    )
    assert caps1["gemini-weekly"] == 750.0
    assert caps1["gemini-5h"] == 260.0
    assert caps1["3p-weekly"] == 210.0
    assert caps1["3p-5h"] == 75.0

    # 2. Case variations and aliases
    caps2 = api_routes.extract_bucket_capacities(
        {"Gemini-Weekly": 800.0, "GEMINI_5H": 280.0, "3P-Weekly": 220.0, "3p_5h": 80.0}
    )
    assert caps2["gemini-weekly"] == 800.0
    assert caps2["gemini-5h"] == 280.0
    assert caps2["3p-weekly"] == 220.0
    assert caps2["3p-5h"] == 80.0

    # 3. Nested dictionary (like query_quota_data or calculate_quota_metrics)
    caps3 = api_routes.extract_bucket_capacities(
        {
            "gemini-weekly": {"calibrated_capacity": 650.0},
            "gemini-5h": {"calibrated_capacity": 220.0},
            "3p-weekly": {"calibratedCapacity": 190.0},
            "3p-5h": {"capacity": 65.0},
        }
    )
    assert caps3["gemini-weekly"] == 650.0
    assert caps3["gemini-5h"] == 220.0
    assert caps3["3p-weekly"] == 190.0
    assert caps3["3p-5h"] == 65.0

    # 4. List of dict rows (like quota_state table rows)
    caps4 = api_routes.extract_bucket_capacities(
        [
            {"bucket_id": "gemini-weekly", "calibrated_capacity": 600.0},
            {"bucket_id": "gemini-5h", "calibrated_capacity": 200.0},
            {"bucketId": "3p-weekly", "calibratedCapacity": 180.0},
            {"bucket": "3p-5h", "capacity": 60.0},
        ]
    )
    assert caps4["gemini-weekly"] == 600.0
    assert caps4["gemini-5h"] == 200.0
    assert caps4["3p-weekly"] == 180.0
    assert caps4["3p-5h"] == 60.0

    # 5. Tuple unpacker helper
    gw, g5, pw, p5 = api_routes._extract_bucket_capacities(caps4)
    assert (gw, g5, pw, p5) == (600.0, 200.0, 180.0, 60.0)

    # 6. Fallback on None
    caps_none = api_routes.extract_bucket_capacities(None)
    assert caps_none["gemini-weekly"] > 0
    assert caps_none["gemini-5h"] > 0
    assert caps_none["3p-weekly"] > 0
    assert caps_none["3p-5h"] > 0


def test_query_summary_data_quota_deduplication(tmp_path: Path):
    """Verifies that query_summary_data calls query_quota_data only ONCE and reuses

    the result for both 'quota' and 'quota_summary' keys.
    """
    db_file = tmp_path / "test_dedup.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        with patch(
            "context_monitor.dashboard.api_routes.query_quota_data",
            wraps=api_routes.query_quota_data,
        ) as mock_qqd:
            res = api_routes.query_summary_data(conn)
            # Must be called exactly once
            assert mock_qqd.call_count == 1
            # Both keys must point to identical results
            assert res["quota"] == res["quota_summary"]
            assert res["quota"] is res["quota_summary"]


def _seed_timeline_test_db(conn) -> None:
    """Helper to seed 45 days of multi-project steps with boundary timestamps."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO conversations (id, title, project, model_name, created_at, total_steps, total_tokens) VALUES 
        ('c1', 'Conv 1', 'project-alpha', 'Gemini Flash', '2026-08-01T00:00:00Z', 10, 10000),
        ('c2', 'Conv 2', 'project-beta', 'Gemini Pro', '2026-08-01T00:00:00Z', 10, 10000),
        ('c3', 'Conv 3', '', 'Gemini Flash', '2026-08-01T00:00:00Z', 10, 10000)
    """
    )
    now_utc = datetime.now(timezone.utc)
    steps = []
    step_idx = 1
    # Distribute past steps across 45 days, 3 distinct hours per day
    for d in range(45):
        for offset_hours in [1, 8, 16]:
            step_dt = now_utc - timedelta(days=d, hours=offset_hours)
            ts = step_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            cid = "c1" if offset_hours == 1 else ("c2" if offset_hours == 8 else "c3")
            steps.append(
                (
                    cid,
                    step_idx,
                    "coding",
                    "code",
                    "edit",
                    "write_to_file",
                    "Gemini Flash",
                    100,
                    "",
                    ts,
                    0.05,
                    500,
                    100,
                )
            )
            step_idx += 1

    cur.executemany(
        """
        INSERT INTO steps (
            conversation_id, step_index, phase, category, activity,
            tool_or_source, model_name, est_tokens, detail, created_at,
            billed_cost, billed_input_tokens, billed_output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        steps,
    )
    conn.commit()


def test_query_timeline_data_days_optimization_parity(tmp_path: Path):
    """Verifies that query_timeline_data with days=7, 14, 30 produces exact parity

    with unbounded queries for the requested window, including steps, tokens, cost,
    sessions, and project attribution breakdown.
    """
    db_file = tmp_path / "test_timeline_parity.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        _seed_timeline_test_db(conn)

        unbounded = api_routes.query_timeline_data(conn, days=None, tz="UTC")
        assert len(unbounded) >= 45

        for d in [7, 14, 30]:
            bounded = api_routes.query_timeline_data(conn, days=d, tz="UTC")
            expected_slice = unbounded[-d:]

            assert len(bounded) == d
            assert len(expected_slice) == d

            # Detailed element-by-element assertion
            for i in range(d):
                b_item = bounded[i]
                u_item = expected_slice[i]
                assert b_item["day"] == u_item["day"]
                assert b_item["steps"] == u_item["steps"]
                assert b_item["tokens"] == u_item["tokens"]
                assert b_item["cost"] == u_item["cost"]
                assert b_item["sessions"] == u_item["sessions"]
                assert b_item["projects"] == u_item["projects"]

            # Exact object match
            assert bounded == expected_slice

        # Project filtering parity
        proj_bounded = api_routes.query_timeline_data(
            conn, days=7, tz="UTC", project="project-alpha"
        )
        proj_unbounded = api_routes.query_timeline_data(
            conn, days=None, tz="UTC", project="project-alpha"
        )
        assert len(proj_bounded) == 7
        assert proj_bounded == proj_unbounded[-7:]
        for item in proj_bounded:
            for p in item["projects"]:
                assert p == "project-alpha"

        # Unassigned project filtering parity
        unassigned_bounded = api_routes.query_timeline_data(
            conn, days=7, tz="UTC", project="(unassigned)"
        )
        unassigned_unbounded = api_routes.query_timeline_data(
            conn, days=None, tz="UTC", project="(unassigned)"
        )
        assert len(unassigned_bounded) == 7
        assert unassigned_bounded == unassigned_unbounded[-7:]
        for item in unassigned_bounded:
            for p in item["projects"]:
                assert p == "(unassigned)"


def test_query_timeline_data_timezones(tmp_path: Path):
    """Verifies that query_timeline_data date attribution and window boundaries

    accurately respect timezones without missing records shifted near UTC day boundaries,
    including extreme timezones from UTC-11 to UTC+14.
    """
    db_file = tmp_path / "test_timeline_tz.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        _seed_timeline_test_db(conn)

        # Also add boundary timestamps right before and after midnight
        now_utc = datetime.now(timezone.utc)
        boundary_steps = [
            (
                "c1",
                99901,
                "coding",
                "code",
                "edit",
                "write_to_file",
                "Gemini Flash",
                100,
                "",
                (now_utc - timedelta(days=6)).strftime("%Y-%m-%dT23:59:59Z"),
                0.05,
                500,
                100,
            ),
            (
                "c2",
                99902,
                "coding",
                "code",
                "edit",
                "write_to_file",
                "Gemini Flash",
                100,
                "",
                (now_utc - timedelta(days=7)).strftime("%Y-%m-%dT00:00:01Z"),
                0.05,
                500,
                100,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO steps (
                conversation_id, step_index, phase, category, activity,
                tool_or_source, model_name, est_tokens, detail, created_at,
                billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            boundary_steps,
        )
        conn.commit()

        test_tzs = [
            "UTC",
            "America/New_York",
            "Asia/Tokyo",
            "Pacific/Honolulu",
            "Europe/London",
            "Pacific/Kiritimati",  # UTC+14
            "Pacific/Niue",  # UTC-11
        ]
        for tz_name in test_tzs:
            unbounded = api_routes.query_timeline_data(conn, days=None, tz=tz_name)
            for d in [7, 14, 30]:
                bounded = api_routes.query_timeline_data(conn, days=d, tz=tz_name)
                assert len(bounded) == d
                assert bounded == unbounded[-d:], (
                    f"Parity mismatch for tz={tz_name}, days={d}"
                )


def test_query_timeline_data_edge_cases(tmp_path: Path):
    """Verifies edge cases for query_timeline_data: string days ('30', '7'),

    float strings ('7.0', '30.0'), zero days (0, '0'), negative days (-5), None,
    booleans (False, True), large overflow numbers, and invalid input ('invalid', '').
    """
    db_file = tmp_path / "test_timeline_edge.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        _seed_timeline_test_db(conn)

        # String days representations
        t30_int = api_routes.query_timeline_data(conn, days=30, tz="UTC")
        t30_str = api_routes.query_timeline_data(conn, days="30", tz="UTC")
        assert len(t30_str) == 30
        assert t30_str == t30_int

        t7_int = api_routes.query_timeline_data(conn, days=7, tz="UTC")
        t7_str = api_routes.query_timeline_data(conn, days="7", tz="UTC")
        assert len(t7_str) == 7
        assert t7_str == t7_int

        # Float string days representations
        t7_float_str = api_routes.query_timeline_data(conn, days="7.0", tz="UTC")
        assert len(t7_float_str) == 7
        assert t7_float_str == t7_int

        t30_float_str = api_routes.query_timeline_data(conn, days="30.0", tz="UTC")
        assert len(t30_float_str) == 30
        assert t30_float_str == t30_int

        # Zero and negative days
        assert api_routes.query_timeline_data(conn, days=0, tz="UTC") == []
        assert api_routes.query_timeline_data(conn, days="0", tz="UTC") == []
        assert api_routes.query_timeline_data(conn, days=-5, tz="UTC") == []

        # None and invalid inputs (graceful full history fallback)
        unbounded = api_routes.query_timeline_data(conn, days=None, tz="UTC")
        assert len(unbounded) >= 45

        invalid_str = api_routes.query_timeline_data(conn, days="invalid", tz="UTC")
        assert invalid_str == unbounded

        empty_str = api_routes.query_timeline_data(conn, days="", tz="UTC")
        assert empty_str == unbounded

        # Booleans should not coerce to 0/1; they fall back to unbounded full scan
        assert api_routes.query_timeline_data(conn, days=False, tz="UTC") == unbounded
        assert api_routes.query_timeline_data(conn, days=True, tz="UTC") == unbounded

        # Extreme integer overflow fallback
        assert api_routes.query_timeline_data(conn, days=10**100, tz="UTC") == unbounded


def test_query_timeline_data_uses_created_at_index(tmp_path: Path):
    """Verifies that SQLite EXPLAIN QUERY PLAN uses idx_steps_created index

    when query_timeline_data executes with a bounded date range cutoff.
    """
    db_file = tmp_path / "test_timeline_index.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        _seed_timeline_test_db(conn)

        executed_queries: list[tuple[str, tuple]] = []

        class SpyCursor:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                executed_queries.append((sql, params))
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

        class SpyConn:
            def __init__(self, real):
                self._real = real

            def cursor(self):
                return SpyCursor(self._real.cursor())

            def __getattr__(self, name):
                return getattr(self._real, name)

        spy_conn = SpyConn(conn)
        bounded_res = api_routes.query_timeline_data(spy_conn, days=7, tz="UTC")
        assert len(bounded_res) == 7

        # Extract the actual timeline query (targeting hourly_step_rollups or fallback steps)
        rollup_queries = [
            (sql, params)
            for sql, params in executed_queries
            if "FROM hourly_step_rollups" in sql and "hour_key IS NOT NULL" in sql
        ]
        if rollup_queries:
            assert len(rollup_queries) == 1
            sql, params = rollup_queries[0]
            assert "hour_key >= ?" in sql
            cur = conn.cursor()
            plan_bounded = cur.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
            bounded_details = [r["detail"] for r in plan_bounded]
            assert any(
                "SEARCH" in d and "hourly_step_rollups" in d for d in bounded_details
            ), f"Expected index search in query plan: {bounded_details}"
        else:
            steps_queries = [
                (sql, params) for sql, params in executed_queries if "FROM steps" in sql
            ]
            assert len(steps_queries) == 1, (
                f"Expected exactly 1 steps query, got: {steps_queries}"
            )
            sql, params = steps_queries[0]
            assert "created_at >= ?" in sql

            cur = conn.cursor()
            plan_bounded = cur.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
            bounded_details = [r["detail"] for r in plan_bounded]
            assert any(
                "idx_steps_created" in d and "SEARCH" in d for d in bounded_details
            ), f"Expected index search in query plan: {bounded_details}"

        # Now verify unbounded query plan
        executed_queries.clear()
        unbounded_res = api_routes.query_timeline_data(spy_conn, days=None, tz="UTC")
        assert len(unbounded_res) >= 45

        unbounded_rollup_queries = [
            (sql, params)
            for sql, params in executed_queries
            if "FROM hourly_step_rollups" in sql and "hour_key IS NOT NULL" in sql
        ]
        if unbounded_rollup_queries:
            assert len(unbounded_rollup_queries) == 1
            u_sql, u_params = unbounded_rollup_queries[0]
            assert "hour_key >= ?" not in u_sql
            plan_unbounded = cur.execute(f"EXPLAIN QUERY PLAN {u_sql}", u_params).fetchall()
            unbounded_details = [r["detail"] for r in plan_unbounded]
            assert any(
                "SCAN hourly_step_rollups" in d for d in unbounded_details
            ), f"Expected full scan in unbounded query plan: {unbounded_details}"
        else:
            unbounded_steps_queries = [
                (sql, params) for sql, params in executed_queries if "FROM steps" in sql
            ]
            assert len(unbounded_steps_queries) == 1
            u_sql, u_params = unbounded_steps_queries[0]
            assert "created_at >= ?" not in u_sql

            plan_unbounded = cur.execute(f"EXPLAIN QUERY PLAN {u_sql}", u_params).fetchall()
            unbounded_details = [r["detail"] for r in plan_unbounded]
            assert any("SCAN steps" in d for d in unbounded_details), (
                f"Expected full table scan in unbounded query plan: {unbounded_details}"
            )


def test_query_timeline_data_malformed_timestamps_graceful(tmp_path: Path):
    """Verifies that rows with malformed or non-ISO timestamps do not crash query_timeline_data."""
    db_file = tmp_path / "test_timeline_malformed.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        _seed_timeline_test_db(conn)

        # Inject malformed created_at rows into steps
        malformed_rows = [
            (
                "c1",
                88801,
                "coding",
                "code",
                "edit",
                "write_to_file",
                "Gemini Flash",
                100,
                "",
                "invalid-iso-string",
                0.05,
                500,
                100,
            ),
            (
                "c2",
                88802,
                "coding",
                "code",
                "edit",
                "write_to_file",
                "Gemini Flash",
                100,
                "",
                "2026-99-99 99:99",
                0.05,
                500,
                100,
            ),
            (
                "c3",
                88803,
                "coding",
                "code",
                "edit",
                "write_to_file",
                "Gemini Flash",
                100,
                "",
                "not-a-timestamp",
                0.05,
                500,
                100,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO steps (
                conversation_id, step_index, phase, category, activity,
                tool_or_source, model_name, est_tokens, detail, created_at,
                billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            malformed_rows,
        )
        conn.commit()

        # Both bounded and unbounded should succeed without unhandled exceptions
        bounded = api_routes.query_timeline_data(conn, days=7, tz="UTC")
        assert len(bounded) == 7

        unbounded = api_routes.query_timeline_data(conn, days=None, tz="UTC")
        assert len(unbounded) >= 45
        assert bounded == unbounded[-7:]


def test_query_timeline_data_cycle_mode_with_quota_reset(tmp_path: Path):
    """Verifies that query_timeline_data with days='cycle' generates a static 7-day quota cycle:
    - Spans from start date to reset date (start_dt.date() to reset_dt.date()).
    - Far-left item has is_start == True and compact %a %H:%M label (e.g. Tue 21:27).
    - Far-right item has is_reset == True, %a %H:%M label, and is_future == True.
    - Intermediate days have YYYY-MM-DD label.
    - Days after today have is_future == True, 0 tokens, and 0.0 cost.
    - Only steps with created_at >= start_dt_utc are included in the week start day count.
    """
    db_file = tmp_path / "test_cycle.db"
    init_db(db_file)
    tz_str = "America/Chicago"
    target_tz = ZoneInfo(tz_str)
    now_local = datetime.now(target_tz)
    today = now_local.date()

    # Anchor reset_dt_local: exactly 5 days from today at 21:27:00
    reset_date_local = today + timedelta(days=5)
    reset_dt_local = datetime(
        reset_date_local.year,
        reset_date_local.month,
        reset_date_local.day,
        21,
        27,
        0,
        tzinfo=target_tz,
    )
    reset_dt_utc = reset_dt_local.astimezone(timezone.utc)
    start_dt_utc = reset_dt_utc - timedelta(days=7)
    start_dt_local = start_dt_utc.astimezone(target_tz)

    start_date = start_dt_local.date()
    end_date = reset_dt_local.date()

    with get_db_connection(db_file) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.75,
                    "reset_time": reset_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "calibrated_capacity": 700.0,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )

        conn.execute(
            "INSERT INTO conversations (id, title, project, model_name, created_at) "
            "VALUES ('c1', 'Conv 1', 'project-a', 'Gemini Flash', ?)",
            (start_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )

        # Seed steps:
        # 1. Step on start_date but BEFORE start_dt_utc (2 hours prior) -> must be EXCLUDED
        ts_before = (start_dt_utc - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 2. Step on start_date AFTER start_dt_utc (1 hour after) -> must be INCLUDED
        ts_after = (start_dt_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 3. Step on today (middle of cycle)
        ts_today = (
            datetime(today.year, today.month, today.day, 12, 0, 0, tzinfo=target_tz)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        # 4. Step far in the past (10 days ago) -> must be EXCLUDED
        ts_past = (start_dt_utc - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 5. Step after reset (reset_dt + 2h) -> must be EXCLUDED
        ts_post_reset = (reset_dt_utc + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        steps_to_insert = [
            ("c1", 1, "exec", "cat", "act", "t", "m", 100, "", ts_before, 1.50, 800, 200),
            ("c1", 2, "exec", "cat", "act", "t", "m", 100, "", ts_after, 3.00, 2000, 500),
            ("c1", 3, "exec", "cat", "act", "t", "m", 100, "", ts_today, 5.00, 4000, 1000),
            ("c1", 4, "exec", "cat", "act", "t", "m", 100, "", ts_past, 2.00, 1000, 500),
            ("c1", 5, "exec", "cat", "act", "t", "m", 100, "", ts_post_reset, 4.00, 3000, 1000),
        ]
        conn.executemany(
            """
            INSERT INTO steps (
                conversation_id, step_index, phase, category, activity,
                tool_or_source, model_name, est_tokens, detail, created_at,
                billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            steps_to_insert,
        )
        conn.commit()

        # Query cycle data
        timeline = api_routes.query_timeline_data(conn, days="cycle", tz=tz_str)

        # Assertions
        assert len(timeline) >= 8  # 7-day cycle spans start_date through reset_date
        assert timeline[0]["day"] == start_date.strftime("%Y-%m-%d")
        assert timeline[-1]["day"] == end_date.strftime("%Y-%m-%d")

        # Far-left item (week start)
        first = timeline[0]
        assert first["is_start"] is True
        assert first["is_reset"] is False
        assert first["label"] == start_dt_local.strftime("%a %H:%M")
        # Verify strict boundary filtering: step_before excluded, only step_after counted
        assert first["steps"] == 1
        assert first["tokens"] == 2500  # 2000 in + 500 out
        assert first["cost"] == 3.00

        # Far-right item (reset day)
        last = timeline[-1]
        assert last["is_start"] is False
        assert last["is_reset"] is True
        assert last["label"] == reset_dt_local.strftime("%a %H:%M")
        assert last["is_future"] is True
        assert last["tokens"] == 0
        assert last["cost"] == 0.0
        assert last["steps"] == 0

        # Intermediate days and future days checks
        for item in timeline[1:-1]:
            assert item["is_start"] is False
            assert item["is_reset"] is False
            assert item["label"] == item["day"]
            item_date = datetime.strptime(item["day"], "%Y-%m-%d").date()
            if item_date > today:
                assert item["is_future"] is True
                assert item["tokens"] == 0
                assert item["cost"] == 0.0
                assert item["steps"] == 0
            elif item_date == today:
                assert item["is_future"] is False
                assert item["tokens"] == 5000
                assert item["cost"] == 5.00
                assert item["steps"] == 1
            else:
                assert item["is_future"] is False


def test_query_timeline_data_cycle_mode_fallback_without_quota(tmp_path: Path):
    """Verifies that if quota_state has no reset time, days='cycle' gracefully falls back
    to a standard 7-day window ending today.
    """
    db_file = tmp_path / "test_fallback.db"
    init_db(db_file)
    tz_str = "America/Chicago"
    target_tz = ZoneInfo(tz_str)
    today = datetime.now(target_tz).date()

    with get_db_connection(db_file) as conn:
        # Without any quota_state rows
        timeline = api_routes.query_timeline_data(conn, days="cycle", tz=tz_str)
        assert len(timeline) == 7
        assert timeline[-1]["day"] == today.strftime("%Y-%m-%d")
        assert timeline[0]["day"] == (today - timedelta(days=6)).strftime("%Y-%m-%d")
        for item in timeline:
            assert item["is_start"] is False
            assert item["is_reset"] is False
            assert item["is_future"] is False
            assert item["label"] == item["day"]

        # Also test with empty reset_time in quota_state
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 1.0,
                    "reset_time": "",
                    "calibrated_capacity": 700.0,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        timeline_empty_reset = api_routes.query_timeline_data(
            conn, days="reset", tz=tz_str
        )
        assert len(timeline_empty_reset) == 7
        assert timeline_empty_reset[-1]["day"] == today.strftime("%Y-%m-%d")


def test_query_timeline_data_cycle_project_filtering(tmp_path: Path):
    """Verifies project filtering correctly isolates project tokens within the cycle window."""
    db_file = tmp_path / "test_project_filter.db"
    init_db(db_file)
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    reset_dt_utc = now_utc + timedelta(days=4)

    with get_db_connection(db_file) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.8,
                    "reset_time": reset_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "calibrated_capacity": 700.0,
                    "fetched_at": now_utc.isoformat(),
                }
            ],
        )

        conn.execute(
            "INSERT INTO conversations (id, title, project) VALUES "
            "('c_alpha', 'Alpha Conv', 'project-alpha'), "
            "('c_beta', 'Beta Conv', 'project-beta')"
        )

        now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.executemany(
            """
            INSERT INTO steps (
                conversation_id, step_index, phase, category, activity,
                tool_or_source, model_name, est_tokens, detail, created_at,
                billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES (?, ?, 'exec', 'cat', 'act', 't', 'm', 100, '', ?, ?, ?, ?)
            """,
            [
                ("c_alpha", 1, now_iso, 2.00, 3000, 1000),  # 4000 tokens
                ("c_beta", 1, now_iso, 3.50, 5000, 2000),   # 7000 tokens
            ],
        )
        conn.commit()

        # Query for project-alpha
        tl_alpha = api_routes.query_timeline_data(
            conn, days="cycle", tz="UTC", project="project-alpha"
        )
        today_str = now_utc.strftime("%Y-%m-%d")
        today_alpha = next(d for d in tl_alpha if d["day"] == today_str)
        assert today_alpha["tokens"] == 4000
        assert today_alpha["cost"] == 2.00
        assert today_alpha["projects"] == {"project-alpha": 4000}

        # Query for project-beta
        tl_beta = api_routes.query_timeline_data(
            conn, days="cycle", tz="UTC", project="project-beta"
        )
        today_beta = next(d for d in tl_beta if d["day"] == today_str)
        assert today_beta["tokens"] == 7000
        assert today_beta["cost"] == 3.50
        assert today_beta["projects"] == {"project-beta": 7000}

        # Query all
        tl_all = api_routes.query_timeline_data(conn, days="cycle", tz="UTC")
        today_all = next(d for d in tl_all if d["day"] == today_str)
        assert today_all["tokens"] == 11000
        assert today_all["cost"] == 5.50
        assert today_all["projects"] == {"project-alpha": 4000, "project-beta": 7000}


def test_get_target_tz_handles_zoneinfo_and_tzinfo_instances(monkeypatch):
    """Verifies get_target_tz directly returns ZoneInfo and tzinfo instances without
    raising TypeError or falling back to environment/system local timezone.
    """
    monkeypatch.setenv("TZ", "UTC")
    chicago_tz = ZoneInfo("America/Chicago")
    tokyo_tz = ZoneInfo("Asia/Tokyo")

    # ZoneInfo instance passed
    assert api_routes.get_target_tz(chicago_tz) == chicago_tz
    assert api_routes.get_target_tz(tokyo_tz) == tokyo_tz

    # datetime.timezone instance passed
    assert api_routes.get_target_tz(timezone.utc) == timezone.utc

    # String passed
    assert api_routes.get_target_tz("America/Chicago") == chicago_tz
    assert api_routes.get_target_tz("  America/Chicago  ") == chicago_tz


def test_query_timeline_data_cycle_edge_cases(tmp_path: Path):
    """Verifies edge cases for cycle mode:
    - Subsecond reset_time and created_at timestamps are included without dropped boundaries
    - Timestamps with spaces (e.g. 'YYYY-MM-DD HH:MM:SS') are correctly matched
    - Expired/past reset times gracefully fall back to 7 days ending today
    - Leading/trailing whitespace in days parameter (e.g. '  cycle  ')
    - Passing tz as a ZoneInfo instance
    - Fallback to 3p-weekly bucket when gemini-weekly is absent
    """
    db_file = tmp_path / "test_cycle_edge.db"
    init_db(db_file)
    tz = ZoneInfo("America/Chicago")
    today = datetime.now(tz).date()

    with get_db_connection(db_file) as conn:
        now_utc = datetime.now(timezone.utc).replace(microsecond=500000)
        reset_dt_utc = now_utc + timedelta(days=4)
        start_dt_utc = reset_dt_utc - timedelta(days=7)

        # 1. Test sub-second timestamps and space-separated timestamps
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.8,
                    "reset_time": reset_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.500Z"),
                    "calibrated_capacity": 700.0,
                    "fetched_at": now_utc.isoformat(),
                }
            ],
        )

        conn.execute(
            "INSERT INTO conversations (id, title, project) VALUES ('c_edge', 'Edge Conv', 'p1')"
        )

        # Insert:
        # a) Step at exact start_dt_utc with subseconds (.500Z) -> must be INCLUDED
        # b) Step on start date with space separator ('YYYY-MM-DD HH:MM:SS') after start_dt_utc -> must be INCLUDED
        # c) Step 10 seconds before start_dt_utc -> must be EXCLUDED
        ts_exact_subsecond = start_dt_utc.strftime("%Y-%m-%dT%H:%M:%S.500Z")
        ts_space = (start_dt_utc + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        ts_before = (start_dt_utc - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn.executemany(
            """
            INSERT INTO steps (
                conversation_id, step_index, phase, category, activity,
                tool_or_source, model_name, est_tokens, detail, created_at,
                billed_cost, billed_input_tokens, billed_output_tokens
            ) VALUES (?, ?, 'exec', 'cat', 'act', 't', 'm', 100, '', ?, 1.0, 500, 500)
            """,
            [
                ("c_edge", 1, ts_exact_subsecond),
                ("c_edge", 2, ts_space),
                ("c_edge", 3, ts_before),
            ],
        )
        conn.commit()

        # Query passing ZoneInfo instance and whitespace in '  cycle  '
        tl = api_routes.query_timeline_data(conn, days="  cycle  ", tz=tz)
        assert len(tl) >= 8
        first_day = tl[0]
        assert first_day["is_start"] is True
        # Both ts_exact_subsecond and ts_space are included, ts_before excluded
        assert first_day["steps"] == 2
        assert first_day["tokens"] == 2000

        # 2. Test expired reset time in the past falls back to 7 days ending today
        past_reset = datetime.now(timezone.utc) - timedelta(days=10)
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.8,
                    "reset_time": past_reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "calibrated_capacity": 700.0,
                    "fetched_at": past_reset.isoformat(),
                }
            ],
        )
        tl_expired = api_routes.query_timeline_data(conn, days="cycle", tz=tz)
        assert len(tl_expired) == 7
        assert tl_expired[-1]["day"] == today.strftime("%Y-%m-%d")
        assert tl_expired[0]["day"] == (today - timedelta(days=6)).strftime("%Y-%m-%d")
        assert all(item["is_start"] is False for item in tl_expired)
        assert all(item["is_reset"] is False for item in tl_expired)

        # 3. Test fallback to 3p-weekly bucket when gemini-weekly is absent
        conn.execute("DELETE FROM quota_state")
        upcoming_3p = datetime.now(timezone.utc) + timedelta(days=3)
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "3p-weekly",
                    "group_name": "Claude and GPT models",
                    "window": "weekly",
                    "remaining_fraction": 0.9,
                    "reset_time": upcoming_3p.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "calibrated_capacity": 200.0,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        tl_3p = api_routes.query_timeline_data(conn, days="cycle", tz=tz)
        assert len(tl_3p) >= 8
        assert tl_3p[0]["is_start"] is True
        assert tl_3p[-1]["is_reset"] is True


def test_frontend_debouncing_and_abort_controller():
    """Verifies Task 4 frontend optimizations in script.js:
    - Trailing debounce timer for db_update SSE events
    - AbortController with signal passing for dashboard fetch requests
    - Lazy heatmap mode rendering rather than all 5 grids concurrently
    - Scoped Lucide icon rendering on updated subtrees
    """
    dashboard_dir = (
        Path(__file__).resolve().parent.parent / "src" / "context_monitor" / "dashboard"
    )
    js_file = dashboard_dir / "static" / "script.js"
    js_content = js_file.read_text(encoding="utf-8")

    # 1. Trailing debounce timer for db_update
    assert "dbUpdateDebounceTimer" in js_content, (
        "Expected dbUpdateDebounceTimer in script.js"
    )
    assert "clearTimeout(dbUpdateDebounceTimer)" in js_content
    assert "setTimeout(" in js_content
    assert "2000" in js_content

    # 2. AbortController with signal passing
    assert "dashboardAbortController" in js_content, (
        "Expected dashboardAbortController in script.js"
    )
    assert "new AbortController()" in js_content
    assert "dashboardAbortController.abort()" in js_content
    assert (
        "fetchOpts = signal ? { signal } : {}" in js_content
        or "{ signal }" in js_content
    )
    assert "AbortError" in js_content

    # 3. Lazy heatmap mode rendering
    assert "renderActiveHeatmapGrid" in js_content
    assert "renderActiveHeatmapGrid(data)" in js_content
    heatmap_fn_body = js_content.split("function renderHeatmapAllViews(data) {")[
        1
    ].split("if (activeHeatmapHover)")[0]
    assert "renderYearGrid(daily);" not in heatmap_fn_body
    assert "renderQuarterGrid(daily);" not in heatmap_fn_body
    assert "renderMonthHourlyGrid(" not in heatmap_fn_body
    assert "renderPast7HourlyGrid(" not in heatmap_fn_body
    assert "renderHourlyAggregateGrid(" not in heatmap_fn_body

    # In setHeatmapMode, renderActiveHeatmapGrid is invoked when switching
    set_mode_body = js_content.split("function setHeatmapMode(mode) {")[1].split(
        "function setTimelineRange"
    )[0]
    assert "renderActiveHeatmapGrid" in set_mode_body
    assert "window.setHeatmapMode = setHeatmapMode" in js_content

    # 4. Scoped Lucide icon rendering and modal centering
    assert "lucide.createIcons({ root: tbody })" in js_content
    assert "lucide.createIcons({ root: modal })" in js_content
    assert "lucide.createIcons({ root: treeEl })" in js_content
    assert "lucide.createIcons({ root: container })" in js_content
    assert "modal.classList.add('flex')" in js_content


def test_frontend_lazy_heatmap_and_abort_execution():
    """Deeply verifies lazy heatmap rendering, SSE debouncing, and AbortController request cancellation via Node execution."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    const calls = {
      year: 0,
      quarter: 0,
      month: 0,
      past7: 0,
      hourly: 0
    };

    const mockElements = {
      streakCurrent: { textContent: '' },
      streakLongest: { textContent: '' },
      activeDaysCount: { textContent: '' },
      heatmapOpsCount: { textContent: '' },
      ghYearView: { className: '' },
      ghQuarterView: { className: '' },
      ghMonthView: { className: '' },
      ghPast7View: { className: '' },
      ghHourlyView: { className: '' },
      hmBtnYear: { className: '' },
      hmBtnQuarter: { className: '' },
      hmBtnMonth: { className: '' },
      hmBtnPast7: { className: '' },
      hmBtnHourly: { className: '' },
      ghTooltipText: { innerHTML: '', textContent: '' },
      sessionModal: { classList: { add: () => {}, remove: () => {} } }
    };

    const mockDocument = {
      getElementById: (id) => mockElements[id] || { textContent: '', className: '', style: {}, addEventListener: () => {}, classList: { add: () => {}, remove: () => {} } },
      querySelectorAll: () => [],
      addEventListener: () => {}
    };

    const mockWindow = {
      location: { search: '' },
      addEventListener: () => {},
      history: { pushState: () => {} }
    };

    let fetchCount = 0;
    let fetchSignals = [];
    let shouldRejectWithAbort = false;

    const mockFetch = (url, opts) => {
      fetchSignals.push(opts ? opts.signal : null);
      fetchCount++;
      if (shouldRejectWithAbort) {
        const err = new Error('The operation was aborted');
        err.name = 'AbortError';
        return Promise.reject(err);
      }
      return Promise.resolve({
        json: () => Promise.resolve({
          models: [],
          categories: {},
          phases: {},
          daily: {},
          streak: { current: 1, longest: 1, total_active_days: 1 }
        })
      });
    };

    const sseListeners = {};
    class MockEventSource {
      constructor(url) { this.url = url; }
      addEventListener(evt, cb) { sseListeners[evt] = cb; }
      close() {}
    }
    global.EventSource = MockEventSource;

    const fn = new Function('window', 'document', 'Chart', 'lucide', 'calls', 'fetch', `
      ${code.replace('function renderYearGrid(', 'function renderYearGrid(...args) { calls.year++; return; }; function _orig_renderYearGrid(')
            .replace('function renderQuarterGrid(', 'function renderQuarterGrid(...args) { calls.quarter++; return; }; function _orig_renderQuarterGrid(')
            .replace('function renderMonthHourlyGrid(', 'function renderMonthHourlyGrid(...args) { calls.month++; return; }; function _orig_renderMonthHourlyGrid(')
            .replace('function renderPast7HourlyGrid(', 'function renderPast7HourlyGrid(...args) { calls.past7++; return; }; function _orig_renderPast7HourlyGrid(')
            .replace('function renderHourlyAggregateGrid(', 'function renderHourlyAggregateGrid(...args) { calls.hourly++; return; }; function _orig_renderHourlyAggregateGrid(')}
      return {
        renderHeatmapAllViews,
        setHeatmapMode,
        fetchDashboardData,
        initLiveSyncSSE,
        getMode: () => currentHeatmapMode,
        getAbortController: () => dashboardAbortController
      };
    `);

    const api = fn(
      mockWindow,
      mockDocument,
      { defaults: { font: {}, elements: {}, plugins: {} }, register: () => {} },
      { createIcons: () => {} },
      calls,
      mockFetch
    );

    const dummyData = {
      daily: { '2026-10-14': { steps: 10, tokens: 500, sessions: 1 } },
      streak: { current: 5, longest: 10, total_active_days: 20 },
      past_30_days_hourly: {},
      past_7_days_hourly: {},
      hourly_aggregate: []
    };

    async function testSuite() {
      // 1. Initial refresh with default mode ('year') -> Only year grid rendered
      api.renderHeatmapAllViews(dummyData);
      if (calls.year !== 1 || calls.quarter !== 0 || calls.month !== 0 || calls.past7 !== 0 || calls.hourly !== 0) {
        throw new Error(`Expected only year grid rendered once, got ${JSON.stringify(calls)}`);
      }

      // 2. Switch to 'month' -> renders month grid on demand
      api.setHeatmapMode('month');
      if (calls.month !== 1) {
        throw new Error(`Expected month grid rendered on switch, got ${JSON.stringify(calls)}`);
      }

      // 3. Switch to 'quarter' -> renders quarter grid on demand
      api.setHeatmapMode('quarter');
      if (calls.quarter !== 1) {
        throw new Error(`Expected quarter grid rendered on switch, got ${JSON.stringify(calls)}`);
      }

      // 4. Switch to 'past7hourly' -> renders past7 grid on demand
      api.setHeatmapMode('past7hourly');
      if (calls.past7 !== 1) {
        throw new Error(`Expected past7 grid rendered on switch, got ${JSON.stringify(calls)}`);
      }

      // 5. Switch to 'hourly' -> renders hourly grid on demand
      api.setHeatmapMode('hourly');
      if (calls.hourly !== 1) {
        throw new Error(`Expected hourly grid rendered on switch, got ${JSON.stringify(calls)}`);
      }

      // 6. Data refresh while in 'hourly' mode -> only hourly grid refreshed
      api.renderHeatmapAllViews(dummyData);
      if (calls.hourly !== 2 || calls.year !== 1) {
        throw new Error(`Expected only active hourly grid refreshed, got ${JSON.stringify(calls)}`);
      }

      // 7. Test in-flight cancellation via AbortController
      api.fetchDashboardData();
      const ctrl1 = api.getAbortController();
      if (!ctrl1) throw new Error('AbortController should be initialized on fetchDashboardData');
      if (ctrl1.signal.aborted) throw new Error('Initial signal should not be aborted');

      // Trigger second request while first is in-flight
      api.fetchDashboardData();
      const ctrl2 = api.getAbortController();
      if (!ctrl1.signal.aborted) throw new Error('First controller should be aborted by second fetch');
      if (ctrl2.signal.aborted) throw new Error('Second controller should remain active');
      if (fetchSignals.length === 0 || !fetchSignals[0]) throw new Error('Signal was not passed to fetch');

      // 8. Test SSE db_update debouncing: rapid burst collapses into a single trailing fetch
      api.initLiveSyncSSE();
      if (!sseListeners['db_update']) throw new Error('db_update SSE listener was not registered');

      const beforeBurstFetches = fetchCount;
      for (let i = 0; i < 5; i++) {
        sseListeners['db_update']({ data: JSON.stringify({ quota: { buckets: {} } }) });
        await new Promise(r => setTimeout(r, 10));
      }

      // Ensure fetch has NOT fired before the 2000ms trailing timer elapses
      if (fetchCount !== beforeBurstFetches) {
        throw new Error(`Debounce failed: fetch occurred early (${fetchCount - beforeBurstFetches} calls during burst)`);
      }

      // Advance past trailing timer
      await new Promise(r => setTimeout(r, 2050));

      // Exactly 8 fetches for the single collapsed Promise.all fetch (active timeline only)
      if (fetchCount !== beforeBurstFetches + 8) {
        throw new Error(`Expected exactly 8 fetches after debounce window, got ${fetchCount - beforeBurstFetches}`);
      }

      // 9. Test AbortError rejection handling: should not throw unhandled rejection
      shouldRejectWithAbort = true;
      await api.fetchDashboardData();

      console.log(JSON.stringify({
        status: 'ok',
        calls,
        signalsCaptured: fetchSignals.length,
        debounceVerified: true
      }));
    }

    testSuite().catch(err => {
      console.error(err);
      process.exit(1);
    });
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    json_line = [
        line for line in res.stdout.strip().splitlines() if line.startswith("{")
    ][-1]
    data = json.loads(json_line)
    assert data["status"] == "ok"
    assert data["calls"]["year"] == 1
    assert data["calls"]["hourly"] == 2
    assert data["signalsCaptured"] > 0
    assert data.get("debounceVerified") is True


def test_frontend_swarm_view_and_campaign_cost_elements():
    """Verifies Task 4 frontend elements in index.html and script.js:
    - HTML: #btnSwarmList and #btnSwarmTree buttons with onclick handlers
    - Script: window.setSwarmLayoutMode toggle functionality
    - Script: Scaled indentation in filterConversations for nested subagents
    - Script: Project cost top cost drivers table with +subagent badge and dual tooltips
    - Script: Inspect session modal with dual Local / Fleet tokens & steps
    - Script: Swarm modal with list view (mini context bars, quota %, compactions, inspect button) and tree view
    """
    dashboard_dir = (
        Path(__file__).resolve().parent.parent / "src" / "context_monitor" / "dashboard"
    )
    html_file = dashboard_dir / "templates" / "index.html"
    js_file = dashboard_dir / "static" / "script.js"

    html_content = html_file.read_text(encoding="utf-8")
    js_content = js_file.read_text(encoding="utf-8")

    # 1. HTML assertions
    assert 'id="btnSwarmList"' in html_content
    assert 'id="btnSwarmTree"' in html_content
    assert "window.setSwarmLayoutMode('list')" in html_content
    assert "window.setSwarmLayoutMode('tree')" in html_content
    assert 'id="sessionViewSwarm"' in html_content
    assert 'id="modalSwarmSummary"' in html_content
    assert 'id="modalSwarmTree"' in html_content

    # 2. Static script.js assertions
    assert "let currentSwarmLayoutMode = 'list';" in js_content
    assert "window.setSwarmLayoutMode = setSwarmLayoutMode;" in js_content
    assert "function setSwarmLayoutMode(mode)" in js_content
    assert "const padClass = depth === 1 ? 'pl-8' : depth === 2 ? 'pl-12' : 'pl-16';" in js_content
    assert "+${s.subagent_count}" in js_content
    assert "Local: $" in js_content
    assert "Fleet: $" in js_content
    assert "${formatTokens(tokVal)} / ${formatTokens(data.fleet_summary.total_tokens)} fleet" in js_content
    assert "${stepVal.toLocaleString()} / ${data.fleet_summary.total_steps.toLocaleString()} fleet" in js_content
    assert "fleet_nodes" in js_content
    assert "context_pct" in js_content
    assert "weekly_quota_pct" in js_content
    assert "Inspect ➔" in js_content

    # 3. Dynamic Node.js DOM verification
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('src/context_monitor/dashboard/static/script.js', 'utf8');

    // Minimal DOM stub
    function createMockElement() {
      let _html = '';
      return {
        className: '',
        textContent: '',
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        style: {},
        children: [],
        appendChild(child) { this.children.push(child); },
        classList: {
          add(cls) { if (!this.className.includes(cls)) this.className += ' ' + cls; },
          remove(cls) { this.className = this.className.replace(cls, '').trim(); }
        }
      };
    }

    const mockElements = {
      btnSwarmList: createMockElement(),
      btnSwarmTree: createMockElement(),
      modalSwarmSummary: createMockElement(),
      modalSwarmBadge: createMockElement(),
      modalSwarmTree: createMockElement(),
      kpiDateRange: createMockElement(),
      topCostDriversBody: createMockElement(),
      topCostDriversSub: createMockElement(),
      topCostScopeBadge: createMockElement(),
      topCostCountBadge: createMockElement(),
      modalTokens: createMockElement(),
      modalTokensSub: createMockElement(),
      modalSteps: createMockElement(),
      modalStepsSub: createMockElement()
    };

    const mockDocument = {
      getElementById: (id) => mockElements[id] || createMockElement(),
      createElement: (tag) => createMockElement(),
      querySelectorAll: () => [],
      addEventListener: () => {}
    };

    const mockWindow = {
      location: { search: '' },
      addEventListener: () => {},
      history: { pushState: () => {} }
    };

    const fn = new Function('window', 'document', 'Chart', 'lucide', `
      ${code}
      return {
        setSwarmLayoutMode,
        renderSwarmView,
        renderKPIs,
        renderProjectCostSection,
        getSwarmLayoutMode: () => currentSwarmLayoutMode,
        setSessionData: (d) => { currentSessionData = d; }
      };
    `);

    const api = fn(
      mockWindow,
      mockDocument,
      { defaults: { font: {}, elements: {}, plugins: {} }, register: () => {} },
      { createIcons: () => {} }
    );

    // Test 1: Layout mode switching
    if (api.getSwarmLayoutMode() !== 'list') throw new Error('Default mode should be list');
    api.setSwarmLayoutMode('tree');
    if (api.getSwarmLayoutMode() !== 'tree') throw new Error('Mode should be tree after setSwarmLayoutMode');
    if (!mockElements.btnSwarmTree.className.includes('bg-brand-600')) throw new Error('Tree button should have active style');
    api.setSwarmLayoutMode('list');
    if (api.getSwarmLayoutMode() !== 'list') throw new Error('Mode should be list after setSwarmLayoutMode');
    if (!mockElements.btnSwarmList.className.includes('bg-brand-600')) throw new Error('List button should have active style');

    // Test 2: Hero KPI Campaign breakdown and zero-subagents reset
    api.renderKPIs({ root_convs: 12, subagent_convs: 4, total_conversations: 16 });
    if (!mockElements.kpiDateRange.textContent.includes('12 campaigns · 4 sub-agents')) {
      throw new Error(`Expected campaign breakdown in kpiDateRange, got: ${mockElements.kpiDateRange.textContent}`);
    }
    api.renderKPIs({ root_convs: 10, subagent_convs: 0, total_conversations: 10 });
    if (mockElements.kpiDateRange.textContent.includes('sub-agents')) {
      throw new Error(`Expected kpiDateRange to reset when subagent_convs=0, got: ${mockElements.kpiDateRange.textContent}`);
    }

    // Test 3: Top Cost Drivers row rendering with +subagent badge
    api.renderProjectCostSection({
      top_sessions: [
        { id: 'c1', title: 'Main Campaign', project: 'test', model_name: 'Gemini Pro', billed_cost: 15.00, local_cost: 5.00, subagent_count: 2, total_tokens: 30000 },
        { id: 'c2', title: 'Solo Task', project: 'test', model_name: 'Flash', billed_cost: 2.00, local_cost: 2.00, subagent_count: 0, total_tokens: 5000 }
      ]
    }, 'all');

    const rows = mockElements.topCostDriversBody.children;
    if (rows.length !== 2) throw new Error(`Expected 2 rows, got ${rows.length}`);
    if (!rows[0].innerHTML.includes('+2')) throw new Error('First row should have +2 subagent badge');
    if (rows[1].innerHTML.includes('+')) throw new Error('Second row should not have subagent badge');
    if (!rows[0].innerHTML.includes('Local: $5.00 · Fleet: $15.00')) throw new Error('First row should have dual Local/Fleet tooltip');

    // Test 4: Swarm List View Rendering and NaN Resilience
    const swarmData = {
      conversation: { id: 'root', title: 'Root Coordinator', billed_cost: 5.00, total_tokens: 10000, total_steps: 10 },
      fleet_summary: { subagent_count: 1, total_cost: 15.00, total_tokens: 30000, total_steps: 25 },
      fleet_nodes: [
        { id: 'sub-1', title: 'Worker Agent', role: 'Worker', model_name: 'Gemini Flash', cost: 10.00, tokens: 20000, steps: 15, context_pct: 65, weekly_quota_pct: 1.48, is_current: false },
        { id: 'root', title: 'Root Coordinator', role: 'Root Coordinator', model_name: 'Gemini Pro', cost: 5.00, tokens: 10000, steps: 10, context_pct: NaN, weekly_quota_pct: NaN, is_current: true }
      ]
    };
    api.setSessionData(swarmData);
    api.setSwarmLayoutMode('list');
    api.renderSwarmView(swarmData);

    const swarmListCards = mockElements.modalSwarmTree.children;
    if (swarmListCards.length !== 2) throw new Error(`Expected 2 cards in swarm list view, got ${swarmListCards.length}`);
    if (!swarmListCards[0].innerHTML.includes('#1')) throw new Error('First card should have rank #1');
    if (!swarmListCards[0].innerHTML.includes('$10.00')) throw new Error('First card should show highest cost ($10.00)');
    if (!swarmListCards[0].innerHTML.includes('(1.48% wk)')) throw new Error('First card should show weekly quota pct');
    if (!swarmListCards[0].innerHTML.includes('Inspect ➔')) throw new Error('Non-current card should have Inspect button');
    if (swarmListCards[1].innerHTML.includes('Inspect ➔')) throw new Error('Current card should not have Inspect button');
    if (swarmListCards[1].innerHTML.includes('NaN')) throw new Error('Second card should not render NaN for quota or context');

    console.log(JSON.stringify({ status: 'ok', tested: ['layoutMode', 'kpiHero', 'costDrivers', 'swarmList'] }));
    """

    res = subprocess.run(
        ["node", "-e", node_script], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    json_line = [
        line for line in res.stdout.strip().splitlines() if line.startswith("{")
    ][-1]
    data = json.loads(json_line)
    assert data["status"] == "ok"
    assert len(data["tested"]) == 4


def test_query_conversations_list_sorting_and_updated_at(tmp_path: Path):
    """Verifies query_conversations_list sorts by COALESCE(updated_at, created_at) DESC and returns updated_at."""
    db_file = tmp_path / "test_sort.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        # Conv 1: older created_at, but updated recently (active session)
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at, total_steps, total_tokens)
            VALUES ('c_older_active', 'Older Active', '2026-10-19T10:00:00Z', '2026-10-20T12:00:00Z', 10, 100)
            """
        )
        # Conv 2: newer created_at, but updated earlier
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at, total_steps, total_tokens)
            VALUES ('c_newer_idle', 'Newer Idle', '2026-10-20T09:00:00Z', '2026-10-20T09:30:00Z', 5, 50)
            """
        )
        conn.commit()

        convs = api_routes.query_conversations_list(conn, view_mode="hierarchy")
        assert len(convs) == 2
        assert convs[0]["id"] == "c_older_active", f"Expected active conversation at index 0, got {convs[0]['id']}"
        assert convs[1]["id"] == "c_newer_idle"
        assert "updated_at" in convs[0]
        assert "created_at" in convs[0]


