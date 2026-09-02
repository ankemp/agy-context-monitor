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

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.path.expanduser("~/.gemini/antigravity/analytics.db"))

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
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
    failed_tool_calls INTEGER DEFAULT 0
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
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project);
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

def get_db_connection(db_path: Path = DEFAULT_DB_PATH, timeout: float = 30.0) -> sqlite3.Connection:
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

def init_db(db_path: Path = DEFAULT_DB_PATH):
    """Initializes the database schema and performs automatic column migrations if needed."""
    with get_db_connection(db_path) as conn:
        conn.executescript(TABLES_SQL)
        cur = conn.cursor()
        
        # Conversations column migrations
        conv_cols = [c[1] for c in cur.execute("PRAGMA table_info(conversations)").fetchall()]
        conv_new_cols = [
            ("project", "TEXT"),
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
            ("billed_output_tokens", "INTEGER DEFAULT 0")
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
            ("billed_output_tokens", "INTEGER DEFAULT 0")
        ]
        for col, ctype in step_new_cols:
            if col not in step_cols:
                try:
                    conn.execute(f"ALTER TABLE steps ADD COLUMN {col} {ctype};")
                except Exception:
                    pass

        # Tool executions column migrations
        tool_cols = [c[1] for c in cur.execute("PRAGMA table_info(tool_executions)").fetchall()]
        tool_new_cols = [
            ("mcp_server", "TEXT"),
            ("mcp_tool", "TEXT")
        ]
        for col, ctype in tool_new_cols:
            if col not in tool_cols:
                try:
                    conn.execute(f"ALTER TABLE tool_executions ADD COLUMN {col} {ctype};")
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

        # Now create indexes after migrations ensure columns exist
        conn.executescript(INDEXES_SQL)
        conn.commit()

if __name__ == "__main__":
    init_db()
    print(f"Initialized Antigravity analytics database at: {DEFAULT_DB_PATH}")
