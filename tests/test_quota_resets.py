import sqlite3
from pathlib import Path

import pytest

from context_monitor.db import (
    get_db_connection,
    get_latest_quota_reset,
    get_quota_reset_events,
    init_db,
    insert_quota_reset_events,
)


def test_quota_reset_events_schema_crud_and_dedup(tmp_path: Path):
    db_file = tmp_path / "resets.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        event = {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "event_type": "early_reset",
            "reset_at": "2026-11-25T18:00:00+00:00",
            "detected_at": "2026-11-25T18:01:00+00:00",
            "old_remaining_fraction": 0.42,
            "new_remaining_fraction": 1.0,
            "delta_fraction": 0.58,
            "old_reset_time": "2026-11-30T20:00:00Z",
            "new_reset_time": "2026-12-02T18:00:00Z",
            "source": "python",
        }
        assert insert_quota_reset_events(conn, [event]) == 1
        # Duplicate within 5 minutes with same bucket/event_type/new_reset_time is suppressed
        dup = dict(event, detected_at="2026-11-25T18:02:30+00:00", source="sidecar")
        assert insert_quota_reset_events(conn, [dup]) == 0

        rows = get_quota_reset_events(conn, bucket_id="gemini-weekly")
        assert len(rows) == 1
        assert rows[0]["event_type"] == "early_reset"
        assert rows[0]["delta_fraction"] == pytest.approx(0.58)
        assert get_latest_quota_reset(conn, "gemini-weekly")["id"] == rows[0]["id"]


def test_quota_reset_events_rejects_invalid_event_type(tmp_path: Path):
    db_file = tmp_path / "invalid.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            insert_quota_reset_events(
                conn,
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "event_type": "bogus",
                    "reset_at": "2026-11-25T18:00:00+00:00",
                    "detected_at": "2026-11-25T18:00:00+00:00",
                    "old_remaining_fraction": 0.4,
                    "new_remaining_fraction": 1.0,
                    "delta_fraction": 0.6,
                },
            )


def test_backfill_force_preserves_quota_reset_events(tmp_path: Path, monkeypatch):
    import scripts.backfill_analytics as backfill

    db_file = tmp_path / "backfill.db"
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "early_reset",
                "reset_at": "2026-11-25T18:00:00+00:00",
                "detected_at": "2026-11-25T18:00:00+00:00",
                "old_remaining_fraction": 0.35,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.65,
                "old_reset_time": "2026-11-30T20:00:00Z",
                "new_reset_time": "2026-12-02T18:00:00Z",
                "source": "python",
            },
        )
    monkeypatch.setattr(
        backfill, "sync_and_get_quota_state", lambda conn, force_refresh=True: []
    )
    backfill.run_backfill(brain_dir=brain_dir, force=True, db_path=db_file)
    with get_db_connection(db_file) as conn:
        assert len(get_quota_reset_events(conn)) == 1


def test_quota_reset_events_schema_indexes_and_existing_db_migration(tmp_path: Path):
    db_file = tmp_path / "legacy.db"
    # Simulate an existing DB created before quota_reset_events existed
    legacy_conn = sqlite3.connect(str(db_file))
    legacy_conn.executescript(
        """
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, step_index INTEGER NOT NULL);
        CREATE TABLE tool_executions (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, step_index INTEGER NOT NULL, tool_name TEXT NOT NULL);
        CREATE TABLE transcript_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, step_index INTEGER NOT NULL, block_type TEXT NOT NULL, content TEXT NOT NULL, block_hash TEXT NOT NULL);
        CREATE TABLE quota_state (
            bucket_id TEXT PRIMARY KEY,
            group_name TEXT NOT NULL,
            window TEXT NOT NULL,
            remaining_fraction REAL NOT NULL,
            reset_time TEXT NOT NULL,
            calibrated_capacity REAL NOT NULL,
            fetched_at TEXT NOT NULL
        );
        INSERT INTO quota_state VALUES ('gemini-weekly', 'Gemini Models', 'weekly', 0.75, '2026-11-30T20:00:00Z', 672.0, '2026-11-24T12:00:00Z');
        PRAGMA user_version = 3;
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    init_db(db_file)

    with get_db_connection(db_file) as conn:
        cols = {
            row[1]: row[2].upper()
            for row in conn.execute("PRAGMA table_info(quota_reset_events)").fetchall()
        }
        expected_cols = {
            "id": "INTEGER",
            "bucket_id": "TEXT",
            "group_name": "TEXT",
            "window": "TEXT",
            "event_type": "TEXT",
            "reset_at": "TEXT",
            "detected_at": "TEXT",
            "old_remaining_fraction": "REAL",
            "new_remaining_fraction": "REAL",
            "delta_fraction": "REAL",
            "old_reset_time": "TEXT",
            "new_reset_time": "TEXT",
            "source": "TEXT",
        }
        assert cols == expected_cols

        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='quota_reset_events'"
            ).fetchall()
        }
        assert "idx_quota_resets_reset_at" in indexes
        assert "idx_quota_resets_bucket_time" in indexes

        # Existing quota_state row was preserved intact
        qs = conn.execute("SELECT bucket_id, remaining_fraction FROM quota_state").fetchall()
        assert len(qs) == 1
        assert qs[0][0] == "gemini-weekly"
        assert qs[0][1] == pytest.approx(0.75)

        # Direct SQL enforces CHECK constraint on event_type
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO quota_reset_events (
                    bucket_id, group_name, window, event_type, reset_at, detected_at,
                    old_remaining_fraction, new_remaining_fraction, delta_fraction
                ) VALUES ('gemini-weekly', 'Gemini Models', 'weekly', 'invalid_type',
                          '2026-11-25T18:00:00Z', '2026-11-25T18:00:00Z', 0.4, 1.0, 0.6)
                """
            )


def test_insert_quota_reset_events_edge_cases_and_dedup_window():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    # Empty / None inputs return 0 cleanly
    assert insert_quota_reset_events(conn, []) == 0
    assert insert_quota_reset_events(conn, None) == 0  # type: ignore[arg-type]
    assert insert_quota_reset_events(conn, {}) == 0

    # Single dict with optional defaults omitted
    inserted = insert_quota_reset_events(
        conn,
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "event_type": "top_up",
            "reset_at": "2026-11-25T18:00:00+00:00",
            "detected_at": "2026-11-25T18:00:00+00:00",
            "old_remaining_fraction": 0.0,
            "new_remaining_fraction": 0.4,
            "delta_fraction": 0.4,
        },
    )
    assert inserted == 1

    # Exactly 300s later with same (bucket_id, event_type, new_reset_time="") is skipped
    assert (
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-5h",
                "group_name": "Gemini Models",
                "window": "5h",
                "event_type": "top_up",
                "reset_at": "2026-11-25T18:05:00+00:00",
                "detected_at": "2026-11-25T18:05:00+00:00",
                "old_remaining_fraction": 0.0,
                "new_remaining_fraction": 0.4,
                "delta_fraction": 0.4,
            },
        )
        == 0
    )

    # 301s later is outside the 5-minute window and is inserted
    assert (
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-5h",
                "group_name": "Gemini Models",
                "window": "5h",
                "event_type": "top_up",
                "reset_at": "2026-11-25T18:05:01+00:00",
                "detected_at": "2026-11-25T18:05:01+00:00",
                "old_remaining_fraction": 0.4,
                "new_remaining_fraction": 0.7,
                "delta_fraction": 0.3,
            },
        )
        == 1
    )

    # Batch containing duplicate within same call skips the duplicate
    batch_count = insert_quota_reset_events(
        conn,
        [
            {
                "bucket_id": "3p-weekly",
                "group_name": "Third Party Models",
                "window": "weekly",
                "event_type": "scheduled",
                "reset_at": "2026-11-26T00:00:00Z",
                "detected_at": "2026-11-26T00:01:00Z",
                "old_remaining_fraction": 0.2,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.8,
                "old_reset_time": "2026-11-26T00:00:00Z",
                "new_reset_time": "2026-12-03T00:00:00Z",
            },
            {
                "bucket_id": "3p-weekly",
                "group_name": "Third Party Models",
                "window": "weekly",
                "event_type": "scheduled",
                "reset_at": "2026-11-26T00:00:00Z",
                "detected_at": "2026-11-26T00:02:00Z",
                "old_remaining_fraction": 0.2,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.8,
                "old_reset_time": "2026-11-26T00:00:00Z",
                "new_reset_time": "2026-12-03T00:00:00Z",
                "source": "sidecar",
            },
        ],
    )
    assert batch_count == 1

    # Defaults applied correctly on raw tuple connection (no Row factory)
    rows = get_quota_reset_events(conn, bucket_id="gemini-5h")
    assert len(rows) == 2
    assert rows[0]["old_remaining_fraction"] == 0.0
    assert rows[0]["old_reset_time"] == ""
    assert rows[0]["new_reset_time"] == ""
    assert rows[0]["source"] == "python"

    # Empty bucket_id fails validation
    with pytest.raises(ValueError, match="bucket_id"):
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "  ",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "scheduled",
                "reset_at": "2026-11-26T00:00:00Z",
                "detected_at": "2026-11-26T00:00:00Z",
                "old_remaining_fraction": 0.1,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.9,
            },
        )
    conn.close()


def test_get_quota_reset_events_filters_ordering_and_latest():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    insert_quota_reset_events(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "scheduled",
                "reset_at": "2026-11-26T10:00:00Z",
                "detected_at": "2026-11-26T10:01:00Z",
                "old_remaining_fraction": 0.2,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.8,
                "new_reset_time": "2026-12-03T10:00:00Z",
            },
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "early_reset",
                "reset_at": "2026-11-24T15:00:00+00:00",
                "detected_at": "2026-11-24T15:00:00+00:00",
                "old_remaining_fraction": 0.4,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.6,
                "new_reset_time": "2026-12-01T15:00:00Z",
            },
            {
                "bucket_id": "gemini-5h",
                "group_name": "Gemini Models",
                "window": "5h",
                "event_type": "scheduled",
                "reset_at": "2026-11-25T05:00:00Z",
                "detected_at": "2026-11-25T05:00:30Z",
                "old_remaining_fraction": 0.1,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.9,
                "new_reset_time": "2026-11-25T10:00:00Z",
            },
        ],
    )

    all_events = get_quota_reset_events(conn)
    assert [e["reset_at"] for e in all_events] == [
        "2026-11-24T15:00:00+00:00",
        "2026-11-25T05:00:00Z",
        "2026-11-26T10:00:00Z",
    ]

    assert len(get_quota_reset_events(conn, window="weekly")) == 2
    assert len(get_quota_reset_events(conn, window="5h")) == 1
    assert len(get_quota_reset_events(conn, limit=2)) == 2
    assert get_quota_reset_events(conn, limit=0) == []

    # Mixed Z vs +00:00 timestamps in since / until filters
    ranged = get_quota_reset_events(
        conn,
        since="2026-11-24T15:00:00Z",
        until="2026-11-25T05:00:00+00:00",
    )
    assert len(ranged) == 2
    assert [e["bucket_id"] for e in ranged] == ["gemini-weekly", "gemini-5h"]

    latest = get_latest_quota_reset(conn, "gemini-weekly")
    assert latest is not None
    assert latest["event_type"] == "scheduled"
    assert latest["reset_at"] == "2026-11-26T10:00:00Z"

    latest_since = get_latest_quota_reset(
        conn, "gemini-weekly", since="2026-11-26T10:00:00+00:00"
    )
    assert latest_since is not None
    assert latest_since["id"] == latest["id"]

    assert (
        get_latest_quota_reset(
            conn, "gemini-weekly", since="2026-11-27T00:00:00+00:00"
        )
        is None
    )
    assert get_latest_quota_reset(conn, "non-existent") is None
    conn.close()


def test_insert_quota_reset_events_respects_begin_immediate_transaction(tmp_path: Path):
    db_file = tmp_path / "tx_test.db"
    init_db(db_file)

    conn1 = get_db_connection(db_file)
    conn2 = get_db_connection(db_file)
    event = {
        "bucket_id": "gemini-weekly",
        "group_name": "Gemini Models",
        "window": "weekly",
        "event_type": "early_reset",
        "reset_at": "2026-11-25T18:00:00+00:00",
        "detected_at": "2026-11-25T18:00:00+00:00",
        "old_remaining_fraction": 0.4,
        "new_remaining_fraction": 1.0,
        "delta_fraction": 0.6,
        "new_reset_time": "2026-12-02T18:00:00Z",
    }

    try:
        conn1.execute("BEGIN IMMEDIATE")
        assert conn1.in_transaction
        assert insert_quota_reset_events(conn1, event) == 1
        # Must NOT have committed the caller's outer transaction
        assert conn1.in_transaction
        assert get_quota_reset_events(conn2) == []

        conn1.execute("ROLLBACK")
        assert not conn1.in_transaction
        assert get_quota_reset_events(conn2) == []

        conn1.execute("BEGIN IMMEDIATE")
        assert insert_quota_reset_events(conn1, event) == 1
        assert conn1.in_transaction
        assert get_quota_reset_events(conn2) == []

        conn1.execute("COMMIT")
        assert not conn1.in_transaction
        assert len(get_quota_reset_events(conn2)) == 1
    finally:
        conn1.close()
        conn2.close()


def test_package_exports_quota_reset_helpers():
    import context_monitor
    import context_monitor.db as db_mod

    for name in (
        "insert_quota_reset_events",
        "get_quota_reset_events",
        "get_latest_quota_reset",
    ):
        assert name in context_monitor.__all__
        assert name in db_mod.__all__
        assert callable(getattr(context_monitor, name))


def test_detect_quota_reset_event_exports():
    import context_monitor
    import context_monitor.quota as quota_mod

    assert "detect_quota_reset_event" in context_monitor.__all__
    assert "detect_quota_reset_event" in quota_mod.__all__
    assert callable(getattr(context_monitor, "detect_quota_reset_event"))
    assert quota_mod.IDLE_REMAINING_THRESHOLD == 0.99
    assert quota_mod.MIN_REMAINING_JUMP == 0.05
    assert quota_mod.FULL_RESET_REMAINING_FLOOR == 0.90
    assert quota_mod.SCHEDULED_TOLERANCE_SECONDS == 900
    assert quota_mod.RESET_TIME_SHIFT_THRESHOLD_SECONDS == 900


def test_detect_scheduled_reset_online_and_offline_overnight():
    from datetime import datetime, timedelta, timezone
    from context_monitor.quota import detect_quota_reset_event

    old_reset = "2026-11-25T18:00:00Z"
    new_reset = "2026-12-02T18:00:00Z"
    old_row = {
        "bucket_id": "gemini-weekly",
        "group_name": "Gemini Models",
        "window": "weekly",
        "remaining_fraction": 0.35,
        "reset_time": old_reset,
        "fetched_at": "2026-11-25T17:00:00Z",
    }

    # 1. Online scheduled reset detected 2m after old_reset
    ev_online = detect_quota_reset_event(
        old_row,
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": new_reset,
        },
        detected_at="2026-11-25T18:02:00Z",
        source="python",
    )
    assert ev_online is not None
    assert ev_online["event_type"] == "scheduled"
    assert ev_online["reset_at"] == old_reset
    assert ev_online["detected_at"] == "2026-11-25T18:02:00+00:00"
    assert ev_online["old_remaining_fraction"] == 0.35
    assert ev_online["new_remaining_fraction"] == 1.0
    assert ev_online["delta_fraction"] == pytest.approx(0.65)
    assert ev_online["old_reset_time"] == old_reset
    assert ev_online["new_reset_time"] == new_reset
    assert ev_online["source"] == "python"

    # 2. Scheduled reset detected slightly early (within 15m tolerance)
    ev_tol = detect_quota_reset_event(
        old_row,
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": new_reset,
        },
        detected_at=datetime(2026, 11, 25, 17, 50, 0, tzinfo=timezone.utc),
    )
    assert ev_tol is not None
    assert ev_tol["event_type"] == "scheduled"
    assert ev_tol["reset_at"] == old_reset

    # 3. Offline overnight scheduled reset (detected 13 hours after old_reset_time)
    ev_offline = detect_quota_reset_event(
        old_row,
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": new_reset,
        },
        detected_at="2026-11-26T07:00:00Z",
    )
    assert ev_offline is not None
    assert ev_offline["event_type"] == "scheduled"
    assert ev_offline["reset_at"] == old_reset

    # 4. Scheduled reset where user burned immediately after reset (delta <= 0, reset_time advanced)
    ev_burned = detect_quota_reset_event(
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.50,
            "reset_time": "2026-11-25T18:00:00Z",
            "fetched_at": "2026-11-25T17:30:00Z",
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.45,
            "reset_time": "2026-11-25T23:00:00Z",
        },
        detected_at="2026-11-25T20:00:00Z",
    )
    assert ev_burned is not None
    assert ev_burned["event_type"] == "scheduled"
    assert ev_burned["reset_at"] == "2026-11-25T18:00:00Z"
    assert ev_burned["delta_fraction"] == pytest.approx(-0.05)


def test_detect_early_reset_shifted_and_unshifted():
    from context_monitor.quota import detect_quota_reset_event

    # 1. Early reset with shifted reset_time -> inferred reset_at = new_reset_dt - 7d
    ev_shifted = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.42,
            "reset_time": "2026-11-30T20:00:00Z",
            "fetched_at": "2026-11-25T12:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-12-02T14:00:00Z",
        },
        detected_at="2026-11-25T14:05:00Z",
    )
    assert ev_shifted is not None
    assert ev_shifted["event_type"] == "early_reset"
    assert ev_shifted["reset_at"] == "2026-11-25T14:00:00+00:00"

    # 2. 5h bucket early reset with shifted reset_time -> uses 5h window for inference
    ev_5h = detect_quota_reset_event(
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.20,
            "reset_time": "2026-11-25T18:00:00Z",
            "fetched_at": "2026-11-25T14:00:00Z",
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 1.0,
            "reset_time": "2026-11-25T20:00:00Z",
        },
        detected_at="2026-11-25T15:02:00Z",
    )
    assert ev_5h is not None
    assert ev_5h["event_type"] == "early_reset"
    assert ev_5h["reset_at"] == "2026-11-25T15:00:00+00:00"

    # 3. Early reset with unshifted reset_time -> inferred falls before old_fetched_at, uses detected_at
    ev_unshifted = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.42,
            "reset_time": "2026-11-30T20:00:00Z",
            "fetched_at": "2026-11-25T12:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-11-30T20:00:00Z",
        },
        detected_at="2026-11-25T14:05:00Z",
    )
    assert ev_unshifted is not None
    assert ev_unshifted["event_type"] == "early_reset"
    assert ev_unshifted["reset_at"] == "2026-11-25T14:05:00+00:00"

    # 4. Early reset from near-full (0.97 -> 1.00, delta = 0.03 > 0.005) with shifted reset_time
    ev_near_full = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.97,
            "reset_time": "2026-11-30T20:00:00Z",
            "fetched_at": "2026-11-25T12:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-12-02T14:00:00Z",
        },
        detected_at="2026-11-25T14:05:00Z",
    )
    assert ev_near_full is not None
    assert ev_near_full["event_type"] == "early_reset"

    # 5. Early reset when old_fetched_at is missing and inferred <= detected_at
    ev_no_fetched = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.40,
            "reset_time": "2026-11-30T20:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-12-02T14:00:00Z",
        },
        detected_at="2026-11-25T14:05:00Z",
    )
    assert ev_no_fetched is not None
    assert ev_no_fetched["event_type"] == "early_reset"
    assert ev_no_fetched["reset_at"] == "2026-11-25T14:00:00+00:00"


def test_detect_mid_cycle_top_up():
    from context_monitor.quota import detect_quota_reset_event

    ev = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.30,
            "reset_time": "2026-11-30T20:00:00Z",
            "fetched_at": "2026-11-25T12:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.55,
            "reset_time": "2026-11-30T20:00:00Z",
        },
        detected_at="2026-11-25T14:00:00Z",
    )
    assert ev is not None
    assert ev["event_type"] == "top_up"
    assert ev["reset_at"] == "2026-11-25T14:00:00+00:00"
    assert ev["delta_fraction"] == pytest.approx(0.25)

    # Exact 5% jump (0.30 -> 0.35 has IEEE-754 delta 0.04999999999999999) still triggers
    ev_exact_5pct = detect_quota_reset_event(
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.30,
            "reset_time": "2026-11-30T20:00:00Z",
            "fetched_at": "2026-11-25T12:00:00Z",
        },
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.35,
            "reset_time": "2026-11-30T20:05:00Z",  # within 15m tolerance
        },
        detected_at="2026-11-25T14:00:00Z",
    )
    assert ev_exact_5pct is not None
    assert ev_exact_5pct["event_type"] == "top_up"


def test_ignore_idle_bucket_drift_and_bootstrap_fallback():
    from context_monitor.quota import detect_quota_reset_event

    # 1. None old_row (first fetch)
    assert (
        detect_quota_reset_event(
            None,
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 1.0,
                "reset_time": "2026-12-02T00:00:00Z",
            },
            detected_at="2026-11-25T14:00:00Z",
        )
        is None
    )

    # 2. Idle ~100% bucket drift (3p-weekly sliding reset_time = now + 7d)
    assert (
        detect_quota_reset_event(
            {
                "bucket_id": "3p-weekly",
                "remaining_fraction": 0.99,
                "reset_time": "2026-12-01T14:00:00Z",
                "fetched_at": "2026-11-24T14:00:00Z",
            },
            {
                "bucket_id": "3p-weekly",
                "remaining_fraction": 1.0,
                "reset_time": "2026-12-02T14:00:00Z",
            },
            detected_at="2026-11-25T14:00:00Z",
        )
        is None
    )

    # 3. Bootstrap fallback row (empty reset_time)
    assert (
        detect_quota_reset_event(
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.50,
                "reset_time": "",
            },
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 1.0,
                "reset_time": "2026-12-02T14:00:00Z",
            },
            detected_at="2026-11-25T14:00:00Z",
        )
        is None
    )

    # 4. Normal burn / jitter
    assert (
        detect_quota_reset_event(
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.80,
                "reset_time": "2026-11-30T20:00:00Z",
            },
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.82,
                "reset_time": "2026-11-30T20:00:00Z",
            },
            detected_at="2026-11-25T14:00:00Z",
        )
        is None
    )

    # 5. Mid-cycle reset_time advance without remaining jump
    assert (
        detect_quota_reset_event(
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.50,
                "reset_time": "2026-11-30T20:00:00Z",
            },
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.50,
                "reset_time": "2026-11-30T20:30:00Z",
            },
            detected_at="2026-11-25T14:00:00Z",
        )
        is None
    )


def test_sync_and_get_quota_state_records_reset_atomically_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from datetime import datetime, timedelta, timezone
    from context_monitor.db import upsert_quota_state
    import context_monitor.quota as quota_mod

    db_file = tmp_path / "atomic_sync.db"
    init_db(db_file)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    old_reset = (now + timedelta(days=4)).isoformat()
    new_reset = (now + timedelta(days=7) - timedelta(minutes=5)).isoformat()
    old_fetched = (now - timedelta(hours=2)).isoformat()

    with get_db_connection(db_file) as conn:
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
                }
            ],
        )

        payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 1.0,
                                "resetTime": new_reset,
                            }
                        ],
                    }
                ]
            }
        }

        state1 = quota_mod.sync_and_get_quota_state(
            conn, force_refresh=True, ls_fetcher=lambda: payload
        )
        assert state1["gemini-weekly"]["remaining_fraction"] == 1.0
        assert not conn.in_transaction

        # Second sync with identical post-reset payload must NOT duplicate reset event
        # and must close BEGIN IMMEDIATE cleanly on write suppression (not in_transaction)
        state2 = quota_mod.sync_and_get_quota_state(
            conn, force_refresh=True, ls_fetcher=lambda: payload
        )
        assert state2["gemini-weekly"]["remaining_fraction"] == 1.0
        assert not conn.in_transaction

        events = get_quota_reset_events(conn, bucket_id="gemini-weekly")
        assert len(events) == 1
        assert events[0]["event_type"] == "early_reset"
        assert events[0]["source"] == "python"

        # Verify rollback atomicity: if upsert_quota_state fails after insert_quota_reset_events,
        # the quota_reset_events insert is rolled back too.
        def failing_upsert(*args, **kwargs):
            raise RuntimeError("simulated write failure")

        topup_payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.50,
                                "resetTime": new_reset,
                            }
                        ],
                    }
                ]
            }
        }
        # First bring remaining down to 0.30 directly in DB so topup triggers
        conn.execute(
            "UPDATE quota_state SET remaining_fraction = 0.30 WHERE bucket_id = 'gemini-weekly'"
        )
        conn.commit()
        with monkeypatch.context() as mp:
            mp.setattr(quota_mod, "upsert_quota_state", failing_upsert)
            with pytest.raises(RuntimeError, match="simulated write failure"):
                quota_mod.sync_and_get_quota_state(
                    conn, force_refresh=True, ls_fetcher=lambda: topup_payload
                )
        assert not conn.in_transaction
        assert len(get_quota_reset_events(conn, bucket_id="gemini-weekly")) == 1

    # Simulated concurrent external writer (e.g. Node sidecar) committing the same reset
    # while Python is in-flight fetching from LS: Python re-reads quota_state inside
    # BEGIN IMMEDIATE and must NOT double-detect or insert a second event.
    db_race = tmp_path / "race_sync.db"
    init_db(db_race)
    with get_db_connection(db_race) as conn_py, get_db_connection(db_race) as conn_sidecar:
        upsert_quota_state(
            conn_py,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.42,
                    "reset_time": old_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": old_fetched,
                }
            ],
        )
        race_payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 1.0,
                            "resetTime": new_reset,
                        }
                    ],
                }
            ]
        }

        def fetcher_with_interleaved_sidecar_commit():
            conn_sidecar.execute("BEGIN IMMEDIATE")
            insert_quota_reset_events(
                conn_sidecar,
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "event_type": "early_reset",
                    "reset_at": (now - timedelta(minutes=5)).isoformat(),
                    "detected_at": now.isoformat(),
                    "old_remaining_fraction": 0.42,
                    "new_remaining_fraction": 1.0,
                    "delta_fraction": 0.58,
                    "old_reset_time": old_reset,
                    "new_reset_time": new_reset,
                    "source": "sidecar",
                },
            )
            conn_sidecar.execute(
                "UPDATE quota_state SET remaining_fraction = 1.0, reset_time = ?, fetched_at = ? WHERE bucket_id = 'gemini-weekly'",
                (new_reset, now.isoformat()),
            )
            conn_sidecar.commit()
            return race_payload

        state_race = quota_mod.sync_and_get_quota_state(
            conn_py,
            force_refresh=True,
            ls_fetcher=fetcher_with_interleaved_sidecar_commit,
        )
        assert state_race["gemini-weekly"]["remaining_fraction"] == 1.0
        assert not conn_py.in_transaction
        race_events = get_quota_reset_events(conn_py, bucket_id="gemini-weekly")
        assert len(race_events) == 1
        assert race_events[0]["source"] == "sidecar"

    # End-to-end: sync_and_get_quota_state inserts early_reset BEFORE calibrate_capacity runs,
    # so pre-reset spend in the same 7d window does NOT inflate calibrated_capacity.
    db_e2e = tmp_path / "sync_cal_floor.db"
    init_db(db_e2e)
    with get_db_connection(db_e2e) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
        )
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
            VALUES ('c1', 1, ?, 'PLANNER_RESPONSE', 'Gemini 3.8 Flash', 250.0)
            """,
            ((now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
            VALUES ('c1', 2, ?, 'PLANNER_RESPONSE', 'Gemini 3.8 Flash', 40.0)
            """,
            ((now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.40,
                    "reset_time": (now + timedelta(days=5)).isoformat(),
                    "calibrated_capacity": 672.0,
                    "fetched_at": (now - timedelta(hours=2)).isoformat(),
                }
            ],
        )
        # Shifted early reset happened 30m ago and user already spent $40 (remaining=0.90)
        shifted_new_reset = (now + timedelta(days=7) - timedelta(minutes=30)).isoformat()
        state_e2e = quota_mod.sync_and_get_quota_state(
            conn,
            force_refresh=True,
            ls_fetcher=lambda: {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.90,
                                "resetTime": shifted_new_reset,
                            }
                        ],
                    }
                ]
            },
        )
        # Post-reset step (-10m, $40) is inside [now - 30m, now]; pre-reset step (-1d, $250) is excluded
        assert state_e2e["gemini-weekly"]["calibrated_capacity"] == pytest.approx(617.6, abs=0.01)
        assert len(get_quota_reset_events(conn, bucket_id="gemini-weekly")) == 1


def test_calibrate_capacity_respects_early_reset_floor_and_skips_top_up(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from context_monitor.db import upsert_quota_state
    from context_monitor.quota import calibrate_capacity

    db_file = tmp_path / "cal_floor.db"
    init_db(db_file)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pre_step_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reset_ts = (now - timedelta(hours=2)).isoformat()
    post_step_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_reset = (now + timedelta(days=5)).isoformat()

    with get_db_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
        )
        conn.executemany(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
            VALUES ('c1', ?, ?, 'PLANNER_RESPONSE', 'Gemini 3.8 Flash', ?)
            """,
            [
                (1, pre_step_ts, 250.0),
                (2, post_step_ts, 40.0),
            ],
        )
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.90,
                    "reset_time": future_reset,
                    "calibrated_capacity": 672.0,
                    "fetched_at": now.isoformat(),
                }
            ],
        )
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "early_reset",
                "reset_at": reset_ts,
                "detected_at": reset_ts,
                "old_remaining_fraction": 0.25,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.75,
                "old_reset_time": (now + timedelta(days=2)).isoformat(),
                "new_reset_time": future_reset,
                "source": "python",
            },
        )

        # Should calibrate ONLY on post-reset $40 (raw=40/0.10=400.0; EMA=0.8*672+0.2*400=617.6),
        # NOT pre+post $290 (raw=2900 -> EMA=0.8*672+0.2*2900=1117.6 -> clamped 1000.0)
        cap_early = calibrate_capacity(
            conn=conn,
            bucket_id="gemini-weekly",
            remaining_fraction=0.90,
            reset_time=future_reset,
            fallback_capacity=672.0,
        )
        assert cap_early == pytest.approx(617.6, abs=0.01)

        # Scheduled reset event also enforces floor (e.g. if reset_at is newer than reset_dt - window)
        sched_ts = (now - timedelta(minutes=90)).isoformat()
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "scheduled",
                "reset_at": sched_ts,
                "detected_at": sched_ts,
                "old_remaining_fraction": 0.20,
                "new_remaining_fraction": 1.0,
                "delta_fraction": 0.80,
                "old_reset_time": sched_ts,
                "new_reset_time": future_reset,
                "source": "python",
            },
        )
        cap_sched = calibrate_capacity(
            conn=conn,
            bucket_id="gemini-weekly",
            remaining_fraction=0.90,
            reset_time=future_reset,
            fallback_capacity=672.0,
        )
        assert cap_sched == pytest.approx(617.6, abs=0.01)

        # After a mid-cycle top_up, calibrate_capacity returns fallback_val (persisted 672.0) unchanged
        topup_ts = (now - timedelta(minutes=30)).isoformat()
        insert_quota_reset_events(
            conn,
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "event_type": "top_up",
                "reset_at": topup_ts,
                "detected_at": topup_ts,
                "old_remaining_fraction": 0.30,
                "new_remaining_fraction": 0.60,
                "delta_fraction": 0.30,
                "old_reset_time": future_reset,
                "new_reset_time": future_reset,
                "source": "python",
            },
        )
        cap_topup = calibrate_capacity(
            conn=conn,
            bucket_id="gemini-weekly",
            remaining_fraction=0.60,
            reset_time=future_reset,
            fallback_capacity=672.0,
        )
        assert cap_topup == 672.0

    # Legacy DB without quota_reset_events table still calibrates gracefully
    legacy_conn = sqlite3.connect(":memory:")
    legacy_conn.executescript(
        """
        CREATE TABLE quota_state (
            bucket_id TEXT PRIMARY KEY,
            calibrated_capacity REAL
        );
        CREATE TABLE steps (
            created_at TEXT,
            model_name TEXT,
            billed_cost REAL
        );
        """
    )
    legacy_conn.execute(
        "INSERT INTO steps VALUES (?, 'Gemini 3.8 Flash', 40.0)",
        (post_step_ts,),
    )
    cap_legacy = calibrate_capacity(
        conn=legacy_conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time=future_reset,
        fallback_capacity=672.0,
    )
    assert cap_legacy == 400.0
    legacy_conn.close()
