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


def test_project_weekly_quota_summary_and_report(
    analytics_test_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture
):
    """Verifies that both --summary and --report include % Wk Quota for projects with proper weekly window filtering."""
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(analytics_test_db) as conn:
            # Add a second project with an old conversation ($50) and a recent same-day space conversation ($3.50)
            conn.execute(
                """
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at,
                    total_steps, total_tokens, billed_cost
                ) VALUES (
                    'conv-legacy-old', 'Old Task', 'legacy-proj', 'Gemini 3.8 Flash',
                    '2026-09-01T10:00:00Z', '2026-09-01T10:10:00Z', 5, 20000, 50.00
                )
                """
            )
            conn.execute(
                """
                INSERT INTO conversations (
                    id, title, project, model_name, created_at, updated_at,
                    total_steps, total_tokens, billed_cost
                ) VALUES (
                    'conv-legacy-new', 'New Task Space Time', 'legacy-proj', 'Gemini 3.8 Flash',
                    '2026-10-14 05:00:00', '2026-10-14 05:10:00', 5, 20000, 3.50
                )
                """
            )
            conn.commit()

            # 1. Test print_summary
            analytics_query.print_summary(conn)
            out_summary = capsys.readouterr().out
            assert "[4] Cost & Context by Project:" in out_summary
            assert "% Wk Quota" in out_summary
            # Project 'agy-monitor' cost is $10.50, weekly is $10.50, capacity is $700.0 => 1.50%
            assert "1.50%" in out_summary
            assert "agy-monitor" in out_summary
            # Project 'legacy-proj' total cost is $53.50, but weekly is $3.50 => 0.50%
            assert "0.50%" in out_summary
            assert "legacy-proj" in out_summary
            assert "$53.50" in out_summary

            # 2. Test generate_global_report
            report_file = tmp_path / "global_analytics.md"
            analytics_query.generate_global_report(conn, output_path=report_file)
            content = report_file.read_text(encoding="utf-8")

            assert "## 6. Cost & Context by Project" in content
            assert (
                "| Project | Cost | % Wk Quota | Share | Sessions | Tokens |" in content
            )
            assert "| `agy-monitor` | $10.50 | 1.50% |" in content
            assert "| `legacy-proj` | $53.50 | 0.50% |" in content
            assert not EMOJI_PATTERN.search(content)


def test_print_velocity_force_multiplier(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Verifies print_velocity includes Force Multiplier, effective hours, and zero division guards."""
    db_file = tmp_path / "test_velocity.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        # Standard case:
        # pairing_sec = 3600 (1h), agent_sec = 1800 (30m)
        # 20 view_file (600s), 40 run_command (1800s), 40 write_to_file (4800s) => saved_sec = 7200s (2h)
        # effective_output = 3600 + 1800 + 7200 = 12600s = 3.5h
        # denominator = 3600s => force_multiplier = 12600 / 3600 = 3.5x
        # pairing_dur = 1h 0m
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                user_prompts, tool_calls_count, lines_written, lines_edited, failed_tool_calls
            ) VALUES (
                'conv_vel', 'Velocity Test', 3600.0, 1800.0, 100, 10000,
                10, 100, 500, 200, 5
            )
            """
        )
        for i in range(20):
            conn.execute(
                "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_vel', ?, 'view_file')",
                (i,),
            )
        for i in range(40):
            conn.execute(
                "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_vel', ?, 'run_command')",
                (20 + i,),
            )
        for i in range(40):
            conn.execute(
                "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_vel', ?, 'write_to_file')",
                (60 + i,),
            )
        conn.commit()

        analytics_query.print_velocity(conn)
        captured = capsys.readouterr().out
        assert "Force Multiplier" in captured
        assert "3.5x" in captured
        assert "(3.5h effective output / 1h 0m active pairing)" in captured
        assert "Human Pairing Time (Inter-Turn)      : 1h 0m" in captured
        assert "Agent Work Time (Root + Subagent Fleet): 30m" in captured
        assert (
            "Est. Dev Time Saved (Tool Execution) : ~2 engineering hours saved to date"
            in captured
        )
        assert "Effective Output Hours               : 3.5h" in captured

    # Edge case: zero pairing and zero agent seconds (no division by zero)
    db_zero = tmp_path / "test_velocity_zero.db"
    init_db(db_zero)
    with get_db_connection(db_zero) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                user_prompts, tool_calls_count
            ) VALUES (
                'conv_zero', 'Zero Test', 0.0, 0.0, 10, 1000, 2, 5
            )
            """
        )
        conn.commit()
        analytics_query.print_velocity(conn)
        out_zero = capsys.readouterr().out
        assert "Force Multiplier" in out_zero
        assert "1.0x (0.0h effective output / 0m active pairing)" in out_zero

    # Fallback: active_pairing_seconds = 0 but active_agent_seconds = 3600
    db_agent = tmp_path / "test_velocity_agent.db"
    init_db(db_agent)
    with get_db_connection(db_agent) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                user_prompts, tool_calls_count
            ) VALUES (
                'conv_agent', 'Agent Fallback', 0.0, 3600.0, 10, 1000, 2, 30
            )
            """
        )
        for i in range(30):
            conn.execute(
                "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_agent', ?, 'write_to_file')",
                (i,),
            )
        conn.commit()
        analytics_query.print_velocity(conn)
        out_agent = capsys.readouterr().out
        # saved_sec = 30 * 120 = 3600. baseline = 3600. multiplier = 1.0 + (3600/3600) = 2.0x
        # effective_hours = (3600 + 3600) / 3600 = 2.0h. pairing_dur = format_duration(3600) = 1h 0m
        assert "Force Multiplier" in out_agent
        assert "2.0x (2.0h effective output / 1h 0m active pairing)" in out_agent

    # Edge case: negative pairing and agent seconds (sanitized to 0m, no negative hours)
    db_neg = tmp_path / "test_velocity_neg.db"
    init_db(db_neg)
    with get_db_connection(db_neg) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                user_prompts, tool_calls_count
            ) VALUES (
                'conv_neg', 'Neg Test', -100.0, -50.0, 10, 1000, 2, 5
            )
            """
        )
        conn.commit()
        analytics_query.print_velocity(conn)
        out_neg = capsys.readouterr().out
        assert "Force Multiplier" in out_neg
        assert "1.0x (0.0h effective output / 0m active pairing)" in out_neg
        assert (
            "Agent Work Time (Root + Subagent Fleet): 0m of pure LLM execution"
            in out_neg
        )

    # Case: positive pairing time but zero saved seconds (multiplier = (3600 + 1800) / 3600 = 1.5x)
    db_no_tools = tmp_path / "test_velocity_no_tools.db"
    init_db(db_no_tools)
    with get_db_connection(db_no_tools) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                user_prompts, tool_calls_count
            ) VALUES (
                'conv_notools', 'No Tools', 3600.0, 1800.0, 10, 1000, 2, 0
            )
            """
        )
        conn.commit()
        analytics_query.print_velocity(conn)
        out_no_tools = capsys.readouterr().out
        assert "1.5x (1.5h effective output / 1h 0m active pairing)" in out_no_tools


def test_format_duration_edge_cases():
    """Verifies format_duration handles negative, zero, and standard durations properly."""
    assert analytics_query.format_duration(0) == "0m"
    assert analytics_query.format_duration(-10) == "0m"
    assert analytics_query.format_duration(-50.0) == "0m"
    assert analytics_query.format_duration(-3600.0) == "0m"
    assert analytics_query.format_duration(1800) == "30m"
    assert analytics_query.format_duration(3600) == "1h 0m"
    assert analytics_query.format_duration(3665) == "1h 1m"


def test_generate_global_report_force_multiplier(tmp_path: Path):
    """Verifies generate_global_report includes Force Multiplier in header block without decorative emojis."""
    db_file = tmp_path / "test_report_fm.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens
            ) VALUES (
                'conv_fm', 'FM Report Test', 1800.0, 900.0, 20, 2000
            )
            """
        )
        for i in range(30):
            conn.execute(
                "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_fm', ?, 'replace_file_content')",
                (i,),
            )
        conn.commit()

        report_file = tmp_path / "global_analytics_fm.md"
        with patch(
            "context_monitor.quota.discover_language_server_credentials",
            return_value=(None, None),
        ):
            analytics_query.generate_global_report(conn, output_path=report_file)

        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "> **Force Multiplier**: **3.5x effective output**" in content
        assert "> **Human Pairing Time (Inter-Turn)**: 30m" in content
        assert "> **Agent Work Time (Root + Subagent Fleet)**: 15m" in content
        assert "> **Est. Dev Time Saved (Tool Execution)**: ~1 hours (1h 0m)" in content
        assert "> **Effective Output Hours**: **1.8h**" in content
        # Clean markdown invariant across the entire report
        assert not EMOJI_PATTERN.search(content), (
            "Markdown report must not contain decorative emojis"
        )


def test_print_summary_force_multiplier_and_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """Verifies print_summary outputs updated Force Multiplier and timing labels."""
    db_file = tmp_path / "test_summary_fm.db"
    init_db(db_file)
    with patch(
        "context_monitor.quota.discover_language_server_credentials",
        return_value=(None, None),
    ):
        with get_db_connection(db_file) as conn:
            conn.execute(
                """
                INSERT INTO conversations (
                    id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens,
                    user_prompts, tool_calls_count
                ) VALUES (
                    'conv_sum', 'Summary Test', 3600.0, 1800.0, 10, 1000, 2, 40
                )
                """
            )
            for i in range(40):
                conn.execute(
                    "INSERT INTO tool_executions (conversation_id, step_index, tool_name) VALUES ('conv_sum', ?, 'write_to_file')",
                    (i,),
                )
            conn.commit()

            analytics_query.print_summary(conn)
            captured = capsys.readouterr().out
            # 3600s pairing + 1800s agent + 4800s saved (40 * 120) = 10200s (2.833h -> 2.8h)
            # Force Multiplier = 10200 / 3600 = 2.8333 -> 2.8x
            assert "Human Pairing Time (Inter-Turn): 1h 0m" in captured
            assert "Agent Work Time (Root + Fleet): 30m" in captured
            assert (
                "Force Multiplier            : 2.8x (2.8h effective output / 1h 0m active pairing)"
                in captured
            )


def test_ingest_and_backfill_subagent_zero_pairing(tmp_path: Path):
    """Verifies that both ingest.py and backfill_analytics.py enforce active_pairing_seconds = 0.0 for subagents."""
    import json
    import ingest
    import backfill_analytics as backfill

    db_file = tmp_path / "test_subagent_zero.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title) VALUES ('root-123', 'Root Task')"
        )
        conn.commit()

    # 1. ingest.sync_to_sqlite: even if analysis had pairing seconds, subagent resolution forces 0.0
    analysis = {
        "title": "Subagent Worker",
        "start_time": "2026-10-23T10:00:00Z",
        "end_time": "2026-10-23T10:10:00Z",
        "duration_seconds": 600.0,
        "active_agent_seconds": 300.0,
        "active_pairing_seconds": 250.0,  # Erroneous pairing time
        "total_chars": 1000,
        "total_tokens": 250,
        "steps": [],
        "parent_id": "root-123",
        "subagent_role": "DeepCoder",
    }
    ingest.sync_to_sqlite("sub-456", "Gemini Pro", analysis, db_path=db_file)
    with get_db_connection(db_file) as conn:
        row = conn.execute(
            "SELECT active_pairing_seconds, active_agent_seconds FROM conversations WHERE id = 'sub-456'"
        ).fetchone()
        assert row["active_pairing_seconds"] == 0.0
        assert row["active_agent_seconds"] == 300.0

    # 2. backfill_analytics parse_conversation & Pass 2
    root_conv_dir = tmp_path / "brain" / "root-conv"
    (root_conv_dir / ".system_generated" / "logs").mkdir(parents=True)
    with open(
        root_conv_dir / ".system_generated" / "logs" / "transcript.jsonl", "w"
    ) as f:
        f.write(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "created_at": "2026-10-23T10:00:00Z",
                    "content": "Hello",
                    "characters": 5,
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-10-23T10:00:10Z",
                    "content": "Hi",
                    "est_tokens": 10,
                }
            )
            + "\n"
        )

    sub_conv_dir = tmp_path / "brain" / "sub-conv"
    (sub_conv_dir / ".system_generated" / "logs").mkdir(parents=True)
    with open(
        sub_conv_dir / ".system_generated" / "logs" / "transcript.jsonl", "w"
    ) as f:
        f.write(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "SYSTEM_MESSAGE",
                    "created_at": "2026-10-23T10:00:10Z",
                    "content": "[Message] timestamp=2026-10-23T10:00:10Z sender=root-conv recipient=sub-conv\nDo work",
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": "2026-10-23T10:00:15Z",
                    "content": "Sub-prompt",
                    "characters": 10,
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-10-23T10:00:25Z",
                    "content": "Done",
                    "est_tokens": 20,
                }
            )
            + "\n"
        )

    # Ingest transcript analysis
    parsed_root = ingest.analyze_transcript(root_conv_dir)
    assert parsed_root["active_pairing_seconds"] > 0.0
    parsed_sub = ingest.analyze_transcript(sub_conv_dir)
    assert parsed_sub["active_pairing_seconds"] == 0.0
    assert parsed_sub["parent_id"] == "root-conv"

    # Backfill
    backfill_db = tmp_path / "backfill.db"
    backfill.run_backfill(brain_dir=tmp_path / "brain", db_path=backfill_db)
    with get_db_connection(backfill_db) as conn:
        r_row = conn.execute(
            "SELECT active_pairing_seconds FROM conversations WHERE id = 'root-conv'"
        ).fetchone()
        assert r_row["active_pairing_seconds"] > 0.0
        s_row = conn.execute(
            "SELECT active_pairing_seconds FROM conversations WHERE id = 'sub-conv'"
        ).fetchone()
        assert s_row["active_pairing_seconds"] == 0.0


def test_ingest_dispatched_subagent_prior_pairing_reset(tmp_path: Path):
    """Verifies that when a child subagent is ingested BEFORE its parent, parent sync resets child's pairing seconds."""
    import ingest

    db_file = tmp_path / "test_out_of_order_sub.db"
    init_db(db_file)

    # 1. Child ingests first standalone (no parent known yet)
    child_analysis = {
        "title": "Subagent Standalone",
        "start_time": "2026-10-23T10:00:00Z",
        "end_time": "2026-10-23T10:10:00Z",
        "duration_seconds": 600.0,
        "active_agent_seconds": 300.0,
        "active_pairing_seconds": 200.0,
        "total_chars": 1000,
        "total_tokens": 250,
        "steps": [],
        "parent_id": None,
        "subagent_role": None,
    }
    ingest.sync_to_sqlite("child-sub", "Gemini Pro", child_analysis, db_path=db_file)

    with get_db_connection(db_file) as conn:
        row = conn.execute(
            "SELECT active_pairing_seconds, parent_id, depth FROM conversations WHERE id = 'child-sub'"
        ).fetchone()
        assert row["active_pairing_seconds"] == 200.0
        assert row["parent_id"] is None
        assert row["depth"] == 0

    # 2. Parent ingests and declares child in dispatched_subagents
    parent_analysis = {
        "title": "Root Coordinator Task",
        "start_time": "2026-10-23T10:00:00Z",
        "end_time": "2026-10-23T10:15:00Z",
        "duration_seconds": 900.0,
        "active_agent_seconds": 400.0,
        "active_pairing_seconds": 120.0,
        "total_chars": 2000,
        "total_tokens": 500,
        "steps": [],
        "dispatched_subagents": [
            {
                "id": "child-sub",
                "role": "DeepCoder",
                "type": "DeepCoder",
                "step_index": 2,
            }
        ],
    }
    ingest.sync_to_sqlite("parent-root", "Gemini Pro", parent_analysis, db_path=db_file)

    with get_db_connection(db_file) as conn:
        child_row = conn.execute(
            "SELECT active_pairing_seconds, parent_id, depth, subagent_role FROM conversations WHERE id = 'child-sub'"
        ).fetchone()
        assert child_row["parent_id"] == "parent-root"
        assert child_row["depth"] == 1
        assert child_row["subagent_role"] == "DeepCoder"
        # Invariant: subagents must have active_pairing_seconds reset to 0.0
        assert child_row["active_pairing_seconds"] == 0.0


def test_backfill_subagent_from_existing_db_lineage(tmp_path: Path):
    """Verifies backfill preloads DB lineage and enforces active_pairing_seconds = 0.0 even without transcript sender header."""
    import json
    import backfill_analytics as backfill

    db_file = tmp_path / "test_backfill_db_lineage.db"
    init_db(db_file)

    # Seed parent and subagent with existing lineage in DB, but subagent having non-zero pairing time
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, title, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens)
            VALUES ('root-db', 'Root Task', 300.0, 150.0, 5, 500)
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (id, title, parent_id, root_id, depth, subagent_role, active_pairing_seconds, active_agent_seconds, total_steps, total_tokens)
            VALUES ('sub-db', 'Subagent Worker', 'root-db', 'root-db', 1, 'DeepCoder', 180.0, 120.0, 5, 400)
            """
        )
        conn.commit()

    # Subagent transcript has NO [Message] sender header
    sub_dir = tmp_path / "brain" / "sub-db"
    (sub_dir / ".system_generated" / "logs").mkdir(parents=True)
    with open(sub_dir / ".system_generated" / "logs" / "transcript.jsonl", "w") as f:
        f.write(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "created_at": "2026-10-23T10:00:00Z",
                    "content": "Execute task",
                    "characters": 12,
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-10-23T10:00:30Z",
                    "content": "Working",
                    "est_tokens": 50,
                }
            )
            + "\n"
        )

    backfill.run_backfill(
        brain_dir=tmp_path / "brain", conversation_id="sub-db", db_path=db_file
    )

    with get_db_connection(db_file) as conn:
        row = conn.execute(
            "SELECT parent_id, depth, subagent_role, active_pairing_seconds FROM conversations WHERE id = 'sub-db'"
        ).fetchone()
        assert row["parent_id"] == "root-db"
        assert row["depth"] == 1
        assert row["subagent_role"] == "DeepCoder"
        assert row["active_pairing_seconds"] == 0.0
