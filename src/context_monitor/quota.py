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

DEFAULT_LOG_PATH = Path(os.path.expanduser("~/Library/Logs/Antigravity/language_server.log"))


class QuotaStateList(list):
    """List of quota state dictionaries supporting bucket_id key indexing."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and item.get("bucket_id") == key:
                    return item
            raise KeyError(f"Bucket '{key}' not found in quota state")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(key, str):
            for item in self:
                if isinstance(item, dict) and item.get("bucket_id") == key:
                    return item
            return default
        return default

    def to_dict(self) -> dict[str, dict]:
        """Converts list of bucket records into a dict keyed by bucket_id."""
        return {
            item["bucket_id"]: item
            for item in self
            if isinstance(item, dict) and "bucket_id" in item
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
        return get_gemini_weekly_capacity() if "gemini" in bid else get_3p_weekly_capacity()
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
    port = _discover_http_port(log_path or DEFAULT_LOG_PATH)
    return port, token


discover_language_server = discover_language_server_credentials


def _discover_csrf_token() -> str | None:
    """Discovers CSRF token from running language_server process args."""
    env_token = os.environ.get("AGY_CSRF_TOKEN")
    if env_token:
        return env_token.strip()

    try:
        proc = subprocess.run(
            ["ps", "-ww", "-eo", "pid,command"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if "language_server" not in line or "grep" in line:
                    continue
                m = re.search(r"--csrf_token(?:=|\s+)([^\s]+)", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass

    return None


def _discover_http_port(log_file: Path | str) -> int | None:
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

    path = Path(log_file)
    if not path.exists() or not path.is_file():
        return None

    try:
        file_size = path.stat().st_size
        chunk_size = 65536  # 64 KB chunks
        max_bytes_to_scan = 2 * 1024 * 1024  # Scan up to 2MB backward

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
    bid = (bucket_id or "").lower()
    is_5h = "5h" in bid

    window_start_prefix = ""
    if reset_time:
        try:
            reset_dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
            if is_5h:
                start_dt = reset_dt - timedelta(hours=5)
            else:
                start_dt = reset_dt - timedelta(days=7)
            # Prefix format matches both Z and +00:00 UTC formats
            window_start_prefix = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            window_start_prefix = ""

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

    used_fraction = 1.0 - float(remaining_fraction)
    if used_fraction < 0.01:
        return fallback_val

    is_3p = "3p" in bid
    total_billed_cost = 0.0

    try:
        if window_start_prefix:
            cur = conn.execute(
                "SELECT model_name, billed_cost FROM steps WHERE created_at >= ?",
                (window_start_prefix,),
            )
        else:
            cur = conn.execute("SELECT model_name, billed_cost FROM steps")

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
    conn: sqlite3.Connection,
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
                fetched_dt = datetime.fromisoformat(str(fetched_at_str).replace("Z", "+00:00"))
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
        groups = raw_payload.get("response", {}).get("groups", [])
        rows_to_upsert = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for grp in groups:
            group_name = grp.get("displayName") or grp.get("group_name", "")
            for b in grp.get("buckets", []):
                bucket_id = b.get("bucketId") or b.get("bucket_id")
                if not bucket_id:
                    continue
                window = b.get("window", "")
                rem_frac = float(b.get("remainingFraction", b.get("remaining_fraction", 1.0)))
                reset_time = b.get("resetTime") or b.get("reset_time") or ""
                fallback_cap = _get_default_capacity(bucket_id)

                calibrated = calibrate_capacity(
                    conn=conn,
                    bucket_id=bucket_id,
                    remaining_fraction=rem_frac,
                    reset_time=reset_time,
                    fallback_capacity=fallback_cap,
                )

                rows_to_upsert.append({
                    "bucket_id": bucket_id,
                    "group_name": group_name,
                    "window": window,
                    "remaining_fraction": rem_frac,
                    "reset_time": reset_time,
                    "calibrated_capacity": calibrated,
                    "fetched_at": now_iso,
                })

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
    upsert_quota_state(conn, fallback_rows)
    fresh_rows = get_quota_state(conn)
    return QuotaStateList(fresh_rows)


def calculate_quota_metrics(
    turn_cost: float = 0.0,
    thread_cost_or_model: float | str = 0.0,
    quota_state: list[dict] | dict[str, dict] | None = None,
    *,
    thread_cost: float | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """
    Calculates percentage of weekly and 5-hour quota consumed by a turn and thread.

    Supports two calling conventions:
    1. calculate_quota_metrics(turn_cost: float, thread_cost: float, quota_state: list[dict])
    2. calculate_quota_metrics(billed_cost: float, model_name: str, quota_state: dict[str, dict])

    Returns:
        dict containing per-bucket metrics ('gemini-weekly', 'gemini-5h', '3p-weekly', '3p-5h')
        as well as top-level convenience keys ('weekly_pct', 'five_hour_pct', 'weekly_remaining_pct', etc.)
    """
    if isinstance(thread_cost_or_model, str):
        billed_val = float(turn_cost)
        eff_model = thread_cost_or_model
        eff_thread_cost = thread_cost if thread_cost is not None else billed_val
    else:
        billed_val = float(turn_cost)
        eff_thread_cost = float(thread_cost_or_model) if thread_cost is None else float(thread_cost)
        eff_model = model_name or "Gemini 3.8 Flash (High)"

    state_by_bucket: dict[str, dict] = {}
    if quota_state is not None:
        if isinstance(quota_state, dict):
            state_by_bucket = quota_state
        elif isinstance(quota_state, list):
            for item in quota_state:
                if isinstance(item, dict) and "bucket_id" in item:
                    state_by_bucket[item["bucket_id"]] = item

    standard_buckets = ["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"]
    bucket_metrics = {}

    for bid in standard_buckets:
        b = state_by_bucket.get(bid, {})
        cap = float(b.get("calibrated_capacity") or _get_default_capacity(bid))
        rem_frac = float(b.get("remaining_fraction", 1.0))
        rem_pct = round(rem_frac * 100.0, 2)
        r_time = str(b.get("reset_time") or "")

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
    except Exception:
        return ""

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    diff_seconds = int((reset_dt - now).total_seconds())
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


def format_quota_pct(pct: float, precision: int = 2) -> str:
    """Formats percentage float as string: e.g. 0.31 -> '0.31%'."""
    return f"{pct:.{precision}f}%"
