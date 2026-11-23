import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_monitor.db import (
    get_db_connection,
    get_quota_reset_events,
    get_quota_state,
    init_db,
    insert_quota_reset_events,
    upsert_quota_state,
)


def _run_node(script: str) -> dict:
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, f"Node script failed:\nSTDERR: {res.stderr}\nSTDOUT: {res.stdout}"
    return json.loads(res.stdout)


def test_sidecar_detect_quota_reset_event_classification():
    node_code = """
    import { detectQuotaResetEvent } from './sidecars/panel/ls_quota.mjs';

    const results = {
      // 1. Scheduled reset (online, exact/near reset_time)
      scheduledOnline: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.35,
          reset_time: '2026-11-25T18:00:00Z',
          fetched_at: '2026-11-25T17:50:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T18:00:00Z',
        },
        '2026-11-25T18:01:00Z',
      ),

      // 2. Scheduled reset (offline overnight, 13h after old_reset_time)
      scheduledOfflineOvernight: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.25,
          reset_time: '2026-11-25T05:00:00Z',
          fetched_at: '2026-11-25T04:00:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T05:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 3. Scheduled reset with immediate burn (delta <= 0, reset_time advanced past old_reset_time)
      scheduledImmediateBurn: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-5h',
          group_name: 'Gemini Models',
          window: '5h',
          remaining_fraction: 0.20,
          reset_time: '2026-11-25T17:00:00Z',
          fetched_at: '2026-11-25T16:55:00Z',
        },
        {
          bucket_id: 'gemini-5h',
          group_name: 'Gemini Models',
          window: '5h',
          remaining_fraction: 0.15,
          reset_time: '2026-11-25T22:10:00Z',
        },
        '2026-11-25T17:10:00Z',
      ),

      // 4. Scheduled tolerance 10m before old_reset_time (clock skew)
      scheduledToleranceBefore: detectQuotaResetEvent(
        {
          bucket_id: '3p-5h',
          group_name: 'Claude and GPT models',
          window: '5h',
          remaining_fraction: 0.40,
          reset_time: '2026-11-25T18:00:00Z',
          fetched_at: '2026-11-25T17:40:00Z',
        },
        {
          bucket_id: '3p-5h',
          group_name: 'Claude and GPT models',
          window: '5h',
          remaining_fraction: 1.0,
          reset_time: '2026-11-25T23:00:00Z',
        },
        '2026-11-25T17:50:00Z',
      ),

      // 5. Early reset (weekly) with bounded inferred reset_at = new_reset - 7d
      earlyResetWeeklyBounded: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.0,
          reset_time: '2026-11-30T20:00:00Z',
          fetched_at: '2026-11-25T17:00:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T17:30:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 6. Early reset (5h) small jump (0.97 -> 1.00) with shifted reset_time
      earlyReset5hSmallJump: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-5h',
          group_name: 'Gemini Models',
          window: '5h',
          remaining_fraction: 0.97,
          reset_time: '2026-11-25T22:00:00Z',
          fetched_at: '2026-11-25T17:30:00Z',
        },
        {
          bucket_id: 'gemini-5h',
          group_name: 'Gemini Models',
          window: '5h',
          remaining_fraction: 1.0,
          reset_time: '2026-11-25T22:45:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 7. Early reset unshifted reset_time (inferred < old fetched_at -> falls back to detectedIso)
      earlyResetUnshifted: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.42,
          reset_time: '2026-11-30T20:00:00Z',
          fetched_at: '2026-11-25T17:00:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.95,
          reset_time: '2026-11-30T20:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 8. Early reset with naive timestamps (no trailing Z)
      earlyResetNaiveUtc: detectQuotaResetEvent(
        {
          bucket_id: '3p-5h',
          group_name: 'Claude and GPT models',
          window: '5h',
          remaining_fraction: 0.50,
          reset_time: '2026-11-25T22:00:00',
          fetched_at: '2026-11-25T17:00:00',
        },
        {
          bucket_id: '3p-5h',
          group_name: 'Claude and GPT models',
          window: '5h',
          remaining_fraction: 1.0,
          reset_time: '2026-11-25T22:30:00',
        },
        '2026-11-25T18:00:00',
      ),

      // 9. Mid-cycle top_up (unchanged reset_time, new_rem < 0.90)
      topUpExact: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.30,
          reset_time: '2026-11-30T20:00:00Z',
          fetched_at: '2026-11-25T17:00:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.55,
          reset_time: '2026-11-30T20:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),
      topUpBoundaryFloat: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.30,
          reset_time: '2026-11-30T20:00:00Z',
          fetched_at: '2026-11-25T17:00:00Z',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.35,
          reset_time: '2026-11-30T20:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 10. Ignored: idle bucket drift (1.0 -> 1.0 and 0.99 -> 1.0)
      ignoredIdle1: detectQuotaResetEvent(
        {
          bucket_id: '3p-weekly',
          group_name: 'Claude and GPT models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T17:00:00Z',
        },
        {
          bucket_id: '3p-weekly',
          group_name: 'Claude and GPT models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T18:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),
      ignoredIdle99: detectQuotaResetEvent(
        {
          bucket_id: '3p-weekly',
          group_name: 'Claude and GPT models',
          window: 'weekly',
          remaining_fraction: 0.99,
          reset_time: '2026-12-02T17:00:00Z',
        },
        {
          bucket_id: '3p-weekly',
          group_name: 'Claude and GPT models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T18:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 11. Ignored: empty old_reset_time (bootstrap fallback row)
      ignoredBootstrap: detectQuotaResetEvent(
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 0.80,
          reset_time: '',
        },
        {
          bucket_id: 'gemini-weekly',
          group_name: 'Gemini Models',
          window: 'weekly',
          remaining_fraction: 1.0,
          reset_time: '2026-12-02T18:00:00Z',
        },
        '2026-11-25T18:00:00Z',
      ),

      // 12. Ignored: null oldRow, normal burn, and sub-threshold jitter
      ignoredNullOld: detectQuotaResetEvent(
        null,
        { bucket_id: 'gemini-weekly', remaining_fraction: 1.0, reset_time: '2026-12-02T18:00:00Z' },
        '2026-11-25T18:00:00Z',
      ),
      ignoredBurn: detectQuotaResetEvent(
        { bucket_id: 'gemini-weekly', remaining_fraction: 0.80, reset_time: '2026-11-30T20:00:00Z' },
        { bucket_id: 'gemini-weekly', remaining_fraction: 0.60, reset_time: '2026-11-30T20:00:00Z' },
        '2026-11-25T18:00:00Z',
      ),
      ignoredSmallJitter: detectQuotaResetEvent(
        { bucket_id: 'gemini-weekly', remaining_fraction: 0.80, reset_time: '2026-11-30T20:00:00Z' },
        { bucket_id: 'gemini-weekly', remaining_fraction: 0.802, reset_time: '2026-11-30T20:30:00Z' },
        '2026-11-25T18:00:00Z',
      ),
    };

    console.log(JSON.stringify(results));
    """
    data = _run_node(node_code)

    expected_keys = {
        "bucket_id",
        "group_name",
        "window",
        "event_type",
        "reset_at",
        "detected_at",
        "old_remaining_fraction",
        "new_remaining_fraction",
        "delta_fraction",
        "old_reset_time",
        "new_reset_time",
        "source",
    }

    s_online = data["scheduledOnline"]
    assert set(s_online.keys()) == expected_keys
    assert s_online["event_type"] == "scheduled"
    assert s_online["reset_at"] == "2026-11-25T18:00:00Z"
    assert s_online["source"] == "sidecar"
    assert s_online["delta_fraction"] == pytest.approx(0.65)

    assert data["scheduledOfflineOvernight"]["event_type"] == "scheduled"
    assert data["scheduledOfflineOvernight"]["reset_at"] == "2026-11-25T05:00:00Z"

    assert data["scheduledImmediateBurn"]["event_type"] == "scheduled"
    assert data["scheduledImmediateBurn"]["reset_at"] == "2026-11-25T17:00:00Z"
    assert data["scheduledImmediateBurn"]["delta_fraction"] == pytest.approx(-0.05)

    assert data["scheduledToleranceBefore"]["event_type"] == "scheduled"
    assert data["scheduledToleranceBefore"]["reset_at"] == "2026-11-25T18:00:00Z"

    early_w = data["earlyResetWeeklyBounded"]
    assert early_w["event_type"] == "early_reset"
    assert early_w["old_remaining_fraction"] == 0.0
    assert early_w["delta_fraction"] == pytest.approx(1.0)
    assert early_w["reset_at"] == "2026-11-25T17:30:00.000Z"

    early_5h = data["earlyReset5hSmallJump"]
    assert early_5h["event_type"] == "early_reset"
    assert early_5h["reset_at"] == "2026-11-25T17:45:00.000Z"

    assert data["earlyResetUnshifted"]["event_type"] == "early_reset"
    assert data["earlyResetUnshifted"]["reset_at"] == "2026-11-25T18:00:00Z"

    assert data["earlyResetNaiveUtc"]["event_type"] == "early_reset"
    assert data["earlyResetNaiveUtc"]["reset_at"] == "2026-11-25T17:30:00.000Z"

    top_up = data["topUpExact"]
    assert top_up["event_type"] == "top_up"
    assert top_up["reset_at"] == "2026-11-25T18:00:00Z"
    assert top_up["delta_fraction"] == 0.25

    top_up_boundary = data["topUpBoundaryFloat"]
    assert top_up_boundary is not None
    assert top_up_boundary["event_type"] == "top_up"
    assert top_up_boundary["reset_at"] == "2026-11-25T18:00:00Z"
    assert top_up_boundary["delta_fraction"] == 0.05

    assert data["ignoredIdle1"] is None
    assert data["ignoredIdle99"] is None
    assert data["ignoredBootstrap"] is None
    assert data["ignoredNullOld"] is None
    assert data["ignoredBurn"] is None
    assert data["ignoredSmallJitter"] is None


def test_sidecar_sync_records_reset_atomically_and_deduplicates(tmp_path: Path):
    db_path = tmp_path / "sidecar_sync_resets.db"
    init_db(db_path)

    now = datetime.now(timezone.utc)
    old_fetched = (now - timedelta(minutes=30)).isoformat()
    old_reset = (now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_reset = (now + timedelta(days=7) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    idle_old_reset = (now + timedelta(days=7) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    idle_new_reset = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db_connection(db_path) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.42,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                },
                {
                    "bucket_id": "3p-weekly",
                    "group_name": "Claude and GPT models",
                    "window": "weekly",
                    "remaining_fraction": 1.0,
                    "reset_time": idle_old_reset,
                    "calibrated_capacity": 200.0,
                    "fetched_at": old_fetched,
                },
            ],
        )

    node_code = f"""
    import {{ createLsQuotaSyncer }} from './sidecars/panel/ls_quota.mjs';

    const payload = {{
      response: {{
        groups: [
          {{
            displayName: 'Gemini Models',
            buckets: [{{
              bucketId: 'gemini-weekly',
              window: 'weekly',
              remainingFraction: 1.0,
              resetTime: {json.dumps(new_reset)},
            }}],
          }},
          {{
            displayName: 'Claude and GPT models',
            buckets: [{{
              bucketId: '3p-weekly',
              window: 'weekly',
              remainingFraction: 1.0,
              resetTime: {json.dumps(idle_new_reset)},
            }}],
          }},
        ],
      }},
    }};

    const syncer = createLsQuotaSyncer({{
      dbPath: {json.dumps(db_path.as_posix())},
      env: {{
        ANTIGRAVITY_LS_ADDRESS: 'localhost:60090',
        ANTIGRAVITY_CSRF_TOKEN: 'test-token',
      }},
      fetchImpl: async () => ({{
        ok: true,
        json: async () => payload,
      }}),
    }});

    const wroteFirst = await syncer.syncIfNeeded({{ force: true }});
    const wroteSecond = await syncer.syncIfNeeded({{ force: true }});
    console.log(JSON.stringify({{ wroteFirst, wroteSecond }}));
    """
    res = _run_node(node_code)
    assert res["wroteFirst"] is True
    assert res["wroteSecond"] is False

    with get_db_connection(db_path) as conn:
        events = get_quota_reset_events(conn)
        assert len(events) == 1
        ev = events[0]
        assert ev["bucket_id"] == "gemini-weekly"
        assert ev["event_type"] == "early_reset"
        assert ev["source"] == "sidecar"
        assert ev["old_remaining_fraction"] == pytest.approx(0.42)
        assert ev["new_remaining_fraction"] == pytest.approx(1.0)
        assert ev["delta_fraction"] == pytest.approx(0.58)
        assert ev["old_reset_time"] == old_reset
        assert ev["new_reset_time"] == new_reset

        state = {r["bucket_id"]: r for r in get_quota_state(conn)}
        assert state["gemini-weekly"]["remaining_fraction"] == pytest.approx(1.0)
        assert state["gemini-weekly"]["reset_time"] == new_reset


def test_sidecar_concurrent_python_write_race_and_5m_dedup(tmp_path: Path):
    db_path = tmp_path / "race.db"
    init_db(db_path)

    now = datetime.now(timezone.utc)
    old_fetched = (now - timedelta(minutes=20)).isoformat()
    old_reset = (now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_reset = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db_connection(db_path) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.40,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                }
            ],
        )

    # Simulate Python sync winning the write race DURING sidecar's async fetchImpl
    node_code = f"""
    import {{ DatabaseSync }} from 'node:sqlite';
    import {{ createLsQuotaSyncer, writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';

    const dbPath = {json.dumps(db_path.as_posix())};
    const syncer = createLsQuotaSyncer({{
      dbPath,
      env: {{
        ANTIGRAVITY_LS_ADDRESS: 'localhost:60090',
        ANTIGRAVITY_CSRF_TOKEN: 'test-token',
      }},
      fetchImpl: async () => {{
        // Python wins the race while sidecar HTTP fetch is in flight
        const pyDb = new DatabaseSync(dbPath);
        pyDb.exec('BEGIN IMMEDIATE;');
        pyDb.prepare(`
          INSERT INTO quota_reset_events (
            bucket_id, group_name, window, event_type, reset_at, detected_at,
            old_remaining_fraction, new_remaining_fraction, delta_fraction,
            old_reset_time, new_reset_time, source
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `).run(
          'gemini-weekly', 'Gemini Models', 'weekly', 'early_reset',
          {json.dumps(now.isoformat())}, {json.dumps(now.isoformat())},
          0.40, 1.0, 0.60, {json.dumps(old_reset)}, {json.dumps(new_reset)}, 'python'
        );
        pyDb.prepare(`
          UPDATE quota_state
          SET remaining_fraction = 1.0, reset_time = ?, fetched_at = ?
          WHERE bucket_id = 'gemini-weekly'
        `).run({json.dumps(new_reset)}, {json.dumps(now.isoformat())});
        pyDb.exec('COMMIT;');
        pyDb.close();

        return {{
          ok: true,
          json: async () => ({{
            response: {{
              groups: [{{
                displayName: 'Gemini Models',
                buckets: [{{
                  bucketId: 'gemini-weekly',
                  window: 'weekly',
                  remainingFraction: 1.0,
                  resetTime: {json.dumps(new_reset)},
                }}],
              }}],
            }},
          }}),
        }};
      }},
    }});

    const wrote = await syncer.syncIfNeeded({{ force: true }});
    console.log(JSON.stringify({{ wrote }}));
    """
    res = _run_node(node_code)
    assert res["wrote"] is True

    with get_db_connection(db_path) as conn:
        events = get_quota_reset_events(conn)
        assert len(events) == 1
        assert events[0]["source"] == "python"

    # Also directly verify 5-minute dedup query in writeQuotaRows when an identical
    # top_up event already exists within 300s even if quota_state still had old_rem
    dedup_db = tmp_path / "dedup_direct.db"
    init_db(dedup_db)
    t_prior = (now - timedelta(seconds=120)).isoformat()
    t_now = now.isoformat()
    with get_db_connection(dedup_db) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.30,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                }
            ],
        )
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "top_up",
                "reset_at": t_prior,
                "detected_at": t_prior,
                "old_remaining_fraction": 0.30,
                "new_remaining_fraction": 0.55,
                "delta_fraction": 0.25,
                "old_reset_time": old_reset,
                "new_reset_time": old_reset,
                "source": "python",
            },
        )

    dedup_node = f"""
    import {{ writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';
    const wrote = writeQuotaRows({json.dumps(dedup_db.as_posix())}, [{{
      bucket_id: 'gemini-weekly',
      group_name: 'Gemini Models',
      window: 'weekly',
      remaining_fraction: 0.55,
      reset_time: {json.dumps(old_reset)},
      calibrated_capacity: 672.0,
      fetched_at: {json.dumps(t_now)},
    }}]);
    console.log(JSON.stringify({{ wrote }}));
    """
    dedup_res = _run_node(dedup_node)
    assert dedup_res["wrote"] is True

    with get_db_connection(dedup_db) as conn:
        events = get_quota_reset_events(conn)
        assert len(events) == 1
        assert events[0]["source"] == "python"


def test_sidecar_write_quota_rows_legacy_db_and_parameterized_safety(tmp_path: Path):
    # 1. Minimal legacy DB that only has quota_state (no quota_reset_events)
    legacy_db = tmp_path / "legacy_quota_only.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE quota_state (
            bucket_id TEXT PRIMARY KEY,
            group_name TEXT NOT NULL,
            window TEXT NOT NULL,
            remaining_fraction REAL NOT NULL,
            reset_time TEXT NOT NULL,
            calibrated_capacity REAL NOT NULL,
            fetched_at TEXT NOT NULL
        );
        INSERT INTO quota_state VALUES (
            'gemini-weekly', 'Gemini Models', 'weekly', 0.35,
            '2026-11-30T20:00:00Z', 672.0, '2026-11-25T17:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()

    legacy_node = f"""
    import {{ writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';
    const wrote = writeQuotaRows({json.dumps(legacy_db.as_posix())}, [{{
      bucket_id: 'gemini-weekly',
      group_name: 'Gemini Models',
      window: 'weekly',
      remaining_fraction: 1.0,
      reset_time: '2026-12-02T18:00:00Z',
      calibrated_capacity: 672.0,
      fetched_at: '2026-11-25T18:00:00Z',
    }}]);
    console.log(JSON.stringify({{ wrote }}));
    """
    legacy_res = _run_node(legacy_node)
    assert legacy_res["wrote"] is True

    conn = sqlite3.connect(legacy_db)
    row = conn.execute(
        "SELECT remaining_fraction, reset_time FROM quota_state WHERE bucket_id = 'gemini-weekly'"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(1.0)
    assert row[1] == "2026-12-02T18:00:00Z"

    # 2. SQL injection / special characters in group_name & bucket_id handled safely via parameterized SQL
    sqli_db = tmp_path / "sqli_safe.db"
    init_db(sqli_db)
    with get_db_connection(sqli_db) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini'; DROP TABLE quota_reset_events; --",
                    "window": "weekly",
                    "remaining_fraction": 0.40,
                    "reset_time": "2026-11-30T20:00:00Z",
                    "calibrated_capacity": 672.0,
                    "fetched_at": "2026-11-25T17:00:00Z",
                }
            ],
        )

    sqli_node = f"""
    import {{ writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';
    const wrote = writeQuotaRows({json.dumps(sqli_db.as_posix())}, [{{
      bucket_id: 'gemini-weekly',
      group_name: "Gemini'; DROP TABLE quota_reset_events; --",
      window: 'weekly',
      remaining_fraction: 1.0,
      reset_time: '2026-12-02T17:30:00Z',
      calibrated_capacity: 672.0,
      fetched_at: '2026-11-25T18:00:00Z',
    }}]);
    console.log(JSON.stringify({{ wrote }}));
    """
    sqli_res = _run_node(sqli_node)
    assert sqli_res["wrote"] is True

    with get_db_connection(sqli_db) as conn:
        events = get_quota_reset_events(conn)
        assert len(events) == 1
        assert events[0]["group_name"] == "Gemini'; DROP TABLE quota_reset_events; --"


def test_sidecar_wal_lock_contention_and_rollback_atomicity(tmp_path: Path):
    import threading
    import time

    # 1. Real WAL write-lock contention between Python writer and Node writeQuotaRows
    wal_db = tmp_path / "wal_contention.db"
    init_db(wal_db)
    now = datetime.now(timezone.utc)
    old_reset = (now + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_reset = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_fetched = (now - timedelta(minutes=15)).isoformat()

    with get_db_connection(wal_db) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.45,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                }
            ],
        )

    lock_acquired = threading.Event()
    py_errors: list[Exception] = []

    def python_writer():
        try:
            with get_db_connection(wal_db) as py_conn:
                py_conn.execute("BEGIN IMMEDIATE")
                lock_acquired.set()
                time.sleep(0.15)
                insert_quota_reset_events(
                    py_conn,
                    {
                        "bucket_id": "gemini-weekly",
                        "group_name": "Gemini Models",
                        "window": "weekly",
                        "event_type": "early_reset",
                        "reset_at": now.isoformat(),
                        "detected_at": now.isoformat(),
                        "old_remaining_fraction": 0.45,
                        "new_remaining_fraction": 1.0,
                        "delta_fraction": 0.55,
                        "old_reset_time": old_reset,
                        "new_reset_time": new_reset,
                        "source": "python",
                    },
                )
                upsert_quota_state(
                    py_conn,
                    [
                        {
                            "bucket_id": "gemini-weekly",
                            "group_name": "Gemini Models",
                            "window": "weekly",
                            "remaining_fraction": 1.0,
                            "reset_time": new_reset,
                            "calibrated_capacity": 672.0,
                            "fetched_at": now.isoformat(),
                        }
                    ],
                )
                py_conn.commit()
        except Exception as exc:
            py_errors.append(exc)
            lock_acquired.set()

    t = threading.Thread(target=python_writer)
    t.start()
    assert lock_acquired.wait(timeout=5.0)

    node_code = f"""
    import {{ writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';
    const wrote = writeQuotaRows({json.dumps(wal_db.as_posix())}, [{{
      bucket_id: 'gemini-weekly',
      group_name: 'Gemini Models',
      window: 'weekly',
      remaining_fraction: 1.0,
      reset_time: {json.dumps(new_reset)},
      calibrated_capacity: 672.0,
      fetched_at: {json.dumps(now.isoformat())},
    }}]);
    console.log(JSON.stringify({{ wrote }}));
    """
    node_res = _run_node(node_code)
    t.join(timeout=5.0)

    assert not py_errors
    assert node_res["wrote"] is True

    with get_db_connection(wal_db) as conn:
        events = get_quota_reset_events(conn)
        assert len(events) == 1
        assert events[0]["source"] == "python"

    # 2. Rollback atomicity when mid-transaction write fails
    rollback_db = tmp_path / "rollback.db"
    init_db(rollback_db)
    with get_db_connection(rollback_db) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.45,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                }
            ],
        )
        # Add a trigger that aborts quota_state update to verify quota_reset_events insert rolls back
        conn.execute(
            """
            CREATE TRIGGER abort_quota_update
            BEFORE UPDATE ON quota_state
            BEGIN
                SELECT RAISE(ABORT, 'simulated failure');
            END;
            """
        )
        conn.commit()

    rb_node = f"""
    import {{ writeQuotaRows }} from './sidecars/panel/ls_quota.mjs';
    const wrote = writeQuotaRows({json.dumps(rollback_db.as_posix())}, [{{
      bucket_id: 'gemini-weekly',
      group_name: 'Gemini Models',
      window: 'weekly',
      remaining_fraction: 1.0,
      reset_time: {json.dumps(new_reset)},
      calibrated_capacity: 672.0,
      fetched_at: {json.dumps(now.isoformat())},
    }}]);
    console.log(JSON.stringify({{ wrote }}));
    """
    rb_res = _run_node(rb_node)
    assert rb_res["wrote"] is False

    with get_db_connection(rollback_db) as conn:
        assert get_quota_reset_events(conn) == []
        state = get_quota_state(conn, "gemini-weekly")
        assert state[0]["remaining_fraction"] == pytest.approx(0.45)

