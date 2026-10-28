"""
Subagent Extraction, Lineage, and Metadata Module
-------------------------------------------------
Encapsulates native gen_metadata protobuf/SQLite extraction, subagent tool-output
parsing, model-transition application helpers, and database stub upserts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from context_monitor.pricing import DEFAULT_MODEL, resolve_subagent_model

__all__ = [
    "DEFAULT_CONVERSATIONS_DIR",
    "SUBAGENT_STOP_WORDS",
    "apply_native_step_models_fallback",
    "apply_step_model_switch",
    "extract_invoked_subagent_ids",
    "extract_native_step_models",
    "extract_subagent_specs",
    "is_manage_subagents_output",
    "is_subagent_dispatch_output",
    "is_valid_subagent_id",
    "parse_manage_subagents_output",
    "upsert_dispatched_subagent_stubs",
]

DEFAULT_CONVERSATIONS_DIR: Path = Path(
    os.path.expanduser("~/.gemini/antigravity/conversations")
)

SUBAGENT_STOP_WORDS: set[str] = {
    "cid", "id", "str", "int", "bool", "none", "null", "true", "false",
    "and", "or", "in", "into", "for", "so", "to", "from", "with", "by", "as",
    "on", "at", "of", "the", "a", "an", "asc", "desc", "text", "real", "integer",
    "select", "where", "table", "column", "index", "walk", "safely", "such",
    "title", "interview", "transcript", "transcripts", "isolation", "history",
    "context", "directory", "logs", "dbs", "analytics", "echoes", "shim",
    "assertions", "discovers", "subagents", "subagent", "conversation",
    "conversations", "conversationid",
}

_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
_HYPHENATED_ID_RE = re.compile(r"^[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)+$")


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

    base_dir = (
        Path(conversations_dir)
        if conversations_dir is not None
        else DEFAULT_CONVERSATIONS_DIR
    )
    cid_clean = cid[:-3] if cid.endswith(".db") else cid
    db_path = base_dir / f"{cid_clean}.db"

    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return {}
    except OSError:
        return {}

    # Import normalize_native_model_name dynamically to prevent circular import
    from context_monitor.pricing import normalize_native_model_name

    conn = None
    step_models: dict[int, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT idx, data FROM gen_metadata ORDER BY idx ASC"
        ).fetchall()
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
                        if (
                            sub_field_num == 19
                            and sub_wire_type == 2
                            and isinstance(sub_val, (bytes, bytearray))
                        ):
                            model_id = bytes(sub_val).decode("utf-8", "ignore")
                        elif (
                            sub_field_num == 21
                            and sub_wire_type == 2
                            and isinstance(sub_val, (bytes, bytearray))
                        ):
                            display_name = bytes(sub_val).decode("utf-8", "ignore")
                        elif (
                            sub_field_num == 20
                            and sub_wire_type == 2
                            and isinstance(sub_val, (bytes, bytearray))
                        ):
                            try:
                                kv_fields = _parse_pb_fields(bytes(sub_val))
                                key_str = ""
                                val_str = ""
                                for kv_field_num, kv_wire_type, kv_val in kv_fields:
                                    if (
                                        kv_field_num == 1
                                        and kv_wire_type == 2
                                        and isinstance(kv_val, (bytes, bytearray))
                                    ):
                                        key_str = bytes(kv_val).decode("utf-8", "ignore")
                                    elif (
                                        kv_field_num == 2
                                        and kv_wire_type == 2
                                        and isinstance(kv_val, (bytes, bytearray))
                                    ):
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


def is_valid_subagent_id(cid: str) -> bool:
    """
    Validates a subagent conversation ID string.
    Rejects empty strings, strings shorter than 3 characters, pure integers,
    strings with underscores, and case-insensitive stop words.
    Accepts UUIDs and hyphenated identifiers with alphanumeric components.
    """
    if not cid or not isinstance(cid, str):
        return False
    clean = cid.strip().strip("\"'").strip()
    if len(clean) < 3:
        return False
    if clean.isdigit():
        return False
    if "_" in clean:
        return False
    if clean.lower() in SUBAGENT_STOP_WORDS:
        return False
    if _UUID_RE.match(clean):
        return True
    if _HYPHENATED_ID_RE.match(clean):
        return True
    return False


def is_subagent_dispatch_output(tool_name: str | None, content: str | None) -> bool:
    """
    Determines whether a tool output or generic step represents an invoke_subagent dispatch.
    When tool_name is known, strictly checks tool_name == 'invoke_subagent'.
    When tool_name is absent or empty, falls back to text patterns while rejecting
    file inspection and command execution outputs.
    """
    if tool_name:
        return tool_name == "invoke_subagent"
    if not content:
        return False
    return (
        ("Created the following subagent" in content or "Created the following subagents:" in content)
        and "Showing lines " not in content
        and "File Path:" not in content
        and "Command exited with code" not in content
    )


def is_manage_subagents_output(tool_name: str | None, content: str | None) -> bool:
    """
    Determines whether a tool output represents a manage_subagents query.
    When tool_name is known, strictly checks tool_name == 'manage_subagents'.
    When tool_name is absent or empty, falls back to text patterns while rejecting
    file inspection and command execution outputs.
    """
    if tool_name:
        return tool_name == "manage_subagents"
    if not content:
        return False
    c_lower = content.lower()
    return (
        ("active subagent" in c_lower or "active sub-agent" in c_lower)
        and "Showing lines " not in content
        and "File Path:" not in content
        and "Command exited with code" not in content
    )


def _clean_arg_str(val: Any) -> str:
    """Strips whitespace and unquotes double/single-encoded JSON strings."""
    if val is None:
        return ""
    s = str(val).strip()
    while True:
        if s.startswith('"') and s.endswith('"') and len(s) >= 2:
            try:
                unquoted = json.loads(s)
                if isinstance(unquoted, str):
                    s = unquoted.strip()
                    continue
            except Exception:
                pass
            inner = s[1:-1].strip()
            if not re.search(r'(?<!\\)"', inner):
                s = inner.replace(r"\"", '"').strip()
                continue
        if s.startswith("'") and s.endswith("'") and len(s) >= 2:
            inner = s[1:-1].strip()
            if not re.search(r"(?<!\\)'", inner):
                s = inner.replace(r"\'", "'").strip()
                continue
        break
    return s


def extract_subagent_specs(tc_args: dict[str, Any]) -> list[dict[str, str]]:
    """
    Extracts subagent specifications from invoke_subagent tool call arguments.
    Handles Antigravity 1.0 (Subagents list or JSON string) and Antigravity 2.0 flat arguments
    (Role, role, TypeName, type, Prompt, prompt, Model, model), cleaning inner quotes.
    """
    if not isinstance(tc_args, dict):
        return []

    sub_raw = tc_args.get("Subagents") or tc_args.get("subagents")
    if sub_raw:
        specs_list = []
        if isinstance(sub_raw, str):
            s = sub_raw.strip()
            if (s.startswith('"') and s.endswith('"')) or (
                s.startswith("'") and s.endswith("'")
            ):
                s = _clean_arg_str(s)
            try:
                parsed = json.loads(s)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                if isinstance(parsed, list):
                    specs_list = parsed
            except Exception:
                pass
        elif isinstance(sub_raw, list):
            specs_list = sub_raw

        if specs_list:
            results = []
            for item in specs_list:
                if isinstance(item, dict):
                    role = _clean_arg_str(item.get("Role") or item.get("role") or "")
                    sub_type = _clean_arg_str(
                        item.get("TypeName")
                        or item.get("type")
                        or item.get("subagent_type")
                        or ""
                    )
                    model = _clean_arg_str(item.get("Model") or item.get("model") or "")
                    results.append(
                        {
                            "role": role or "Autonomous Worker",
                            "type": sub_type or "self",
                            "model": model or "inherit",
                            "Role": role or "Autonomous Worker",
                            "TypeName": sub_type or "self",
                            "Model": model or "inherit",
                        }
                    )
            if results:
                return results

    flat_keys = (
        "Role",
        "role",
        "TypeName",
        "type",
        "subagent_type",
        "subagentType",
        "Prompt",
        "prompt",
        "Model",
        "model",
    )
    if any(k in tc_args for k in flat_keys):
        raw_role = tc_args.get("Role") or tc_args.get("role")
        raw_type = (
            tc_args.get("TypeName")
            or tc_args.get("type")
            or tc_args.get("subagent_type")
            or tc_args.get("subagentType")
        )
        raw_model = tc_args.get("Model") or tc_args.get("model")

        role = _clean_arg_str(raw_role)
        sub_type = _clean_arg_str(raw_type)
        model = _clean_arg_str(raw_model)

        return [
            {
                "role": role if role else "Autonomous Worker",
                "type": sub_type if sub_type else "self",
                "model": model if model else "inherit",
                "Role": role if role else "Autonomous Worker",
                "TypeName": sub_type if sub_type else "self",
                "Model": model if model else "inherit",
            }
        ]

    return []


def extract_invoked_subagent_ids(content: str) -> list[str]:
    """
    Extracts valid subagent conversation IDs from tool output or system messages.
    Supports JSON objects, key-value mappings (conversationId: <id>), and UUIDs.
    Filters all candidate IDs with is_valid_subagent_id and preserves order.
    """
    if not content or not isinstance(content, str):
        return []

    candidates: list[str] = []

    # 1. Look for JSON objects/arrays with conversationId / conversation_id / id
    for m in re.finditer(r"(\{.*?\}|\[.*?\])", content, re.DOTALL):
        snippet = m.group(1).strip()
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                for k in ("conversationId", "conversation_id", "id"):
                    val = parsed.get(k)
                    if isinstance(val, str):
                        candidates.append(val)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        for k in ("conversationId", "conversation_id", "id"):
                            val = item.get(k)
                            if isinstance(val, str):
                                candidates.append(val)
        except Exception:
            pass

    # 2. Key-value regex where key is followed by : or =
    kv_pattern = re.compile(
        r'\b(?:conversation(?:_?id)?|cid|id)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]+)',
        re.IGNORECASE,
    )
    for m in kv_pattern.finditer(content):
        candidates.append(m.group(1))

    # 3. Canonical UUIDs
    uuid_pattern = re.compile(
        r"\b([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b",
        re.IGNORECASE,
    )
    for m in uuid_pattern.finditer(content):
        candidates.append(m.group(1))

    # Filter through is_valid_subagent_id and deduplicate preserving order
    valid_ids: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        cand_clean = cand.strip().strip("\"'").strip()
        if is_valid_subagent_id(cand_clean) and cand_clean not in seen:
            seen.add(cand_clean)
            valid_ids.append(cand_clean)

    return valid_ids


def parse_manage_subagents_output(content: str) -> list[dict[str, str]]:
    """Parse manage_subagents tool output when active subagent(s): appears."""
    if not content or (
        "active subagent" not in content.lower()
        and "active sub-agent" not in content.lower()
    ):
        return []

    results: list[dict[str, str]] = []

    # 1. Try to find and parse JSON array
    json_match = re.search(r"\[\s*\{.*?\}\s*\]", content, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        cid = _clean_arg_str(
                            item.get("conversationId")
                            or item.get("conversation_id")
                            or item.get("id")
                            or ""
                        )
                        if cid and is_valid_subagent_id(cid):
                            role = _clean_arg_str(
                                item.get("role") or item.get("Role") or ""
                            )
                            sub_type = _clean_arg_str(
                                item.get("type") or item.get("TypeName") or ""
                            )
                            model = _clean_arg_str(
                                item.get("model") or item.get("Model") or ""
                            )
                            results.append(
                                {
                                    "id": cid,
                                    "conversationId": cid,
                                    "role": role or "Autonomous Worker",
                                    "type": sub_type or "self",
                                    "model": model or "inherit",
                                    "Role": role or "Autonomous Worker",
                                    "TypeName": sub_type or "self",
                                    "Model": model or "inherit",
                                }
                            )
        except Exception:
            pass

    if results:
        return results

    # 2. Line-by-line regex extraction for text or bullet points
    kv_cid = re.compile(
        r'\b(?:conversation(?:_?id)?|cid|id)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]+)',
        re.IGNORECASE,
    )
    uuid_cid = re.compile(
        r"\b([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b",
        re.IGNORECASE,
    )

    for line in content.splitlines():
        cid_m = kv_cid.search(line)
        if not cid_m:
            cid_m = uuid_cid.search(line)
        if cid_m:
            cid = cid_m.group(1).strip()
            if not is_valid_subagent_id(cid):
                continue
            role_m = re.search(r'\brole["\':\s=]+([^,\n\)]+)', line, re.IGNORECASE)
            type_m = re.search(
                r'\btype(?:Name)?["\':\s=]+([^,\n\)]+)', line, re.IGNORECASE
            )
            model_m = re.search(r'\bmodel["\':\s=]+([^,\n\)]+)', line, re.IGNORECASE)

            role = _clean_arg_str(role_m.group(1)) if role_m else ""
            sub_type = _clean_arg_str(type_m.group(1)) if type_m else ""
            model = _clean_arg_str(model_m.group(1)) if model_m else ""
            results.append(
                {
                    "id": cid,
                    "conversationId": cid,
                    "role": role or "Autonomous Worker",
                    "type": sub_type or "self",
                    "model": model or "inherit",
                    "Role": role or "Autonomous Worker",
                    "TypeName": sub_type or "self",
                    "Model": model or "inherit",
                }
            )

    return results


def apply_step_model_switch(
    switched_model: str,
    first_model: str | None,
    cur_model: str,
    steps: list[dict],
    models_used: list[str],
    model_transitions: list[str],
    *,
    backfill_default: bool = True,
) -> tuple[str, str]:
    """
    Applies a model switch to conversation state:
    - If first_model is None: sets first_model and cur_model to switched_model,
      replaces DEFAULT_MODEL steps with switched_model if backfill_default is True,
      and resets models_used and model_transitions to [switched_model].
    - If first_model is already set: updates cur_model to switched_model,
      appends to model_transitions (if different from the last entry),
      and appends to models_used (if not already present).
    Returns (new_first_model, new_cur_model).
    """
    if not switched_model:
        return first_model or cur_model, cur_model

    if first_model is None:
        first_model = switched_model
        if backfill_default:
            for s in steps:
                if s.get("model_name") == DEFAULT_MODEL:
                    s["model_name"] = first_model
        cur_model = switched_model
        models_used.clear()
        models_used.append(cur_model)
        model_transitions.clear()
        model_transitions.append(cur_model)
    else:
        cur_model = switched_model
        if not model_transitions or model_transitions[-1] != cur_model:
            model_transitions.append(cur_model)
        if cur_model not in models_used:
            models_used.append(cur_model)

    return first_model, cur_model


def apply_native_step_models_fallback(
    native_step_models: dict[int, str],
    steps: list[dict],
) -> tuple[str, str, list[str], list[str]]:
    """
    Applies ground-truth native step models from gen_metadata SQLite to steps.
    Sets s["model_name"] for each step, and returns:
    (first_model, cur_model, models_used, model_transitions).
    """
    if not native_step_models:
        return "", "", [], []

    sorted_native = sorted(native_step_models.items(), key=lambda x: x[0])
    first_native = sorted_native[0][1]
    cur_model = sorted_native[-1][1]

    models_used: list[str] = []
    for _, m in sorted_native:
        if m not in models_used:
            models_used.append(m)

    model_transitions: list[str] = []
    for _, m in sorted_native:
        if not model_transitions or model_transitions[-1] != m:
            model_transitions.append(m)

    for s in steps:
        s_idx = s.get("step_index", 0)
        if s_idx in native_step_models:
            s["model_name"] = native_step_models[s_idx]
        else:
            preceding = [m for idx, m in sorted_native if idx <= s_idx]
            s["model_name"] = preceding[-1] if preceding else first_native

    return first_native, cur_model, models_used, model_transitions


def upsert_dispatched_subagent_stubs(
    conn: sqlite3.Connection,
    parent_id: str,
    root_id: str,
    parent_depth: int,
    parent_title: str,
    parent_project: str | None,
    parent_model: str,
    dispatched_subagents: list[dict],
    *,
    conversations_dir: Path | None = None,
    resolve_native: bool = False,
) -> None:
    """
    Upserts child stub records for dispatched subagents and purges orphan 0-step stubs.
    Preserves child model_name and models_used if total_steps > 0.
    """
    # 1. Purge stale / false-positive 0-step stubs for this parent
    valid_child_ids = [
        ch["id"]
        for ch in dispatched_subagents
        if isinstance(ch, dict) and ch.get("id") and is_valid_subagent_id(ch["id"])
    ]
    if valid_child_ids:
        placeholders = ",".join("?" for _ in valid_child_ids)
        conn.execute(
            f"DELETE FROM conversations WHERE parent_id = ? AND (total_steps IS NULL OR total_steps = 0) AND id NOT IN ({placeholders})",
            [parent_id, *valid_child_ids],
        )
    else:
        conn.execute(
            "DELETE FROM conversations WHERE parent_id = ? AND (total_steps IS NULL OR total_steps = 0)",
            (parent_id,),
        )

    # 2. Upsert valid dispatched subagents
    for child in dispatched_subagents:
        ch_id = child.get("id")
        if not ch_id or not is_valid_subagent_id(ch_id):
            continue

        ch_role = child.get("role") or "Autonomous Worker"
        ch_type = child.get("type") or "self"
        ch_step = child.get("step_index")

        if resolve_native:
            native_ch_models = extract_native_step_models(
                ch_id, conversations_dir=conversations_dir
            )
            if native_ch_models:
                ch_model = sorted(native_ch_models.items(), key=lambda x: x[0])[-1][1]
            else:
                ch_model = resolve_subagent_model(
                    child.get("model"), parent_model=parent_model
                )
        else:
            ch_model = resolve_subagent_model(
                child.get("model"), parent_model=parent_model
            )

        clean_t = parent_title or ""
        m_sub = re.match(r"^\[.+?\]\s*(.+)", clean_t)
        if m_sub:
            clean_t = m_sub.group(1)
        ch_title = f"[{ch_role}] {clean_t[:90]}"

        conn.execute(
            """
            INSERT INTO conversations (
                id, title, project, parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index, active_pairing_seconds,
                model_name, models_used
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id = excluded.parent_id,
                root_id = excluded.root_id,
                depth = excluded.depth,
                active_pairing_seconds = 0.0,
                subagent_role = CASE
                    WHEN excluded.subagent_role IS NOT NULL AND excluded.subagent_role != 'Autonomous Worker' THEN excluded.subagent_role
                    ELSE COALESCE(conversations.subagent_role, excluded.subagent_role)
                END,
                title = CASE
                    WHEN (conversations.title LIKE '[Autonomous Worker]%' OR conversations.title IS NULL OR conversations.title = '')
                         AND excluded.title NOT LIKE '[Autonomous Worker]%' THEN excluded.title
                    ELSE conversations.title
                END,
                subagent_type = COALESCE(excluded.subagent_type, conversations.subagent_type),
                invoking_step_index = COALESCE(excluded.invoking_step_index, conversations.invoking_step_index),
                project = COALESCE(conversations.project, excluded.project),
                model_name = CASE
                    WHEN conversations.total_steps IS NULL OR conversations.total_steps = 0 OR conversations.model_name IS NULL OR conversations.model_name = '' THEN excluded.model_name
                    ELSE conversations.model_name
                END,
                models_used = CASE
                    WHEN conversations.total_steps IS NULL OR conversations.total_steps = 0 OR conversations.models_used IS NULL OR conversations.models_used = '' THEN excluded.models_used
                    ELSE conversations.models_used
                END
            """,
            (
                ch_id,
                ch_title,
                parent_project,
                parent_id,
                root_id,
                parent_depth + 1,
                ch_role,
                ch_type,
                ch_step,
                ch_model,
                ch_model,
            ),
        )
