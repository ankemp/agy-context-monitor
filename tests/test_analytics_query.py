"""
Tests for CLI Analytics Integration (scripts/analytics_query.py)
----------------------------------------------------------------
Covers --quota, --cid / --conversation, --summary quota status, --report markdown table,
and clean markdown/output invariant (zero emojis in quota output & report).
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from context_monitor.db import get_db_connection, init_db, upsert_quota_state

# Ensure scripts directory is importable
_root = Path(__file__).resolve().parent.parent
if str(_root / "scripts") not in sys.path:
    sys.path.insert(0, str(_root / "scripts"))

import analytics_query  # noqa: E402


EMOJI_PATTERN = re.compile(
    r"[\U0001F600-\U0001F64F"  # Emoticons
    r"\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
    r"\U0001F680-\U0001F6FF"  # Transport and Map
    r"\U0001F700-\U0001F77F"  # Alchemical Symbols
    r"\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    r"\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    r"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    r"\U0001FA00-\U0001FA6F"  # Chess Symbols
    r"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    r"\U00002702-\U000027B0"  # Dingbats
    r"]+",
    flags=re.UNICODE,
)


@pytest.fixture
def analytics_test_db(tmp_path: Path):
    """Creates a temporary test database populated with conversations, steps, and quota state."""
    db_file = tmp_path / "test_analytics.db"
    init_db(db_file)
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_file) as conn:
        upsert_quota_state(
            conn,
            [
                {
                    "bucket_id": "gemini-weekly",
                    "group_name": "Gemini Models",
                    "window": "weekly",
                    "remaining_fraction": 0.85,
                    "reset_time": "2026-10-21T02:27:22Z",
                    "calibrated_capacity": 700.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "gemini-5h",
                    "group_name": "Gemini Models",
                    "window": "5h",
                    "remaining_fraction": 0.90,
                    "reset_time": "2026-10-14T18:00:00Z",
                    "calibrated_capacity": 250.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "3p-weekly",
                    "group_name": "Claude and GPT models",
                    "window": "weekly",
                    "remaining_fraction": 0.95,
                    "reset_time": "2026-10-21T02:27:22Z",
                    "calibrated_capacity": 200.0,
                    "fetched_at": now_iso,
                },
                {
                    "bucket_id": "3p-5h",
                    "group_name": "Claude and GPT models",
                    "window": "5h",
                    "remaining_fraction": 0.98,
                    "reset_time": "2026-10-14T18:00:00Z",
                    "calibrated_capacity": 70.0,
                    "fetched_at": now_iso,
                },
            ],
        )

        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'test-conv-root', 'Primary Analytics Task', 'agy-monitor', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:00:00Z', '2026-10-14T10:30:00Z',
                10, 60000, 7.00, 50000, 10000,
                NULL, 'test-conv-root', 0, NULL, NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, model_name, created_at, updated_at,
                total_steps, total_tokens, billed_cost, billed_input_tokens, billed_output_tokens,
                parent_id, root_id, depth, subagent_role, subagent_type
            ) VALUES (
                'test-conv-sub', 'Worker Subagent 1', 'agy-monitor', 'Gemini 3.8 Flash (High)',
                '2026-10-14T10:05:00Z', '2026-10-14T10:25:00Z',
                5, 20000, 3.50, 15000, 5000,
                'test-conv-root', 'test-conv-root', 1, 'coding-worker', 'subagent'
            )
            """
        )

        for idx in range(1, 4):
            conn.execute(
                """
                INSERT INTO steps (
                    conversation_id, step_index, created_at, source, step_type,
                    category, activity, tool_or_source, est_tokens, thinking_tokens,
                    phase, model_name, billed_cost, billed_input_tokens, billed_output_tokens
                ) VALUES (?, ?, '2026-10-14T10:00:00Z', 'assistant', 'PLANNER_RESPONSE',
                          'Agent Reasoning & Text', 'Reasoning', 'Planner', 5000, 500,
                          'execution', 'Gemini 3.8 Flash (High)', ?, 4000, 1000)
                """,
                ("test-conv-root", idx, 2.0 * idx),
            )
        conn.commit()
    return db_file


def test_print_quota_global(analytics_test_db: Path, capsys: pytest.CaptureFixture):
    """Verifies print_quota without cid outputs bucket status, capacities, countdowns, and top sessions."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.print_quota(conn, cid=None)

    captured = capsys.readouterr().out
    assert "ANTIGRAVITY SUBSCRIPTION QUOTA & RATE LIMIT STATUS" in captured
    assert "Gemini Weekly Quota" in captured
    assert "Gemini 5-Hour Quota" in captured
    assert "Claude/3P Weekly Quota" in captured
    assert "Claude/3P 5-Hour Quota" in captured
    assert "85.0% remaining" in captured
    assert "90.0% remaining" in captured
    assert "Cap: $700.0" in captured
    assert "Top 5 Sessions by Quota Impact:" in captured
    assert "test-conv-root" in captured

    # Invariant: No decorative emojis in quota CLI output
    assert not EMOJI_PATTERN.search(captured), (
        "CLI quota output must not contain decorative emojis"
    )


def test_print_quota_conversation(
    analytics_test_db: Path, capsys: pytest.CaptureFixture
):
    """Verifies print_quota with cid outputs session, subagent, and turn % breakdown."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.print_quota(conn, cid="test-conv-root")

    captured = capsys.readouterr().out
    assert "CONVERSATION QUOTA BREAKDOWN: test-conv-root" in captured
    assert "Primary Analytics Task" in captured
    assert "Session Weekly Quota Used" in captured
    assert "Session 5-Hour Quota Used" in captured
    assert "Subagent Fleet Impact" in captured
    assert "Total Fleet Weekly Quota" in captured
    assert "Max Single Turn Impact" in captured
    assert "Avg Turn Impact" in captured
    assert "LIVE SUBSCRIPTION BUCKET STATUS" in captured
    assert "SUBAGENT BREAKDOWN:" in captured
    assert "test-conv-sub" in captured
    assert "coding-worker" in captured
    assert "TOP 5 EXPENSIVE TURNS:" in captured

    # Exact numbers: Session cost $7.0 / $700.0 = 1.0000%
    assert "1.0000%" in captured
    # Subagent cost $3.5 / $700.0 = 0.5000%
    assert "0.5000%" in captured
    # Fleet cost $10.5 / $700.0 = 1.5000%
    assert "1.5000%" in captured

    # Invariant: Clean output (no emojis)
    assert not EMOJI_PATTERN.search(captured), (
        "Conversation quota output must not contain emojis"
    )


def test_print_quota_prefix_matching(
    analytics_test_db: Path, capsys: pytest.CaptureFixture
):
    """Verifies print_quota matches UUID prefixes like 'test-conv-r'."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.print_quota(conn, cid="test-conv-r")

    captured = capsys.readouterr().out
    assert "CONVERSATION QUOTA BREAKDOWN: test-conv-root" in captured


def test_print_quota_not_found(analytics_test_db: Path, capsys: pytest.CaptureFixture):
    """Verifies print_quota handles nonexistent cid gracefully."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.print_quota(conn, cid="nonexistent-id-999")

    captured = capsys.readouterr().out
    assert "Conversation 'nonexistent-id-999' not found in database." in captured


def test_print_summary_includes_quota(
    analytics_test_db: Path, capsys: pytest.CaptureFixture
):
    """Verifies print_summary outputs Gemini and Claude/3P weekly and 5h quota lines."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.print_summary(conn)

    captured = capsys.readouterr().out
    assert "Gemini Weekly Quota" in captured
    assert "Gemini 5-Hour Quota" in captured
    assert "Claude/3P Weekly Quota" in captured
    assert "Claude/3P 5-Hour Quota" in captured
    assert "85.0% remaining" in captured
    assert "Cap: $700.0" in captured


def test_generate_global_report_quota_section(analytics_test_db: Path, tmp_path: Path):
    """Verifies generate_global_report produces Section 5 with clean Markdown table."""
    report_file = tmp_path / "global_analytics.md"
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            analytics_query.generate_global_report(conn, output_path=report_file)

    assert report_file.exists()
    content = report_file.read_text(encoding="utf-8")

    assert "## 5. Subscription Quotas & Rate Limits" in content
    assert (
        "| Quota Bucket | Window | Remaining % | Reset Countdown | Calibrated Capacity |"
        in content
    )
    assert "| Gemini Models | Weekly | 85.0% |" in content
    assert "$700.00" in content
    assert "| Claude / 3P Models | Weekly | 95.0% |" in content
    assert "$200.00" in content

    # Clean markdown invariant: verify Section 5 contains no emojis
    section5 = content.split("## 5. Subscription Quotas & Rate Limits")[-1]
    assert not EMOJI_PATTERN.search(section5), (
        "Report Section 5 must not contain decorative emojis"
    )


def test_main_cli_dispatch(analytics_test_db: Path, capsys: pytest.CaptureFixture):
    """Verifies main() CLI argument parsing and dispatch for --quota and --cid."""
    with (
        patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ),
        patch("analytics_query.DEFAULT_DB_PATH", analytics_test_db),
        patch(
            "analytics_query.get_db_connection",
            lambda: get_db_connection(analytics_test_db),
        ),
    ):
        # 1. Test --quota
        with patch.object(sys, "argv", ["analytics_query.py", "--quota"]):
            analytics_query.main()
        out1 = capsys.readouterr().out
        assert "ANTIGRAVITY SUBSCRIPTION QUOTA & RATE LIMIT STATUS" in out1

        # 2. Test --quota --cid
        with patch.object(
            sys, "argv", ["analytics_query.py", "--quota", "--cid", "test-conv-root"]
        ):
            analytics_query.main()
        out2 = capsys.readouterr().out
        assert "CONVERSATION QUOTA BREAKDOWN: test-conv-root" in out2

        # 3. Test --cid alone
        with patch.object(
            sys, "argv", ["analytics_query.py", "--cid", "test-conv-root"]
        ):
            analytics_query.main()
        out3 = capsys.readouterr().out
        assert "CONVERSATION QUOTA BREAKDOWN: test-conv-root" in out3


def test_quota_capacity_defaults_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """Verifies that uncalibrated/fallback quota states use canonical settings defaults (672, 236.7, 200, 70)."""
    db_file = tmp_path / "test_uncalibrated.db"
    init_db(db_file)
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(db_file) as conn:
            analytics_query.print_quota(conn, cid=None)
            out_quota = capsys.readouterr().out
            assert "Cap: $672.0" in out_quota
            assert "Cap: $236.7" in out_quota
            assert "Cap: $200.0" in out_quota
            assert "Cap: $70.0" in out_quota
            assert "Cap: $48.0" not in out_quota
            assert "Cap: $168.0" not in out_quota
            assert "Cap: $12.0" not in out_quota

            analytics_query.print_summary(conn)
            out_summary = capsys.readouterr().out
            assert "[Cap: $672.0]" in out_summary
            assert "[Cap: $236.7]" in out_summary
            assert "[Cap: $200.0]" in out_summary
            assert "[Cap: $70.0]" in out_summary

            report_file = tmp_path / "fallback_report.md"
            analytics_query.generate_global_report(conn, output_path=report_file)
            report_text = report_file.read_text(encoding="utf-8")
            assert "$672.00" in report_text
            assert "$236.70" in report_text
            assert "$200.00" in report_text
            assert "$70.00" in report_text
            assert "$48.00" not in report_text
            assert "$168.00" not in report_text
            assert "$12.00" not in report_text
