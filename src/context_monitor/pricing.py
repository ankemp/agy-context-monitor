"""
Centralized Rate Cards, Context Window Limits, and Turn Cost Calculations
-------------------------------------------------------------------------
Single source of truth for LLM pricing and context window capacities.
"""

import re

__all__ = [
    "DEFAULT_CONVERSATIONS_DIR",
    "DEFAULT_MODEL",
    "MODEL_CONTEXT_LIMITS",
    "PRICING",
    "RAW_MODEL_DISPLAY_MAP",
    "SUBAGENT_MODEL_TIER_MAP",
    "calculate_step_cost",
    "extract_model_switch",
    "extract_native_step_models",
    "get_model_context_limit",
    "get_pricing_key",
    "normalize_native_model_name",
    "recompute_step_costs",
    "resolve_subagent_model",
]

DEFAULT_MODEL: str = "Gemini 3.8 Flash (High)"

RAW_MODEL_DISPLAY_MAP: dict[str, str] = {
    "gemini-3.8-flash-tiered": "Gemini 3.8 Flash (Tiered)",
    "gemini-3.8-flash": "Gemini 3.8 Flash (High)",
    "gemini-4-argon": "Gemini 4 Argon (Medium)",
    "gemini-4-argon-b": "Gemini 4 Argon (High)",
    "model_placeholder_m322": "Gemini 3.8 Flash (Tiered)",
    "model_placeholder_m318": "Gemini 3.8 Flash (High)",
    "model_placeholder_m319": "Gemini 3.8 Flash (Medium)",
    "model_placeholder_m417": "Gemini 4 Argon (High)",
    "model_placeholder_m418": "Gemini 4 Argon (Medium)",
    "model_placeholder_m419": "Gemini 4 Argon (Low)",
}

SUBAGENT_MODEL_TIER_MAP: dict[str, str] = {
    "pro": "Gemini 4 Argon (Medium)",
    "flash": "Gemini 3.8 Flash (High)",
    "flash_lite": "Gemini 2.5 Flash Lite",
    "flash-lite": "Gemini 2.5 Flash Lite",
}

# Rates per 1,000,000 tokens: (input_rate_low, input_rate_high_tier_over_128k, output_rate)
PRICING: dict[str, tuple[float, float, float]] = {
    "argon": (2.00, 2.00, 10.00),  # Gemini 4 Argon ($2.00/M in, $10.00/M out flat)
    "pro": (1.25, 2.50, 10.00),  # Gemini Pro / 3.1 Pro / 2.5 Pro
    "flash": (0.075, 0.15, 0.60),  # Gemini Flash / 3.6 / 3.7 / 3.8 Flash
    "claude": (3.00, 3.00, 15.00),  # Claude 3.5 / 3.7 Sonnet / Opus
    "gpt": (2.50, 2.50, 10.00),  # GPT-4o / o1 / o3
}

MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "argon": 1_000_000,
    "flash": 1_000_000,
    "pro": 2_000_000,
    "claude": 200_000,
    "gpt": 128_000,
}


def get_pricing_key(model_name: str) -> str:
    """Resolves a model name string to its canonical pricing key."""
    m = (model_name or "").lower()
    if "argon" in m or "argo" in m or "gemini-4" in m:
        return "argon"
    if "claude" in m or "sonnet" in m or "opus" in m:
        return "claude"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "gpt"
    if "pro" in m:
        return "pro"
    return "flash"


def get_model_context_limit(model_name: str) -> int:
    """Returns the maximum context token window limit for the given model."""
    pkey = get_pricing_key(model_name)
    return MODEL_CONTEXT_LIMITS.get(pkey, 1_000_000)


def calculate_step_cost(
    running_ctx: int,
    ephemeral_tokens: int,
    output_tokens: int,
    model_name: str,
) -> tuple[float, int, int]:
    """
    Computes (step_cost, billed_input_tokens, billed_output_tokens) for a single turn.
    The input prompt size is running_ctx + ephemeral_tokens.
    """
    pkey = get_pricing_key(model_name)
    in_rate_low, in_rate_high, out_rate = PRICING[pkey]
    safe_ctx = max(0, running_ctx or 0)
    safe_eph = max(0, ephemeral_tokens or 0)
    safe_out = max(0, output_tokens or 0)
    actual_in = safe_ctx + safe_eph
    in_rate = in_rate_high if actual_in > 128_000 else in_rate_low
    step_cost = (actual_in / 1_000_000 * in_rate) + (safe_out / 1_000_000 * out_rate)
    return step_cost, actual_in, safe_out


def extract_model_switch(content: str) -> str | None:
    """
    Extracts the newly selected model name from a USER_INPUT step's content.
    Safely scopes to <USER_SETTINGS_CHANGE>...</USER_SETTINGS_CHANGE> if present,
    and handles standard Antigravity setting change phrasings and formatting variations.
    """
    if not content or (
        "Model Selection" not in content and "<USER_SETTINGS_CHANGE>" not in content
    ):
        return None

    search_text = content
    if "<USER_SETTINGS_CHANGE>" in content:
        blocks = re.findall(
            r"<USER_SETTINGS_CHANGE>(.*?)</USER_SETTINGS_CHANGE>", content, re.DOTALL
        )
        if blocks:
            search_text = "\n".join(blocks)

    pattern = (
        r"(?:The user changed setting\s+`?Model Selection`?\s+from\s+.+?\s+to\s+|"
        r"Model Selection\`?\s+from\s+.+?\s+to\s+)"
        r"(.+?)"
        r"(?:\.\s*No need|\.\s*[\r\n]|\.[\r\n]|\.\s*$|[\r\n]|</USER_SETTINGS_CHANGE>|$)"
    )
    m = re.search(pattern, search_text)
    if m:
        model = m.group(1).strip().strip("`\"'")
        if model and not model.startswith("<") and not model.endswith(">"):
            return model
    return None


def normalize_native_model_name(
    model_id: str = "",
    display_name: str = "",
    model_enum: str = "",
) -> str | None:
    """
    Normalizes native model identifiers, display names, and enums from
    gen_metadata into clean canonical display names.
    """
    mid = (model_id or "").strip().strip("`\"'")
    dname = (display_name or "").strip().strip("`\"'")
    menum = (model_enum or "").strip().strip("`\"'")

    if dname:
        dname = dname.splitlines()[0].strip()

    # 1. Tiered Flash display name check (model_id or model_enum)
    if mid.lower() == "gemini-3.8-flash-tiered" or menum.lower() == "model_placeholder_m322":
        return "Gemini 3.8 Flash (Tiered)"

    # 2. If clean display_name is present
    if dname:
        if dname.lower() in ("gemini-3.8-flash-tiered", "model_placeholder_m322"):
            return "Gemini 3.8 Flash (Tiered)"
        if dname.lower() in RAW_MODEL_DISPLAY_MAP:
            return RAW_MODEL_DISPLAY_MAP[dname.lower()]
        return dname

    # 3. Lookup model_enum (more specific tier) then model_id in RAW_MODEL_DISPLAY_MAP
    if menum.lower() in RAW_MODEL_DISPLAY_MAP:
        return RAW_MODEL_DISPLAY_MAP[menum.lower()]
    if mid.lower() in RAW_MODEL_DISPLAY_MAP:
        return RAW_MODEL_DISPLAY_MAP[mid.lower()]

    if mid:
        return mid
    if menum:
        return menum

    return None


def resolve_subagent_model(
    requested_tier: str | None,
    parent_model: str | None = None,
) -> str:
    """
    Resolves the effective model for a subagent given a requested tier
    and the parent conversation's active model.
    """
    clean_parent = (parent_model or "").strip()
    clean_parent = clean_parent.replace("->", "→")
    parts = [p.strip() for p in clean_parent.split("→") if p.strip()]
    clean_parent = parts[-1] if parts else ""

    if not clean_parent or clean_parent.lower() in ("inherit", "default", "none", "null"):
        clean_parent = DEFAULT_MODEL

    req = (requested_tier or "").strip()
    if not req or req.lower() in ("inherit", "default"):
        return clean_parent

    tier_key = req.lower().replace(" ", "_")
    if tier_key in SUBAGENT_MODEL_TIER_MAP:
        return SUBAGENT_MODEL_TIER_MAP[tier_key]

    norm = normalize_native_model_name(req)
    if norm:
        return norm

    return req


def recompute_step_costs(
    steps: list[dict], active_model: str
) -> dict[str, float | int]:
    """
    Recalculates billed cost, input/output tokens, active context tokens, and compaction counts
    across steps for a given model. Mutates each step dict with billed_cost, billed_input_tokens,
    and billed_output_tokens.
    """
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0
    compaction_count = 0
    pruned_tokens = 0

    for s in steps:
        m_name = s.get("model_name", active_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)

        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
            compaction_count += 1
            if running_ctx > tokens:
                pruned_tokens += running_ctx - tokens
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            step_cost, step_in_tokens, step_out_tokens = calculate_step_cost(
                running_ctx, current_ephemeral, tokens, m_name
            )
            total_billed_cost += step_cost
            total_billed_input_tokens += step_in_tokens
            total_billed_output_tokens += step_out_tokens
            running_ctx += tokens
            current_ephemeral = 0
        else:
            running_ctx += tokens

        s["billed_cost"] = step_cost
        s["billed_input_tokens"] = step_in_tokens
        s["billed_output_tokens"] = step_out_tokens

    return {
        "billed_cost": total_billed_cost,
        "billed_input_tokens": total_billed_input_tokens,
        "billed_output_tokens": total_billed_output_tokens,
        "active_context_tokens": running_ctx,
        "compaction_count": compaction_count,
        "pruned_tokens": pruned_tokens,
    }


# Re-export subagent symbols for backwards compatibility
from context_monitor.subagent import (
    DEFAULT_CONVERSATIONS_DIR,
    extract_native_step_models,
)


