import sqlite3
import pytest

from context_monitor.db import (
    checkpoint_wal_if_needed,
    get_db_connection,
    init_db,
    upsert_quota_state,
    get_quota_state,
)
from context_monitor.settings import (
    DEFAULT_GEMINI_WEEKLY_CAPACITY,
    DEFAULT_GEMINI_5H_CAPACITY,
    DEFAULT_3P_WEEKLY_CAPACITY,
    DEFAULT_3P_5H_CAPACITY,
    QUOTA_CACHE_TTL_SECONDS,
    QUOTA_REQUEST_TIMEOUT_SECONDS,
    get_settings,
    get_gemini_weekly_capacity,
    get_gemini_5h_capacity,
    get_3p_weekly_capacity,
    get_3p_5h_capacity,
    get_quota_cache_ttl,
    get_quota_request_timeout,
)


def test_init_db_creates_quota_state_table(tmp_path):
    db_file = tmp_path / "test_init.db"
    init_db(db_file)

    with get_db_connection(db_file) as conn:
        cur = conn.cursor()
        cols = {
            c[1]: c[2] for c in cur.execute("PRAGMA table_info(quota_state)").fetchall()
        }

    expected_cols = {
        "bucket_id": "TEXT",
        "group_name": "TEXT",
        "window": "TEXT",
        "remaining_fraction": "REAL",
        "reset_time": "TEXT",
        "calibrated_capacity": "REAL",
        "fetched_at": "TEXT",
    }
    for col, col_type in expected_cols.items():
        assert col in cols, f"Column '{col}' missing in quota_state"
        assert cols[col].upper() == col_type, (
            f"Column '{col}' type mismatch: {cols[col]} != {col_type}"
        )


def test_init_db_accepts_connection_directly():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    cur = conn.cursor()
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "quota_state" in tables
    assert "conversations" in tables
    assert "steps" in tables
    conn.close()


def test_init_db_idempotent():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    # Calling init_db second time should be completely safe and idempotent
    init_db(conn)
    cur = conn.cursor()
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "quota_state" in tables
    conn.close()


def test_quota_state_schema_migration():
    conn = sqlite3.connect(":memory:")
    # Simulate an older schema without calibrated_capacity and fetched_at
    conn.execute("""
        CREATE TABLE quota_state (
            bucket_id TEXT PRIMARY KEY,
            group_name TEXT NOT NULL,
            window TEXT NOT NULL,
            remaining_fraction REAL NOT NULL,
            reset_time TEXT NOT NULL
        );
    """)
    conn.commit()

    # Run init_db which should perform migration
    init_db(conn)

    cols = [c[1] for c in conn.execute("PRAGMA table_info(quota_state)").fetchall()]
    assert "calibrated_capacity" in cols
    assert "fetched_at" in cols
    conn.close()


def test_upsert_and_get_quota_state():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    rows = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.9348,
            "reset_time": "2026-10-21T02:27:22Z",
            "calibrated_capacity": 672.0,
            "fetched_at": "2026-10-14T12:00:00Z",
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.9591,
            "reset_time": "2026-10-14T14:00:00Z",
            "calibrated_capacity": 236.7,
            "fetched_at": "2026-10-14T12:00:00Z",
        },
    ]

    upsert_quota_state(conn, rows)

    stored = get_quota_state(conn)
    assert len(stored) == 2

    # Check ordering by bucket_id
    assert stored[0]["bucket_id"] == "gemini-5h"
    assert stored[0]["window"] == "5h"
    assert pytest.approx(stored[0]["calibrated_capacity"]) == 236.7

    assert stored[1]["bucket_id"] == "gemini-weekly"
    assert stored[1]["window"] == "weekly"
    assert pytest.approx(stored[1]["calibrated_capacity"]) == 672.0
    conn.close()


def test_upsert_quota_state_on_conflict_update():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    initial = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.9348,
            "reset_time": "2026-10-21T02:27:22Z",
            "calibrated_capacity": 672.0,
            "fetched_at": "2026-10-14T12:00:00Z",
        }
    ]
    upsert_quota_state(conn, initial)

    updated = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models Updated",
            "window": "weekly",
            "remaining_fraction": 0.8812,
            "reset_time": "2026-10-21T02:27:22Z",
            "calibrated_capacity": 675.5,
            "fetched_at": "2026-10-14T12:05:00Z",
        }
    ]
    upsert_quota_state(conn, updated)

    stored = get_quota_state(conn)
    assert len(stored) == 1
    assert stored[0]["group_name"] == "Gemini Models Updated"
    assert pytest.approx(stored[0]["remaining_fraction"]) == 0.8812
    assert pytest.approx(stored[0]["calibrated_capacity"]) == 675.5
    assert stored[0]["fetched_at"] == "2026-10-14T12:05:00Z"
    conn.close()


def test_upsert_quota_state_edge_cases():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    # Empty list
    upsert_quota_state(conn, [])
    assert get_quota_state(conn) == []

    # Single dict instead of list
    upsert_quota_state(
        conn,
        {
            "bucket_id": "3p-weekly",
            "group_name": "Third Party Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-10-21T00:00:00Z",
            "calibrated_capacity": 200.0,
            "fetched_at": "2026-10-14T12:00:00Z",
        },
    )
    res = get_quota_state(conn)
    assert len(res) == 1
    assert res[0]["bucket_id"] == "3p-weekly"

    # Specific bucket_id retrieval
    res_3p = get_quota_state(conn, bucket_id="3p-weekly")
    assert len(res_3p) == 1
    assert res_3p[0]["bucket_id"] == "3p-weekly"

    res_none = get_quota_state(conn, bucket_id="non-existent")
    assert res_none == []
    conn.close()


def test_settings_quota_defaults():
    assert DEFAULT_GEMINI_WEEKLY_CAPACITY == 672.0
    assert DEFAULT_GEMINI_5H_CAPACITY == 236.7
    assert DEFAULT_3P_WEEKLY_CAPACITY == 200.0
    assert DEFAULT_3P_5H_CAPACITY == 70.0
    assert QUOTA_CACHE_TTL_SECONDS == 60
    assert QUOTA_REQUEST_TIMEOUT_SECONDS == 1.5

    settings = get_settings()
    assert settings["default_weekly_capacity_gemini"] == 672.0
    assert settings["default_5h_capacity_gemini"] == 236.7
    assert settings["default_weekly_capacity_3p"] == 200.0
    assert settings["default_5h_capacity_3p"] == 70.0
    assert settings["quota_cache_ttl_seconds"] == 60
    assert settings["quota_request_timeout_seconds"] == 1.5
    assert settings["subscription_monthly_price"] == 200.0


def test_settings_quota_environment_overrides(monkeypatch):
    monkeypatch.setenv("AGY_GEMINI_WEEKLY_CAPACITY", "750.0")
    monkeypatch.setenv("AGY_GEMINI_5H_CAPACITY", "250.0")
    monkeypatch.setenv("AGY_3P_WEEKLY_CAPACITY", "220.0")
    monkeypatch.setenv("AGY_3P_5H_CAPACITY", "80.0")
    monkeypatch.setenv("AGY_QUOTA_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("AGY_QUOTA_REQUEST_TIMEOUT_SECONDS", "3.0")

    assert get_gemini_weekly_capacity() == 750.0
    assert get_gemini_5h_capacity() == 250.0
    assert get_3p_weekly_capacity() == 220.0
    assert get_3p_5h_capacity() == 80.0
    assert get_quota_cache_ttl() == 120
    assert get_quota_request_timeout() == 3.0

    settings = get_settings()
    assert settings["default_weekly_capacity_gemini"] == 750.0
    assert settings["default_5h_capacity_gemini"] == 250.0
    assert settings["default_weekly_capacity_3p"] == 220.0
    assert settings["default_5h_capacity_3p"] == 80.0
    assert settings["quota_cache_ttl_seconds"] == 120
    assert settings["quota_request_timeout_seconds"] == 3.0


def test_upsert_quota_state_none_and_missing_values():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    # 1. Null remaining_fraction and calibrated_capacity should not raise TypeError
    # 2. Null reset_time should not store literal "None" string
    # 3. Null fetched_at should default to current UTC ISO timestamp
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "test-nulls",
                "remaining_fraction": None,
                "calibrated_capacity": None,
                "reset_time": None,
                "fetched_at": None,
            }
        ],
    )
    stored = get_quota_state(conn, bucket_id="test-nulls")
    assert len(stored) == 1
    assert stored[0]["remaining_fraction"] == 1.0
    assert stored[0]["calibrated_capacity"] == 0.0
    assert stored[0]["reset_time"] == ""
    assert stored[0]["fetched_at"] != ""
    assert stored[0]["fetched_at"] != "None"
    assert "T" in stored[0]["fetched_at"]  # Valid ISO timestamp

    # 4. Zero (0.0) remaining_fraction should NOT be replaced by 1.0 default
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "test-zero",
                "remaining_fraction": 0.0,
                "calibrated_capacity": 0.0,
            }
        ],
    )
    stored_zero = get_quota_state(conn, bucket_id="test-zero")
    assert stored_zero[0]["remaining_fraction"] == 0.0

    # 5. Missing or whitespace bucket_id raises ValueError
    with pytest.raises(ValueError, match="bucket_id is required"):
        upsert_quota_state(conn, [{"bucket_id": None}])

    with pytest.raises(ValueError, match="bucket_id is required"):
        upsert_quota_state(conn, [{"bucket_id": "   "}])

    conn.close()


def test_upsert_quota_state_camel_case_keys():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    # Test Language Server ConnectRPC payload structure
    ls_bucket = {
        "bucketId": "gemini-weekly",
        "displayName": "Weekly Limit Remaining",
        "window": "weekly",
        "remainingFraction": 0.8421,
        "resetTime": "2026-10-21T02:27:22Z",
        "calibratedCapacity": 672.0,
        "fetchedAt": "2026-10-14T13:00:00Z",
    }
    upsert_quota_state(conn, [ls_bucket])

    stored = get_quota_state(conn, bucket_id="gemini-weekly")
    assert len(stored) == 1
    assert stored[0]["bucket_id"] == "gemini-weekly"
    assert stored[0]["group_name"] == "Weekly Limit Remaining"
    assert stored[0]["window"] == "weekly"
    assert pytest.approx(stored[0]["remaining_fraction"]) == 0.8421
    assert stored[0]["reset_time"] == "2026-10-21T02:27:22Z"
    assert pytest.approx(stored[0]["calibrated_capacity"]) == 672.0
    assert stored[0]["fetched_at"] == "2026-10-14T13:00:00Z"
    conn.close()


def test_quota_state_updated_at_bidirectional_compatibility():
    conn = sqlite3.connect(":memory:")
    init_db(conn)

    # Upsert with updated_at instead of fetched_at
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "3p-weekly",
                "window": "weekly",
                "remaining_fraction": 0.5,
                "updated_at": "2026-10-14T13:30:00Z",
            }
        ],
    )

    stored = get_quota_state(conn, bucket_id="3p-weekly")
    assert len(stored) == 1
    # Both fetched_at and updated_at are present and equal
    assert stored[0]["fetched_at"] == "2026-10-14T13:30:00Z"
    assert stored[0]["updated_at"] == "2026-10-14T13:30:00Z"
    conn.close()


def test_settings_context_monitor_prefix_overrides(monkeypatch):
    monkeypatch.setenv("CONTEXT_MONITOR_GEMINI_WEEKLY_CAPACITY", "800.0")
    monkeypatch.setenv("CONTEXT_MONITOR_GEMINI_5H_CAPACITY", "300.0")
    monkeypatch.setenv("CONTEXT_MONITOR_3P_WEEKLY_CAPACITY", "250.0")
    monkeypatch.setenv("CONTEXT_MONITOR_3P_5H_CAPACITY", "90.0")
    monkeypatch.setenv("CONTEXT_MONITOR_QUOTA_CACHE_TTL_SECONDS", "180")
    monkeypatch.setenv("CONTEXT_MONITOR_QUOTA_REQUEST_TIMEOUT_SECONDS", "4.5")

    assert get_gemini_weekly_capacity() == 800.0
    assert get_gemini_5h_capacity() == 300.0
    assert get_3p_weekly_capacity() == 250.0
    assert get_3p_5h_capacity() == 90.0
    assert get_quota_cache_ttl() == 180
    assert get_quota_request_timeout() == 4.5


def test_context_monitor_package_exports():
    import context_monitor

    assert hasattr(context_monitor, "upsert_quota_state")
    assert hasattr(context_monitor, "get_quota_state")
    assert callable(context_monitor.upsert_quota_state)
    assert callable(context_monitor.get_quota_state)


def test_db_connection_sets_journal_size_limit(tmp_path):
    db_path = tmp_path / "wal_limit.db"
    with get_db_connection(db_path) as conn:
        limit = conn.execute("PRAGMA journal_size_limit;").fetchone()[0]
        assert limit == 67108864


def test_checkpoint_wal_if_needed(tmp_path):
    db_path = tmp_path / "wal_test.db"
    with get_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE t (x TEXT);")
        conn.commit()

        # Insert some rows to ensure WAL has data
        conn.executemany("INSERT INTO t VALUES (?);", [(f"val-{i}",) for i in range(100)])
        conn.commit()

        wal_path = tmp_path / "wal_test.db-wal"
        assert wal_path.exists()
        current_wal_size = wal_path.stat().st_size
        assert current_wal_size > 0

        # If WAL is smaller than max_wal_bytes, no checkpoint is performed
        assert not checkpoint_wal_if_needed(conn, db_path, max_wal_bytes=current_wal_size + 10000)

        # If WAL size is >= max_wal_bytes, checkpoint is performed and WAL is truncated
        assert checkpoint_wal_if_needed(conn, db_path, max_wal_bytes=current_wal_size)
        assert wal_path.stat().st_size == 0


def test_get_db_connection_accepts_str_path(tmp_path):
    str_path = str(tmp_path / "str_path.db")
    with get_db_connection(str_path) as conn:
        limit = conn.execute("PRAGMA journal_size_limit;").fetchone()[0]
        assert limit == 67108864


def test_checkpoint_wal_if_needed_auto_resolves_db_path(tmp_path):
    db_path = tmp_path / "wal_auto.db"
    with get_db_connection(db_path) as conn:
        conn.execute("CREATE TABLE t (x TEXT);")
        conn.commit()
        conn.executemany("INSERT INTO t VALUES (?);", [(f"val-{i}",) for i in range(50)])
        conn.commit()

        wal_path = tmp_path / "wal_auto.db-wal"
        assert wal_path.exists()
        assert wal_path.stat().st_size > 0

        # db_path=None should auto-extract db path from conn
        assert checkpoint_wal_if_needed(conn, db_path=None, max_wal_bytes=1)
        assert wal_path.stat().st_size == 0


def test_checkpoint_wal_if_needed_handles_active_readers_busy(tmp_path):
    db_path = tmp_path / "wal_busy.db"
    conn1 = get_db_connection(db_path)
    conn1.execute("CREATE TABLE t (x TEXT);")
    conn1.commit()
    conn1.execute("INSERT INTO t VALUES ('initial');")
    conn1.commit()

    # Open reader with active read transaction
    conn2 = get_db_connection(db_path)
    cur2 = conn2.cursor()
    cur2.execute("BEGIN")
    cur2.execute("SELECT * FROM t").fetchall()

    # conn1 writes more to generate uncheckpointed WAL frames
    conn1.execute("INSERT INTO t VALUES ('more');")
    conn1.commit()

    wal_path = tmp_path / "wal_busy.db-wal"
    assert wal_path.exists()
    assert wal_path.stat().st_size > 0

    # Reader blocks TRUNCATE checkpoint; must return False and not truncate
    assert not checkpoint_wal_if_needed(conn1, db_path, max_wal_bytes=1)
    assert wal_path.stat().st_size > 0

    # Reader finishes and closes
    conn2.commit()
    conn2.close()

    # Checkpoint now succeeds and truncates WAL
    assert checkpoint_wal_if_needed(conn1, db_path, max_wal_bytes=1)
    assert wal_path.stat().st_size == 0
    conn1.close()


def test_checkpoint_wal_if_needed_edge_cases():
    conn = sqlite3.connect(":memory:")
    # Memory DB has no WAL file; should return False without error
    assert not checkpoint_wal_if_needed(conn, db_path=":memory:", max_wal_bytes=1)
    assert not checkpoint_wal_if_needed(conn, db_path=None, max_wal_bytes=1)
    conn.close()
    # Closed connection should return False without error
    assert not checkpoint_wal_if_needed(conn, max_wal_bytes=1)




