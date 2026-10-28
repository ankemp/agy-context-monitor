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
from context_monitor.pricing import (
    extract_native_step_models,
    normalize_native_model_name,
    resolve_subagent_model,
)
import scripts.ingest as ingest


def _encode_varint(v: int) -> bytes:
    b = bytearray()
    while v >= 0x80:
        b.append((v & 0x7F) | 0x80)
        v >>= 7
    b.append(v & 0x7F)
    return bytes(b)


def _encode_ld(fn: int, d: bytes) -> bytes:
    return _encode_varint((fn << 3) | 2) + _encode_varint(len(d)) + d


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

            # 5. Fleet nodes: flat list sorted by cost DESC, tokens DESC
            fleet_nodes = details["fleet_nodes"]
            self.assertEqual(len(fleet_nodes), 3)
            self.assertEqual(fleet_nodes[0]["id"], "worker-1")  # $3.50
            self.assertEqual(fleet_nodes[1]["id"], "root-1")    # $2.00
            self.assertEqual(fleet_nodes[2]["id"], "coord-1")   # $1.00

            w_node = fleet_nodes[0]
            self.assertEqual(w_node["cost"], 3.50)
            self.assertEqual(w_node["tokens"], 1500)
            self.assertEqual(w_node["active_tokens"], 1500)
            self.assertEqual(w_node["cost_share_pct"], round(3.50 / 6.50 * 100, 1))
            self.assertEqual(w_node["token_share_pct"], round(1500 / 3000 * 100, 1))
            self.assertGreater(w_node["max_tokens"], 0)
            self.assertGreater(w_node["context_pct"], 0.0)
            self.assertIsNotNone(w_node["weekly_quota_pct"])

            conn.close()

    def test_session_details_flat_and_double_encoded_invoke_subagent_blocks(self):
        """Verify query_session_details properly parses flat Antigravity 2.0 args and double-encoded JSON for invoke_subagent."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_invoke_parsing.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (id, title, model_name, created_at, total_steps, total_tokens, billed_cost)
                VALUES ('conv-flat', 'Parent Session', 'Gemini Pro', '2026-10-18T10:00:00Z', 2, 2000, 0.50)
            """)
            conn.execute("""
                INSERT INTO steps (conversation_id, step_index, phase, created_at, est_tokens, billed_cost)
                VALUES ('conv-flat', 1, 'exec', '2026-10-18T10:00:05Z', 500, 0.25)
            """)
            conn.execute("""
                INSERT INTO steps (conversation_id, step_index, phase, created_at, est_tokens, billed_cost)
                VALUES ('conv-flat', 2, 'exec', '2026-10-18T10:00:10Z', 500, 0.25)
            """)

            # Step 1: Flat args invoke_subagent
            flat_args = json.dumps({"Role": "DeepCoder", "TypeName": "autonomous", "Model": "gemini-1.5-pro", "Prompt": "Fix bug"})
            conn.execute("""
                INSERT INTO transcript_blocks (conversation_id, step_index, block_type, call_id, content, block_hash)
                VALUES ('conv-flat', 1, 'tool_use', 'call_flat_1', ?, 'h1')
            """, (flat_args,))
            conn.execute("""
                INSERT INTO tool_executions (conversation_id, step_index, tool_name)
                VALUES ('conv-flat', 1, 'invoke_subagent')
            """)

            # Step 1: Double-encoded JSON string invoke_subagent
            double_encoded = json.dumps(json.dumps({"Role": "Planner", "TypeName": "lead", "Model": "gemini-1.5-flash"}))
            conn.execute("""
                INSERT INTO transcript_blocks (conversation_id, step_index, block_type, call_id, content, block_hash)
                VALUES ('conv-flat', 1, 'tool_use', 'call_flat_2', ?, 'h2')
            """, (double_encoded,))
            conn.execute("""
                INSERT INTO tool_executions (conversation_id, step_index, tool_name)
                VALUES ('conv-flat', 1, 'invoke_subagent')
            """)

            # Step 2: Tool result pairing with call_flat_1
            conn.execute("""
                INSERT INTO transcript_blocks (conversation_id, step_index, block_type, call_id, content, block_hash)
                VALUES ('conv-flat', 2, 'tool_result', 'call_flat_1', 'Subagent created conversationId: 11111111-2222-3333-4444-555555555555', 'h3')
            """)

            conn.commit()

            details = query_session_details(conn, "conv-flat")
            conn.close()

            self.assertIsNotNone(details)
            steps = details["steps"]
            step1_blocks = steps[0]["blocks"]
            self.assertEqual(len(step1_blocks), 2)

            # Block 0: Flat args
            b0 = step1_blocks[0]
            self.assertTrue(b0.get("is_subagent"))
            self.assertEqual(len(b0["subagents"]), 1)
            self.assertEqual(b0["subagents"][0]["Role"], "DeepCoder")
            self.assertEqual(b0["subagents"][0]["TypeName"], "autonomous")
            self.assertEqual(b0["subagents"][0]["Model"], "gemini-1.5-pro")

            # Block 1: Double-encoded
            b1 = step1_blocks[1]
            self.assertTrue(b1.get("is_subagent"))
            self.assertEqual(len(b1["subagents"]), 1)
            self.assertEqual(b1["subagents"][0]["Role"], "Planner")
            self.assertEqual(b1["subagents"][0]["TypeName"], "lead")
            self.assertEqual(b1["subagents"][0]["Model"], "gemini-1.5-flash")

            # Step 2 Block 0: Result paired
            step2_blocks = steps[1]["blocks"]
            b_res = step2_blocks[0]
            self.assertEqual(b_res.get("paired_tool_name"), "invoke_subagent")
            self.assertTrue(b_res.get("is_subagent"))
            self.assertEqual(b_res.get("subagent_conversation_id"), "11111111-2222-3333-4444-555555555555")
            self.assertEqual(len(b_res["subagents"]), 1)
            self.assertEqual(b_res["subagents"][0]["Role"], "DeepCoder")

    def test_session_details_all_zero_cost_swarm(self):
        """Verify query_session_details cleanly handles swarms where all subagents have 0 cost and 0 tokens."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_zero_swarm.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (id, root_id, parent_id, title, model_name, created_at, total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens)
                VALUES 
                ('zero-root', NULL, NULL, 'Zero Root', 'Gemini Pro', '2026-10-18T10:00:00Z', 0, 0, 0.0, 0, 0),
                ('zero-child', 'zero-root', 'zero-root', 'Zero Child', 'Gemini Flash', '2026-10-18T10:05:00Z', 0, 0, 0.0, 0, 0)
            """)
            conn.commit()

            details = query_session_details(conn, "zero-root")
            conn.close()

            self.assertIsNotNone(details)
            fleet_nodes = details["fleet_nodes"]
            self.assertEqual(len(fleet_nodes), 2)
            for node in fleet_nodes:
                self.assertEqual(node["cost"], 0.0)
                self.assertEqual(node["tokens"], 0)
                self.assertEqual(node["cost_share_pct"], 0.0)
                self.assertEqual(node["token_share_pct"], 0.0)

    def test_session_details_with_empty_transcript_content(self):
        """Verify query_session_details handles transcript blocks with empty string content gracefully."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "test_empty_blocks.db"
            init_db(db_file)
            conn = get_db_connection(db_file)

            conn.execute("""
                INSERT INTO conversations (id, title, model_name, created_at, total_steps, total_tokens, billed_cost)
                VALUES ('conv-empty-blocks', 'Empty Blocks Session', 'Gemini Pro', '2026-10-18T10:00:00Z', 1, 100, 0.05)
            """)
            conn.execute("""
                INSERT INTO steps (conversation_id, step_index, phase, created_at, est_tokens, billed_cost)
                VALUES ('conv-empty-blocks', 1, 'exec', '2026-10-18T10:00:05Z', 100, 0.05)
            """)
            conn.execute("""
                INSERT INTO transcript_blocks (conversation_id, step_index, block_type, call_id, content, block_hash)
                VALUES 
                ('conv-empty-blocks', 1, 'tool_use', 'call_empty_1', '', 'h1'),
                ('conv-empty-blocks', 1, 'tool_result', 'call_empty_1', '', 'h2')
            """)
            conn.execute("""
                INSERT INTO tool_executions (conversation_id, step_index, tool_name)
                VALUES ('conv-empty-blocks', 1, 'invoke_subagent')
            """)
            conn.commit()

            details = query_session_details(conn, "conv-empty-blocks")
            conn.close()

            self.assertIsNotNone(details)
            steps = details["steps"]
            self.assertEqual(len(steps), 1)
            blocks = steps[0]["blocks"]
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0]["subagents"], [])

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
        from context_monitor.subagent import _clean_arg_str, extract_subagent_specs
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

    def test_normalize_native_model_name_and_tier_resolution(self):
        """Verify normalize_native_model_name handles placeholders, tiered flash, raw IDs, and resolve_subagent_model handles inherit/tiers."""
        from context_monitor.pricing import DEFAULT_MODEL

        self.assertEqual(
            normalize_native_model_name("gemini-3.8-flash-tiered", "", "MODEL_PLACEHOLDER_M322"),
            "Gemini 3.8 Flash (Tiered)",
        )
        self.assertEqual(
            normalize_native_model_name("gemini-4-argon", "Gemini 4 Argon (Medium)", "MODEL_PLACEHOLDER_M418"),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            normalize_native_model_name("gemini-4-argon", "", ""),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            normalize_native_model_name("gemini-4-argon-b", "", ""),
            "Gemini 4 Argon (High)",
        )
        self.assertIsNone(normalize_native_model_name("", "", ""))

        # Additional placeholder enums discovered in wild conversations
        self.assertEqual(
            normalize_native_model_name("gemini-3.8-flash", "", "MODEL_PLACEHOLDER_M319"),
            "Gemini 3.8 Flash (Medium)",
        )
        self.assertEqual(
            normalize_native_model_name("gemini-4-argon", "", "MODEL_PLACEHOLDER_M419"),
            "Gemini 4 Argon (Low)",
        )
        self.assertEqual(
            normalize_native_model_name("gemini-4-argon-b", "", "MODEL_PLACEHOLDER_M417"),
            "Gemini 4 Argon (High)",
        )
        self.assertEqual(
            normalize_native_model_name("custom-model-id", "", ""),
            "custom-model-id",
        )

        # Basic tier resolution
        self.assertEqual(
            resolve_subagent_model("inherit", "Gemini 3.8 Flash (High) → Gemini 4 Argon (Medium)"),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            resolve_subagent_model("pro", "Gemini 3.8 Flash (High)"),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            resolve_subagent_model("flash", "Gemini 4 Argon (Medium)"),
            "Gemini 3.8 Flash (High)",
        )
        self.assertEqual(
            resolve_subagent_model("flash_lite", "Gemini 4 Argon (Medium)"),
            "Gemini 2.5 Flash Lite",
        )

        # Advanced tier resolution edge cases: mixed arrows, trailing arrows, inherit parent, raw slugs
        self.assertEqual(
            resolve_subagent_model("inherit", "Gemini 3.8 Flash (High) → Gemini 3.8 Flash (Medium) -> Gemini 4 Argon (Medium)"),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            resolve_subagent_model("inherit", "Gemini 4 Argon (Medium) → "),
            "Gemini 4 Argon (Medium)",
        )
        self.assertEqual(
            resolve_subagent_model("inherit", "inherit"),
            DEFAULT_MODEL,
        )
        self.assertEqual(
            resolve_subagent_model("inherit", None),
            DEFAULT_MODEL,
        )
        self.assertEqual(
            resolve_subagent_model("gemini-3.8-flash-tiered", "Gemini 4 Argon (Medium)"),
            "Gemini 3.8 Flash (Tiered)",
        )
        self.assertEqual(
            resolve_subagent_model("gemini-4-argon", "Gemini 3.8 Flash (High)"),
            "Gemini 4 Argon (Medium)",
        )

    def test_extract_native_step_models_from_sqlite_and_missing_db(self):
        """Verify extract_native_step_models behavior on nonexistent, empty, corrupt, and valid native conversation DBs."""
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # 1. Nonexistent CID
            res_nonexistent = extract_native_step_models("nonexistent-cid", conversations_dir=tmp_path)
            self.assertEqual(res_nonexistent, {})
            self.assertFalse((tmp_path / "nonexistent-cid.db").exists())
            self.assertEqual(extract_native_step_models("", conversations_dir=tmp_path), {})

            # 2. 0-byte DB file
            empty_file = tmp_path / "empty-cid.db"
            empty_file.touch()
            res_empty = extract_native_step_models("empty-cid", conversations_dir=tmp_path)
            self.assertEqual(res_empty, {})

            # 3. Corrupt/truncated protobuf bytes in gen_metadata
            corrupt_file = tmp_path / "corrupt-cid.db"
            conn = sqlite3.connect(corrupt_file)
            conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, X'12030102', 4)")
            conn.commit()
            conn.close()
            res_corrupt = extract_native_step_models("corrupt-cid", conversations_dir=tmp_path)
            self.assertEqual(res_corrupt, {})

            # 4. Valid Argon session with step indices 1, 2, 3
            argon_file = tmp_path / "argon-cid.db"
            conn = sqlite3.connect(argon_file)
            conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            f2_argon = _encode_ld(2, _encode_varint(1) + _encode_varint(2) + _encode_varint(3))
            f1_argon = _encode_ld(1, _encode_ld(19, b"gemini-4-argon") + _encode_ld(21, b"Gemini 4 Argon (Medium)"))
            argon_payload = f2_argon + f1_argon
            conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)", (argon_payload, len(argon_payload)))
            conn.commit()
            conn.close()
            res_argon = extract_native_step_models("argon-cid", conversations_dir=tmp_path)
            self.assertEqual(
                res_argon,
                {1: "Gemini 4 Argon (Medium)", 2: "Gemini 4 Argon (Medium)", 3: "Gemini 4 Argon (Medium)"},
            )
            # Verify cid ending with .db suffix resolves identical db file
            res_argon_suffix = extract_native_step_models("argon-cid.db", conversations_dir=tmp_path)
            self.assertEqual(res_argon_suffix, res_argon)

            # 5. Tiered flash session where field 1 -> 19 = "gemini-3.8-flash-tiered", 1 -> 21 = ""
            tiered_file = tmp_path / "tiered-cid.db"
            conn = sqlite3.connect(tiered_file)
            conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            f2_tiered = _encode_ld(2, _encode_varint(1))
            f1_tiered = _encode_ld(1, _encode_ld(19, b"gemini-3.8-flash-tiered") + _encode_ld(21, b""))
            tiered_payload = f2_tiered + f1_tiered
            conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)", (tiered_payload, len(tiered_payload)))
            conn.commit()
            conn.close()
            res_tiered = extract_native_step_models("tiered-cid", conversations_dir=tmp_path)
            self.assertEqual(res_tiered, {1: "Gemini 3.8 Flash (Tiered)"})

            # 6. Partial protobuf recovery: valid field 2 and field 1 followed by truncated trailing field
            partial_file = tmp_path / "partial-cid.db"
            conn = sqlite3.connect(partial_file)
            conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            f2_part = _encode_ld(2, _encode_varint(10) + _encode_varint(11))
            f1_part = _encode_ld(1, _encode_ld(19, b"gemini-4-argon") + _encode_ld(21, b"Gemini 4 Argon (Medium)"))
            # Truncated length-delimited field 8 (specifies length 20 but only 2 bytes provided)
            f8_truncated = _encode_varint((8 << 3) | 2) + _encode_varint(20) + b"\xaa\xbb"
            partial_payload = f2_part + f1_part + f8_truncated
            conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)", (partial_payload, len(partial_payload)))
            conn.commit()
            conn.close()
            res_partial = extract_native_step_models("partial-cid", conversations_dir=tmp_path)
            self.assertEqual(res_partial, {10: "Gemini 4 Argon (Medium)", 11: "Gemini 4 Argon (Medium)"})

            # 7. Fallback when field 2 is omitted and idx is NULL
            nullidx_file = tmp_path / "nullidx-cid.db"
            conn = sqlite3.connect(nullidx_file)
            conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (NULL, ?, ?)", (f1_part, len(f1_part)))
            conn.commit()
            conn.close()
            res_nullidx = extract_native_step_models("nullidx-cid", conversations_dir=tmp_path)
            self.assertEqual(res_nullidx, {1: "Gemini 4 Argon (Medium)"})

    def test_ingest_subagent_inherits_parent_argon_model_when_no_native_db(self):
        """Verify subagent inherits parent Argon model when no native DB exists, and calculates cost at Argon rates."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            # 1. Setup parent transcript
            parent_dir = tmp_path / "parent-argon"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            parent_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": (
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                    "created_at": "2026-10-20T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching worker-1 and worker-2",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Inherit", "TypeName": "self", "Model": "inherit", "Prompt": "Task 1"},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Pro", "TypeName": "self", "Model": "pro", "Prompt": "Task 2"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-1"\n}',
                    "created_at": "2026-10-20T10:00:06Z",
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-2"\n}',
                    "created_at": "2026-10-20T10:00:07Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in parent_tx:
                    f.write(json.dumps(entry) + "\n")

            p_analysis = ingest.analyze_transcript(parent_dir)
            self.assertEqual(p_analysis["latest_model"], "Gemini 4 Argon (Medium)")
            self.assertTrue(p_analysis.get("has_explicit_model", False))

            # Sync parent to SQLite
            ingest.sync_to_sqlite("parent-argon", p_analysis["latest_model"], p_analysis, db_path=db_file)

            conn = get_db_connection(db_file)
            c1_stub = conn.execute("SELECT model_name, models_used, total_steps FROM conversations WHERE id = 'child-1'").fetchone()
            c2_stub = conn.execute("SELECT model_name, models_used, total_steps FROM conversations WHERE id = 'child-2'").fetchone()
            self.assertIsNotNone(c1_stub)
            self.assertIsNotNone(c2_stub)
            self.assertEqual(c1_stub["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(c2_stub["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(c1_stub["total_steps"], 0)
            conn.close()

            # 2. Setup child-1 transcript (no USER_INPUT step, no native DB)
            child1_dir = tmp_path / "child-1"
            child1_logs = child1_dir / ".system_generated" / "logs"
            child1_logs.mkdir(parents=True)

            child1_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:07Z sender=parent-argon recipient=child-1\nExecute task",
                    "created_at": "2026-10-20T10:00:08Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Working on the assigned task...",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:15Z",
                },
            ]
            with open(child1_logs / "transcript.jsonl", "w") as f:
                for entry in child1_tx:
                    f.write(json.dumps(entry) + "\n")

            c1_analysis = ingest.analyze_transcript(child1_dir)
            self.assertFalse(c1_analysis.get("has_explicit_model", False))

            # Sync child-1 to SQLite
            ingest.sync_to_sqlite("child-1", c1_analysis["latest_model"], c1_analysis, db_path=db_file)

            conn = get_db_connection(db_file)
            c1_row = conn.execute("SELECT model_name, models_used, billed_cost, total_steps FROM conversations WHERE id = 'child-1'").fetchone()
            c1_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'child-1' AND step_index = 2").fetchone()
            conn.close()

            self.assertEqual(c1_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(c1_step["model_name"], "Gemini 4 Argon (Medium)")
            # In Argon: 1000 tokens out = 1000 / 1e6 * $10.00 = $0.010
            # (In Flash it would have been $0.0006)
            self.assertAlmostEqual(c1_row["billed_cost"], 0.010, places=4)
            self.assertAlmostEqual(c1_step["billed_cost"], 0.010, places=4)

    def test_ingest_subagent_uses_native_gen_metadata_model_and_preserves_on_parent_resync(self):
        """Verify subagent uses native DB gen_metadata model and parent re-sync does not overwrite it."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)
            convs_dir = tmp_path / "conversations"
            convs_dir.mkdir(parents=True)

            # 1. Setup parent transcript
            parent_dir = tmp_path / "parent-pro"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            parent_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": (
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                    "created_at": "2026-10-20T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching worker-pro",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Pro", "TypeName": "self", "Model": "pro", "Prompt": "Task Pro"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-pro"\n}',
                    "created_at": "2026-10-20T10:00:06Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in parent_tx:
                    f.write(json.dumps(entry) + "\n")

            p_analysis = ingest.analyze_transcript(parent_dir, conversations_dir=convs_dir)
            ingest.sync_to_sqlite("parent-pro", p_analysis["latest_model"], p_analysis, db_path=db_file, conversations_dir=convs_dir)

            # 2. Create native conversations/child-pro.db with gen_metadata reporting gemini-3.8-flash-tiered
            child_db_file = convs_dir / "child-pro.db"
            c_conn = sqlite3.connect(child_db_file)
            c_conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            f2 = _encode_ld(2, _encode_varint(2))
            f1 = _encode_ld(1, _encode_ld(19, b"gemini-3.8-flash-tiered") + _encode_ld(21, b""))
            payload = f2 + f1
            c_conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)", (payload, len(payload)))
            c_conn.commit()
            c_conn.close()

            # 3. Setup child-pro transcript
            child_dir = tmp_path / "child-pro"
            child_logs = child_dir / ".system_generated" / "logs"
            child_logs.mkdir(parents=True)

            child_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:06Z sender=parent-pro recipient=child-pro\nExecute pro task",
                    "created_at": "2026-10-20T10:00:07Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Executing pro task with tiered model...",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:15Z",
                },
            ]
            with open(child_logs / "transcript.jsonl", "w") as f:
                for entry in child_tx:
                    f.write(json.dumps(entry) + "\n")

            c_analysis = ingest.analyze_transcript(child_dir, conversations_dir=convs_dir)
            self.assertEqual(c_analysis["latest_model"], "Gemini 3.8 Flash (Tiered)")
            ingest.sync_to_sqlite("child-pro", c_analysis["latest_model"], c_analysis, db_path=db_file, conversations_dir=convs_dir)

            conn = get_db_connection(db_file)
            cp_row = conn.execute("SELECT model_name, models_used, total_steps FROM conversations WHERE id = 'child-pro'").fetchone()
            cp_step = conn.execute("SELECT model_name FROM steps WHERE conversation_id = 'child-pro' AND step_index = 2").fetchone()
            conn.close()

            self.assertEqual(cp_row["model_name"], "Gemini 3.8 Flash (Tiered)")
            self.assertEqual(cp_step["model_name"], "Gemini 3.8 Flash (Tiered)")
            self.assertEqual(cp_row["total_steps"], 2)

            # 4. Parent subsequently re-syncs
            p_analysis_2 = ingest.analyze_transcript(parent_dir, conversations_dir=convs_dir)
            ingest.sync_to_sqlite("parent-pro", p_analysis_2["latest_model"], p_analysis_2, db_path=db_file, conversations_dir=convs_dir)

            # 5. Verify child-pro's model_name is NOT overwritten by parent stub
            conn = get_db_connection(db_file)
            cp_after = conn.execute("SELECT model_name, models_used, total_steps FROM conversations WHERE id = 'child-pro'").fetchone()
            conn.close()
            self.assertEqual(cp_after["model_name"], "Gemini 3.8 Flash (Tiered)")
            self.assertEqual(cp_after["models_used"], "Gemini 3.8 Flash (Tiered)")

    def test_ingest_subagent_with_flash_preserves_flash_on_subsequent_turns(self):
        """Verify subagent dispatched with flash tier remains flash across multiple turns, not mutating to parent Argon."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            # 1. Setup parent transcript with Argon
            parent_dir = tmp_path / "parent-argon"
            parent_logs = parent_dir / ".system_generated" / "logs"
            parent_logs.mkdir(parents=True)

            parent_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": (
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                    "created_at": "2026-10-20T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching worker-flash",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Flash", "TypeName": "self", "Model": "flash", "Prompt": "Fast task"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-flash"\n}',
                    "created_at": "2026-10-20T10:00:06Z",
                },
            ]
            with open(parent_logs / "transcript.jsonl", "w") as f:
                for entry in parent_tx:
                    f.write(json.dumps(entry) + "\n")

            p_analysis = ingest.analyze_transcript(parent_dir)
            ingest.sync_to_sqlite("parent-argon", p_analysis["latest_model"], p_analysis, db_path=db_file)

            # 2. Setup child-flash transcript turn 1
            child_dir = tmp_path / "child-flash"
            child_logs = child_dir / ".system_generated" / "logs"
            child_logs.mkdir(parents=True)

            child_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:06Z sender=parent-argon recipient=child-flash\nExecute fast task",
                    "created_at": "2026-10-20T10:00:07Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Finished turn 1",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:15Z",
                },
            ]
            with open(child_logs / "transcript.jsonl", "w") as f:
                for entry in child_tx:
                    f.write(json.dumps(entry) + "\n")

            c_analysis_1 = ingest.analyze_transcript(child_dir)
            ingest.sync_to_sqlite("child-flash", c_analysis_1["latest_model"], c_analysis_1, db_path=db_file)

            conn = get_db_connection(db_file)
            c1_row = conn.execute("SELECT model_name, models_used, billed_cost, total_steps FROM conversations WHERE id = 'child-flash'").fetchone()
            conn.close()
            self.assertEqual(c1_row["model_name"], "Gemini 3.8 Flash (High)")
            self.assertEqual(c1_row["total_steps"], 2)
            # Flash rate: 1000 / 1e6 * 0.60 = 0.0006
            self.assertAlmostEqual(c1_row["billed_cost"], 0.0006, places=4)

            # 3. Child-flash executes turn 2
            child_tx_turn2 = [
                {
                    "step_index": 3,
                    "type": "TOOL_CALL_RESULT",
                    "source": "SYSTEM",
                    "content": "Operation successful",
                    "created_at": "2026-10-20T10:00:20Z",
                },
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Finished turn 2",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:25Z",
                },
            ]
            with open(child_logs / "transcript.jsonl", "a") as f:
                for entry in child_tx_turn2:
                    f.write(json.dumps(entry) + "\n")

            c_analysis_2 = ingest.analyze_transcript(child_dir)
            ingest.sync_to_sqlite("child-flash", c_analysis_2["latest_model"], c_analysis_2, db_path=db_file)

            # 4. Verify turn 2 STILL preserves Flash and does NOT mutate to parent's Argon
            conn = get_db_connection(db_file)
            c2_row = conn.execute("SELECT model_name, models_used, billed_cost, total_steps FROM conversations WHERE id = 'child-flash'").fetchone()
            c2_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'child-flash' AND step_index = 4").fetchone()
            conn.close()

            self.assertEqual(c2_row["model_name"], "Gemini 3.8 Flash (High)")
            self.assertEqual(c2_row["total_steps"], 4)
            self.assertEqual(c2_step["model_name"], "Gemini 3.8 Flash (High)")
            # 2 planner steps at Flash rates: total cost approx $0.0013 - $0.0014 (not Argon $0.02+)
            self.assertLess(c2_row["billed_cost"], 0.003)

    def test_ingest_subagent_3_level_cascade(self):
        """Verify 3-level deep cascade (Root Argon -> Coordinator Inherit -> Worker Inherit) correctly propagates Argon."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            # 1. Setup Root with Argon
            root_dir = tmp_path / "root-session"
            root_logs = root_dir / ".system_generated" / "logs"
            root_logs.mkdir(parents=True)

            root_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": (
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                    "created_at": "2026-10-20T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching coordinator",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Coordinator", "TypeName": "self", "Model": "inherit", "Prompt": "Coord task"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "coord-subagent"\n}',
                    "created_at": "2026-10-20T10:00:06Z",
                },
            ]
            with open(root_logs / "transcript.jsonl", "w") as f:
                for entry in root_tx:
                    f.write(json.dumps(entry) + "\n")

            r_analysis = ingest.analyze_transcript(root_dir)
            ingest.sync_to_sqlite("root-session", r_analysis["latest_model"], r_analysis, db_path=db_file)

            # 2. Setup Coordinator transcript (dispatches Worker with inherit)
            coord_dir = tmp_path / "coord-subagent"
            coord_logs = coord_dir / ".system_generated" / "logs"
            coord_logs.mkdir(parents=True)

            coord_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:06Z sender=root-session recipient=coord-subagent\nCoordinate work",
                    "created_at": "2026-10-20T10:00:07Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching leaf worker",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Leaf Worker", "TypeName": "self", "Model": "inherit", "Prompt": "Leaf task"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:12Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "worker-leaf"\n}',
                    "created_at": "2026-10-20T10:00:13Z",
                },
            ]
            with open(coord_logs / "transcript.jsonl", "w") as f:
                for entry in coord_tx:
                    f.write(json.dumps(entry) + "\n")

            c_analysis = ingest.analyze_transcript(coord_dir)
            ingest.sync_to_sqlite("coord-subagent", c_analysis["latest_model"], c_analysis, db_path=db_file)

            # 3. Setup Worker transcript
            worker_dir = tmp_path / "worker-leaf"
            worker_logs = worker_dir / ".system_generated" / "logs"
            worker_logs.mkdir(parents=True)

            worker_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:13Z sender=coord-subagent recipient=worker-leaf\nDo leaf work",
                    "created_at": "2026-10-20T10:00:14Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Done leaf work",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:20Z",
                },
            ]
            with open(worker_logs / "transcript.jsonl", "w") as f:
                for entry in worker_tx:
                    f.write(json.dumps(entry) + "\n")

            w_analysis = ingest.analyze_transcript(worker_dir)
            ingest.sync_to_sqlite("worker-leaf", w_analysis["latest_model"], w_analysis, db_path=db_file)

            # 4. Verify lineage and model inheritance down all 3 levels
            conn = get_db_connection(db_file)
            r_row = conn.execute("SELECT model_name, depth FROM conversations WHERE id = 'root-session'").fetchone()
            c_row = conn.execute("SELECT model_name, depth, parent_id, root_id FROM conversations WHERE id = 'coord-subagent'").fetchone()
            w_row = conn.execute("SELECT model_name, depth, parent_id, root_id, billed_cost FROM conversations WHERE id = 'worker-leaf'").fetchone()
            w_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'worker-leaf' AND step_index = 2").fetchone()
            conn.close()

            self.assertEqual(r_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(r_row["depth"], 0)

            self.assertEqual(c_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(c_row["depth"], 1)
            self.assertEqual(c_row["parent_id"], "root-session")
            self.assertEqual(c_row["root_id"], "root-session")

            self.assertEqual(w_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(w_row["depth"], 2)
            self.assertEqual(w_row["parent_id"], "coord-subagent")
            self.assertEqual(w_row["root_id"], "root-session")
            self.assertEqual(w_step["model_name"], "Gemini 4 Argon (Medium)")
            # 1000 tokens out at Argon rate ($10/M) = $0.010
            self.assertAlmostEqual(w_row["billed_cost"], 0.010, places=4)
            self.assertAlmostEqual(w_step["billed_cost"], 0.010, places=4)

    def test_backfill_resolves_native_and_inherited_subagent_models(self):
        """Verify backfill resolves native gen_metadata and inherits parent model for subagents."""
        import sqlite3
        import scripts.backfill_analytics as backfill

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            brain_dir = tmp_path / "brain"
            brain_dir.mkdir(parents=True)
            conversations_dir = tmp_path / "conversations"
            conversations_dir.mkdir(parents=True)
            db_file = tmp_path / "test_analytics.db"

            # 1. Setup root-1 in brain_dir (switches to Gemini 4 Argon, dispatches 3 subagents)
            root_dir = brain_dir / "root-1"
            root_logs = root_dir / ".system_generated" / "logs"
            root_logs.mkdir(parents=True)

            root_tx = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": (
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                    "created_at": "2026-10-20T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Dispatching workers",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Inherit", "TypeName": "self", "Model": "inherit", "Prompt": "Inherit task"},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Pro Downgraded", "TypeName": "self", "Model": "pro", "Prompt": "Pro downgraded task"},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Pro Offline", "TypeName": "self", "Model": "pro", "Prompt": "Pro offline task"},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Worker Stub", "TypeName": "self", "Model": "pro", "Prompt": "Stub task"},
                        },
                    ],
                    "created_at": "2026-10-20T10:00:05Z",
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "sub-inherit"\n}',
                    "created_at": "2026-10-20T10:00:06Z",
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "sub-pro-downgraded"\n}',
                    "created_at": "2026-10-20T10:00:07Z",
                },
                {
                    "step_index": 5,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "sub-pro-offline"\n}',
                    "created_at": "2026-10-20T10:00:08Z",
                },
                {
                    "step_index": 6,
                    "type": "GENERIC",
                    "source": "SYSTEM",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "sub-stub-only"\n}',
                    "created_at": "2026-10-20T10:00:09Z",
                },
            ]
            with open(root_logs / "transcript.jsonl", "w") as f:
                for entry in root_tx:
                    f.write(json.dumps(entry) + "\n")

            # 2. Setup sub-inherit in brain_dir
            sub_inherit_dir = brain_dir / "sub-inherit"
            sub_inherit_logs = sub_inherit_dir / ".system_generated" / "logs"
            sub_inherit_logs.mkdir(parents=True)
            sub_inherit_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:06Z sender=root-1 recipient=sub-inherit\nExecute inherit task",
                    "created_at": "2026-10-20T10:00:09Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Finished inherit task",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:15Z",
                },
            ]
            with open(sub_inherit_logs / "transcript.jsonl", "w") as f:
                for entry in sub_inherit_tx:
                    f.write(json.dumps(entry) + "\n")

            # 3. Setup sub-pro-downgraded in brain_dir
            sub_down_dir = brain_dir / "sub-pro-downgraded"
            sub_down_logs = sub_down_dir / ".system_generated" / "logs"
            sub_down_logs.mkdir(parents=True)
            sub_down_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:07Z sender=root-1 recipient=sub-pro-downgraded\nExecute downgraded task",
                    "created_at": "2026-10-20T10:00:10Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Finished downgraded task",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:16Z",
                },
            ]
            with open(sub_down_logs / "transcript.jsonl", "w") as f:
                for entry in sub_down_tx:
                    f.write(json.dumps(entry) + "\n")

            # 4. Setup sub-pro-offline in brain_dir
            sub_off_dir = brain_dir / "sub-pro-offline"
            sub_off_logs = sub_off_dir / ".system_generated" / "logs"
            sub_off_logs.mkdir(parents=True)
            sub_off_tx = [
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "source": "SYSTEM",
                    "content": "[Message] timestamp=2026-10-20T10:00:08Z sender=root-1 recipient=sub-pro-offline\nExecute offline task",
                    "created_at": "2026-10-20T10:00:11Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Finished offline task",
                    "est_tokens": 1000,
                    "created_at": "2026-10-20T10:00:17Z",
                },
            ]
            with open(sub_off_logs / "transcript.jsonl", "w") as f:
                for entry in sub_off_tx:
                    f.write(json.dumps(entry) + "\n")

            # 5. Create synthetic conversations_dir / "sub-pro-downgraded.db" with gen_metadata
            child_db_file = conversations_dir / "sub-pro-downgraded.db"
            c_conn = sqlite3.connect(child_db_file)
            c_conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
            f2 = _encode_ld(2, _encode_varint(2))
            f1 = _encode_ld(1, _encode_ld(19, b"gemini-3.8-flash-tiered") + _encode_ld(21, b""))
            payload = f2 + f1
            c_conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (0, ?, ?)", (payload, len(payload)))
            c_conn.commit()
            c_conn.close()

            # Leave sub-inherit and sub-pro-offline without .db files in conversations_dir

            # 6. Run backfill
            backfill.run_backfill(
                brain_dir=brain_dir,
                db_path=db_file,
                conversations_dir=conversations_dir,
                force=True,
            )

            # 7. Assert analytics.db
            conn = get_db_connection(db_file)
            root_row = conn.execute("SELECT model_name, models_used FROM conversations WHERE id = 'root-1'").fetchone()
            sub_inherit_row = conn.execute("SELECT model_name, models_used, billed_cost FROM conversations WHERE id = 'sub-inherit'").fetchone()
            sub_down_row = conn.execute("SELECT model_name, models_used, billed_cost FROM conversations WHERE id = 'sub-pro-downgraded'").fetchone()
            sub_off_row = conn.execute("SELECT model_name, models_used, billed_cost FROM conversations WHERE id = 'sub-pro-offline'").fetchone()
            sub_stub_row = conn.execute("SELECT model_name, models_used, parent_id, depth FROM conversations WHERE id = 'sub-stub-only'").fetchone()

            sub_inherit_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'sub-inherit' AND step_index = 2").fetchone()
            sub_down_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'sub-pro-downgraded' AND step_index = 2").fetchone()
            sub_off_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'sub-pro-offline' AND step_index = 2").fetchone()
            conn.close()

            self.assertEqual(root_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(sub_inherit_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(sub_down_row["model_name"], "Gemini 3.8 Flash (Tiered)")
            self.assertEqual(sub_off_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertIsNotNone(sub_stub_row)
            self.assertEqual(sub_stub_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(sub_stub_row["parent_id"], "root-1")
            self.assertEqual(sub_stub_row["depth"], 1)

            self.assertEqual(sub_inherit_step["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(sub_down_step["model_name"], "Gemini 3.8 Flash (Tiered)")
            self.assertEqual(sub_off_step["model_name"], "Gemini 4 Argon (Medium)")

            # Cost assertions: Argon output is $10/M tokens = $0.010 for 1000 tokens (plus minor input tokens)
            self.assertAlmostEqual(sub_inherit_row["billed_cost"], 0.010, places=3)
            self.assertAlmostEqual(sub_off_row["billed_cost"], 0.010, places=3)
            # Flash output is $0.60/M tokens = $0.0006 for 1000 tokens
            self.assertAlmostEqual(sub_down_row["billed_cost"], 0.0006, places=4)

    def test_backfill_3_level_cascade_propagates_argon_and_recomputes_costs(self):
        """Verify 3-level cascade in backfill (Root Argon -> Coord Inherit -> Leaf Inherit) propagates Argon and bills at Argon rates."""
        import scripts.backfill_analytics as backfill

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            brain_dir = tmp_path / "brain"
            brain_dir.mkdir(parents=True)
            db_file = tmp_path / "test_analytics.db"

            # 1. Root with Argon
            root_dir = brain_dir / "bf-root"
            (root_dir / ".system_generated" / "logs").mkdir(parents=True)
            with open(root_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
                f.write(json.dumps({
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n</USER_SETTINGS_CHANGE>",
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Dispatching coordinator",
                    "tool_calls": [{"name": "invoke_subagent", "args": {"Role": "Coordinator", "Model": "inherit"}}],
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 3,
                    "type": "GENERIC",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "bf-coord"\n}',
                }) + "\n")

            # 2. Coordinator (dispatches leaf)
            coord_dir = brain_dir / "bf-coord"
            (coord_dir / ".system_generated" / "logs").mkdir(parents=True)
            with open(coord_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
                f.write(json.dumps({
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "content": "[Message] timestamp=2026-10-20T10:00:06Z sender=bf-root recipient=bf-coord\nCoordinate work",
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Dispatching leaf",
                    "tool_calls": [{"name": "invoke_subagent", "args": {"Role": "Leaf", "Model": "inherit"}}],
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 3,
                    "type": "GENERIC",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "bf-leaf"\n}',
                }) + "\n")

            # 3. Leaf
            leaf_dir = brain_dir / "bf-leaf"
            (leaf_dir / ".system_generated" / "logs").mkdir(parents=True)
            with open(leaf_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
                f.write(json.dumps({
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "content": "[Message] timestamp=2026-10-20T10:00:10Z sender=bf-coord recipient=bf-leaf\nDo leaf work",
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Done leaf work",
                    "est_tokens": 1000,
                }) + "\n")

            backfill.run_backfill(brain_dir=brain_dir, db_path=db_file, force=True)

            conn = get_db_connection(db_file)
            r_row = conn.execute("SELECT model_name, depth FROM conversations WHERE id = 'bf-root'").fetchone()
            c_row = conn.execute("SELECT model_name, depth, parent_id, root_id FROM conversations WHERE id = 'bf-coord'").fetchone()
            l_row = conn.execute("SELECT model_name, depth, parent_id, root_id, billed_cost FROM conversations WHERE id = 'bf-leaf'").fetchone()
            l_step = conn.execute("SELECT model_name, billed_cost FROM steps WHERE conversation_id = 'bf-leaf' AND step_index = 2").fetchone()
            conn.close()

            self.assertEqual(r_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(r_row["depth"], 0)
            self.assertEqual(c_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(c_row["depth"], 1)
            self.assertEqual(c_row["parent_id"], "bf-root")
            self.assertEqual(c_row["root_id"], "bf-root")
            self.assertEqual(l_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertEqual(l_row["depth"], 2)
            self.assertEqual(l_row["parent_id"], "bf-coord")
            self.assertEqual(l_row["root_id"], "bf-root")
            self.assertEqual(l_step["model_name"], "Gemini 4 Argon (Medium)")
            self.assertAlmostEqual(l_row["billed_cost"], 0.010, places=3)
            self.assertAlmostEqual(l_step["billed_cost"], 0.010, places=3)

    def test_backfill_single_conversation_id_resolves_inherited_model(self):
        """Verify backfilling a single conversation_id discovers parent in brain_dir and inherits model."""
        import scripts.backfill_analytics as backfill

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            brain_dir = tmp_path / "brain"
            brain_dir.mkdir(parents=True)
            db_file = tmp_path / "test_analytics.db"

            # Setup parent in brain_dir (Argon, dispatches child with 'pro')
            p_dir = brain_dir / "p-single"
            (p_dir / ".system_generated" / "logs").mkdir(parents=True)
            with open(p_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
                f.write(json.dumps({
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from `Gemini 3.8 Flash (High)` to `Gemini 4 Argon (Medium)`.\n</USER_SETTINGS_CHANGE>",
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Dispatching worker",
                    "tool_calls": [{"name": "invoke_subagent", "args": {"Role": "Worker", "Model": "pro"}}],
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 3,
                    "type": "GENERIC",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "c-single"\n}',
                }) + "\n")

            # Setup child in brain_dir
            c_dir = brain_dir / "c-single"
            (c_dir / ".system_generated" / "logs").mkdir(parents=True)
            with open(c_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
                f.write(json.dumps({
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "content": "[Message] timestamp=2026-10-20T10:00:05Z sender=p-single recipient=c-single\nWork",
                }) + "\n")
                f.write(json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "content": "Working",
                    "est_tokens": 1000,
                }) + "\n")

            # Run backfill ONLY on c-single
            backfill.run_backfill(brain_dir=brain_dir, conversation_id="c-single", db_path=db_file, force=True)

            conn = get_db_connection(db_file)
            c_row = conn.execute("SELECT model_name, billed_cost FROM conversations WHERE id = 'c-single'").fetchone()
            conn.close()

            self.assertIsNotNone(c_row)
            self.assertEqual(c_row["model_name"], "Gemini 4 Argon (Medium)")
            self.assertAlmostEqual(c_row["billed_cost"], 0.010, places=3)

    def test_subagent_module_exports_and_false_positive_filtering(self):
        """Test subagent parsing helpers, ID validation, and false-positive filtering."""
        from context_monitor.subagent import (
            is_valid_subagent_id,
            is_subagent_dispatch_output,
            is_manage_subagents_output,
            extract_invoked_subagent_ids,
            parse_manage_subagents_output,
        )
        from context_monitor.pricing import recompute_step_costs

        # 1. Dispatch output filtering
        self.assertFalse(
            is_subagent_dispatch_output("read_file", '829: or "Created the following subagent" in content')
        )
        self.assertFalse(
            is_subagent_dispatch_output("view_file", 'Created the following subagent: {"conversationId": "child-1"}')
        )
        self.assertFalse(
            is_subagent_dispatch_output("", "Created At: 2026-10-28\nShowing lines 1-50 of 100\n10: Created the following subagent")
        )
        self.assertFalse(
            is_subagent_dispatch_output("", "File Path: /some/path\nCreated the following subagent: child-1")
        )
        self.assertFalse(
            is_subagent_dispatch_output("", "Command exited with code 0.\nCreated the following subagents:\n- child-1")
        )
        self.assertTrue(
            is_subagent_dispatch_output(
                "invoke_subagent",
                'Created the following subagent:\n{"conversationId": "49a774e0-203d-4fa8-9d10-edc5b945fe77"}',
            )
        )
        self.assertTrue(
            is_subagent_dispatch_output(
                "",
                'Created the following subagents:\n- DeepCoder Coordinator (conversationId: child-456, type: self)',
            )
        )

        # 2. Manage subagents output filtering
        self.assertFalse(is_manage_subagents_output("view_file", "active subagent list"))
        self.assertFalse(is_manage_subagents_output("read_file", "Showing lines 1-10 of 20\nactive subagent"))
        self.assertTrue(is_manage_subagents_output("manage_subagents", "active subagent(s):\n- child-1"))
        self.assertTrue(is_manage_subagents_output("", "active subagent(s):\n- child-1"))

        # 3. ID Validation
        self.assertTrue(is_valid_subagent_id("49a774e0-203d-4fa8-9d10-edc5b945fe77"))
        self.assertTrue(is_valid_subagent_id("child-1"))
        self.assertTrue(is_valid_subagent_id("sub-pro-downgraded"))
        self.assertTrue(is_valid_subagent_id("wave-child-1"))

        self.assertFalse(is_valid_subagent_id(""))
        self.assertFalse(is_valid_subagent_id("s"))
        self.assertFalse(is_valid_subagent_id("1104"))
        self.assertFalse(is_valid_subagent_id("cid_found"))
        self.assertFalse(is_valid_subagent_id("step_index"))
        self.assertFalse(is_valid_subagent_id("transcripts"))
        self.assertFalse(is_valid_subagent_id("TRANSCRIPTS"))
        self.assertFalse(is_valid_subagent_id("directory"))
        self.assertFalse(is_valid_subagent_id("asc"))
        self.assertFalse(is_valid_subagent_id("DESC"))
        self.assertFalse(is_valid_subagent_id("text"))
        self.assertFalse(is_valid_subagent_id("subagents"))

        # 4. Extract invoked subagent IDs
        self.assertEqual(
            extract_invoked_subagent_ids("Found 535 total conversation transcripts and conversation directory 1104"),
            [],
        )
        self.assertEqual(
            parse_manage_subagents_output("active subagent(s): SELECT id FROM conversations ORDER BY id ASC, id DESC"),
            [],
        )
        self.assertEqual(
            extract_invoked_subagent_ids('Created the following subagent:\n{\n  "conversationId": "child-1"\n}'),
            ["child-1"],
        )
        self.assertEqual(
            extract_invoked_subagent_ids(
                'Created the following subagents:\n- DeepCoder Coordinator (conversationId: child-456, type: self)'
            ),
            ["child-456"],
        )

    def test_pricing_recompute_step_costs(self):
        """Test pricing.recompute_step_costs with checkpoints, ephemerals, and planners."""
        from context_monitor.pricing import recompute_step_costs

        steps = [
            {"step_type": "USER_INPUT", "est_tokens": 500, "model_name": "Gemini 3.8 Flash (High)"},
            {"step_type": "PLANNER_RESPONSE", "est_tokens": 1000, "model_name": "Gemini 3.8 Flash (High)"},
            {"step_type": "EPHEMERAL_MESSAGE", "est_tokens": 200},
            {"step_type": "PLANNER_RESPONSE", "est_tokens": 800, "model_name": "Gemini 3.8 Flash (High)"},
            {"step_type": "CHECKPOINT", "est_tokens": 1200},  # Compaction from 2300 down to 1200
            {"step_type": "PLANNER_RESPONSE", "est_tokens": 400, "model_name": "Gemini 3.8 Flash (High)"},
        ]

        res = recompute_step_costs(steps, "Gemini 3.8 Flash (High)")
        self.assertEqual(res["compaction_count"], 1)
        self.assertEqual(res["pruned_tokens"], 1100)  # 2300 - 1200
        self.assertEqual(res["active_context_tokens"], 1600)  # 1200 + 400
        self.assertGreater(res["billed_cost"], 0.0)
        self.assertGreater(res["billed_input_tokens"], 0)
        self.assertGreater(res["billed_output_tokens"], 0)

        # Ensure each step has billed fields populated
        for s in steps:
            self.assertIn("billed_cost", s)
            self.assertIn("billed_input_tokens", s)
            self.assertIn("billed_output_tokens", s)

    def test_subagent_helpers_model_application_and_stubs(self):
        """Test apply_step_model_switch, apply_native_step_models_fallback, and upsert_dispatched_subagent_stubs."""
        from context_monitor.subagent import (
            apply_step_model_switch,
            apply_native_step_models_fallback,
            upsert_dispatched_subagent_stubs,
        )
        from context_monitor.pricing import DEFAULT_MODEL

        # 1. apply_step_model_switch
        steps = [
            {"step_index": 1, "model_name": DEFAULT_MODEL},
            {"step_index": 2, "model_name": DEFAULT_MODEL},
        ]
        models_used = []
        model_transitions = []
        first_m, cur_m = apply_step_model_switch(
            "Gemini 4 Argon (Medium)", None, DEFAULT_MODEL, steps, models_used, model_transitions
        )
        self.assertEqual(first_m, "Gemini 4 Argon (Medium)")
        self.assertEqual(cur_m, "Gemini 4 Argon (Medium)")
        self.assertEqual(steps[0]["model_name"], "Gemini 4 Argon (Medium)")
        self.assertEqual(steps[1]["model_name"], "Gemini 4 Argon (Medium)")
        self.assertEqual(models_used, ["Gemini 4 Argon (Medium)"])
        self.assertEqual(model_transitions, ["Gemini 4 Argon (Medium)"])

        # Mid-session switch
        first_m, cur_m = apply_step_model_switch(
            "Gemini 3.8 Flash (Tiered)", first_m, cur_m, steps, models_used, model_transitions
        )
        self.assertEqual(first_m, "Gemini 4 Argon (Medium)")
        self.assertEqual(cur_m, "Gemini 3.8 Flash (Tiered)")
        self.assertEqual(models_used, ["Gemini 4 Argon (Medium)", "Gemini 3.8 Flash (Tiered)"])
        self.assertEqual(model_transitions, ["Gemini 4 Argon (Medium)", "Gemini 3.8 Flash (Tiered)"])

        # 2. apply_native_step_models_fallback
        native_map = {1: "Gemini 3.8 Flash (High)", 3: "Gemini 4 Argon (High)"}
        steps2 = [
            {"step_index": 1, "model_name": "Old"},
            {"step_index": 2, "model_name": "Old"},
            {"step_index": 3, "model_name": "Old"},
            {"step_index": 4, "model_name": "Old"},
        ]
        f_m, c_m, used, trans = apply_native_step_models_fallback(native_map, steps2)
        self.assertEqual(f_m, "Gemini 3.8 Flash (High)")
        self.assertEqual(c_m, "Gemini 4 Argon (High)")
        self.assertEqual(used, ["Gemini 3.8 Flash (High)", "Gemini 4 Argon (High)"])
        self.assertEqual(trans, ["Gemini 3.8 Flash (High)", "Gemini 4 Argon (High)"])
        self.assertEqual(steps2[0]["model_name"], "Gemini 3.8 Flash (High)")
        self.assertEqual(steps2[1]["model_name"], "Gemini 3.8 Flash (High)")
        self.assertEqual(steps2[2]["model_name"], "Gemini 4 Argon (High)")
        self.assertEqual(steps2[3]["model_name"], "Gemini 4 Argon (High)")

        # 3. upsert_dispatched_subagent_stubs
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_stubs.db"
            init_db(db_path)
            conn = get_db_connection(db_path)

            # Insert parent
            conn.execute(
                "INSERT INTO conversations (id, title, root_id, depth, total_steps, model_name) VALUES (?, ?, ?, ?, ?, ?)",
                ("parent-root", "Parent Task", "parent-root", 0, 10, "Gemini 4 Argon (High)"),
            )
            # Insert a bogus stale 0-step stub
            conn.execute(
                "INSERT INTO conversations (id, title, root_id, parent_id, depth, total_steps) VALUES (?, ?, ?, ?, ?, ?)",
                ("stale-stub-1", "Stale Stub", "parent-root", "parent-root", 1, 0),
            )
            # Insert an active child with steps > 0
            conn.execute(
                "INSERT INTO conversations (id, title, root_id, parent_id, depth, total_steps, model_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("active-child-1", "Active Child", "parent-root", "parent-root", 1, 5, "Gemini 3.8 Flash (High)"),
            )
            conn.commit()

            dispatched = [
                {"id": "new-child-1", "role": "Worker 1", "type": "self", "model": "pro", "step_index": 2},
                {"id": "active-child-1", "role": "Active Child", "type": "self", "model": "inherit", "step_index": 1},
            ]

            upsert_dispatched_subagent_stubs(
                conn,
                parent_id="parent-root",
                root_id="parent-root",
                parent_depth=0,
                parent_title="Parent Task",
                parent_project="test-proj",
                parent_model="Gemini 4 Argon (High)",
                dispatched_subagents=dispatched,
            )
            conn.commit()

            # stale-stub-1 should have been deleted because it had total_steps=0 and was not in dispatched
            stale_row = conn.execute("SELECT id FROM conversations WHERE id = 'stale-stub-1'").fetchone()
            self.assertIsNone(stale_row)

            # new-child-1 should have been inserted with pro model -> Gemini 4 Argon (Medium)
            new_row = conn.execute("SELECT id, title, model_name, depth FROM conversations WHERE id = 'new-child-1'").fetchone()
            self.assertIsNotNone(new_row)
            self.assertEqual(new_row["depth"], 1)
            self.assertEqual(new_row["model_name"], "Gemini 4 Argon (Medium)")

            # active-child-1 should have retained its existing model_name because total_steps > 0
            active_row = conn.execute("SELECT id, model_name FROM conversations WHERE id = 'active-child-1'").fetchone()
            self.assertIsNotNone(active_row)
            self.assertEqual(active_row["model_name"], "Gemini 3.8 Flash (High)")

            conn.close()

    def test_ingest_ignores_subagent_strings_inside_read_file_and_prunes_stale_stubs(self):
        """Verify ingest ignores subagent strings inside read_file and prunes stale stubs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            root_cid = "root-agent-123"
            transcript_dir = tmp_path / root_cid / ".system_generated" / "logs"
            transcript_dir.mkdir(parents=True)
            transcript_file = transcript_dir / "transcript.jsonl"

            # Pre-insert a bogus 0-step stub row ("bogus-stub", parent_id=root_cid, total_steps=0)
            conn = get_db_connection(db_file)
            conn.execute(
                "INSERT INTO conversations (id, title, root_id, depth, total_steps, model_name) VALUES (?, ?, ?, ?, ?, ?)",
                (root_cid, "Root Agent", root_cid, 0, 10, "Gemini 4 Argon (High)"),
            )
            conn.execute(
                "INSERT INTO conversations (id, title, root_id, parent_id, depth, total_steps) VALUES (?, ?, ?, ?, ?, ?)",
                ("bogus-stub", "Bogus Child", root_cid, root_cid, 1, 0),
            )
            conn.commit()
            conn.close()

            transcript_lines = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Please read the file and launch subagent",
                    "created_at": "2026-10-28T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Reading file...",
                    "tool_calls": [
                        {
                            "name": "read_file",
                            "args": {
                                "Path": "/tmp/dummy.py",
                            },
                        }
                    ],
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "TOOL",
                    "content": (
                        "Created At: 2026-10-28\n"
                        "Showing lines 1-50 of 100\n"
                        '829: or "Created the following subagent" in content\n'
                        '882: or "active subagent" in content.lower()\n'
                        'Found 535 total conversation transcripts and conversation directory 1104\n'
                        '{"conversationId": "child-fake"}\n'
                    ),
                },
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Launching real subagent...",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Role": "Real Worker",
                                "TypeName": "self",
                                "Prompt": "Do work",
                            },
                        }
                    ],
                },
                {
                    "step_index": 5,
                    "type": "GENERIC",
                    "source": "TOOL",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "child-real"\n}',
                },
            ]
            with open(transcript_file, "w", encoding="utf-8") as f:
                for line in transcript_lines:
                    f.write(json.dumps(line) + "\n")

            analysis = ingest.analyze_transcript(transcript_file, cid=root_cid)
            dispatched = analysis.get("dispatched_subagents", [])
            dispatched_ids = [d["id"] for d in dispatched]
            self.assertEqual(dispatched_ids, ["child-real"])

            ingest.sync_to_sqlite(root_cid, analysis["latest_model"], analysis, db_path=db_file)

            conn = get_db_connection(db_file)
            rows = conn.execute("SELECT id FROM conversations WHERE parent_id = ?", (root_cid,)).fetchall()
            child_ids = [r["id"] for r in rows]
            conn.close()

            self.assertEqual(child_ids, ["child-real"])

    def test_backfill_purges_bogus_zero_step_stubs_and_ignores_read_file_subagent_text(self):
        import scripts.backfill_analytics as backfill

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_file = tmp_path / "test_analytics.db"
            init_db(db_file)

            # Pre-populate analytics.db with bogus 0-step stub rows ("transcripts", "1104", "cid", "and", total_steps=0)
            conn = get_db_connection(db_file)
            bogus_ids = ["transcripts", "1104", "cid", "and"]
            for bid in bogus_ids:
                conn.execute(
                    "INSERT INTO conversations (id, title, total_steps, model_name) VALUES (?, ?, 0, ?)",
                    (bid, f"Bogus {bid}", "Gemini 4 Argon (High)"),
                )
            conn.commit()
            conn.close()

            # Create a conversation in brain_dir that calls read_file on text containing "Created the following subagent"
            # and dispatches one real subagent "sub-valid-1".
            brain_dir = tmp_path / "brain"
            parent_cid = "bf-parent-root"
            parent_dir = brain_dir / parent_cid / ".system_generated" / "logs"
            parent_dir.mkdir(parents=True)
            transcript_file = parent_dir / "transcript.jsonl"

            transcript_lines = [
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Please inspect files and launch worker",
                    "created_at": "2026-10-28T10:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Reading file...",
                    "tool_calls": [
                        {
                            "name": "read_file",
                            "args": {
                                "Path": "/tmp/dummy.py",
                            },
                        }
                    ],
                },
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "source": "TOOL",
                    "content": (
                        "Created At: 2026-10-28\n"
                        "Showing lines 1-50 of 100\n"
                        '829: or "Created the following subagent" in content\n'
                        '882: or "active subagent" in content.lower()\n'
                        'Found 535 total conversation transcripts and conversation directory 1104\n'
                        '{"conversationId": "fake-subagent-from-file"}\n'
                    ),
                },
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "Launching real worker...",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Role": "Real Worker",
                                "TypeName": "self",
                                "Prompt": "Do work",
                            },
                        }
                    ],
                },
                {
                    "step_index": 5,
                    "type": "GENERIC",
                    "source": "TOOL",
                    "content": 'Created the following subagent:\n{\n  "conversationId": "sub-valid-1"\n}',
                },
            ]
            with open(transcript_file, "w", encoding="utf-8") as f:
                for line in transcript_lines:
                    f.write(json.dumps(line) + "\n")

            backfill.run_backfill(brain_dir=brain_dir, db_path=db_file, force=True)

            conn = get_db_connection(db_file)
            rows = conn.execute("SELECT id, total_steps FROM conversations").fetchall()
            conv_ids = {r["id"] for r in rows}
            conn.close()

            # Assert that "transcripts", "1104", "cid", and "and" are deleted
            for bid in bogus_ids:
                self.assertNotIn(bid, conv_ids)
            # Assert "fake-subagent-from-file" was NOT created as a stub
            self.assertNotIn("fake-subagent-from-file", conv_ids)
            # Assert parent and "sub-valid-1" are present
            self.assertIn(parent_cid, conv_ids)
            self.assertIn("sub-valid-1", conv_ids)


if __name__ == "__main__":
    unittest.main()


