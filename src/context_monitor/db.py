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
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "DEFAULT_DB_PATH",
    "TABLES_SQL",
    "INDEXES_SQL",
    "get_db_connection",
    "init_db",
    "upsert_quota_state",
    "get_quota_state",
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
CREATE INDEX IF NOT EXISTS idx_tool_exec_conv ON tool_executions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_tool_exec_name ON tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_server ON tool_executions(mcp_server);
CREATE INDEX IF NOT EXISTS idx_tool_exec_mcp_tool ON tool_executions(mcp_tool);
CREATE INDEX IF NOT EXISTS idx_tool_exec_path ON tool_executions(target_path);
CREATE INDEX IF NOT EXISTS idx_blocks_conv_hash ON transcript_blocks(conversation_id, block_hash);
CREATE INDEX IF NOT EXISTS idx_blocks_conv_step ON transcript_blocks(conversation_id, step_index);
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
    if isinstance(db_or_conn, sqlite3.Connection):
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


if __name__ == "__main__":
    init_db()
    print(f"Initialized Antigravity analytics database at: {DEFAULT_DB_PATH}")
