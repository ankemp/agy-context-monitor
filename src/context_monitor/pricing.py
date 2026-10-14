"""
Centralized Rate Cards, Context Window Limits, and Turn Cost Calculations
-------------------------------------------------------------------------
Single source of truth for LLM pricing and context window capacities.
"""

import re

__all__ = [
    "DEFAULT_MODEL",
    "MODEL_CONTEXT_LIMITS",
    "PRICING",
    "calculate_step_cost",
    "extract_model_switch",
    "get_model_context_limit",
    "get_pricing_key",
]

DEFAULT_MODEL: str = "Gemini 3.8 Flash (High)"

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
