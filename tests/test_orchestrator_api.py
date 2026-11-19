from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from context_monitor.dashboard.api_routes import (
    query_conversations_list,
    query_summary_data,
)
from context_monitor.db import get_db_connection, init_db


def test_query_summary_data_unfiltered_null_step_model_falls_back_to_conversation_model(
    tmp_path: Path,
):
    db_file = tmp_path / "test_summary_null_step_model.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute(
        """
        INSERT INTO conversations (
            id, title, project, model_name, created_at, total_steps, total_tokens
        ) VALUES
        ('c-neon', 'Neon Conv', 'proj-a', 'Orchestrator (Neon)', '2026-11-18T10:00:00Z', 1, 5000),
        ('c-blank', 'Blank Model Conv', NULL, '   ', '2026-11-18T10:05:00Z', 1, 300)
        """
    )
    conn.execute(
        """
        INSERT INTO steps (
            conversation_id, step_index, model_name, est_tokens,
            billed_input_tokens, billed_output_tokens, created_at
        ) VALUES
        ('c-neon', 1, NULL, 5000, 4000, 1000, '2026-11-18T10:00:05Z'),
        ('c-blank', 1, '', 300, 200, 100, '2026-11-18T10:05:05Z')
        """
    )
    conn.commit()

    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        summary = query_summary_data(conn, project=None)
        summary_proj = query_summary_data(conn, project="proj-a")
        summary_unassigned = query_summary_data(conn, project="(unassigned)")

    conn.close()

    by_model = {m["model_name"]: m for m in summary["models"]}
    assert "Orchestrator (Neon)" in by_model
    assert by_model["Orchestrator (Neon)"]["model_family"] == "Gemini 4 Neon"
    assert by_model["Orchestrator (Neon)"]["tokens"] == 5000
    assert by_model["Orchestrator (Neon)"]["steps"] == 1

    assert "Gemini Flash" in by_model
    assert by_model["Gemini Flash"]["tokens"] == 300
    assert by_model["Gemini Flash"]["steps"] == 1

    by_proj_model = {m["model_name"]: m for m in summary_proj["models"]}
    assert list(by_proj_model.keys()) == ["Orchestrator (Neon)"]
    assert by_proj_model["Orchestrator (Neon)"]["model_family"] == "Gemini 4 Neon"
    assert by_proj_model["Orchestrator (Neon)"]["tokens"] == 5000

    by_unassigned_model = {m["model_name"]: m for m in summary_unassigned["models"]}
    assert list(by_unassigned_model.keys()) == ["Gemini Flash"]
    assert by_unassigned_model["Gemini Flash"]["tokens"] == 300


def test_query_conversations_list_returns_models_used_and_model_families_for_root_and_subagents(
    tmp_path: Path,
):
    db_file = tmp_path / "test_conv_model_families.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    conn.execute(
        """
        INSERT INTO conversations (
            id, title, project, model_name, models_used, created_at, updated_at,
            parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index
        ) VALUES
        (
            'root-orch',
            'Orchestrator Campaign',
            'proj-a',
            'Orchestrator (Neon) → Gemini 4 Hydrogen (Orchestrator)',
            'Orchestrator (Neon), Gemini 4 Hydrogen (Orchestrator)',
            '2026-11-18T10:00:00Z',
            '2026-11-18T10:30:00Z',
            NULL,
            'root-orch',
            0,
            NULL,
            NULL,
            NULL
        ),
        (
            'sub-hyd',
            'Hydrogen Worker',
            'proj-a',
            'Gemini 4 Hydrogen (Orchestrator)',
            'Gemini 4 Hydrogen (Orchestrator)',
            '2026-11-18T10:05:00Z',
            '2026-11-18T10:20:00Z',
            'root-orch',
            'root-orch',
            1,
            'Worker',
            'self',
            2
        ),
        (
            'root-trunc',
            'Truncated Switch Chain',
            'proj-a',
            'Orchestrator (Neon) → ... → Gemini 4 Hydrogen (Orchestrator) -> Gemini 4 Hydrogen (High)',
            NULL,
            '2026-11-18T11:00:00Z',
            '2026-11-18T11:10:00Z',
            NULL,
            'root-trunc',
            0,
            NULL,
            NULL,
            NULL
        )
        """
    )
    conn.commit()

    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        hier_convs = query_conversations_list(conn, view_mode="hierarchy")
        flat_convs = query_conversations_list(conn, view_mode="flat")

    conn.close()

    root_orch = next(c for c in hier_convs if c["id"] == "root-orch")
    assert (
        root_orch["models_used"]
        == "Orchestrator (Neon), Gemini 4 Hydrogen (Orchestrator)"
    )
    assert root_orch["model_families"] == ["Gemini 4 Neon", "Gemini 4 Hydrogen"]
    assert len(root_orch["subagents"]) == 1
    sub = root_orch["subagents"][0]
    assert sub["id"] == "sub-hyd"
    assert sub["models_used"] == "Gemini 4 Hydrogen (Orchestrator)"
    assert sub["model_families"] == ["Gemini 4 Hydrogen"]

    root_trunc = next(c for c in hier_convs if c["id"] == "root-trunc")
    assert root_trunc["model_families"] == ["Gemini 4 Neon", "Gemini 4 Hydrogen"]

    flat_sub = next(c for c in flat_convs if c["id"] == "sub-hyd")
    assert flat_sub["model_families"] == ["Gemini 4 Hydrogen"]
