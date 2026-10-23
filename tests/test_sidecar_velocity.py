import json
import sqlite3
import subprocess
from pathlib import Path


def test_sidecar_query_velocity_metrics_cold_start_and_sliding_windows(tmp_path: Path):
    """Verifies that queryVelocityMetrics in query.mjs computes live rolling TPM/TPH,
    clamps sub-minute active durations, and falls back to default peak ceilings under cold start."""
    db_path = tmp_path / "test_velocity_sidecar.db"
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
        billed_output_tokens INTEGER DEFAULT 0
    );

    CREATE TABLE hourly_step_rollups (
        hour_key TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        step_count INTEGER DEFAULT 0,
        est_tokens INTEGER DEFAULT 0,
        billed_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        PRIMARY KEY (hour_key, conversation_id)
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

    # Conversation with 120s active agent time (2m) and 600s pairing time (10m = 1/6h)
    cur.execute("""
    INSERT INTO conversations (
        id, title, model_name, created_at, updated_at, total_tokens,
        billed_input_tokens, billed_output_tokens, active_agent_seconds, active_pairing_seconds
    ) VALUES (
        'conv-vel-1', 'Velocity Test', 'Gemini 3.7 Flash',
        '2026-10-23T13:00:00Z', '2026-10-23T14:00:00Z', 12000,
        150000, 30000, 120, 600
    )
    """)

    # Insert steps relative to 2026-10-23T14:00:00Z:
    # Step 1: 2 mins ago (in 5m and 60m): est 1000, billed 5000 (4000 in, 1000 out)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-vel-1', 1, '2026-10-23T13:58:00Z', 1000, 4000, 1000)
    """)
    # Step 2: 4 mins ago (in 5m and 60m): est 2000, billed 10000 (8000 in, 2000 out)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-vel-1', 2, '2026-10-23T13:56:00Z', 2000, 8000, 2000)
    """)
    # Step 3: 25 mins ago (outside 5m, in 60m): est 5000, billed 25000 (20000 in, 5000 out)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-vel-1', 3, '2026-10-23T13:35:00Z', 5000, 20000, 5000)
    """)
    # Step 4: 90 mins ago (outside 60m): est 10000, billed 50000
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-vel-1', 4, '2026-10-23T12:30:00Z', 10000, 40000, 10000)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics, loadSessionById, getSessionData, queryGlobal, getGlobalData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const metrics = queryVelocityMetrics(db, {{ conversationId: 'conv-vel-1', now }});
    const session = loadSessionById(db, 'conv-vel-1', null, now);
    const globalData = queryGlobal(db, now);

    console.log(JSON.stringify({{ metrics, session, globalData }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    m = data["metrics"]
    # 5m rolling: Steps 1 + 2 => est 3000, billed 15000
    # TPM = 3000 / 5 = 600, billed TPM = 15000 / 5 = 3000
    assert m["rolling5mContextTpm"] == 600.0
    assert m["rolling5mBilledTpm"] == 3000.0

    # 60m rolling: Steps 1 + 2 + 3 => est 8000, billed 40000
    # TPH = 8000 context, 40000 billed
    assert m["rolling60mContextTph"] == 8000.0
    assert m["rolling60mBilledTph"] == 40000.0

    # Active rates: 120s agent time (2 mins), 600s pairing time (1/6 hr)
    # Total tokens = 12000, billed = 180000
    # activeContextTpm = 12000 / 2 = 6000.0
    # activeBilledTpm = 180000 / 2 = 90000.0
    # pairingBilledTph = 180000 / (1/6) = 1080000.0
    assert m["activeContextTpm"] == 6000.0
    assert m["activeBilledTpm"] == 90000.0
    assert m["pairingBilledTph"] == 1080000.0

    # Cold start defaults: peakBilledTpm = 100000.0, peakContextTpm = 10000.0
    assert m["peakBilledTpm"] == 100000.0
    assert m["peakContextTpm"] == 10000.0

    # Gauge arc percentages:
    # arcPct = (3000 / 100000) * 100 = 3.0%
    # contextFillPct = (600 / 10000) * 100 = 6.0%
    assert m["arcPct"] == 3.0
    assert m["contextFillPct"] == 6.0

    # Session attaching velocity
    sess = data["session"]
    assert "velocity" in sess
    assert sess["velocity"]["rolling5mBilledTpm"] == 3000.0
    assert sess["velocity"]["arcPct"] == 3.0

    # Global data attaching velocity stats
    g = data["globalData"]
    assert "activeContextTpm" in g
    assert "activeBilledTpm" in g
    assert "pairingBilledTph" in g
    assert "rolling60mBilledTph" in g


def test_sidecar_query_velocity_fleet_aggregation(tmp_path: Path):
    """Verifies that rootId scopes queries across root and subagents."""
    db_path = tmp_path / "test_velocity_fleet.db"
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
        billed_output_tokens INTEGER DEFAULT 0
    );

    CREATE TABLE hourly_step_rollups (
        hour_key TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        step_count INTEGER DEFAULT 0,
        est_tokens INTEGER DEFAULT 0,
        billed_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        PRIMARY KEY (hour_key, conversation_id)
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

    cur.execute("""
    INSERT INTO conversations (id, root_id, title, model_name, active_agent_seconds, billed_input_tokens, billed_output_tokens)
    VALUES ('root-c1', 'root-c1', 'Root Coordinator', 'Gemini 3.7 Flash', 120, 10000, 0)
    """)
    cur.execute("""
    INSERT INTO conversations (id, root_id, parent_id, title, model_name, active_agent_seconds, billed_input_tokens, billed_output_tokens)
    VALUES ('sub-c1', 'root-c1', 'root-c1', 'Subagent Worker', 'Gemini 3.7 Flash', 120, 20000, 0)
    """)
    cur.execute("""
    INSERT INTO conversations (id, root_id, title, model_name, active_agent_seconds, billed_input_tokens, billed_output_tokens)
    VALUES ('unrelated-c', 'unrelated-c', 'Unrelated', 'Gemini 3.7 Flash', 120, 99999, 0)
    """)

    # Steps 2m ago (in 5m window)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('root-c1', 1, '2026-10-23T13:58:00Z', 1000, 5000, 0)
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('sub-c1', 1, '2026-10-23T13:58:00Z', 2000, 10000, 0)
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('unrelated-c', 1, '2026-10-23T13:58:00Z', 50000, 500000, 0)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics, getSessionData }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const fleetMetrics = queryVelocityMetrics(db, {{ rootId: 'root-c1', now }});
    const session = getSessionData(db, 'root-c1', null, now);
    console.log(JSON.stringify({{ fleetMetrics, session }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    fm = data["fleetMetrics"]
    # Aggregates root-c1 (1000/5000) and sub-c1 (2000/10000) => 3000 / 15000
    assert fm["rolling5mContextTpm"] == 600.0
    assert fm["rolling5mBilledTpm"] == 3000.0

    # Session fleet should contain fleet.velocity when is_swarm is true
    sess = data["session"]
    assert sess["fleet"]["is_swarm"] is True
    assert "velocity" in sess["fleet"]
    assert sess["fleet"]["velocity"]["rolling5mBilledTpm"] == 3000.0


def test_sidecar_frontend_velocity_dom_and_static_invariants():
    """Verifies that index.html removes visual speedometer and bullet gauge elements while
    retaining global telemetry facts, and styles.css enforces static visuals without velocity rules."""
    html_path = Path("sidecars/panel/index.html")
    css_path = Path("sidecars/panel/styles.css")
    js_path = Path("sidecars/panel/app.js")

    html = html_path.read_text()
    css = css_path.read_text()
    js = js_path.read_text()

    # Removed Active Capacity card elements
    assert 'id="velocityArcPath"' not in html
    assert 'id="velocityArcFill"' not in html
    assert 'id="velocityLiveTpm"' not in html
    assert 'id="velocityLiveTph"' not in html
    assert 'id="velocityActiveTpm"' not in html
    assert 'id="contextVelocityFill"' not in html
    assert 'id="contextVelocityTpm"' not in html

    # Removed Swarm Fleet card elements
    assert 'id="fleetVelocityBlock"' not in html
    assert 'id="fleetVelocityTpm"' not in html
    assert 'id="fleetVelocityBar"' not in html

    # Removed Subscription Quota burn labels
    assert 'id="quota5hTph"' not in html
    assert 'id="quotaWeeklyTph"' not in html

    # Removed Global view velocity elements
    assert 'id="globalActiveTpm"' not in html
    assert 'id="globalPairingTph"' not in html
    assert 'id="globalContextTpm"' not in html
    assert 'id="globalRollingTph"' not in html

    # Static visuals invariant in CSS
    assert "transition: none !important" in css
    assert "animation: none !important" in css
    assert ".velocity-container" not in css
    assert ".speedometer-arc" not in css
    assert ".bullet-bar" not in css

    # App.js wiring: removed elements not referenced
    assert "velocityArcFill" not in js
    assert "velocityLiveTpm" not in js
    assert "contextVelocityFill" not in js
    assert "fleetVelocityBar" not in js
    assert "quotaWeeklyTph" not in js
    assert "globalActiveTpm" not in js
    assert "globalPairingTph" not in js


def test_sidecar_frontend_render_velocity_execution():
    """Executes app.js in Node with a simulated DOM to deeply verify sidecar session and quota
    rendering when velocity data is received, ensuring removed visual elements are not accessed."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        listeners: {},
        children: [],
        get innerHTML() { return _html; },
        set innerHTML(val) {
          _html = val;
          if (val === '') this.children = [];
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; },
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, force) {
            if (force === undefined) {
              if (this.classes.has(c)) this.classes.delete(c);
              else this.classes.add(c);
            } else if (force) {
              this.classes.add(c);
            } else {
              this.classes.delete(c);
            }
          },
          contains(c) { return this.classes.has(c); }
        },
        addEventListener(evt, fn) { this.listeners[evt] = fn; },
        dispatchEvent(evt) { if (this.listeners[evt]) this.listeners[evt](); }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'inspect-banner-label', 'btn-reset-inspect',
      'fleet-card', 'fleet-title', 'fleet-count-badge', 'fleet-summary-strip', 'fleet-total-cost', 'fleet-total-tokens', 'fleet-shielding', 'fleet-node-list',
      'global-card', 'global-convs', 'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'compaction-banner', 'compaction-count', 'compaction-pruned', 'compaction-window',
      'quota-card', 'quota-title', 'quota-session-impact', 'quota-tab-gemini', 'quota-tab-3p',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-card', 'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-card', 'top-steps-list', 'steps-range',
      'actions-card', 'btn-audit', 'btn-plan', 'btn-compact', 'toast'
    ];

    for (const id of ids) {
      elements[id] = makeEl(id);
    }

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (tag) => makeEl(tag),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    const mockWindow = {
      sidecar: {
        conversationId: 'conv-vel-test',
        fetch: async () => ({
          ok: true,
          json: async () => ({
            model_name: 'Gemini 3.8 Flash',
            billed_cost: 6.72,
            pct: 25.0,
            active_tokens: 250000,
            max_tokens: 1000000,
            weekly_quota_pct: 1.0,
            velocity: {
              rolling5mContextTpm: 1200.0,
              rolling5mBilledTpm: 50000.0,
              rolling60mContextTph: 72000.0,
              rolling60mBilledTph: 1500000.0,
              activeContextTpm: 1000.0,
              activeBilledTpm: 45000.0,
              pairingBilledTph: 1200000.0,
              peakBilledTpm: 100000.0,
              peakContextTpm: 10000.0,
              arcPct: 50.0,
              contextFillPct: 12.0
            },
            fleet: {
              is_swarm: true,
              agent_count: 2,
              total_cost: 12.0,
              total_tokens: 500000,
              shielding_ratio: 2.0,
              velocity: {
                rolling5mBilledTpm: 80000.0,
                arcPct: 80.0
              },
              nodes: []
            },
            quota: {
              prefix: 'gemini',
              weekly_remaining_pct: 85.0,
              five_hour_remaining_pct: 90.0,
              weekly_billed_tph: 150000.0,
              five_hour_billed_tph: 80000.0,
              buckets: {
                'gemini-weekly': { remaining_pct: 85.0, billed_tph: 150000.0 },
                'gemini-5h': { remaining_pct: 90.0, billed_tph: 80000.0 }
              }
            }
          })
        })
      }
    };

    let intervals = [];
    const customSetInterval = (fn, ms) => {
      const id = setTimeout(fn, ms);
      intervals.push(id);
      return id;
    };

    const fn = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn(mockWindow, mockDocument, mockWindow.sidecar.fetch, customSetInterval);

    setTimeout(() => {
      intervals.forEach(clearTimeout);

      const res = {
        capPct: elements['capacity-pct'].textContent,
        capSub: elements['capacity-sub'].textContent,
        modelBadge: elements['model-badge'].textContent,
        quotaWeeklyPct: elements['quota-weekly-pct'].textContent,
        quota5hPct: elements['quota-5h-pct'].textContent,
        hasVelocityArcFill: 'velocityArcFill' in elements,
        hasContextVelocityFill: 'contextVelocityFill' in elements
      };

      console.log(JSON.stringify(res));
      process.exit(0);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["capPct"] == "25.0%"
    assert "250.0k / 1.00M tokens" in data["capSub"]
    assert data["modelBadge"] == "Gemini 3.8 Flash"
    assert data["quotaWeeklyPct"] == "85.0% remaining"
    assert data["quota5hPct"] == "90.0% remaining"
    assert data["hasVelocityArcFill"] is False
    assert data["hasContextVelocityFill"] is False


def test_sidecar_query_velocity_idle_window_and_zero_guards(tmp_path: Path):
    """Verifies that an idle conversation has 0.0 rolling rates and 0.0 arcPct while
    retaining historical active-time velocity, and zero durations never trigger ZeroDivisionError."""
    db_path = tmp_path / "test_velocity_idle.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT, created_at TEXT, updated_at TEXT,
        total_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0,
        active_agent_seconds REAL DEFAULT 0.0, active_pairing_seconds REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0
    );
    """)

    # Idle conversation: last step was 3 hours ago
    cur.execute("""
    INSERT INTO conversations (id, title, total_tokens, billed_input_tokens, billed_output_tokens, active_agent_seconds, active_pairing_seconds)
    VALUES ('conv-idle', 'Idle Conv', 50000, 200000, 50000, 300, 1800)
    """)
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-idle', 1, '2026-10-23T10:00:00Z', 50000, 200000, 50000)
    """)

    # Zero-duration conversation
    cur.execute("""
    INSERT INTO conversations (id, title, total_tokens, billed_input_tokens, billed_output_tokens, active_agent_seconds, active_pairing_seconds)
    VALUES ('conv-zero', 'Zero Conv', 0, 0, 0, 0, 0)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const idleMetrics = queryVelocityMetrics(db, {{ conversationId: 'conv-idle', now }});
    const zeroMetrics = queryVelocityMetrics(db, {{ conversationId: 'conv-zero', now }});
    console.log(JSON.stringify({{ idleMetrics, zeroMetrics }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    idle = data["idleMetrics"]
    # Rolling 5m and 60m must be 0.0
    assert idle["rolling5mContextTpm"] == 0.0
    assert idle["rolling5mBilledTpm"] == 0.0
    assert idle["rolling60mContextTph"] == 0.0
    assert idle["rolling60mBilledTph"] == 0.0
    assert idle["arcPct"] == 0.0
    assert idle["contextFillPct"] == 0.0

    # Historical active rates preserved (300s agent time = 5 mins => 50k / 5 = 10k TPM; billed 250k / 5 = 50k TPM)
    assert idle["activeContextTpm"] == 10000.0
    assert idle["activeBilledTpm"] == 50000.0
    # Pairing time 1800s = 0.5h => 250k / 0.5 = 500k TPH
    assert idle["pairingBilledTph"] == 500000.0

    # Zero duration conversation returns 0.0 without errors
    zero = data["zeroMetrics"]
    assert zero["activeContextTpm"] == 0.0
    assert zero["activeBilledTpm"] == 0.0
    assert zero["pairingBilledTph"] == 0.0


def test_sidecar_query_velocity_p95_peak_calibration(tmp_path: Path):
    """Verifies that queryVelocityMetrics calculates calibrated peak values when >= 3 hours exist."""
    db_path = tmp_path / "test_velocity_p95.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT, created_at TEXT, updated_at TEXT,
        total_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0,
        active_agent_seconds REAL DEFAULT 0.0, active_pairing_seconds REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0
    );
    CREATE TABLE hourly_step_rollups (
        hour_key TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        step_count INTEGER DEFAULT 0,
        est_tokens INTEGER DEFAULT 0,
        billed_tokens INTEGER DEFAULT 0,
        billed_cost REAL DEFAULT 0.0,
        PRIMARY KEY (hour_key, conversation_id)
    );
    """)

    cur.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Conv')")
    hours_data = [
        ("2026-10-23T10", "c1", 10, 6000, 60000, 0.5),
        ("2026-10-23T11", "c1", 15, 12000, 120000, 1.0),
        ("2026-10-23T12", "c1", 20, 18000, 180000, 1.5),
        ("2026-10-23T13", "c1", 25, 24000, 240000, 2.0),
    ]
    cur.executemany(
        "INSERT INTO hourly_step_rollups VALUES (?, ?, ?, ?, ?, ?)",
        hours_data,
    )
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const m = queryVelocityMetrics(db, {{ now }});
    console.log(JSON.stringify(m));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    m = json.loads(res.stdout)

    # 4 distinct hours should calibrate peak capacities instead of using default 100k
    assert m["peakBilledTpm"] > 0
    assert m["peakContextTpm"] > 0
    assert m["peakBilledTpm"] != 100000.0


def test_sidecar_query_velocity_malformed_timestamps_and_missing_tables(tmp_path: Path):
    """Verifies that malformed timestamps (spaces, NULLs, empty) and missing tables fall back gracefully."""
    db_path = tmp_path / "test_velocity_malformed.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, active_agent_seconds REAL DEFAULT 60.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0
    );
    """)

    cur.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Conv')")
    # Step with space in timestamp (2m ago)
    cur.execute("INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 1, '2026-10-23 13:58:00', 1000, 5000)")
    # Step with NULL timestamp
    cur.execute("INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 2, NULL, 500, 2500)")
    # Step with empty string timestamp
    cur.execute("INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens) VALUES ('c1', 3, '', 500, 2500)")
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const m = queryVelocityMetrics(db, {{ conversationId: 'c1', now }});
    const nullDbMetrics = queryVelocityMetrics(null, {{}});
    console.log(JSON.stringify({{ m, nullDbMetrics }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["m"]["rolling5mContextTpm"] == 200.0  # 1000 / 5
    assert data["m"]["rolling5mBilledTpm"] == 1000.0  # 5000 / 5
    assert data["nullDbMetrics"]["peakBilledTpm"] == 100000.0


def test_sidecar_load_session_by_id_historical_now_and_quota_tph(tmp_path: Path):
    """Verifies that loadSessionById forwards the historical now parameter to getQuotaSummary,
    and calculates non-zero quota burn rates from steps within the window even when model_name is NULL."""
    db_path = tmp_path / "test_session_quota_tph.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, model_name TEXT, created_at TEXT, updated_at TEXT,
        total_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0,
        active_agent_seconds REAL DEFAULT 0.0, active_pairing_seconds REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, model_name TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0
    );
    CREATE TABLE quota_state (
        bucket_id TEXT PRIMARY KEY, group_name TEXT NOT NULL, window TEXT NOT NULL,
        remaining_fraction REAL NOT NULL, reset_time TEXT NOT NULL, calibrated_capacity REAL NOT NULL, fetched_at TEXT NOT NULL
    );
    """)

    # Quota resets relative to 2026-10-23T14:00:00Z:
    # 5h window: resets in 3h => 2h elapsed
    cur.execute("""
    INSERT INTO quota_state VALUES (
        'gemini-5h', 'Gemini Models', '5h', 0.60, '2026-10-23T17:00:00Z', 236.7, '2026-10-23T14:00:00Z'
    );
    """)
    # weekly window: resets in 5 days (120h) => 48h elapsed
    cur.execute("""
    INSERT INTO quota_state VALUES (
        'gemini-weekly', 'Gemini Models', 'weekly', 0.85, '2026-10-28T14:00:00Z', 672.0, '2026-10-23T14:00:00Z'
    );
    """)
    cur.execute("""
    INSERT INTO conversations (id, title, model_name, created_at, updated_at, total_tokens, active_agent_seconds)
    VALUES ('c-quota-hist', 'Quota Hist Test', 'Gemini 3.7 Flash', '2026-10-23T13:00:00Z', '2026-10-23T14:00:00Z', 5000, 120)
    """)

    # 1 hour ago (within 5h window, 2h elapsed): 60,000 billed tokens. model_name is NULL (default).
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, model_name, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('c-quota-hist', 1, '2026-10-23T13:00:00Z', NULL, 1000, 50000, 10000)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, loadSessionById }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const session = loadSessionById(db, 'c-quota-hist', null, now);
    console.log(JSON.stringify(session.quota));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    quota = json.loads(res.stdout)

    # 60,000 tokens over 2.0 elapsed hours = 30,000 tok/hr
    assert quota["five_hour_billed_tph"] == 30000.0
    assert quota["weekly_billed_tph"] > 0.0


def test_sidecar_query_velocity_flexible_options_and_invalid_date(tmp_path: Path):
    """Verifies that queryVelocityMetrics accepts a bare string CID, snake_case options,
    and handles invalid date inputs without crashing."""
    db_path = tmp_path / "test_opts.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, total_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0,
        billed_output_tokens INTEGER DEFAULT 0, active_agent_seconds REAL DEFAULT 0.0, active_pairing_seconds REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0
    );
    """)
    cur.execute("""
    INSERT INTO conversations (id, total_tokens, billed_input_tokens, active_agent_seconds)
    VALUES ('conv-opts-1', 10000, 50000, 120)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');

    // 1. Bare string conversationId
    const mStr = queryVelocityMetrics(db, 'conv-opts-1');

    // 2. snake_case conversation_id
    const mSnake = queryVelocityMetrics(db, {{ conversation_id: 'conv-opts-1' }});

    // 3. Invalid date string
    const mBadDate = queryVelocityMetrics(db, {{ conversationId: 'conv-opts-1', now: 'not-a-valid-date' }});

    console.log(JSON.stringify({{ mStr, mSnake, mBadDate }}));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["mStr"]["activeContextTpm"] == 5000.0
    assert data["mSnake"]["activeBilledTpm"] == 25000.0
    assert data["mBadDate"]["activeBilledTpm"] == 25000.0


def test_sidecar_query_velocity_independent_step_fallback(tmp_path: Path):
    """Verifies that if conversations has total_tokens > 0 but billed_tokens = 0,
    it independently falls back to steps for billed tokens rather than leaving them at 0."""
    db_path = tmp_path / "test_fallback_indep.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, title TEXT, total_tokens INTEGER DEFAULT 0,
        billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0,
        active_agent_seconds REAL DEFAULT 120.0, active_pairing_seconds REAL DEFAULT 0.0
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, step_index INTEGER,
        created_at TEXT, est_tokens INTEGER DEFAULT 0, billed_input_tokens INTEGER DEFAULT 0, billed_output_tokens INTEGER DEFAULT 0
    );
    """)

    # Conversation has total_tokens=10000, but billed_input_tokens=0
    cur.execute("""
    INSERT INTO conversations (id, title, total_tokens, billed_input_tokens, billed_output_tokens, active_agent_seconds)
    VALUES ('conv-partial', 'Partial Conv', 10000, 0, 0, 120.0)
    """)
    # Steps has billed tokens
    cur.execute("""
    INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens)
    VALUES ('conv-partial', 1, '2026-10-23T13:58:00Z', 1000, 60000, 20000)
    """)
    conn.commit()
    conn.close()

    node_code = f"""
    import {{ openDatabase, queryVelocityMetrics }} from './sidecars/panel/query.mjs';
    const db = openDatabase('{db_path}');
    const now = new Date('2026-10-23T14:00:00Z');

    const m = queryVelocityMetrics(db, {{ conversationId: 'conv-partial', now }});
    console.log(JSON.stringify(m));
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    m = json.loads(res.stdout)

    # 10,000 est / 2 min = 5,000 TPM
    assert m["activeContextTpm"] == 5000.0
    # 80,000 billed / 2 min = 40,000 TPM (recovered from steps!)
    assert m["activeBilledTpm"] == 40000.0


def test_sidecar_frontend_render_global_and_bullet_classes():
    """Verifies that renderGlobal runs cleanly with velocity payloads without populating removed
    global throughput elements, and sidecar gracefully ignores removed bullet elements."""
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('sidecars/panel/app.js', 'utf8');

    const elements = {};
    function makeEl(id) {
      let _html = '';
      return {
        id,
        textContent: '',
        className: '',
        style: {},
        hidden: false,
        attributes: {},
        children: [],
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          remove(c) { this.classes.delete(c); },
          toggle(c, f) { if (f) this.classes.add(c); else this.classes.delete(c); },
          contains(c) { return this.classes.has(c); }
        },
        appendChild(c) { this.children.push(c); },
        removeChild(c) { this.children = this.children.filter(x => x !== c); },
        setAttribute(k, v) { this.attributes[k] = v; },
        getAttribute(k) { return this.attributes[k]; }
      };
    }

    const ids = [
      'status-badge', 'model-badge', 'cost-badge', 'quota-badge',
      'inspect-banner', 'fleet-card', 'global-card', 'global-convs',
      'global-tokens', 'global-cost', 'global-quota', 'global-compactions', 'global-pruned',
      'globalActiveTpm', 'globalPairingTph',
      'compaction-banner', 'quota-card', 'capacity-card', 'top-steps-card', 'actions-card',
      'quota-weekly-pct', 'quota-weekly-reset', 'quota-weekly-bar', 'quota-weekly-sub',
      'quota-5h-pct', 'quota-5h-reset', 'quota-5h-bar', 'quota-5h-sub',
      'capacity-pct', 'capacity-sub', 'capacity-bar', 'category-list',
      'top-steps-list', 'steps-range', 'toast'
    ];
    for (const id of ids) elements[id] = makeEl(id);

    const mockDocument = {
      getElementById: (id) => elements[id],
      createElement: (t) => makeEl(t),
      addEventListener: () => {},
      readyState: 'complete',
      hidden: false
    };

    // 1. Test Global rendering
    const mockWindowGlobal = {
      sidecar: {
        conversationId: null,
        fetch: async () => ({
          ok: true,
          json: async () => ({
            total_conversations: 5,
            total_tokens: 500000,
            total_cost: 2.50,
            weekly_quota_pct: 1.25,
            activeBilledTpm: 12500.0,
            pairingBilledTph: 250000.0,
            quota: null
          })
        })
      }
    };

    const fn1 = new Function('window', 'document', 'fetch', 'setInterval', code);
    fn1(mockWindowGlobal, mockDocument, mockWindowGlobal.sidecar.fetch, () => {});

    setTimeout(() => {
      const globalRes = {
        activeTpm: elements['globalActiveTpm'].textContent,
        pairingTph: elements['globalPairingTph'].textContent,
        totalTokens: elements['global-tokens'].textContent,
        totalCost: elements['global-cost'].textContent
      };

      // 2. Test active session rendering without bullet bar errors
      const mockWindowActive = {
        sidecar: {
          conversationId: 'c1',
          fetch: async () => ({
            ok: true,
            json: async () => ({
              model_name: 'Gemini 3.7 Flash',
              active_tokens: 100000,
              max_tokens: 1000000,
              pct: 10.0,
              velocity: {
                contextFillPct: 65.0,
                rolling5mContextTpm: 6500.0
              }
            })
          })
        }
      };

      const fn2 = new Function('window', 'document', 'fetch', 'setInterval', code);
      fn2(mockWindowActive, mockDocument, mockWindowActive.sidecar.fetch, () => {});

      setTimeout(() => {
        const activeRes = {
          pct: elements['capacity-pct'].textContent,
          hasBulletBar: 'contextVelocityFill' in elements
        };

        console.log(JSON.stringify({ globalRes, activeRes }));
        process.exit(0);
      }, 50);
    }, 50);
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed: {res.stderr}"
    data = json.loads(res.stdout)

    assert data["globalRes"]["activeTpm"] == ""
    assert data["globalRes"]["pairingTph"] == ""
    assert data["globalRes"]["totalTokens"] == "500.0k"
    assert data["globalRes"]["totalCost"] == "$2.50"
    assert data["activeRes"]["pct"] == "10.0%"
    assert data["activeRes"]["hasBulletBar"] is False


