"""
Session Timing & Force Multiplier Calculations
----------------------------------------------
Computes pure human inter-turn pairing time, step-level agent execution time
(excluding long async wait gaps), and developer Force Multiplier metrics.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

AFK_THRESHOLD_SECONDS: float = 900.0
MAX_AGENT_STEP_DELTA_SECONDS: float = 1800.0
MAX_ASYNC_WAKEUP_DELTA_SECONDS: float = 60.0

IGNORED_STEP_TYPES = frozenset(
    {
        "EPHEMERAL_MESSAGE",
        "CHECKPOINT",
        "CONVERSATION_HISTORY",
    }
)


def _safe_int(val: Any) -> int:
    """Safely convert any value to a non-negative int."""
    if val is None:
        return 0
    try:
        i = int(float(val))
        return max(0, i)
    except (ValueError, TypeError, OverflowError):
        return 0


def _safe_float(val: Any) -> float:
    """Safely convert any value to a non-negative finite float."""
    if val is None:
        return 0.0
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return max(0.0, f)
    except (ValueError, TypeError, OverflowError):
        return 0.0


def estimate_prompt_pairing_seconds(char_count: Any) -> float:
    """Estimate human prompt reading/thinking/writing time from character count.

    Uses a 15-second base cognitive overhead plus 1 second per 5 characters
    (~60 WPM effective composition rate), clamped to [15s, 900s].
    """
    safe_chars = _safe_int(char_count)
    return min(AFK_THRESHOLD_SECONDS, max(15.0, 15.0 + (safe_chars / 5.0)))


def _estimate_planner_generation_seconds(step: dict[str, Any]) -> float:
    """Estimate minimal LLM execution time from output and thinking tokens.

    Returns 0.0 if out_tok <= 0, otherwise min(60.0, max(1.0, out_tok / 80.0)).
    """
    out_tok = _safe_int(step.get("est_tokens")) + _safe_int(step.get("thinking_tokens"))
    if out_tok > 0:
        return min(60.0, max(1.0, out_tok / 80.0))
    return 0.0


def _parse_iso_timestamp(ts: Any) -> datetime | None:
    """Parse an ISO 8601 string, Unix epoch number, or datetime into a UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)

    # Check numeric epoch (int or float)
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    str_ts = str(ts).strip()
    if not str_ts:
        return None

    # Try numeric string epoch (e.g. "1761000000" or "1761000000.5")
    try:
        epoch = float(str_ts)
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass

    try:
        clean_ts = str_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_ts)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def compute_session_timings(
    steps: list[dict[str, Any]],
    is_subagent: bool = False,
) -> tuple[float, float, float]:
    """Compute (duration_seconds, active_agent_seconds, active_pairing_seconds).

    - duration_seconds: Wall-clock span from first to last valid step timestamp.
    - active_pairing_seconds:
        * Always 0.0 when is_subagent is True.
        * For root conversations, measures the human gap between the end of an
          agent turn (prev_dt) and the next USER_INPUT step when 0 < gap <= 900s.
        * For the first USER_INPUT, consecutive USER_INPUTs, or gaps > 900s (AFK)
          or <= 0s, credits estimate_prompt_pairing_seconds(characters).
    - active_agent_seconds:
        * Ignores step types in {"EPHEMERAL_MESSAGE", "CHECKPOINT", "CONVERSATION_HISTORY"}.
        * Sums step-to-step deltas for non-USER_INPUT steps.
        * Caps SYSTEM_MESSAGE transitions (subagent/daemon wakeups) at 60s so
          idle waiting for background work is not counted as active execution.
        * Caps normal agent/tool transitions at 1800s.
        * Credits fallback token-based generation time min(60.0, max(1.0, (est_tokens + thinking_tokens) / 80.0))
          when a PLANNER_RESPONSE step has a 0s timestamp delta or no prior timestamp.
    """
    if not steps:
        return (0.0, 0.0, 0.0)

    start_dt: datetime | None = None
    end_dt: datetime | None = None
    prev_dt: datetime | None = None
    prev_step_type: str | None = None

    active_agent_seconds: float = 0.0
    active_pairing_seconds: float = 0.0

    for step in steps:
        if not isinstance(step, dict):
            continue

        s_type = str(step.get("type") or step.get("step_type") or "UNKNOWN").strip()
        if s_type in IGNORED_STEP_TYPES:
            continue

        raw_ts = step.get("timestamp") or step.get("created_at")
        dt = _parse_iso_timestamp(raw_ts)
        if dt is None:
            continue

        if start_dt is None:
            start_dt = dt
        end_dt = dt

        if s_type == "USER_INPUT":
            if not is_subagent:
                chars = _safe_int(step.get("characters") or step.get("char_count"))
                est_prompt_sec = estimate_prompt_pairing_seconds(chars)
                if prev_dt is None or prev_step_type == "USER_INPUT":
                    active_pairing_seconds += est_prompt_sec
                else:
                    gap = (dt - prev_dt).total_seconds()
                    if 0.0 < gap <= AFK_THRESHOLD_SECONDS:
                        active_pairing_seconds += gap
                    else:
                        active_pairing_seconds += est_prompt_sec
        else:
            if prev_dt is not None:
                delta = (dt - prev_dt).total_seconds()
                max_delta = (
                    MAX_ASYNC_WAKEUP_DELTA_SECONDS
                    if s_type == "SYSTEM_MESSAGE"
                    else MAX_AGENT_STEP_DELTA_SECONDS
                )
                if 0.0 < delta <= max_delta:
                    active_agent_seconds += delta
                elif delta == 0.0 and s_type == "PLANNER_RESPONSE":
                    active_agent_seconds += _estimate_planner_generation_seconds(step)
            elif s_type == "PLANNER_RESPONSE":
                active_agent_seconds += _estimate_planner_generation_seconds(step)

        prev_dt = dt
        prev_step_type = s_type

    duration_seconds = (
        max(0.0, (end_dt - start_dt).total_seconds())
        if (start_dt is not None and end_dt is not None)
        else 0.0
    )

    if is_subagent:
        active_pairing_seconds = 0.0

    return (
        round(duration_seconds, 2),
        round(active_agent_seconds, 2),
        round(active_pairing_seconds, 2),
    )


def calculate_force_multiplier(
    pairing_seconds: Any,
    agent_seconds: Any,
    saved_seconds: Any,
) -> tuple[float, float, float]:
    """Calculate (force_multiplier, effective_output_seconds, denominator_seconds).

    Formula:
        Force Multiplier = (Human Pairing + Agent Work + Dev Time Saved) / Human Pairing
    Falls back to agent_seconds (or 1.0) as the denominator if pairing_seconds <= 0.
    """
    safe_pairing = _safe_float(pairing_seconds)
    safe_agent = _safe_float(agent_seconds)
    safe_saved = _safe_float(saved_seconds)

    if safe_pairing > 0.0:
        denominator = safe_pairing
        effective_output = safe_pairing + safe_agent + safe_saved
    elif safe_agent > 0.0:
        denominator = safe_agent
        effective_output = safe_agent + safe_saved
    else:
        return (1.0, 0.0, 0.0)

    force_multiplier = max(1.0, effective_output / denominator)
    return (force_multiplier, effective_output, denominator)


__all__ = [
    "AFK_THRESHOLD_SECONDS",
    "MAX_AGENT_STEP_DELTA_SECONDS",
    "MAX_ASYNC_WAKEUP_DELTA_SECONDS",
    "IGNORED_STEP_TYPES",
    "estimate_prompt_pairing_seconds",
    "compute_session_timings",
    "calculate_force_multiplier",
]
