"""
Tests for Quota Client & Dynamic Capacity Calibration Engine
------------------------------------------------------------
Covers Language Server credentials discovery, RPC fetch, SQLite caching,
concurrency-safe capacity calibration, boundary safeguards, and fallback scenarios.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from context_monitor.db import get_db_connection, init_db, upsert_quota_state
from context_monitor.quota import (
    CAPACITY_BOUNDS,
    LAPLACE_ALPHA,
    MIN_PROFILE_STEPS,
    QuotaStateList,
    _estimate_hours_until_exhaustion_weighted,
    _integrate_profile_weight,
    calculate_quota_metrics,
    calculate_quota_pacing,
    calibrate_capacity,
    discover_language_server,
    discover_language_server_credentials,
    fetch_live_quota_from_ls,
    fetch_raw_quota_summary,
    format_quota_pct,
    format_reset_countdown,
    get_capacity_bounds,
    get_hourly_activity_profile,
    normalize_activity_profile,
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


def test_discover_language_server_credentials_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verifies environment variables AGY_HTTP_PORT and AGY_CSRF_TOKEN take precedence."""
    monkeypatch.setenv("AGY_HTTP_PORT", "55555")
    monkeypatch.setenv("AGY_CSRF_TOKEN", "env-csrf-token-999")

    port, token = discover_language_server_credentials(
        log_path=tmp_path / "nonexistent.log"
    )
    assert port == 55555
    assert token == "env-csrf-token-999"


def test_discover_language_server_missing_process_or_log(tmp_path: Path):
    """Verifies graceful handling when process or log is missing."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  PID COMMAND\n123 /bin/other\n"
        )
        port, token = discover_language_server_credentials(
            log_path=tmp_path / "nonexistent.log"
        )
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
        mock_run.return_value = MagicMock(
            returncode=0, stdout="language_server --csrf_token tok-1"
        )
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
        result = fetch_raw_quota_summary(
            port=56421, csrf_token="test-token", timeout=1.5
        )

    assert result == fake_json
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert (
        req.full_url
        == "http://127.0.0.1:56421/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
    )
    assert req.headers["X-codeium-csrf-token"] == "test-token"
    assert req.headers["Content-type"] == "application/json"


def test_fetch_raw_quota_summary_timeout_and_error():
    """Verifies that timeouts or HTTP errors return None without raising."""
    # Test timeout
    with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
        res = fetch_raw_quota_summary(port=56421, csrf_token="test-token", timeout=1.5)
    assert res is None

    # Test URLError
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
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
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash (High)')"
    )
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

    state = sync_and_get_quota_state(
        conn, force_refresh=True, ls_fetcher=lambda: mock_ls_payload
    )
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
    state = sync_and_get_quota_state(
        conn, force_refresh=True, ls_fetcher=lambda: mock_fresh_reset
    )
    assert (
        state["gemini-weekly"]["calibrated_capacity"] == DEFAULT_GEMINI_WEEKLY_CAPACITY
    )
    conn.close()


def test_low_billed_cost_uses_fallback_capacity(tmp_path: Path):
    """Verifies that if window billed cost < $0.50, fallback capacity is used."""
    db_file = tmp_path / "test_low_cost.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
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
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "remaining_fraction": 0.50,
                "reset_time": "2026-10-14T00:00:00Z",
                "calibrated_capacity": 800.0,
                "fetched_at": "2026-10-14T00:00:00Z",
            }
        ],
    )

    # Fresh reset: used_fraction = 0.0001 < 0.01
    cap = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.9999,
        reset_time="2026-10-21T00:00:00Z",
        fallback_capacity=672.0,
    )
    assert (
        cap == 800.0
    )  # Preserved from previous calibration rather than falling back to 672.0
    conn.close()


def test_calibration_5h_window_boundary(tmp_path: Path):
    """Verifies that 5h calibration only counts steps within (reset_time - 5 hours)."""
    db_file = tmp_path / "test_5h.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )

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
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Mixed')"
    )

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
    assert (
        state_empty["gemini-weekly"]["calibrated_capacity"]
        == DEFAULT_GEMINI_WEEKLY_CAPACITY
    )
    assert state_empty["gemini-5h"]["calibrated_capacity"] == DEFAULT_GEMINI_5H_CAPACITY
    assert state_empty["3p-weekly"]["calibrated_capacity"] == DEFAULT_3P_WEEKLY_CAPACITY
    assert state_empty["3p-5h"]["calibrated_capacity"] == DEFAULT_3P_5H_CAPACITY

    # 2. When DB has existing (expired) rows and fetch fails: returns existing rows without error
    conn.execute(
        "UPDATE quota_state SET remaining_fraction = 0.77 WHERE bucket_id = 'gemini-weekly'"
    )
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE quota_state SET fetched_at = ?", (past_iso,))
    conn.commit()

    state_cached = sync_and_get_quota_state(
        conn, ttl_seconds=60, ls_fetcher=failing_fetcher
    )
    assert state_cached["gemini-weekly"]["remaining_fraction"] == 0.77
    conn.close()


def test_calculate_quota_metrics_calling_conventions():
    """Verifies calculate_quota_metrics works with both task conventions."""
    mock_state_list = QuotaStateList(
        [
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
        ]
    )

    # Convention 1: (turn_cost, thread_cost, quota_state)
    m1 = calculate_quota_metrics(
        turn_cost=0.672, thread_cost_or_model=2.016, quota_state=mock_state_list
    )
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
        assert port is not None, (
            "Language server is running with token, but port was not discovered from log"
        )
        assert isinstance(port, int)
        assert port > 1024


def test_quota_function_aliases():
    """Verifies that backwards-compatible function aliases point to canonical functions."""
    assert discover_language_server is discover_language_server_credentials
    assert fetch_live_quota_from_ls is fetch_raw_quota_summary


def test_quota_state_list_dict_interface():
    """Verifies that QuotaStateList supports dictionary membership, methods, and list indexing."""
    qs = QuotaStateList(
        [
            {"bucket_id": "gemini-weekly", "calibrated_capacity": 672.0},
            {"bucket_id": "gemini-5h", "calibrated_capacity": 236.7},
        ]
    )
    # Dict membership and retrieval
    assert "gemini-weekly" in qs
    assert "gemini-5h" in qs
    assert "3p-weekly" not in qs
    assert qs["gemini-weekly"]["calibrated_capacity"] == 672.0
    assert qs.get("gemini-weekly")["calibrated_capacity"] == 672.0
    assert qs.get("3p-weekly", "fallback") == "fallback"

    # List indexing
    assert qs[0]["bucket_id"] == "gemini-weekly"
    assert qs.get(0)["bucket_id"] == "gemini-weekly"
    assert qs.get(99, None) is None

    # Dict methods
    assert list(qs.keys()) == ["gemini-weekly", "gemini-5h"]
    assert len(list(qs.values())) == 2
    items = list(qs.items())
    assert items[0][0] == "gemini-weekly"
    assert items[1][0] == "gemini-5h"
    d = qs.to_dict()
    assert isinstance(d, dict)
    assert "gemini-weekly" in d


def test_calculate_quota_metrics_keyword_args_and_robustness():
    """Verifies calculate_quota_metrics with keyword arguments, camelCase dicts, and session metrics."""
    # Keyword arguments calling convention
    m = calculate_quota_metrics(
        billed_cost=0.50,
        model_name="Claude 3.7 Sonnet",
    )
    assert m["bucket_prefix"] == "3p"
    assert m["turn_pct"] > 0
    assert m["session_weekly_pct"] == m["thread_weekly_pct"]

    # Positional turn + thread with string model
    m2 = calculate_quota_metrics(0.50, 2.50, "Gemini 3.8 Flash (High)")
    assert m2["bucket_prefix"] == "gemini"
    assert m2["turn_pct"] > 0
    assert m2["thread_pct"] > m2["turn_pct"]

    # Raw camelCase bucket dicts from RPC
    camel_state = [
        {
            "bucketId": "gemini-weekly",
            "remainingFraction": 0.85,
            "resetTime": "2026-10-21T02:27:22Z",
            "calibratedCapacity": 672.0,
        }
    ]
    m3 = calculate_quota_metrics(1.0, 5.0, camel_state)
    assert m3["gemini-weekly"]["quota_remaining_pct"] == 85.0
    assert m3["gemini-weekly"]["calibrated_capacity"] == 672.0


def test_format_reset_countdown_and_pct_edge_cases():
    """Verifies timezone normalization and formatting edge cases."""
    # Naive timestamp string
    cd1 = format_reset_countdown(
        "2026-10-21T02:27:22",
        now=datetime(2026, 10, 14, 2, 27, 22),
    )
    assert cd1 == "7d 0h"

    # Timezone offset timestamp string
    cd2 = format_reset_countdown(
        "2026-10-21T02:27:22-05:00",
        now=datetime(2026, 10, 14, 7, 27, 22, tzinfo=timezone.utc),
    )
    assert cd2 == "7d 0h"

    # Invalid and empty strings
    assert format_reset_countdown("invalid") == ""
    assert format_reset_countdown("") == ""
    assert format_reset_countdown(None) == ""

    # format_quota_pct with None and strings
    assert format_quota_pct(None) == "0.00%"
    assert format_quota_pct("0.456", precision=1) == "0.5%"


def test_calibrate_capacity_edge_cases(tmp_path: Path):
    """Verifies that calibrate_capacity safely handles empty/invalid reset times and offsets."""
    db_file = tmp_path / "test_cal_edges.db"
    init_db(db_file)
    conn = get_db_connection(db_file)

    # Empty or invalid reset_time -> returns fallback capacity
    cap1 = calibrate_capacity(conn, "gemini-weekly", 0.50, "", 672.0)
    assert cap1 == 672.0
    cap2 = calibrate_capacity(conn, "gemini-weekly", 0.50, "invalid-time", 672.0)
    assert cap2 == 672.0

    # None or non-numeric remaining_fraction -> treated as 1.0 (used_fraction=0 < 0.01) -> fallback
    cap3 = calibrate_capacity(
        conn, "gemini-weekly", None, "2026-10-21T00:00:00Z", 672.0
    )
    assert cap3 == 672.0

    # Timezone offset string is converted to UTC to match steps
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c_tz', 'TZ', 'Gemini 3.8 Flash')"
    )
    # Step at 2026-10-14T07:30:00Z
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c_tz', 1, '2026-10-14T07:30:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 33.60)
    """)
    conn.commit()

    # reset_time at 2026-10-21T03:00:00-05:00 -> in UTC: 2026-10-21T08:00:00Z
    # 7 days earlier: 2026-10-14T08:00:00Z
    # Step at 07:30:00Z is BEFORE the 7d window start -> should not be counted!
    cap4 = calibrate_capacity(
        conn,
        "gemini-weekly",
        0.95,
        "2026-10-21T03:00:00-05:00",
        672.0,
    )
    # Outside window -> billed cost = 0 < 0.50 -> fallback 672.0
    assert cap4 == 672.0
    conn.close()


def test_sync_and_get_quota_state_default_connection_and_payload_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verifies that conn=None manages connection and raw payload without 'response' key is supported."""
    db_file = tmp_path / "test_default_conn.db"
    init_db(db_file)
    monkeypatch.setattr("context_monitor.db.DEFAULT_DB_PATH", db_file)

    direct_payload = {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "window": "weekly",
                        "remainingFraction": 0.88,
                        "resetTime": "2026-10-21T00:00:00Z",
                    }
                ],
            }
        ]
    }

    # Pass conn=None -> opens managed connection automatically
    state = sync_and_get_quota_state(
        conn=None, force_refresh=True, ls_fetcher=lambda: direct_payload
    )
    assert "gemini-weekly" in state
    assert state["gemini-weekly"]["remaining_fraction"] == 0.88


def test_calculate_quota_metrics_cleaned_signature():
    """Verifies calculate_quota_metrics with new clean signature and backwards compatibility."""
    state = {
        "gemini-weekly": {
            "calibrated_capacity": 672.0,
            "remaining_fraction": 0.90,
            "reset_time": "2026-10-21T02:27:22Z",
        },
        "3p-weekly": {
            "calibrated_capacity": 200.0,
            "remaining_fraction": 0.95,
            "reset_time": "2026-10-21T02:27:22Z",
        },
    }

    # 1. Clean positional signature: (turn_cost, thread_cost, quota_state, model_name)
    m1 = calculate_quota_metrics(6.72, 13.44, state, "Gemini 3.8 Flash")
    assert abs(m1["turn_pct"] - 1.00) < 0.01  # 6.72 / 672 = 1.0%
    assert abs(m1["thread_pct"] - 2.00) < 0.01  # 13.44 / 672 = 2.0%
    assert m1["model_name"] == "Gemini 3.8 Flash"

    # 2. Clean keyword-only signature: billed_cost, session_cost
    m2 = calculate_quota_metrics(
        billed_cost=2.00,
        session_cost=4.00,
        model_name="Claude 3.7 Sonnet",
        quota_state=state,
    )
    assert abs(m2["turn_pct"] - 1.00) < 0.01  # 2.00 / 200 = 1.0%
    assert abs(m2["thread_pct"] - 2.00) < 0.01  # 4.00 / 200 = 2.0%
    assert m2["bucket_prefix"] == "3p"

    # 3. Backwards compatibility: thread_cost_or_model in kwargs
    m3 = calculate_quota_metrics(
        turn_cost=3.36,
        thread_cost_or_model=6.72,
        quota_state=state,
    )
    assert abs(m3["turn_pct"] - 0.50) < 0.01
    assert abs(m3["thread_pct"] - 1.00) < 0.01


def test_calculate_quota_pacing_nominal():
    # 7-day cycle (168h), reset in 6d 4h (148h left -> 20h elapsed). 88.5% remaining -> 11.5% used.
    # Projected: 11.5% / (20/168) = 96.6%
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-20T16:00:00Z"  # 148h ahead
    res = calculate_quota_pacing(
        0.885, reset_iso, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert abs(res["projected_pct"] - 96.6) < 0.5
    assert res["status"] == "moderate"
    assert res["is_calibrating"] is False
    assert abs(res["used_pct"] - 11.5) < 0.01
    assert abs(res["elapsed_hours"] - 20.0) < 0.01
    assert abs(res["remaining_hours"] - 148.0) < 0.01
    assert abs(res["burn_rate_pct_per_hour"] - (11.5 / 20.0)) < 0.01
    assert abs(res["projected_cost"] - 483.0) < 1.0
    assert res["hours_until_exhaustion"] is None
    assert res["days_until_exhaustion"] is None
    assert "billed_tph" in res
    assert "est_tph" in res
    assert "billed_tpm" in res
    assert isinstance(res["billed_tph"], float)
    assert isinstance(res["est_tph"], float)
    assert isinstance(res["billed_tpm"], float)


def test_calculate_quota_pacing_early_cycle_guard():
    # Early cycle: elapsed 1 hour (< 2.5h) in a 168h window
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-21T11:00:00Z"  # 167h ahead -> 1h elapsed
    res = calculate_quota_pacing(
        0.99, reset_iso, window_hours=168.0, calibrated_capacity=672.0, now=now
    )
    assert res["is_calibrating"] is True
    assert res["status"] == "calibrating"
    assert res["projected_pct"] == res["used_pct"]
    assert res["hours_until_exhaustion"] is None
    assert res["days_until_exhaustion"] is None

    # Test fraction guard (< 0.015 elapsed fraction)
    # 2.4h on 168h cycle: 2.4/168 = 0.01428 < 0.015
    reset_iso_2 = "2026-10-21T09:36:00Z"  # 165.6h ahead -> 2.4h elapsed
    res2 = calculate_quota_pacing(
        0.98, reset_iso_2, window_hours=168.0, calibrated_capacity=672.0, now=now
    )
    assert res2["is_calibrating"] is True
    assert res2["status"] == "calibrating"

    # 5-hour cycle proportional guard (min(2.5, 5.0 * 0.1) = 0.5h threshold):
    # At 0.4h elapsed (< 0.5h), is_calibrating is True
    reset_5h_cal = "2026-10-14T16:36:00Z"  # 4.6h ahead -> 0.4h elapsed
    res_5h_cal = calculate_quota_pacing(
        0.98, reset_5h_cal, window_hours=5.0, calibrated_capacity=150.0, now=now
    )
    assert res_5h_cal["is_calibrating"] is True
    assert res_5h_cal["status"] == "calibrating"

    # At 1.0h elapsed (> 0.5h, elapsed_fraction = 1.0/5.0 = 0.20 > 0.015), is_calibrating is False
    reset_5h_ready = "2026-10-14T16:00:00Z"  # 4.0h ahead -> 1.0h elapsed
    res_5h_ready = calculate_quota_pacing(
        0.95, reset_5h_ready, window_hours=5.0, calibrated_capacity=150.0, now=now
    )
    assert res_5h_ready["is_calibrating"] is False
    assert res_5h_ready["status"] == "on_track"


def test_calculate_quota_pacing_statuses():
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    # 50 hours elapsed out of 168h (118h remaining)
    reset_iso = "2026-10-19T10:00:00Z"  # 118h ahead -> 50h elapsed

    # On track: 10% used in 50h -> projected = 10 / (50/168) = 33.6% (<= 80%)
    res_on_track = calculate_quota_pacing(
        0.90, reset_iso, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert res_on_track["status"] == "on_track"
    assert res_on_track["projected_pct"] < 80.0

    # Moderate: 28% used in 50h -> projected = 28 / (50/168) = 94.08% (80% - 100%)
    res_mod = calculate_quota_pacing(
        0.72, reset_iso, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert res_mod["status"] == "moderate"
    assert 80.0 <= res_mod["projected_pct"] <= 100.0

    # Exceeding: 40% used in 50h -> projected = 40 / (50/168) = 134.4% (> 100%)
    res_exc = calculate_quota_pacing(
        0.60, reset_iso, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert res_exc["status"] == "exceeding"
    assert res_exc["projected_pct"] > 100.0
    # Burn rate: 40% / 50h = 0.8% / h. Remaining: 60%. Hours until exhaustion = 60 / 0.8 = 75h (< 118h remaining)
    assert res_exc["hours_until_exhaustion"] is not None
    assert abs(res_exc["hours_until_exhaustion"] - 75.0) < 0.5
    assert res_exc["days_until_exhaustion"] is not None
    assert abs(res_exc["days_until_exhaustion"] - 3.1) < 0.1


def test_calculate_quota_pacing_edge_cases():
    # Missing or invalid reset time -> fallback to is_calibrating=True
    res_none_reset = calculate_quota_pacing(0.90, None)
    assert res_none_reset["is_calibrating"] is True
    assert res_none_reset["status"] == "calibrating"

    res_empty_reset = calculate_quota_pacing(0.90, "")
    assert res_empty_reset["is_calibrating"] is True
    assert res_empty_reset["status"] == "calibrating"

    # None or invalid remaining fraction
    res_none_frac = calculate_quota_pacing(None, "2026-10-20T16:00:00Z")
    assert res_none_frac["used_pct"] == 0.0

    # Negative and > 1.0 remaining fraction clamping
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-20T16:00:00Z"
    res_neg_frac = calculate_quota_pacing(-0.5, reset_iso, now=now)
    assert res_neg_frac["used_pct"] == 100.0
    res_over_frac = calculate_quota_pacing(1.5, reset_iso, now=now)
    assert res_over_frac["used_pct"] == 0.0

    # 100% quota exhausted (remaining_fraction = 0.0)
    res_exhausted = calculate_quota_pacing(
        0.0, reset_iso, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert res_exhausted["used_pct"] == 100.0
    assert res_exhausted["hours_until_exhaustion"] == 0.0
    assert res_exhausted["days_until_exhaustion"] == 0.0
    assert res_exhausted["status"] == "exceeding"

    # String window_hours and invalid window_hours fallback
    res_str_win = calculate_quota_pacing(
        0.885, reset_iso, window_hours="168.0", calibrated_capacity=500.0, now=now
    )
    assert abs(res_str_win["projected_pct"] - 96.6) < 0.5
    res_inv_win = calculate_quota_pacing(
        0.885, reset_iso, window_hours="invalid", calibrated_capacity=500.0, now=now
    )
    assert abs(res_inv_win["projected_pct"] - 96.6) < 0.5
    res_neg_win = calculate_quota_pacing(
        0.885, reset_iso, window_hours=-5.0, calibrated_capacity=500.0, now=now
    )
    assert abs(res_neg_win["projected_pct"] - 96.6) < 0.5

    # String now parameter
    res_str_now = calculate_quota_pacing(
        0.885,
        reset_iso,
        window_hours=168.0,
        calibrated_capacity=500.0,
        now="2026-10-14T12:00:00Z",
    )
    assert abs(res_str_now["projected_pct"] - 96.6) < 0.5

    # Datetime object for reset_time_iso
    reset_dt = datetime(2026, 10, 20, 16, 0, 0, tzinfo=timezone.utc)
    res_dt_reset = calculate_quota_pacing(
        0.885, reset_dt, window_hours=168.0, calibrated_capacity=500.0, now=now
    )
    assert abs(res_dt_reset["projected_pct"] - 96.6) < 0.5

    # format_reset_countdown with string now and datetime reset_dt
    assert format_reset_countdown(reset_dt, now="2026-10-14T12:00:00Z") == "6d 4h"

    # Exact status boundaries: <=80 on_track, 80-100 moderate, >100 exceeding
    # At 20h elapsed of 168h: elapsed_fraction = 20/168
    # If used_fraction = 0.80 * (20/168) -> projected_pct = 80.0%
    rem_80 = 1.0 - (0.80 * (20.0 / 168.0))
    res_80 = calculate_quota_pacing(rem_80, reset_iso, window_hours=168.0, now=now)
    assert res_80["projected_pct"] == 80.0
    assert res_80["status"] == "on_track"

    # If used_fraction = 1.0 * (20/168) -> projected_pct = 100.0%
    rem_100 = 1.0 - (1.00 * (20.0 / 168.0))
    res_100 = calculate_quota_pacing(rem_100, reset_iso, window_hours=168.0, now=now)
    assert res_100["projected_pct"] == 100.0
    assert res_100["status"] == "moderate"

    # If used_fraction = 1.0001 * (20/168) -> projected_pct = 100.01%
    rem_100_01 = 1.0 - (1.0001 * (20.0 / 168.0))
    res_100_01 = calculate_quota_pacing(
        rem_100_01, reset_iso, window_hours=168.0, now=now
    )
    assert res_100_01["projected_pct"] == 100.01
    assert res_100_01["status"] == "exceeding"

    # 5-hour window pacing
    reset_5h = "2026-10-14T13:00:00Z"  # 1h ahead -> 4h elapsed of 5h window
    res_5h = calculate_quota_pacing(
        0.74, reset_5h, window_hours=5.0, calibrated_capacity=146.0, now=now
    )
    # used: 26%, elapsed: 4/5 = 80%. projected: 26 / 0.8 = 32.5%
    assert abs(res_5h["projected_pct"] - 32.5) < 0.5
    assert res_5h["status"] == "on_track"


def test_calculate_quota_metrics_pacing_integration():
    state = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.885,
            "reset_time": "2026-10-20T16:00:00Z",
            "calibrated_capacity": 500.0,
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 0.740,
            "reset_time": "2026-10-14T13:00:00Z",
            "calibrated_capacity": 146.0,
        },
    ]
    m = calculate_quota_metrics(
        billed_cost=1.0,
        model_name="Gemini 3.8 Flash (High)",
        quota_state=state,
    )
    assert "weekly_pacing" in m
    assert "five_hour_pacing" in m
    assert "projected_weekly_pct" in m
    assert "projected_weekly_cost" in m
    assert "pacing_status" in m
    assert "projected_5h_pct" in m
    assert "pacing" in m["gemini-weekly"]
    assert "pacing" in m["gemini-5h"]
    assert isinstance(m["weekly_pacing"], dict)
    assert isinstance(m["five_hour_pacing"], dict)


def test_calibration_ema_smoothing(tmp_path: Path):
    """Verifies that calibrate_capacity applies 80/20 EMA smoothing when prior capacity exists,
    and uses raw calibration directly when prior capacity <= 0."""
    db_file = tmp_path / "test_ema.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    # Seed prior capacity of $600.00
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "group_name": "Gemini Models",
                "window": "weekly",
                "remaining_fraction": 0.50,
                "reset_time": "2026-10-21T10:00:00Z",
                "calibrated_capacity": 600.0,
                "fetched_at": "2026-10-14T10:00:00Z",
            }
        ],
    )
    # Add step cost: $80.00 billed cost within window
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 80.0)
    """)
    conn.commit()

    # remaining_fraction = 0.90 -> used_fraction = 0.10
    # raw_calibrated = 80.0 / 0.10 = 800.0
    # Prior persisted_capacity = 600.0
    # EMA: 0.80 * 600.0 + 0.20 * 800.0 = 480.0 + 160.0 = 640.0
    cap = calibrate_capacity(
        conn=conn,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=672.0,
    )
    assert cap == 640.0

    # Initial calibration without prior capacity uses raw directly (no EMA)
    conn2 = get_db_connection(tmp_path / "test_no_prior.db")
    init_db(tmp_path / "test_no_prior.db")
    conn2.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    conn2.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 80.0)
    """)
    conn2.commit()
    cap_initial = calibrate_capacity(
        conn=conn2,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=672.0,
    )
    # raw is 800.0, which is within [350.0, 1000.0]
    assert cap_initial == 800.0

    # Prior capacity <= 0 (e.g. 0.0 or negative) also uses raw directly
    upsert_quota_state(
        conn2,
        [
            {
                "bucket_id": "gemini-weekly",
                "calibrated_capacity": 0.0,
            }
        ],
    )
    cap_zero_prior = calibrate_capacity(
        conn=conn2,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=672.0,
    )
    assert cap_zero_prior == 800.0

    # Legacy out-of-bounds prior capacity ($2000.0) is clamped to upper bound before EMA
    # Prior clamped: 1000.0, raw: 800.0 -> EMA: 0.80*1000 + 0.20*800 = 800 + 160 = 960.0
    upsert_quota_state(
        conn2,
        [
            {
                "bucket_id": "gemini-weekly",
                "calibrated_capacity": 2000.0,
            }
        ],
    )
    cap_legacy_prior = calibrate_capacity(
        conn=conn2,
        bucket_id="gemini-weekly",
        remaining_fraction=0.90,
        reset_time="2026-10-21T10:00:00Z",
        fallback_capacity=672.0,
    )
    assert cap_legacy_prior == 960.0

    conn.close()
    conn2.close()


def test_calibration_clamping(tmp_path: Path):
    """Verifies that calibrate_capacity clamps capacities within defined bounds for each bucket family:
    - Gemini Weekly: [350.00, 1000.00]
    - Gemini 5-Hour: [100.00, 400.00]
    - 3P Weekly: [100.00, 500.00]
    - 3P 5-Hour: [35.00, 150.00]
    """
    db_file = tmp_path / "test_clamp.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c_gemini', 'G', 'Gemini 3.8 Flash'), ('c_3p', '3P', 'Claude 3.7 Sonnet')"
    )

    # 1. Gemini Weekly: bounds [350.0, 1000.0]
    # Lower clamp: cost $10.0 / 0.05 used = $200.0 -> clamped to 350.0
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c_gemini', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 10.0)
    """)
    conn.commit()
    cap_gw_low = calibrate_capacity(
        conn, "gemini-weekly", 0.95, "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_gw_low == 350.0

    # Upper clamp: cost $60.0 / 0.05 used = $1200.0 -> clamped to 1000.0
    conn.execute(
        "UPDATE steps SET billed_cost = 60.0 WHERE conversation_id = 'c_gemini' AND step_index = 1"
    )
    conn.commit()
    cap_gw_high = calibrate_capacity(
        conn, "gemini-weekly", 0.95, "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_gw_high == 1000.0

    # 2. Gemini 5-Hour: bounds [100.0, 400.0]
    # Lower clamp: cost $2.50 / 0.05 = $50.0 -> clamped to 100.0
    conn.execute(
        "UPDATE steps SET billed_cost = 2.50 WHERE conversation_id = 'c_gemini' AND step_index = 1"
    )
    conn.commit()
    cap_g5_low = calibrate_capacity(
        conn, "gemini-5h", 0.95, "2026-10-14T15:00:00Z", 236.7
    )
    assert cap_g5_low == 100.0

    # Upper clamp: cost $30.0 / 0.05 = $600.0 -> clamped to 400.0
    conn.execute(
        "UPDATE steps SET billed_cost = 30.0 WHERE conversation_id = 'c_gemini' AND step_index = 1"
    )
    conn.commit()
    cap_g5_high = calibrate_capacity(
        conn, "gemini-5h", 0.95, "2026-10-14T15:00:00Z", 236.7
    )
    assert cap_g5_high == 400.0

    # 3. 3P Weekly: bounds [100.0, 500.0]
    # Lower clamp: cost $2.50 / 0.05 = $50.0 -> clamped to 100.0
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c_3p', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Claude 3.7 Sonnet', 2.50)
    """)
    conn.commit()
    cap_3pw_low = calibrate_capacity(
        conn, "3p-weekly", 0.95, "2026-10-21T10:00:00Z", 200.0
    )
    assert cap_3pw_low == 100.0

    # Upper clamp: cost $40.0 / 0.05 = $800.0 -> clamped to 500.0
    conn.execute(
        "UPDATE steps SET billed_cost = 40.0 WHERE conversation_id = 'c_3p' AND step_index = 1"
    )
    conn.commit()
    cap_3pw_high = calibrate_capacity(
        conn, "3p-weekly", 0.95, "2026-10-21T10:00:00Z", 200.0
    )
    assert cap_3pw_high == 500.0

    # 4. 3P 5-Hour: bounds [35.0, 150.0]
    # Lower clamp: cost $1.0 / 0.05 = $20.0 -> clamped to 35.0
    conn.execute(
        "UPDATE steps SET billed_cost = 1.0 WHERE conversation_id = 'c_3p' AND step_index = 1"
    )
    conn.commit()
    cap_3p5_low = calibrate_capacity(conn, "3p-5h", 0.95, "2026-10-14T15:00:00Z", 70.0)
    assert cap_3p5_low == 35.0

    # Upper clamp: cost $15.0 / 0.05 = $300.0 -> clamped to 150.0
    conn.execute(
        "UPDATE steps SET billed_cost = 15.0 WHERE conversation_id = 'c_3p' AND step_index = 1"
    )
    conn.commit()
    cap_3p5_high = calibrate_capacity(conn, "3p-5h", 0.95, "2026-10-14T15:00:00Z", 70.0)
    assert cap_3p5_high == 150.0

    # 5. Combined EMA + Clamping:
    # Prior persisted capacity is $950.0. Raw calibration produces $1500.0.
    # EMA: 0.80 * 950 + 0.20 * 1500 = 760 + 300 = 1060.0.
    # Clamped to upper bound of gemini-weekly: 1000.0.
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "calibrated_capacity": 950.0,
            }
        ],
    )
    conn.execute(
        "UPDATE steps SET billed_cost = 75.0 WHERE conversation_id = 'c_gemini' AND step_index = 1"
    )
    conn.commit()
    # cost 75.0 / 0.05 = 1500.0 raw
    cap_combined = calibrate_capacity(
        conn, "gemini-weekly", 0.95, "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_combined == 1000.0

    # 6. Fallback clamping safeguards:
    # When used_fraction < 0.01 (fresh reset) or total_billed_cost < 0.50,
    # out-of-bounds persisted capacity or fallback capacity must still be clamped.
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "calibrated_capacity": 150.0,  # Below 350.0 lower bound
            }
        ],
    )
    cap_clamped_fallback = calibrate_capacity(
        conn, "gemini-weekly", 0.999, "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_clamped_fallback == 350.0

    # Out-of-bounds upper fallback capacity
    cap_upper_fallback = calibrate_capacity(
        conn,
        "gemini-5h",
        0.50,
        "",
        2500.0,  # Missing reset_time, fallback 2500.0 clamped to 400.0
    )
    assert cap_upper_fallback == 400.0

    conn.close()


def test_calibration_bucket_variants_and_model_isolation(tmp_path: Path):
    """Verifies that custom/variant bucket IDs (e.g. claude-weekly, claude-5h, 3p_weekly)
    map to 3P bounds and properly aggregate Claude model costs without Gemini leakage."""
    db_file = tmp_path / "test_variants.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c_mix', 'Mixed', 'Claude 3.7 Sonnet')"
    )

    # Confirm CAPACITY_BOUNDS dictionary and get_capacity_bounds mappings
    assert CAPACITY_BOUNDS["gemini-weekly"] == (350.0, 1000.0)
    assert CAPACITY_BOUNDS["gemini-5h"] == (100.0, 400.0)
    assert CAPACITY_BOUNDS["3p-weekly"] == (100.0, 500.0)
    assert CAPACITY_BOUNDS["3p-5h"] == (35.0, 150.0)
    assert get_capacity_bounds("claude-weekly") == (100.0, 500.0)
    assert get_capacity_bounds("claude-5h") == (35.0, 150.0)
    assert get_capacity_bounds("3p_weekly") == (100.0, 500.0)
    assert get_capacity_bounds("3p_5h") == (35.0, 150.0)
    assert get_capacity_bounds("gemini_weekly") == (350.0, 1000.0)
    assert get_capacity_bounds("gemini_5h") == (100.0, 400.0)

    # Add Claude step cost $15.00 and Gemini step cost $40.00
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c_mix', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Claude 3.7 Sonnet', 15.0)
    """)
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c_mix', 2, '2026-10-14T10:05:00Z', 'USER_MESSAGE', 'Gemini 3.8 Pro', 40.0)
    """)
    conn.commit()

    # claude-weekly bucket: used_fraction = 0.05.
    # Should ONLY count Claude steps ($15.00 / 0.05 = $300.00), NOT Gemini ($55.00 / 0.05 = $1100.00)
    cap_claude = calibrate_capacity(
        conn, "claude-weekly", 0.95, "2026-10-21T10:00:00Z", 200.0
    )
    assert cap_claude == 300.0

    conn.close()


def test_calibration_nan_and_corrupt_inputs(tmp_path: Path):
    """Verifies that NaN, Inf, and non-numeric inputs do not corrupt calibration."""
    db_file = tmp_path / "test_corrupt.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    conn.execute("""
        INSERT INTO steps (conversation_id, step_index, created_at, step_type, model_name, billed_cost)
        VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE', 'Gemini 3.8 Flash', 10.0)
    """)
    conn.commit()

    # NaN remaining_fraction should fall back safely to fallback_val (672.0)
    cap_nan = calibrate_capacity(
        conn, "gemini-weekly", float("nan"), "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_nan == 672.0

    # Inf remaining_fraction should fall back safely
    cap_inf = calibrate_capacity(
        conn, "gemini-weekly", float("inf"), "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_inf == 672.0

    # NaN in persisted capacity is sanitized to 0.0
    upsert_quota_state(
        conn,
        [
            {
                "bucket_id": "gemini-weekly",
                "calibrated_capacity": float("nan"),
            }
        ],
    )
    # With sanitized persisted capacity, initial calibration runs raw: 10.0 / 0.05 = 200.0 -> clamped to 350.0
    cap_nan_prior = calibrate_capacity(
        conn, "gemini-weekly", 0.95, "2026-10-21T10:00:00Z", 672.0
    )
    assert cap_nan_prior == 350.0

    conn.close()


def test_get_hourly_activity_profile_and_normalization(tmp_path: Path):
    """Verifies SQLite 24x7 activity extraction and Laplace-smoothed normalization."""
    assert MIN_PROFILE_STEPS == 50
    assert LAPLACE_ALPHA == 0.15

    db_file = tmp_path / "test_profile.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    # Insert 100 steps on Wednesday 2026-10-14 at 14:00 UTC (weekday=2, hour=14)
    for i in range(100):
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', ?, '2026-10-14T14:15:00Z', 'USER_MESSAGE')",
            (i + 1,),
        )
    conn.commit()

    profile = get_hourly_activity_profile(conn)
    assert profile["total_steps"] == 100
    assert len(profile["matrix"]) == 7
    assert all(len(row) == 24 for row in profile["matrix"])
    assert profile["matrix"][2][14] == 100

    weights, total = normalize_activity_profile(profile)
    assert total == 100
    assert weights is not None
    assert abs(sum(sum(r) for r in weights) - 1.0) < 1e-6
    # Peak bucket (2, 14) must be significantly higher than an offline bucket (0, 3)
    assert weights[2][14] > weights[0][3] * 20
    # Offline bucket must still have non-zero Laplace floor
    assert weights[0][3] > 0.0

    # Cold-start (< 50 steps) returns (None, total)
    cold_matrix = [[0] * 24 for _ in range(7)]
    cold_matrix[0][10] = 20
    w_cold, t_cold = normalize_activity_profile(
        {"matrix": cold_matrix, "total_steps": 20}
    )
    assert w_cold is None
    assert t_cold == 20

    # Malformed inputs return (None, 0)
    assert normalize_activity_profile(None) == (None, 0)
    assert normalize_activity_profile({"matrix": [[1, 2]]}) == (None, 0)
    assert normalize_activity_profile({}) == (None, 0)
    assert normalize_activity_profile({"matrix": None}) == (None, 0)
    assert normalize_activity_profile({"matrix": [[0] * 24 for _ in range(6)]}) == (
        None,
        0,
    )
    assert normalize_activity_profile({"matrix": [[-1] * 24 for _ in range(7)]}) == (
        None,
        0,
    )
    assert normalize_activity_profile(
        {"matrix": [[float("nan")] * 24 for _ in range(7)]}
    ) == (None, 0)
    assert normalize_activity_profile("not a matrix") == (None, 0)

    # Empty DB / None connection handling
    empty_profile = get_hourly_activity_profile(None)
    assert empty_profile["total_steps"] == 0
    assert len(empty_profile["matrix"]) == 7
    assert all(len(row) == 24 for row in empty_profile["matrix"])

    # Boundary conditions around MIN_PROFILE_STEPS = 50
    matrix_49 = [[0] * 24 for _ in range(7)]
    matrix_49[1][10] = 49
    w_49, t_49 = normalize_activity_profile({"matrix": matrix_49, "total_steps": 49})
    assert w_49 is None
    assert t_49 == 49

    matrix_50 = [[0] * 24 for _ in range(7)]
    matrix_50[1][10] = 50
    w_50, t_50 = normalize_activity_profile({"matrix": matrix_50, "total_steps": 50})
    assert w_50 is not None
    assert t_50 == 50
    assert abs(sum(sum(r) for r in w_50) - 1.0) < 1e-6

    # Bare matrix (list of lists) without dict wrapping
    w_bare, t_bare = normalize_activity_profile(matrix_50)
    assert w_bare is not None
    assert t_bare == 50

    # Boolean values are rejected as malformed
    assert normalize_activity_profile({"matrix": [[True] * 24 for _ in range(7)]}) == (
        None,
        0,
    )
    assert normalize_activity_profile({"matrix": matrix_50, "total_steps": True}) == (
        None,
        0,
    )

    # All-zero matrix with claimed total_steps is rejected as invalid
    assert normalize_activity_profile(
        {"matrix": [[0] * 24 for _ in range(7)], "total_steps": 100}
    ) == (None, 0)

    # Low-history matrix (<50 steps) claiming high total_steps still falls back to cold-start
    w_mismatch, t_mismatch = normalize_activity_profile(
        {"matrix": cold_matrix, "total_steps": 100}
    )
    assert w_mismatch is None
    assert t_mismatch == 20

    # High-history matrix (50 steps) with restricted total_steps (<50) respects cold-start
    w_rest, t_rest = normalize_activity_profile(
        {"matrix": matrix_50, "total_steps": 20}
    )
    assert w_rest is None
    assert t_rest == 20

    # Timestamp variations: lowercase 'z', lowercase 't', and space separator
    conn.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 101, '2026-10-15t10:15:00z', 'USER_MESSAGE')"
    )
    conn.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 102, '2026-10-16 11:20:00', 'USER_MESSAGE')"
    )
    conn.commit()
    p_var = get_hourly_activity_profile(conn)
    # Thursday (weekday=3, hour=10)
    assert p_var["matrix"][3][10] == 1
    # Friday (weekday=4, hour=11)
    assert p_var["matrix"][4][11] == 1
    assert p_var["total_steps"] == 102

    # Database with missing steps table degrades gracefully without raising
    empty_db = tmp_path / "empty_no_steps.db"
    conn_no_steps = sqlite3.connect(empty_db)
    p_no_steps = get_hourly_activity_profile(conn_no_steps)
    assert p_no_steps["total_steps"] == 0
    assert len(p_no_steps["matrix"]) == 7
    conn_no_steps.close()

    conn.close()


def test_get_hourly_activity_profile_caching(tmp_path: Path):
    """Verifies that get_hourly_activity_profile caches queries and invalidates properly."""
    import time
    from unittest.mock import patch
    from context_monitor.db import get_db_connection, init_db
    from context_monitor.quota import (
        get_hourly_activity_profile,
        invalidate_activity_profile_cache,
    )

    db_file = tmp_path / "test_caching.db"
    init_db(db_file)
    conn = get_db_connection(db_file)
    conn.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', ?, '2026-10-14T14:15:00Z', 'USER_MESSAGE')",
            (i + 1,),
        )
    conn.commit()

    invalidate_activity_profile_cache()

    p1 = get_hourly_activity_profile(conn)
    assert p1["total_steps"] == 10

    # Mutating returned profile must not affect cache
    p1["matrix"][2][14] = 999999
    p1["total_steps"] = 999999

    # Cache hit: should return pristine cached profile
    p2 = get_hourly_activity_profile(conn)
    assert p2["total_steps"] == 10
    assert p2["matrix"][2][14] == 10

    # force_refresh=True should bypass/refresh cache
    p3 = get_hourly_activity_profile(conn, force_refresh=True)
    assert p3["total_steps"] == 10

    # Invalidate cache explicitly
    invalidate_activity_profile_cache()
    p4 = get_hourly_activity_profile(conn)
    assert p4["total_steps"] == 10

    # Invalidate specific db_path
    invalidate_activity_profile_cache(db_path=db_file)
    p5 = get_hourly_activity_profile(conn)
    assert p5["total_steps"] == 10

    # Check TTL expiration: mock time advancing > 300s and verify database is re-queried
    t_start = time.monotonic()
    with patch("time.monotonic", return_value=t_start + 301.0):
        p6 = get_hourly_activity_profile(conn)
        assert p6["total_steps"] == 10

    # None connection should return empty profile safely without error
    empty_p = get_hourly_activity_profile(None)
    assert empty_p["total_steps"] == 0

    conn.close()


def test_get_hourly_activity_profile_multi_connection_reactivity(tmp_path: Path):
    """Verifies that commits from another connection reactively invalidate the cache."""
    from context_monitor.db import get_db_connection, init_db
    from context_monitor.quota import (
        get_hourly_activity_profile,
        invalidate_activity_profile_cache,
    )

    db_file = tmp_path / "test_multi_conn.db"
    init_db(db_file)
    invalidate_activity_profile_cache()

    conn1 = get_db_connection(db_file)
    conn1.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    conn1.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 1, '2026-10-14T14:15:00Z', 'USER_MESSAGE')"
    )
    conn1.commit()

    # conn1 caches 1 step
    p1 = get_hourly_activity_profile(conn1)
    assert p1["total_steps"] == 1

    # conn2 writes and commits a new step
    conn2 = get_db_connection(db_file)
    conn2.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 2, '2026-10-14T15:15:00Z', 'USER_MESSAGE')"
    )
    conn2.commit()
    conn2.close()

    # conn1 query immediately reflects conn2's committed step without manual invalidation
    p2 = get_hourly_activity_profile(conn1)
    assert p2["total_steps"] == 2

    conn1.close()


def test_get_hourly_activity_profile_uncommitted_isolation(tmp_path: Path):
    """Verifies uncommitted transactions do not pollute shared cache or leak across connections."""
    from context_monitor.db import get_db_connection, init_db
    from context_monitor.quota import (
        get_hourly_activity_profile,
        invalidate_activity_profile_cache,
    )

    db_file = tmp_path / "test_isolation.db"
    init_db(db_file)
    invalidate_activity_profile_cache()

    conn1 = get_db_connection(db_file)
    conn1.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    conn1.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 1, '2026-10-14T14:15:00Z', 'USER_MESSAGE')"
    )
    conn1.commit()

    # Seed clean committed cache
    p_committed = get_hourly_activity_profile(conn1)
    assert p_committed["total_steps"] == 1

    # conn1 starts an uncommitted transaction
    conn1.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 2, '2026-10-14T16:15:00Z', 'USER_MESSAGE')"
    )
    assert conn1.in_transaction is True

    # conn1 sees its own uncommitted step
    p_uncommitted = get_hourly_activity_profile(conn1)
    assert p_uncommitted["total_steps"] == 2

    # conn2 must NOT see conn1's uncommitted step (dirty read prevented)
    conn2 = get_db_connection(db_file)
    p_conn2 = get_hourly_activity_profile(conn2)
    assert p_conn2["total_steps"] == 1
    conn2.close()

    # conn1 rolls back
    conn1.rollback()
    assert conn1.in_transaction is False

    # conn1 now returns the clean committed state
    p_after_rollback = get_hourly_activity_profile(conn1)
    assert p_after_rollback["total_steps"] == 1

    conn1.close()


def test_get_hourly_activity_profile_in_memory_and_cleanup():
    """Verifies that in-memory databases cache correctly and clean up on invalidation."""
    import sqlite3
    from context_monitor.quota import (
        _PROFILE_CACHE,
        get_hourly_activity_profile,
        invalidate_activity_profile_cache,
    )

    invalidate_activity_profile_cache()

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE steps (conversation_id TEXT, step_index INT, created_at TEXT, step_type TEXT)"
    )
    conn.execute(
        "INSERT INTO steps VALUES ('c1', 1, '2026-10-14T10:00:00Z', 'USER_MESSAGE')"
    )
    conn.commit()

    p1 = get_hourly_activity_profile(conn)
    assert p1["total_steps"] == 1

    # Cache hit
    p2 = get_hourly_activity_profile(conn)
    assert p2["total_steps"] == 1

    # In-memory update via same connection
    conn.execute(
        "INSERT INTO steps VALUES ('c1', 2, '2026-10-14T11:00:00Z', 'USER_MESSAGE')"
    )
    conn.commit()
    p3 = get_hourly_activity_profile(conn)
    assert p3["total_steps"] == 2

    cache_key = f"conn_{id(conn)}"
    assert cache_key in _PROFILE_CACHE

    # Invalidate cache for this connection
    invalidate_activity_profile_cache(conn)
    assert cache_key not in _PROFILE_CACHE

    conn.close()


def test_get_hourly_activity_profile_recreated_db(tmp_path: Path):
    """Verifies that unlinking and recreating a db file at the same path invalidates cache."""
    import os
    from context_monitor.db import get_db_connection, init_db
    from context_monitor.quota import (
        get_hourly_activity_profile,
        invalidate_activity_profile_cache,
    )

    db_file = tmp_path / "test_recreate.db"
    init_db(db_file)
    invalidate_activity_profile_cache()

    conn1 = get_db_connection(db_file)
    conn1.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c1', 'T1', 'Gemini 3.8 Flash')"
    )
    conn1.execute(
        "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c1', 1, '2026-10-14T14:15:00Z', 'USER_MESSAGE')"
    )
    conn1.commit()

    p1 = get_hourly_activity_profile(conn1)
    assert p1["total_steps"] == 1
    conn1.close()

    # Recreate the file with 3 steps
    os.remove(db_file)
    init_db(db_file)
    conn2 = get_db_connection(db_file)
    conn2.execute(
        "INSERT INTO conversations (id, title, model_name) VALUES ('c2', 'T2', 'Gemini 3.8 Flash')"
    )
    for i in range(3):
        conn2.execute(
            "INSERT INTO steps (conversation_id, step_index, created_at, step_type) VALUES ('c2', ?, '2026-10-14T14:15:00Z', 'USER_MESSAGE')",
            (i + 1,),
        )
    conn2.commit()

    p2 = get_hourly_activity_profile(conn2)
    assert p2["total_steps"] == 3
    conn2.close()


def test_invalidate_activity_profile_cache_export():
    import context_monitor

    assert hasattr(context_monitor, "invalidate_activity_profile_cache")
    assert callable(context_monitor.invalidate_activity_profile_cache)


def test_calculate_quota_pacing_activity_weighted_morning_sprint():
    """Verifies that a 4-hour weekday morning sprint with an activity profile
    does not project as a 24x7 non-stop burn across overnight/weekend hours."""
    matrix = [[0] * 24 for _ in range(7)]
    for day in range(5):  # Mon(0) .. Fri(4)
        for hr in range(9, 17):
            matrix[day][hr] = 10
    profile = {"matrix": matrix, "total_steps": 400}

    now = datetime(2026, 10, 12, 13, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-19T09:00:00Z"

    res = calculate_quota_pacing(
        0.92,
        reset_iso,
        window_hours=168.0,
        calibrated_capacity=672.0,
        now=now,
        activity_profile=profile,
    )
    assert res["weighting_mode"] == "activity_weighted"
    assert res["linear_projected_pct"] > 300.0
    assert 80.0 <= res["projected_pct"] <= 100.0
    assert res["status"] == "moderate"
    assert res["elapsed_activity_pct"] > 8.0
    assert res["hours_until_exhaustion"] is None


def test_calculate_quota_pacing_overnight_exhaustion_horizon():
    """Verifies that hours_until_exhaustion accounts for low-activity overnight hours."""
    matrix = [[0] * 24 for _ in range(7)]
    for day in range(7):
        for hr in range(9, 17):
            matrix[day][hr] = 20
    profile = {"matrix": matrix, "total_steps": 1120}

    now = datetime(2026, 10, 14, 17, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-19T17:00:00Z"

    res_weighted = calculate_quota_pacing(
        0.20, reset_iso, window_hours=168.0, now=now, activity_profile=profile
    )
    res_linear = calculate_quota_pacing(
        0.20, reset_iso, window_hours=168.0, now=now, activity_profile=None
    )
    assert res_weighted["status"] == "exceeding"
    assert res_weighted["hours_until_exhaustion"] > res_linear["hours_until_exhaustion"]


def test_integrate_profile_weight_and_boundary_integration():
    """Verifies exact interval integration across hour boundaries, sub-hour seconds, and full weeks."""
    uniform_weight = 1.0 / 168.0
    weights = [[uniform_weight] * 24 for _ in range(7)]

    # 1. Exact 168-hour week equals 1.0
    start = datetime(2026, 10, 12, 9, 15, 30, tzinfo=timezone.utc)
    end = start + timedelta(hours=168)
    w_week = _integrate_profile_weight(weights, start, end)
    assert abs(w_week - 1.0) < 1e-6

    # 2. Sub-hour partial segment (30 minutes in single hour)
    t1 = datetime(2026, 10, 12, 10, 15, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 12, 10, 45, 0, tzinfo=timezone.utc)
    w_half = _integrate_profile_weight(weights, t1, t2)
    assert abs(w_half - 0.5 * uniform_weight) < 1e-7

    # 3. Straddling hour boundary (10:45 to 11:15 = 15m in h=10, 15m in h=11)
    t3 = datetime(2026, 10, 12, 10, 45, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 10, 12, 11, 15, 0, tzinfo=timezone.utc)
    w_straddle = _integrate_profile_weight(weights, t3, t4)
    assert abs(w_straddle - 0.5 * uniform_weight) < 1e-7

    # 4. Inverted or zero interval returns 0.0
    assert _integrate_profile_weight(weights, t2, t1) == 0.0
    assert _integrate_profile_weight(weights, t1, t1) == 0.0


def test_estimate_hours_until_exhaustion_weighted_edge_cases():
    """Verifies weighted exhaustion horizon with overnight pauses and timeout termination."""
    matrix = [[0] * 24 for _ in range(7)]
    for d in range(5):
        for h in range(9, 17):
            matrix[d][h] = 50
    weights, _ = normalize_activity_profile({"matrix": matrix, "total_steps": 2000})
    assert weights is not None

    now = datetime(2026, 10, 12, 17, 0, 0, tzinfo=timezone.utc)
    # Zero or negative needed weight returns 0.0
    assert _estimate_hours_until_exhaustion_weighted(weights, now, 0.0) == 0.0
    assert _estimate_hours_until_exhaustion_weighted(weights, now, -0.05) == 0.0

    # Exhaustion requiring weight that cannot be met within max_hours returns None
    assert (
        _estimate_hours_until_exhaustion_weighted(weights, now, 50.0, max_hours=48.0)
        is None
    )


def test_calculate_quota_pacing_backwards_compatibility_and_fallbacks():
    """Verifies that calling calculate_quota_pacing without activity_profile or with cold-start/invalid profiles falls back cleanly."""
    reset_iso = "2026-10-19T12:00:00Z"
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # 1. No activity profile: weighting_mode='linear'
    res_none = calculate_quota_pacing(0.50, reset_iso, window_hours=168.0, now=now)
    assert res_none["weighting_mode"] == "linear"
    assert res_none["elapsed_activity_pct"] == 57.14
    assert res_none["linear_projected_pct"] == res_none["projected_pct"]

    # 2. Cold start profile (< 50 steps): falls back to linear
    cold_profile = {"matrix": [[0] * 24 for _ in range(7)], "total_steps": 10}
    cold_profile["matrix"][0][10] = 10
    res_cold = calculate_quota_pacing(
        0.50, reset_iso, window_hours=168.0, now=now, activity_profile=cold_profile
    )
    assert res_cold["weighting_mode"] == "linear"
    assert res_cold["elapsed_activity_pct"] == 57.14

    # 3. Malformed profile: falls back to linear
    res_malformed = calculate_quota_pacing(
        0.50,
        reset_iso,
        window_hours=168.0,
        now=now,
        activity_profile={"matrix": "invalid"},
    )
    assert res_malformed["weighting_mode"] == "linear"

    # 4. Early-cycle guard: linear without profile
    early_now = datetime(2026, 10, 12, 13, 0, 0, tzinfo=timezone.utc)
    early_reset = "2026-10-19T12:00:00Z"  # only 1h elapsed < 2.5h min
    res_early_linear = calculate_quota_pacing(
        0.98, early_reset, window_hours=168.0, now=early_now, activity_profile=None
    )
    assert res_early_linear["weighting_mode"] == "linear"
    assert res_early_linear["is_calibrating"] is True
    assert res_early_linear["status"] == "calibrating"
    assert res_early_linear["projected_pct"] == 2.0
    assert res_early_linear["linear_projected_pct"] == 2.0

    # 5. Early-cycle guard: with active activity profile
    valid_profile = {"matrix": [[10] * 24 for _ in range(7)], "total_steps": 1680}
    res_early_profile = calculate_quota_pacing(
        0.98,
        early_reset,
        window_hours=168.0,
        now=early_now,
        activity_profile=valid_profile,
    )
    assert res_early_profile["weighting_mode"] == "activity_weighted"
    assert res_early_profile["is_calibrating"] is True
    assert res_early_profile["status"] == "calibrating"
    assert res_early_profile["projected_pct"] == 2.0
    assert res_early_profile["linear_projected_pct"] == 2.0
    assert res_early_profile["hours_until_exhaustion"] is None
    assert res_early_profile["days_until_exhaustion"] is None


def test_calculate_quota_metrics_forwards_activity_profile():
    """Verifies that calculate_quota_metrics accepts and forwards activity_profile to pacing."""
    matrix = [[0] * 24 for _ in range(7)]
    for day in range(5):
        for hr in range(9, 17):
            matrix[day][hr] = 10
    profile = {"matrix": matrix, "total_steps": 400}

    now = datetime(2026, 10, 12, 13, 0, 0, tzinfo=timezone.utc)
    state = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 0.92,
            "reset_time": "2026-10-19T09:00:00Z",
            "calibrated_capacity": 672.0,
        }
    ]

    # Passing activity_profile directly as kwarg
    m = calculate_quota_metrics(
        billed_cost=1.0,
        model_name="Gemini 3.8 Flash (High)",
        quota_state=state,
        now=now,
        activity_profile=profile,
    )
    assert m["weekly_pacing"]["weighting_mode"] == "activity_weighted"
    assert "elapsed_activity_pct" in m["weekly_pacing"]
    assert "linear_projected_pct" in m["weekly_pacing"]
    assert m["projected_weekly_pct"] == m["weekly_pacing"]["projected_pct"]

    # Passing activity_profile via kwargs
    m_kw = calculate_quota_metrics(
        billed_cost=1.0,
        model_name="Gemini 3.8 Flash (High)",
        quota_state=state,
        now=now,
        **{"activity_profile": profile},
    )
    assert m_kw["weekly_pacing"]["weighting_mode"] == "activity_weighted"


def test_normalize_activity_profile_pre_normalized_with_zeros_and_cold_start():
    """Verifies that pre-normalized matrices with zero-weight cells are preserved,
    while explicitly specified cold-start step counts (< 50) are respected."""
    # 40 active hours with 0.025 weight, 128 inactive hours with 0.0 weight -> sum = 1.0
    matrix_with_zeros = [[0.0] * 24 for _ in range(7)]
    for d in range(5):
        for h in range(9, 17):
            matrix_with_zeros[d][h] = 0.025
    assert abs(sum(sum(r) for r in matrix_with_zeros) - 1.0) < 1e-6

    # 1. Bare pre-normalized matrix with zero cells is accepted
    w_bare, t_bare = normalize_activity_profile(matrix_with_zeros)
    assert w_bare is not None
    assert abs(sum(sum(r) for r in w_bare) - 1.0) < 1e-6
    assert t_bare >= MIN_PROFILE_STEPS

    # 2. Pre-normalized matrix wrapped in dict with explicit high total_steps
    w_high, t_high = normalize_activity_profile(
        {"matrix": matrix_with_zeros, "total_steps": 250}
    )
    assert w_high is not None
    assert t_high == 250

    # 3. Pre-normalized matrix wrapped in dict with explicit cold-start total_steps (< 50)
    w_cold, t_cold = normalize_activity_profile(
        {"matrix": matrix_with_zeros, "total_steps": 20}
    )
    assert w_cold is None
    assert t_cold == 20


def test_integrate_and_estimate_exhaustion_malformed_inputs():
    """Verifies that helper functions degrade gracefully on malformed/invalid inputs without exceptions."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(hours=10)

    # _integrate_profile_weight
    assert _integrate_profile_weight(None, now, later) == 0.0
    assert _integrate_profile_weight([], now, later) == 0.0
    assert _integrate_profile_weight([[0.0] * 10], now, later) == 0.0
    assert _integrate_profile_weight([[0.0] * 24 for _ in range(5)], now, later) == 0.0
    assert _integrate_profile_weight([[0.0] * 24 for _ in range(7)], None, later) == 0.0
    assert (
        _integrate_profile_weight([[0.0] * 24 for _ in range(7)], "bad-start", later)
        == 0.0
    )

    # _estimate_hours_until_exhaustion_weighted
    assert _estimate_hours_until_exhaustion_weighted(None, now, 0.5) is None
    assert _estimate_hours_until_exhaustion_weighted([], now, 0.5) is None
    assert _estimate_hours_until_exhaustion_weighted([[0.0] * 10], now, 0.5) is None
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(5)], now, 0.5
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], None, 0.5
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], "bad-time", 0.5
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], now, float("nan")
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], now, float("inf")
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], now, 0.5, max_hours=-10.0
        )
        is None
    )
    assert (
        _estimate_hours_until_exhaustion_weighted(
            [[0.0] * 24 for _ in range(7)], now, 0.5, max_hours=0.0
        )
        is None
    )


def test_calculate_quota_pacing_activity_weighted_morning_sprint_exceeding_exhaustion():
    """Verifies that a heavy morning sprint exceeding 100% quota projects exhaustion
    across overnight low-activity periods (taking much longer than naive 24x7 burn rate)."""
    # Mon-Fri 09:00-17:00 UTC active
    matrix = [[0] * 24 for _ in range(7)]
    for day in range(5):
        for hr in range(9, 17):
            matrix[day][hr] = 10
    profile = {"matrix": matrix, "total_steps": 400}

    # Cycle started Monday 09:00 UTC, now is Monday 13:00 UTC (4h elapsed). Reset is next Monday 09:00 UTC.
    now = datetime(2026, 10, 12, 13, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-19T09:00:00Z"

    # User consumed 15% quota in 4 hours (remaining_fraction = 0.85).
    # Linear projection: 15% / (4 / 168) = 630.0%!
    # Linear exhaustion: 85% remaining / (15% / 4h = 3.75%/h) = 22.7 hours (Tuesday morning).
    # Activity-weighted: In 4 active hours out of 40 weekly active hours, 15% used -> projected ~169%.
    # Schedule-aware exhaustion: Today only 4 active hours remain (13:00-17:00), then 16h offline.
    # Exhaustion spans across Tuesday and into Wednesday (> 40h wall clock).
    res = calculate_quota_pacing(
        0.85,
        reset_iso,
        window_hours=168.0,
        calibrated_capacity=672.0,
        now=now,
        activity_profile=profile,
    )
    assert res["weighting_mode"] == "activity_weighted"
    assert res["status"] == "exceeding"
    assert res["projected_pct"] > 100.0
    assert res["linear_projected_pct"] > 600.0
    assert res["hours_until_exhaustion"] is not None
    # Schedule-aware exhaustion must be significantly longer than linear 22.7h due to overnight sleep
    assert res["hours_until_exhaustion"] > 40.0
    assert res["days_until_exhaustion"] is not None
    assert res["days_until_exhaustion"] > 1.5


def test_calculate_quota_pacing_5h_window_with_activity_profile():
    """Verifies that 5-hour rolling windows properly integrate activity weights."""
    matrix = [[0] * 24 for _ in range(7)]
    for day in range(7):
        for hr in range(9, 17):
            matrix[day][hr] = 20
    profile = {"matrix": matrix, "total_steps": 1120}

    # Wednesday 13:00 UTC. 5h reset at 15:00 UTC (start was 10:00 UTC).
    # 3 active hours elapsed (10:00-13:00), 2 active hours remaining (13:00-15:00).
    now = datetime(2026, 10, 14, 13, 0, 0, tzinfo=timezone.utc)
    reset_5h = "2026-10-14T15:00:00Z"

    res = calculate_quota_pacing(
        0.70,
        reset_5h,
        window_hours=5.0,
        calibrated_capacity=150.0,
        now=now,
        activity_profile=profile,
    )
    assert res["weighting_mode"] == "activity_weighted"
    assert res["elapsed_hours"] == 3.0
    assert res["remaining_hours"] == 2.0
    assert abs(res["elapsed_activity_pct"] - 60.0) < 1.0  # 3 of 5 equal-weight hours
    assert abs(res["projected_pct"] - 50.0) < 1.0  # 30% used / 60% elapsed = 50%
    assert res["status"] == "on_track"


def test_calculate_quota_pacing_fully_exhausted_activity_weighted():
    """Verifies that remaining_fraction = 0.0 with activity profile produces 0h exhaustion."""
    matrix = [[10] * 24 for _ in range(7)]
    profile = {"matrix": matrix, "total_steps": 1680}

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-20T16:00:00Z"

    res = calculate_quota_pacing(
        0.0,
        reset_iso,
        window_hours=168.0,
        calibrated_capacity=500.0,
        now=now,
        activity_profile=profile,
    )
    assert res["used_pct"] == 100.0
    assert res["status"] == "exceeding"
    assert res["hours_until_exhaustion"] == 0.0
    assert res["days_until_exhaustion"] == 0.0


def test_calculate_quota_pacing_velocity_telemetry(tmp_path: Path):
    """Verifies that calculate_quota_pacing computes window-scoped billed_tph, est_tph, and billed_tpm."""
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-20T16:00:00Z"  # 148h ahead -> 20h elapsed in 168h window

    # Test explicit token counts passed directly
    res_direct = calculate_quota_pacing(
        0.885,
        reset_iso,
        window_hours=168.0,
        calibrated_capacity=500.0,
        now=now,
        billed_tokens=200_000,
        est_tokens=20_000,
    )
    assert res_direct["billed_tph"] == 10_000.0  # 200,000 / 20.0 hrs
    assert res_direct["est_tph"] == 1_000.0  # 20,000 / 20.0 hrs
    assert res_direct["billed_tpm"] == round(10_000.0 / 60.0, 2)  # 166.67

    # Test database-backed token query
    db_file = tmp_path / "test_pacing_vel.db"
    init_db(db_file)
    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test Conv')")
        # Step within elapsed window (now is 2026-10-14 12:00, cycle started 2026-10-13 16:00)
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens, model_name)
            VALUES ('c1', 1, '2026-10-14T10:00:00Z', 50_000, 300_000, 100_000, 'Gemini 3.8 Flash')
            """
        )
        conn.commit()

        res_db = calculate_quota_pacing(
            0.885,
            reset_iso,
            window_hours=168.0,
            calibrated_capacity=500.0,
            now=now,
            conn=conn,
        )
        assert res_db["billed_tph"] == 20_000.0  # 400,000 / 20.0 hrs
        assert res_db["est_tph"] == 2_500.0  # 50,000 / 20.0 hrs
        assert res_db["billed_tpm"] == round(20_000.0 / 60.0, 2)

        # Test calculate_quota_metrics forwards conn to calculate_quota_pacing
        mock_state = [
            {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.885,
                "reset_time": reset_iso,
                "calibrated_capacity": 500.0,
            }
        ]
        qm = calculate_quota_metrics(
            billed_cost=1.5,
            model_name="Gemini 3.8 Flash",
            quota_state=mock_state,
            now=now,
            conn=conn,
        )
        assert "billed_tph" in qm["gemini-weekly"]["pacing"]
        assert qm["gemini-weekly"]["pacing"]["billed_tph"] == 20_000.0

        # Test forwarding billed_tokens and est_tokens directly through calculate_quota_metrics
        qm_direct = calculate_quota_metrics(
            billed_cost=1.5,
            model_name="Gemini 3.8 Flash",
            quota_state=mock_state,
            now=now,
            billed_tokens=400_000,
            est_tokens=50_000,
        )
        assert qm_direct["weekly_billed_tph"] == 20_000.0
        assert qm_direct["gemini-weekly"]["pacing"]["billed_tph"] == 20_000.0


def test_calculate_quota_pacing_sub_minute_clamp_and_non_finite_inputs():
    """Verifies that sub-minute elapsed time clamps denominator to 60s and non-finite inputs are cleaned."""
    now = datetime(2026, 10, 14, 12, 0, 5, tzinfo=timezone.utc)
    reset_iso = "2026-10-21T12:00:00Z"  # 168h window, 5s elapsed

    # 5 seconds elapsed with 100k tokens: clamped to 1/60 hr denominator
    res = calculate_quota_pacing(
        0.999,
        reset_iso,
        window_hours=168.0,
        now=now,
        billed_tokens=100_000,
        est_tokens=10_000,
    )
    # Expected with 1/60 hr denominator:
    # billed_tph: 100_000 * 60 = 6_000_000.0 (not 72,000,000.0 from 5s)
    # billed_tpm: 6_000_000.0 / 60 = 100_000.0
    assert res["billed_tph"] == 6_000_000.0
    assert res["billed_tpm"] == 100_000.0
    assert res["est_tph"] == 600_000.0

    # Non-finite tokens (NaN / Inf)
    res_nan = calculate_quota_pacing(
        0.90,
        "2026-10-20T16:00:00Z",
        now=datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc),
        billed_tokens=float("nan"),  # type: ignore
        est_tokens=float("inf"),  # type: ignore
    )
    assert res_nan["billed_tph"] == 0.0
    assert res_nan["est_tph"] == 0.0
    assert res_nan["billed_tpm"] == 0.0


def test_calculate_quota_pacing_boundary_z_timestamp_inclusion(tmp_path: Path):
    """Verifies that steps at the exact boundary of now with 'Z' suffix are correctly included."""
    db_file = tmp_path / "test_boundary_z.db"
    init_db(db_file)

    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    reset_iso = "2026-10-20T16:00:00Z"  # 20h elapsed

    with get_db_connection(db_file) as conn:
        conn.execute("INSERT INTO conversations (id, title) VALUES ('c1', 'Test Conv')")
        # Step at exact now timestamp with 'Z'
        conn.execute(
            """
            INSERT INTO steps (conversation_id, step_index, created_at, est_tokens, billed_input_tokens, billed_output_tokens, model_name)
            VALUES ('c1', 1, '2026-10-14T12:00:00Z', 10_000, 80_000, 20_000, 'Gemini 3.8 Flash')
            """
        )
        conn.commit()

        res = calculate_quota_pacing(
            0.885,
            reset_iso,
            window_hours=168.0,
            now=now,
            conn=conn,
        )

    # 100,000 billed tokens / 20.0 hrs = 5,000.0
    assert res["billed_tph"] == 5_000.0
    assert res["est_tph"] == 500.0
