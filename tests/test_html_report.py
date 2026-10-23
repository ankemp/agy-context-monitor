from context_monitor.html_report import generate_html_report, format_tokens
from context_monitor.diff import DiffResult


def test_format_tokens():
    assert format_tokens(500) == "500"
    assert format_tokens(1_500) == "1.5k"
    assert format_tokens(25_400) == "25.4k"
    assert format_tokens(1_000_000) == "1.00M"
    assert format_tokens(2_500_000) == "2.50M"


def test_generate_html_report_structure():
    analysis = {
        "latest_model": "Gemini 3.7 Flash",
        "latest_max_tokens": 1_000_000,
        "total_tokens": 120_000,
        "active_context_tokens": 120_000,
        "total_chars": 456_000,
        "billed_cost": 0.045,
        "billed_input_tokens": 110_000,
        "billed_output_tokens": 10_000,
        "category_stats": {
            "Tool Output: File Contents (`view_file`)": 200_000,
            "Agent Reasoning & Text": 150_000,
            "User Requests": 50_000,
            "System: Ephemeral Injections": 56_000,
        },
        "steps": [
            {
                "step_index": 0,
                "tool_or_source": "User",
                "characters": 50_000,
                "est_tokens": 13_157,
                "detail": "Initial user prompt",
            },
            {
                "step_index": 1,
                "tool_or_source": "view_file",
                "characters": 200_000,
                "est_tokens": 52_631,
                "detail": "File: src/context_monitor/db.py",
            },
            {
                "step_index": 2,
                "tool_or_source": "Agent",
                "characters": 150_000,
                "est_tokens": 39_473,
                "detail": "Planning next steps",
            },
        ],
    }

    html_out = generate_html_report(
        cid="test-conversation-123",
        model_name="Gemini 3.7 Flash",
        max_tokens=1_000_000,
        analysis=analysis,
    )

    # 1. Must be a self-contained HTML document
    assert "<!DOCTYPE html>" in html_out
    assert "</html>" in html_out

    # 2. Must use allowed gstatic Tailwind CDN per Antigravity generative_ui spec
    assert "https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js" in html_out

    # 3. Must enforce static chart rule (transition: none !important; animation: none !important;)
    assert "transition: none !important;" in html_out
    assert "animation: none !important;" in html_out

    # 4. Must use Antigravity host semantic theme variables
    for var_name in [
        "var(--card)",
        "var(--foreground)",
        "var(--border)",
        "var(--muted-foreground)",
        "var(--sidebar)",
    ]:
        assert var_name in html_out

    # 5. Must contain prompt action buttons and triggerPrompt function
    assert "triggerPrompt" in html_out
    assert "Audit Redundant Reads" in html_out
    assert "Summarize &amp; Reset" in html_out
    assert "Inspect Top Step" in html_out
    assert "window.parent.postMessage" in html_out

    # 6. Status check: 120k / 1M = 12% -> Optimal
    assert "Optimal: 12.0%" in html_out

    # 7. Default active boundary check when no compaction
    assert "Active Context Window:" in html_out
    assert "Steps #0–#2" in html_out


def test_generate_html_report_status_thresholds():
    base_analysis = {
        "latest_model": "Gemini Pro",
        "latest_max_tokens": 100_000,
        "total_chars": 100_000,
        "steps": [],
        "category_stats": {},
    }

    # Optimal: <60%
    base_analysis["active_context_tokens"] = 50_000
    out_opt = generate_html_report("cid", "Gemini Pro", 100_000, base_analysis)
    assert "Optimal: 50.0%" in out_opt

    # Elevated: 60%-79%
    base_analysis["active_context_tokens"] = 65_000
    out_elev = generate_html_report("cid", "Gemini Pro", 100_000, base_analysis)
    assert "Elevated: 65.0%" in out_elev

    # Critical: >=80%
    base_analysis["active_context_tokens"] = 85_000
    out_crit = generate_html_report("cid", "Gemini Pro", 100_000, base_analysis)
    assert "Critical: 85.0%" in out_crit


def test_generate_html_report_compaction_banner():
    analysis = {
        "latest_model": "Claude 3.7 Sonnet",
        "latest_max_tokens": 200_000,
        "active_context_tokens": 80_000,
        "total_tokens": 80_000,
        "total_chars": 304_000,
        "steps": [
            {
                "step_index": 10,
                "tool_or_source": "User",
                "characters": 10_000,
                "est_tokens": 2600,
            },
            {
                "step_index": 11,
                "tool_or_source": "Agent",
                "characters": 20_000,
                "est_tokens": 5200,
            },
        ],
        "category_stats": {},
    }

    diff_res = DiffResult(
        added=[],
        removed=[{"hash": "h1"}, {"hash": "h2"}],
        common=[],
        compaction_detected=True,
        net_token_delta=-45000,
        removed_tokens=45000,
        added_tokens=0,
        pruned_step_indices=[1, 2, 3, 4],
        summary="Pruned 4 steps",
    )

    html_out = generate_html_report(
        "cid", "Claude 3.7 Sonnet", 200_000, analysis, diff_res=diff_res
    )

    assert "Memory Compaction" in html_out
    assert "Pruned 4 historical steps (~45.0k freed)" in html_out
    assert "Active: #10–#11" in html_out


def test_generate_html_report_includes_static_velocity_telemetry():
    analysis = {
        "latest_model": "Gemini 3.8 Flash",
        "latest_max_tokens": 1_000_000,
        "total_tokens": 60_000,
        "active_context_tokens": 60_000,
        "total_chars": 228_000,
        "billed_cost": 1.25,
        "billed_input_tokens": 240_000,
        "billed_output_tokens": 30_000,
        "active_agent_seconds": 180,
        "active_pairing_seconds": 600,
        "category_stats": {"User Requests": 228_000},
        "steps": [],
    }
    html_out = generate_html_report("cid-vel", "Gemini 3.8 Flash", 1_000_000, analysis)
    assert "tok/min" in html_out
    assert "tok/hr" in html_out
    assert "Context Fill TPM" in html_out
    assert "Billed TPM / TPH" in html_out
    assert "transition: none !important;" in html_out
    assert "animation: none !important;" in html_out


def test_generate_html_report_with_precomputed_velocity_and_gauge_clamping():
    """Verifies precomputed velocity is respected, bullet bar clamps at 100%, and None values are safely handled."""
    # Case 1: Pre-computed high velocity exceeding DEFAULT_PEAK_CONTEXT_TPM (10,000)
    analysis = {
        "latest_model": "Gemini 3.8 Flash",
        "latest_max_tokens": 1_000_000,
        "total_tokens": 50_000,
        "active_context_tokens": 50_000,
        "total_chars": 190_000,
        "billed_cost": 2.0,
        "billed_input_tokens": 400_000,
        "billed_output_tokens": 50_000,
        "active_agent_seconds": 60,
        "active_pairing_seconds": 120,
        "velocity": {
            "active_context_tpm": 50_000.0,
            "active_billed_tpm": 450_000.0,
            "active_billed_tph": 27_000_000.0,
            "pairing_billed_tph": 13_500_000.0,
        },
        "category_stats": {"User Requests": 190_000},
        "steps": [],
    }
    html_out = generate_html_report("cid-pre", "Gemini 3.8 Flash", 1_000_000, analysis)
    assert "50.0k tok/min" in html_out
    assert "450.0k tok/min · 27.00M tok/hr" in html_out
    # Clamped to 100.0%
    assert "width: 100.0%;" in html_out

    # Case 2: Velocity with None values
    analysis_none = {
        "latest_model": "Gemini 3.8 Flash",
        "latest_max_tokens": 1_000_000,
        "total_tokens": 10_000,
        "active_context_tokens": 10_000,
        "total_chars": 38_000,
        "billed_cost": 0.1,
        "billed_input_tokens": 10_000,
        "billed_output_tokens": 1_000,
        "velocity": {
            "active_context_tpm": None,
            "active_billed_tpm": None,
            "active_billed_tph": None,
        },
        "category_stats": {},
        "steps": [],
    }
    html_out_none = generate_html_report(
        "cid-none", "Gemini 3.8 Flash", 1_000_000, analysis_none
    )
    assert "0 tok/min" in html_out_none
    assert "width: 0.0%;" in html_out_none
