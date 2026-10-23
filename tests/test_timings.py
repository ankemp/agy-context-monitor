"""Unit tests for pure inter-turn human pairing time, agent execution time, and force multiplier."""

import pytest

from context_monitor.timings import (
    AFK_THRESHOLD_SECONDS,
    MAX_AGENT_STEP_DELTA_SECONDS,
    MAX_ASYNC_WAKEUP_DELTA_SECONDS,
    calculate_force_multiplier,
    compute_session_timings,
    estimate_prompt_pairing_seconds,
)


def test_constants():
    assert AFK_THRESHOLD_SECONDS == 900.0
    assert MAX_AGENT_STEP_DELTA_SECONDS == 1800.0
    assert MAX_ASYNC_WAKEUP_DELTA_SECONDS == 60.0


def test_package_exports():
    import context_monitor

    assert hasattr(context_monitor, "AFK_THRESHOLD_SECONDS")
    assert hasattr(context_monitor, "MAX_AGENT_STEP_DELTA_SECONDS")
    assert hasattr(context_monitor, "MAX_ASYNC_WAKEUP_DELTA_SECONDS")
    assert hasattr(context_monitor, "estimate_prompt_pairing_seconds")
    assert hasattr(context_monitor, "compute_session_timings")
    assert hasattr(context_monitor, "calculate_force_multiplier")

    for symbol in (
        "AFK_THRESHOLD_SECONDS",
        "MAX_AGENT_STEP_DELTA_SECONDS",
        "MAX_ASYNC_WAKEUP_DELTA_SECONDS",
        "estimate_prompt_pairing_seconds",
        "compute_session_timings",
        "calculate_force_multiplier",
    ):
        assert symbol in context_monitor.__all__


def test_estimate_prompt_pairing_seconds():
    # Base 15s + 1s per 5 chars, clamped to [15.0, 900.0]
    assert estimate_prompt_pairing_seconds(0) == 15.0
    assert estimate_prompt_pairing_seconds(100) == 35.0
    assert estimate_prompt_pairing_seconds(50000) == 900.0
    # Edge cases: negative, None, string numbers, invalid strings, floats
    assert estimate_prompt_pairing_seconds(-10) == 15.0
    assert estimate_prompt_pairing_seconds(None) == 15.0
    assert estimate_prompt_pairing_seconds("100") == 35.0
    assert estimate_prompt_pairing_seconds("not_a_number") == 15.0
    assert estimate_prompt_pairing_seconds(100.5) == 35.0


def test_compute_session_timings_root_conversation():
    steps = [
        # Initial user prompt: 100 chars -> 15 + 20 = 35s pairing
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "USER_INPUT",
            "characters": 100,
            "est_tokens": 25,
        },
        # Agent step 1: +10s agent work
        {
            "timestamp": "2026-10-20T10:00:10Z",
            "type": "PLANNER_RESPONSE",
            "characters": 400,
            "est_tokens": 100,
        },
        # Agent step 2: +15s agent work
        {
            "timestamp": "2026-10-20T10:00:25Z",
            "type": "PLANNER_RESPONSE",
            "characters": 800,
            "est_tokens": 200,
        },
        # Human reads output & writes next prompt for 120s (<= 900s AFK threshold) -> +120s pairing
        {
            "timestamp": "2026-10-20T10:02:25Z",
            "type": "USER_INPUT",
            "characters": 50,
            "est_tokens": 12,
        },
        # Agent responds: +20s agent work
        {
            "timestamp": "2026-10-20T10:02:45Z",
            "type": "PLANNER_RESPONSE",
            "characters": 300,
            "est_tokens": 75,
        },
        # Human goes AFK for 1 hour (3600s > 900s), then sends 200-char prompt -> +55s estimated pairing
        {
            "timestamp": "2026-10-20T11:02:45Z",
            "type": "USER_INPUT",
            "characters": 200,
            "est_tokens": 50,
        },
        # Agent responds: +5s agent work
        {
            "timestamp": "2026-10-20T11:02:50Z",
            "type": "PLANNER_RESPONSE",
            "characters": 200,
            "est_tokens": 50,
        },
    ]

    duration_sec, agent_sec, pairing_sec = compute_session_timings(
        steps, is_subagent=False
    )
    assert duration_sec == 3770.0
    assert agent_sec == 10.0 + 15.0 + 20.0 + 5.0
    assert pairing_sec == 35.0 + 120.0 + 55.0


def test_compute_session_timings_subagent_zero_pairing_and_wakeup_exclusion():
    steps = [
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "USER_INPUT",
            "characters": 500,
            "est_tokens": 125,
        },
        {
            "timestamp": "2026-10-20T10:00:12Z",
            "type": "PLANNER_RESPONSE",
            "characters": 400,
            "est_tokens": 100,
        },
        # Parent waits 300s for a subagent wakeup via SYSTEM_MESSAGE (> 60s cap -> excluded!)
        {
            "timestamp": "2026-10-20T10:05:12Z",
            "type": "SYSTEM_MESSAGE",
            "characters": 300,
            "est_tokens": 75,
        },
        # Agent processes wakeup for 8s
        {
            "timestamp": "2026-10-20T10:05:20Z",
            "type": "PLANNER_RESPONSE",
            "characters": 600,
            "est_tokens": 150,
        },
    ]

    # As a subagent: pairing is 0.0, agent work is 12s + 8s = 20s (300s idle wait excluded)
    duration_sec, agent_sec, pairing_sec = compute_session_timings(
        steps, is_subagent=True
    )
    assert duration_sec == 320.0
    assert agent_sec == 20.0
    assert pairing_sec == 0.0


def test_compute_session_timings_system_message_within_cap():
    # SYSTEM_MESSAGE wakeup <= 60s should be credited as active agent work
    steps = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "USER_INPUT", "characters": 100},
        {"timestamp": "2026-10-20T10:00:10Z", "type": "PLANNER_RESPONSE"},
        # 25s wakeup delta (<= 60s cap) -> credited!
        {"timestamp": "2026-10-20T10:00:35Z", "type": "SYSTEM_MESSAGE"},
        {"timestamp": "2026-10-20T10:00:40Z", "type": "PLANNER_RESPONSE"},
    ]
    dur, agent, pairing = compute_session_timings(steps, is_subagent=False)
    assert dur == 40.0
    assert agent == 10.0 + 25.0 + 5.0  # 40.0s
    assert pairing == 35.0


def test_calculate_force_multiplier():
    # 10m (600s) pairing, 20m (1200s) agent work, 30m (1800s) dev time saved
    # Total output = 3600s, Force Multiplier = 3600 / 600 = 6.0x
    fm, eff_sec, denom_sec = calculate_force_multiplier(600.0, 1200.0, 1800.0)
    assert fm == pytest.approx(6.0)
    assert eff_sec == pytest.approx(3600.0)
    assert denom_sec == pytest.approx(600.0)

    # Zero pairing fallback to agent_seconds
    fm_sub, eff_sub, denom_sub = calculate_force_multiplier(0.0, 300.0, 600.0)
    assert fm_sub == pytest.approx(3.0)
    assert eff_sub == pytest.approx(900.0)
    assert denom_sub == pytest.approx(300.0)

    # Zero pairing and zero agent returns default (1.0, 0.0, 0.0)
    fm_zero, eff_zero, denom_zero = calculate_force_multiplier(0.0, 0.0, 500.0)
    assert fm_zero == 1.0
    assert eff_zero == 0.0
    assert denom_zero == 0.0

    # Robustness against string inputs, NaN, negative values, Inf
    fm_str, eff_str, denom_str = calculate_force_multiplier("600.0", "1200.0", "1800.0")
    assert fm_str == pytest.approx(6.0)
    assert eff_str == pytest.approx(3600.0)
    assert denom_str == pytest.approx(600.0)

    fm_nan, eff_nan, denom_nan = calculate_force_multiplier(
        "invalid", float("nan"), 100.0
    )
    assert fm_nan == 1.0
    assert eff_nan == 0.0
    assert denom_nan == 0.0

    fm_neg, eff_neg, denom_neg = calculate_force_multiplier(-100.0, 50.0, 50.0)
    assert fm_neg == pytest.approx(2.0)
    assert eff_neg == pytest.approx(100.0)
    assert denom_neg == pytest.approx(50.0)


def test_compute_session_timings_edge_cases():
    # Empty steps
    assert compute_session_timings([]) == (0.0, 0.0, 0.0)

    # Ignored steps only
    ignored = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "EPHEMERAL_MESSAGE"},
        {"timestamp": "2026-10-20T10:01:00Z", "type": "CHECKPOINT"},
        {"timestamp": "2026-10-20T10:02:00Z", "type": "CONVERSATION_HISTORY"},
    ]
    assert compute_session_timings(ignored) == (0.0, 0.0, 0.0)

    # Consecutive USER_INPUT steps credit estimate_prompt_pairing_seconds each
    consecutive_prompts = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "USER_INPUT", "characters": 100},
        {"timestamp": "2026-10-20T10:00:10Z", "type": "USER_INPUT", "characters": 50},
    ]
    dur, agent, pairing = compute_session_timings(consecutive_prompts)
    assert dur == 10.0
    assert agent == 0.0
    assert pairing == 35.0 + 25.0

    # Delta == 0 fallback for PLANNER_RESPONSE with tokens
    zero_delta_steps = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "USER_INPUT", "characters": 100},
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "PLANNER_RESPONSE",
            "est_tokens": 160,
            "thinking_tokens": 80,
        },
    ]
    dur, agent, pairing = compute_session_timings(zero_delta_steps)
    # out_tok = 240 -> 240 / 80.0 = 3.0s fallback
    assert agent == 3.0
    assert pairing == 35.0

    # Delta == 0 fallback for PLANNER_RESPONSE with 0 tokens (must still credit 1.0s floor!)
    zero_delta_zero_tokens = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "USER_INPUT", "characters": 100},
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "PLANNER_RESPONSE",
            "est_tokens": 0,
            "thinking_tokens": 0,
        },
    ]
    dur, agent, pairing = compute_session_timings(zero_delta_zero_tokens)
    assert agent == 1.0
    assert pairing == 35.0

    # First step is PLANNER_RESPONSE with tokens
    first_step_planner = [
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "PLANNER_RESPONSE",
            "est_tokens": 80,
        },
    ]
    dur, agent, pairing = compute_session_timings(first_step_planner)
    assert agent == 1.0
    assert pairing == 0.0

    # First step is PLANNER_RESPONSE with 0 tokens (must still credit 1.0s floor!)
    first_step_zero_tokens = [
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "PLANNER_RESPONSE",
            "est_tokens": 0,
        },
    ]
    dur, agent, pairing = compute_session_timings(first_step_zero_tokens)
    assert agent == 1.0
    assert pairing == 0.0

    # Mixed offset-naive and offset-aware ISO timestamps
    mixed_tz_steps = [
        {"timestamp": "2026-10-20T10:00:00Z", "type": "USER_INPUT", "characters": 100},
        {
            "timestamp": "2026-10-20 10:00:15",
            "type": "PLANNER_RESPONSE",
            "characters": 200,
        },
    ]
    dur, agent, pairing = compute_session_timings(mixed_tz_steps)
    assert dur == 15.0
    assert agent == 15.0
    assert pairing == 35.0

    # Unix epoch timestamps (int and float and string)
    epoch_steps = [
        {"timestamp": 1761000000, "type": "USER_INPUT", "characters": 100},
        {"timestamp": "1761000020", "type": "PLANNER_RESPONSE", "characters": 200},
    ]
    dur, agent, pairing = compute_session_timings(epoch_steps)
    assert dur == 20.0
    assert agent == 20.0
    assert pairing == 35.0

    # Malformed steps in list (None, non-dict, non-int characters)
    malformed_steps = [
        None,
        "corrupted_entry",
        {
            "timestamp": "2026-10-20T10:00:00Z",
            "type": "USER_INPUT",
            "characters": "not_an_int",
        },
        {
            "timestamp": "2026-10-20T10:00:10Z",
            "type": "PLANNER_RESPONSE",
            "est_tokens": "invalid",
        },
    ]
    dur, agent, pairing = compute_session_timings(malformed_steps)
    assert dur == 10.0
    assert agent == 10.0
    assert pairing == 15.0  # Safe fallback for invalid char count

    # Backward compatibility with created_at and step_type keys
    compat_steps = [
        {
            "created_at": "2026-10-20T10:00:00Z",
            "step_type": "USER_INPUT",
            "characters": 100,
        },
        {
            "created_at": "2026-10-20T10:00:15Z",
            "step_type": "PLANNER_RESPONSE",
            "characters": 200,
        },
    ]
    dur, agent, pairing = compute_session_timings(compat_steps)
    assert dur == 15.0
    assert agent == 15.0
    assert pairing == 35.0
