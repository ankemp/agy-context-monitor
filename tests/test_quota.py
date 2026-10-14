"""
Tests for Quota Client & Dynamic Capacity Calibration Engine
------------------------------------------------------------
Covers Language Server credentials discovery, RPC fetch, SQLite caching,
concurrency-safe capacity calibration, boundary safeguards, and fallback scenarios.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from context_monitor.db import get_db_connection, init_db, upsert_quota_state
from context_monitor.quota import (
    QuotaStateList,
    calculate_quota_metrics,
    calibrate_capacity,
    discover_language_server,
    discover_language_server_credentials,
    fetch_live_quota_from_ls,
    fetch_raw_quota_summary,
    format_quota_pct,
    format_reset_countdown,
    sync_and_get_quota_state,
)
from context_monitor.settings import (
    DEFAULT_3P_5H_CAPACITY,
    DEFAULT_3P_WEEKLY_CAPACITY,
    DEFAULT_GEMINI_5H_CAPACITY,
    DEFAULT_GEMINI_WEEKLY_CAPACITY,
    get_settings,
)


def test_quota_state_table_and_default_settings(tmp_path: Path):
    """Verifies that quota_state table is initialized and settings defaults exist."""
    db_file = tmp_path / "test_quota.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(quota_state)").fetchall()]
    assert "bucket_id" in cols
    assert "remaining_fraction" in cols
    assert "reset_time" in cols
    assert "calibrated_capacity" in cols
    settings = get_settings()
    assert settings["subscription_monthly_price"] == 200.0
    assert settings["default_weekly_capacity_gemini"] == 672.0
    assert settings["default_5h_capacity_gemini"] == 236.7
    assert settings["default_weekly_capacity_3p"] == 200.0
    assert settings["default_5h_capacity_3p"] == 70.0


def test_discover_language_server_credentials_mock(tmp_path: Path):
    """Verifies discovery of port from log file and CSRF token from ps output."""
    log_file = tmp_path / "language_server.log"
    log_file.write_text(
        "I1012 21:06:08.430961 1 server.go:651] Language server listening on random port at 49152 for HTTP\n"
    )

    fake_ps = (
        "  PID COMMAND\n"
        "93645 /bin/language_server --csrf_token test-token-abcd-1234 --port 0\n"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_ps)
        port, token = discover_language_server_credentials(log_path=log_file)

    assert port == 49152
    assert token == "test-token-abcd-1234"


def test_discover_language_server_credentials_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verifies environment variables AGY_HTTP_PORT and AGY_CSRF_TOKEN take precedence."""
    monkeypatch.setenv("AGY_HTTP_PORT", "55555")
    monkeypatch.setenv("AGY_CSRF_TOKEN", "env-csrf-token-999")

    port, token = discover_language_server_credentials(log_path=tmp_path / "nonexistent.log")
    assert port == 55555
    assert token == "env-csrf-token-999"


def test_discover_language_server_missing_process_or_log(tmp_path: Path):
    """Verifies graceful handling when process or log is missing."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="  PID COMMAND\n123 /bin/other\n")
        port, token = discover_language_server_credentials(log_path=tmp_path / "nonexistent.log")
    assert port is None
    assert token is None


def test_discover_language_server_large_log_reads_backward(tmp_path: Path):
    """Verifies that large logs are scanned backwards and find the latest HTTP port."""
    log_file = tmp_path / "large_language_server.log"
    with open(log_file, "wb") as f:
        # Older startup port
        f.write(b"Language server listening on random port at 40001 for HTTP\n")
        # 100,000 filler lines (~8 MB)
        f.write(b"ERROR: logging before google.Init: some debug line\n" * 100000)
        # Most recent startup port at end
        f.write(b"Language server listening on random port at 56789 for HTTP\n")
        f.write(b"ERROR: logging before google.Init: startup complete\n")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="language_server --csrf_token tok-1")
        port, token = discover_language_server_credentials(log_path=log_file)

    assert port == 56789
    assert token == "tok-1"


def test_fetch_raw_quota_summary_success():
    """Verifies successful HTTP POST to ConnectRPC endpoint."""
    fake_json = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.9348,
                            "resetTime": "2026-10-21T02:27:22Z",
                        }
                    ],
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(fake_json).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = fetch_raw_quota_summary(port=56421, csrf_token="test-token", timeout=1.5)

    assert result == fake_json
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:56421/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
    assert req.headers["X-codeium-csrf-token"] == "test-token"
    assert req.headers["Content-type"] == "application/json"


def test_fetch_raw_quota_summary_timeout_and_error():
    """Verifies that timeouts or HTTP errors return None without raising."""
    # Test timeout
    with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
        res = fetch_raw_quota_summary(port=56421, csrf_token="test-token", timeout=1.5)
    assert res is None

    # Test URLError
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        res = fetch_raw_quota_summary(port=56421, csrf_token="test-token", timeout=1.5)
    assert res is None

    # Test missing arguments
    assert fetch_raw_quota_summary(0, "token") is None
    assert fetch_raw_quota_summary(56421, "") is None


def test_calibration_from_db_and_concurrency_isolation(tmp_path: Path):
    """
    Verifies capacity calibration:
        Q = sum(billed_cost) / (1 - remaining_fraction)
    Using live telemetry sample: $43.8064 / (1 - 0.93481183) = $672.00.
    Also verifies isolated per-thread percentage calculation.
    """
    db_file = tmp_path / "test_cal.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute("INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash (High)')")
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'PLANNER_RESPONSE', 'Gemini 3.8 Flash (High)', 43.8064)
    """)
    conn.commit()

    mock_ls_payload = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remainingFraction": 0.93481183,
                            "resetTime": "2026-10-21T02:27:22Z",
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remainingFraction": 0.95912546,
                            "resetTime": "2026-10-14T14:00:00Z",
                        },
                    ],
                }
            ]
        }
    }

    state = sync_and_get_quota_state(conn, force_refresh=True, ls_fetcher=lambda: mock_ls_payload)
    assert abs(state["gemini-weekly"]["calibrated_capacity"] - 672.0) < 0.5

    # Thread A with $0.672 cost = 0.1% of weekly quota
    m_a = calculate_quota_metrics(0.672, "Gemini 3.8 Flash (High)", state)
    assert abs(m_a["weekly_pct"] - 0.1) < 0.001
    assert abs(m_a["weekly_remaining_pct"] - 93.48) < 0.05
    conn.close()


def test_near_zero_reset_uses_fallback_capacity(tmp_path: Path):
    """Verifies that right after reset (used_fraction < 0.01), fallback capacity is used."""
    db_file = tmp_path / "test_zero.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    mock_fresh_reset = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.9995,
                            "resetTime": "2026-10-21T02:27:22Z",
                        }
                    ],
                }
            ]
        }
    }
    state = sync_and_get_quota_state(conn, force_refresh=True, ls_fetcher=lambda: mock_fresh_reset)
    assert state["gemini-weekly"]["calibrated_capacity"] == DEFAULT_GEMINI_WEEKLY_CAPACITY
    conn.close()


def test_low_billed_cost_uses_fallback_capacity(tmp_path: Path):
    """Verifies that if window billed cost < $0.50, fallback capacity is used."""
    db_file = tmp_path / "test_low_cost.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute("INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')")
    # Only $0.20 cost (< $0.50 threshold)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 0.20)
    """)
    conn.commit()

    cap = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.80,  # 20% used
        reset_time="2026-10-21T02:27:22Z",
        fallback_capacity=672.0,
    )
    assert cap == 672.0
    conn.close()


def test_calibration_preserves_existing_calibrated_capacity(tmp_path: Path):
    """Verifies that an existing calibrated capacity (> 0) is preserved during fresh reset."""
    db_file = tmp_path / "test_preserve.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Seed quota_state with an already calibrated capacity of $800.00
    upsert_quota_state(conn, [{
        "bucket_id": "gemini-weekly",
        "group_name": "Gemini Models",
        "window": "weekly",
        "remaining_fraction": 0.50,
        "reset_time": "2026-10-14T00:00:00Z",
        "calibrated_capacity": 800.0,
        "fetched_at": "2026-10-14T00:00:00Z",
    }])

    # Fresh reset: used_fraction = 0.0001 < 0.01
    cap = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.9999,
        reset_time="2026-10-21T00:00:00Z",
        fallback_capacity=672.0,
    )
    assert cap == 800.0  # Preserved from previous calibration rather than falling back to 672.0
    conn.close()


def test_calibration_5h_window_boundary(tmp_path: Path):
    """Verifies that 5h calibration only counts steps within (reset_time - 5 hours)."""
    db_file = tmp_path / "test_5h.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute("INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')")

    # reset_time is 2026-10-14T15:00:00Z -> 5h window starts at 2026-10-14T10:00:00Z
    # Step 1: 09:30:00Z (outside 5h window, cost $10.00)
    # Step 2: 11:00:00Z (inside 5h window, cost $11.835)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T09:30:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 10.00)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 2, '2026-10-14T11:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 11.835)
    """)
    conn.commit()

    # remaining_fraction = 0.95 -> used_fraction = 0.05
    # inside window cost = 11.835 -> calibrated = 11.835 / 0.05 = 236.70
    cap = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-5h",
        remaining_fraction=0.95,
        reset_time="2026-10-14T15:00:00Z",
        fallback_capacity=236.7,
    )
    assert abs(cap - 236.70) < 0.01
    conn.close()


def test_model_family_filtering(tmp_path: Path):
    """Verifies that Gemini steps don't leak into 3P buckets and vice versa."""
    db_file = tmp_path / "test_family.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute("INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Mixed')")

    # Gemini cost = $33.60
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Pro', 33.60)
    """)
    # Claude cost = $10.00
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 2, '2026-10-14T10:05:00Z', 'USER_MESSAGE', 'Claude 3.7 Sonnet', 10.00)
    """)
    conn.commit()

    # Gemini weekly: remaining = 0.95 -> used = 0.05. 33.60 / 0.05 = 672.0
    cap_gemini = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.95,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=DEFAULT_GEMINI_WEEKLY_CAPACITY,
    )
    assert abs(cap_gemini - 672.0) < 0.01

    # 3P weekly: remaining = 0.95 -> used = 0.05. 10.00 / 0.05 = 200.0
    cap_3p = calibrate_capacity(
        conn=conn,
        bucket_id="3p-weekly",
        remaining_fraction=0.95,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=DEFAULT_3P_WEEKLY_CAPACITY,
    )
    assert abs(cap_3p - 200.0) < 0.01
    conn.close()


def test_sync_and_get_quota_state_cache_hit_and_miss(tmp_path: Path):
    """Verifies that fresh cache is returned without calling ls_fetcher, and expired cache triggers fetch."""
    db_file = tmp_path / "test_cache.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    fetch_call_count = 0

    def mock_fetcher():
        nonlocal fetch_call_count
        fetch_call_count += 1
        return {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.90,
                                "resetTime": "2026-10-21T00:00:00Z",
                            }
                        ],
                    }
                ]
            }
        }

    # First call: cache miss, calls fetcher
    state1 = sync_and_get_quota_state(conn, ls_fetcher=mock_fetcher)
    assert fetch_call_count == 1
    assert state1["gemini-weekly"]["remaining_fraction"] == 0.90

    # Second call immediately: fresh cache hit (< 60s TTL), does NOT call fetcher
    state2 = sync_and_get_quota_state(conn, ls_fetcher=mock_fetcher)
    assert fetch_call_count == 1
    assert state2["gemini-weekly"]["remaining_fraction"] == 0.90

    # Third call with force_refresh=True: calls fetcher
    state3 = sync_and_get_quota_state(conn, force_refresh=True, ls_fetcher=mock_fetcher)
    assert fetch_call_count == 2
    assert state3["gemini-weekly"]["remaining_fraction"] == 0.90

    # Fourth call with expired TTL: artificially age fetched_at by 120s
    past_iso = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    conn.execute("UPDATE quota_state SET fetched_at = ?", (past_iso,))
    conn.commit()

    state4 = sync_and_get_quota_state(conn, ttl_seconds=60, ls_fetcher=mock_fetcher)
    assert fetch_call_count == 3
    assert state4["gemini-weekly"]["remaining_fraction"] == 0.90
    conn.close()


def test_sync_and_get_quota_state_offline_fallback(tmp_path: Path):
    """Verifies fallback when Language Server is offline / unreachable."""
    db_file = tmp_path / "test_offline.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    def failing_fetcher():
        raise ConnectionRefusedError("Language server offline")

    # 1. When DB is empty and fetch fails: returns synthesized fallback rows with default capacities
    state_empty = sync_and_get_quota_state(conn, ls_fetcher=failing_fetcher)
    assert len(state_empty) == 4
    assert state_empty["gemini-weekly"]["calibrated_capacity"] == DEFAULT_GEMINI_WEEKLY_CAPACITY
    assert state_empty["gemini-5h"]["calibrated_capacity"] == DEFAULT_GEMINI_5H_CAPACITY
    assert state_empty["3p-weekly"]["calibrated_capacity"] == DEFAULT_3P_WEEKLY_CAPACITY
    assert state_empty["3p-5h"]["calibrated_capacity"] == DEFAULT_3P_5H_CAPACITY

    # 2. When DB has existing (expired) rows and fetch fails: returns existing rows without error
    conn.execute("UPDATE quota_state SET remaining_fraction = 0.77 WHERE bucket_id = 'gemini-weekly'")
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE quota_state SET fetched_at = ?", (past_iso,))
    conn.commit()

    state_cached = sync_and_get_quota_state(conn, ttl_seconds=60, ls_fetcher=failing_fetcher)
    assert state_cached["gemini-weekly"]["remaining_fraction"] == 0.77
    conn.close()


def test_calculate_quota_metrics_calling_conventions():
    """Verifies calculate_quota_metrics works with both task conventions."""
    mock_state_list = QuotaStateList([
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.934,
            "reset_time": "2026-10-21T02:27:22Z",
            "calibrated_capacity": 672.0,
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.950,
            "reset_time": "2026-10-14T22:36:42Z",
            "calibrated_capacity": 236.7,
        },
        {
            "bucket_id": "3p-weekly",
            "group_name": "Claude and GPT models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "2026-10-21T18:00:00Z",
            "calibrated_capacity": 200.0,
        },
        {
            "bucket_id": "3p-5h",
            "group_name": "Claude and GPT models",
            "window": "5h",
            "remaining_fraction": 1.0,
            "reset_time": "2026-10-14T23:00:00Z",
            "calibrated_capacity": 70.0,
        },
    ])

    # Convention 1: (turn_cost, thread_cost, quota_state)
    m1 = calculate_quota_metrics(turn_cost=0.672, thread_cost_or_model=2.016, quota_state=mock_state_list)
    assert "gemini-weekly" in m1
    assert "gemini-5h" in m1
    assert "3p-weekly" in m1
    assert "3p-5h" in m1
    assert abs(m1["gemini-weekly"]["turn_pct"] - 0.1) < 0.001
    assert abs(m1["gemini-weekly"]["thread_pct"] - 0.3) < 0.001
    assert m1["gemini-weekly"]["quota_remaining_pct"] == 93.4
    assert m1["gemini-weekly"]["calibrated_capacity"] == 672.0
    assert m1["gemini-weekly"]["reset_time"] == "2026-10-21T02:27:22Z"

    # Convention 2: (billed_cost, model_name, quota_state)
    m2 = calculate_quota_metrics(0.672, "Gemini 3.8 Flash (High)", mock_state_list)
    assert abs(m2["weekly_pct"] - 0.1) < 0.001
    assert abs(m2["weekly_remaining_pct"] - 93.4) < 0.01

    # 3P model routing
    m3 = calculate_quota_metrics(1.00, "Claude 3.7 Sonnet", mock_state_list)
    assert abs(m3["weekly_pct"] - 0.5) < 0.001  # $1.00 / $200.00 = 0.5%
    assert m3["bucket_prefix"] == "3p"


def test_format_reset_countdown_and_pct():
    """Verifies reset countdown and percentage formatting."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # 6 days 7 hours
    t1 = "2026-10-20T19:30:00Z"
    assert format_reset_countdown(t1, now=now) == "6d 7h"

    # 4 hours 19 minutes
    t2 = "2026-10-14T16:19:00Z"
    assert format_reset_countdown(t2, now=now) == "4h 19m"

    # 45 minutes
    t3 = "2026-10-14T12:45:00Z"
    assert format_reset_countdown(t3, now=now) == "45m"

    # Past reset
    t4 = "2026-10-14T11:00:00Z"
    assert format_reset_countdown(t4, now=now) == "0m"

    # Empty string
    assert format_reset_countdown("") == ""

    # Percentage formatting
    assert format_quota_pct(0.312) == "0.31%"
    assert format_quota_pct(0.312, precision=3) == "0.312%"


def test_live_language_server_discovery():
    """
    Integration check: If Antigravity language_server is running on this machine,
    verifies that discover_language_server_credentials finds a valid port and token.
    """
    port, token = discover_language_server_credentials()
    # If language_server is running, both port and token should be found
    if token is not None:
        assert isinstance(token, str)
        assert len(token) > 10
    if port is not None:
        assert isinstance(port, int)
        assert port > 1024


def test_quota_function_aliases():
    """Verifies that backwards-compatible function aliases point to canonical functions."""
    assert discover_language_server is discover_language_server_credentials
    assert fetch_live_quota_from_ls is fetch_raw_quota_summary
