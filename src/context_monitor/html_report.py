"""
Antigravity Context Monitor - Generative UI HTML Report Generator
-----------------------------------------------------------------
Generates a self-contained, theme-aware, compact (<500px) HTML card
for inline chat embedding via <agent-embed> and side-pane artifact viewing.
"""

from __future__ import annotations

import html
from typing import Any


CHARS_PER_TOKEN = 3.8


def format_tokens(n: int | float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def generate_html_report(
    cid: str,
    model_name: str,
    max_tokens: int,
    analysis: dict[str, Any],
    diff_res: Any | None = None,
) -> str:
    """
    Renders an interactive, theme-aware HTML artifact card complying with
    Antigravity's generative_ui specifications (<500px height, gstatic Tailwind,
    host semantic tokens, static charts, and prompt actions).
    """
    active_model = analysis.get("latest_model", model_name)
    active_max = analysis.get("latest_max_tokens", max_tokens) or 1_000_000
    active_tokens = analysis.get(
        "active_context_tokens", analysis.get("total_tokens", 0)
    )
    active_pct = min(100.0, (active_tokens / active_max * 100)) if active_max else 0.0
    cost = analysis.get("billed_cost", 0.0)

    # Status level & colors
    if active_pct >= 80.0:
        status_label = "Critical"
        status_badge_class = "bg-rose-500/20 text-rose-400 border-rose-500/30"
    elif active_pct >= 60.0:
        status_label = "Elevated"
        status_badge_class = "bg-amber-500/20 text-amber-400 border-amber-500/30"
    else:
        status_label = "Optimal"
        status_badge_class = "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"

    # Category segments for stacked capacity bar
    cat_stats = analysis.get("category_stats", {})
    total_chars = analysis.get("total_chars", 1) or 1

    # Aggregate categories into core buckets
    bucket_colors = {
        "Tools & Results": ("#f59e0b", 0),  # amber
        "Agent Response": ("#6366f1", 0),  # indigo
        "Thinking": ("#a855f7", 0),  # purple
        "User Input": ("#06b6d4", 0),  # cyan
        "System & Other": ("#64748b", 0),  # slate
    }

    for cat_name, chars in cat_stats.items():
        c_lower = cat_name.lower()
        if "tool" in c_lower:
            color, prev_c = bucket_colors["Tools & Results"]
            bucket_colors["Tools & Results"] = (color, prev_c + chars)
        elif "think" in c_lower:
            color, prev_c = bucket_colors["Thinking"]
            bucket_colors["Thinking"] = (color, prev_c + chars)
        elif "user" in c_lower:
            color, prev_c = bucket_colors["User Input"]
            bucket_colors["User Input"] = (color, prev_c + chars)
        elif "agent" in c_lower or "planner" in c_lower or "assistant" in c_lower:
            color, prev_c = bucket_colors["Agent Response"]
            bucket_colors["Agent Response"] = (color, prev_c + chars)
        else:
            color, prev_c = bucket_colors["System & Other"]
            bucket_colors["System & Other"] = (color, prev_c + chars)

    # Active memory boundary & compaction info
    steps = analysis.get("steps", [])
    active_step_min = steps[0]["step_index"] if steps else 0
    active_step_max = steps[-1]["step_index"] if steps else 0

    compaction_detected = False
    pruned_count = 0
    pruned_tokens = 0

    if diff_res is not None:
        compaction_detected = getattr(diff_res, "compaction_detected", False)
        pruned_steps = getattr(diff_res, "pruned_step_indices", [])
        pruned_count = len(pruned_steps)
        pruned_tokens = getattr(diff_res, "removed_tokens", 0)

    # Top consumers
    top_steps = sorted(steps, key=lambda s: s.get("characters", 0), reverse=True)[:3]

    # Generate segments HTML
    bar_segments_html = []
    legend_html = []

    for bucket_name, (color, chars) in bucket_colors.items():
        if chars <= 0:
            continue
        pct_of_context = (chars / total_chars) * active_pct
        toks = int(chars / CHARS_PER_TOKEN)
        bar_segments_html.append(
            f'<div style="width: {pct_of_context:.2f}%; background-color: {color};" '
            f'title="{bucket_name}: ~{format_tokens(toks)} ({pct_of_context:.1f}%)"></div>'
        )
        legend_html.append(
            f'<div class="flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">'
            f'<span class="w-2 h-2 rounded-full shrink-0" style="background-color: {color};"></span>'
            f'<span class="truncate">{bucket_name}</span>'
            f'<span class="font-mono text-[var(--foreground)] ml-auto">~{format_tokens(toks)}</span>'
            f"</div>"
        )

    # Top steps HTML
    top_steps_html = []
    for s in top_steps:
        s_idx = s.get("step_index", 0)
        s_tool = html.escape(str(s.get("tool_or_source", "Step")))
        s_toks = format_tokens(s.get("est_tokens", 0))
        s_detail = html.escape(str(s.get("detail", ""))[:48])
        top_steps_html.append(
            f'<div class="flex items-center justify-between text-xs py-1 border-b border-[var(--border)] last:border-b-0">'
            f'<div class="flex items-center gap-1.5 truncate">'
            f'<span class="font-mono text-[var(--muted-foreground)]">#{s_idx}</span>'
            f'<span class="font-medium text-[var(--foreground)] truncate">{s_tool}</span>'
            f"{f'<span class="text-[var(--muted-foreground)] truncate">({s_detail})</span>' if s_detail else ''}"
            f"</div>"
            f'<span class="font-mono font-medium text-[var(--foreground)] ml-2 shrink-0">~{s_toks}</span>'
            f"</div>"
        )

    # Compaction banner HTML
    compaction_banner_html = ""
    if compaction_detected:
        compaction_banner_html = f"""
        <div class="flex items-center justify-between bg-amber-500/10 border border-amber-500/30 rounded-lg px-2.5 py-1.5 text-xs">
          <div class="flex items-center gap-1.5 text-amber-400">
            <span class="font-semibold">Memory Compaction</span>
            <span class="text-amber-300/80">Pruned {pruned_count} historical steps (~{format_tokens(pruned_tokens)} freed)</span>
          </div>
          <span class="font-mono text-amber-400/90 text-[11px]">Active: #{active_step_min}–#{active_step_max}</span>
        </div>
        """
    else:
        compaction_banner_html = f"""
        <div class="flex items-center justify-between text-[11px] text-[var(--muted-foreground)] px-1">
          <span>Active Context Window: <span class="font-mono text-[var(--foreground)]">Steps #{active_step_min}–#{active_step_max}</span></span>
          <span>Billed Value: <span class="font-mono text-[var(--foreground)]">${cost:,.4f}</span></span>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    /* Invariant: Disable all animations for static visual rendering */
    *, *::before, *::after {{
      transition: none !important;
      animation: none !important;
    }}
  </style>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-2 font-sans select-none">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-3.5 shadow-sm space-y-3 max-w-lg mx-auto">
    <!-- Header: Model & Status -->
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 truncate">
        <span class="font-semibold text-sm text-[var(--foreground)] truncate">{html.escape(active_model)}</span>
        <span class="text-xs text-[var(--muted-foreground)] font-mono">({format_tokens(active_max)} limit)</span>
      </div>
      <div class="flex items-center gap-1.5 shrink-0">
        <span class="px-2 py-0.5 text-xs font-medium rounded-full border {status_badge_class}">
          {status_label}: {active_pct:.1f}%
        </span>
      </div>
    </div>

    <!-- Multi-Segment Capacity Bar -->
    <div class="space-y-1">
      <div class="flex justify-between text-xs text-[var(--muted-foreground)] font-mono">
        <span>{format_tokens(active_tokens)} tokens</span>
        <span>{format_tokens(max(0, active_max - active_tokens))} free</span>
      </div>
      <div class="w-full h-2.5 bg-[var(--sidebar)] rounded-full overflow-hidden flex border border-[var(--border)]">
        {"".join(bar_segments_html)}
      </div>
    </div>

    <!-- Category Breakdown Legend -->
    <div class="grid grid-cols-2 gap-x-4 gap-y-1 py-1 border-y border-[var(--border)]">
      {"".join(legend_html)}
    </div>

    <!-- Memory Boundary / Compaction Status -->
    {compaction_banner_html}

    <!-- Top Token Consuming Steps -->
    <div class="space-y-1">
      <div class="text-[11px] font-medium text-[var(--muted-foreground)] uppercase tracking-wider">Top Consuming Steps</div>
      <div class="divide-y divide-[var(--border)] bg-[var(--sidebar)]/50 rounded-lg p-2 border border-[var(--border)]">
        {"".join(top_steps_html)}
      </div>
    </div>

    <!-- Interactive Prompt Actions (v2.16.0) -->
    <div class="pt-1 flex flex-wrap items-center gap-1.5">
      <button onclick="triggerPrompt('Audit redundant file reads and suggest ways to prune context')"
              class="px-2.5 py-1 text-xs font-medium rounded-lg bg-[var(--secondary)] text-[var(--secondary-foreground)] hover:opacity-85 border border-[var(--border)]">
        Audit Redundant Reads
      </button>
      <button onclick="triggerPrompt('Summarize our progress so far and current state so we can reset into a clean conversation')"
              class="px-2.5 py-1 text-xs font-medium rounded-lg bg-[var(--secondary)] text-[var(--secondary-foreground)] hover:opacity-85 border border-[var(--border)]">
        Summarize &amp; Reset
      </button>
      <button onclick="triggerPrompt('Explain what consumed the most tokens in the largest step of this conversation')"
              class="px-2.5 py-1 text-xs font-medium rounded-lg bg-[var(--secondary)] text-[var(--secondary-foreground)] hover:opacity-85 border border-[var(--border)]">
        Inspect Top Step
      </button>
    </div>
  </div>

  <script>
    function triggerPrompt(text) {{
      try {{
        if (window.parent && window.parent !== window) {{
          window.parent.postMessage({{ type: 'prompt', text: text }}, '*');
          window.parent.postMessage({{ action: 'set_prompt', prompt: text }}, '*');
        }}
      }} catch (e) {{}}
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text);
      }}
    }}
  </script>
</body>
</html>
"""
