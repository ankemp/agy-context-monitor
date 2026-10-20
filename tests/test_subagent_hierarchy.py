import unittest
import tempfile
import json
from pathlib import Path

from context_monitor.db import get_db_connection, init_db
from context_monitor.dashboard.api_routes import (
    query_conversations_list,
    query_session_details,
    query_summary_data,
)
import scripts.ingest as ingest


class TestSubagentHierarchy(unittest.TestCase):
    def test_schema_lineage_columns_and_indexes(self):
        """Verify that conversations table has the required lineage columns and indexes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)

            conn = get_db_connection(db_file)
            cur = conn.cursor()

            # Verify column existence
            cols = {
                c[1] for c in cur.execute("PRAGMA table_info(conversations)").fetchall()
            }
            self.assertIn("parent_id", cols)
            self.assertIn("root_id", cols)
            self.assertIn("depth", cols)
            self.assertIn("subagent_role", cols)
            self.assertIn("subagent_type", cols)
            self.assertIn("invoking_step_index", cols)

            # Verify indexes
            indexes = {
                r[1]
                for r in cur.execute(
                    "SELECT type, name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("idx_conversations_parent", indexes)
            self.assertIn("idx_conversations_root", indexes)
            self.assertIn("idx_conversations_depth", indexes)
            conn.close()

    def test_analyze_transcript_lineage_bidirectional(self):
        """Test top-down and bottom-up lineage extraction from transcript jsonl."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # 1. Parent transcript with invoke_subagent and subagent creation
            parent_dir = tmp_path / "parent-123"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            parent_transcript = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Please implement feature X with subagents",
                    "created_at": "2026-10-06T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Launching coordinator subagent...",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Subagents": [
                                    {
                                        "Role": "DeepCoder Coordinator",
                                        "TypeName": "self",
                                        "Prompt": "Coordinate workers",
                                    }
                                ]
                            },
                        }
                    ],
                    "created_at": "2026-10-06T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": "Created the following subagents:\n- DeepCoder Coordinator (conversationId: child-456, type: self)\n\nNow communicate with them.",
                    "created_at": "2026-10-06T10:00:06Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in parent_transcript:
                    f.write(json.dumps(entry) + "\n")

            p_data = ingest.analyze_transcript(parent_dir)
            self.assertIsNotNone(p_data)
            self.assertEqual(len(p_data["dispatched_subagents"]), 1)
            dispatched = p_data["dispatched_subagents"][0]
            self.assertEqual(dispatched["id"], "child-456")
            self.assertEqual(dispatched["role"], "DeepCoder Coordinator")
            self.assertEqual(dispatched["type"], "self")
            self.assertEqual(dispatched["step_index"], 3)

            # 2. Child transcript receiving initial task message from parent
            child_dir = tmp_path / "child-456"
            child_logs = child_dir / ".system_generated" / "logs"
            child_logs.mkdir(parents=True)

            child_transcript = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-06T10:00:06Z sender=parent-123 recipient=child-456\nCoordinate workers for feature X",
                    "created_at": "2026-10-06T10:00:07Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Starting coordination...",
                    "created_at": "2026-10-06T10:00:10Z",
                },
            ]
            with open(child_logs / "transcript.jsonl", "w") as f:
                for entry in child_transcript:
                    f.write(json.dumps(entry) + "\n")

            c_data = ingest.analyze_transcript(child_dir)
            self.assertIsNotNone(c_data)
            self.assertEqual(c_data["parent_id"], "parent-123")

    def test_analyze_transcript_flat_and_wave_subagent_dispatch(self):
        """Verify Antigravity 2.0 flat invoke_subagent args (including JSON-quoted strings) and multi-call waves preserve each subagent role and type."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent_dir = Path(tmp_dir) / "parent-wave"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            transcript = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Run parallel wave review",
                    "created_at": "2026-10-19T22:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching two reviewers in parallel...",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Role": '"Standards Review Worker"',
                                "TypeName": '"DeepInvestigatorWorkerL0"',
                                "Model": '"inherit"',
                                "Prompt": '"Check coding standards"',
                            },
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Role": "Spec Review Worker",
                                "TypeName": "research",
                                "Model": "flash",
                                "Prompt": "Check spec compliance",
                            },
                        },
                    ],
                    "created_at": "2026-10-19T22:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "wave-child-1",\n  "logAbsoluteUri": "file:///tmp/wave-child-1"\n}',
                    "created_at": "2026-10-19T22:00:06Z",
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "wave-child-2",\n  "logAbsoluteUri": "file:///tmp/wave-child-2"\n}',
                    "created_at": "2026-10-19T22:00:07Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in transcript:
                    f.write(json.dumps(entry) + "\n")

            p_data = ingest.analyze_transcript(parent_dir)
            self.assertEqual(len(p_data["dispatched_subagents"]), 2)
            self.assertEqual(p_data["dispatched_subagents"][0]["id"], "wave-child-1")
            self.assertEqual(p_data["dispatched_subagents"][0]["role"], "Standards Review Worker")
            self.assertEqual(p_data["dispatched_subagents"][0]["type"], "DeepInvestigatorWorkerL0")
            self.assertEqual(p_data["dispatched_subagents"][1]["id"], "wave-child-2")
            self.assertEqual(p_data["dispatched_subagents"][1]["role"], "Spec Review Worker")
            self.assertEqual(p_data["dispatched_subagents"][1]["type"], "research")

    def test_analyze_transcript_manage_subagents_output(self):
        """Verify parsing manage_subagents tool output enriches or appends active subagents."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent_dir = Path(tmp_dir) / "parent-mgmt"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            transcript = [
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Checking active subagents...",
                    "tool_calls": [
                        {
                            "name": "manage_subagents",
                            "args": {"Action": "list"},
                        }
                    ],
                    "created_at": "2026-10-19T22:00:05Z",
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Found 2 active subagent(s):\n[\n  {"conversationId": "sub-101", "role": "\\"Tester\\"", "type": "qa-runner"},\n  {"conversationId": "sub-102", "role": "Architect", "type": "planner"}\n]',
                    "created_at": "2026-10-19T22:00:06Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in transcript:
                    f.write(json.dumps(entry) + "\n")

            p_data = ingest.analyze_transcript(parent_dir)
            self.assertEqual(len(p_data["dispatched_subagents"]), 2)
            self.assertEqual(p_data["dispatched_subagents"][0]["id"], "sub-101")
            self.assertEqual(p_data["dispatched_subagents"][0]["role"], "Tester")
            self.assertEqual(p_data["dispatched_subagents"][0]["type"], "qa-runner")
            self.assertEqual(p_data["dispatched_subagents"][1]["id"], "sub-102")
            self.assertEqual(p_data["dispatched_subagents"][1]["role"], "Architect")
            self.assertEqual(p_data["dispatched_subagents"][1]["type"], "planner")

    def test_conversations_hierarchy_and_flat_query(self):
        """Test query_conversations_list in hierarchy and flat modes with tree rollups."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at, total_steps, total_characters, total_tokens,
                    duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                    user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                    billed_cost, billed_input_tokens, billed_output_tokens,
                    parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
                ) VALUES 
                ('root-1', 'Root Campaign Task', 'my-repo', 'Gemini Pro', '2026-10-06T10:00:00Z', '2026-10-06T10:30:00Z', 10, 5000, 1000,
                 1800, 300, 400, 200, 5, 8, 0, 50, 10, 0, 2.00, 800, 200, NULL, 'root-1', 0, NULL, NULL, NULL),
                ('coord-1', '[Coordinator] Root Campaign Task', 'my-repo', 'Gemini Flash', '2026-10-06T10:05:00Z', '2026-10-06T10:20:00Z', 5, 2000, 500,
                 900, 150, 200, 100, 1, 4, 0, 0, 0, 0, 1.00, 400, 100, 'root-1', 'root-1', 1, 'Coordinator', 'self', 2),
                ('worker-1', '[Worker] Root Campaign Task', 'my-repo', 'Gemini Pro', '2026-10-06T10:10:00Z', '2026-10-06T10:25:00Z', 20, 15000, 1500,
                 900, 500, 600, 400, 1, 15, 1, 200, 50, 0, 3.50, 1200, 300, 'coord-1', 'root-1', 2, 'Worker', 'self', 3),
                ('indep-1', 'Standalone Single Session', 'other-repo', 'Gemini Flash', '2026-10-06T11:00:00Z', '2026-10-06T11:10:00Z', 8, 3000, 600,
                 600, 100, 150, 50, 2, 5, 0, 10, 5, 0, 0.50, 500, 100, NULL, 'indep-1', 0, NULL, NULL, NULL)
            """)
            conn.commit()

            # 1. Hierarchy view (default)
            hier_convs = query_conversations_list(conn, view_mode="hierarchy")
            self.assertEqual(len(hier_convs), 2)  # Only root-1 and indep-1

            root_conv = next(c for c in hier_convs if c["id"] == "root-1")
            self.assertEqual(root_conv["subagent_count"], 2)
            self.assertEqual(root_conv["fleet_cost"], 6.50)  # 2.00 + 1.00 + 3.50
            self.assertEqual(root_conv["fleet_tokens"], 3000)  # 1000 + 500 + 1500
            self.assertEqual(root_conv["fleet_steps"], 35)  # 10 + 5 + 20
            self.assertEqual(len(root_conv["subagents"]), 2)
            # Verify subagents are ordered by depth
            self.assertEqual(root_conv["subagents"][0]["id"], "coord-1")
            self.assertEqual(root_conv["subagents"][0]["depth"], 1)
            self.assertEqual(root_conv["subagents"][1]["id"], "worker-1")
            self.assertEqual(root_conv["subagents"][1]["depth"], 2)

            indep_conv = next(c for c in hier_convs if c["id"] == "indep-1")
            self.assertEqual(indep_conv["subagent_count"], 0)
            self.assertEqual(indep_conv["fleet_cost"], 0.50)

            # 2. Flat view
            flat_convs = query_conversations_list(conn, view_mode="flat")
            self.assertEqual(len(flat_convs), 4)  # All 4 sessions present
            ids = {c["id"] for c in flat_convs}
            self.assertEqual(ids, {"root-1", "coord-1", "worker-1", "indep-1"})

            conn.close()

    def test_session_details_ancestors_and_swarm_tree(self):
        """Test query_session_details generates proper ancestors, children, and nested fleet tree."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at, total_steps, total_characters, total_tokens,
                    duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                    user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                    billed_cost, billed_input_tokens, billed_output_tokens,
                    parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
                ) VALUES 
                ('root-1', 'Root Campaign Task', 'my-repo', 'Gemini Pro', '2026-10-06T10:00:00Z', '2026-10-06T10:30:00Z', 10, 5000, 1000,
                 1800, 300, 400, 200, 5, 8, 0, 50, 10, 0, 2.00, 800, 200, NULL, 'root-1', 0, NULL, NULL, NULL),
                ('coord-1', '[Coordinator] Root Campaign Task', 'my-repo', 'Gemini Flash', '2026-10-06T10:05:00Z', '2026-10-06T10:20:00Z', 5, 2000, 500,
                 900, 150, 200, 100, 1, 4, 0, 0, 0, 0, 1.00, 400, 100, 'root-1', 'root-1', 1, 'Coordinator', 'self', 2),
                ('worker-1', '[Worker] Root Campaign Task', 'my-repo', 'Gemini Pro', '2026-10-06T10:10:00Z', '2026-10-06T10:25:00Z', 20, 15000, 1500,
                 900, 500, 600, 400, 1, 15, 1, 200, 50, 0, 3.50, 1200, 300, 'coord-1', 'root-1', 2, 'Worker', 'self', 3)
            """)
            conn.commit()

            # Query details for the intermediate coordinator session
            details = query_session_details(conn, "coord-1")
            self.assertIsNotNone(details)
            self.assertEqual(details["conversation"]["id"], "coord-1")

            # 1. Ancestors: should trace back to root-1
            self.assertEqual(len(details["ancestors"]), 1)
            self.assertEqual(details["ancestors"][0]["id"], "root-1")
            self.assertEqual(details["ancestors"][0]["role"], "Parent")

            # 2. Children: should have direct child worker-1
            self.assertEqual(len(details["children"]), 1)
            self.assertEqual(details["children"][0]["id"], "worker-1")
            self.assertEqual(details["children"][0]["role"], "Worker")

            # 3. Fleet summary: rollup across the entire swarm
            summary = details["fleet_summary"]
            self.assertEqual(summary["root_id"], "root-1")
            self.assertEqual(summary["subagent_count"], 2)
            self.assertEqual(summary["total_cost"], 6.50)
            self.assertEqual(summary["total_tokens"], 3000)
            self.assertEqual(summary["total_steps"], 35)

            # 4. Fleet tree: root-1 -> coord-1 -> worker-1
            tree = details["fleet_tree"]
            self.assertEqual(tree["id"], "root-1")
            self.assertFalse(tree["is_current"])
            self.assertEqual(len(tree["children"]), 1)

            coord_node = tree["children"][0]
            self.assertEqual(coord_node["id"], "coord-1")
            self.assertTrue(coord_node["is_current"])  # Active target!
            self.assertEqual(len(coord_node["children"]), 1)

            worker_node = coord_node["children"][0]
            self.assertEqual(worker_node["id"], "worker-1")
            self.assertFalse(worker_node["is_current"])

            conn.close()

    def test_kpi_summary_root_and_subagent_counts(self):
        """Test query_summary_data reports root_convs and subagent_convs counts."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at, total_steps, total_characters, total_tokens,
                    duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                    user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                    billed_cost, billed_input_tokens, billed_output_tokens,
                    parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
                ) VALUES 
                ('root-1', 'Root Campaign Task', 'my-repo', 'Gemini Pro', '2026-10-06T10:00:00Z', '2026-10-06T10:30:00Z', 10, 5000, 1000,
                 1800, 300, 400, 200, 5, 8, 0, 50, 10, 0, 2.00, 800, 200, NULL, 'root-1', 0, NULL, NULL, NULL),
                ('coord-1', '[Coordinator] Root Campaign Task', 'my-repo', 'Gemini Flash', '2026-10-06T10:05:00Z', '2026-10-06T10:20:00Z', 5, 2000, 500,
                 900, 150, 200, 100, 1, 4, 0, 0, 0, 0, 1.00, 400, 100, 'root-1', 'root-1', 1, 'Coordinator', 'self', 2)
            """)
            conn.commit()

            summary = query_summary_data(conn)
            self.assertEqual(summary["total_convs"], 2)
            self.assertEqual(summary["root_convs"], 1)
            self.assertEqual(summary["subagent_convs"], 1)

            conn.close()

    def test_clean_arg_str_and_extract_subagent_specs_edge_cases(self):
        """Test edge cases in _clean_arg_str and extract_subagent_specs."""
        from scripts.ingest import _clean_arg_str, extract_subagent_specs
        import scripts.backfill_analytics as backfill

        # _clean_arg_str edge cases
        self.assertEqual(_clean_arg_str(None), "")
        self.assertEqual(_clean_arg_str(""), "")
        self.assertEqual(_clean_arg_str("   "), "")
        self.assertEqual(_clean_arg_str('"Reviewer"'), "Reviewer")
        self.assertEqual(_clean_arg_str("'Reviewer'"), "Reviewer")
        self.assertEqual(_clean_arg_str('"\\"Reviewer\\""'), "Reviewer")
        self.assertEqual(_clean_arg_str(123), "123")
        # Strings with internal quotes should preserve internal quote tokens
        self.assertEqual(_clean_arg_str('"\\"Hello\\" and \\"World\\""'), '"Hello" and "World"')
        self.assertEqual(_clean_arg_str("'Hello' and 'World'"), "'Hello' and 'World'")

        # extract_subagent_specs edge cases
        self.assertEqual(extract_subagent_specs({}), [])
        self.assertEqual(extract_subagent_specs("not a dict"), [])

        # Flat lower-case args with defaults
        specs = extract_subagent_specs({"prompt": "Do task"})
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["role"], "Autonomous Worker")
        self.assertEqual(specs[0]["type"], "self")
        self.assertEqual(specs[0]["model"], "inherit")

        # Double encoded Subagents string
        double_encoded = json.dumps([{"Role": "Specialist", "TypeName": "custom"}])
        specs_json = extract_subagent_specs({"Subagents": json.dumps(double_encoded)})
        self.assertEqual(len(specs_json), 1)
        self.assertEqual(specs_json[0]["role"], "Specialist")
        self.assertEqual(specs_json[0]["type"], "custom")

        # Parity with backfill_analytics
        bf_specs = backfill.extract_subagent_specs({"Role": '"Planner"', "TypeName": "lead"})
        self.assertEqual(len(bf_specs), 1)
        self.assertEqual(bf_specs[0]["role"], "Planner")
        self.assertEqual(bf_specs[0]["type"], "lead")

    def test_parse_manage_subagents_output_edge_cases(self):
        """Test parse_manage_subagents_output on JSON and regex text formats."""
        from scripts.ingest import parse_manage_subagents_output

        # Empty or non-matching content
        self.assertEqual(parse_manage_subagents_output(""), [])
        self.assertEqual(parse_manage_subagents_output("No subagents here"), [])

        # Words ending in 'id:' such as 'solid:' should not match as conversationId
        false_positive_text = "active subagent(s):\n- style is solid: border-box\n"
        self.assertEqual(parse_manage_subagents_output(false_positive_text), [])

        # Bullet format with conversationId, role, type
        text_content = (
            "Currently 2 active subagent(s):\n"
            "- conversationId: child-1, role: Auditor, type: audit-tool\n"
            "- conversationId: child-2, role: Cleaner, type: cleanup\n"
        )
        parsed = parse_manage_subagents_output(text_content)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "child-1")
        self.assertEqual(parsed[0]["role"], "Auditor")
        self.assertEqual(parsed[0]["type"], "audit-tool")
        self.assertEqual(parsed[1]["id"], "child-2")
        self.assertEqual(parsed[1]["role"], "Cleaner")
        self.assertEqual(parsed[1]["type"], "cleanup")

    def test_sync_to_sqlite_fleet_rollups(self):
        """Test that sync_to_sqlite accurately calculates fleet_agent_count and fleet_cost across root and subagents."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)

            conn = get_db_connection(db_file)
            # Create a root session
            root_analysis = {
                "title": "Root Task",
                "start_time": "2026-10-19T10:00:00Z",
                "end_time": "2026-10-19T10:30:00Z",
                "duration_seconds": 1800,
                "total_chars": 1000,
                "total_tokens": 500,
                "active_context_tokens": 500,
                "billed_cost": 2.50,
                "steps": [],
                "dispatched_subagents": [
                    {"id": "sub-1", "role": "Worker 1", "type": "self", "step_index": 2}
                ],
            }
            ingest.sync_to_sqlite("root-sess", "Gemini Pro", root_analysis, db_path=db_file)

            # Check root rollups
            self.assertEqual(root_analysis.get("fleet_agent_count"), 2)  # root + sub-1 placeholder
            self.assertEqual(root_analysis.get("fleet_cost"), 2.50)

            # Ingest child session
            child_analysis = {
                "title": "[Worker 1] Root Task",
                "start_time": "2026-10-19T10:05:00Z",
                "end_time": "2026-10-19T10:20:00Z",
                "duration_seconds": 900,
                "total_chars": 800,
                "total_tokens": 400,
                "active_context_tokens": 400,
                "billed_cost": 1.25,
                "parent_id": "root-sess",
                "subagent_role": "Worker 1",
                "steps": [],
            }
            ingest.sync_to_sqlite("sub-1", "Gemini Pro", child_analysis, db_path=db_file)

            # Both root and child reflect 2 agents and 3.75 total cost
            self.assertEqual(child_analysis.get("fleet_agent_count"), 2)
            self.assertEqual(child_analysis.get("fleet_cost"), 3.75)
            conn.close()

    def test_backfill_analytics_title_update_from_autonomous_worker(self):
        """Test that backfill updates titles previously named [Autonomous Worker] when a specific role is discovered."""
        from scripts.backfill_analytics import run_backfill
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            # Create transcripts: parent dispatching worker with role Standards Reviewer
            parent_dir = tmp_path / "parent-c"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            parent_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Implement feature Y",
                    "created_at": "2026-10-19T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching worker",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Standards Reviewer", "TypeName": "custom"},
                        }
                    ],
                    "created_at": "2026-10-19T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-c",\n  "logAbsoluteUri": "file:///tmp/child-c"\n}',
                    "created_at": "2026-10-19T10:00:06Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in parent_tx:
                    f.write(json.dumps(entry) + "\n")

            # Child transcript with old title
            child_dir = tmp_path / "child-c"
            child_logs = child_dir / ".system_generated" / "logs"
            child_logs.mkdir(parents=True)

            child_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-19T10:00:06Z sender=parent-c\nDo review",
                    "created_at": "2026-10-19T10:00:07Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Done",
                    "created_at": "2026-10-19T10:00:10Z",
                },
            ]
            with open(child_logs / "transcript.jsonl", "w") as f:
                for entry in child_tx:
                    f.write(json.dumps(entry) + "\n")

            # First insert with [Autonomous Worker]
            conn = get_db_connection(db_file)
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('child-c', '[Autonomous Worker] Implement feature Y', '2026-10-19T10:00:07Z', '2026-10-19T10:00:10Z')"
            )
            conn.commit()
            conn.close()

            # Run backfill on tmp_path
            run_backfill(brain_dir=tmp_path, db_path=db_file)

            conn = get_db_connection(db_file)
            child_row = conn.execute("SELECT title, subagent_role FROM conversations WHERE id = 'child-c'").fetchone()
            self.assertEqual(child_row["subagent_role"], "Standards Reviewer")
            self.assertEqual(child_row["title"], "[Standards Reviewer] Implement feature Y")
            conn.close()

    def test_wave_dispatch_queue_alignment_with_failed_call(self):
        """Verify that a failed invoke_subagent step does not cause subsequent successful dispatches to get misaligned roles."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent_dir = Path(tmp_dir) / "parent-failed-call"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            transcript = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Run parallel tasks",
                    "created_at": "2026-10-19T22:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching two workers...",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Failed Worker", "Prompt": "Will fail"},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Successful Worker", "Prompt": "Will succeed"},
                        },
                    ],
                    "created_at": "2026-10-19T22:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "status": "ERROR",
                    "error": "Failed to spawn process",
                    "content": "Error: unable to invoke subagent",
                    "created_at": "2026-10-19T22:00:06Z",
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "succ-child-1"\n}',
                    "created_at": "2026-10-19T22:00:07Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in transcript:
                    f.write(json.dumps(entry) + "\n")

            p_data = ingest.analyze_transcript(parent_dir)
            self.assertEqual(len(p_data["dispatched_subagents"]), 1)
            self.assertEqual(p_data["dispatched_subagents"][0]["id"], "succ-child-1")
            # Should have Successful Worker, NOT Failed Worker
            self.assertEqual(p_data["dispatched_subagents"][0]["role"], "Successful Worker")

    def test_sync_to_sqlite_updates_child_autonomous_worker_title(self):
        """Verify sync_to_sqlite updates child conversation title and role from [Autonomous Worker] on conflict."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_analytics.db"
            init_db(db_file)

            conn = get_db_connection(db_file)
            # Parent inserts child as Autonomous Worker first
            initial_analysis = {
                "title": "Parent Pipeline",
                "start_time": "2026-10-19T10:00:00Z",
                "end_time": "2026-10-19T10:10:00Z",
                "duration_seconds": 600,
                "total_chars": 500,
                "total_tokens": 250,
                "active_context_tokens": 250,
                "billed_cost": 1.0,
                "steps": [],
                "dispatched_subagents": [
                    {"id": "child-w1", "role": "Autonomous Worker", "type": "self", "step_index": 2}
                ],
            }
            ingest.sync_to_sqlite("parent-sess", "Gemini Pro", initial_analysis, db_path=db_file)

            child_before = conn.execute("SELECT title, subagent_role FROM conversations WHERE id = 'child-w1'").fetchone()
            self.assertEqual(child_before["title"], "[Autonomous Worker] Parent Pipeline")
            self.assertEqual(child_before["subagent_role"], "Autonomous Worker")

            # Later pass discovers specific role: Spec Reviewer
            updated_analysis = {
                "title": "Parent Pipeline",
                "start_time": "2026-10-19T10:00:00Z",
                "end_time": "2026-10-19T10:15:00Z",
                "duration_seconds": 900,
                "total_chars": 800,
                "total_tokens": 400,
                "active_context_tokens": 400,
                "billed_cost": 1.5,
                "steps": [],
                "dispatched_subagents": [
                    {"id": "child-w1", "role": "Spec Reviewer", "type": "custom", "step_index": 2}
                ],
            }
            ingest.sync_to_sqlite("parent-sess", "Gemini Pro", updated_analysis, db_path=db_file)

            child_after = conn.execute("SELECT title, subagent_role FROM conversations WHERE id = 'child-w1'").fetchone()
            self.assertEqual(child_after["title"], "[Spec Reviewer] Parent Pipeline")
            self.assertEqual(child_after["subagent_role"], "Spec Reviewer")
            conn.close()


if __name__ == "__main__":
    unittest.main()
