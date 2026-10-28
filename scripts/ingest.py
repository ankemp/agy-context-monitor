#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Antigravity Context Monitor & Intelligence Engine
-------------------------------------------------
Lifecycle hook script triggered via PostInvocation.
Performs lightweight, zero-overhead transcript analysis:
1. Updates offline clean artifact ~/.gemini/antigravity/brain/<id>/context_usage.md
2. Syncs structured session, efficiency, MCP, and velocity metrics to centralized SQLite DB (~/.gemini/antigravity/analytics.db)
3. Returns empty JSON {} to keep conversation context 100% clean.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

_root = Path(__file__).resolve().parent.parent
if str(_root / "src") not in sys.path:
    sys.path.insert(0, str(_root / "src"))

from context_monitor.db import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_db_connection,
    refresh_conversation_rollups,
)
from context_monitor.diff import DiffResult, diff_block_lists  # noqa: E402
from context_monitor.formats import BlockParser  # noqa: E402
from context_monitor.html_report import generate_html_report  # noqa: E402
from context_monitor.pricing import (  # noqa: E402
    DEFAULT_MODEL,
    calculate_step_cost,
    extract_model_switch,
    extract_native_step_models,
    get_model_context_limit,
    resolve_subagent_model,
)
from context_monitor.project import (  # noqa: E402
    extract_project_from_text,
    extract_project_from_tool_args,
    normalize_project_name,
)
from context_monitor.quota import (  # noqa: E402
    calculate_quota_metrics,
    format_quota_pct,
    format_reset_countdown,
    get_hourly_activity_profile,
    sync_and_get_quota_state,
)
from context_monitor.settings import should_generate_artifacts  # noqa: E402
from context_monitor.timings import compute_session_timings  # noqa: E402
from context_monitor.velocity import (  # noqa: E402
    compute_active_velocity,
    safe_float_metric,
)

CHARS_PER_TOKEN = 3.8


def format_tokens(n: int | float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


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
    """Extracts subagent specifications from invoke_subagent tool call arguments.

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
                        if cid:
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
    for line in content.splitlines():
        cid_m = re.search(
            r'\b(?:conversation_?id|cid|id)["\':\s]+([a-zA-Z0-9_\-]+)',
            line,
            re.IGNORECASE,
        )
        if not cid_m:
            cid_m = re.search(
                r"\b([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b",
                line,
                re.IGNORECASE,
            )
        if cid_m:
            cid = cid_m.group(1).strip()
            role_m = re.search(r'\brole["\':\s]+([^,\n\)]+)', line, re.IGNORECASE)
            type_m = re.search(
                r'\btype(?:Name)?["\':\s]+([^,\n\)]+)', line, re.IGNORECASE
            )
            model_m = re.search(r'\bmodel["\':\s]+([^,\n\)]+)', line, re.IGNORECASE)

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


def format_ephemeral_status(
    active_pct: float,
    active_tokens: int | float,
    active_max: int | float,
    added_tok: int | float = 0,
    turn_cost: float = 0.0,
    quota_metrics: dict | None = None,
    *,
    session_cost: float | None = None,
    turn_wk_pct: float | None = None,
    diff_res: Any = None,
    fleet_cost: float | None = None,
    fleet_wk_pct: float | None = None,
    fleet_agent_count: int = 1,
) -> str:
    """Formats transient progress / status line for Antigravity 2.0 ephemeral display.

    Invariant: Zero context pollution - ephemeral messages only, never written to
    transcript.jsonl.
    """
    if diff_res and getattr(diff_res, "compaction_detected", False):
        pruned_steps = getattr(diff_res, "pruned_step_indices", [])
        p_min = min(pruned_steps) if pruned_steps else 0
        p_max = max(pruned_steps) if pruned_steps else 0
        rem_toks = getattr(diff_res, "removed_tokens", 0)
        return (
            f"Context Compaction Detected: Pruned steps #{p_min}–#{p_max} "
            f"(~{format_tokens(rem_toks)} tokens freed across {len(diff_res.removed)} blocks). "
            f"Active window: {active_pct:.1f}% ({format_tokens(active_tokens)}/{format_tokens(active_max)})."
        )

    if active_pct >= 80.0:
        return (
            f"Context Warning: Window at {active_pct:.1f}% capacity "
            f"({format_tokens(active_tokens)} / {format_tokens(active_max)} tokens). "
            f"Consider summarizing or resetting conversation."
        )

    delta_str = f" · +{format_tokens(added_tok)} tok this turn" if added_tok > 0 else ""
    ctx_prefix = (
        f"Context: {active_pct:.1f}% ({format_tokens(active_tokens)} / "
        f"{format_tokens(active_max)} tokens){delta_str}"
    )

    effective_cost = session_cost if session_cost is not None else turn_cost

    has_valid_quota = False
    if quota_metrics and isinstance(quota_metrics, dict):
        has_rem = quota_metrics.get("weekly_remaining_pct") is not None
        has_reset = bool(
            quota_metrics.get("weekly_reset_countdown")
            or quota_metrics.get("weekly_reset_time")
        )
        if has_rem and has_reset:
            has_valid_quota = True

    if not has_valid_quota:
        if fleet_agent_count > 1 and fleet_cost is not None:
            return (
                f"{ctx_prefix} · Fleet ({fleet_agent_count}) Value: ${fleet_cost:,.2f}"
            )
        return f"{ctx_prefix} · Session Value: ${effective_cost:,.2f}"

    sess_wk_pct = quota_metrics.get("session_weekly_pct")
    if sess_wk_pct is None:
        sess_wk_pct = quota_metrics.get(
            "thread_weekly_pct", quota_metrics.get("weekly_pct", 0.0)
        )

    if fleet_agent_count > 1 and fleet_cost is not None:
        if fleet_wk_pct is not None:
            eff_fleet_wk_pct = fleet_wk_pct
        elif effective_cost and effective_cost > 0 and sess_wk_pct is not None:
            eff_fleet_wk_pct = (fleet_cost / effective_cost) * float(sess_wk_pct)
        else:
            eff_fleet_wk_pct = sess_wk_pct or 0.0
        sess_part = f"Fleet ({fleet_agent_count}): {format_quota_pct(eff_fleet_wk_pct, 2)} wk (${fleet_cost:,.2f})"
    else:
        sess_part = (
            f"Session: {format_quota_pct(sess_wk_pct, 2)} wk (${effective_cost:,.2f})"
        )

    rem_wk_pct = quota_metrics.get("weekly_remaining_pct", 100.0)
    reset_cd = quota_metrics.get("weekly_reset_countdown") or format_reset_countdown(
        str(quota_metrics.get("weekly_reset_time") or "")
    )
    reset_str = f" ({reset_cd})" if reset_cd else ""
    quota_part = f"Quota: {format_quota_pct(rem_wk_pct, 1)} wk{reset_str}"

    parts = [ctx_prefix, sess_part]
    if turn_wk_pct is not None and round(turn_wk_pct, 2) > 0:
        parts.append(f"+{format_quota_pct(turn_wk_pct, 2)} turn")
    parts.append(quota_part)

    rem_5h_pct = quota_metrics.get("five_hour_remaining_pct")
    if rem_5h_pct is not None and rem_5h_pct < 25.0:
        cd_5h = quota_metrics.get(
            "five_hour_reset_countdown"
        ) or format_reset_countdown(
            str(quota_metrics.get("five_hour_reset_time") or "")
        )
        cd_5h_str = f" ({cd_5h})" if cd_5h else ""
        parts.append(f"5h Limit: {format_quota_pct(rem_5h_pct, 1)}{cd_5h_str}")

    return " · ".join(parts)


def detect_model(transcript_path: Path) -> tuple[str, str, int]:
    first_model = DEFAULT_MODEL
    latest_model = DEFAULT_MODEL
    found_first = False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                except Exception:
                    continue
                if step.get("type") == "USER_INPUT":
                    content = step.get("content", "")
                    switched_model = extract_model_switch(content)
                    if switched_model:
                        if not found_first:
                            first_model = switched_model
                            found_first = True
                        latest_model = switched_model
    except Exception:
        pass

    max_tokens = get_model_context_limit(latest_model)
    return first_model, latest_model, max_tokens


def extract_file_basename(content: str) -> str:
    m = re.search(r"File Path:\s*`?(?:file://)?([^`\r\n]+)`?", content)
    if m:
        path_str = m.group(1).strip()
        return Path(path_str).name
    return "File view"


def extract_system_message_detail(content: str) -> str:
    m_task = re.search(r"sender=([^\s]+)", content)
    m_code = re.search(r"exited with code (\d+)", content)
    sender = m_task.group(1).split("/")[-1] if m_task else "Task"
    code = f"(exit {m_code.group(1)})" if m_code else ""
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
        and not line.strip().startswith("<")
        and not line.strip().startswith("The following")
        and not line.strip().startswith("[Message]")
    ]
    snippet = lines[0][:40] if lines else ""
    return f"{sender} {code} {snippet}".strip()


def classify_phase(step_type: str, tool_name: str) -> str:
    s_upper = step_type.upper()
    t_lower = tool_name.lower() if tool_name else ""

    if t_lower in (
        "view_file",
        "grep_search",
        "find_by_name",
        "list_dir",
        "search_web",
        "read_url_content",
        "read_resource",
        "list_resources",
    ) or s_upper in (
        "VIEW_FILE",
        "GREP_SEARCH",
        "FIND",
        "LIST_DIRECTORY",
        "SEARCH_WEB",
        "READ_URL_CONTENT",
    ):
        return "discovery"

    if t_lower in (
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "generate_image",
        "create_or_update_file",
        "push_files",
    ) or s_upper in ("CODE_ACTION", "GENERATE_IMAGE"):
        return "execution"

    if (
        t_lower
        in (
            "run_command",
            "manage_task",
            "command_status",
            "invoke_subagent",
            "send_message",
            "manage_subagents",
        )
        or t_lower.startswith("mcp")
        or s_upper in ("RUN_COMMAND", "INVOKE_SUBAGENT", "SYSTEM_MESSAGE")
    ):
        return "verification"

    return "reasoning"


def analyze_transcript(
    transcript_path: Path | str,
    initial_model: str | None = None,
    *,
    include_tool_schemas: bool = False,
    cid: str | None = None,
    conversations_dir: Path | None = None,
) -> dict:
    transcript_path = Path(transcript_path)
    if cid is None:
        p = transcript_path.resolve()
        if (
            p.name in ("transcript.jsonl", "transcript_full.jsonl")
            and p.parent.name == "logs"
            and len(p.parents) >= 3
            and p.parents[1].name == ".system_generated"
        ):
            cid = p.parents[2].name
        elif (
            p.name in ("transcript.jsonl", "transcript_full.jsonl")
            and p.parent.name == "logs"
            and len(p.parents) >= 2
        ):
            cid = p.parents[1].name
        elif "brain" in p.parts:
            b_idx = p.parts.index("brain")
            if b_idx + 1 < len(p.parts):
                cid = p.parts[b_idx + 1]
        elif p.is_dir():
            cid = p.name
        elif p.name in ("transcript.jsonl", "transcript_full.jsonl"):
            cid = p.parent.name
        else:
            cid = p.stem

    if cid and cid.endswith(".db"):
        cid = cid[:-3]

    if transcript_path.is_dir():
        t_file = transcript_path / ".system_generated" / "logs" / "transcript.jsonl"
        if not t_file.exists():
            t_file = transcript_path / "transcript.jsonl"
        transcript_path = t_file

    native_step_models = (
        extract_native_step_models(cid, conversations_dir=conversations_dir)
        if cid
        else {}
    )

    steps = []
    category_stats = {}
    total_chars = 0
    total_thinking_tokens = 0
    user_prompts = 0
    redundant_reads = 0
    lines_written = 0
    lines_edited = 0
    failed_tool_calls = 0
    seen_files = set()
    pending_tool_calls = []
    tool_execs = []
    parsed_blocks = []
    block_parser = BlockParser()
    title = ""
    start_time = None
    end_time = None
    first_model = initial_model
    if initial_model is None:
        cur_model = DEFAULT_MODEL
        models_used = []
        model_transitions = []
    else:
        cur_model = initial_model
        models_used = [cur_model]
        model_transitions = [cur_model]
    project = None
    detected_parent_id = None
    dispatched_subagents = []
    pending_subagent_specs = []

    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue

            step_idx = step.get("step_index", len(steps) + 1)
            parsed_blocks.extend(block_parser.parse_step(step, step_idx))

            source = step.get("source", "UNKNOWN")
            step_type = step.get("type", "UNKNOWN")
            created_at = step.get("created_at", "")
            content = step.get("content", "")
            thinking = step.get("thinking", "")
            tool_calls = step.get("tool_calls", [])
            exit_code = step.get("exit_code")
            is_error = (
                1
                if (
                    step.get("error")
                    or step.get("status") == "ERROR"
                    or (exit_code is not None and exit_code != 0)
                )
                else 0
            )

            # Detect mid-session model switch
            if step_type == "USER_INPUT":
                switched_model = extract_model_switch(content)
                if switched_model:
                    if first_model is None:
                        first_model = switched_model
                        for s in steps:
                            if s.get("model_name") == DEFAULT_MODEL:
                                s["model_name"] = first_model
                        cur_model = switched_model
                        models_used = [cur_model]
                        model_transitions = [cur_model]
                    else:
                        cur_model = switched_model
                        if not model_transitions or model_transitions[-1] != cur_model:
                            model_transitions.append(cur_model)
                        if cur_model not in models_used:
                            models_used.append(cur_model)

            if step_idx in native_step_models:
                native_m = native_step_models[step_idx]
                if first_model is None:
                    first_model = native_m
                    for s in steps:
                        if s.get("model_name") == DEFAULT_MODEL:
                            s["model_name"] = first_model
                    cur_model = native_m
                    models_used = [cur_model]
                    model_transitions = [cur_model]
                elif native_m != cur_model:
                    cur_model = native_m
                    if not model_transitions or model_transitions[-1] != cur_model:
                        model_transitions.append(cur_model)
                    if cur_model not in models_used:
                        models_used.append(cur_model)

            if not project and content:
                project = extract_project_from_text(content)

            if is_error:
                failed_tool_calls += 1

            if created_at:
                if start_time is None:
                    start_time = created_at
                end_time = created_at

            content_len = len(content)
            thinking_len = len(thinking)
            tool_calls_len = len(json.dumps(tool_calls)) if tool_calls else 0
            step_chars = content_len + thinking_len + tool_calls_len
            est_tokens = (
                step.get("est_tokens")
                if step.get("est_tokens") is not None
                else int(step_chars / CHARS_PER_TOKEN)
            )
            step_thinking_tokens = (
                int(thinking_len / CHARS_PER_TOKEN) if thinking_len else 0
            )
            total_chars += step_chars
            total_thinking_tokens += step_thinking_tokens

            activity = step_type
            tool_or_source = source
            category = step_type
            detail = ""
            primary_tool_name = ""

            if step_type == "PLANNER_RESPONSE":
                activity = "Agent Reasoning & Text"
                tool_or_source = "Agent"
                category = "Agent Reasoning & Text"
                if tool_calls:
                    pending_tool_calls = [
                        (tc.get("name", ""), tc.get("args", {}))
                        for tc in tool_calls
                        if isinstance(tc, dict)
                    ]
                    tool_names = [tc[0] for tc in pending_tool_calls]
                    primary_tool_name = tool_names[0] if tool_names else ""
                    detail = f"Tools called: {', '.join(tool_names)}"
                    for tc_name, tc_args in pending_tool_calls:
                        target_p = (
                            tc_args.get("Path")
                            or tc_args.get("path")
                            or tc_args.get("TargetFile")
                            or tc_args.get("target_file")
                            or tc_args.get("AbsolutePath")
                            or tc_args.get("SearchPath")
                            or tc_args.get("DirectoryPath")
                            or ""
                        )
                        proj_path = (
                            target_p or tc_args.get("Cwd") or tc_args.get("cwd") or ""
                        )
                        if not proj_path and tc_args.get("CommandLine"):
                            proj_path = tc_args.get("CommandLine")
                        if not project:
                            project = extract_project_from_tool_args(tc_args)
                        clean_path = str(target_p).strip("\"'") if target_p else None

                        if tc_name == "invoke_subagent":
                            specs = extract_subagent_specs(tc_args)
                            if not specs:
                                specs = [
                                    {
                                        "role": "Autonomous Worker",
                                        "type": "self",
                                        "model": "inherit",
                                        "Role": "Autonomous Worker",
                                        "TypeName": "self",
                                        "Model": "inherit",
                                    }
                                ]
                            pending_subagent_specs.extend(specs)
                        elif tc_name == "view_file" and clean_path:
                            norm_path = clean_path.lower()
                            if norm_path in seen_files:
                                redundant_reads += 1
                            else:
                                seen_files.add(norm_path)
                        elif tc_name == "write_to_file":
                            code = tc_args.get("CodeContent", "")
                            lines_written += len(code.splitlines())
                        elif tc_name in (
                            "replace_file_content",
                            "multi_replace_file_content",
                        ):
                            rep = tc_args.get("ReplacementContent", "")
                            lines_edited += len(rep.splitlines())

                        mcp_server = None
                        mcp_tool = None
                        stored_tool_name = tc_name

                        if tc_name == "call_mcp_tool":
                            server = str(tc_args.get("ServerName", "")).strip("\"'")
                            t_name = str(tc_args.get("ToolName", "")).strip("\"'")
                            if server and t_name:
                                mcp_server = server
                                mcp_tool = t_name
                                stored_tool_name = f"mcp:{server}:{t_name}"
                            elif server:
                                mcp_server = server
                        elif tc_name.startswith("mcp_"):
                            parts = tc_name.split("_", 2)
                            mcp_server = parts[1] if len(parts) > 1 else "mcp"
                            mcp_tool = parts[2] if len(parts) > 2 else tc_name
                            stored_tool_name = tc_name

                        tool_execs.append(
                            {
                                "step_index": step_idx,
                                "tool_name": stored_tool_name,
                                "mcp_server": mcp_server,
                                "mcp_tool": mcp_tool,
                                "target_path": clean_path,
                                "command_line": tc_args.get("CommandLine"),
                                "exit_code": exit_code,
                                "is_error": is_error,
                            }
                        )
                else:
                    pending_tool_calls = []
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")

            elif step_type == "USER_INPUT":
                user_prompts += 1
                activity = "User Request"
                tool_or_source = "User"
                category = "User Requests"
                pending_tool_calls = []
                clean = re.sub(r"\s+", " ", content).strip()
                if "<USER_REQUEST>" in clean and "</USER_REQUEST>" in clean:
                    s = clean.find("<USER_REQUEST>") + len("<USER_REQUEST>")
                    e = clean.find("</USER_REQUEST>")
                    clean = clean[s:e].strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")
                if not title and clean:
                    title = clean[:120]

            elif step_type == "CHECKPOINT":
                activity = "Session Summary"
                tool_or_source = "System"
                category = "Session Summaries (Checkpoint)"
                pending_tool_calls = []
                detail = "Context truncation summary"

            elif step_type == "SYSTEM_MESSAGE":
                activity = "Background Task Output"
                tool_or_source = "System Task"
                category = "System: Background Task Outputs"
                pending_tool_calls = []
                detail = extract_system_message_detail(content)
                if step_idx <= 2 and not detected_parent_id:
                    m_snd = re.search(
                        r"\[Message\]\s+timestamp=[^\s]+\s+sender=([a-zA-Z0-9_\-]+)",
                        content,
                    )
                    if m_snd:
                        detected_parent_id = m_snd.group(1)

            elif step_type == "EPHEMERAL_MESSAGE":
                activity = "System Ephemeral Status"
                tool_or_source = "Hook / System"
                category = "System: Ephemeral Injections"
                pending_tool_calls = []
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")

            elif step_type == "GENERIC":
                tool_info = (
                    pending_tool_calls.pop(0) if pending_tool_calls else ("", {})
                )
                tool_name = tool_info[0]
                tool_args = tool_info[1]
                primary_tool_name = tool_name

                if (
                    tool_name == "invoke_subagent"
                    or "Created the following subagent" in content
                    or "Created the following subagents:" in content
                ) and tool_name not in (
                    "run_command",
                    "view_file",
                    "search_web",
                    "manage_task",
                ):
                    activity = "Subagent Dispatched"
                    tool_or_source = "`invoke_subagent`"
                    category = "Tool Output: `invoke_subagent`"
                    found_cids = re.findall(
                        r'conversation(?:_?id)?["\':\s]+([a-zA-Z0-9_\-]+)',
                        content,
                        re.IGNORECASE,
                    )
                    if not found_cids:
                        found_cids = re.findall(
                            r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
                            content,
                        )
                    found_cids = list(dict.fromkeys(found_cids))
                    if not found_cids and is_error and pending_subagent_specs:
                        pending_subagent_specs.pop(0)

                    for ch_id in found_cids:
                        spec = (
                            pending_subagent_specs.pop(0)
                            if pending_subagent_specs
                            else {}
                        )
                        dispatched_subagents.append(
                            {
                                "id": ch_id,
                                "role": spec.get("role")
                                or spec.get("Role")
                                or "Autonomous Worker",
                                "type": spec.get("type")
                                or spec.get("TypeName")
                                or "self",
                                "model": spec.get("model")
                                or spec.get("Model")
                                or "inherit",
                                "step_index": step_idx,
                            }
                        )
                    detail = (
                        f"Dispatched {len(found_cids)} subagent(s)"
                        if found_cids
                        else "Subagent dispatch"
                    )
                elif (
                    tool_name == "manage_subagents"
                    or "active subagent" in content.lower()
                    or "active sub-agent" in content.lower()
                ) and tool_name not in (
                    "run_command",
                    "view_file",
                    "search_web",
                    "manage_task",
                ):
                    activity = "Subagent Status"
                    tool_or_source = "`manage_subagents`"
                    category = "Tool Output: `manage_subagents`"
                    parsed_agents = parse_manage_subagents_output(content)
                    for pa in parsed_agents:
                        cid_found = pa.get("id") or pa.get("conversationId")
                        if not cid_found:
                            continue
                        matched = False
                        for ds in dispatched_subagents:
                            if ds.get("id") == cid_found:
                                if pa.get("role") and (
                                    ds.get("role") in (None, "", "Autonomous Worker")
                                    or pa.get("role") != "Autonomous Worker"
                                ):
                                    ds["role"] = pa["role"]
                                if pa.get("type"):
                                    ds["type"] = pa["type"]
                                if pa.get("model"):
                                    ds["model"] = pa["model"]
                                matched = True
                                break
                        if not matched:
                            dispatched_subagents.append(
                                {
                                    "id": cid_found,
                                    "role": pa.get("role", "Autonomous Worker"),
                                    "type": pa.get("type", "self"),
                                    "model": pa.get("model", "inherit"),
                                    "step_index": step_idx,
                                }
                            )
                    detail = (
                        f"Active subagent(s): {len(parsed_agents)}"
                        if parsed_agents
                        else "Manage subagents"
                    )
                elif tool_name == "view_file" or "File Path:" in content:
                    activity = "File Content"
                    tool_or_source = "`view_file`"
                    category = "Tool Output: File Contents (`view_file`)"
                    if "AbsolutePath" in tool_args:
                        detail = f"File: {Path(tool_args['AbsolutePath']).name}"
                    else:
                        detail = f"File: {extract_file_basename(content)}"
                elif tool_name == "run_command" or "The command exited" in content:
                    activity = "Shell Output"
                    tool_or_source = "`run_command`"
                    category = "Tool Output: Shell Execution (`run_command`)"
                    cmd = tool_args.get("CommandLine", "").strip()
                    detail = (
                        f"Cmd: {cmd[:40]}..."
                        if len(cmd) > 40
                        else (f"Cmd: {cmd}" if cmd else "Shell stdout/stderr")
                    )
                elif (
                    tool_name == "list_dir"
                    or "Summary: This directory contains" in content
                ):
                    activity = "Directory Listing"
                    tool_or_source = "`list_dir`"
                    category = "Tool Output: Directory Listings (`list_dir`)"
                    dir_path = tool_args.get("DirectoryPath", "")
                    detail = (
                        f"Dir: {Path(dir_path).name or dir_path}"
                        if dir_path
                        else "Directory listing"
                    )
                elif tool_name == "find_by_name" or (
                    "Found " in content and "results" in content
                ):
                    activity = "File Search"
                    tool_or_source = "`find_by_name`"
                    category = "Tool Output: File Search (`find_by_name`)"
                    pattern = tool_args.get("Pattern", "")
                    detail = f"Pattern: {pattern}" if pattern else "File search"
                elif tool_name == "grep_search" or "LineNumber" in content:
                    activity = "Pattern Search"
                    tool_or_source = "`grep_search`"
                    category = "Tool Output: Pattern Search (`grep_search`)"
                    query = tool_args.get("Query", "")
                    detail = f"Grep: {query}" if query else "Ripgrep search"
                elif tool_name == "write_to_file" or "Created file" in content:
                    activity = "File Write"
                    tool_or_source = "`write_to_file`"
                    category = "Tool Output: `write_to_file`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"'")
                    detail = (
                        f"File: {Path(target).name}" if target else "Write confirmation"
                    )
                elif (
                    tool_name == "replace_file_content"
                    or "The following changes were made" in content
                ):
                    activity = "File Edit"
                    tool_or_source = "`replace_file_content`"
                    category = "Tool Output: `replace_file_content`"
                    target = str(tool_args.get("TargetFile", "")).strip("\"'")
                    detail = (
                        f"Edit: {Path(target).name}" if target else "Edit diff block"
                    )
                elif tool_name == "call_mcp_tool" or tool_name.startswith("mcp_"):
                    srv = tool_args.get("ServerName", "")
                    t_n = tool_args.get("ToolName", tool_name)
                    activity = f"MCP: {srv}/{t_n}" if srv else f"MCP: {t_n}"
                    tool_or_source = f"`{t_n}`"
                    category = "Tool Output: MCP Tools"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
                else:
                    activity = f"Tool: {tool_name}" if tool_name else "Generic Output"
                    tool_or_source = f"`{tool_name}`" if tool_name else "Tool"
                    category = "Tool Output: Other"
                    clean = re.sub(r"\s+", " ", content).strip()
                    detail = clean[:60] + ("..." if len(clean) > 60 else "")
            else:
                pending_tool_calls = []
                activity = step_type
                tool_or_source = source
                category = step_type
                clean = re.sub(r"\s+", " ", content).strip()
                detail = clean[:60] + ("..." if len(clean) > 60 else "")

            phase = classify_phase(step_type, primary_tool_name)
            category_stats[category] = category_stats.get(category, 0) + step_chars

            steps.append(
                {
                    "step_index": step_idx,
                    "created_at": created_at,
                    "source": source,
                    "step_type": step_type,
                    "category": category,
                    "activity": activity,
                    "tool_or_source": tool_or_source,
                    "characters": step_chars,
                    "est_tokens": est_tokens,
                    "thinking_tokens": step_thinking_tokens,
                    "phase": phase,
                    "model_name": cur_model,
                    "detail": detail,
                }
            )

    total_tokens = int(total_chars / CHARS_PER_TOKEN)
    duration_seconds, active_agent_seconds, active_pairing_seconds = (
        compute_session_timings(steps, is_subagent=bool(detected_parent_id))
    )

    if first_model is None and native_step_models:
        sorted_steps = sorted(native_step_models.items())
        first_model = sorted_steps[0][1]
        cur_model = sorted_steps[-1][1]
        models_used = []
        for _, m in sorted_steps:
            if m not in models_used:
                models_used.append(m)
        model_transitions = []
        for _, m in sorted_steps:
            if not model_transitions or model_transitions[-1] != m:
                model_transitions.append(m)
        for s in steps:
            s_idx = s.get("step_index", 0)
            if s_idx in native_step_models:
                s["model_name"] = native_step_models[s_idx]
            else:
                preceding = [m for idx, m in sorted_steps if idx <= s_idx]
                s["model_name"] = preceding[-1] if preceding else first_model

    has_explicit_model = bool(first_model is not None)
    if first_model is None:
        first_model = cur_model
        if not models_used:
            models_used = [cur_model]
        if not model_transitions:
            model_transitions = [cur_model]

    # Append dynamically loaded tool schemas if explicitly requested
    if include_tool_schemas:
        parsed_blocks.extend(block_parser.load_tool_schemas())

    # Calculate billed cost based on steps
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0

    for s in steps:
        m_name = s.get("model_name", cur_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)

        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
            # Context truncation: reset running_ctx to checkpoint tokens
            running_ctx = tokens
            current_ephemeral = 0
        elif s_type == "EPHEMERAL_MESSAGE":
            # Transient injection for immediate turn only
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

    billed_tokens = total_billed_input_tokens + total_billed_output_tokens
    est_tok = running_ctx if running_ctx > 0 else total_tokens
    velocity = compute_active_velocity(
        est_tokens=est_tok,
        billed_tokens=billed_tokens,
        active_agent_seconds=active_agent_seconds,
        active_pairing_seconds=active_pairing_seconds,
    )

    return {
        "title": title or "Active Session",
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "active_agent_seconds": active_agent_seconds,
        "active_pairing_seconds": active_pairing_seconds,
        "velocity": velocity,
        "thinking_tokens": total_thinking_tokens,
        "user_prompts": user_prompts,
        "tool_calls_count": len(tool_execs),
        "redundant_file_reads": redundant_reads,
        "lines_written": lines_written,
        "lines_edited": lines_edited,
        "failed_tool_calls": failed_tool_calls,
        "billed_cost": total_billed_cost,
        "billed_input_tokens": total_billed_input_tokens,
        "billed_output_tokens": total_billed_output_tokens,
        "latest_model": cur_model,
        "models_used": models_used,
        "model_transitions": model_transitions,
        "latest_max_tokens": get_model_context_limit(cur_model),
        "project": project,
        "steps": steps,
        "total_chars": total_chars,
        "total_tokens": total_tokens,
        "active_context_tokens": running_ctx,
        "category_stats": category_stats,
        "tool_executions": tool_execs,
        "parsed_blocks": parsed_blocks,
        "parent_id": detected_parent_id,
        "dispatched_subagents": dispatched_subagents,
        "has_explicit_model": has_explicit_model,
    }


def _recompute_step_costs(analysis: dict, active_model: str) -> None:
    """Recalculates billed cost and input/output tokens across steps for a given model."""
    total_billed_cost = 0.0
    total_billed_input_tokens = 0
    total_billed_output_tokens = 0
    running_ctx = 0
    current_ephemeral = 0

    for s in analysis.get("steps", []):
        m_name = s.get("model_name", active_model)
        s_type = s.get("step_type")
        tokens = s.get("est_tokens", 0)

        step_cost = 0.0
        step_in_tokens = 0
        step_out_tokens = 0

        if s_type == "CHECKPOINT":
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

    analysis["billed_cost"] = total_billed_cost
    analysis["billed_input_tokens"] = total_billed_input_tokens
    analysis["billed_output_tokens"] = total_billed_output_tokens
    analysis["active_context_tokens"] = running_ctx


def sync_to_sqlite(
    cid: str,
    model_name: str,
    analysis: dict,
    *,
    attach_profile: bool = True,
    db_path: Path | None = None,
    conversations_dir: Path | None = None,
):
    """Safely and incrementally syncs session data to SQLite in WAL mode and computes turn diff."""
    diff_res = None
    try:
        target_db = Path(db_path) if db_path else DEFAULT_DB_PATH
        if not target_db:
            return None
        conn = get_db_connection(target_db, timeout=5.0)
        try:
            with conn:
                cur = conn.cursor()

                # Check existing state in steps and transcript_blocks
                row_steps = cur.execute(
                    "SELECT COALESCE(MAX(step_index), 0), COUNT(*) FROM steps WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
                prev_max_step = row_steps[0] if row_steps else 0
                prev_step_count = row_steps[1] if row_steps else 0

                row_blocks = cur.execute(
                    "SELECT COUNT(*) FROM transcript_blocks WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
                prev_block_count = row_blocks[0] if row_blocks else 0

                curr_blocks = analysis.get("parsed_blocks", [])
                curr_steps = analysis.get("steps", [])

                # Check if append-only candidate
                curr_steps_le = [
                    s for s in curr_steps if s["step_index"] <= prev_max_step
                ]
                curr_blocks_le = [
                    b for b in curr_blocks if b.step_index <= prev_max_step
                ]

                is_append_only = False
                if prev_step_count > 0 and prev_max_step > 0:
                    if (
                        len(curr_steps_le) == prev_step_count
                        and len(curr_blocks_le) == prev_block_count
                    ):
                        last_block_row = cur.execute(
                            "SELECT block_hash FROM transcript_blocks WHERE conversation_id = ? ORDER BY step_index DESC, id DESC LIMIT 1",
                            (cid,),
                        ).fetchone()
                        first_block_row = cur.execute(
                            "SELECT block_hash FROM transcript_blocks WHERE conversation_id = ? ORDER BY step_index ASC, id ASC LIMIT 1",
                            (cid,),
                        ).fetchone()

                        last_step_row = cur.execute(
                            "SELECT step_index, est_tokens FROM steps WHERE conversation_id = ? AND step_index = ?",
                            (cid, prev_max_step),
                        ).fetchone()
                        first_step_row = cur.execute(
                            "SELECT step_index, est_tokens FROM steps WHERE conversation_id = ? ORDER BY step_index ASC LIMIT 1",
                            (cid,),
                        ).fetchone()

                        blocks_match = True
                        if prev_block_count > 0:
                            if (
                                not last_block_row
                                or not curr_blocks_le
                                or curr_blocks_le[-1].hash != last_block_row[0]
                                or not first_block_row
                                or curr_blocks_le[0].hash != first_block_row[0]
                            ):
                                blocks_match = False
                        else:
                            if curr_blocks_le:
                                blocks_match = False

                        steps_match = True
                        if (
                            not last_step_row
                            or curr_steps_le[-1]["step_index"] != last_step_row[0]
                            or curr_steps_le[-1]["est_tokens"] != last_step_row[1]
                            or not first_step_row
                            or curr_steps_le[0]["step_index"] != first_step_row[0]
                            or curr_steps_le[0]["est_tokens"] != first_step_row[1]
                        ):
                            steps_match = False

                        if blocks_match and steps_match:
                            is_append_only = True

                if is_append_only:
                    new_blocks = [
                        b for b in curr_blocks if b.step_index > prev_max_step
                    ]
                    if new_blocks:
                        diff_res = diff_block_lists([], new_blocks)
                    else:
                        diff_res = DiffResult()
                else:
                    prev_blocks = cur.execute(
                        """
                        SELECT step_index, block_type, content, block_hash, call_id
                        FROM transcript_blocks
                        WHERE conversation_id = ?
                        ORDER BY step_index ASC, id ASC
                        """,
                        (cid,),
                    ).fetchall()
                    if curr_blocks and prev_blocks:
                        diff_res = diff_block_lists(prev_blocks, curr_blocks)
                    elif curr_blocks:
                        diff_res = diff_block_lists([], curr_blocks)
                    else:
                        diff_res = DiffResult()

                active_model = analysis.get("latest_model", model_name)
                parent_id = analysis.get("parent_id")
                subagent_role = analysis.get("subagent_role")
                subagent_type = analysis.get("subagent_type")
                invoking_step = analysis.get("invoking_step_index")
                root_id = cid
                depth = 0

                # Check if an existing row already has lineage from a parent's dispatch
                existing = cur.execute(
                    "SELECT parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index, project, title, compaction_count, pruned_tokens, model_name, models_used, total_steps FROM conversations WHERE id = ?",
                    (cid,),
                ).fetchone()

                comp_count = 0
                pruned_toks = 0
                if existing:
                    try:
                        comp_count = existing["compaction_count"] or 0
                    except (IndexError, KeyError):
                        comp_count = 0
                    try:
                        pruned_toks = existing["pruned_tokens"] or 0
                    except (IndexError, KeyError):
                        pruned_toks = 0
                    if not parent_id and existing["parent_id"]:
                        parent_id = existing["parent_id"]
                    if not subagent_role and existing["subagent_role"]:
                        subagent_role = existing["subagent_role"]
                    if not subagent_type and existing["subagent_type"]:
                        subagent_type = existing["subagent_type"]
                    if (
                        invoking_step is None
                        and existing["invoking_step_index"] is not None
                    ):
                        invoking_step = existing["invoking_step_index"]

                if diff_res and diff_res.compaction_detected:
                    comp_count += 1
                    pruned_toks += diff_res.removed_tokens

                if parent_id and not analysis.get("parent_id"):
                    analysis["parent_id"] = parent_id
                if subagent_role and not analysis.get("subagent_role"):
                    analysis["subagent_role"] = subagent_role

                p_row = None
                if parent_id:
                    if parent_id == cid:
                        parent_id = None
                        root_id = cid
                        depth = 0
                    else:
                        p_row = cur.execute(
                            "SELECT id, root_id, parent_id, project, depth, title, model_name FROM conversations WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        if p_row and (
                            p_row["root_id"] == cid
                            or p_row["id"] == cid
                            or p_row["parent_id"] == cid
                        ):
                            parent_id = None
                            root_id = cid
                            depth = 0
                        elif p_row:
                            root_id = p_row["root_id"] or p_row["id"]
                            depth = (p_row["depth"] or 0) + 1
                            if not analysis.get("project") and p_row["project"]:
                                analysis["project"] = normalize_project_name(
                                    p_row["project"]
                                )
                            if (
                                not analysis.get("title")
                                or analysis["title"] in ("Active Session", "")
                                or (
                                    analysis["title"].startswith("[Autonomous Worker]")
                                    and subagent_role
                                    and subagent_role != "Autonomous Worker"
                                )
                            ) and p_row["title"]:
                                role_label = subagent_role or "Worker"
                                clean_p_title = p_row["title"]
                                m_sub = re.match(r"^\[.+?\]\s*(.+)", clean_p_title)
                                if m_sub:
                                    clean_p_title = m_sub.group(1)
                                analysis["title"] = (
                                    f"[{role_label}] {clean_p_title[:90]}"
                                )
                        else:
                            root_id = parent_id
                            depth = 1

                if not analysis.get("has_explicit_model"):
                    native_step_models = extract_native_step_models(
                        cid, conversations_dir=conversations_dir
                    )
                    if native_step_models:
                        sorted_native = sorted(native_step_models.items())
                        first_native = sorted_native[0][1]
                        active_model = sorted_native[-1][1]
                        analysis["latest_model"] = active_model
                        models_used_list = []
                        for _, m in sorted_native:
                            if m not in models_used_list:
                                models_used_list.append(m)
                        model_transitions = []
                        for _, m in sorted_native:
                            if not model_transitions or model_transitions[-1] != m:
                                model_transitions.append(m)
                        analysis["models_used"] = models_used_list
                        analysis["model_transitions"] = model_transitions
                        analysis["latest_max_tokens"] = get_model_context_limit(active_model)

                        for s in analysis.get("steps", []):
                            s_idx = s.get("step_index", 0)
                            if s_idx in native_step_models:
                                s["model_name"] = native_step_models[s_idx]
                            else:
                                preceding = [m for idx, m in sorted_native if idx <= s_idx]
                                s["model_name"] = preceding[-1] if preceding else first_native

                        _recompute_step_costs(analysis, active_model)
                        is_append_only = False
                    else:
                        existing_model = None
                        existing_steps = 0
                        existing_invoking_step = None
                        if existing:
                            try:
                                existing_model = existing["model_name"]
                                existing_steps = existing["total_steps"] or 0
                                existing_invoking_step = existing["invoking_step_index"]
                            except (IndexError, KeyError):
                                pass

                        p_model = None
                        if p_row:
                            try:
                                p_model = p_row["model_name"]
                            except (IndexError, KeyError):
                                pass

                        inherited_m = None
                        if existing_model:
                            inherited_m = existing_model
                        elif p_model:
                            inherited_m = resolve_subagent_model("inherit", p_model)

                        if inherited_m and inherited_m != active_model:
                            active_model = inherited_m
                            analysis["latest_model"] = active_model
                            analysis["models_used"] = [active_model]
                            analysis["model_transitions"] = [active_model]
                            analysis["latest_max_tokens"] = get_model_context_limit(active_model)

                            for s in analysis.get("steps", []):
                                s["model_name"] = active_model

                            _recompute_step_costs(analysis, active_model)
                            is_append_only = False

                if parent_id is not None or subagent_role is not None or depth > 0:
                    analysis["active_pairing_seconds"] = 0.0
                    analysis["velocity"] = compute_active_velocity(
                        est_tokens=analysis.get(
                            "active_context_tokens", analysis.get("total_tokens", 0)
                        ),
                        billed_tokens=(
                            analysis.get("billed_input_tokens", 0)
                            + analysis.get("billed_output_tokens", 0)
                        ),
                        active_agent_seconds=analysis.get("active_agent_seconds", 0.0),
                        active_pairing_seconds=0.0,
                    )
                elif "velocity" not in analysis or not analysis["velocity"]:
                    analysis["velocity"] = compute_active_velocity(
                        est_tokens=analysis.get(
                            "active_context_tokens", analysis.get("total_tokens", 0)
                        ),
                        billed_tokens=(
                            analysis.get("billed_input_tokens", 0)
                            + analysis.get("billed_output_tokens", 0)
                        ),
                        active_agent_seconds=analysis.get("active_agent_seconds", 0.0),
                        active_pairing_seconds=analysis.get(
                            "active_pairing_seconds", 0.0
                        ),
                    )

                if not analysis.get("project") and existing:
                    try:
                        if existing["project"]:
                            analysis["project"] = normalize_project_name(
                                existing["project"]
                            )
                    except (IndexError, KeyError):
                        pass

                if not analysis.get("project") and root_id:
                    swarm_row = cur.execute(
                        """
                        SELECT project FROM conversations
                        WHERE (id = ? OR root_id = ?)
                          AND project IS NOT NULL
                          AND project != ''
                          AND lower(project) NOT IN ('(unassigned)', 'unassigned')
                        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, depth ASC
                        LIMIT 1
                        """,
                        (root_id, root_id, root_id),
                    ).fetchone()
                    if swarm_row:
                        try:
                            if swarm_row["project"]:
                                analysis["project"] = swarm_row["project"]
                        except (IndexError, KeyError):
                            if swarm_row[0]:
                                analysis["project"] = swarm_row[0]

                if analysis.get("project"):
                    analysis["project"] = normalize_project_name(analysis["project"])

                models_used_list = analysis.get("models_used", [])
                model_transitions = analysis.get("model_transitions", [])
                if not models_used_list and active_model:
                    models_used_list = [active_model]
                if not model_transitions:
                    model_transitions = models_used_list

                if len(model_transitions) > 1:
                    if len(model_transitions) <= 3:
                        conv_model_name = " → ".join(model_transitions)
                    else:
                        conv_model_name = (
                            f"{model_transitions[0]} → ... → {model_transitions[-1]}"
                        )
                    models_used_str = ", ".join(models_used_list)
                elif models_used_list:
                    conv_model_name = models_used_list[0]
                    models_used_str = models_used_list[0]
                else:
                    conv_model_name = active_model
                    models_used_str = active_model

                # Upsert conversation record with hierarchy and compaction columns
                conn.execute(
                    """
                    INSERT INTO conversations (
                        id, title, project, model_name, models_used, created_at, updated_at, total_steps, total_characters, total_tokens,
                        duration_seconds, active_agent_seconds, active_pairing_seconds, thinking_tokens,
                        user_prompts, tool_calls_count, redundant_file_reads, lines_written, lines_edited, failed_tool_calls,
                        billed_cost, billed_input_tokens, billed_output_tokens,
                        parent_id, root_id, depth, subagent_role, subagent_type, invoking_step_index,
                        compaction_count, pruned_tokens
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        project = COALESCE(excluded.project, conversations.project),
                        model_name = excluded.model_name,
                        models_used = excluded.models_used,
                        updated_at = excluded.updated_at,
                        total_steps = excluded.total_steps,
                        total_characters = excluded.total_characters,
                        total_tokens = excluded.total_tokens,
                        duration_seconds = excluded.duration_seconds,
                        active_agent_seconds = excluded.active_agent_seconds,
                        active_pairing_seconds = excluded.active_pairing_seconds,
                        thinking_tokens = excluded.thinking_tokens,
                        user_prompts = excluded.user_prompts,
                        tool_calls_count = excluded.tool_calls_count,
                        redundant_file_reads = excluded.redundant_file_reads,
                        lines_written = excluded.lines_written,
                        lines_edited = excluded.lines_edited,
                        failed_tool_calls = excluded.failed_tool_calls,
                        billed_cost = excluded.billed_cost,
                        billed_input_tokens = excluded.billed_input_tokens,
                        billed_output_tokens = excluded.billed_output_tokens,
                        parent_id = excluded.parent_id,
                        root_id = excluded.root_id,
                        depth = excluded.depth,
                        subagent_role = COALESCE(excluded.subagent_role, conversations.subagent_role),
                        subagent_type = COALESCE(excluded.subagent_type, conversations.subagent_type),
                        invoking_step_index = COALESCE(excluded.invoking_step_index, conversations.invoking_step_index),
                        compaction_count = excluded.compaction_count,
                        pruned_tokens = excluded.pruned_tokens
                """,
                    (
                        cid,
                        analysis["title"],
                        analysis.get("project"),
                        conv_model_name,
                        models_used_str,
                        analysis["start_time"] or datetime.now().isoformat(),
                        analysis["end_time"] or datetime.now().isoformat(),
                        len(analysis["steps"]),
                        analysis["total_chars"],
                        analysis["total_tokens"],
                        analysis["duration_seconds"],
                        analysis.get("active_agent_seconds", 0.0),
                        analysis.get("active_pairing_seconds", 0.0),
                        analysis.get("thinking_tokens", 0),
                        analysis.get("user_prompts", 0),
                        analysis.get("tool_calls_count", 0),
                        analysis.get("redundant_file_reads", 0),
                        analysis.get("lines_written", 0),
                        analysis.get("lines_edited", 0),
                        analysis.get("failed_tool_calls", 0),
                        analysis.get("billed_cost", 0.0),
                        analysis.get("billed_input_tokens", 0),
                        analysis.get("billed_output_tokens", 0),
                        parent_id,
                        root_id,
                        depth,
                        subagent_role,
                        subagent_type,
                        invoking_step,
                        comp_count,
                        pruned_toks,
                    ),
                )

                # Upsert dispatched subagents so child rows have lineage right away
                for child in analysis.get("dispatched_subagents", []):
                    ch_id = child["id"]
                    ch_role = child.get("role")
                    ch_type = child.get("type")
                    ch_step = child.get("step_index")
                    ch_model = resolve_subagent_model(
                        child.get("model"), parent_model=active_model
                    )
                    clean_t = analysis["title"]
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
                                WHEN conversations.total_steps = 0 OR conversations.model_name IS NULL OR conversations.model_name = '' THEN excluded.model_name
                                ELSE conversations.model_name
                            END,
                            models_used = CASE
                                WHEN conversations.total_steps = 0 OR conversations.models_used IS NULL OR conversations.models_used = '' THEN excluded.models_used
                                ELSE conversations.models_used
                            END
                    """,
                        (
                            ch_id,
                            ch_title,
                            analysis.get("project"),
                            cid,
                            root_id,
                            depth + 1,
                            ch_role,
                            ch_type,
                            ch_step,
                            ch_model,
                            ch_model,
                        ),
                    )

                # Propagate project to unassigned swarm members
                if analysis.get("project"):
                    conn.execute(
                        "UPDATE conversations SET project = ? WHERE (id = ? OR root_id = ?) AND (project IS NULL OR project = '' OR lower(project) IN ('(unassigned)', 'unassigned'))",
                        (analysis["project"], root_id, root_id),
                    )

                if is_append_only:
                    steps_to_insert = [
                        s for s in curr_steps if s["step_index"] > prev_max_step
                    ]
                    if steps_to_insert:
                        step_params = [
                            (
                                cid,
                                s["step_index"],
                                s["created_at"],
                                s["source"],
                                s["step_type"],
                                s["category"],
                                s["activity"],
                                s["tool_or_source"],
                                s["characters"],
                                s["est_tokens"],
                                s.get("thinking_tokens", 0),
                                s.get("phase", "reasoning"),
                                s.get("model_name", active_model),
                                s["detail"],
                                s.get("billed_cost", 0.0),
                                s.get("billed_input_tokens", 0),
                                s.get("billed_output_tokens", 0),
                            )
                            for s in steps_to_insert
                        ]
                        conn.executemany(
                            """
                            INSERT INTO steps (
                                conversation_id, step_index, created_at, source, step_type, category, activity,
                                tool_or_source, characters, est_tokens, thinking_tokens, phase, model_name, detail,
                                billed_cost, billed_input_tokens, billed_output_tokens
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            step_params,
                        )

                    tools_to_insert = [
                        te
                        for te in analysis.get("tool_executions", [])
                        if te["step_index"] > prev_max_step
                    ]
                    if tools_to_insert:
                        tool_params = [
                            (
                                cid,
                                te["step_index"],
                                te["tool_name"],
                                te.get("mcp_server"),
                                te.get("mcp_tool"),
                                te.get("target_path"),
                                te.get("command_line"),
                                te.get("exit_code"),
                                te.get("is_error", 0),
                            )
                            for te in tools_to_insert
                        ]
                        conn.executemany(
                            """
                            INSERT INTO tool_executions (
                                conversation_id, step_index, tool_name, mcp_server, mcp_tool,
                                target_path, command_line, exit_code, is_error
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            tool_params,
                        )

                    blocks_to_insert = [
                        b for b in curr_blocks if b.step_index > prev_max_step
                    ]
                    if blocks_to_insert:
                        block_params = []
                        for b in blocks_to_insert:
                            content_str = b.text
                            if b.arguments is not None:
                                import json

                                content_str += "\n" + json.dumps(b.arguments)

                            block_params.append(
                                (
                                    cid,
                                    b.step_index,
                                    b.kind.value,
                                    content_str,
                                    b.hash,
                                    b.call_id,
                                )
                            )

                        conn.executemany(
                            """
                            INSERT INTO transcript_blocks (
                                conversation_id, step_index, block_type, content, block_hash, call_id
                            )
                            VALUES (?, ?, ?, ?, ?, ?)
                        """,
                            block_params,
                        )
                else:
                    conn.execute(
                        "DELETE FROM steps WHERE conversation_id = ?",
                        (cid,),
                    )
                    step_params = [
                        (
                            cid,
                            s["step_index"],
                            s["created_at"],
                            s["source"],
                            s["step_type"],
                            s["category"],
                            s["activity"],
                            s["tool_or_source"],
                            s["characters"],
                            s["est_tokens"],
                            s.get("thinking_tokens", 0),
                            s.get("phase", "reasoning"),
                            s.get("model_name", active_model),
                            s["detail"],
                            s.get("billed_cost", 0.0),
                            s.get("billed_input_tokens", 0),
                            s.get("billed_output_tokens", 0),
                        )
                        for s in curr_steps
                    ]
                    conn.executemany(
                        """
                        INSERT INTO steps (
                            conversation_id, step_index, created_at, source, step_type, category, activity,
                            tool_or_source, characters, est_tokens, thinking_tokens, phase, model_name, detail,
                            billed_cost, billed_input_tokens, billed_output_tokens
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(conversation_id, step_index) DO UPDATE SET
                            characters = excluded.characters,
                            est_tokens = excluded.est_tokens,
                            thinking_tokens = excluded.thinking_tokens,
                            phase = excluded.phase,
                            model_name = excluded.model_name,
                            detail = excluded.detail,
                            billed_cost = excluded.billed_cost,
                            billed_input_tokens = excluded.billed_input_tokens,
                            billed_output_tokens = excluded.billed_output_tokens
                    """,
                        step_params,
                    )

                    conn.execute(
                        "DELETE FROM tool_executions WHERE conversation_id = ?", (cid,)
                    )
                    if analysis.get("tool_executions"):
                        tool_params = [
                            (
                                cid,
                                te["step_index"],
                                te["tool_name"],
                                te.get("mcp_server"),
                                te.get("mcp_tool"),
                                te.get("target_path"),
                                te.get("command_line"),
                                te.get("exit_code"),
                                te.get("is_error", 0),
                            )
                            for te in analysis["tool_executions"]
                        ]
                        conn.executemany(
                            """
                            INSERT INTO tool_executions (
                                conversation_id, step_index, tool_name, mcp_server, mcp_tool,
                                target_path, command_line, exit_code, is_error
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            tool_params,
                        )

                    conn.execute(
                        "DELETE FROM transcript_blocks WHERE conversation_id = ?",
                        (cid,),
                    )
                    if curr_blocks:
                        block_params = []
                        for b in curr_blocks:
                            content_str = b.text
                            if b.arguments is not None:
                                import json

                                content_str += "\n" + json.dumps(b.arguments)

                            block_params.append(
                                (
                                    cid,
                                    b.step_index,
                                    b.kind.value,
                                    content_str,
                                    b.hash,
                                    b.call_id,
                                )
                            )

                        conn.executemany(
                            """
                            INSERT INTO transcript_blocks (
                                conversation_id, step_index, block_type, content, block_hash, call_id
                            )
                            VALUES (?, ?, ?, ?, ?, ?)
                        """,
                            block_params,
                        )

                # Refresh rollups for this conversation
                refresh_conversation_rollups(conn, cid)
                conn.commit()

            # Query fleet rollups across root + subagents
            try:
                fleet_row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(billed_cost), 0.0) FROM conversations WHERE root_id = ? OR id = ?",
                    (root_id, root_id),
                ).fetchone()
                if fleet_row:
                    analysis["fleet_agent_count"] = max(fleet_row[0] or 1, 1)
                    analysis["fleet_cost"] = (
                        float(fleet_row[1])
                        if fleet_row[1] is not None
                        else float(analysis.get("billed_cost", 0.0))
                    )
            except Exception:
                pass

            # Refresh and cache quota state and activity profile
            try:
                analysis["quota_state"] = sync_and_get_quota_state(
                    conn, ttl_seconds=300
                )
                if attach_profile:
                    analysis["activity_profile"] = get_hourly_activity_profile(conn)
            except Exception:
                pass

        finally:
            conn.close()
        return diff_res
    except Exception:
        # Never break hook execution on db errors
        return diff_res


def _extract_bucket_view(
    buckets: dict[str, dict] | list[dict] | None,
    bucket_id: str,
    default_cap: float = 672.0,
) -> dict[str, Any]:
    b = {}
    if isinstance(buckets, dict):
        b = buckets.get(bucket_id) or {}
    elif isinstance(buckets, list):
        for item in buckets:
            if isinstance(item, dict) and (
                item.get("bucket_id") == bucket_id or item.get("bucketId") == bucket_id
            ):
                b = item
                break

    cap = float(
        b.get("calibrated_capacity")
        if b.get("calibrated_capacity") is not None
        else (b.get("calibratedCapacity") or default_cap)
    )
    rem_raw = (
        b.get("remaining_fraction")
        if b.get("remaining_fraction") is not None
        else b.get("remainingFraction", 1.0)
    )
    rem_pct = float(rem_raw if rem_raw is not None else 1.0) * 100.0
    r_time = str(b.get("reset_time") or b.get("resetTime") or "")
    cd = format_reset_countdown(r_time)
    cd_str = f"`{cd}` to reset" if cd else "no reset scheduled"
    return {
        "capacity": cap,
        "remaining_pct": rem_pct,
        "reset_time": r_time,
        "countdown": cd,
        "countdown_str": cd_str,
    }


def _resolve_quota_and_activity(
    analysis: dict[str, Any],
    db_path: Path | None = None,
    *,
    force_profile: bool = False,
) -> tuple[Any, Any]:
    """Resolve quota state and activity profile from analysis or database fallback."""
    quota_state = analysis.get("quota_state")
    activity_profile = analysis.get("activity_profile")
    target_db = db_path if db_path is not None else DEFAULT_DB_PATH
    need_profile = force_profile or should_generate_artifacts()
    if (
        (quota_state is None or (activity_profile is None and need_profile))
        and target_db
        and target_db.exists()
    ):
        try:
            conn = get_db_connection(target_db, timeout=2.0)
            try:
                if quota_state is None:
                    quota_state = sync_and_get_quota_state(conn, ttl_seconds=300)
                    analysis["quota_state"] = quota_state
                if activity_profile is None and need_profile:
                    activity_profile = get_hourly_activity_profile(conn)
                    analysis["activity_profile"] = activity_profile
            finally:
                conn.close()
        except Exception:
            pass
    return quota_state, activity_profile


def generate_context_usage_report(
    cid: str,
    model_name: str,
    max_tokens: int,
    analysis: dict,
    artifact_dir: Path | None = None,
    diff_res: Any = None,
) -> str:
    try:
        active_model = analysis.get("latest_model", model_name)
        active_max = analysis.get("latest_max_tokens", max_tokens)
        active_tokens = analysis.get(
            "active_context_tokens", analysis.get("total_tokens", 0)
        )
        used_tokens = analysis.get("total_tokens", 0)
        active_pct = (active_tokens / active_max * 100) if active_max else 0.0
        cost = analysis.get("billed_cost", 0.0)
        in_toks = analysis.get("billed_input_tokens", 0)
        out_toks = analysis.get("billed_output_tokens", 0)

        top_steps = sorted(
            analysis.get("steps", []),
            key=lambda s: s.get("characters", 0),
            reverse=True,
        )[:10]

        # Check database for lineage and swarm fleet info
        parent_info = None
        fleet_children = []
        try:
            if DEFAULT_DB_PATH and DEFAULT_DB_PATH.exists():
                c_conn = get_db_connection(DEFAULT_DB_PATH, timeout=2.0)
                with c_conn:
                    cur_row = c_conn.execute(
                        "SELECT parent_id, root_id, depth, subagent_role FROM conversations WHERE id = ?",
                        (cid,),
                    ).fetchone()
                    if cur_row and cur_row["parent_id"]:
                        p_row = c_conn.execute(
                            "SELECT id, title, project FROM conversations WHERE id = ?",
                            (cur_row["parent_id"],),
                        ).fetchone()
                        if p_row:
                            parent_info = {
                                "id": p_row["id"],
                                "title": p_row["title"]
                                or f"Parent ({p_row['id'][:8]})",
                                "role": cur_row["subagent_role"] or "Worker",
                                "depth": cur_row["depth"] or 1,
                            }

                    # Fetch children / descendants
                    ch_rows = c_conn.execute(
                        """
                        SELECT id, title, subagent_role, depth, total_steps, total_tokens, billed_cost
                        FROM conversations
                        WHERE (parent_id = ? OR (root_id = ? AND id != ?))
                        ORDER BY depth ASC, created_at ASC
                    """,
                        (cid, cid, cid),
                    ).fetchall()
                    fleet_children = [dict(r) for r in ch_rows]
                c_conn.close()
        except Exception:
            pass

        # Quota metrics resolution
        quota_state, activity_profile = _resolve_quota_and_activity(
            analysis, force_profile=True
        )

        state_dict: dict[str, dict] = {}
        if quota_state:
            if isinstance(quota_state, dict):
                state_dict = quota_state
            elif isinstance(quota_state, list):
                for item in quota_state:
                    if isinstance(item, dict):
                        bid = item.get("bucket_id") or item.get("bucketId")
                        if bid:
                            state_dict[bid] = item

        session_cost = float(cost)
        latest_step_cost = 0.0
        for s in reversed(analysis.get("steps", [])):
            if s.get("billed_cost", 0.0) > 0:
                latest_step_cost = float(s["billed_cost"])
                break

        quota_metrics = None
        if state_dict:
            try:
                quota_metrics = calculate_quota_metrics(
                    turn_cost=latest_step_cost,
                    thread_cost=session_cost,
                    model_name=active_model,
                    quota_state=state_dict,
                    activity_profile=activity_profile,
                )
            except Exception:
                quota_metrics = None

        turn_pct = float(quota_metrics.get("turn_pct", 0.0)) if quota_metrics else 0.0
        sess_pct = (
            float(
                quota_metrics.get(
                    "session_weekly_pct",
                    quota_metrics.get("thread_weekly_pct", 0.0),
                )
            )
            if quota_metrics
            else 0.0
        )

        gw_view = _extract_bucket_view(state_dict, "gemini-weekly", 672.0)
        g5_view = _extract_bucket_view(state_dict, "gemini-5h", 236.7)
        pw_view = _extract_bucket_view(state_dict, "3p-weekly", 200.0)
        p5_view = _extract_bucket_view(state_dict, "3p-5h", 70.0)

        gw_cap = gw_view["capacity"]
        gw_rem = gw_view["remaining_pct"]
        gw_cd_str = gw_view["countdown_str"]

        g5_rem = g5_view["remaining_pct"]
        g5_cd_str = g5_view["countdown_str"]

        pw_rem = pw_view["remaining_pct"]
        pw_cd_str = pw_view["countdown_str"]

        p5_rem = p5_view["remaining_pct"]
        p5_cd_str = p5_view["countdown_str"]

        weekly_capacity = (
            float(quota_metrics.get("calibrated_weekly_capacity") or 0.0)
            if quota_metrics
            else 0.0
        )
        if weekly_capacity <= 0:
            weekly_capacity = (
                pw_view["capacity"]
                if ("claude" in active_model.lower() or "gpt" in active_model.lower())
                else gw_view["capacity"]
            )
        if weekly_capacity <= 0:
            weekly_capacity = 672.0

        lines = [
            "# Live Context Window Usage Report",
            f"\n> **Conversation**: `{cid}`  ",
            f"> **Model**: `{active_model}` ({format_tokens(active_max)} token window limit)  ",
        ]
        if parent_info:
            lines.append(
                f"> **Lineage**: Sub-Agent (Role: `{parent_info['role']}`, Tier: {parent_info['depth']}) spawned by [{parent_info['title']}](conversation://{parent_info['id']})  "
            )
        velocity = analysis.get("velocity")
        if not velocity:
            velocity = compute_active_velocity(
                est_tokens=active_tokens,
                billed_tokens=in_toks + out_toks,
                active_agent_seconds=analysis.get("active_agent_seconds", 0.0),
                active_pairing_seconds=analysis.get("active_pairing_seconds", 0.0),
            )

        vel_dict = velocity or {}
        ctx_tpm = safe_float_metric(vel_dict.get("active_context_tpm"))
        bld_tpm = safe_float_metric(vel_dict.get("active_billed_tpm"))
        bld_tph = safe_float_metric(vel_dict.get("active_billed_tph"))

        lines.extend(
            [
                f"> **Active Context Window**: **{active_pct:.1f}%** ({format_tokens(active_tokens)} / {format_tokens(active_max)} estimated tokens)  ",
                f"> **Active Context Fill Rate**: **{ctx_tpm:,.1f} tok/min**  ",
                f"> **Active Billed Throughput**: **{bld_tpm:,.1f} tok/min** / **{bld_tph:,.1f} tok/hr**  ",
                f"> **Cumulative Disk Footprint**: **{format_tokens(used_tokens)} tokens** across {len(analysis.get('steps', []))} logged steps  ",
                f"> **Session API Value**: **${cost:,.4f}** (~{format_tokens(in_toks)} in / {format_tokens(out_toks)} out)\n",
                "## Subscription & Quota Impact\n",
                f"- Turn Cost: `${latest_step_cost:,.2f}` (`{turn_pct:.2f}%` of weekly quota)",
                f"- Session Cost: `${session_cost:,.2f}` (`{sess_pct:.2f}%` of weekly quota)",
                "- Subscription Quota Status:",
                f"  - Gemini Weekly: `{gw_rem:.1f}%` remaining ({gw_cd_str}, calibrated capacity `${gw_cap:,.2f}`)",
                f"  - Gemini 5-Hour: `{g5_rem:.1f}%` remaining ({g5_cd_str})",
                f"  - Claude & GPT Weekly: `{pw_rem:.1f}%` remaining ({pw_cd_str})",
                f"  - Claude & GPT 5-Hour: `{p5_rem:.1f}%` remaining ({p5_cd_str})\n",
                "## 1. Granular Context Breakdown by Category\n",
                "| Category | Characters | Est. Tokens | Share |",
                "| :--- | :--- | :--- | :--- |",
            ]
        )

        for cat, chars in sorted(
            analysis.get("category_stats", {}).items(), key=lambda x: x[1], reverse=True
        ):
            toks = int(chars / CHARS_PER_TOKEN)
            cat_pct = (
                (chars / analysis["total_chars"] * 100)
                if analysis.get("total_chars")
                else 0.0
            )
            lines.append(
                f"| {cat} | {chars:,} | ~{format_tokens(toks)} | {cat_pct:.1f}% |"
            )

        lines.extend(
            [
                "\n## 2. Top Context-Consuming Steps\n",
                "| Step # | Activity | Source / Tool | Est. Tokens | % Wk Quota | Detail |",
                "| :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )

        for s in top_steps:
            step_cost = float(s.get("billed_cost") or s.get("cost") or 0.0)
            step_wk_quota = (
                (step_cost / weekly_capacity * 100.0) if weekly_capacity > 0 else 0.0
            )
            step_index = s.get("step_index", 1)
            activity = s.get("activity", "Turn")
            source = s.get("tool_or_source", "Model")
            est_tokens = s.get("est_tokens", 0)
            detail = s.get("detail", "")
            lines.append(
                f"| #{step_index} | {activity} | {source} | ~{format_tokens(est_tokens)} | {step_wk_quota:.2f}% | {detail} |"
            )

        sec_num = 3
        if diff_res and getattr(diff_res, "compaction_detected", False):
            pruned_indices = getattr(diff_res, "pruned_step_indices", [])
            p_min = min(pruned_indices) if pruned_indices else 0
            p_max = max(pruned_indices) if pruned_indices else 0
            removed_toks = getattr(diff_res, "removed_tokens", 0)
            steps_list = analysis.get("steps", [])
            act_min = steps_list[0]["step_index"] if steps_list else 0
            act_max = steps_list[-1]["step_index"] if steps_list else 0

            lines.extend(
                [
                    f"\n## {sec_num}. Active Context Memory Boundary\n",
                    f"> **Active Memory Window**: Steps #{act_min} to #{act_max} (currently visible to model)  ",
                    f"> **Context Compaction**: {len(pruned_indices)} historical steps pruned (~{format_tokens(removed_toks)} freed across {len(diff_res.removed)} blocks)  ",
                    f"> **Pruned Step Range**: Steps #{p_min} to #{p_max} (older history trimmed to respect context limits)\n",
                ]
            )
            sec_num += 1

        if fleet_children:
            total_fleet_toks = sum(c["total_tokens"] or 0 for c in fleet_children)
            total_fleet_cost = sum(c["billed_cost"] or 0.0 for c in fleet_children)
            total_fleet_steps = sum(c["total_steps"] or 0 for c in fleet_children)

            lines.extend(
                [
                    f"\n## {sec_num}. Delegated Swarm Fleet (Context Shielding)\n",
                    f"> **Dispatched Workers**: {len(fleet_children)} sub-agents  ",
                    f"> **Delegated Fleet Work**: ~{format_tokens(total_fleet_toks)} tokens (${total_fleet_cost:,.4f}) across {total_fleet_steps} steps  ",
                    f"> **Context Shielding Benefit**: Delegating tasks avoided injecting ~{format_tokens(total_fleet_toks)} tokens of verbose sub-task output into this active context window.\n",
                    "| Sub-Agent | Role | Tier | Steps | Tokens | Cost | % Wk Quota |",
                    "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
                ]
            )
            for ch in fleet_children:
                ch_title = ch.get("title") or f"Sub-Agent {ch['id'][:8]}"
                ch_role = ch.get("subagent_role") or "Worker"
                ch_depth = ch.get("depth") or 1
                ch_steps = ch.get("total_steps") or 0
                ch_toks = ch.get("total_tokens") or 0
                ch_c = ch.get("billed_cost") or 0.0
                subagent_wk_quota = (
                    (ch_c / weekly_capacity * 100.0) if weekly_capacity > 0 else 0.0
                )
                lines.append(
                    f"| [{ch_title}](conversation://{ch['id']}) | {ch_role} | Tier {ch_depth} | {ch_steps} | ~{format_tokens(ch_toks)} | ${ch_c:,.2f} | {subagent_wk_quota:.2f}% |"
                )

        lines.extend(
            [
                "\n---\n*Report generated automatically by the `context-monitor` plugin.* · [Context Monitor](sidecar://context-monitor/panel/)"
            ]
        )

        report_text = "\n".join(lines) + "\n"
        if artifact_dir is not None:
            artifact_dir = Path(artifact_dir)
            report_file = artifact_dir / "context_usage.md"
            html_file = artifact_dir / "context_usage.html"
            report_file.parent.mkdir(parents=True, exist_ok=True)
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(report_text)

            # Generate companion HTML report (Generative UI card)
            try:
                html_content = generate_html_report(
                    cid, active_model, active_max, analysis, diff_res=diff_res
                )
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception:
                pass

        return report_text
    except Exception:
        return ""


generate_artifact_report = generate_context_usage_report


def main():
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        input_data = {}

    transcript_path_str = input_data.get("transcriptPath")
    conversation_id = input_data.get("conversationId", "unknown")

    if not transcript_path_str or not os.path.exists(transcript_path_str):
        print(json.dumps({}))
        return

    # Defensive fallback: resolve conversation_id from brain path if missing or generic
    if (not conversation_id or conversation_id == "unknown") and transcript_path_str:
        p = Path(transcript_path_str)
        parts = p.parts
        if "brain" in parts:
            idx = parts.index("brain")
            if idx + 1 < len(parts):
                conversation_id = parts[idx + 1]

    transcript_path = Path(transcript_path_str)
    analysis = analyze_transcript(
        transcript_path,
        cid=conversation_id
        if conversation_id and conversation_id != "unknown"
        else None,
    )
    latest_model = analysis.get("latest_model", DEFAULT_MODEL)
    max_tokens = analysis.get(
        "latest_max_tokens", get_model_context_limit(latest_model)
    )

    diff_res = sync_to_sqlite(
        conversation_id,
        latest_model,
        analysis,
        attach_profile=should_generate_artifacts(),
    )

    # Refresh latest_model and max_tokens in case sync_to_sqlite resolved an inherited or native model
    latest_model = analysis.get("latest_model", latest_model)
    max_tokens = analysis.get(
        "latest_max_tokens", get_model_context_limit(latest_model)
    )

    # Artifact reports (context_usage.md / .html) are deprecated in favor of the native
    # Antigravity side panel extension. They are disabled by default and only generated
    # if explicitly enabled in ~/.gemini/antigravity/context_monitor_settings.json
    artifact_dir = Path(
        os.path.expanduser(f"~/.gemini/antigravity/brain/{conversation_id}")
    )
    if artifact_dir.exists() and should_generate_artifacts():
        generate_artifact_report(
            conversation_id,
            latest_model,
            max_tokens,
            analysis,
            artifact_dir,
            diff_res=diff_res,
        )

    quota_state, activity_profile = _resolve_quota_and_activity(analysis)

    quota_metrics = None
    turn_wk_pct = None
    if quota_state:
        try:
            session_cost = analysis.get("billed_cost", 0.0)
            latest_step_cost = 0.0
            for s in reversed(analysis.get("steps", [])):
                if s.get("billed_cost", 0.0) > 0:
                    latest_step_cost = float(s["billed_cost"])
                    break

            quota_metrics = calculate_quota_metrics(
                turn_cost=latest_step_cost,
                thread_cost=session_cost,
                model_name=latest_model,
                quota_state=quota_state,
                activity_profile=activity_profile,
            )
            if (
                analysis.get("parent_id") or analysis.get("subagent_role")
            ) and quota_metrics.get("turn_pct", 0) > 0:
                turn_wk_pct = quota_metrics.get("turn_pct")
        except Exception:
            quota_metrics = None

    # Return transient ephemeral message for Antigravity 2.0 UI
    # Invariant: Zero context pollution - ephemeralMessage is never written to transcript.jsonl
    active_tokens = analysis.get(
        "active_context_tokens", analysis.get("total_tokens", 0)
    )
    active_max = analysis.get("latest_max_tokens", max_tokens)
    active_pct = (active_tokens / active_max * 100) if active_max else 0.0
    session_cost_val = analysis.get("billed_cost", 0.0)
    added_tok = getattr(diff_res, "added_tokens", 0) if diff_res else 0

    fleet_agent_count = analysis.get("fleet_agent_count", 1)
    fleet_cost = analysis.get("fleet_cost")
    fleet_wk_pct = None
    if quota_metrics and fleet_cost is not None:
        cal_cap = quota_metrics.get("calibrated_weekly_capacity")
        if cal_cap and cal_cap > 0:
            fleet_wk_pct = (fleet_cost / cal_cap) * 100.0
        elif (
            session_cost_val
            and session_cost_val > 0
            and quota_metrics.get("session_weekly_pct") is not None
        ):
            fleet_wk_pct = (fleet_cost / session_cost_val) * float(
                quota_metrics["session_weekly_pct"]
            )

    ephemeral_msg = format_ephemeral_status(
        active_pct=active_pct,
        active_tokens=active_tokens,
        active_max=active_max,
        added_tok=added_tok,
        turn_cost=session_cost_val,
        session_cost=session_cost_val,
        quota_metrics=quota_metrics,
        turn_wk_pct=turn_wk_pct,
        diff_res=diff_res,
        fleet_cost=fleet_cost,
        fleet_wk_pct=fleet_wk_pct,
        fleet_agent_count=fleet_agent_count,
    )

    output_payload = {"injectSteps": [{"ephemeralMessage": ephemeral_msg}]}
    print(json.dumps(output_payload))


if __name__ == "__main__":
    main()
