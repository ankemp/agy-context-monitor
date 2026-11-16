"""
Centralized Rate Cards, Context Window Limits, and Turn Cost Calculations
-------------------------------------------------------------------------
Single source of truth for LLM pricing and context window capacities.
"""

from datetime import datetime
import re

__all__ = [
    "CACHED_INPUT_PRICING",
    "CACHE_TTL_SECONDS",
    "DEFAULT_CONVERSATIONS_DIR",
    "DEFAULT_MODEL",
    "MODEL_CONTEXT_LIMITS",
    "POST_INTRO_CACHED_INPUT_PRICING",
    "POST_INTRO_PRICING",
    "POST_INTRO_START_DATE",
    "PRICING",
    "RAW_MODEL_DISPLAY_MAP",
    "SUBAGENT_MODEL_TIER_MAP",
    "calculate_step_cost",
    "extract_model_switch",
    "extract_native_step_models",
    "extract_native_step_tokens",
    "get_effective_rates",
    "get_model_context_limit",
    "get_model_family",
    "get_pricing_key",
    "is_legacy_model",
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
    "gemini-4-carbon": "Gemini 4 Carbon (Medium)",
    "gemini-4-hydrogen": "Gemini 4 Hydrogen (High)",
    "gemini-pro-orchestrator": "Orchestrator (Carbon)",
    "gemini-flash-worker-agy": "Gemini Flash",
    "model_placeholder_m322": "Gemini 3.8 Flash (Tiered)",
    "model_placeholder_m318": "Gemini 3.8 Flash (High)",
    "model_placeholder_m319": "Gemini 3.8 Flash (Medium)",
    "model_placeholder_m417": "Gemini 4 Argon (High)",
    "model_placeholder_m418": "Gemini 4 Argon (Medium)",
    "model_placeholder_m419": "Gemini 4 Argon (Low)",
    "model_placeholder_m437": "Gemini 4 Carbon (Medium)",
    "model_placeholder_m438": "Gemini 4 Carbon (Low)",
    "model_placeholder_m525": "Gemini 4 Hydrogen (High)",
    "model_placeholder_m526": "Gemini 4 Hydrogen (Medium)",
    "model_placeholder_m528": "Gemini 4 Hydrogen (XHigh)",
    "model_placeholder_m667": "Orchestrator (Carbon)",
    "model_placeholder_m669": "Gemini Flash",
}

SUBAGENT_MODEL_TIER_MAP: dict[str, str] = {
    "pro": "Gemini 4 Argon (Medium)",
    "flash": "Gemini 3.8 Flash (High)",
    "flash_lite": "Gemini 2.5 Flash Lite",
    "flash-lite": "Gemini 2.5 Flash Lite",
    "carbon": "Gemini 4 Carbon (Medium)",
    "argon": "Gemini 4 Argon (Medium)",
    "hydrogen": "Gemini 4 Hydrogen (High)",
}

# Rates per 1,000,000 tokens: (input_rate_low, input_rate_high_tier_over_128k, output_rate)
PRICING: dict[str, tuple[float, float, float]] = {
    "carbon": (2.00, 2.00, 10.00),   # Gemini 4 Carbon intro ($2.00/M in, $10.00/M out flat; $4/$20 starting 2026-12-01)
    "argon": (2.00, 2.00, 10.00),    # Gemini 4 Argon intro ($2.00/M in, $10.00/M out flat; $4/$20 starting 2026-12-01)
    "hydrogen": (1.50, 1.50, 7.50),  # Gemini 4 Hydrogen ($1.50/M in, $7.50/M out flat)
    "pro": (1.25, 2.50, 10.00),      # Gemini Pro / 3.1 Pro / 2.5 Pro
    "flash": (0.075, 0.15, 0.60),    # Gemini Flash / 3.6 / 3.7 / 3.8 Flash
    "claude": (3.00, 3.00, 15.00),   # Claude 3.5 / 3.7 Sonnet / Opus
    "gpt": (2.50, 2.50, 10.00),      # GPT-4o / o1 / o3
}

# Implicit prompt cache-read rates per 1,000,000 tokens (Gemini 4 family: 95% discount)
CACHED_INPUT_PRICING: dict[str, float] = {
    "carbon": 0.10,
    "argon": 0.10,
    "hydrogen": 0.075,
}

# Scheduled rate increase for Gemini 4 Carbon & Argon effective December 1, 2026
POST_INTRO_START_DATE: str = "2026-12-01"
POST_INTRO_PRICING: dict[str, tuple[float, float, float]] = {
    "carbon": (4.00, 4.00, 20.00),
    "argon": (4.00, 4.00, 20.00),
}
POST_INTRO_CACHED_INPUT_PRICING: dict[str, float] = {
    "carbon": 0.20,
    "argon": 0.20,
}

# Implicit KV prefix cache warm window used when transcript lacks native cache_read_tokens
CACHE_TTL_SECONDS: int = 300

MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "carbon": 1_000_000,
    "argon": 1_000_000,
    "hydrogen": 1_000_000,
    "flash": 1_000_000,
    "pro": 2_000_000,
    "claude": 200_000,
    "gpt": 128_000,
}


def get_pricing_key(model_name: str) -> str:
    """Resolves a model name string to its canonical pricing key."""
    m = (model_name or "").lower()
    if "carbon" in m or "orchestrator" in m:
        return "carbon"
    if "hydrogen" in m:
        return "hydrogen"
    if "argon" in m or "argo" in m:
        return "argon"
    if "claude" in m or "sonnet" in m or "opus" in m:
        return "claude"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "gpt"
    if "gemini-4" in m or "gemini 4" in m:
        return "argon"
    if "pro" in m:
        return "pro"
    return "flash"


def get_effective_rates(
    pricing_key: str, created_at: str | None = None
) -> tuple[float, float, float, float | None]:
    """
    Returns (in_rate_low, in_rate_high, out_rate, cached_in_rate) for the pricing key,
    applying scheduled post-introductory pricing when created_at >= 2026-12-01.
    """
    date_prefix = ""
    if created_at and isinstance(created_at, str):
        date_prefix = created_at.strip()[:10]
    if (
        len(date_prefix) == 10
        and date_prefix[4] == "-"
        and date_prefix[7] == "-"
        and date_prefix >= POST_INTRO_START_DATE
        and pricing_key in POST_INTRO_PRICING
    ):
        in_low, in_high, out_rate = POST_INTRO_PRICING[pricing_key]
        cache_rate = POST_INTRO_CACHED_INPUT_PRICING.get(pricing_key)
        return in_low, in_high, out_rate, cache_rate

    in_low, in_high, out_rate = PRICING[pricing_key]
    cache_rate = CACHED_INPUT_PRICING.get(pricing_key)
    return in_low, in_high, out_rate, cache_rate


def get_model_context_limit(model_name: str) -> int:
    """Returns the maximum context token window limit for the given model."""
    pkey = get_pricing_key(model_name)
    return MODEL_CONTEXT_LIMITS.get(pkey, 1_000_000)


_TIER_SUFFIX_RE = re.compile(
    r"\s*\((?:high|medium|low|xhigh|tiered|thinking|reasoning)\)\s*$",
    re.IGNORECASE,
)
_LEGACY_GEMINI_VERSION_RE = re.compile(
    r"\bgemini[- ]?(?:1(?:\.\d+)?|2(?:\.\d+)?|3\.[0-7])\b",
    re.IGNORECASE,
)


def get_model_family(model_name: str) -> str:
    """Normalizes a model name into its display family (stripping tier suffixes)."""
    raw = (model_name or "").strip()
    if not raw:
        return "Gemini"

    resolved = RAW_MODEL_DISPLAY_MAP.get(raw.lower(), raw)
    collapsed = re.sub(r"\s+", " ", resolved).strip()
    lower = collapsed.lower()
    if "orchestrator" in lower and ("carbon" in lower or lower == "orchestrator"):
        return "Gemini 4 Carbon"

    family = _TIER_SUFFIX_RE.sub("", collapsed).strip()
    family = re.sub(r"\s+", " ", family).strip()
    return family or "Gemini"


def is_legacy_model(model_name: str) -> bool:
    """Returns True if the model is a deprecated/legacy Gemini generation."""
    raw = (model_name or "").strip()
    if not raw:
        return False

    resolved = RAW_MODEL_DISPLAY_MAP.get(raw.lower(), raw)
    m = re.sub(r"\s+", " ", resolved.lower()).strip()

    if (
        "orchestrator" in m
        or "gemini 4" in m
        or "gemini-4" in m
        or "3.8" in m
        or m == "gemini flash"
    ):
        return False

    if _LEGACY_GEMINI_VERSION_RE.search(m):
        return True

    if ("gemini pro" in m or "gemini-pro" in m) and "4" not in m:
        return True

    return False


def calculate_step_cost(
    running_ctx: int,
    ephemeral_tokens: int,
    output_tokens: int,
    model_name: str,
    *,
    cached_tokens: int = 0,
    created_at: str | None = None,
) -> tuple[float, int, int]:
    """
    Computes (step_cost, billed_input_tokens, billed_output_tokens) for a single turn.
    The total input prompt size is running_ctx + ephemeral_tokens; any cached prefix
    tokens (up to running_ctx) are billed at the model's cached-input rate when available.
    """
    pkey = get_pricing_key(model_name)
    in_rate_low, in_rate_high, out_rate, cache_rate = get_effective_rates(
        pkey, created_at=created_at
    )
    safe_ctx = max(0, running_ctx or 0)
    safe_eph = max(0, ephemeral_tokens or 0)
    safe_out = max(0, output_tokens or 0)
    actual_in = safe_ctx + safe_eph
    safe_cached = min(max(0, cached_tokens or 0), safe_ctx)
    uncached_in = actual_in - safe_cached
    in_rate = in_rate_high if actual_in > 128_000 else in_rate_low
    eff_cache_rate = (
        cache_rate if (cache_rate is not None and safe_cached > 0) else in_rate
    )
    step_cost = (
        (uncached_in / 1_000_000 * in_rate)
        + (safe_cached / 1_000_000 * eff_cache_rate)
        + (safe_out / 1_000_000 * out_rate)
    )
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


def extract_native_step_tokens(step: dict) -> dict[str, int]:
    """Extracts native API token telemetry (input_tokens, cache_read_tokens, output_tokens) from a transcript step."""
    out: dict[str, int] = {}
    for key in ("input_tokens", "cache_read_tokens", "output_tokens"):
        raw = step.get(key)
        if raw is None:
            continue
        try:
            out[key] = max(0, int(raw))
        except (ValueError, TypeError):
            pass
    if "input_tokens" in out and "cache_read_tokens" not in out:
        out["cache_read_tokens"] = 0
    return out


def _parse_iso_ts(ts: str | None) -> datetime | None:
    """Parses an ISO-8601 timestamp string into a datetime, returning None on invalid input."""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip().replace(" ", "T")
    if s.endswith(("z", "Z")):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def recompute_step_costs(
    steps: list[dict], active_model: str
) -> dict[str, float | int]:
    """
    Recalculates billed cost, input/output tokens, active context tokens, and compaction counts
    across steps for a given model. Uses native transcript token counts (input_tokens,
    cache_read_tokens, output_tokens) when present, falling back to cumulative context
    estimation with implicit KV prefix caching for models with cached-input pricing.
    Mutates each step dict with billed_cost, billed_input_tokens, and billed_output_tokens.
    """
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0
    compaction_count = 0
    pruned_tokens = 0
    prev_cached_ctx = 0
    prev_pkey: str | None = None
    prev_planner_dt: datetime | None = None

    for s in steps:
        m_name = s.get("model_name", active_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)
        created_at = s.get("created_at")

        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
            compaction_count += 1
            if running_ctx > tokens:
                pruned_tokens += running_ctx - tokens
            running_ctx = tokens
            current_ephemeral = 0
            prev_cached_ctx = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            current_ephemeral = tokens
        elif s_type == "PLANNER_RESPONSE":
            cur_pkey = get_pricing_key(m_name)
            cur_dt = _parse_iso_ts(created_at)
            raw_in = s.get("input_tokens")
            raw_out = s.get("output_tokens")

            if raw_in is not None and raw_out is not None:
                uncached_in = max(0, int(raw_in))
                cached_in = max(0, int(s.get("cache_read_tokens") or 0))
                out_tok = max(0, int(raw_out))
                total_in = uncached_in + cached_in
                step_cost, step_in_tokens, step_out_tokens = calculate_step_cost(
                    total_in,
                    0,
                    out_tok,
                    m_name,
                    cached_tokens=cached_in,
                    created_at=created_at,
                )
                if total_in > 0 or out_tok > 0:
                    running_ctx = total_in + out_tok
            else:
                cache_warm = (
                    cur_pkey in CACHED_INPUT_PRICING
                    and prev_pkey == cur_pkey
                    and prev_cached_ctx > 0
                    and running_ctx >= prev_cached_ctx
                )
                if cache_warm and cur_dt is not None and prev_planner_dt is not None:
                    if (cur_dt - prev_planner_dt).total_seconds() > CACHE_TTL_SECONDS:
                        cache_warm = False
                cached_tok = prev_cached_ctx if cache_warm else 0
                step_cost, step_in_tokens, step_out_tokens = calculate_step_cost(
                    running_ctx,
                    current_ephemeral,
                    tokens,
                    m_name,
                    cached_tokens=cached_tok,
                    created_at=created_at,
                )
                running_ctx += tokens

            total_billed_cost += step_cost
            total_billed_input_tokens += step_in_tokens
            total_billed_output_tokens += step_out_tokens
            prev_cached_ctx = running_ctx
            prev_pkey = cur_pkey
            prev_planner_dt = cur_dt
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


