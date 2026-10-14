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
import os
import re
import sqlite3
import subprocess
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
    "format_reset_countdown",
    "format_quota_pct",
    "QuotaStateList",
]

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


def calibrate_capacity(
    conn: sqlite3.Connection,
    bucket_id: str,
    remaining_fraction: float,
    reset_time: str,
    fallback_capacity: float,
) -> float:
    """
    Calibrates window compute capacity against steps recorded in SQLite:
        calibrated = sum(billed_cost) / (1 - remaining_fraction)

    Boundary safeguards:
    - If used_fraction < 0.01 or sum(billed_cost) < 0.50:
      returns existing persisted calibrated_capacity if > 0 else fallback_capacity.
    - Otherwise returns round(calibrated, 2).
    """
    bid = (bucket_id or "").strip().lower()
    is_5h = "5h" in bid

    persisted_capacity = 0.0
    try:
        cur = conn.execute(
            "SELECT calibrated_capacity FROM quota_state WHERE bucket_id = ?",
            (bucket_id,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            persisted_capacity = float(row[0])
    except Exception:
        persisted_capacity = 0.0

    fallback_val = persisted_capacity if persisted_capacity > 0 else fallback_capacity

    if not reset_time:
        return fallback_val

    try:
        reset_dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        else:
            reset_dt = reset_dt.astimezone(timezone.utc)

        if is_5h:
            start_dt = reset_dt - timedelta(hours=5)
        else:
            start_dt = reset_dt - timedelta(days=7)
        # Prefix format in UTC matches steps.created_at ISO format
        window_start_prefix = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return fallback_val

    try:
        rem_val = float(remaining_fraction) if remaining_fraction is not None else 1.0
    except (ValueError, TypeError):
        rem_val = 1.0

    used_fraction = 1.0 - rem_val
    if used_fraction < 0.01:
        return fallback_val

    is_3p = "3p" in bid
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

    calibrated = total_billed_cost / used_fraction
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
            try:
                fetched_dt = datetime.fromisoformat(
                    str(fetched_at_str).replace("Z", "+00:00")
                )
                if fetched_dt.tzinfo is None:
                    fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
                else:
                    fetched_dt = fetched_dt.astimezone(timezone.utc)
                if (now - fetched_dt).total_seconds() > ttl_seconds:
                    all_fresh = False
                    break
            except Exception:
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


def calculate_quota_metrics(
    turn_cost: float = 0.0,
    thread_cost_or_model: float | str = 0.0,
    quota_state: list[dict] | dict[str, dict] | None = None,
    *,
    thread_cost: float | None = None,
    model_name: str | None = None,
    billed_cost: float | None = None,
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
    if billed_cost is not None:
        turn_cost = billed_cost

    # If quota_state was passed as model_name string (e.g. calculate_quota_metrics(turn, thread, "Gemini Pro"))
    if isinstance(quota_state, str):
        eff_model = quota_state
        quota_state = None
    else:
        eff_model = model_name or "Gemini 3.8 Flash (High)"

    if isinstance(thread_cost_or_model, str):
        billed_val = float(turn_cost)
        eff_model = thread_cost_or_model
        eff_thread_cost = thread_cost if thread_cost is not None else billed_val
    else:
        billed_val = float(turn_cost)
        if thread_cost is not None:
            eff_thread_cost = float(thread_cost)
        else:
            eff_thread_cost = float(thread_cost_or_model)

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

        bucket_metrics[bid] = {
            "turn_pct": t_pct,
            "thread_pct": th_pct,
            "quota_remaining_pct": rem_pct,
            "reset_time": r_time,
            "calibrated_capacity": cap,
            "reset_countdown": format_reset_countdown(r_time),
        }

    pkey = get_pricing_key(eff_model)
    is_3p = pkey in ("claude", "gpt")
    prefix = "3p" if is_3p else "gemini"

    weekly_key = f"{prefix}-weekly"
    five_h_key = f"{prefix}-5h"

    weekly_m = bucket_metrics[weekly_key]
    five_h_m = bucket_metrics[five_h_key]

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
    }
    return result


def format_reset_countdown(reset_time_iso: str, now: datetime | None = None) -> str:
    """
    Formats ISO-8601 UTC reset timestamp into human-readable countdown:
    e.g., '6d 8h', '4h 19m', '45m', or '0m'.
    """
    if not reset_time_iso:
        return ""
    try:
        reset_dt = datetime.fromisoformat(reset_time_iso.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        else:
            reset_dt = reset_dt.astimezone(timezone.utc)

        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=timezone.utc)
        else:
            now_dt = now.astimezone(timezone.utc)

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
