#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Analytics Database Schema & Connection Helper
---------------------------------------------------------
Manages SQLite connection pooling, table schemas, WAL mode configuration,
and schema migrations for cross-conversation analytics.
"""

import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DB_PATH",
    "TABLES_SQL",
    "INDEXES_SQL",
    "get_db_connection",
    "init_db",
    "upsert_quota_state",
    "get_quota_state",
    "refresh_conversation_rollups",
    "ensure_rollups_synced",
    "invalidate_rollups_sync_cache",
]

DEFAULT_DB_PATH = Path(os.path.expanduser("~/.gemini/antigravity/analytics.db"))

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    project TEXT,
    model_name TEXT,
    models_used TEXT,
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
    invoking_step_index INTEGER,
    compaction_count INTEGER DEFAULT 0,
    pruned_tokens INTEGER DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
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
    billed_output_tokens INTEGER DEFAULT 0,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, step_index)
);

CREATE TABLE IF NOT EXISTS tool_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    mcp_server TEXT,
    mcp_tool TEXT,
    target_path TEXT,
    command_line TEXT,
    exit_code INTEGER,
    is_error INTEGER DEFAULT 0,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transcript_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    block_type TEXT NOT NULL,
    content TEXT NOT NULL,
    block_hash TEXT NOT NULL,
    call_id TEXT,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quota_state (
    bucket_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    window TEXT NOT NULL,
    remaining_fraction REAL NOT NULL,
    reset_time TEXT NOT NULL,
    calibrated_capacity REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hourly_step_rollups (
    hour_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    step_count INTEGER DEFAULT 0,
    est_tokens INTEGER DEFAULT 0,
    billed_tokens INTEGER DEFAULT 0,
    billed_cost REAL DEFAULT 0.0,
    PRIMARY KEY (hour_key, conversation_id)
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project);
CREATE INDEX IF NOT EXISTS idx_conversations_models_used ON conversations(models_used);
CREATE INDEX IF NOT EXISTS idx_conversations_parent ON conversations(parent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_root ON conversations(root_id);
CREATE INDEX IF NOT EXISTS idx_conversations_depth ON conversations(depth);
CREATE INDEX IF NOT EXISTS idx_steps_created ON steps(created_at);
CREATE INDEX IF NOT EXISTS idx_steps_conv_step ON steps(conversation_id, step_index);
CREATE INDEX IF NOT EXISTS idx_steps_category ON steps(category);
CREATE INDEX IF NOT EXISTS idx_steps_activity ON steps(activity);
CREATE INDEX IF NOT EXISTS idx_steps_phase ON steps(phase);
CREATE INDEX IF NOT EXISTS idx_steps_model ON steps(model_name);
CREATE INDEX IF NOT EXISTS idx_steps_cat_tokens ON steps(category, est_tokens);
CREATE INDEX IF NOT EXISTS idx_steps_model_tokens ON steps(model_name, est_tokens);
CREATE INDEX IF NOT EXISTS idx_steps_phase_tokens ON steps(phase, est_tokens);
CREATE INDEX IF NOT EXISTS idx_steps_est_tokens ON steps(est_tokens DESC);
CREATE INDEX IF NOT EXISTS idx_steps_model_billed ON steps(model_name, billed_input_tokens, billed_output_tokens, est_tokens);
CREATE INDEX IF NOT EXISTS idx_steps_created_cost ON steps(created_at, model_name, billed_cost);
CREATE INDEX IF NOT EXISTS idx_tool_exec_conv ON tool_executions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_tool_exec_name ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_server ON tool_executions(mcp_server);
CREATE INDEX IF NOT EXISTS idx_tool_exec_mcp_tool ON tool_executions(mcp_tool);
CREATE INDEX IF NOT EXISTS idx_tool_exec_path ON tool_executions(target_path);
CREATE INDEX IF NOT EXISTS idx_blocks_conv_hash ON transcript_blocks(conversation_id, block_hash);
CREATE INDEX IF NOT EXISTS idx_blocks_conv_step ON transcript_blocks(conversation_id, step_index);
CREATE INDEX IF NOT EXISTS idx_blocks_tools ON transcript_blocks(block_type) WHERE block_type = 'tools';
CREATE INDEX IF NOT EXISTS idx_rollups_conv ON hourly_step_rollups(conversation_id);
"""


def get_db_connection(
    db_path: Path = DEFAULT_DB_PATH, timeout: float = 30.0
) -> sqlite3.Connection:
    """Returns an initialized SQLite connection configured for concurrent WAL-mode access."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row

    # Configure WAL mode & concurrency safeguards
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA cache_size = -64000;")
    conn.execute("PRAGMA mmap_size = 268435456;")
    conn.execute("PRAGMA temp_store = MEMORY;")

    return conn


def init_db(db_or_conn: Path | str | sqlite3.Connection = DEFAULT_DB_PATH):
    """Initializes the database schema and performs automatic column migrations if needed.

    Accepts either a database file path (Path or str) or an existing sqlite3.Connection.
    """
    if isinstance(db_or_conn, sqlite3.Connection) or (
        hasattr(db_or_conn, "cursor") and not isinstance(db_or_conn, (str, Path))
    ):
        _init_db_conn(db_or_conn)
    else:
        path = Path(db_or_conn) if isinstance(db_or_conn, str) else db_or_conn
        with get_db_connection(path) as conn:
            _init_db_conn(conn)


def _init_db_conn(conn: sqlite3.Connection) -> None:
    """Executes schema initialization and migrations on the provided connection."""
    conn.executescript(TABLES_SQL)
    cur = conn.cursor()

    # Conversations column migrations
    conv_cols = [
        c[1] for c in cur.execute("PRAGMA table_info(conversations)").fetchall()
    ]
    conv_new_cols = [
        ("project", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("total_steps", "INTEGER DEFAULT 0"),
        ("total_characters", "INTEGER DEFAULT 0"),
        ("total_tokens", "INTEGER DEFAULT 0"),
        ("duration_seconds", "REAL DEFAULT 0.0"),
        ("active_agent_seconds", "REAL DEFAULT 0.0"),
        ("active_pairing_seconds", "REAL DEFAULT 0.0"),
        ("thinking_tokens", "INTEGER DEFAULT 0"),
        ("user_prompts", "INTEGER DEFAULT 0"),
        ("tool_calls_count", "INTEGER DEFAULT 0"),
        ("redundant_file_reads", "INTEGER DEFAULT 0"),
        ("lines_written", "INTEGER DEFAULT 0"),
        ("lines_edited", "INTEGER DEFAULT 0"),
        ("failed_tool_calls", "INTEGER DEFAULT 0"),
        ("billed_cost", "REAL DEFAULT 0.0"),
        ("billed_input_tokens", "INTEGER DEFAULT 0"),
        ("billed_output_tokens", "INTEGER DEFAULT 0"),
        ("parent_id", "TEXT"),
        ("root_id", "TEXT"),
        ("depth", "INTEGER DEFAULT 0"),
        ("subagent_role", "TEXT"),
        ("subagent_type", "TEXT"),
        ("invoking_step_index", "INTEGER"),
        ("compaction_count", "INTEGER DEFAULT 0"),
        ("pruned_tokens", "INTEGER DEFAULT 0"),
        ("models_used", "TEXT"),
    ]
    for col, ctype in conv_new_cols:
        if col not in conv_cols:
            try:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {ctype};")
            except Exception:
                pass

    # Steps column migrations
    step_cols = [c[1] for c in cur.execute("PRAGMA table_info(steps)").fetchall()]
    step_new_cols = [
        ("thinking_tokens", "INTEGER DEFAULT 0"),
        ("phase", "TEXT"),
        ("model_name", "TEXT"),
        ("billed_cost", "REAL DEFAULT 0.0"),
        ("billed_input_tokens", "INTEGER DEFAULT 0"),
        ("billed_output_tokens", "INTEGER DEFAULT 0"),
    ]
    for col, ctype in step_new_cols:
        if col not in step_cols:
            try:
                conn.execute(f"ALTER TABLE steps ADD COLUMN {col} {ctype};")
            except Exception:
                pass

    # Tool executions column migrations
    tool_cols = [
        c[1] for c in cur.execute("PRAGMA table_info(tool_executions)").fetchall()
    ]
    tool_new_cols = [("mcp_server", "TEXT"), ("mcp_tool", "TEXT")]
    for col, ctype in tool_new_cols:
        if col not in tool_cols:
            try:
                conn.execute(f"ALTER TABLE tool_executions ADD COLUMN {col} {ctype};")
            except Exception:
                pass

    # Quota state column migrations
    quota_cols = [
        c[1] for c in cur.execute("PRAGMA table_info(quota_state)").fetchall()
    ]
    quota_new_cols = [
        ("group_name", "TEXT NOT NULL DEFAULT ''"),
        ("window", "TEXT NOT NULL DEFAULT ''"),
        ("remaining_fraction", "REAL NOT NULL DEFAULT 1.0"),
        ("reset_time", "TEXT NOT NULL DEFAULT ''"),
        ("calibrated_capacity", "REAL NOT NULL DEFAULT 0.0"),
        ("fetched_at", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, ctype in quota_new_cols:
        if col not in quota_cols:
            try:
                conn.execute(f"ALTER TABLE quota_state ADD COLUMN {col} {ctype};")
            except Exception:
                pass

    # Backfill any steps with NULL model_name from parent conversation
    try:
        conn.execute("""
            UPDATE steps SET model_name = (
                SELECT COALESCE(conversations.model_name, 'Gemini Flash')
                FROM conversations WHERE conversations.id = steps.conversation_id
            ) WHERE model_name IS NULL;
        """)
    except Exception:
        pass

    # Backfill models_used for existing conversations if NULL
    try:
        conn.execute("""
            UPDATE conversations
            SET models_used = COALESCE(
                (SELECT group_concat(m, ', ') FROM (SELECT DISTINCT model_name AS m FROM steps WHERE conversation_id = conversations.id AND model_name IS NOT NULL ORDER BY step_index ASC)),
                model_name,
                'Gemini Flash'
            )
            WHERE models_used IS NULL;
        """)
    except Exception:
        pass

    # Now create indexes after migrations ensure columns exist
    try:
        conn.executescript(INDEXES_SQL)
    except Exception:
        for stmt in INDEXES_SQL.strip().split(";"):
            if stmt.strip():
                try:
                    conn.execute(stmt)
                except Exception:
                    pass
    conn.commit()

    # Clean up legacy block_type = 'tools' rows in transcript_blocks if present
    try:
        user_ver = cur.execute("PRAGMA user_version;").fetchone()[0]
        if user_ver < 2:
            has_blocks = (
                cur.execute("SELECT 1 FROM transcript_blocks LIMIT 1").fetchone()
                is not None
            )
            if has_blocks:
                cur.execute(
                    "SELECT 1 FROM transcript_blocks WHERE block_type = 'tools' LIMIT 1"
                )
                if cur.fetchone() is not None:
                    conn.execute("DELETE FROM transcript_blocks WHERE block_type = 'tools'")
                    conn.commit()
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except Exception:
                        pass
                conn.execute("PRAGMA user_version = 2;")
                conn.commit()
    except Exception:
        pass

    # Ensure hourly_step_rollups is in sync with steps
    try:
        ensure_rollups_synced(conn)
    except Exception:
        pass


def upsert_quota_state(conn: sqlite3.Connection, quota_rows: list[dict] | dict) -> None:
    """Upserts quota state records into the quota_state table.

    Supports both snake_case and Language Server ConnectRPC camelCase keys.
    Gracefully handles None, omitted fields, and type conversions.

    Args:
        conn: An open SQLite connection.
        quota_rows: A list of dicts (or a single dict) containing quota state fields:
            bucket_id, group_name, window, remaining_fraction, reset_time,
            calibrated_capacity, fetched_at.
    """
    if not quota_rows:
        return

    if isinstance(quota_rows, dict):
        quota_rows = [quota_rows]

    sql = """
    INSERT INTO quota_state (
        bucket_id,
        group_name,
        window,
        remaining_fraction,
        reset_time,
        calibrated_capacity,
        fetched_at
    ) VALUES (
        :bucket_id,
        :group_name,
        :window,
        :remaining_fraction,
        :reset_time,
        :calibrated_capacity,
        :fetched_at
    )
    ON CONFLICT(bucket_id) DO UPDATE SET
        group_name = excluded.group_name,
        window = excluded.window,
        remaining_fraction = excluded.remaining_fraction,
        reset_time = excluded.reset_time,
        calibrated_capacity = excluded.calibrated_capacity,
        fetched_at = excluded.fetched_at;
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        for row in quota_rows:
            raw_bid = row.get("bucket_id")
            if raw_bid is None:
                raw_bid = row.get("bucketId")
            if not raw_bid or not str(raw_bid).strip():
                raise ValueError(
                    "bucket_id is required and cannot be empty for quota_state upsert"
                )
            bucket_id = str(raw_bid).strip()

            raw_grp = row.get("group_name")
            if raw_grp is None:
                raw_grp = row.get("groupName") or row.get("displayName")
            group_name = str(raw_grp) if raw_grp is not None else ""

            raw_win = row.get("window")
            window = str(raw_win) if raw_win is not None else ""

            raw_rem = row.get("remaining_fraction")
            if raw_rem is None:
                raw_rem = row.get("remainingFraction")
            try:
                remaining_fraction = float(raw_rem) if raw_rem is not None else 1.0
                if math.isnan(remaining_fraction) or math.isinf(remaining_fraction):
                    remaining_fraction = 1.0
            except (ValueError, TypeError):
                remaining_fraction = 1.0

            raw_reset = row.get("reset_time")
            if raw_reset is None:
                raw_reset = row.get("resetTime")
            reset_time = str(raw_reset) if raw_reset is not None else ""

            raw_cap = row.get("calibrated_capacity")
            if raw_cap is None:
                raw_cap = row.get("calibratedCapacity")
            try:
                calibrated_capacity = float(raw_cap) if raw_cap is not None else 0.0
                if math.isnan(calibrated_capacity) or math.isinf(calibrated_capacity):
                    calibrated_capacity = 0.0
            except (ValueError, TypeError):
                calibrated_capacity = 0.0

            raw_fetched = row.get("fetched_at")
            if raw_fetched is None:
                raw_fetched = (
                    row.get("fetchedAt")
                    or row.get("updated_at")
                    or row.get("updatedAt")
                )
            fetched_at = str(raw_fetched) if raw_fetched else now_iso

            conn.execute(
                sql,
                {
                    "bucket_id": bucket_id,
                    "group_name": group_name,
                    "window": window,
                    "remaining_fraction": remaining_fraction,
                    "reset_time": reset_time,
                    "calibrated_capacity": calibrated_capacity,
                    "fetched_at": fetched_at,
                },
            )


def get_quota_state(
    conn: sqlite3.Connection, bucket_id: str | None = None
) -> list[dict]:
    """Retrieves cached quota state records as a list of dictionaries.

    Args:
        conn: An open SQLite connection.
        bucket_id: Optional bucket_id to filter by.

    Returns:
        List of dictionaries with keys:
        bucket_id, group_name, window, remaining_fraction, reset_time,
        calibrated_capacity, fetched_at, updated_at.
    """
    if bucket_id:
        cursor = conn.execute(
            "SELECT bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at "
            "FROM quota_state WHERE bucket_id = ? ORDER BY bucket_id ASC",
            (bucket_id,),
        )
    else:
        cursor = conn.execute(
            "SELECT bucket_id, group_name, window, remaining_fraction, reset_time, calibrated_capacity, fetched_at "
            "FROM quota_state ORDER BY bucket_id ASC"
        )
    rows = cursor.fetchall()
    result = []
    for r in rows:
        row_dict = (
            dict(r)
            if isinstance(r, sqlite3.Row)
            else {
                "bucket_id": r[0],
                "group_name": r[1],
                "window": r[2],
                "remaining_fraction": float(r[3]),
                "reset_time": r[4],
                "calibrated_capacity": float(r[5]),
                "fetched_at": r[6],
            }
        )
        # Provide updated_at alias for bidirectional compatibility
        if "updated_at" not in row_dict:
            row_dict["updated_at"] = row_dict.get("fetched_at", "")
        result.append(row_dict)
    return result


def refresh_conversation_rollups(
    conn: sqlite3.Connection, conversation_id: str
) -> None:
    """Recomputes and updates hourly_step_rollups for a specific conversation."""
    was_in_transaction = getattr(conn, "in_transaction", False)
    conn.execute(
        "DELETE FROM hourly_step_rollups WHERE conversation_id = ?",
        (conversation_id,),
    )
    conn.execute(
        """
        INSERT INTO hourly_step_rollups (
            hour_key, conversation_id, step_count, est_tokens, billed_tokens, billed_cost
        )
        SELECT 
            substr(replace(created_at, ' ', 'T'), 1, 13) AS hour_key,
            conversation_id,
            COUNT(*) AS step_count,
            COALESCE(SUM(est_tokens), 0) AS est_tokens,
            COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS billed_tokens,
            COALESCE(SUM(COALESCE(billed_cost, 0.0)), 0.0) AS billed_cost
        FROM steps
        WHERE conversation_id = ? AND created_at IS NOT NULL AND created_at != ''
        GROUP BY hour_key, conversation_id
        """,
        (conversation_id,),
    )
    if not was_in_transaction:
        conn.commit()


_ROLLUP_SYNC_LOCK = threading.Lock()
_LAST_SYNCED_STATE: dict[Any, tuple[int | None, int]] = {}


def _extract_db_path(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("PRAGMA database_list;").fetchall()
        for row in rows:
            name = row[1] if len(row) > 1 else ""
            file = row[2] if len(row) > 2 else ""
            if name == "main" and file:
                return str(Path(file).resolve())
    except Exception:
        pass
    return None


def invalidate_rollups_sync_cache() -> None:
    """Invalidates the cached rollups sync states across connections."""
    with _ROLLUP_SYNC_LOCK:
        _LAST_SYNCED_STATE.clear()


def _rebuild_hourly_rollups(conn: sqlite3.Connection) -> None:
    was_in_transaction = getattr(conn, "in_transaction", False)
    conn.execute("DELETE FROM hourly_step_rollups")
    conn.execute(
        """
        INSERT INTO hourly_step_rollups (
            hour_key, conversation_id, step_count, est_tokens, billed_tokens, billed_cost
        )
        SELECT 
            substr(replace(created_at, ' ', 'T'), 1, 13) AS hour_key,
            conversation_id,
            COUNT(*) AS step_count,
            COALESCE(SUM(est_tokens), 0) AS est_tokens,
            COALESCE(SUM(COALESCE(billed_input_tokens, 0) + COALESCE(billed_output_tokens, 0)), 0) AS billed_tokens,
            COALESCE(SUM(COALESCE(billed_cost, 0.0)), 0.0) AS billed_cost
        FROM steps
        WHERE created_at IS NOT NULL AND created_at != ''
        GROUP BY hour_key, conversation_id
        """
    )
    if not was_in_transaction:
        conn.commit()


def _get_db_sync_state(conn: sqlite3.Connection) -> tuple[Any, Any]:
    db_path = _extract_db_path(conn)
    if db_path:
        try:
            st = Path(db_path).stat()
            mtime = st.st_mtime_ns
            size = st.st_size
        except OSError:
            mtime = size = 0
        try:
            st_wal = Path(f"{db_path}-wal").stat()
            wal_mtime = st_wal.st_mtime_ns
            wal_size = st_wal.st_size
        except OSError:
            wal_mtime = wal_size = 0
        return (db_path, (mtime, size, wal_mtime, wal_size))
    else:
        tc = getattr(conn, "total_changes", 0)
        return (id(conn), tc)


def _record_rollup_synced(conn: sqlite3.Connection) -> None:
    cache_key, state = _get_db_sync_state(conn)
    with _ROLLUP_SYNC_LOCK:
        _LAST_SYNCED_STATE[cache_key] = state


def ensure_rollups_synced(conn: sqlite3.Connection, force: bool = False) -> None:
    """Verifies that hourly_step_rollups matches the steps table and rebuilds if out of sync."""
    cur = conn.cursor()

    # 1. Fast check: is hourly_step_rollups empty?
    has_rollups = (
        cur.execute("SELECT 1 FROM hourly_step_rollups LIMIT 1").fetchone() is not None
    )
    if not has_rollups:
        # Check if steps table has data; if empty, nothing to build
        has_steps = (
            cur.execute(
                "SELECT 1 FROM steps WHERE created_at IS NOT NULL AND created_at != '' LIMIT 1"
            ).fetchone()
            is not None
        )
        if not has_steps:
            return
        # Initial build
        _rebuild_hourly_rollups(conn)
        _record_rollup_synced(conn)
        return

    # 2. If rollups exist, avoid 124ms full table scan across 672k rows if database is unchanged
    in_tx = getattr(conn, "in_transaction", False)
    if not force and not in_tx:
        cache_key, state = _get_db_sync_state(conn)
        with _ROLLUP_SYNC_LOCK:
            last_state = _LAST_SYNCED_STATE.get(cache_key)
            if last_state is not None and last_state == state:
                return

    # 3. Check whether steps and hourly_step_rollups are in sync
    step_stats = cur.execute(
        """
        SELECT 
            COUNT(*), 
            COALESCE(SUM(est_tokens), 0), 
            COALESCE(SUM(COALESCE(billed_cost, 0.0)), 0.0)
        FROM steps 
        WHERE created_at IS NOT NULL AND created_at != ''
        """
    ).fetchone()
    step_cnt = step_stats[0] if step_stats else 0
    step_tokens = step_stats[1] if step_stats else 0
    step_cost = step_stats[2] if step_stats else 0.0

    rollup_stats = cur.execute(
        """
        SELECT 
            COALESCE(SUM(step_count), 0), 
            COALESCE(SUM(est_tokens), 0), 
            COALESCE(SUM(billed_cost), 0.0)
        FROM hourly_step_rollups
        """
    ).fetchone()
    rollup_cnt = rollup_stats[0] if rollup_stats else 0
    rollup_tokens = rollup_stats[1] if rollup_stats else 0
    rollup_cost = rollup_stats[2] if rollup_stats else 0.0

    if (
        step_cnt != rollup_cnt
        or step_tokens != rollup_tokens
        or abs(step_cost - rollup_cost) > 1e-4
    ):
        _rebuild_hourly_rollups(conn)

    if not in_tx:
        _record_rollup_synced(conn)


if __name__ == "__main__":
    init_db()
    print(f"Initialized Antigravity analytics database at: {DEFAULT_DB_PATH}")
