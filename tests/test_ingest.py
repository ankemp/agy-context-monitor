"""
Tests for Ingestion Hook, Ephemeral Indicator, Artifact Reports & Backfill Parity
---------------------------------------------------------------------------------
Covers:
1. Indicator line formatting with live quota, cached quota, quota fallback, turn %, and 5h limit.
2. Clean markdown artifact reports with Subscription & Quota Impact section and zero emojis.
3. Historical backfill script execution and quota_state initialization.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

import pytest
from context_monitor.db import get_db_connection, init_db, upsert_quota_state
from scripts.backfill_analytics import run_backfill
from scripts.ingest import (
    _extract_bucket_view,
    analyze_transcript,
    format_ephemeral_status,
    generate_artifact_report,
    generate_context_usage_report,
    main,
    sync_to_sqlite,
)


def test_format_ephemeral_status_live_quota():
    """Verifies indicator line formatting with active live quota metrics."""
    quota_metrics = {
        "weekly_pct": 0.31,
        "five_hour_pct": 0.88,
        "weekly_remaining_pct": 93.5,
        "five_hour_remaining_pct": 95.9,
        "weekly_reset_countdown": "6d 8h",
        "five_hour_reset_countdown": "4h 19m",
    }
    msg = format_ephemeral_status(
        active_pct=14.9,
        active_tokens=148800,
        active_max=1000000,
        added_tok=3400,
        turn_cost=2.09,
        quota_metrics=quota_metrics,
    )
    assert "Context: 14.9% (148.8k / 1.00M tokens) · +3.4k tok this turn" in msg
    assert "Session: 0.31% wk ($2.09)" in msg
    assert "Quota: 93.5% wk (6d 8h)" in msg
    # 5h remaining is 95.9% (>= 25%), so 5h limit is not appended
    assert "5h Limit" not in msg


def test_format_ephemeral_status_cached_quota():
    """Verifies indicator line formatting when using cached quota values from SQLite."""
    quota_metrics = {
        "session_weekly_pct": 0.45,
        "five_hour_pct": 1.2,
        "weekly_remaining_pct": 88.2,
        "five_hour_remaining_pct": 74.0,
        "weekly_reset_countdown": "5d 2h",
        "five_hour_reset_countdown": "3h 10m",
    }
    msg = format_ephemeral_status(
        active_pct=25.0,
        active_tokens=250000,
        active_max=1000000,
        added_tok=0,
        turn_cost=3.02,
        quota_metrics=quota_metrics,
    )
    assert "Context: 25.0% (250.0k / 1.00M tokens)" in msg
    assert "Session: 0.45% wk ($3.02)" in msg
    assert "Quota: 88.2% wk (5d 2h)" in msg


def test_format_ephemeral_status_quota_fallback():
    """Verifies graceful fallback to Session Value: $X.XX when quota is unavailable."""
    # When quota_metrics is None
    msg_none = format_ephemeral_status(
        active_pct=10.0,
        active_tokens=100000,
        active_max=1000000,
        added_tok=1500,
        turn_cost=1.50,
        quota_metrics=None,
    )
    assert (
        "Context: 10.0% (100.0k / 1.00M tokens) · +1.5k tok this turn · Session Value: $1.50"
        == msg_none
    )

    # When quota_metrics is empty
    msg_empty = format_ephemeral_status(
        active_pct=10.0,
        active_tokens=100000,
        active_max=1000000,
        added_tok=0,
        turn_cost=1.50,
        quota_metrics={},
    )
    assert "Context: 10.0% (100.0k / 1.00M tokens) · Session Value: $1.50" == msg_empty

    # When quota_metrics lacks reset time / countdown (offline / uncalibrated fallback)
    msg_offline = format_ephemeral_status(
        active_pct=5.0,
        active_tokens=50000,
        active_max=1000000,
        added_tok=0,
        turn_cost=0.50,
        quota_metrics={
            "weekly_remaining_pct": 100.0,
            "weekly_reset_countdown": "",
            "weekly_reset_time": "",
        },
    )
    assert "Context: 5.0% (50.0k / 1.00M tokens) · Session Value: $0.50" == msg_offline


def test_format_ephemeral_status_with_turn_percentage():
    """Verifies optional turn % is rendered when turn_wk_pct is provided."""
    quota_metrics = {
        "session_weekly_pct": 0.50,
        "weekly_remaining_pct": 92.0,
        "weekly_reset_countdown": "6d 0h",
    }
    msg = format_ephemeral_status(
        active_pct=12.0,
        active_tokens=120000,
        active_max=1000000,
        added_tok=800,
        turn_cost=3.36,
        quota_metrics=quota_metrics,
        turn_wk_pct=0.08,
    )
    assert "Session: 0.50% wk ($3.36)" in msg
    assert "+0.08% turn" in msg
    assert "Quota: 92.0% wk (6d 0h)" in msg


def test_format_ephemeral_status_5h_limit_warning():
    """Verifies that 5-hour limit warning is appended when remaining fraction drops below 25%."""
    quota_metrics = {
        "session_weekly_pct": 0.31,
        "weekly_remaining_pct": 93.5,
        "five_hour_remaining_pct": 24.1,
        "weekly_reset_countdown": "6d 8h",
        "five_hour_reset_countdown": "1h 12m",
    }
    msg = format_ephemeral_status(
        active_pct=14.9,
        active_tokens=148800,
        active_max=1000000,
        added_tok=3400,
        turn_cost=2.09,
        quota_metrics=quota_metrics,
    )
    assert "Quota: 93.5% wk (6d 8h)" in msg
    assert "5h Limit: 24.1% (1h 12m)" in msg


def test_format_ephemeral_status_compaction_and_warning():
    """Verifies compaction detected and capacity warning messages take precedence."""
    diff_res = MagicMock()
    diff_res.compaction_detected = True
    diff_res.pruned_step_indices = [2, 3, 4]
    diff_res.removed_tokens = 5000
    diff_res.removed = [1, 2, 3]

    msg_comp = format_ephemeral_status(
        active_pct=40.0,
        active_tokens=400000,
        active_max=1000000,
        diff_res=diff_res,
    )
    assert "Context Compaction Detected: Pruned steps #2–#4" in msg_comp

    msg_warn = format_ephemeral_status(
        active_pct=85.0,
        active_tokens=850000,
        active_max=1000000,
    )
    assert "Context Warning: Window at 85.0% capacity" in msg_warn


def test_format_ephemeral_status_replaces_session_with_fleet_when_subagents_present():
    quota_metrics = {
        "weekly_remaining_pct": 75.0,
        "weekly_reset_countdown": "4d 2h",
        "session_weekly_pct": 0.25,
    }
    msg = format_ephemeral_status(
        active_pct=15.0,
        active_tokens=150_000,
        active_max=1_000_000,
        added_tok=2_500,
        turn_cost=0.10,
        session_cost=1.68,
        quota_metrics=quota_metrics,
        fleet_cost=5.38,
        fleet_wk_pct=0.80,
        fleet_agent_count=4,
    )
    assert "Fleet (4): 0.80% wk ($5.38)" in msg
    assert "Session:" not in msg


def test_format_ephemeral_status_fleet_fallback_no_quota():
    msg = format_ephemeral_status(
        active_pct=10.0,
        active_tokens=100_000,
        active_max=1_000_000,
        added_tok=0,
        turn_cost=1.00,
        session_cost=1.00,
        quota_metrics=None,
        fleet_cost=3.50,
        fleet_agent_count=3,
    )
    assert "Fleet (3) Value: $3.50" in msg
    assert "Session Value:" not in msg


def test_format_ephemeral_status_single_agent_retains_session_prefix():
    quota_metrics = {
        "weekly_remaining_pct": 75.0,
        "weekly_reset_countdown": "4d 2h",
        "session_weekly_pct": 0.25,
    }
    msg = format_ephemeral_status(
        active_pct=15.0,
        active_tokens=150_000,
        active_max=1_000_000,
        added_tok=0,
        turn_cost=0.10,
        session_cost=1.68,
        quota_metrics=quota_metrics,
        fleet_cost=1.68,
        fleet_agent_count=1,
    )
    assert "Session: 0.25% wk ($1.68)" in msg
    assert "Fleet (" not in msg


def test_format_ephemeral_status_subagents_without_fleet_cost_retains_session_prefix():
    quota_metrics = {
        "weekly_remaining_pct": 75.0,
        "weekly_reset_countdown": "4d 2h",
        "session_weekly_pct": 0.25,
    }
    msg = format_ephemeral_status(
        active_pct=15.0,
        active_tokens=150_000,
        active_max=1_000_000,
        added_tok=0,
        turn_cost=0.10,
        session_cost=1.68,
        quota_metrics=quota_metrics,
        fleet_cost=None,
        fleet_agent_count=4,
    )
    assert "Session: 0.25% wk ($1.68)" in msg
    assert "Fleet (" not in msg


def test_artifact_report_subscription_and_quota_section_no_emojis(tmp_path: Path):
    """Verifies artifact markdown report includes Subscription & Quota Impact without emojis."""
    analysis = {
        "title": "Test Session",
        "start_time": "2026-10-14T10:00:00Z",
        "end_time": "2026-10-14T10:30:00Z",
        "duration_seconds": 1800,
        "total_chars": 50000,
        "total_tokens": 12000,
        "active_context_tokens": 12000,
        "billed_cost": 2.09,
        "billed_input_tokens": 8000,
        "billed_output_tokens": 4000,
        "latest_model": "Gemini 3.8 Flash (High)",
        "latest_max_tokens": 1000000,
        "category_stats": {"Code Action: Edits": 30000, "System Instructions": 20000},
        "steps": [
            {
                "step_index": 1,
                "created_at": "2026-10-14T10:00:00Z",
                "source": "AI",
                "step_type": "PLANNER_RESPONSE",
                "category": "Code Action: Edits",
                "activity": "Editing code",
                "tool_or_source": "replace_file_content",
                "characters": 30000,
                "est_tokens": 8000,
                "detail": "Fixing bug",
                "billed_cost": 2.09,
            }
        ],
        "quota_state": {
            "gemini-weekly": {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "remaining_fraction": 0.9348,
                "reset_time": "2026-10-21T02:27:22Z",
                "calibrated_capacity": 672.0,
            },
            "gemini-5h": {
                "bucket_id": "gemini-5h",
                "group_name": "Gemini Models",
                "remaining_fraction": 0.9591,
                "reset_time": "2026-10-14T14:00:00Z",
                "calibrated_capacity": 236.7,
            },
            "3p-weekly": {
                "bucket_id": "3p-weekly",
                "group_name": "Claude and GPT models",
                "remaining_fraction": 1.0,
                "reset_time": "2026-10-21T02:27:22Z",
                "calibrated_capacity": 200.0,
            },
            "3p-5h": {
                "bucket_id": "3p-5h",
                "group_name": "Claude and GPT models",
                "remaining_fraction": 1.0,
                "reset_time": "2026-10-14T14:00:00Z",
                "calibrated_capacity": 70.0,
            },
        },
    }

    report_md = generate_context_usage_report(
        cid="test-conv-quota",
        model_name="Gemini 3.8 Flash (High)",
        max_tokens=1000000,
        analysis=analysis,
        artifact_dir=tmp_path,
    )

    # 1. Check required section header and items
    assert "## Subscription & Quota Impact" in report_md
    assert "- Turn Cost: `$2.09` (`0.31%` of weekly quota)" in report_md
    assert "- Session Cost: `$2.09` (`0.31%` of weekly quota)" in report_md
    assert "- Subscription Quota Status:" in report_md
    assert "- Gemini Weekly: `93.5%` remaining (" in report_md
    assert "calibrated capacity `$672.00`)" in report_md
    assert "- Gemini 5-Hour: `95.9%` remaining (" in report_md
    assert "- Claude & GPT Weekly: `100.0%` remaining (" in report_md
    assert "- Claude & GPT 5-Hour: `100.0%` remaining (" in report_md

    # 2. Check alias generate_artifact_report works identically
    assert generate_artifact_report is generate_context_usage_report
    report_file = tmp_path / "context_usage.md"
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == report_md

    # 3. Core Invariant: Clean markdown - zero decorative emojis in the markdown report
    # Common emoji Unicode blocks: Emoticons, Symbols, Pictographs, Transport, Misc
    emoji_pattern = re.compile(
        r"[\U0001F600-\U0001F64F"  # Emoticons
        r"\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
        r"\U0001F680-\U0001F6FF"  # Transport and Map
        r"\U0001F1E0-\U0001F1FF"  # Flags
        r"\U00002702-\U000027B0"  # Dingbats
        r"\U000024C2-\U0001F251"
        r"\U0001F900-\U0001F9FF"  # Supplemental Symbols
        r"\U0001FA00-\U0001FA6F"  # Chess Symbols
        r"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        r"]+",
        flags=re.UNICODE,
    )
    emoji_matches = emoji_pattern.findall(report_md)
    assert emoji_matches == [], f"Found emojis in markdown report: {emoji_matches}"


def test_backfill_analytics_runs_cleanly(tmp_path: Path):
    """Verifies that run_backfill initializes quota_state and runs cleanly without errors."""
    test_db = tmp_path / "backfill_test.db"
    test_brain = tmp_path / "brain"
    test_conv = test_brain / "test-cid-backfill"
    test_conv.mkdir(parents=True)

    # Write a minimal transcript.jsonl
    transcript_file = test_conv / "transcript.jsonl"
    steps = [
        {"step_index": 0, "type": "USER_INPUT", "content": "Hello", "est_tokens": 10},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "Hi there! Let's code.",
            "est_tokens": 20,
            "characters": 25,
            "created_at": "2026-10-14T10:00:00Z",
        },
    ]
    with open(transcript_file, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(s) + "\n" for s in steps)

    # Run backfill targeted at test_brain and test_db
    run_backfill(brain_dir=test_brain, db_path=test_db)

    # Verify database has conversations, steps, and initialized quota_state table
    with get_db_connection(test_db) as conn:
        c_count = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
        assert c_count == 1
        s_count = conn.execute("SELECT count(*) FROM steps").fetchone()[0]
        assert s_count == 2

        # Check quota_state table exists and has rows
        quota_rows = conn.execute(
            "SELECT bucket_id, calibrated_capacity FROM quota_state"
        ).fetchall()
        b_ids = [r["bucket_id"] for r in quota_rows]
        assert "gemini-weekly" in b_ids
        assert "gemini-5h" in b_ids
        assert "3p-weekly" in b_ids
        assert "3p-5h" in b_ids


def test_sync_to_sqlite_attaches_quota_state(tmp_path: Path):
    """Verifies sync_to_sqlite populates analysis['quota_state']."""
    test_db = tmp_path / "sync_quota_test.db"
    init_db(test_db)

    import scripts.ingest as ingest_mod

    orig_db = ingest_mod.DEFAULT_DB_PATH
    try:
        ingest_mod.DEFAULT_DB_PATH = test_db
        analysis = {
            "title": "Sync Quota Test",
            "start_time": "2026-10-14T10:00:00Z",
            "end_time": "2026-10-14T10:05:00Z",
            "duration_seconds": 300,
            "total_chars": 1000,
            "total_tokens": 250,
            "active_context_tokens": 250,
            "billed_cost": 0.50,
            "billed_input_tokens": 200,
            "billed_output_tokens": 50,
            "latest_model": "Gemini 3.8 Flash (High)",
            "category_stats": {},
            "steps": [
                {
                    "step_index": 1,
                    "created_at": "2026-10-14T10:00:00Z",
                    "source": "AI",
                    "step_type": "PLANNER_RESPONSE",
                    "category": "Code Action: Edits",
                    "activity": "Editing code",
                    "tool_or_source": "replace_file_content",
                    "characters": 1000,
                    "est_tokens": 250,
                    "detail": "Test",
                    "billed_cost": 0.50,
                    "billed_input_tokens": 200,
                    "billed_output_tokens": 50,
                }
            ],
            "tool_executions": [],
            "parsed_blocks": [],
        }
        sync_to_sqlite("conv-sync-quota", "Gemini 3.8 Flash (High)", analysis)
        assert "quota_state" in analysis
        assert analysis["quota_state"] is not None
        assert "gemini-weekly" in analysis["quota_state"]
        assert "activity_profile" in analysis
        assert analysis["activity_profile"] is not None
        assert "matrix" in analysis["activity_profile"]
    finally:
        ingest_mod.DEFAULT_DB_PATH = orig_db


def test_sync_to_sqlite_commits_with_warm_quota_cache(tmp_path: Path):
    """Verifies sync_to_sqlite commits conversation and step records even when quota cache is warm."""
    from datetime import datetime, timezone

    test_db = tmp_path / "sync_warm_quota_test.db"
    init_db(test_db)

    now_iso = datetime.now(timezone.utc).isoformat()
    warm_rows = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.89,
            "reset_time": "2026-10-21T00:00:00Z",
            "calibrated_capacity": 672.0,
            "fetched_at": now_iso,
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.78,
            "reset_time": "2026-10-15T00:00:00Z",
            "calibrated_capacity": 236.7,
            "fetched_at": now_iso,
        },
    ]
    with get_db_connection(test_db) as conn:
        upsert_quota_state(conn, warm_rows)

    import scripts.ingest as ingest_mod

    orig_db = ingest_mod.DEFAULT_DB_PATH
    try:
        ingest_mod.DEFAULT_DB_PATH = test_db
        analysis = {
            "title": "Warm Cache Commit Test",
            "start_time": now_iso,
            "end_time": now_iso,
            "duration_seconds": 120,
            "total_chars": 2000,
            "total_tokens": 500,
            "active_context_tokens": 500,
            "billed_cost": 0.15,
            "billed_input_tokens": 400,
            "billed_output_tokens": 100,
            "latest_model": "Gemini 3.8 Flash (High)",
            "category_stats": {"User Requests": 500},
            "steps": [
                {
                    "step_index": 0,
                    "created_at": now_iso,
                    "source": "USER_EXPLICIT",
                    "step_type": "USER_INPUT",
                    "category": "User Requests",
                    "activity": "User Prompt",
                    "tool_or_source": "User",
                    "characters": 500,
                    "est_tokens": 125,
                    "detail": "Test prompt",
                    "billed_cost": 0.0,
                    "billed_input_tokens": 0,
                    "billed_output_tokens": 0,
                }
            ],
            "tool_executions": [],
            "parsed_blocks": [],
        }
        sync_to_sqlite("conv-warm-cache-1", "Gemini 3.8 Flash (High)", analysis)

        with get_db_connection(test_db) as verify_conn:
            conv_row = verify_conn.execute(
                "SELECT * FROM conversations WHERE id = 'conv-warm-cache-1'"
            ).fetchone()
            assert conv_row is not None, "Conversation row was rolled back!"
            assert conv_row["title"] == "Warm Cache Commit Test"
            assert conv_row["total_tokens"] == 500

            step_row = verify_conn.execute(
                "SELECT * FROM steps WHERE conversation_id = 'conv-warm-cache-1'"
            ).fetchone()
            assert step_row is not None, "Step row was rolled back!"
            assert step_row["step_index"] == 0
    finally:
        ingest_mod.DEFAULT_DB_PATH = orig_db


def test_ingest_main_e2e_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """Verifies main() reads JSON from stdin, runs silently, and outputs valid ephemeralMessage JSON."""
    test_db = tmp_path / "main_e2e.db"
    init_db(test_db)
    monkeypatch.setattr("scripts.ingest.DEFAULT_DB_PATH", test_db)

    transcript_file = tmp_path / "transcript.jsonl"
    steps = [
        {"step_index": 0, "type": "USER_INPUT", "content": "How's the weather?"},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "Sunny and 72 degrees.",
            "characters": 25,
            "est_tokens": 10,
        },
    ]
    with open(transcript_file, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(s) + "\n" for s in steps)

    input_payload = json.dumps(
        {
            "conversationId": "test-e2e-main",
            "transcriptPath": str(transcript_file),
        }
    )

    monkeypatch.setattr("sys.stdin", MagicMock(read=lambda: input_payload))
    with patch(
        "json.load",
        return_value={
            "conversationId": "test-e2e-main",
            "transcriptPath": str(transcript_file),
        },
    ):
        main()

    captured = capsys.readouterr()
    out = json.loads(captured.out.strip())
    assert "injectSteps" in out
    assert len(out["injectSteps"]) == 1
    assert "ephemeralMessage" in out["injectSteps"][0]
    msg = out["injectSteps"][0]["ephemeralMessage"]
    assert "Context:" in msg
    assert "Quota:" in msg or "Session Value:" in msg


def test_format_ephemeral_status_with_session_cost_kwarg():
    """Verifies that format_ephemeral_status accepts session_cost as a keyword argument."""
    msg = format_ephemeral_status(
        active_pct=20.0,
        active_tokens=200000,
        active_max=1000000,
        session_cost=4.50,
        quota_metrics={
            "session_weekly_pct": 0.67,
            "weekly_remaining_pct": 90.0,
            "weekly_reset_countdown": "5d 0h",
        },
    )
    assert "Session: 0.67% wk ($4.50)" in msg
    assert "Quota: 90.0% wk (5d 0h)" in msg


def test_format_ephemeral_status_computes_countdown_from_reset_time_if_missing():
    """Verifies that countdown is computed on the fly if weekly_reset_countdown is missing."""
    qm = {
        "weekly_remaining_pct": 91.5,
        "weekly_reset_time": "2026-10-21T02:27:22Z",
        "five_hour_remaining_pct": 20.0,
        "five_hour_reset_time": "2026-10-14T18:00:00Z",
    }
    msg = format_ephemeral_status(
        active_pct=10.0,
        active_tokens=100000,
        active_max=1000000,
        quota_metrics=qm,
    )
    assert "Quota: 91.5% wk (" in msg
    assert "5h Limit: 20.0% (" in msg


def test_generate_context_usage_report_plain_list_and_camelcase_buckets():
    """Verifies artifact report correctly processes plain list quota_state and camelCase ConnectRPC keys."""
    analysis = {
        "title": "Plain List Test",
        "start_time": "2026-10-14T10:00:00Z",
        "end_time": "2026-10-14T10:15:00Z",
        "duration_seconds": 900,
        "total_chars": 20000,
        "total_tokens": 5000,
        "active_context_tokens": 5000,
        "billed_cost": 1.50,
        "billed_input_tokens": 4000,
        "billed_output_tokens": 1000,
        "latest_model": "Gemini 3.8 Flash (High)",
        "latest_max_tokens": 1000000,
        "category_stats": {"AI Generation": 20000},
        "steps": [
            {
                "step_index": 1,
                "created_at": "2026-10-14T10:00:00Z",
                "source": "AI",
                "step_type": "PLANNER_RESPONSE",
                "category": "AI Generation",
                "activity": "Response",
                "tool_or_source": "Model",
                "characters": 20000,
                "est_tokens": 5000,
                "detail": "Test",
                "billed_cost": 1.50,
            }
        ],
        "quota_state": [
            {
                "bucketId": "gemini-weekly",
                "remainingFraction": 0.85,
                "resetTime": "2026-10-21T02:27:22Z",
                "calibratedCapacity": 672.0,
            },
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.90,
                "resetTime": "2026-10-14T14:00:00Z",
                "calibratedCapacity": 236.7,
            },
            {
                "bucketId": "3p-weekly",
                "remainingFraction": 1.0,
                "resetTime": "2026-10-21T02:27:22Z",
                "calibratedCapacity": 200.0,
            },
            {
                "bucketId": "3p-5h",
                "remainingFraction": 1.0,
                "resetTime": "2026-10-14T14:00:00Z",
                "calibratedCapacity": 70.0,
            },
        ],
    }

    report_md = generate_context_usage_report(
        cid="cid-plain-list",
        model_name="Gemini 3.8 Flash (High)",
        max_tokens=1000000,
        analysis=analysis,
    )
    assert len(report_md) > 0
    assert "## Subscription & Quota Impact" in report_md
    assert "- Gemini Weekly: `85.0%` remaining (" in report_md
    assert "- Gemini 5-Hour: `90.0%` remaining (" in report_md


def test_generate_context_usage_report_multi_step_tool_reversed_cost():
    """Verifies that when the last step is a tool call (cost=0), latest_step_cost finds the preceding planner response."""
    analysis = {
        "title": "Multi-Step Tool Test",
        "total_chars": 15000,
        "total_tokens": 3000,
        "active_context_tokens": 3000,
        "billed_cost": 2.50,
        "latest_model": "Gemini 3.8 Flash (High)",
        "latest_max_tokens": 1000000,
        "steps": [
            {
                "step_index": 1,
                "created_at": "2026-10-14T10:00:00Z",
                "source": "AI",
                "step_type": "PLANNER_RESPONSE",
                "category": "AI Generation",
                "activity": "Response",
                "tool_or_source": "Model",
                "characters": 10000,
                "est_tokens": 2500,
                "detail": "Executing tool",
                "billed_cost": 2.50,
            },
            {
                "step_index": 2,
                "created_at": "2026-10-14T10:00:05Z",
                "source": "TOOL",
                "step_type": "TOOL_OUTPUT",
                "category": "Tool Output",
                "activity": "Tool Result",
                "tool_or_source": "run_command",
                "characters": 5000,
                "est_tokens": 500,
                "detail": "Tool executed",
                "billed_cost": 0.0,
            },
        ],
        "quota_state": {
            "gemini-weekly": {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.90,
                "reset_time": "2026-10-21T00:00:00Z",
                "calibrated_capacity": 672.0,
            }
        },
    }

    report_md = generate_context_usage_report(
        cid="cid-multi-step",
        model_name="Gemini 3.8 Flash (High)",
        max_tokens=1000000,
        analysis=analysis,
    )
    # Turn Cost should reflect the $2.50 planner response, NOT $0.00 from step 2
    assert "- Turn Cost: `$2.50` (`0.37%` of weekly quota)" in report_md


def test_extract_bucket_view_helper():
    """Verifies _extract_bucket_view parses snake_case, camelCase, and fallbacks."""
    buckets = {
        "gemini-weekly": {
            "calibrated_capacity": 672.0,
            "remaining_fraction": 0.85,
            "reset_time": "2026-10-21T02:27:22Z",
        },
        "3p-weekly": {
            "calibratedCapacity": 250.0,
            "remainingFraction": 0.90,
            "resetTime": "2026-10-21T02:27:22Z",
        },
    }

    v_gem = _extract_bucket_view(buckets, "gemini-weekly", 672.0)
    assert v_gem["capacity"] == 672.0
    assert v_gem["remaining_pct"] == 85.0
    assert "to reset" in v_gem["countdown_str"]

    v_3p = _extract_bucket_view(buckets, "3p-weekly", 200.0)
    assert v_3p["capacity"] == 250.0
    assert v_3p["remaining_pct"] == 90.0

    v_fallback = _extract_bucket_view(buckets, "nonexistent", 100.0)
    assert v_fallback["capacity"] == 100.0
    assert v_fallback["remaining_pct"] == 100.0
    assert v_fallback["countdown_str"] == "no reset scheduled"

    # Test None input robustness
    v_none = _extract_bucket_view(None, "gemini-weekly", 672.0)
    assert v_none["capacity"] == 672.0
    assert v_none["remaining_pct"] == 100.0
    assert v_none["countdown_str"] == "no reset scheduled"

    # Test list input robustness (snake_case and camelCase)
    list_buckets = [
        {
            "bucket_id": "gemini-weekly",
            "calibrated_capacity": 600.0,
            "remaining_fraction": 0.80,
            "reset_time": "2026-10-21T02:27:22Z",
        },
        {
            "bucketId": "3p-weekly",
            "calibratedCapacity": 180.0,
            "remainingFraction": 0.75,
            "resetTime": "2026-10-21T02:27:22Z",
        },
    ]
    v_list_gem = _extract_bucket_view(list_buckets, "gemini-weekly")
    assert v_list_gem["capacity"] == 600.0
    assert v_list_gem["remaining_pct"] == 80.0

    v_list_3p = _extract_bucket_view(list_buckets, "3p-weekly")
    assert v_list_3p["capacity"] == 180.0
    assert v_list_3p["remaining_pct"] == 75.0


def test_generate_context_usage_report_top_steps_and_fleet_weekly_quota_columns(
    tmp_path: Path,
):
    """Verifies % Wk Quota columns in both Top Context Consumers and Delegated Swarm Fleet tables."""
    db_file = tmp_path / "analytics.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, title, model_name, created_at, total_steps, total_tokens, billed_cost)
            VALUES ('parent-100', 'Parent Task', 'Gemini 3.8 Flash (High)', '2026-10-14T10:00:00Z', 10, 50000, 6.72)
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (id, title, model_name, created_at, parent_id, root_id, depth, subagent_role, total_steps, total_tokens, billed_cost)
            VALUES ('sub-101', 'Subagent 1', 'Gemini 3.8 Flash (High)', '2026-10-14T10:05:00Z', 'parent-100', 'parent-100', 1, 'worker', 5, 20000, 3.36)
            """
        )
        conn.commit()

    analysis = {
        "title": "Quota Table Test",
        "total_chars": 20000,
        "total_tokens": 5000,
        "active_context_tokens": 5000,
        "billed_cost": 6.72,
        "latest_model": "Gemini 3.8 Flash (High)",
        "latest_max_tokens": 1000000,
        "steps": [
            {
                "step_index": 1,
                "created_at": "2026-10-14T10:00:00Z",
                "source": "AI",
                "step_type": "PLANNER_RESPONSE",
                "category": "AI Generation",
                "activity": "Response",
                "tool_or_source": "Model",
                "characters": 15000,
                "est_tokens": 3000,
                "detail": "Large output step",
                "billed_cost": 6.72,
            },
        ],
        "quota_state": {
            "gemini-weekly": {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.90,
                "reset_time": "2026-10-21T00:00:00Z",
                "calibrated_capacity": 672.0,
            }
        },
    }

    with patch("scripts.ingest.DEFAULT_DB_PATH", db_file):
        report_md = generate_context_usage_report(
            cid="parent-100",
            model_name="Gemini 3.8 Flash (High)",
            max_tokens=1000000,
            analysis=analysis,
        )

    # 1. Top Context Consumers has % Wk Quota
    assert (
        "| Step # | Activity | Source / Tool | Est. Tokens | % Wk Quota | Detail |"
        in report_md
    )
    # $6.72 / $672.0 * 100 = 1.00%
    assert "| #1 | Response | Model | ~3.0k | 1.00% | Large output step |" in report_md

    # 2. Delegated Swarm Fleet has % Wk Quota
    assert (
        "| Sub-Agent | Role | Tier | Steps | Tokens | Cost | % Wk Quota |" in report_md
    )
    # Subagent cost: $3.36 / $672.0 * 100 = 0.50%
    assert (
        "| [Subagent 1](conversation://sub-101) | worker | Tier 1 | 5 | ~20.0k | $3.36 | 0.50% |"
        in report_md
    )


def test_generate_context_usage_report_resolves_and_caches_activity_profile(
    tmp_path: Path,
):
    from datetime import datetime, timezone

    db_file = tmp_path / "test_ingest_profile.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    for i in range(60):
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, step_type, billed_cost) VALUES ('c1', ?, '2026-10-14T14:00:00Z', 'USER_MESSAGE', 0.1)",
            (i + 1,),
        )
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "remaining_fraction": 0.85,
                "reset_time": "2026-10-20T14:00:00Z",
                "calibrated_capacity": 672.0,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    conn.commit()
    conn.close()

    analysis = {
        "steps": [{"billed_cost": 0.5}],
        "category_stats": {"tool_result": 500},
        "total_chars": 500,
        "billed_cost": 0.5,
    }

    with patch("scripts.ingest.DEFAULT_DB_PATH", db_file):
        report_md = generate_context_usage_report(
            cid="c1",
            model_name="Gemini 3.8 Flash (High)",
            max_tokens=1000000,
            analysis=analysis,
        )
        assert "quota_state" in analysis
        assert "activity_profile" in analysis
        assert analysis["activity_profile"]["total_steps"] == 60
        assert "## Subscription & Quota Impact" in report_md


def test_analyze_transcript_excludes_mcp_tool_schemas_by_default(tmp_path: Path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}\n'
        '{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Hi"}\n',
        encoding="utf-8",
    )
    res = analyze_transcript(transcript)
    assert all(b.kind.value != "tools" for b in res["parsed_blocks"])


def test_analyze_transcript_single_pass_model_detection(tmp_path: Path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"step_index": 1, "type": "SYSTEM", "content": "Init"}\n'
        '{"step_index": 2, "type": "USER_INPUT", "content": "The user changed setting Model Selection from None to Gemini 2.5 Flash."}\n'
        '{"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Done"}\n',
        encoding="utf-8",
    )
    res = analyze_transcript(transcript)
    assert res["latest_model"] == "Gemini 2.5 Flash"
    assert res["models_used"] == ["Gemini 2.5 Flash"]
    assert res["steps"][0]["model_name"] == "Gemini 2.5 Flash"


def test_sync_to_sqlite_incremental_append_and_compaction_fallback(tmp_path: Path):
    db_file = tmp_path / "test_incremental.db"
    init_db(db_file)
    cid = "test-conv-inc"

    # Step 1: Initial sync with 2 steps
    t1 = tmp_path / "t1.jsonl"
    t1.write_text(
        '{"step_index": 1, "type": "USER_INPUT", "content": "Turn 1", "created_at": "2026-10-20T10:00:00Z"}\n'
        '{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Resp 1", "created_at": "2026-10-20T10:00:05Z"}\n',
        encoding="utf-8",
    )
    res1 = analyze_transcript(t1)
    with patch("scripts.ingest.DEFAULT_DB_PATH", db_file):
        diff1 = sync_to_sqlite(cid, "Gemini Flash", res1)
        assert diff1 is not None
        assert not diff1.compaction_detected

        conn = get_db_connection(db_file)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM steps WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transcript_blocks WHERE conversation_id = ?",
                (cid,),
            ).fetchone()[0]
            > 0
        )
        conn.close()

        # Step 2: Incremental append (add step 3)
        t2 = tmp_path / "t2.jsonl"
        t2.write_text(
            '{"step_index": 1, "type": "USER_INPUT", "content": "Turn 1", "created_at": "2026-10-20T10:00:00Z"}\n'
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Resp 1", "created_at": "2026-10-20T10:00:05Z"}\n'
            '{"step_index": 3, "type": "USER_INPUT", "content": "Turn 2", "created_at": "2026-10-20T10:01:00Z"}\n',
            encoding="utf-8",
        )
        res2 = analyze_transcript(t2)
        diff2 = sync_to_sqlite(cid, "Gemini Flash", res2)
        assert diff2 is not None
        assert not diff2.compaction_detected
        assert len(diff2.added) > 0

        conn = get_db_connection(db_file)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM steps WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
            == 3
        )
        conv_row = conn.execute(
            "SELECT compaction_count, pruned_tokens, total_steps FROM conversations WHERE id = ?",
            (cid,),
        ).fetchone()
        assert conv_row["compaction_count"] == 0
        assert conv_row["total_steps"] == 3
        conn.close()

        # Step 3: Compaction occurs (turn 1 removed, replaced by checkpoint)
        t3 = tmp_path / "t3.jsonl"
        t3.write_text(
            '{"step_index": 2, "type": "CHECKPOINT", "content": "Summary of turn 1", "created_at": "2026-10-20T10:02:00Z"}\n'
            '{"step_index": 3, "type": "USER_INPUT", "content": "Turn 2", "created_at": "2026-10-20T10:02:05Z"}\n'
            '{"step_index": 4, "type": "PLANNER_RESPONSE", "content": "Resp 2", "created_at": "2026-10-20T10:02:10Z"}\n',
            encoding="utf-8",
        )
        res3 = analyze_transcript(t3)
        diff3 = sync_to_sqlite(cid, "Gemini Flash", res3)
        assert diff3 is not None
        assert diff3.compaction_detected

        conn = get_db_connection(db_file)
        conv_row = conn.execute(
            "SELECT compaction_count, pruned_tokens, total_steps FROM conversations WHERE id = ?",
            (cid,),
        ).fetchone()
        assert conv_row["compaction_count"] == 1
        assert conv_row["pruned_tokens"] > 0
        db_steps = [
            r[0]
            for r in conn.execute(
                "SELECT step_index FROM steps WHERE conversation_id = ? ORDER BY step_index",
                (cid,),
            ).fetchall()
        ]
        assert db_steps == [2, 3, 4], f"Expected [2, 3, 4] but got {db_steps}"
        conn.close()


def test_sync_to_sqlite_incremental_append_with_ephemeral_steps(tmp_path: Path):
    """Verifies incremental append succeeds when prior turns contain steps without blocks."""
    db_file = tmp_path / "test_ephemeral_append.db"
    init_db(db_file)
    cid = "test-conv-eph"

    t1 = tmp_path / "t1.jsonl"
    t1.write_text(
        '{"step_index": 1, "type": "USER_INPUT", "content": "Turn 1", "created_at": "2026-10-20T10:00:00Z"}\n'
        '{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Resp 1", "created_at": "2026-10-20T10:00:05Z"}\n'
        '{"step_index": 3, "type": "EPHEMERAL_MESSAGE", "content": "Context: 10%", "created_at": "2026-10-20T10:00:06Z"}\n',
        encoding="utf-8",
    )
    res1 = analyze_transcript(t1)
    with patch("scripts.ingest.DEFAULT_DB_PATH", db_file):
        diff1 = sync_to_sqlite(cid, "Gemini Flash", res1)
        assert diff1 is not None

        # Turn 2: strictly append step 4
        t2 = tmp_path / "t2.jsonl"
        t2.write_text(
            '{"step_index": 1, "type": "USER_INPUT", "content": "Turn 1", "created_at": "2026-10-20T10:00:00Z"}\n'
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Resp 1", "created_at": "2026-10-20T10:00:05Z"}\n'
            '{"step_index": 3, "type": "EPHEMERAL_MESSAGE", "content": "Context: 10%", "created_at": "2026-10-20T10:00:06Z"}\n'
            '{"step_index": 4, "type": "USER_INPUT", "content": "Turn 2", "created_at": "2026-10-20T10:01:00Z"}\n',
            encoding="utf-8",
        )
        res2 = analyze_transcript(t2)

        executed_deletes = []
        orig_conn = get_db_connection(db_file)

        def trace(stmt):
            if any(
                t in stmt
                for t in (
                    "DELETE FROM steps",
                    "DELETE FROM tool_executions",
                    "DELETE FROM transcript_blocks",
                )
            ):
                executed_deletes.append(stmt)

        orig_conn.set_trace_callback(trace)

        with patch("scripts.ingest.get_db_connection", return_value=orig_conn):
            diff2 = sync_to_sqlite(cid, "Gemini Flash", res2)

        assert diff2 is not None
        assert not diff2.compaction_detected
        assert (
            len(executed_deletes) == 0
        ), f"Expected 0 DELETE statements on append, got {executed_deletes}"


def test_analyze_transcript_project_detection_from_path_and_cmdline(tmp_path: Path):
    """Verifies that analyze_transcript extracts project from modern tool calls with Path or CommandLine."""
    # 1. Antigravity 2.0 read_file with Path
    t1 = tmp_path / "t_path.jsonl"
    t1.write_text(
        '{"step_index": 1, "type": "USER_INPUT", "content": "Inspect file"}\n'
        '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "args": {"Path": "/Users/akemp/Workspace/agy-context-monitor/src/db.py"}}]}\n',
        encoding="utf-8",
    )
    res1 = analyze_transcript(t1)
    assert res1["project"] == "agy-context-monitor"

    # 2. run_command with CommandLine in workspace
    t2 = tmp_path / "t_cmd.jsonl"
    t2.write_text(
        '{"step_index": 1, "type": "USER_INPUT", "content": "Run tests"}\n'
        '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "cd /Users/akemp/Workspace/ledger && pytest"}}]}\n',
        encoding="utf-8",
    )
    res2 = analyze_transcript(t2)
    assert res2["project"] == "ledger"


def test_sync_to_sqlite_lineage_cycle_prevention(tmp_path: Path):
    """Verifies that cyclic parent linkages are prevented and broken into root conversations."""
    db_file = tmp_path / "test_cycle.db"
    init_db(db_file)
    cid_a = "conv-cycle-a"
    cid_b = "conv-cycle-b"

    # Step 1: Insert B as initial root, then A with parent B
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, parent_id, root_id, depth) VALUES (?, ?, ?, ?, ?)",
        (cid_b, "Conv B", None, cid_b, 0),
    )
    conn.execute(
        "INSERT INTO conversations (id, title, parent_id, root_id, depth) VALUES (?, ?, ?, ?, ?)",
        (cid_a, "Conv A", cid_b, cid_b, 1),
    )
    conn.commit()
    conn.close()

    # Step 2: Try to sync B with parent A -> cycle would form (B -> A -> B)
    analysis_b = {
        "title": "Conv B",
        "parent_id": cid_a,
        "steps": [],
        "total_chars": 100,
        "total_tokens": 50,
        "duration_seconds": 10,
        "start_time": "2026-10-20T10:00:00Z",
        "end_time": "2026-10-20T10:00:10Z",
    }
    with patch("scripts.ingest.DEFAULT_DB_PATH", db_file):
        sync_to_sqlite(cid_b, "Gemini Flash", analysis_b)

    conn = get_db_connection(db_file)
    row_b = conn.execute("SELECT parent_id, root_id, depth FROM conversations WHERE id = ?", (cid_b,)).fetchone()
    conn.close()

    assert row_b["parent_id"] is None, f"Expected parent_id to be None to break cycle, got {row_b['parent_id']}"
    assert row_b["root_id"] == cid_b
    assert row_b["depth"] == 0



