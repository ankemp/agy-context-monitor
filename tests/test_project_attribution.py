import sqlite3
import pytest
from context_monitor.db import get_db_connection, init_db
from context_monitor.project import (
    backfill_db_projects,
    extract_project_from_text,
    extract_project_from_tool_args,
    normalize_project_name,
)


def test_normalize_project_name_strips_trailing_punctuation():
    assert normalize_project_name("ledger.") == "ledger"
    assert normalize_project_name("ledger,") == "ledger"
    assert normalize_project_name("agy-context-monitor)") == "agy-context-monitor"
    assert normalize_project_name("  \"healer-roguelite\".. ") == "healer-roguelite"
    assert normalize_project_name("") is None
    assert normalize_project_name("...") is None
    assert normalize_project_name(None) is None
    assert normalize_project_name("   ") is None
    assert normalize_project_name("`vinny-companion`") == "vinny-companion"
    assert normalize_project_name("'poc-hosting':") == "poc-hosting"
    assert normalize_project_name("(unassigned)") is None


def test_extract_project_from_text_variants():
    # Worktree path
    assert (
        extract_project_from_text(
            "/Users/akemp/.gemini/antigravity/worktrees/ledger/research_plan_soft_1501/src/app.py"
        )
        == "ledger"
    )
    # Escaped JSON worktree path
    assert (
        extract_project_from_text(
            '{"Path": "\\"/Users/akemp/.gemini/antigravity/worktrees/poc-hosting/branch/index.ts\\""}'
        )
        == "poc-hosting"
    )
    # Case-insensitive workspace path & trailing dot in prose
    assert (
        extract_project_from_text(" Checked /Users/akemp/workspace/ledger. ")
        == "ledger"
    )
    # <user_information> Active Workspaces
    user_info = """<user_information>
Active Workspaces:
- /Users/akemp/Workspace/agy-context-monitor
</user_information>"""
    assert extract_project_from_text(user_info) == "agy-context-monitor"
    # Legacy arrow syntax in <user_information>
    legacy_info = "<user_information>\n/Users/akemp/Workspace/vinny-companion -> /Users/akemp/Workspace/vinny-companion\n</user_information>"
    assert extract_project_from_text(legacy_info) == "vinny-companion"
    # Non-workspace global path returns None
    assert (
        extract_project_from_text("/Users/akemp/.gemini/antigravity/builtin/skills/SKILL.md")
        is None
    )
    # Windows style workspace path
    assert (
        extract_project_from_text(r"C:\Users\akemp\Workspace\healer-roguelite\package.json")
        == "healer-roguelite"
    )
    # Markdown link with trailing parenthesis
    assert (
        extract_project_from_text("(file:///Users/akemp/Workspace/ledger/README.md)")
        == "ledger"
    )
    # Empty and None
    assert extract_project_from_text("") is None
    assert extract_project_from_text(None) is None


def test_extract_project_from_tool_args():
    assert (
        extract_project_from_tool_args(
            {"Path": '"/Users/akemp/.gemini/antigravity/worktrees/ledger/branch/foo.py"'}
        )
        == "ledger"
    )
    assert (
        extract_project_from_tool_args(
            {"CommandLine": "cd /Users/akemp/Workspace/healer-roguelite && pytest"}
        )
        == "healer-roguelite"
    )
    assert (
        extract_project_from_tool_args(
            {"workspaceUris": ["file:///Users/akemp/Workspace/agy-context-monitor"]}
        )
        == "agy-context-monitor"
    )
    assert extract_project_from_tool_args({}) is None
    assert extract_project_from_tool_args(None) is None


def test_backfill_db_projects_recovers_historical_and_swarm(tmp_path):
    db_file = tmp_path / "test_proj_backfill.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # 1. Conversation with trailing dot 'ledger.'
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('c-dot', 'Dot', 'ledger.', 'c-dot', 0)"
    )
    # 2. Historical conversation (no brain dir) with worktree path in tool_executions
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('c-hist-tool', 'Hist', NULL, 'c-hist-tool', 0)"
    )
    conn.execute(
        "INSERT INTO tool_executions (conversation_id, step_index, tool_name, target_path) VALUES ('c-hist-tool', 1, 'read_file', '/Users/akemp/.gemini/antigravity/worktrees/ledger/feat/a.py')"
    )
    # 3. Swarm where child has project in transcript_blocks, root and sibling have NULL
    conn.execute(
        "INSERT INTO conversations (id, title, project, parent_id, root_id, depth) VALUES ('c-root', 'Root', NULL, NULL, 'c-root', 0)"
    )
    conn.execute(
        "INSERT INTO conversations (id, title, project, parent_id, root_id, depth) VALUES ('c-child1', 'Child 1', NULL, 'c-root', 'c-root', 1)"
    )
    conn.execute(
        "INSERT INTO conversations (id, title, project, parent_id, root_id, depth) VALUES ('c-child2', 'Child 2', NULL, 'c-child1', 'c-root', 2)"
    )
    conn.execute(
        "INSERT INTO transcript_blocks (conversation_id, step_index, block_type, content, block_hash) VALUES ('c-child2', 1, 'user', 'Inspect /Users/akemp/Workspace/vinny-companion/package.json', 'h1')"
    )
    conn.commit()

    updated = backfill_db_projects(conn)
    assert updated >= 5

    rows = dict(conn.execute("SELECT id, project FROM conversations").fetchall())
    conn.close()

    assert rows["c-dot"] == "ledger"
    assert rows["c-hist-tool"] == "ledger"
    assert rows["c-root"] == "vinny-companion"
    assert rows["c-child1"] == "vinny-companion"
    assert rows["c-child2"] == "vinny-companion"


def test_analyze_transcript_and_parse_conversation_worktree_and_generic(tmp_path):
    import json
    from pathlib import Path
    from scripts.ingest import analyze_transcript
    from scripts.backfill_analytics import parse_conversation

    # 1. GENERIC step with workspace path and trailing dot
    t1_dir = tmp_path / "conv_generic"
    t1_dir.mkdir()
    t1_file = t1_dir / "transcript.jsonl"
    t1_file.write_text(
        json.dumps({"step_index": 1, "type": "GENERIC", "content": "Checked /Users/akemp/workspace/ledger."}) + "\n",
        encoding="utf-8",
    )
    res_ingest1 = analyze_transcript(t1_file)
    assert res_ingest1["project"] == "ledger"
    res_backfill1 = parse_conversation(t1_dir)
    assert res_backfill1["conversation"]["project"] == "ledger"

    # 2. Tool call with worktree path
    t2_dir = tmp_path / "conv_worktree"
    t2_dir.mkdir()
    t2_file = t2_dir / "transcript.jsonl"
    t2_file.write_text(
        json.dumps({
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {
                    "name": "read_file",
                    "args": {"Path": "/Users/akemp/.gemini/antigravity/worktrees/ledger/feature/test.py"},
                }
            ],
        }) + "\n",
        encoding="utf-8",
    )
    res_ingest2 = analyze_transcript(t2_file)
    assert res_ingest2["project"] == "ledger"
    res_backfill2 = parse_conversation(t2_dir)
    assert res_backfill2["conversation"]["project"] == "ledger"


def test_sync_to_sqlite_swarm_bidirectional_propagation(tmp_path):
    from pathlib import Path
    from scripts.ingest import sync_to_sqlite

    db_file = tmp_path / "test_sync_swarm.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Insert root with no project
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('root-1', 'Root Conv', NULL, 'root-1', 0)"
    )
    conn.commit()
    conn.close()

    # Child discovers project
    child_analysis = {
        "title": "Child Worker",
        "project": "ledger",
        "parent_id": "root-1",
        "steps": [],
        "models_used": ["model-a"],
        "total_chars": 100,
        "total_tokens": 25,
        "duration_seconds": 10.0,
        "start_time": None,
        "end_time": None,
    }
    sync_to_sqlite("child-1", "model-a", child_analysis, db_path=db_file)

    conn = get_db_connection(db_file)
    root_proj = conn.execute("SELECT project FROM conversations WHERE id = 'root-1'").fetchone()[0]
    assert root_proj == "ledger"

    # Grandchild with no project inherits from root_id
    grandchild_analysis = {
        "title": "Grandchild Worker",
        "project": None,
        "parent_id": "child-1",
        "steps": [],
        "models_used": ["model-a"],
        "total_chars": 50,
        "total_tokens": 12,
        "duration_seconds": 5.0,
        "start_time": None,
        "end_time": None,
    }
    conn.close()
    sync_to_sqlite("grandchild-1", "model-a", grandchild_analysis, db_path=db_file)

    conn = get_db_connection(db_file)
    gc_proj = conn.execute("SELECT project FROM conversations WHERE id = 'grandchild-1'").fetchone()[0]
    assert gc_proj == "ledger"
    conn.close()


def test_run_backfill_preserves_historical_and_propagates_swarms(tmp_path):
    from pathlib import Path
    import json
    from scripts.backfill_analytics import run_backfill

    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    db_file = tmp_path / "test_backfill_swarm.db"
    init_db(db_file)

    # 1. Historical root conversation in DB without a brain directory
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('hist-root', 'Historical Root', NULL, 'hist-root', 0)"
    )
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('hist-isolated', 'Isolated Conv', 'saved-proj', 'hist-isolated', 0)"
    )
    conn.commit()
    conn.close()

    # 2. Child conversation in brain directory that has project and parent_id = 'hist-root'
    child_dir = brain_dir / "child-subagent"
    child_dir.mkdir()
    (child_dir / "transcript.jsonl").write_text(
        json.dumps({
            "step_index": 1,
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-10-21T01:00:00Z sender=hist-root\nCoordinate task",
        }) + "\n" +
        json.dumps({
            "step_index": 2,
            "type": "USER_INPUT",
            "content": "Working on /Users/akemp/Workspace/vinny-companion/src",
        }) + "\n" +
        json.dumps({
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "read_file", "args": {"Path": "/Users/akemp/Workspace/vinny-companion/README.md"}}],
        }) + "\n",
        encoding="utf-8",
    )

    run_backfill(brain_dir=brain_dir, db_path=db_file)

    conn = get_db_connection(db_file)
    rows = dict(conn.execute("SELECT id, project FROM conversations").fetchall())
    conn.close()

    assert rows["hist-isolated"] == "saved-proj"
    assert rows["child-subagent"] == "vinny-companion"
    assert rows["hist-root"] == "vinny-companion"


def test_extract_project_from_text_modern_user_info_and_containers():
    # 1. Modern Active Workspaces with non-Workspace parent dir
    user_info = """<user_information>
Active Workspaces:
- /Users/akemp/Projects/web-frontend
Command Working Directory: /Users/akemp/Projects/web-frontend
</user_information>"""
    assert extract_project_from_text(user_info) == "web-frontend"

    # 2. Command Working Directory fallback
    cwd_info = """<user_information>
The USER's OS version is mac.
Command Working Directory: /home/ubuntu/repos/backend-service
</user_information>"""
    assert extract_project_from_text(cwd_info) == "backend-service"

    # 3. Generic home directory does not produce user's name as project
    home_info = """<user_information>
Command Working Directory: /Users/akemp
</user_information>"""
    assert extract_project_from_text(home_info) is None

    # 4. Containers and Codespaces (/workspace and /workspaces)
    assert extract_project_from_text("/workspaces/my-cool-app/src/index.ts") == "my-cool-app"
    assert extract_project_from_text("/workspace/cloud-api/main.go") == "cloud-api"
    assert extract_project_from_text("file:///workspaces/my-cool-app") == "my-cool-app"


def test_extract_project_from_tool_args_search_directory():
    assert (
        extract_project_from_tool_args({"SearchDirectory": "/workspaces/my-cool-app"})
        == "my-cool-app"
    )
    assert (
        extract_project_from_tool_args({"search_directory": "/Users/akemp/Workspace/ledger"})
        == "ledger"
    )


def test_backfill_db_projects_row_factory_safety_and_unassigned_cleanup():
    conn = sqlite3.connect(":memory:")
    # Ensure default row_factory is None
    assert conn.row_factory is None
    conn.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, project TEXT, parent_id TEXT, root_id TEXT, depth INTEGER)"
    )
    conn.execute(
        "CREATE TABLE tool_executions (id INTEGER PRIMARY KEY, conversation_id TEXT, target_path TEXT, command_line TEXT)"
    )
    # c-recoverable has literal '(unassigned)' and tool execution
    conn.execute(
        "INSERT INTO conversations VALUES ('c-recoverable', 'Conv 1', '(unassigned)', NULL, 'c-recoverable', 0)"
    )
    conn.execute(
        "INSERT INTO tool_executions (conversation_id, target_path) VALUES ('c-recoverable', '/Users/akemp/Workspace/ledger/main.py')"
    )
    # c-unrecoverable has literal '(unassigned)' and no paths
    conn.execute(
        "INSERT INTO conversations VALUES ('c-unrecoverable', 'Conv 2', '(unassigned)', NULL, 'c-unrecoverable', 0)"
    )
    conn.commit()

    updated = backfill_db_projects(conn)
    assert updated == 2
    # Ensure conn.row_factory was restored to None
    assert conn.row_factory is None

    rows = dict(conn.execute("SELECT id, project FROM conversations").fetchall())
    assert rows["c-recoverable"] == "ledger"
    assert rows["c-unrecoverable"] is None


def test_init_db_sets_user_version_3_when_conversations_exist(tmp_path):
    db_file = tmp_path / "conv_test.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, project, root_id, depth) VALUES ('c1', 'Title', 'ledger.', 'c1', 0)"
    )
    conn.commit()

    # Re-running init_db triggers _init_db_conn migration to v3
    init_db(conn)
    ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    proj = conn.execute("SELECT project FROM conversations WHERE id = 'c1'").fetchone()["project"]
    conn.close()
    assert ver == 3
    assert proj == "ledger"


def test_swarm_root_priority_over_child_project(tmp_path):
    from scripts.backfill_analytics import run_backfill
    import json

    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    db_file = tmp_path / "test_swarm_priority.db"
    init_db(db_file)

    # Root has project 'root-app'
    root_dir = brain_dir / "root-conv"
    root_dir.mkdir()
    (root_dir / "transcript.jsonl").write_text(
        json.dumps({
            "step_index": 1,
            "type": "USER_INPUT",
            "content": "Working in /Users/akemp/Workspace/root-app",
        }) + "\n",
        encoding="utf-8",
    )

    # Child 1 has project 'child-app'
    child1_dir = brain_dir / "child-1"
    child1_dir.mkdir()
    (child1_dir / "transcript.jsonl").write_text(
        json.dumps({
            "step_index": 1,
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-10-21T01:00:00Z sender=root-conv\nSubtask",
        }) + "\n" +
        json.dumps({
            "step_index": 2,
            "type": "USER_INPUT",
            "content": "Working in /Users/akemp/Workspace/child-app",
        }) + "\n",
        encoding="utf-8",
    )

    # Child 2 has NO project, should inherit root's 'root-app'
    child2_dir = brain_dir / "child-2"
    child2_dir.mkdir()
    (child2_dir / "transcript.jsonl").write_text(
        json.dumps({
            "step_index": 1,
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-10-21T01:00:00Z sender=root-conv\nGeneric subtask",
        }) + "\n",
        encoding="utf-8",
    )

    run_backfill(brain_dir=brain_dir, db_path=db_file)

    conn = get_db_connection(db_file)
    rows = dict(conn.execute("SELECT id, project FROM conversations").fetchall())
    conn.close()

    assert rows["root-conv"] == "root-app"
    assert rows["child-1"] == "child-app"
    # Child 2 inherits root's canonical project 'root-app'
    assert rows["child-2"] == "root-app"
