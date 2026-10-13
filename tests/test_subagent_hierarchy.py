import unittest
import tempfile
import sqlite3
import json
from pathlib import Path

from context_monitor.db import get_db_connection, init_db
from context_monitor.dashboard.api_routes import (
    query_conversations_list,
    query_session_details,
    query_summary_data
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
            cols = {c[1] for c in cur.execute("PRAGMA table_info(conversations)").fetchall()}
            self.assertIn("parent_id", cols)
            self.assertIn("root_id", cols)
            self.assertIn("depth", cols)
            self.assertIn("subagent_role", cols)
            self.assertIn("subagent_type", cols)
            self.assertIn("invoking_step_index", cols)

            # Verify indexes
            indexes = {r[1] for r in cur.execute("SELECT type, name FROM sqlite_master WHERE type='index'").fetchall()}
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
                    "created_at": "2026-10-06T10:00:00Z"
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
                                    {"Role": "DeepCoder Coordinator", "TypeName": "self", "Prompt": "Coordinate workers"}
                                ]
                            }
                        }
                    ],
                    "created_at": "2026-10-06T10:00:05Z"
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagents:\n- DeepCoder Coordinator (conversationId: child-456, type: self)\n\nNow communicate with them.',
                    "created_at": "2026-10-06T10:00:06Z"
                }
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
                    "content": '[Message] timestamp=2026-10-06T10:00:06Z sender=parent-123 recipient=child-456\nCoordinate workers for feature X',
                    "created_at": "2026-10-06T10:00:07Z"
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Starting coordination...",
                    "created_at": "2026-10-06T10:00:10Z"
                }
            ]
            with open(child_logs / "transcript.jsonl", "w") as f:
                for entry in child_transcript:
                    f.write(json.dumps(entry) + "\n")

            c_data = ingest.analyze_transcript(child_dir)
            self.assertIsNotNone(c_data)
            self.assertEqual(c_data["parent_id"], "parent-123")

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


if __name__ == "__main__":
    unittest.main()
