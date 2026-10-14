"""
Turn-to-Turn Context Diff Engine
---------------------------------
Calculates set differences (added, removed, common) between conversation turns
using content block hashing, tracks token deltas, and detects context compaction
and truncation boundaries where older history blocks are pruned.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from context_monitor.formats import ContentBlock

__all__ = ["DiffResult", "diff_block_lists", "get_turn_diff_from_db", "normalize_block"]

CHARS_PER_TOKEN = 3.8


@dataclass
class DiffResult:
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    common: list[dict[str, Any]] = field(default_factory=list)
    added_tokens: int = 0
    removed_tokens: int = 0
    common_tokens: int = 0
    net_token_delta: int = 0
    compaction_detected: bool = False
    pruned_step_indices: list[int] = field(default_factory=list)
    pruned_kinds: dict[str, int] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(content: str) -> int:
    """Estimates tokens for a string using standard 3.8 chars/token ratio."""
    if not content:
        return 0
    return max(1, int(len(content) / CHARS_PER_TOKEN))


def normalize_block(b: Any) -> dict[str, Any]:
    """Normalizes ContentBlock, dict, or sqlite3.Row into a consistent dict representation."""
    if hasattr(b, "hash"):  # ContentBlock
        kind_val = b.kind.value if hasattr(b.kind, "value") else str(b.kind)
        content_val = b.text
        if b.arguments is not None:
            content_val += "\n" + json.dumps(b.arguments, sort_keys=True)
        tokens = estimate_tokens(content_val)
        return {
            "kind": kind_val,
            "block_type": kind_val,
            "text": b.text,
            "content": content_val,
            "step_index": b.step_index,
            "call_id": b.call_id,
            "name": b.name,
            "arguments": b.arguments,
            "hash": b.hash,
            "block_hash": b.hash,
            "est_tokens": tokens,
        }
    elif isinstance(b, (dict, sqlite3.Row)):
        d = dict(b)
        kind_val = d.get("block_type") or d.get("kind") or "generic"
        content_val = d.get("content") or d.get("text") or ""
        hash_val = d.get("block_hash") or d.get("hash")
        if not hash_val:
            hash_val = hashlib.sha1(
                f"{kind_val}:{content_val}".encode("utf-8")
            ).hexdigest()
        tokens = d.get("est_tokens")
        if tokens is None:
            tokens = estimate_tokens(content_val)
        return {
            "kind": kind_val,
            "block_type": kind_val,
            "text": d.get("text", content_val),
            "content": content_val,
            "step_index": d.get("step_index", 0),
            "call_id": d.get("call_id"),
            "name": d.get("name"),
            "arguments": d.get("arguments"),
            "hash": hash_val,
            "block_hash": hash_val,
            "est_tokens": tokens,
        }
    else:
        content_val = str(b)
        hash_val = hashlib.sha1(f"generic:{content_val}".encode("utf-8")).hexdigest()
        tokens = estimate_tokens(content_val)
        return {
            "kind": "generic",
            "block_type": "generic",
            "text": content_val,
            "content": content_val,
            "step_index": 0,
            "call_id": None,
            "name": None,
            "arguments": None,
            "hash": hash_val,
            "block_hash": hash_val,
            "est_tokens": tokens,
        }


def diff_block_lists(
    list_a: Sequence[ContentBlock | dict[str, Any] | sqlite3.Row],
    list_b: Sequence[ContentBlock | dict[str, Any] | sqlite3.Row],
) -> DiffResult:
    """
    Computes set differences (added, removed, common) between block list A and block list B.

    - added: blocks present in B but not in A
    - removed: blocks present in A but not in B
    - common: blocks present in both A and B
    """
    norm_a = [normalize_block(x) for x in list_a]
    norm_b = [normalize_block(x) for x in list_b]

    a_keys = {x["hash"] for x in norm_a}
    b_keys = {x["hash"] for x in norm_b}

    added = [x for x in norm_b if x["hash"] not in a_keys]
    removed = [x for x in norm_a if x["hash"] not in b_keys]
    common = [x for x in norm_b if x["hash"] in a_keys]

    added_tokens = sum(x["est_tokens"] for x in added)
    removed_tokens = sum(x["est_tokens"] for x in removed)
    common_tokens = sum(x["est_tokens"] for x in common)
    net_token_delta = added_tokens - removed_tokens

    compaction_detected = len(removed) > 0
    pruned_step_indices = (
        sorted(list({x["step_index"] for x in removed})) if removed else []
    )
    pruned_kinds: dict[str, int] = {}
    for x in removed:
        k = x["kind"]
        pruned_kinds[k] = pruned_kinds.get(k, 0) + 1

    summary_parts = []
    if added:
        summary_parts.append(f"+{len(added)} blocks (~{added_tokens:,} tok)")
    if removed:
        summary_parts.append(f"-{len(removed)} pruned (~{removed_tokens:,} tok)")
    if not summary_parts:
        summary = "No change"
    else:
        summary = ", ".join(summary_parts)

    return DiffResult(
        added=added,
        removed=removed,
        common=common,
        added_tokens=added_tokens,
        removed_tokens=removed_tokens,
        common_tokens=common_tokens,
        net_token_delta=net_token_delta,
        compaction_detected=compaction_detected,
        pruned_step_indices=pruned_step_indices,
        pruned_kinds=pruned_kinds,
        summary=summary,
    )


def get_turn_diff_from_db(
    conn: sqlite3.Connection,
    conversation_id: str,
    step_a: int,
    step_b: int,
    cumulative: bool = True,
) -> dict[str, Any]:
    """
    Fetches transcript blocks and step metadata from the database for turn A and turn B,
    computes the diff, and returns a structured response dictionary.
    """
    cur = conn.cursor()

    # Step metadata
    step_a_row = cur.execute(
        """
        SELECT step_index, created_at, source, activity, tool_or_source, est_tokens, phase, model_name, detail
        FROM steps
        WHERE conversation_id = ? AND step_index = ?
    """,
        (conversation_id, step_a),
    ).fetchone()

    step_b_row = cur.execute(
        """
        SELECT step_index, created_at, source, activity, tool_or_source, est_tokens, phase, model_name, detail
        FROM steps
        WHERE conversation_id = ? AND step_index = ?
    """,
        (conversation_id, step_b),
    ).fetchone()

    # Query blocks
    if cumulative:
        blocks_a_rows = cur.execute(
            """
            SELECT step_index, block_type, content, block_hash, call_id
            FROM transcript_blocks
            WHERE conversation_id = ? AND step_index <= ?
            ORDER BY step_index ASC, id ASC
        """,
            (conversation_id, step_a),
        ).fetchall()

        blocks_b_rows = cur.execute(
            """
            SELECT step_index, block_type, content, block_hash, call_id
            FROM transcript_blocks
            WHERE conversation_id = ? AND step_index <= ?
            ORDER BY step_index ASC, id ASC
        """,
            (conversation_id, step_b),
        ).fetchall()
    else:
        blocks_a_rows = cur.execute(
            """
            SELECT step_index, block_type, content, block_hash, call_id
            FROM transcript_blocks
            WHERE conversation_id = ? AND step_index = ?
            ORDER BY id ASC
        """,
            (conversation_id, step_a),
        ).fetchall()

        blocks_b_rows = cur.execute(
            """
            SELECT step_index, block_type, content, block_hash, call_id
            FROM transcript_blocks
            WHERE conversation_id = ? AND step_index = ?
            ORDER BY id ASC
        """,
            (conversation_id, step_b),
        ).fetchall()

    diff_res = diff_block_lists(blocks_a_rows, blocks_b_rows)

    return {
        "conversation_id": conversation_id,
        "step_a": dict(step_a_row) if step_a_row else {"step_index": step_a},
        "step_b": dict(step_b_row) if step_b_row else {"step_index": step_b},
        "cumulative": cumulative,
        "added": diff_res.added,
        "removed": diff_res.removed,
        "common": diff_res.common,
        "stats": {
            "added_count": len(diff_res.added),
            "removed_count": len(diff_res.removed),
            "common_count": len(diff_res.common),
            "added_tokens": diff_res.added_tokens,
            "removed_tokens": diff_res.removed_tokens,
            "common_tokens": diff_res.common_tokens,
            "net_token_delta": diff_res.net_token_delta,
            "compaction_detected": diff_res.compaction_detected,
            "pruned_step_indices": diff_res.pruned_step_indices,
            "pruned_kinds": diff_res.pruned_kinds,
            "summary": diff_res.summary,
        },
    }
