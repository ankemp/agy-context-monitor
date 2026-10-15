"""
Antigravity Quota Client & Dynamic Capacity Calibration Engine
--------------------------------------------------------------
Discovers the local Antigravity Language Server RPC endpoint, fetches
live bucket fractions (gemini-weekly, gemini-5h, 3p-weekly, 3p-5h),
caches them in SQLite (quota_state table) with a 60s TTL, and dynamically
calibrates subscription window capacity against recorded step costs.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from context_monitor.db import get_quota_state, upsert_quota_state
from context_monitor.pricing import get_pricing_key
from context_monitor.settings import (
    get_3p_5h_capacity,
    get_3p_weekly_capacity,
    get_gemini_5h_capacity,
    get_gemini_weekly_capacity,
    get_quota_cache_ttl,
    get_quota_request_timeout,
)

__all__ = [
    "discover_language_server_credentials",
    "discover_language_server",
    "fetch_raw_quota_summary",
    "fetch_live_quota_from_ls",
    "calibrate_capacity",
    "sync_and_get_quota_state",
    "calculate_quota_metrics",
    "calculate_quota_pacing",
    "format_reset_countdown",
    "format_quota_pct",
    "QuotaStateList",
    "CAPACITY_BOUNDS",
    "get_capacity_bounds",
    "MIN_PROFILE_STEPS",
    "LAPLACE_ALPHA",
    "get_hourly_activity_profile",
    "invalidate_activity_profile_cache",
    "normalize_activity_profile",
]

MIN_PROFILE_STEPS: int = 50
LAPLACE_ALPHA: float = 0.15

DEFAULT_LOG_PATH = Path(
    os.path.expanduser("~/Library/Logs/Antigravity/language_server.log")
)


class QuotaStateList(list):
    """List of quota state dictionaries supporting bucket_id key indexing and dict-like inspection."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and (
                    item.get("bucket_id") == key or item.get("bucketId") == key
                ):
                    return item
            raise KeyError(f"Bucket '{key}' not found in quota state")
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str):
            return any(
                isinstance(item, dict)
                and (item.get("bucket_id") == key or item.get("bucketId") == key)
                for item in self
            )
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and (
                    item.get("bucket_id") == key or item.get("bucketId") == key
                ):
                    return item
            return default
        if isinstance(key, int):
            if 0 <= key < len(self):
                return self[key]
            return default
        return default

    def keys(self) -> list[str]:
        """Returns bucket_id keys for all buckets in the list."""
        return [
            item.get("bucket_id") or item.get("bucketId", "")
            for item in self
            if isinstance(item, dict)
            and (item.get("bucket_id") or item.get("bucketId"))
        ]

    def values(self) -> list[dict]:
        """Returns bucket dictionaries in the list."""
        return [item for item in self if isinstance(item, dict)]

    def items(self) -> list[tuple[str, dict]]:
        """Returns (bucket_id, bucket_dict) tuples."""
        return [
            (item.get("bucket_id") or item.get("bucketId", ""), item)
            for item in self
            if isinstance(item, dict)
            and (item.get("bucket_id") or item.get("bucketId"))
        ]

    def to_dict(self) -> dict[str, dict]:
        """Converts list of bucket records into a dict keyed by bucket_id."""
        return {
            (item.get("bucket_id") or item.get("bucketId", "")): item
            for item in self
            if isinstance(item, dict)
            and (item.get("bucket_id") or item.get("bucketId"))
        }


def _to_utc_datetime(
    val: str | datetime | None,
    default_now: bool = False,
) -> datetime | None:
    """Normalizes a datetime, ISO string, or None into a timezone-aware UTC datetime."""
    if val is None or val == "":
        return datetime.now(timezone.utc) if default_now else None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if isinstance(val, str):
        try:
            s = val.strip()
            if s.endswith(("z", "Z")):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc) if default_now else None
    return datetime.now(timezone.utc) if default_now else None


def _get_default_capacity(bucket_id: str) -> float:
    """Returns baseline capacity from settings for a given bucket_id."""
    bid = (bucket_id or "").lower()
    if bid == "gemini-weekly":
        return get_gemini_weekly_capacity()
    if bid == "gemini-5h":
        return get_gemini_5h_capacity()
    if bid == "3p-weekly":
        return get_3p_weekly_capacity()
    if bid == "3p-5h":
        return get_3p_5h_capacity()
    if "weekly" in bid:
        return (
            get_gemini_weekly_capacity()
            if "gemini" in bid
            else get_3p_weekly_capacity()
        )
    return get_gemini_5h_capacity() if "gemini" in bid else get_3p_5h_capacity()


def discover_language_server_credentials(
    log_path: Path | str | None = None,
) -> tuple[int | None, str | None]:
    """
    Discovers the Antigravity Language Server HTTP port and CSRF token.

    Returns:
        tuple[int | None, str | None]: (http_port, csrf_token) or (None, None) on failure.
    """
    token = _discover_csrf_token()
    port = _discover_http_port(log_path)
    return port, token


discover_language_server = discover_language_server_credentials


def _discover_csrf_token() -> str | None:
    """Discovers CSRF token from running language_server process args."""
    env_token = os.environ.get("AGY_CSRF_TOKEN")
    if env_token:
        return env_token.strip()

    ps_commands = [
        ["ps", "-ww", "-eo", "pid,command"],
        ["ps", "aux"],
        ["ps", "-ef"],
    ]

    for cmd in ps_commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0:
                for line in reversed(proc.stdout.splitlines()):
                    if "language_server" not in line or "grep" in line:
                        continue
                    m = re.search(r"--csrf_token(?:=|\s+)([^\s]+)", line)
                    if m:
                        return m.group(1).strip().strip("'\"")
        except Exception:
            continue

    return None


def _discover_http_port(log_file: Path | str | None = None) -> int | None:
    r"""
    Discovers HTTP port from language_server.log matching
    'listening on random port at (\d+) for HTTP\b'.
    Searches backward from the end of the file to handle large logs efficiently.
    """
    env_port = os.environ.get("AGY_HTTP_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass

    if log_file is not None:
        path = Path(log_file)
        if not path.exists() or not path.is_file():
            return None
    else:
        for candidate in [
            DEFAULT_LOG_PATH,
            Path(os.path.expanduser("~/.config/Antigravity/logs/language_server.log")),
            Path(
                os.path.expanduser(
                    "~/.local/state/Antigravity/logs/language_server.log"
                )
            ),
        ]:
            if candidate.exists() and candidate.is_file():
                path = candidate
                break
        else:
            return None

    try:
        file_size = path.stat().st_size
        chunk_size = 65536  # 64 KB chunks
        max_bytes_to_scan = (
            100 * 1024 * 1024
        )  # Scan up to 100MB backward (handles long-running sessions)

        pattern = re.compile(rb"listening on random port at (\d+) for HTTP\b")

        with open(path, "rb") as f:
            bytes_scanned = 0
            offset = file_size
            remainder = b""

            while offset > 0 and bytes_scanned < max_bytes_to_scan:
                read_len = min(chunk_size, offset)
                offset -= read_len
                f.seek(offset)
                chunk = f.read(read_len) + remainder
                bytes_scanned += read_len

                matches = pattern.findall(chunk)
                if matches:
                    return int(matches[-1].decode("ascii"))

                remainder = chunk[:128]
    except Exception:
        return None

    return None


def fetch_raw_quota_summary(
    port: int,
    csrf_token: str,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """
    POSTs to RetrieveUserQuotaSummary on local Language Server ConnectRPC endpoint.

    Args:
        port: Local HTTP port.
        csrf_token: CSRF token.
        timeout: Socket timeout in seconds (default 1.5s).

    Returns:
        Parsed JSON dictionary or None on failure/timeout without raising.
    """
    if not port or not csrf_token:
        return None

    if timeout is None:
        timeout = get_quota_request_timeout()

    url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": csrf_token,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                raw_bytes = response.read()
                return json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None

    return None


fetch_live_quota_from_ls = fetch_raw_quota_summary


# Clamping bounds for dynamically calibrated subscription capacities (min_usd, max_usd)
CAPACITY_BOUNDS: dict[str, tuple[float, float]] = {
    "gemini-weekly": (350.0, 1000.0),
    "gemini-5h": (100.0, 400.0),
    "3p-weekly": (100.0, 500.0),
    "3p-5h": (35.0, 150.0),
    "claude-weekly": (100.0, 500.0),
    "claude-5h": (35.0, 150.0),
}


def get_capacity_bounds(bucket_id: str) -> tuple[float, float]:
    """Returns (min_capacity, max_capacity) clamping bounds for a given bucket family."""
    bid = (bucket_id or "").strip().lower().replace("_", "-")
    if bid in CAPACITY_BOUNDS:
        return CAPACITY_BOUNDS[bid]
    is_5h = "5h" in bid
    is_3p = "3p" in bid or "claude" in bid or "gpt" in bid
    if is_3p:
        return (35.0, 150.0) if is_5h else (100.0, 500.0)
    return (100.0, 400.0) if is_5h else (350.0, 1000.0)


def calibrate_capacity(
    conn: sqlite3.Connection,
    bucket_id: str,
    remaining_fraction: float,
    reset_time: str,
    fallback_capacity: float,
) -> float:
    """
    Calibrates window compute capacity against steps recorded in SQLite:
        raw_calibrated = sum(billed_cost) / (1 - remaining_fraction)

    Stabilization safeguards:
    - If used_fraction < 0.01 or sum(billed_cost) < 0.50:
      returns existing persisted calibrated_capacity if > 0 else fallback_capacity
      (clamped to bucket bounds).
    - Applies Exponential Moving Average (EMA) smoothing when a valid prior
      persisted_capacity > 0 exists:
          calibrated = 0.80 * persisted_capacity + 0.20 * raw_calibrated
      Otherwise uses raw_calibrated directly for initial calibration.
    - Applies boundary clamping based on bucket family:
      * Gemini Weekly: [350.00, 1000.00]
      * Gemini 5-Hour: [100.00, 400.00]
      * 3P Weekly: [100.00, 500.00]
      * 3P 5-Hour: [35.00, 150.00]
    - Returns round(calibrated, 2).
    """
    bid = (bucket_id or "").strip().lower().replace("_", "-")
    is_5h = "5h" in bid
    min_cap, max_cap = get_capacity_bounds(bucket_id)

    persisted_capacity = 0.0
    try:
        cur = conn.execute(
            "SELECT calibrated_capacity FROM quota_state WHERE bucket_id = ? OR LOWER(bucket_id) = ? OR LOWER(REPLACE(bucket_id, '_', '-')) = ?",
            (bucket_id, (bucket_id or "").lower(), bid),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            persisted_capacity = float(row[0])
            if (
                math.isnan(persisted_capacity)
                or math.isinf(persisted_capacity)
                or persisted_capacity <= 0
            ):
                persisted_capacity = 0.0
    except Exception:
        persisted_capacity = 0.0

    raw_fallback = persisted_capacity if persisted_capacity > 0 else fallback_capacity
    try:
        fallback_float = float(raw_fallback) if raw_fallback is not None else 672.0
        if math.isnan(fallback_float) or math.isinf(fallback_float):
            fallback_float = 672.0
    except (ValueError, TypeError):
        fallback_float = 672.0

    fallback_val = round(max(min_cap, min(max_cap, fallback_float)), 2)

    reset_dt = _to_utc_datetime(reset_time)
    if reset_dt is None:
        return fallback_val

    if is_5h:
        start_dt = reset_dt - timedelta(hours=5)
    else:
        start_dt = reset_dt - timedelta(days=7)
    # Prefix format in UTC matches steps.created_at ISO format
    window_start_prefix = start_dt.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        rem_val = float(remaining_fraction) if remaining_fraction is not None else 1.0
        if math.isnan(rem_val) or math.isinf(rem_val):
            rem_val = 1.0
    except (ValueError, TypeError):
        rem_val = 1.0

    used_fraction = 1.0 - rem_val
    if used_fraction < 0.01:
        return fallback_val

    is_3p = "3p" in bid or "claude" in bid or "gpt" in bid
    total_billed_cost = 0.0

    try:
        cur = conn.execute(
            "SELECT model_name, billed_cost FROM steps WHERE created_at >= ?",
            (window_start_prefix,),
        )

        for r in cur.fetchall():
            m_name = r[0] or ""
            cost = float(r[1] or 0.0)
            pkey = get_pricing_key(m_name)
            if is_3p and pkey in ("claude", "gpt"):
                total_billed_cost += cost
            elif not is_3p and pkey not in ("claude", "gpt"):
                total_billed_cost += cost
    except Exception:
        return fallback_val

    if total_billed_cost < 0.50:
        return fallback_val

    raw_calibrated = total_billed_cost / used_fraction

    if persisted_capacity > 0:
        prior_clamped = max(min_cap, min(max_cap, persisted_capacity))
        calibrated = 0.80 * prior_clamped + 0.20 * raw_calibrated
    else:
        calibrated = raw_calibrated

    calibrated = max(min_cap, min(max_cap, calibrated))

    return round(calibrated, 2)


def sync_and_get_quota_state(
    conn: sqlite3.Connection | None = None,
    force_refresh: bool = False,
    ttl_seconds: int | None = None,
    ls_fetcher: Any = None,
) -> QuotaStateList:
    """
    Checks cached quota_state from db. If cached rows exist, are younger than TTL (60s),
    and not force_refresh, returns them.
    Otherwise attempts discovery and RPC fetch.
    If fetch succeeds, calibrates capacity and upserts into quota_state.
    If fetch fails, returns cached rows (even if expired) or synthesizes fallback rows.
    """
    if conn is None:
        from context_monitor.db import get_db_connection

        with get_db_connection() as managed_conn:
            return sync_and_get_quota_state(
                conn=managed_conn,
                force_refresh=force_refresh,
                ttl_seconds=ttl_seconds,
                ls_fetcher=ls_fetcher,
            )

    if ttl_seconds is None:
        ttl_seconds = get_quota_cache_ttl()

    cached_rows = get_quota_state(conn)

    cache_is_fresh = False
    if cached_rows and not force_refresh:
        now = datetime.now(timezone.utc)
        all_fresh = True
        for r in cached_rows:
            fetched_at_str = r.get("fetched_at") or r.get("updated_at")
            if not fetched_at_str:
                all_fresh = False
                break
            fetched_dt = _to_utc_datetime(fetched_at_str)
            if fetched_dt is None or (now - fetched_dt).total_seconds() > ttl_seconds:
                all_fresh = False
                break
        cache_is_fresh = all_fresh

    if cache_is_fresh:
        return QuotaStateList(cached_rows)

    raw_payload = None
    if ls_fetcher is not None:
        try:
            raw_payload = ls_fetcher()
        except Exception:
            raw_payload = None
    else:
        try:
            port, token = discover_language_server_credentials()
            if port and token:
                raw_payload = fetch_raw_quota_summary(port, token)
        except Exception:
            raw_payload = None

    if raw_payload and isinstance(raw_payload, dict):
        response_data = (
            raw_payload.get("response")
            if isinstance(raw_payload.get("response"), dict)
            else raw_payload
        )
        groups = response_data.get("groups", [])
        rows_to_upsert = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for grp in groups:
            group_name = grp.get("displayName") or grp.get("group_name", "")
            for b in grp.get("buckets", []):
                bucket_id = b.get("bucketId") or b.get("bucket_id")
                if not bucket_id:
                    continue
                window = b.get("window", "")
                raw_rem = (
                    b.get("remainingFraction")
                    if b.get("remainingFraction") is not None
                    else b.get("remaining_fraction", 1.0)
                )
                try:
                    rem_frac = float(raw_rem) if raw_rem is not None else 1.0
                except (ValueError, TypeError):
                    rem_frac = 1.0
                reset_time = b.get("resetTime") or b.get("reset_time") or ""
                fallback_cap = _get_default_capacity(bucket_id)

                calibrated = calibrate_capacity(
                    conn=conn,
                    bucket_id=bucket_id,
                    remaining_fraction=rem_frac,
                    reset_time=reset_time,
                    fallback_capacity=fallback_cap,
                )

                rows_to_upsert.append(
                    {
                        "bucket_id": bucket_id,
                        "group_name": group_name,
                        "window": window,
                        "remaining_fraction": rem_frac,
                        "reset_time": reset_time,
                        "calibrated_capacity": calibrated,
                        "fetched_at": now_iso,
                    }
                )

        if rows_to_upsert:
            upsert_quota_state(conn, rows_to_upsert)
            fresh_rows = get_quota_state(conn)
            return QuotaStateList(fresh_rows)

    if cached_rows:
        return QuotaStateList(cached_rows)

    # Synthesize fallback rows if no cache exists
    now_iso = datetime.now(timezone.utc).isoformat()
    fallback_rows = [
        {
            "bucket_id": "gemini-weekly",
            "group_name": "Gemini Models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "",
            "calibrated_capacity": get_gemini_weekly_capacity(),
            "fetched_at": now_iso,
        },
        {
            "bucket_id": "gemini-5h",
            "group_name": "Gemini Models",
            "window": "5h",
            "remaining_fraction": 1.0,
            "reset_time": "",
            "calibrated_capacity": get_gemini_5h_capacity(),
            "fetched_at": now_iso,
        },
        {
            "bucket_id": "3p-weekly",
            "group_name": "Claude and GPT models",
            "window": "weekly",
            "remaining_fraction": 1.0,
            "reset_time": "",
            "calibrated_capacity": get_3p_weekly_capacity(),
            "fetched_at": now_iso,
        },
        {
            "bucket_id": "3p-5h",
            "group_name": "Claude and GPT models",
            "window": "5h",
            "remaining_fraction": 1.0,
            "reset_time": "",
            "calibrated_capacity": get_3p_5h_capacity(),
            "fetched_at": now_iso,
        },
    ]
    try:
        upsert_quota_state(conn, fallback_rows)
        fresh_rows = get_quota_state(conn)
        if fresh_rows:
            return QuotaStateList(fresh_rows)
    except Exception:
        pass
    return QuotaStateList(fallback_rows)


def _classify_pacing_status(projected_pct: float) -> str:
    """Classifies projected end-of-window consumption into pacing status."""
    if projected_pct <= 80.0:
        return "on_track"
    elif projected_pct <= 100.0:
        return "moderate"
    return "exceeding"


def _integrate_profile_weight(
    weights: list[list[float]],
    start_dt: datetime | str | None,
    end_dt: datetime | str | None,
) -> float:
    """
    Integrates hourly weights over interval [start_dt, end_dt] with sub-hour precision.
    Boundary partial hours are weighted by elapsed overlap seconds (delta_t / 3600.0).
    """
    if (
        not weights
        or not isinstance(weights, (list, tuple))
        or len(weights) != 7
        or any(not isinstance(r, (list, tuple)) or len(r) != 24 for r in weights)
    ):
        return 0.0

    start = _to_utc_datetime(start_dt)
    end = _to_utc_datetime(end_dt)
    if start is None or end is None or start >= end:
        return 0.0

    cur = start
    total_weight = 0.0

    while cur < end:
        next_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment_end = min(next_hour, end)
        delta_sec = (segment_end - cur).total_seconds()
        frac_hour = delta_sec / 3600.0

        d = cur.weekday()
        h = cur.hour
        if 0 <= d < 7 and 0 <= h < 24:
            total_weight += frac_hour * float(weights[d][h])

        cur = segment_end

    return total_weight


def _estimate_hours_until_exhaustion_weighted(
    weights: list[list[float]],
    now_dt: datetime | str | None,
    needed_weight: float,
    max_hours: float = 336.0,
) -> float | None:
    """
    Steps forward from now_dt hour-by-hour accumulating hourly weights until
    needed_weight is reached, returning estimated wall-clock hours (or None if
    exhaustion exceeds max_hours).
    """
    if (
        not weights
        or not isinstance(weights, (list, tuple))
        or len(weights) != 7
        or any(not isinstance(r, (list, tuple)) or len(r) != 24 for r in weights)
    ):
        return None

    now = _to_utc_datetime(now_dt)
    if now is None:
        return None

    try:
        w_needed = float(needed_weight)
        if math.isnan(w_needed) or math.isinf(w_needed):
            return None
    except (ValueError, TypeError):
        return None

    if w_needed <= 0.0:
        return 0.0

    try:
        eff_max_hours = float(max_hours)
        if math.isnan(eff_max_hours) or eff_max_hours <= 0:
            return None
    except (ValueError, TypeError):
        return None

    cur = now
    accumulated_weight = 0.0
    max_seconds = eff_max_hours * 3600.0

    while True:
        elapsed_sec = (cur - now).total_seconds()
        if elapsed_sec >= max_seconds:
            return None

        next_hour = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        max_dt = now + timedelta(seconds=max_seconds)
        segment_end = min(next_hour, max_dt)
        delta_sec = (segment_end - cur).total_seconds()
        if delta_sec <= 0:
            return None

        frac_hour = delta_sec / 3600.0
        d = cur.weekday()
        h = cur.hour
        w_hour = float(weights[d][h]) if (0 <= d < 7 and 0 <= h < 24) else (1.0 / 168.0)
        seg_weight = frac_hour * w_hour

        if accumulated_weight + seg_weight >= w_needed:
            rem_weight = w_needed - accumulated_weight
            if w_hour > 0:
                frac_needed = max(0.0, min(frac_hour, rem_weight / w_hour))
                sec_needed = frac_needed * 3600.0
                total_sec = elapsed_sec + sec_needed
                return total_sec / 3600.0
            else:
                return elapsed_sec / 3600.0

        accumulated_weight += seg_weight
        cur = segment_end


def calculate_quota_pacing(
    remaining_fraction: float | None,
    reset_time_iso: str | None,
    window_hours: float = 168.0,
    calibrated_capacity: float = 672.0,
    now: datetime | None = None,
    activity_profile: dict[str, Any] | list[list[float]] | None = None,
) -> dict[str, Any]:
    """
    Calculates quota pacing, burn rate, and projected end-of-window consumption.

    Args:
        remaining_fraction: Fraction of quota remaining in current cycle (0.0 to 1.0).
        reset_time_iso: ISO-8601 UTC reset timestamp.
        window_hours: Cycle duration in hours (e.g., 168.0 for weekly, 5.0 for 5h).
        calibrated_capacity: Calibrated compute capacity in dollars for the window.
        now: Optional reference datetime (defaults to current UTC time).
        activity_profile: Optional 7x24 empirical activity matrix or profile dict.

    Returns:
        dict containing:
            used_pct: Percentage of quota consumed so far.
            projected_pct: Projected percentage consumption at window reset.
            projected_cost: Projected dollar cost consumed at window reset.
            burn_rate_pct_per_hour: Current burn rate in % per hour.
            elapsed_hours: Hours elapsed since current cycle began.
            remaining_hours: Hours remaining until cycle reset.
            status: 'calibrating' | 'on_track' | 'moderate' | 'exceeding'.
            hours_until_exhaustion: Estimated hours until 100% quota exhausted, or None.
            days_until_exhaustion: Estimated days until 100% quota exhausted, or None.
            is_calibrating: True if within early-cycle guard window.
            elapsed_activity_pct: Percentage of expected window activity elapsed.
            linear_projected_pct: Unweighted wall-clock projection for comparison.
            weighting_mode: 'activity_weighted' or 'linear'.
    """
    try:
        rem_val = float(remaining_fraction) if remaining_fraction is not None else 1.0
        if math.isnan(rem_val) or math.isinf(rem_val):
            rem_val = 1.0
    except (ValueError, TypeError):
        rem_val = 1.0
    rem_val = max(0.0, min(1.0, rem_val))
    used_fraction = 1.0 - rem_val
    used_pct = round(used_fraction * 100.0, 2)

    try:
        cap = float(calibrated_capacity) if calibrated_capacity is not None else 672.0
        if math.isnan(cap) or math.isinf(cap) or cap <= 0:
            cap = 672.0
    except (ValueError, TypeError):
        cap = 672.0

    try:
        eff_window = float(window_hours) if window_hours is not None else 168.0
    except (ValueError, TypeError):
        eff_window = 168.0
    if eff_window <= 0:
        eff_window = 168.0

    now_dt = _to_utc_datetime(now, default_now=True)

    has_valid_reset = False
    remaining_hours = eff_window
    reset_dt = _to_utc_datetime(reset_time_iso)
    if reset_dt is not None:
        diff_sec = (reset_dt - now_dt).total_seconds()
        remaining_hours = max(0.0, diff_sec / 3600.0)
        has_valid_reset = True

    if not has_valid_reset:
        elapsed_hours = 0.0
        elapsed_fraction = 0.0
    else:
        elapsed_hours = max(0.0, min(eff_window, eff_window - remaining_hours))
        elapsed_fraction = elapsed_hours / eff_window if eff_window > 0 else 0.0

    # Early-cycle safety guard (proportional to window length: min(2.5h, 10% of window)
    # or elapsed_fraction < 0.015, or missing reset time)
    min_elapsed_hours = min(2.5, eff_window * 0.1)
    is_calibrating = (
        (not has_valid_reset)
        or (elapsed_hours < min_elapsed_hours)
        or (elapsed_fraction < 0.015)
    )

    burn_rate_pct_per_hour = (
        round(used_pct / elapsed_hours, 4) if elapsed_hours > 0 else 0.0
    )

    # Linear baseline projection
    if is_calibrating or elapsed_fraction <= 0:
        linear_projected_pct = used_pct
    else:
        linear_projected_pct = round((used_fraction / elapsed_fraction) * 100.0, 2)

    # Activity profile weighting
    weights, _ = normalize_activity_profile(activity_profile)
    use_activity_weights = False
    f_activity = 0.0
    w_elapsed = 0.0

    if weights is not None and has_valid_reset and reset_dt is not None:
        start_dt = reset_dt - timedelta(hours=eff_window)
        clamped_now = max(start_dt, min(reset_dt, now_dt))
        w_elapsed = _integrate_profile_weight(weights, start_dt, clamped_now)
        w_remaining = _integrate_profile_weight(weights, clamped_now, reset_dt)
        w_window = w_elapsed + w_remaining
        if w_window > 0 and w_elapsed > 0:
            f_activity = min(1.0, max(0.0, w_elapsed / w_window))
            use_activity_weights = True

    if use_activity_weights and weights is not None and reset_dt is not None:
        weighting_mode = "activity_weighted"
        elapsed_activity_pct = round(f_activity * 100.0, 2)
        if is_calibrating:
            projected_pct = used_pct
            status = "calibrating"
            hours_until_exhaustion = None
            days_until_exhaustion = None
        else:
            projected_pct = round((used_fraction / f_activity) * 100.0, 2)
            status = _classify_pacing_status(projected_pct)

            if projected_pct > 100.0 and used_fraction > 0:
                w_needed = (1.0 - used_fraction) * (w_elapsed / used_fraction)
                est_hours = _estimate_hours_until_exhaustion_weighted(
                    weights, clamped_now, w_needed
                )
                if est_hours is not None:
                    hours_until_exhaustion = round(est_hours, 1)
                    days_until_exhaustion = round(hours_until_exhaustion / 24.0, 1)
                else:
                    hours_until_exhaustion = None
                    days_until_exhaustion = None
            else:
                hours_until_exhaustion = None
                days_until_exhaustion = None
    else:
        weighting_mode = "linear"
        elapsed_activity_pct = round(elapsed_fraction * 100.0, 2)
        if is_calibrating:
            projected_pct = used_pct
            status = "calibrating"
            hours_until_exhaustion = None
            days_until_exhaustion = None
        else:
            projected_pct = linear_projected_pct
            status = _classify_pacing_status(projected_pct)

            if projected_pct > 100.0 and burn_rate_pct_per_hour > 0:
                rem_quota_pct = max(0.0, 100.0 - used_pct)
                hours_until_exhaustion = round(
                    rem_quota_pct / burn_rate_pct_per_hour, 1
                )
                days_until_exhaustion = round(hours_until_exhaustion / 24.0, 1)
            else:
                hours_until_exhaustion = None
                days_until_exhaustion = None

    projected_cost = round((projected_pct / 100.0) * cap, 2)

    return {
        "used_pct": used_pct,
        "projected_pct": projected_pct,
        "projected_cost": projected_cost,
        "burn_rate_pct_per_hour": burn_rate_pct_per_hour,
        "elapsed_hours": round(elapsed_hours, 2),
        "remaining_hours": round(remaining_hours, 2),
        "status": status,
        "hours_until_exhaustion": hours_until_exhaustion,
        "days_until_exhaustion": days_until_exhaustion,
        "is_calibrating": is_calibrating,
        "elapsed_activity_pct": elapsed_activity_pct,
        "linear_projected_pct": linear_projected_pct,
        "weighting_mode": weighting_mode,
    }


def calculate_quota_metrics(
    turn_cost: float = 0.0,
    thread_cost: float = 0.0,
    quota_state: list[dict] | dict[str, dict] | None = None,
    model_name: str | None = None,
    *,
    billed_cost: float | None = None,
    session_cost: float | None = None,
    now: datetime | None = None,
    activity_profile: dict[str, Any] | list[list[float]] | None = None,
    **kwargs,
) -> dict[str, Any]:
    """
    Calculates percentage of weekly and 5-hour quota consumed by a turn and thread.

    Supports multiple calling conventions:
    1. calculate_quota_metrics(turn_cost: float, thread_cost: float, quota_state: list[dict])
    2. calculate_quota_metrics(billed_cost: float, model_name: str, quota_state: dict[str, dict])
    3. Keyword arguments: billed_cost=..., model_name=..., quota_state=...

    Returns:
        dict containing per-bucket metrics ('gemini-weekly', 'gemini-5h', '3p-weekly', '3p-5h')
        as well as top-level convenience keys ('weekly_pct', 'five_hour_pct', 'weekly_remaining_pct', etc.)
    """
    eff_now = now if now is not None else kwargs.get("now")
    eff_activity_profile = (
        activity_profile
        if activity_profile is not None
        else kwargs.get("activity_profile")
    )

    if "thread_cost_or_model" in kwargs:
        tcom = kwargs["thread_cost_or_model"]
        if isinstance(tcom, str):
            model_name = tcom
        else:
            thread_cost = float(tcom)

    if billed_cost is not None:
        turn_cost = billed_cost

    eff_model = model_name or "Gemini 3.8 Flash (High)"
    if isinstance(thread_cost, str):
        eff_model = thread_cost
        billed_val = float(turn_cost)
        eff_thread_cost = session_cost if session_cost is not None else billed_val
    else:
        billed_val = float(turn_cost)
        if session_cost is not None:
            eff_thread_cost = float(session_cost)
        elif thread_cost != 0.0:
            eff_thread_cost = float(thread_cost)
        elif billed_cost is not None:
            eff_thread_cost = billed_val
        else:
            eff_thread_cost = float(thread_cost)

    if isinstance(quota_state, str):
        eff_model = quota_state
        quota_state = None

    if "model_name" in kwargs and kwargs["model_name"]:
        eff_model = kwargs["model_name"]

    state_by_bucket: dict[str, dict] = {}
    if quota_state is not None:
        if isinstance(quota_state, dict):
            state_by_bucket = quota_state
        elif isinstance(quota_state, list):
            for item in quota_state:
                if isinstance(item, dict):
                    bid = item.get("bucket_id") or item.get("bucketId")
                    if bid:
                        state_by_bucket[bid] = item

    standard_buckets = ["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"]
    bucket_metrics = {}

    for bid in standard_buckets:
        b = state_by_bucket.get(bid, {})
        cap_val = (
            b.get("calibrated_capacity")
            if b.get("calibrated_capacity") is not None
            else b.get("calibratedCapacity")
        )
        cap = float(cap_val or _get_default_capacity(bid))
        rem_frac_val = (
            b.get("remaining_fraction")
            if b.get("remaining_fraction") is not None
            else b.get("remainingFraction", 1.0)
        )
        rem_frac = float(rem_frac_val if rem_frac_val is not None else 1.0)
        rem_pct = round(rem_frac * 100.0, 2)
        r_time = str(b.get("reset_time") or b.get("resetTime") or "")

        t_pct = round((billed_val / cap) * 100.0, 4) if cap > 0 else 0.0
        th_pct = round((eff_thread_cost / cap) * 100.0, 4) if cap > 0 else 0.0

        is_5h = "5h" in bid
        w_hours = 5.0 if is_5h else 168.0
        pacing = calculate_quota_pacing(
            remaining_fraction=rem_frac,
            reset_time_iso=r_time,
            window_hours=w_hours,
            calibrated_capacity=cap,
            now=eff_now,
            activity_profile=eff_activity_profile,
        )

        bucket_metrics[bid] = {
            "turn_pct": t_pct,
            "thread_pct": th_pct,
            "quota_remaining_pct": rem_pct,
            "reset_time": r_time,
            "calibrated_capacity": cap,
            "reset_countdown": format_reset_countdown(r_time, now=eff_now),
            "pacing": pacing,
        }

    pkey = get_pricing_key(eff_model)
    is_3p = pkey in ("claude", "gpt")
    prefix = "3p" if is_3p else "gemini"

    weekly_key = f"{prefix}-weekly"
    five_h_key = f"{prefix}-5h"

    weekly_m = bucket_metrics[weekly_key]
    five_h_m = bucket_metrics[five_h_key]
    weekly_pacing = weekly_m["pacing"]
    five_h_pacing = five_h_m["pacing"]

    result: dict[str, Any] = {
        # Bucket-keyed dictionaries (gemini-weekly, gemini-5h, 3p-weekly, 3p-5h)
        "gemini-weekly": bucket_metrics["gemini-weekly"],
        "gemini-5h": bucket_metrics["gemini-5h"],
        "3p-weekly": bucket_metrics["3p-weekly"],
        "3p-5h": bucket_metrics["3p-5h"],
        # Model-routed top-level convenience metrics
        "model_name": eff_model,
        "pricing_key": pkey,
        "bucket_prefix": prefix,
        "turn_pct": weekly_m["turn_pct"],
        "thread_pct": weekly_m["thread_pct"],
        "weekly_pct": weekly_m["turn_pct"],
        "five_hour_pct": five_h_m["turn_pct"],
        "thread_weekly_pct": weekly_m["thread_pct"],
        "thread_5h_pct": five_h_m["thread_pct"],
        "session_weekly_pct": weekly_m["thread_pct"],
        "session_5h_pct": five_h_m["thread_pct"],
        "weekly_remaining_pct": weekly_m["quota_remaining_pct"],
        "five_hour_remaining_pct": five_h_m["quota_remaining_pct"],
        "weekly_reset_time": weekly_m["reset_time"],
        "five_hour_reset_time": five_h_m["reset_time"],
        "weekly_reset_countdown": weekly_m["reset_countdown"],
        "five_hour_reset_countdown": five_h_m["reset_countdown"],
        "calibrated_weekly_capacity": weekly_m["calibrated_capacity"],
        "calibrated_5h_capacity": five_h_m["calibrated_capacity"],
        # Pacing and projection metrics
        "weekly_pacing": weekly_pacing,
        "five_hour_pacing": five_h_pacing,
        "projected_weekly_pct": weekly_pacing["projected_pct"],
        "projected_weekly_cost": weekly_pacing["projected_cost"],
        "pacing_status": weekly_pacing["status"],
        "projected_5h_pct": five_h_pacing["projected_pct"],
    }
    return result


def format_reset_countdown(
    reset_time_iso: str | datetime | None, now: datetime | str | None = None
) -> str:
    """
    Formats ISO-8601 UTC reset timestamp into human-readable countdown:
    e.g., '6d 8h', '4h 19m', '45m', or '0m'.
    """
    reset_dt = _to_utc_datetime(reset_time_iso)
    if reset_dt is None:
        return ""
    try:
        now_dt = _to_utc_datetime(now, default_now=True)

        diff_seconds = int((reset_dt - now_dt).total_seconds())
        if diff_seconds <= 0:
            return "0m"

        days = diff_seconds // 86400
        hours = (diff_seconds % 86400) // 3600
        minutes = (diff_seconds % 3600) // 60

        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{max(1, minutes)}m"
    except Exception:
        return ""


def format_quota_pct(pct: float | None, precision: int = 2) -> str:
    """Formats percentage float as string: e.g. 0.31 -> '0.31%'."""
    try:
        val = float(pct if pct is not None else 0.0)
    except (ValueError, TypeError):
        val = 0.0
    return f"{val:.{precision}f}%"


class _ProfileCacheEntry:
    __slots__ = (
        "timestamp",
        "profile",
        "st_ino",
        "st_mtime_ns",
        "st_size",
        "wal_mtime_ns",
        "wal_size",
        "data_version",
        "total_changes",
    )

    def __init__(
        self,
        timestamp: float,
        profile: dict[str, Any],
        st_ino: int | None = None,
        st_mtime_ns: int | None = None,
        st_size: int | None = None,
        wal_mtime_ns: int | None = None,
        wal_size: int | None = None,
        data_version: int | None = None,
        total_changes: int = 0,
    ) -> None:
        self.timestamp = timestamp
        self.profile = profile
        self.st_ino = st_ino
        self.st_mtime_ns = st_mtime_ns
        self.st_size = st_size
        self.wal_mtime_ns = wal_mtime_ns
        self.wal_size = wal_size
        self.data_version = data_version
        self.total_changes = total_changes


_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE: dict[str, _ProfileCacheEntry] = {}
PROFILE_CACHE_TTL: float = 300.0
_PROFILE_CACHE_MAX_ENTRIES: int = 256


def _extract_db_path(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("PRAGMA database_list;").fetchall()
        for row in rows:
            name = (
                row["name"] if hasattr(row, "keys") and "name" in row.keys() else row[1]
            )
            if name == "main":
                f = (
                    row["file"]
                    if hasattr(row, "keys") and "file" in row.keys()
                    else row[2]
                )
                if f and str(f).strip():
                    return str(Path(f).resolve())
    except Exception:
        pass
    return None


def _copy_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "matrix": [row[:] for row in profile["matrix"]],
        "total_steps": int(profile["total_steps"]),
        "timezone": str(profile.get("timezone", "UTC")),
    }


def invalidate_activity_profile_cache(
    db_path: str | Path | sqlite3.Connection | None = None,
) -> None:
    """
    Invalidates the in-memory hourly activity profile cache.
    If db_path is provided (a database path or connection), invalidates entries
    for that specific database. Otherwise, invalidates the entire cache.
    """
    with _PROFILE_CACHE_LOCK:
        if db_path is not None:
            if isinstance(db_path, sqlite3.Connection):
                extracted = _extract_db_path(db_path)
                cache_key = extracted if extracted else f"conn_{id(db_path)}"
                _PROFILE_CACHE.pop(cache_key, None)
            else:
                resolved = str(Path(db_path).resolve())
                _PROFILE_CACHE.pop(resolved, None)
        else:
            _PROFILE_CACHE.clear()


def get_hourly_activity_profile(
    conn: sqlite3.Connection | None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Extracts empirical 7x24 (168-hour) UTC activity profile from steps table.
    Rows 0..6 correspond to Mon..Sun; columns 0..23 correspond to UTC hours.

    Caches results in memory for up to 300 seconds per database, automatically
    invalidating when SQLite data_version changes, connection total_changes changes,
    file mtime/size/inode changes, force_refresh=True is passed, or
    invalidate_activity_profile_cache() is invoked.

    Returns:
        {"matrix": list[list[int]], "total_steps": int, "timezone": "UTC"}
    """
    empty_profile: dict[str, Any] = {
        "matrix": [[0] * 24 for _ in range(7)],
        "total_steps": 0,
        "timezone": "UTC",
    }
    if conn is None:
        return empty_profile

    # If connection is in an active uncommitted transaction, do not use or pollute
    # the shared committed cache: query conn directly and return.
    in_tx = getattr(conn, "in_transaction", False)

    db_path = _extract_db_path(conn)
    cache_key = db_path if db_path else f"conn_{id(conn)}"

    now = time.monotonic()

    def _get_db_wal_stat_and_dv(
        target_conn: sqlite3.Connection, target_db_path: str | None
    ) -> tuple[
        int | None, int | None, int | None, int | None, int | None, int | None, int
    ]:
        c_ino = c_mtime_ns = c_size = c_wal_mtime_ns = c_wal_size = None
        if target_db_path:
            try:
                st = Path(target_db_path).stat()
                c_ino = st.st_ino
                c_mtime_ns = st.st_mtime_ns
                c_size = st.st_size
            except OSError:
                pass

            try:
                st_wal = Path(str(target_db_path) + "-wal").stat()
                c_wal_mtime_ns = st_wal.st_mtime_ns
                c_wal_size = st_wal.st_size
            except OSError:
                pass

        try:
            c_dv = target_conn.execute("PRAGMA data_version;").fetchone()[0]
        except Exception:
            c_dv = None

        try:
            c_tc = getattr(target_conn, "total_changes", 0)
        except Exception:
            c_tc = 0

        return c_ino, c_mtime_ns, c_size, c_wal_mtime_ns, c_wal_size, c_dv, c_tc

    (
        cur_ino,
        cur_mtime_ns,
        cur_size,
        cur_wal_mtime_ns,
        cur_wal_size,
        cur_dv,
        cur_tc,
    ) = _get_db_wal_stat_and_dv(conn, db_path)

    if not force_refresh and not in_tx:
        with _PROFILE_CACHE_LOCK:
            entry = _PROFILE_CACHE.get(cache_key)
            if entry is not None and (now - entry.timestamp) < PROFILE_CACHE_TTL:
                is_valid = False
                if db_path is not None:
                    file_unchanged = (
                        cur_ino == entry.st_ino
                        and cur_mtime_ns == entry.st_mtime_ns
                        and cur_size == entry.st_size
                        and cur_wal_mtime_ns == entry.wal_mtime_ns
                        and cur_wal_size == entry.wal_size
                    )
                    dv_unchanged = (
                        cur_dv is not None
                        and entry.data_version is not None
                        and cur_dv == entry.data_version
                    ) or (cur_dv is None and entry.data_version is None)
                    tc_unchanged = cur_tc == entry.total_changes
                    if file_unchanged and dv_unchanged and tc_unchanged:
                        is_valid = True
                else:
                    if cur_tc == entry.total_changes:
                        is_valid = True

                if is_valid:
                    return _copy_profile(entry.profile)

    matrix: list[list[int]] = [[0] * 24 for _ in range(7)]
    total_steps = 0
    query_success = False

    try:
        cursor = conn.execute(
            "SELECT substr(created_at, 1, 13) AS hour_key, COUNT(*) AS step_count "
            "FROM steps WHERE created_at IS NOT NULL AND created_at != '' "
            "GROUP BY hour_key"
        )
        for row in cursor.fetchall():
            hour_key = row[0]
            step_count = int(row[1] or 0)
            if not hour_key or step_count <= 0:
                continue

            clean_key = str(hour_key).strip().replace(" ", "T").replace("t", "T")
            if len(clean_key) == 13:
                dt_str = clean_key + ":00:00Z"
            elif len(clean_key) == 10:
                dt_str = clean_key + "T00:00:00Z"
            else:
                dt_str = clean_key

            dt = _to_utc_datetime(dt_str)
            if dt is not None:
                d = dt.weekday()
                h = dt.hour
                if 0 <= d < 7 and 0 <= h < 24:
                    matrix[d][h] += step_count
                    total_steps += step_count
        query_success = True
    except Exception:
        pass

    fresh_profile = {
        "matrix": matrix,
        "total_steps": total_steps,
        "timezone": "UTC",
    }

    if query_success and not in_tx:
        (
            cur_ino,
            cur_mtime_ns,
            cur_size,
            cur_wal_mtime_ns,
            cur_wal_size,
            cur_dv,
            cur_tc,
        ) = _get_db_wal_stat_and_dv(conn, db_path)

        new_entry = _ProfileCacheEntry(
            timestamp=time.monotonic(),
            profile=fresh_profile,
            st_ino=cur_ino,
            st_mtime_ns=cur_mtime_ns,
            st_size=cur_size,
            wal_mtime_ns=cur_wal_mtime_ns,
            wal_size=cur_wal_size,
            data_version=cur_dv,
            total_changes=cur_tc,
        )

        with _PROFILE_CACHE_LOCK:
            if len(_PROFILE_CACHE) >= _PROFILE_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    _PROFILE_CACHE, key=lambda k: _PROFILE_CACHE[k].timestamp
                )
                _PROFILE_CACHE.pop(oldest_key, None)
            _PROFILE_CACHE[cache_key] = new_entry

    return _copy_profile(fresh_profile)


def normalize_activity_profile(
    activity_profile: dict[str, Any] | list[list[float]] | None,
) -> tuple[list[list[float]] | None, int]:
    """
    Validates and normalizes a 7x24 activity profile with Laplace uniform-prior smoothing
    and cold-start confidence scaling.

    Args:
        activity_profile: Dict with 'matrix' (7x24) and optional 'total_steps',
                         or raw 7x24 list of lists, or None.

    Returns:
        (weights_7x24, total_steps) where weights sum to 1.0, or (None, total_steps)
        if total_steps < MIN_PROFILE_STEPS, or (None, 0) if invalid.
    """
    if activity_profile is None:
        return None, 0

    raw_matrix: Any = None
    total_steps_val: Any = None

    if isinstance(activity_profile, dict):
        raw_matrix = activity_profile.get("matrix")
        total_steps_val = activity_profile.get("total_steps")
    elif isinstance(activity_profile, list):
        raw_matrix = activity_profile
    else:
        return None, 0

    if not isinstance(raw_matrix, (list, tuple)) or len(raw_matrix) != 7:
        return None, 0

    for row in raw_matrix:
        if not isinstance(row, (list, tuple)) or len(row) != 24:
            return None, 0
        for cell in row:
            if (
                isinstance(cell, bool)
                or not isinstance(cell, (int, float))
                or math.isnan(cell)
                or math.isinf(cell)
                or cell < 0
            ):
                return None, 0

    matrix_sum = sum(sum(float(cell) for cell in row) for row in raw_matrix)

    if total_steps_val is not None:
        if isinstance(total_steps_val, bool):
            return None, 0
        try:
            t_val = float(total_steps_val)
            if math.isnan(t_val) or math.isinf(t_val) or t_val < 0:
                return None, 0
            total_steps = int(round(t_val))
        except (ValueError, TypeError):
            return None, 0
    else:
        total_steps = int(round(matrix_sum))

    if matrix_sum <= 0:
        return None, 0

    # If the input is already a normalized 7x24 weight matrix (sum approx 1.0 and all cells >= 0)
    # Exclude single-point matrices (e.g. 1.0 in a single cell and 0 elsewhere) which represent a 1-step raw count
    is_single_point_matrix = any(
        float(cell) >= 0.99 for row in raw_matrix for cell in row
    )
    if (
        abs(matrix_sum - 1.0) < 1e-4
        and all(float(cell) >= 0 for row in raw_matrix for cell in row)
        and not is_single_point_matrix
    ):
        if total_steps_val is not None and total_steps < MIN_PROFILE_STEPS:
            return None, total_steps
        norm_weights = [[float(cell) for cell in row] for row in raw_matrix]
        effective_t = (
            total_steps if total_steps >= MIN_PROFILE_STEPS else MIN_PROFILE_STEPS
        )
        return norm_weights, effective_t

    # Effective step count reflects empirical evidence in the matrix
    effective_steps = min(total_steps, int(round(matrix_sum)))
    if effective_steps < MIN_PROFILE_STEPS:
        return None, effective_steps

    # Confidence ramp: confidence_scale = min(1.0, effective_steps / 200.0)
    confidence_scale = min(1.0, effective_steps / 200.0)
    # Effective empirical weight: empirical_weight = confidence_scale * (1.0 - LAPLACE_ALPHA)
    empirical_weight = confidence_scale * (1.0 - LAPLACE_ALPHA)

    uniform_hour_weight = 1.0 / 168.0
    weights: list[list[float]] = []
    for d in range(7):
        row_weights: list[float] = []
        for h in range(24):
            empirical_share = float(raw_matrix[d][h]) / matrix_sum
            w = (
                empirical_weight * empirical_share
                + (1.0 - empirical_weight) * uniform_hour_weight
            )
            row_weights.append(w)
        weights.append(row_weights)

    return weights, effective_steps
