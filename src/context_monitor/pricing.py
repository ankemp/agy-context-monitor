"""
Centralized Rate Cards, Context Window Limits, and Turn Cost Calculations
-------------------------------------------------------------------------
Single source of truth for LLM pricing and context window capacities.
"""

import os
from pathlib import Path
import re
import sqlite3

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
    "resolve_subagent_model",
]

DEFAULT_CONVERSATIONS_DIR: Path = Path(os.path.expanduser("~/.gemini/antigravity/conversations"))

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


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """
    Decodes a protobuf varint starting at `offset`.
    Returns (value, new_offset). Raises ValueError on truncated or malformed varint.
    """
    val = 0
    shift = 0
    n = len(data)
    while offset < n:
        byte = data[offset]
        offset += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, offset
        shift += 7
        if shift > 64:
            raise ValueError("Varint too long")
    raise ValueError("Truncated varint")


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decodes a byte string containing packed varints, preserving partial results."""
    values: list[int] = []
    offset = 0
    n = len(data)
    while offset < n:
        try:
            val, offset = _decode_varint(data, offset)
            values.append(val)
        except Exception:
            break
    return values


def _parse_pb_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """
    Parses top-level protobuf fields from `data` into tuples of (field_num, wire_type, value).
    Guards against malformed wire types and out-of-bounds byte lengths, returning partial results.
    """
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0
    n = len(data)
    while offset < n:
        try:
            tag, offset = _decode_varint(data, offset)
        except Exception:
            break
        field_num = tag >> 3
        wire_type = tag & 0x7

        if field_num == 0:
            break

        if wire_type == 0:  # Varint
            try:
                val, offset = _decode_varint(data, offset)
            except Exception:
                break
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:  # 64-bit
            if offset + 8 > n:
                break
            val = data[offset : offset + 8]
            offset += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:  # Length-delimited
            try:
                length, offset = _decode_varint(data, offset)
            except Exception:
                break
            if length < 0 or offset + length > n:
                break
            val = data[offset : offset + length]
            offset += length
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:  # 32-bit
            if offset + 4 > n:
                break
            val = data[offset : offset + 4]
            offset += 4
            fields.append((field_num, wire_type, val))
        else:
            # Unsupported or corrupt wire type
            break

    return fields


def extract_native_step_models(
    cid: str,
    conversations_dir: Path | None = None,
) -> dict[int, str]:
    """
    Extracts ground-truth step model assignments from native SQLite DB (~/.gemini/antigravity/conversations/<cid>.db)
    gen_metadata table. Uses read-only SQLite URI and varint/protobuf decoder.
    Returns mapping of {step_index: model_name}.
    """
    if not cid:
        return {}

    base_dir = Path(conversations_dir) if conversations_dir is not None else DEFAULT_CONVERSATIONS_DIR
    cid_clean = cid[:-3] if cid.endswith(".db") else cid
    db_path = base_dir / f"{cid_clean}.db"

    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return {}
    except OSError:
        return {}

    conn = None
    step_models: dict[int, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        rows = cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC").fetchall()
        for idx, raw_data in rows:
            if not raw_data or not isinstance(raw_data, (bytes, bytearray, memoryview)):
                continue

            try:
                top_fields = _parse_pb_fields(bytes(raw_data))
            except Exception:
                continue

            step_indices: list[int] = []
            f1_bytes: bytes | None = None
            for fn, wt, val in top_fields:
                if fn == 2:
                    if wt == 0 and isinstance(val, int):
                        step_indices.append(val)
                    elif wt == 2 and isinstance(val, (bytes, bytearray)):
                        try:
                            step_indices.extend(_decode_packed_varints(bytes(val)))
                        except Exception:
                            pass
                elif fn == 1 and wt == 2 and isinstance(val, (bytes, bytearray)):
                    f1_bytes = bytes(val)

            model_id = ""
            display_name = ""
            model_enum = ""
            if f1_bytes:
                try:
                    sub_fields = _parse_pb_fields(f1_bytes)
                    for sub_field_num, sub_wire_type, sub_val in sub_fields:
                        if sub_field_num == 19 and sub_wire_type == 2 and isinstance(sub_val, (bytes, bytearray)):
                            model_id = bytes(sub_val).decode("utf-8", "ignore")
                        elif sub_field_num == 21 and sub_wire_type == 2 and isinstance(sub_val, (bytes, bytearray)):
                            display_name = bytes(sub_val).decode("utf-8", "ignore")
                        elif sub_field_num == 20 and sub_wire_type == 2 and isinstance(sub_val, (bytes, bytearray)):
                            try:
                                kv_fields = _parse_pb_fields(bytes(sub_val))
                                key_str = ""
                                val_str = ""
                                for kv_field_num, kv_wire_type, kv_val in kv_fields:
                                    if kv_field_num == 1 and kv_wire_type == 2 and isinstance(kv_val, (bytes, bytearray)):
                                        key_str = bytes(kv_val).decode("utf-8", "ignore")
                                    elif kv_field_num == 2 and kv_wire_type == 2 and isinstance(kv_val, (bytes, bytearray)):
                                        val_str = bytes(kv_val).decode("utf-8", "ignore")
                                if key_str.strip().lower() == "model_enum":
                                    model_enum = val_str.strip()
                            except Exception:
                                pass
                except Exception:
                    pass

            resolved_model = normalize_native_model_name(
                model_id=model_id,
                display_name=display_name,
                model_enum=model_enum,
            )
            if resolved_model:
                if not step_indices:
                    fallback_idx = (idx + 1) if isinstance(idx, int) else 1
                    step_indices = [fallback_idx]
                for st in step_indices:
                    step_models[st] = resolved_model

        return step_models
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

