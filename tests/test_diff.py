import json
import sqlite3
import pytest
from context_monitor.db import TABLES_SQL, INDEXES_SQL
from context_monitor.formats import ContentBlock, BlockType
from context_monitor.diff import DiffResult, diff_block_lists, get_turn_diff_from_db, normalize_block
from context_monitor.dashboard.api_routes import query_turn_diff

def test_normalize_content_block():
    cb = ContentBlock(kind=BlockType.USER, text="Hello world", step_index=1)
    norm = normalize_block(cb)
    assert norm["kind"] == "user"
    assert norm["text"] == "Hello world"
    assert norm["step_index"] == 1
    assert norm["hash"] == cb.hash
    assert norm["est_tokens"] > 0

def test_normalize_dict():
    d = {
        "block_type": "tool_use",
        "content": '{"query": "SELECT 1"}',
        "step_index": 2,
        "call_id": "2_0",
        "block_hash": "abc123hash"
    }
    norm = normalize_block(d)
    assert norm["kind"] == "tool_use"
    assert norm["content"] == '{"query": "SELECT 1"}'
    assert norm["step_index"] == 2
    assert norm["hash"] == "abc123hash"

def test_diff_block_lists_added_only():
    b1 = ContentBlock(kind=BlockType.USER, text="Prompt 1", step_index=1)
    b2 = ContentBlock(kind=BlockType.ASSISTANT, text="Response 1", step_index=2)
    b3 = ContentBlock(kind=BlockType.USER, text="Prompt 2", step_index=3)

    list_a = [b1, b2]
    list_b = [b1, b2, b3]

    res = diff_block_lists(list_a, list_b)
    assert len(res.added) == 1
    assert res.added[0]["hash"] == b3.hash
    assert len(res.removed) == 0
    assert len(res.common) == 2
    assert res.compaction_detected is False
    assert res.net_token_delta > 0

def test_diff_block_lists_compaction_detected():
    b_sys = ContentBlock(kind=BlockType.SYSTEM, text="System rules", step_index=0)
    b_old1 = ContentBlock(kind=BlockType.USER, text="Old prompt 1", step_index=1)
    b_old2 = ContentBlock(kind=BlockType.ASSISTANT, text="Old response 1", step_index=2)
    b_new = ContentBlock(kind=BlockType.USER, text="New prompt", step_index=5)

    # In turn A, context has system + old1 + old2
    list_a = [b_sys, b_old1, b_old2]
    # In turn B, context was compacted: old1 and old2 were dropped, only system + new prompt remain
    list_b = [b_sys, b_new]

    res = diff_block_lists(list_a, list_b)
    assert len(res.added) == 1
    assert res.added[0]["hash"] == b_new.hash
    assert len(res.removed) == 2
    assert res.compaction_detected is True
    assert 1 in res.pruned_step_indices
    assert 2 in res.pruned_step_indices
    assert res.pruned_kinds["user"] == 1
    assert res.pruned_kinds["assistant"] == 1

def test_diff_block_lists_identical():
    b1 = ContentBlock(kind=BlockType.USER, text="Same prompt", step_index=1)
    list_a = [b1]
    list_b = [b1]

    res = diff_block_lists(list_a, list_b)
    assert len(res.added) == 0
    assert len(res.removed) == 0
    assert len(res.common) == 1
    assert res.net_token_delta == 0
    assert res.compaction_detected is False
    assert res.summary == "No change"

def test_diff_block_lists_empty():
    res = diff_block_lists([], [])
    assert len(res.added) == 0
    assert len(res.removed) == 0
    assert len(res.common) == 0
    assert res.net_token_delta == 0
    assert res.compaction_detected is False

@pytest.fixture
def in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(TABLES_SQL)
    conn.executescript(INDEXES_SQL)
    
    # Seed a sample conversation
    conn.execute("""
        INSERT INTO conversations (id, title, model_name, created_at, total_steps, total_tokens)
        VALUES ('conv-100', 'Test Turn Diff Session', 'Gemini 3.7 Flash', '2026-08-26T20:00:00Z', 3, 1500)
    """)

    # Seed steps
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, activity, tool_or_source, est_tokens)
        VALUES ('conv-100', 1, '2026-08-26T20:00:00Z', 'USER', 'USER_INPUT', 'User Prompt', 'user', 50)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, activity, tool_or_source, est_tokens)
        VALUES ('conv-100', 2, '2026-08-26T20:00:05Z', 'MODEL', 'PLANNER_RESPONSE', 'Tool Execution', 'view_file', 400)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, source, step_type, activity, tool_or_source, est_tokens)
        VALUES ('conv-100', 3, '2026-08-26T20:00:10Z', 'MODEL', 'PLANNER_RESPONSE', 'Assistant Response', 'assistant', 200)
    """)

    # Seed transcript blocks
    b1 = ContentBlock(kind=BlockType.USER, text="Show me src/diff.py", step_index=1)
    b2 = ContentBlock(kind=BlockType.TOOL_USE, text="view_file(path='src/diff.py')", step_index=2, call_id="2_0")
    b3 = ContentBlock(kind=BlockType.TOOL_RESULT, text="def diff_block_lists(): pass", step_index=2, call_id="2_0")
    b4 = ContentBlock(kind=BlockType.ASSISTANT, text="Here is the diff engine file.", step_index=3)

    for b in [b1, b2, b3, b4]:
        conn.execute("""
            INSERT INTO transcript_blocks (conversation_id, step_index, block_type, content, block_hash, call_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ('conv-100', b.step_index, b.kind.value, b.text, b.hash, b.call_id))

    conn.commit()
    yield conn
    conn.close()

def test_get_turn_diff_from_db_cumulative(in_memory_db):
    diff = get_turn_diff_from_db(in_memory_db, 'conv-100', 1, 2, cumulative=True)
    assert diff["conversation_id"] == "conv-100"
    assert diff["step_a"]["step_index"] == 1
    assert diff["step_b"]["step_index"] == 2
    assert diff["cumulative"] is True
    # Step 2 added tool_use and tool_result blocks
    assert diff["stats"]["added_count"] == 2
    assert diff["stats"]["removed_count"] == 0
    assert diff["stats"]["common_count"] == 1  # User prompt block retained
    assert diff["stats"]["compaction_detected"] is False

def test_get_turn_diff_from_db_discrete_step(in_memory_db):
    diff = get_turn_diff_from_db(in_memory_db, 'conv-100', 1, 3, cumulative=False)
    assert diff["cumulative"] is False
    assert diff["stats"]["added_count"] == 1  # Step 3 assistant block
    assert diff["stats"]["removed_count"] == 1  # Step 1 user block
    assert diff["stats"]["common_count"] == 0

def test_query_turn_diff_api(in_memory_db):
    res = query_turn_diff(in_memory_db, 'conv-100', 1, 3, cumulative=True)
    assert res is not None
    assert res["conversation_title"] == "Test Turn Diff Session"
    assert res["model_name"] == "Gemini 3.7 Flash"
    assert res["stats"]["added_count"] == 3  # Step 2 (2 blocks) + Step 3 (1 block)
    assert res["stats"]["common_count"] == 1

def test_query_turn_diff_not_found(in_memory_db):
    res = query_turn_diff(in_memory_db, 'non-existent-conv', 1, 2)
    assert res is None
